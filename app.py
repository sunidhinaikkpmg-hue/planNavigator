from typing import TypedDict, Annotated, List, Union, Any, Dict
import re
from langgraph.graph import StateGraph, END
from langchain_core.tools import Tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.checkpoint.memory import MemorySaver
import os
import operator
from sqlalchemy import create_engine, text
import sqlalchemy
import pg8000
from google.cloud.sql.connector import Connector, IPTypes
from google.cloud import dlp_v2

# Add these imports for RAGPipeline
from google.cloud import aiplatform
from vertexai.generative_models import GenerativeModel
from langchain_google_vertexai import VertexAIEmbeddings
import vertexai
import json

from PlanHealthLLM import RAGPipeline

from dotenv import load_dotenv
load_dotenv()


# Define the state structure
class AgentState(TypedDict):
    query: str
    user_id: str
    messages: Annotated[list, operator.add]
    sql_results: dict
    recommendations: list
    vector_results: list
    next_agent: str
    final_response: str
    error: str

class LangGraphOrchestrator:
    VALID_ROUTE_KEYS = {"sql", "recommend", "simulate", "multi"}

    def __init__(self, config):
        self.config = config
        
        # Retrieve API key from environment variable
        google_api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not google_api_key:
            raise ValueError("Google API key not found. Please set the GOOGLE_API_KEY or GEMINI_API_KEY environment variable.")
            
        # Initialize DLP Client
        self.project_id = os.getenv("GCP_PROJECT_ID")
        if self.project_id:
            self.dlp_client = dlp_v2.DlpServiceClient()
        else:
            # Log a critical warning or raise an exception if PII protection is mandatory
            print("CRITICAL: GCP_PROJECT_ID not set. DLP guardrails are currently BYPASSED.")
            
        # Rename to _llm to discourage direct usage bypassing the guardrails
        self._llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", api_key=google_api_key)
        
        # Database connection setup
        # Database connection setup (supports Cloud SQL Connector via INSTANCE_CONNECTION_NAME)
        db_url = os.getenv("DATABASE_URL")
        instance_connection_name = os.getenv("INSTANCE_CONNECTION_NAME")

        if db_url:
            # Standard URL connection (local/dev, or any reachable host)
            self.engine = create_engine(db_url)

        elif instance_connection_name:
            # Cloud SQL Python Connector (recommended for Cloud Run/GCP)
            db_user = os.getenv("DB_USER", "postgres")
            db_pass = os.getenv("DB_PASSWORD", "")
            db_name = os.getenv("DB_NAME", "postgres")

            # Choose IP type: PUBLIC if private IP not enabled; PRIVATE if you enable it later
            ip_type = IPTypes.PRIVATE if os.getenv("PRIVATE_IP") else IPTypes.PUBLIC

            # Create the connector once and reuse it
            self.connector = Connector(refresh_strategy="LAZY")

            def getconn() -> pg8000.dbapi.Connection:
                return self.connector.connect(
                    instance_connection_name,   # "project:region:instance"
                    "pg8000",
                    user=db_user,
                    password=db_pass,
                    db=db_name,
                    ip_type=ip_type,
                )

            # SQLAlchemy engine using connector-provided connections (pooling handled by SQLAlchemy)
            self.engine = sqlalchemy.create_engine(
                "postgresql+pg8000://",
                creator=getconn,
                pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
                max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "2")),
                pool_timeout=int(os.getenv("DB_POOL_TIMEOUT", "30")),
                pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "1800")),
            )

        else:
            # Fallback: manual host/port (requires network access + allowlisting if public IP)
            user = os.getenv("DB_USER", "postgres")
            password = os.getenv("DB_PASSWORD", "")
            host = os.getenv("DB_HOST", "localhost")
            port = os.getenv("DB_PORT", "5432")
            dbname = os.getenv("DB_NAME", "postgres")
            db_url = f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
            self.engine = create_engine(db_url)

        # Initialize tools for each agent
        self.sql_tool = Tool(
            name="sql_query",
            func=self._execute_sql,
            description="Query PostgreSQL database"
        )
        
        self.recommendation_tool = Tool(
            name="ml_recommend",
            func=self._get_recommendations,
            description="Get ML-based recommendations"
        )
        
        self.vector_tool = Tool(
            name="vector_search",
            func=self._vector_search,
            description="Search vector database"
        )

        vertexai.init(
            project=os.getenv("GCP_PROJECT_ID", "adv-lighthouse-plnv"),
            location=os.getenv("GCP_LOCATION", "us-central1")
        )
        
        # Initialize RAG Pipeline
        self.rag_pipeline = self._initialize_rag_pipeline()
        
        # Update vector_tool to use RAGPipeline
        self.vector_tool = Tool(
            name="vector_search",
            func=self._vector_search_with_rag,
            description="Search vector database and generate comprehensive answers"
        )
        
        # Build the graph
        self.app = self._build_graph()

    def _initialize_rag_pipeline(self):
        """Initialize the RAGPipeline with your configuration"""
        # Use environment variables or config
        index_endpoint_name = os.getenv(
            "VECTOR_INDEX_ENDPOINT",
            "projects/127510819679/locations/us-central1/indexEndpoints/3776015399875772416"
        )
        bucket_name = os.getenv("VECTOR_BUCKET", "plnv-vector-bucket")
        metadata_file = os.getenv(
            "VECTOR_METADATA_FILE",
            "metadata/metadata_20260512_060948.json"
        )
        return RAGPipeline(index_endpoint_name, bucket_name, metadata_file)
    
    def _build_graph(self):
        # Create workflow
        workflow = StateGraph(AgentState)
        
        # Add nodes
        workflow.add_node("router", self.route_query)
        workflow.add_node("sql_agent", self.sql_agent)
        workflow.add_node("recommendation_agent", self.recommendation_agent)
        workflow.add_node("simulator_agent", self.simulator_agent)
        workflow.add_node("synthesizer", self.synthesize_response)
        
        # Add edges
        workflow.set_entry_point("router")
        
        # Conditional routing based on query analysis
        workflow.add_conditional_edges(
            "router",
            self.determine_next_agent,
            {
                "sql": "sql_agent",
                "recommend": "recommendation_agent",
                "simulate": "simulator_agent",
                "multi": "sql_agent"  # Start with SQL for multi-agent
            }
        )
        
        # SQL agent can route to recommendation or end
        workflow.add_conditional_edges(
            "sql_agent",
            self.after_sql,
            {
                "recommend": "recommendation_agent",
                "synthesize": "synthesizer",
                "end": END
            }
        )
        
        # Recommendation agent routes
        workflow.add_conditional_edges(
            "recommendation_agent",
            self.after_recommendation,
            {
                "simulate": "simulator_agent",
                "synthesize": "synthesizer",
                "end": END
            }
        )
        
        # Simulator agent always goes to synthesizer
        workflow.add_edge("simulator_agent", "synthesizer")
        
        # Synthesizer ends
        workflow.add_edge("synthesizer", END)
        
        # Compile with memory
        memory = MemorySaver()
        app = workflow.compile(checkpointer=memory)
        
        return app
    
    def route_query(self, state: AgentState) -> AgentState:
        """Analyze query and determine routing - enhanced for RAG"""
        query = state["query"]
        
        # Use LLM to analyze query intent
        analysis_prompt = f"""
        Analyze this query and determine which agents are needed:
        Query: {query}
        
        Respond with one of: 'sql', 'recommend', 'simulate', 'multi'
        - sql: Needs database query (participant data, balances, etc.)
        - recommend: Needs ML recommendations or predictions
        - simulate: Needs document search about plans, policies, or general financial advice
        - multi: Needs multiple agents
        
        If the query asks about:
        - Retirement plans, investment options, plan health → simulate
        - Specific participants, balances, contributions → sql
        - Predictions, optimal amounts, forecasting → recommend
        - Combination of above → multi
        
        Also identify if user_id is mentioned.
        """
        
        response = self._invoke_llm_with_guardrails(analysis_prompt)
        intent = self._normalize_route_intent(response.content)
        
        print(f"Routing intent: {intent}")
        
        update = {"messages": [f"Query analyzed. Intent: {intent}"], "next_agent": intent}
        
        # Extract user_id if mentioned
        if "user" in query.lower():
            user_id = self._extract_user_id(query)
            if user_id:
                update["user_id"] = user_id
        
        return update
    
    def determine_next_agent(self, state: AgentState) -> str:
        """Determine which agent to run based on router analysis"""
        next_agent = state.get("next_agent", "sql")
        if next_agent not in self.VALID_ROUTE_KEYS:
            return "sql"
        return next_agent

    def _normalize_route_intent(self, raw_intent: str) -> str:
        """Coerce LLM routing output to one of the graph's valid branch keys."""
        normalized = raw_intent.strip().lower()
        if normalized in self.VALID_ROUTE_KEYS:
            return normalized

        match = re.search(r"\b(sql|recommend|simulate|multi)\b", normalized)
        if match:
            return match.group(1)

        return "sql"
    
    def sql_agent(self, state: AgentState) -> AgentState:
        """Execute SQL queries"""
        try:
            query = state["query"]
            
            # Generate SQL query using LLM
            sql_prompt = f"""
            Generate a PostgreSQL query to answer this request: {query}
            Ignore the columns if it has redacted from the Guardrail.
            
            Available Schema:
            - plans (plan_id, name, sponsor_id, plan_type)
            - sponsors (id, industry)
            - participants (id, plan_id, compensation, status, birth_date, hire_date)
            - participant_balances (participant_id, balance_amount, as_of_date)
            - contributions (participant_id, amount, contribution_date, type)
            - investment_options (plan_id, option_name, asset_class)
            
            Instructions:
            - Use explicit JOINs when data spans tables (e.g., plans and participants join on plan_id).
            - For "401(k)" plan types, filter using plans.plan_type = '401(k)'.
            - Return ONLY the raw SQL query. Do not include markdown fences or explanation.
            """
            
            sql_query = self._invoke_llm_with_guardrails(sql_prompt).content

            print(f"Generated SQL: {sql_query}")
            
            # Execute query
            results = self._execute_sql(sql_query)
            
            update = {
                "sql_results": results,
                "messages": [f"SQL executed: {sql_query}"]
            }
            
            # Determine if we need recommendation agent
            if any(word in query.lower() for word in ["recommend", "predict"]) and results.get("data"):
                update["next_agent"] = "recommend"
            else:
                update["next_agent"] = "synthesize"
            
            return update
                
        except Exception as e:
            return {"error": str(e), "next_agent": "synthesize"}
    
    def recommendation_agent(self, state: AgentState) -> AgentState:
        """Generate ML recommendations"""
        try:
            # Use SQL results if available
            context = {
                "user_id": state.get("user_id"),
                "sql_data": state.get("sql_results", {})
            }
            
            recommendations = self._get_recommendations(context)
            
            update = {
                "recommendations": recommendations,
                "messages": ["ML recommendations generated"]
            }
            
            # Check if we need vector search
            if "similar" in state["query"].lower():
                update["next_agent"] = "simulate"
            else:
                update["next_agent"] = "synthesize"
            
            return update
                
        except Exception as e:
            return {"error": str(e), "next_agent": "synthesize"}
    
    def simulator_agent(self, state: AgentState) -> AgentState:
        """Vector database search and chat using RAGPipeline"""
        try:
            query = state["query"]
            
            # Check if we need to incorporate SQL results or recommendations
            enhanced_query = self._enhance_query_with_context(state)
            
            # Use RAGPipeline to get comprehensive answer
            rag_result = self.rag_pipeline.answer_question(
                enhanced_query,
                num_sources=5
            )
            
            # Extract relevant information for state
            vector_results = {
                "answer": rag_result["answer"],
                "sources": [
                    {
                        "id": s["id"],
                        "source": s["source"],
                        "relevance": s["distance"],
                        "preview": s["text"][:200] + "..." if len(s["text"]) > 200 else s["text"]
                    }
                    for s in rag_result["sources"]
                ],
                "num_sources": rag_result["num_sources"]
            }
            
            return {
                "vector_results": vector_results,
                "messages": [f"RAG search completed with {rag_result['num_sources']} sources"]
            }
            
        except Exception as e:
            return {
                "error": f"RAG Pipeline error: {str(e)}",
                "vector_results": {},
                "messages": [f"Error in RAG pipeline: {str(e)}"]
            }
    
    def _enhance_query_with_context(self, state: AgentState) -> str:
        """Enhance the query with context from previous agents"""
        query = state["query"]
        
        # Add context from SQL results if available
        if state.get("sql_results") and state["sql_results"].get("data"):
            query += f"\n\nAdditional context from database: {json.dumps(state['sql_results']['data'][:3])}"
        
        # Add context from recommendations if available
        if state.get("recommendations"):
            query += f"\n\nML recommendations: {state['recommendations'][:3]}"
        
        return query

    def _vector_search_with_rag(self, context: dict) -> list:
        """Vector search using RAGPipeline"""
        query = context.get("query", "")
        
        # Use RAGPipeline for search
        try:
            result = self.rag_pipeline.answer_question(query, num_sources=5)
            
            # Format results for compatibility
            return [
                {
                    "content": result["answer"],
                    "sources": result["sources"],
                    "type": "rag_response"
                }
            ]
        except Exception as e:
            return [{"error": str(e), "type": "rag_error"}]


    
    def synthesize_response(self, state: AgentState) -> AgentState:
        """Synthesize final response from all agent outputs including RAG"""
        
        # Check if we have RAG results with comprehensive answers
        if state.get("vector_results") and state["vector_results"].get("answer"):
            # RAG already provides comprehensive answers, so we can use it directly
            rag_answer = state["vector_results"]["answer"]
            
            # Combine with other results if needed
            synthesis_prompt = f"""
            Combine these results into a comprehensive response:
            
            Original Query: {state['query']}
            
            Database Results: {self._format_sql_results(state.get('sql_results', {}))}
            
            ML Recommendations: {state.get('recommendations', 'None')}
            
            RAG Analysis: {rag_answer}
            
            Sources Used: {len(state['vector_results'].get('sources', []))} documents
            
            Provide a unified, clear response that leverages all available information.
            """
        else:
            # Fallback to original synthesis
            synthesis_prompt = f"""
            Create a comprehensive response based on these results:
            
            Original Query: {state['query']}
            SQL Results: {state.get('sql_results', 'None')}
            ML Recommendations: {state.get('recommendations', 'None')}
            Messages: {state.get('messages', [])}
            
            Provide a clear, helpful response to the user.
            """
        
        final_response = self._invoke_llm_with_guardrails(synthesis_prompt).content
        return {"final_response": final_response}

    def _format_sql_results(self, sql_results: dict) -> str:
        """Format SQL results for synthesis"""
        if not sql_results or "error" in sql_results:
            return "No database results"
        
        data = sql_results.get("data", [])
        if not data:
            return "Empty result set"
        
        # Show first few rows
        return f"{len(data)} rows found. Sample: {json.dumps(data[:3], indent=2)}"

    
    def after_sql(self, state: AgentState) -> str:
        """Determine next step after SQL agent"""
        return state.get("next_agent", "end")
    
    def after_recommendation(self, state: AgentState) -> str:
        """Determine next step after recommendation agent"""
        return state.get("next_agent", "end")
    
    def _apply_redaction(self, text: str) -> str:
        """Redact PII from text using Google Cloud DLP."""
        if not isinstance(text, str) or not text.strip():
            return text

        if not hasattr(self, 'dlp_client') or not self.project_id:
            return text

        parent = f"projects/{self.project_id}/locations/global"
        
        # Configure the types of info to detect
        info_types = [{"name": "EMAIL_ADDRESS"}, {"name": "PHONE_NUMBER"}, {"name": "PERSON_NAME"}]#, {"name": "DATE_OF_BIRTH"}]
        
        inspect_config = {"info_types": info_types}
        
        # Configure the de-identification transformation (replacement)
        deidentify_config = {
            "info_type_transformations": {
                "transformations": [
                    {
                        "primitive_transformation": {
                            "replace_with_info_type_config": {}
                        }
                    }
                ]
            }
        }
        
        try:
            response = self.dlp_client.deidentify_content(
                request={
                    "parent": parent,
                    "deidentify_config": deidentify_config,
                    "inspect_config": inspect_config,
                    "item": {"value": text},
                }
            )
            return response.item.value
        except Exception as e:
            print(f"DLP Redaction failed: {e}")
            return text

    def _invoke_llm_with_guardrails(self, prompt: str):
        """Invoke LLM with redaction guardrails on input and output."""
        redacted_prompt = self._apply_redaction(prompt)
        print(f"Redacted Prompt: {redacted_prompt}")
        response = self._llm.invoke(redacted_prompt)
        if hasattr(response, "content") and isinstance(response.content, str):
            response.content = self._apply_redaction(response.content)
        return response

    # Tool implementations
    def _execute_sql(self, query: str) -> dict:
        """Execute SQL query against PostgreSQL"""
        # Improved extraction: strip markdown and conversational text
        clean_query = query.strip()
        clean_query = re.sub(r"```sql|```", "", clean_query).strip()
        
        # Isolate the core query keywords if the model included preamble text
        sql_match = re.search(r"(SELECT|INSERT|UPDATE|DELETE|WITH)\b.*", clean_query, re.IGNORECASE | re.DOTALL)
        if sql_match:
            clean_query = sql_match.group(0).rstrip(';') + ';'
        
        try:
            with self.engine.connect() as connection:
                result = connection.execute(text(clean_query))
                # Convert ResultProxy to list of dicts using mappings()
                rows = [dict(row) for row in result.mappings()]
                return {"data": rows, "count": len(rows)}
        except Exception as e:
            return {"error": str(e), "query": clean_query}
    
    def _get_recommendations(self, context: dict) -> list:
        """Get ML-based recommendations"""
        # Implementation here
        pass
    
    def _vector_search(self, context: dict) -> list:
        """Search vector database"""
        # Implementation here
        pass
    
    def _extract_user_id(self, query: str) -> str:
        """Extract user ID from query"""
        # Implementation here
        pass
    
    async def process_query(self, query: str, thread_id: str = "default"):
        """Process a query through the graph"""
        # Minimal input allowing LangGraph to merge with historical state from the checkpointer
        input_data = {
            "query": query,
            "messages": []
        }
        
        # Run the graph with conversation memory
        config = {"configurable": {"thread_id": thread_id}}
        result = await self.app.ainvoke(input_data, config)
        
        return result

# Advanced LangGraph with parallel execution
class AdvancedLangGraphOrchestrator(LangGraphOrchestrator):
    def _build_graph(self):
        workflow = StateGraph(AgentState)
        
        # Add nodes
        workflow.add_node("router", self.route_query)
        # workflow.add_node("parallel_executor", self.parallel_execute)
        workflow.add_node("sql_agent", self.sql_agent)
        workflow.add_node("recommendation_agent", self.recommendation_agent)
        workflow.add_node("simulator_agent", self.simulator_agent)
        workflow.add_node("synthesizer", self.synthesize_response)
        
        # Entry point
        workflow.set_entry_point("router")
        
        # Routing logic
        workflow.add_conditional_edges(
            "router",
            self.routing_decision,
            {
                "single": "sql_agent",
                # "parallel": "parallel_executor",
                "sequential": "sql_agent"
            }
        )
        
        # Parallel execution node
        # workflow.add_edge("parallel_executor", "synthesizer")
        
        # Sequential paths
        workflow.add_conditional_edges(
            "sql_agent",
            lambda x: "recommendation_agent" if x.get("needs_ml") else "synthesizer"
        )
        
        workflow.add_edge("recommendation_agent", "synthesizer")
        workflow.add_edge("simulator_agent", "synthesizer")
        workflow.add_edge("synthesizer", END)
        
        return workflow.compile()
    
    def parallel_execute(self, state: AgentState) -> AgentState:
        """Execute multiple agents in parallel"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        agents_to_run = state.get("agents_to_run", [])
        
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {}
            
            if "sql" in agents_to_run:
                futures[executor.submit(self.sql_agent, state)] = "sql"
            if "recommend" in agents_to_run:
                futures[executor.submit(self.recommendation_agent, state)] = "recommend"
            if "simulate" in agents_to_run:
                futures[executor.submit(self.simulator_agent, state)] = "simulate"
            
            # Collect results
            for future in as_completed(futures):
                agent_name = futures[future]
                try:
                    result = future.result()
                    # Merge results back into state
                    state.update(result)
                except Exception as e:
                    state["messages"].append(f"Error in {agent_name}: {str(e)}")
        
        return state

# Usage example
async def main():
    config = {
        "sql_config": {...},
        "ml_config": {...},
        "vector_config": {
            "index_endpoint": "projects/.../indexEndpoints/...",
            "bucket": "plnv-vector-bucket",
            "metadata_file": "metadata/metadata_20260512_060948.json"
        }
    }
    
    orchestrator = LangGraphOrchestrator(config)
    
    # Query that uses RAG
    result1 = await orchestrator.process_query(
        "What are the key recommendations for improving plan health for 401k plans?",
        thread_id="session_123"
    )
    
    # Query that combines SQL and RAG
    result2 = await orchestrator.process_query(
        "Show me participants with low balances and explain what investment options might help them",
        thread_id="session_123"
    )
    
    print(result2["final_response"])

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())