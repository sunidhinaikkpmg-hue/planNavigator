from google.cloud import aiplatform
from vertexai.language_models import TextGenerationModel
from vertexai.generative_models import GenerativeModel
from langchain_google_vertexai import VertexAIEmbeddings
import json
from typing import List, Dict
import os
from dotenv import load_dotenv
load_dotenv()

# Initialize Vertex AI
aiplatform.init(
    project="adv-lighthouse-plnv",
    location="us-central1"
)

class RAGPipeline:
    def __init__(self, index_endpoint_name: str, bucket_name: str, metadata_file: str):
        self.endpoint = aiplatform.MatchingEngineIndexEndpoint(index_endpoint_name)
        self.bucket_name = bucket_name
        self.metadata_file = metadata_file
        self.embeddings = VertexAIEmbeddings(
            model_name="gemini-embedding-001",
            project="adv-lighthouse-plnv",
            location="us-central1"
        )
        # Initialize Gemini Pro for generation
        self.llm = GenerativeModel("gemini-2.5-flash")
        
    def search_documents(self, query: str, num_results: int = 5) -> List[Dict]:
        """Search for relevant documents"""
        # Generate query embedding
        query_embedding = self.embeddings.embed_query(query)
        
        # Search in vector index
        response = self.endpoint.find_neighbors(
            deployed_index_id="financial_products_deployed",
            queries=[query_embedding],
            num_neighbors=num_results
        )
        
        # Load metadata
        from google.cloud import storage
        client = storage.Client()
        bucket = client.bucket(self.bucket_name)
        blob = bucket.blob(self.metadata_file)
        metadata_dict = json.loads(blob.download_as_text())
        
        # Collect results with text
        results = []
        for neighbor in response[0]:
            metadata = metadata_dict.get(neighbor.id, {})
            results.append({
                "id": neighbor.id,
                "distance": neighbor.distance,
                "text": metadata.get("text", ""),
                "source": metadata.get("source", "")
            })
        
        return results
    
    def format_context(self, search_results: List[Dict]) -> str:
        """Format search results as context for LLM"""
        context_parts = []
        
        for i, result in enumerate(search_results, 1):
            context_parts.append(
                f"[Document {i} - Source: {result['source']}]\n"
                f"{result['text']}\n"
            )
        
        return "\n".join(context_parts)
    
    def generate_answer(self, query: str, context: str, system_prompt: str = None) -> str:
        """Generate answer using LLM with context"""
        
        if system_prompt is None:
            system_prompt = """You are a helpful financial advisor assistant. 
            Use the provided context to answer questions about financial plans and retirement products.
            Base your answers only on the provided context. If the information is not in the context, 
            say so clearly. Be specific and cite the source documents when possible."""
        
        prompt = f"""{system_prompt}

Context from financial documents:
{context}

Question: {query}

Please provide a comprehensive answer based on the context above. 
If specific recommendations are mentioned in the documents, include them.
"""
        
        # Generate response
        response = self.llm.generate_content(prompt)
        return response.text
    
    def answer_question(self, query: str, num_sources: int = 5) -> Dict:
        """Complete RAG pipeline: search + generate"""
        
        # 1. Search for relevant documents
        print(f"Searching for relevant information...")
        search_results = self.search_documents(query, num_sources)
        
        # 2. Format context
        context = self.format_context(search_results)
        
        # 3. Generate answer
        print(f"Generating answer based on {len(search_results)} sources...")
        answer = self.generate_answer(query, context)
        
        return {
            "query": query,
            "answer": answer,
            "sources": search_results,
            "num_sources": len(search_results)
        }
