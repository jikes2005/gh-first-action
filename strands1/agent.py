import json
import os
import logging

import requests
from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent, tool

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hello_agent")

app = BedrockAgentCoreApp()

# Settings come from environment variables so you can change them without code edits
API_BASE_URL = os.environ.get("API_BASE_URL", "https://api.example.com")
API_TOKEN = os.environ.get("API_TOKEN", "")
TIMEOUT = 10
MAX_CHARS = 8000  # keep tool output small enough for the model's context


def _shorten(data) -> str:
    text = json.dumps(data) if not isinstance(data, str) else data
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "... [truncated]"


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
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return _shorten(resp.json().get("current_weather", {}))
    except requests.RequestException as e:
        return f"Weather API call failed: {e}"


@tool
def call_my_api(endpoint: str, query: dict | None = None) -> str:
    """Call the company REST API and return its JSON response.

    Use this to look up records such as customers, orders or accounts.

    Args:
        endpoint: Path after the base URL, e.g. "/orders/123" or "/customers".
        query: Optional query-string parameters, e.g. {"status": "open"}.
    """
    headers = {"Accept": "application/json"}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"
    try:
        resp = requests.get(
            API_BASE_URL.rstrip("/") + "/" + endpoint.lstrip("/"),
            params=query or {},
            headers=headers,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        try:
            return _shorten(resp.json())
        except ValueError:
            return _shorten(resp.text)
    except requests.RequestException as e:
        return f"API call failed: {e}"


agent = Agent(
    system_prompt=(
        "You are a helpful assistant. Use the available tools to get real data "
        "before answering, and base your answer on the tool results. "
        "If a tool fails, say so rather than guessing."
    ),
    tools=[get_weather, call_my_api],
)

log.info("Agent loaded with tools: %s", agent.tool_names)

@app.entrypoint
def invoke(payload):
    log.info("Received payload: %s", payload)

    if isinstance(payload, str):
        prompt = payload
    else:
        prompt = (payload.get("prompt") or payload.get("input")
                  or payload.get("message") or "Hello!")
    result = agent(prompt)
    tools_used = {name: m.call_count for name, m in result.metrics.tool_metrics.items()}
    log.info("Tools used: %s", tools_used)

    return {"result": result.message, "tools_used": tools_used, "prompt_seen": prompt}


if __name__ == "__main__":
    app.run()