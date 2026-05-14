import React, { useState, useEffect, useRef } from 'react';
import './App.css';

const App = () => {
  const [messages, setMessages] = useState([]);
  const [inputMessage, setInputMessage] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [sessionId, setSessionId] = useState(`session_${Date.now()}`);
  const [activeAgents, setActiveAgents] = useState([]);
  const [agentFlow, setAgentFlow] = useState([]);
  const messagesEndRef = useRef(null);

  // const BACKEND_URL = 'https://8080-cs-91efd324-e4aa-4a0c-8590-07cca07993fb.cs-asia-southeast1-bool.cloudshell.dev'; // Update this to your Flask backend URL
  // const BACKEND_URL = '';
  const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || '';

  const agents = {
    router: { name: 'Router', icon: '🚦', description: 'Analyzes query intent' },
    sql_agent: { name: 'SQL Agent', icon: '🗄️', description: 'Queries database' },
    recommendation_agent: { name: 'ML Agent', icon: '🤖', description: 'Generates recommendations' },
    simulator_agent: { name: 'RAG Agent', icon: '📚', description: 'Searches documents' },
    synthesizer: { name: 'Synthesizer', icon: '✨', description: 'Creates final response' }
  };

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const parseAgentFlow = (details) => {
    const flow = ['router']; // Always starts with router
    
    if (details?.messages) {
      details.messages.forEach(msg => {
        if (msg.includes('Intent: sql')) flow.push('sql_agent');
        else if (msg.includes('Intent: recommend')) flow.push('recommendation_agent');
        else if (msg.includes('Intent: simulate')) flow.push('simulator_agent');
        else if (msg.includes('SQL executed')) flow.push('sql_agent');
        else if (msg.includes('ML recommendations')) flow.push('recommendation_agent');
        else if (msg.includes('RAG search')) flow.push('simulator_agent');
      });
    }
    
    // Always ends with synthesizer
    flow.push('synthesizer');
    return flow;
  };

  const simulateAgentProgress = async (agentList) => {
    setActiveAgents([]);
    setAgentFlow([]);
    
    for (let i = 0; i < agentList.length; i++) {
      await new Promise(resolve => setTimeout(resolve, 500));
      setActiveAgents([agentList[i]]);
      setAgentFlow(prev => [...prev, agentList[i]]);
    }
    
    // Clear active agents after completion
    setTimeout(() => setActiveAgents([]), 1000);
  };

  // In src/App.js, update the sendMessage function to show errors in chat:

const sendMessage = async () => {
  if (!inputMessage.trim()) return;

  const userMessage = {
    type: 'user',
    content: inputMessage,
    timestamp: new Date().toLocaleTimeString()
  };

  setMessages(prev => [...prev, userMessage]);
  setInputMessage('');
  setIsLoading(true);

  // Add timeout wrapper
  const fetchWithTimeout = async (url, options, timeout = 30000) => {
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), timeout);
    
    try {
      const response = await fetch(url, {
        ...options,
        signal: controller.signal
      });
      clearTimeout(id);
      return response;
    } catch (error) {
      clearTimeout(id);
      throw error;
    }
  };

  try {
    // Debug message
    setMessages(prev => [...prev, {
      type: 'bot',
      content: `🔄 Sending request...`,
      timestamp: new Date().toLocaleTimeString()
    }]);

    const response = await fetchWithTimeout(`${BACKEND_URL}/query`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      credentials: 'include',
      body: JSON.stringify({
        query: inputMessage,
        thread_id: sessionId
      }),
    }, 30000); // 30 second timeout

    // Log response status
    setMessages(prev => [...prev, {
      type: 'bot',
      content: `📡 Got response: Status ${response.status}`,
      timestamp: new Date().toLocaleTimeString()
    }]);

    const text = await response.text();
    
    // Try to parse JSON
    let data;
    try {
      data = JSON.parse(text);
    } catch (e) {
      setMessages(prev => [...prev, {
        type: 'bot',
        content: `❌ Invalid JSON response: ${text.substring(0, 200)}...`,
        timestamp: new Date().toLocaleTimeString()
      }]);
      return;
    }

    // Display the response
    if (response.ok && data.response) {
      setMessages(prev => [...prev, {
        type: 'bot',
        content: data.response,
        timestamp: new Date().toLocaleTimeString(),
        details: data.details
      }]);
      
      // Animate agents after showing response
      if (data.details) {
        const flow = parseAgentFlow(data.details);
        simulateAgentProgress(flow);
      }
    } else {
      setMessages(prev => [...prev, {
        type: 'bot',
        content: `❌ Error: ${data.error || JSON.stringify(data)}`,
        timestamp: new Date().toLocaleTimeString()
      }]);
    }

  } catch (error) {
    if (error.name === 'AbortError') {
      setMessages(prev => [...prev, {
        type: 'bot',
        content: `❌ Request timeout after 30 seconds`,
        timestamp: new Date().toLocaleTimeString()
      }]);
    } else {
      setMessages(prev => [...prev, {
        type: 'bot',
        content: `❌ Error: ${error.message}`,
        timestamp: new Date().toLocaleTimeString()
      }]);
    }
  } finally {
    setIsLoading(false);
  }
};


// Add this function in your App component
const testConnection = async () => {
  setMessages(prev => [...prev, {
    type: 'bot',
    content: `Testing connection to ${BACKEND_URL}...`,
    timestamp: new Date().toLocaleTimeString()
  }]);

  try {
    const response = await fetch(`${BACKEND_URL}/health`);
    const data = await response.text();
    
    setMessages(prev => [...prev, {
      type: 'bot',
      content: `✅ Connection successful! Response: ${data}`,
      timestamp: new Date().toLocaleTimeString()
    }]);
  } catch (error) {
    setMessages(prev => [...prev, {
      type: 'bot',
      content: `❌ Connection failed! 
Error: ${error.message}
URL tried: ${BACKEND_URL}/health
Make sure Flask is running and URL is correct.`,
      timestamp: new Date().toLocaleTimeString()
    }]);
  }
};

// Add this button in your JSX (maybe near the example queries)
<button 
  onClick={testConnection}
  style={{ 
    backgroundColor: '#ff9800', 
    color: 'white',
    padding: '10px 20px',
    border: 'none',
    borderRadius: '5px',
    cursor: 'pointer',
    marginBottom: '10px'
  }}
>
  🔧 Test Backend Connection
</button>


  const handleKeyPress = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  return (
    <div className="app-container">
      <div className="header">
        <h1>🤖 Plan Navigator AI Assistant</h1>
        <div className="session-info">Session: {sessionId.slice(0, 15)}...</div>
      </div>

      <div className="main-content">
        {/* Agent Flow Visualization */}
        <div className="agent-flow-container">
          <h3>Agent Pipeline</h3>
          <div className="agent-flow">
            {Object.entries(agents).map(([key, agent]) => (
              <div 
                key={key} 
                className={`agent-node ${activeAgents.includes(key) ? 'active' : ''} ${agentFlow.includes(key) ? 'visited' : ''}`}
              >
                <div className="agent-icon">{agent.icon}</div>
                <div className="agent-name">{agent.name}</div>
                <div className="agent-description">{agent.description}</div>
              </div>
            ))}
          </div>
          {agentFlow.length > 1 && (
            <div className="flow-path">
              {agentFlow.map((agent, idx) => (
                <span key={idx}>
                  {agents[agent]?.name || agent}
                  {idx < agentFlow.length - 1 && ' → '}
                </span>
              ))}
            </div>
          )}
        </div>

        {/* Chat Interface */}
        <div className="chat-container">
          <div className="messages">
            {messages.map((message, index) => (
              <div key={index} className={`message ${message.type}`}>
                <div className="message-header">
                  <span className="message-sender">
                    {message.type === 'user' ? '👤 You' : '🤖 Assistant'}
                  </span>
                  <span className="message-time">{message.timestamp}</span>
                </div>
                <div className="message-content">
                  {message.content}
                </div>
                {message.details && (
                  <details className="message-details">
                    <summary>View Details</summary>
                    <pre>{JSON.stringify(message.details, null, 2)}</pre>
                  </details>
                )}
              </div>
            ))}
            {isLoading && (
              <div className="message bot loading">
                <div className="typing-indicator">
                  <span></span>
                  <span></span>
                  <span></span>
                </div>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>

          <div className="input-container">
            <textarea
              value={inputMessage}
              onChange={(e) => setInputMessage(e.target.value)}
              onKeyPress={handleKeyPress}
              placeholder="Ask me about plan health, participants, recommendations..."
              className="message-input"
              rows="2"
            />
            <button 
              onClick={sendMessage} 
              disabled={isLoading || !inputMessage.trim()}
              className="send-button"
            >
              {isLoading ? '⏳' : '📤'} Send
            </button>
          </div>
        </div>
      </div>

      <div className="example-queries">
        <h4>Try these examples:</h4>
        <div className="query-chips">
          <button onClick={() => setInputMessage("What's the overall health of our 401(k) plans?")}>
            Plan Health Analysis
          </button>
          <button onClick={() => setInputMessage("Show me participant balances for plan_id 123")}>
            SQL Query
          </button>
          <button onClick={() => setInputMessage("Recommend optimal contribution amounts")}>
            ML Recommendations
          </button>
          <button onClick={() => setInputMessage("What are the best investment strategies for retirement?")}>
            RAG Search
          </button>
        </div>
      </div>
    </div>
  );
};

export default App;
