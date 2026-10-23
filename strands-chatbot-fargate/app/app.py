"""Hello-world agentic chatbot: Strands agent behind a FastAPI web app.

Runs on ECS Fargate (or locally with `uvicorn app:app --port 8080`).
"""
import json
import logging
import os
import threading
import uuid
from collections import OrderedDict

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from strands import Agent, tool
from strands.models import BedrockModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("chatbot")

MODEL_ID = os.environ.get("MODEL_ID", "").strip()
REGION = os.environ.get("AWS_REGION", "us-east-1")
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "500"))
TOOL_TIMEOUT = 10
MAX_TOOL_CHARS = 8000

SYSTEM_PROMPT = (
    "You are a friendly, concise assistant in a chat window. "
    "Use your tools to get real data before answering questions they can help with, "
    "and base your answer on the tool results. If a tool fails, say so rather than guessing."
)


# ---------- Tools ----------

def _shorten(data) -> str:
    text = data if isinstance(data, str) else json.dumps(data)
    return text if len(text) <= MAX_TOOL_CHARS else text[:MAX_TOOL_CHARS] + "... [truncated]"


@tool
def get_weather(latitude: float, longitude: float) -> str:
    """Get the current weather for a location.

    Args:
        latitude: Latitude of the location, e.g. 43.65 for Toronto.
        longitude: Longitude of the location, e.g. -79.38 for Toronto.
    """
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": latitude, "longitude": longitude, "current_weather": "true"},
            timeout=TOOL_TIMEOUT,
        )
        resp.raise_for_status()
        return _shorten(resp.json().get("current_weather", {}))
    except requests.RequestException as e:
        return f"Weather API call failed: {e}"


TOOLS = [get_weather]


# ---------- Per-session agents (conversation memory) ----------

class Session:
    def __init__(self):
        kwargs = {"system_prompt": SYSTEM_PROMPT, "tools": TOOLS, "callback_handler": None}
        if MODEL_ID:
            kwargs["model"] = BedrockModel(model_id=MODEL_ID, region_name=REGION, temperature=0.2)
        self.agent = Agent(**kwargs)
        self.lock = threading.Lock()  # one turn at a time per conversation


_sessions: "OrderedDict[str, Session]" = OrderedDict()
_sessions_lock = threading.Lock()


def get_session(session_id: str) -> Session:
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session is None:
            if len(_sessions) >= MAX_SESSIONS:
                _sessions.popitem(last=False)  # drop the oldest conversation
            session = Session()
            _sessions[session_id] = session
        else:
            _sessions.move_to_end(session_id)
        return session


# ---------- Web app ----------

app = FastAPI(title="Strands Chatbot")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    tools_used: dict[str, int]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    # Sync endpoint: FastAPI runs it in a worker thread, so slow model calls don't block other users.
    message = req.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is empty")

    session_id = req.session_id or str(uuid.uuid4())
    session = get_session(session_id)

    try:
        with session.lock:
            result = session.agent(message)
    except Exception as e:  # surface model/permission errors to the UI instead of a blank 500
        log.exception("Agent call failed")
        raise HTTPException(status_code=502, detail=f"Agent error: {e}")

    tools_used = {name: m.call_count for name, m in result.metrics.tool_metrics.items()}
    log.info("session=%s tools_used=%s", session_id, tools_used)
    return ChatResponse(reply=str(result).strip(), session_id=session_id, tools_used=tools_used)


@app.post("/api/reset")
def reset(req: ChatRequest):
    if req.session_id:
        with _sessions_lock:
            _sessions.pop(req.session_id, None)
    return {"status": "reset"}
