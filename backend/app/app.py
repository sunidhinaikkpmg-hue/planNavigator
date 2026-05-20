import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

# Load environment variables before any local imports that initialize the database
load_dotenv(Path(__file__).parent.parent / ".env")

from sqlalchemy.orm import Session
from .db.db import get_db
from fastapi import FastAPI, Depends, HTTPException, status, Header
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .auth import (
    UserCreate,
    UserLogin,
    TokenResponse,
    register_user,
    authenticate_user,
    create_access_token,
    verify_token,
    load_users,
    ACCESS_TOKEN_EXPIRE_MINUTES,
)
from .api.data import router as data_router
from .api.rag import router as rag_router

AI_GATEWAY_URL = "https://ai.gateway.lovable.dev/v1/chat/completions"
AI_MODEL = os.getenv("AI_GATEWAY_MODEL", "google/gemini-3-flash-preview")



def get_allowed_origins() -> list[str]:
    raw_origins = os.getenv("ALLOWED_ORIGINS", "*")
    if raw_origins.strip() == "*":
        return ["*"]
    origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    return origins or ["*"]


def get_api_key() -> str:
    api_key = os.getenv("LOVABLE_API_KEY")
    if not api_key:
        raise RuntimeError("LOVABLE_API_KEY is not configured")
    return api_key


def build_gateway_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_api_key()}",
        "Content-Type": "application/json",
    }


def gateway_error(status_code: int, default_message: str, credits_message: str | None = None) -> JSONResponse:
    if status_code == 429:
        return JSONResponse(status_code=429, content={"error": "Rate limits exceeded, please try again later."})
    if status_code == 402:
        return JSONResponse(status_code=402, content={"error": credits_message or "AI credits exhausted."})
    return JSONResponse(status_code=500, content={"error": default_message})


async def post_gateway_json(payload: dict[str, Any], default_message: str, credits_message: str | None = None) -> JSONResponse | dict[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        response = await client.post(AI_GATEWAY_URL, headers=build_gateway_headers(), json=payload)

    if response.status_code != 200:
        return gateway_error(response.status_code, default_message, credits_message)

    return response.json()


async def post_gateway_stream(payload: dict[str, Any], default_message: str, credits_message: str | None = None) -> JSONResponse | StreamingResponse:
    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
    request = client.build_request("POST", AI_GATEWAY_URL, headers=build_gateway_headers(), json=payload)
    response = await client.send(request, stream=True)

    if response.status_code != 200:
        await response.aread()
        await response.aclose()
        await client.aclose()
        return gateway_error(response.status_code, default_message, credits_message)

    async def iterate() -> Any:
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(iterate(), media_type="text/event-stream")


class ExplainRequest(BaseModel):
    test: dict[str, Any]


class ForecastRequest(BaseModel):
    test: dict[str, Any]


class WhatIfRequest(BaseModel):
    scenario: str
    planContext: dict[str, Any] | None = None


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]


app = FastAPI(title="Plan Navigator API", version="1.0.0")

app.include_router(data_router, prefix="/api")
app.include_router(rag_router, prefix="/api/rag")

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_allowed_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def log_cors_origins():
    """Log allowed CORS origins on startup for debugging"""
    origins = get_allowed_origins()
    print("\n" + "=" * 60)
    print("CORS Configuration:")
    if origins == ["*"]:
        print("  ALLOWED_ORIGINS: * (All origins allowed)")
    else:
        print("  ALLOWED_ORIGINS:")
        for origin in origins:
            print(f"    - {origin}")
    print("=" * 60 + "\n")


async def get_current_user(authorization: str = Header(None)) -> str:
    """Dependency to get current user from JWT token"""
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authorization header")
    
    try:
        scheme, token = authorization.split()
        if scheme.lower() != "bearer":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication scheme")
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authorization header")
    
    email = verify_token(token)
    if not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    
    return email


@app.post("/api/auth/register", response_model=TokenResponse)
async def register(user: UserCreate):
    """Register a new user"""
    success, message = register_user(user.email, user.password)
    
    if not success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)
    
    # Create token after successful registration
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        email=user.email,
        expires_delta=access_token_expires
    )
    
    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        email=user.email
    )


@app.post("/api/auth/login", response_model=TokenResponse)
async def login(user: UserLogin):
    """Login user with email and password"""
    success, message = authenticate_user(user.email, user.password)
    
    if not success:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=message)
    
    # Create token after successful authentication
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        email=user.email,
        expires_delta=access_token_expires
    )
    
    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        email=user.email
    )


@app.get("/api/auth/me")
async def get_me(current_user: str = Depends(get_current_user)):
    """Get current user info"""
    return {"email": current_user}


@app.post("/api/auth/logout")
async def logout():
    """Logout endpoint (frontend should discard token)"""
    return {"message": "Logged out successfully"}


@app.get("/api/auth/users")
async def list_users():
    """Get list of all registered users (for debugging/testing)"""
    users_data = load_users()
    user_list = [{"email": user["email"], "created_at": user.get("created_at")} for user in users_data.get("users", [])]
    return {"users": user_list, "count": len(user_list)}


@app.get("/health")
async def healthcheck(db: Session = Depends(get_db)) -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/explain-test", response_model=None)
async def explain_test(request: ExplainRequest):
    test = request.test
    peer_line = (
        f"Peer percentile: {test['peerBenchmark']['percentile']} (peer set: {test['peerBenchmark']['peerSet']}). "
        f"Bottom quartile {test['peerBenchmark']['bottomQuartile']}, median {test['peerBenchmark']['median']}, "
        f"top quartile {test['peerBenchmark']['topQuartile']}."
        if test.get("peerBenchmark")
        else "No peer benchmark available."
    )

    user_prompt = (
        f"Diagnostic test: \"{test['name']}\" ({test['category']})\n"
        f"Status: {str(test['status']).upper()}\n"
        f"Current value: {test['currentValue']}\n"
        f"Internal benchmark: {test['benchmark']}\n"
        f"{peer_line}\n"
        f"Description: {test['description']}\n"
        f"Existing recommendation: {test['recommendation']}\n\n"
        f"Write a plain-English explanation in 3 short sections using markdown:\n"
        f"**Why this is {'failing' if test['status'] == 'fail' else 'concerning'}**: 1-2 sentences explaining what the number means and why it triggers this status.\n"
        f"**How you compare**: 1-2 sentences interpreting the peer percentile in plain English (e.g., \"you trail 7 out of 10 similar plans\").\n"
        f"**What this plan should do**: 2-3 specific, prioritized actions for THIS plan based on its current value, peer position, and root cause. Be concrete - name programs, thresholds, or campaigns. Avoid generic advice.\n\n"
        "Total response under 180 words. No preamble, no closing summary."
    )

    payload = {
        "model": AI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You are a 401(k) plan diagnostic interpreter. You explain test results to plan sponsors clearly, specifically, and without jargon. Always be concrete and actionable.",
            },
            {"role": "user", "content": user_prompt},
        ],
        "stream": True,
    }

    return await post_gateway_stream(
        payload,
        default_message="AI gateway error",
        credits_message="AI credits exhausted. Add credits in Lovable Cloud settings.",
    )


@app.post("/api/forecast-test", response_model=None)
async def forecast_test(request: ForecastRequest):
    test = request.test
    peer_line = f"Peer percentile {test['peerBenchmark']['percentile']}. " if test.get("peerBenchmark") else ""
    user_prompt = (
        f"Test \"{test['name']}\" ({test['category']}). Current {test['currentValue']}, benchmark {test['benchmark']}, status {test['status']}. "
        f"{peer_line}\n\n"
        "Project a plausible 12-month trajectory if no action is taken. Return 6 monthly data points (current + 5 forward) plus a one-sentence narrative."
    )

    payload = {
        "model": AI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You are a 401(k) diagnostic forecaster. Project realistic short-term trajectories for plan health metrics based on current value and trend pressure. Be conservative - avoid dramatic swings unless status is failing.",
            },
            {"role": "user", "content": user_prompt},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "return_forecast",
                    "description": "Return a 6-point trajectory and projected outlook.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "points": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string", "description": "e.g. 'Now', '+1mo', '+3mo', '+6mo', '+9mo', '+12mo'"},
                                        "value": {"type": "number", "description": "Numeric value of the metric (no unit)"},
                                    },
                                    "required": ["label", "value"],
                                    "additionalProperties": False,
                                },
                            },
                            "unit": {"type": "string", "description": "Unit suffix, e.g. '%', 'pp', '' "},
                            "projectedValue": {"type": "string", "description": "Display string for 12-month projected value, e.g. '71%'"},
                            "trend": {"type": "string", "enum": ["improving", "stable", "declining", "at_risk"]},
                            "willFail": {"type": "boolean", "description": "True if test is projected to move from pass->warn or warn->fail within 12 months"},
                            "narrative": {"type": "string", "description": "One sentence explaining the projection."},
                        },
                        "required": ["points", "unit", "projectedValue", "trend", "willFail", "narrative"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "return_forecast"}},
    }

    data = await post_gateway_json(payload, default_message="AI gateway error")
    if isinstance(data, JSONResponse):
        return data

    tool_call = (((data.get("choices") or [{}])[0]).get("message") or {}).get("tool_calls") or []
    args = json.loads(tool_call[0]["function"]["arguments"]) if tool_call else {"error": "No forecast returned"}
    return JSONResponse(content=args)


@app.post("/api/what-if-simulator", response_model=None)
async def what_if_simulator(request: WhatIfRequest):
    context = request.planContext
    context_line = (
        f"Current plan: participation {context['participation']}%, avg deferral {context['deferral']}%, total contribution {context['totalContribution']}%, "
        f"ADP spread {context['adpSpread']}, employer match utilization {context['matchUtilization']}%, ~{context['eligibleEmployees']} eligible employees, avg salary ~${context['avgSalary']}."
        if context
        else "Use mid-market 401(k) defaults."
    )
    user_prompt = (
        f"{context_line}\n\n"
        f"Scenario: \"{request.scenario}\"\n\n"
        "Simulate the impact across the four key dimensions below. Be realistic - cite the directional logic in the rationale."
    )

    payload = {
        "model": AI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You are a 401(k) plan design simulator. You estimate the directional and quantitative impact of plan design changes on participation, deferral, ADP test results, and total annual employer cost. Use research-backed assumptions: auto-enrollment lifts participation 20-30pp, default escalation lifts deferral ~1pp/yr, raising default deferral lifts avg deferral 60-80% of the delta, etc. Always note key assumptions.",
            },
            {"role": "user", "content": user_prompt},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "return_simulation",
                    "description": "Return the simulated impact of the scenario.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string", "description": "One-sentence headline of net impact."},
                            "impacts": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "metric": {"type": "string", "enum": ["Participation Rate", "Avg Deferral Rate", "ADP Test Spread", "Total Annual Plan Cost"]},
                                        "currentValue": {"type": "string"},
                                        "projectedValue": {"type": "string"},
                                        "delta": {"type": "string", "description": "e.g. '+18 pp', '-$240k', '+0.4%'"},
                                        "direction": {"type": "string", "enum": ["positive", "negative", "neutral"]},
                                        "rationale": {"type": "string", "description": "1 sentence explaining the math/logic."},
                                    },
                                    "required": ["metric", "currentValue", "projectedValue", "delta", "direction", "rationale"],
                                    "additionalProperties": False,
                                },
                            },
                            "assumptions": {"type": "array", "items": {"type": "string"}, "description": "3-4 key assumptions used in the simulation."},
                            "recommendation": {"type": "string", "description": "Plain-English next step recommendation in 1-2 sentences."},
                        },
                        "required": ["summary", "impacts", "assumptions", "recommendation"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "return_simulation"}},
    }

    data = await post_gateway_json(payload, default_message="AI gateway error")
    if isinstance(data, JSONResponse):
        return data

    tool_call = (((data.get("choices") or [{}])[0]).get("message") or {}).get("tool_calls") or []
    args = json.loads(tool_call[0]["function"]["arguments"]) if tool_call else {"error": "No simulation returned"}
    return JSONResponse(content=args)


@app.post("/api/retirementiq-chat", response_model=None)
async def retirementiq_chat(request: ChatRequest):
    payload = {
        "model": AI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You are RetirementAI, an AI assistant for 401(k) plan sponsors. Your role is to help plan sponsors understand retirement plan health metrics, compliance requirements, participant behavior, and operational best practices.\n\nEXPERTISE AREAS:\n- Plan health metrics: participation rates, deferral rates, average account balances, loan/hardship withdrawal trends\n- Compliance & regulatory: ERISA requirements, IRS deadlines, ADP/ACP testing, Form 5500, plan amendments\n- Participant behavior: enrollment trends, contribution patterns, investment allocation, retirement readiness\n- Fiduciary responsibilities: fee benchmarking, investment monitoring, plan governance\n- Industry benchmarks: typical participation rates (70-80%), average deferral rates (6-7%), employer match strategies\n\nRESPONSE GUIDELINES:\n- Be professional, concise, and actionable\n- Use specific numbers, percentages, and industry benchmarks where appropriate\n- When discussing metrics, explain what 'good' looks like and how to improve\n- Suggest next steps or action items when relevant\n- If a question is about a specific plan's data, note that you're providing general guidance and recommend consulting their plan advisor for plan-specific analysis\n\nGUARDRAILS:\n- Do NOT provide specific legal, tax, or investment advice. Always recommend consulting a qualified advisor for such matters.\n- Do NOT make guarantees about investment performance or returns.\n- Do NOT discuss topics unrelated to retirement plans, 401(k) administration, or employee benefits.\n- If asked about unrelated topics, politely redirect: 'I'm designed to help with retirement plan questions. Could I help you with something related to your 401(k) plan instead?'\n- Do NOT generate harmful, discriminatory, or misleading content.\n- Do NOT share or fabricate specific participant personal data.\n- Always include a brief disclaimer when discussing compliance or regulatory matters: recommend verifying with legal counsel.\n- Keep responses focused and under 500 words unless a detailed explanation is specifically requested.",
            },
            *[{"role": message.role, "content": message.content} for message in request.messages],
        ],
        "stream": True,
    }

    return await post_gateway_stream(
        payload,
        default_message="AI service error",
        credits_message="AI credits exhausted. Please add funds in Settings > Workspace > Usage.",
    )