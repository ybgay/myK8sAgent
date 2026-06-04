"""FastAPI application serving the Web UI and WebSocket API for k8s-agent.

Endpoints:
  GET  /              Web UI (chat interface)
  GET  /api/health    Health check
  POST /api/chat      Send a message, get response (non-streaming)
  WS   /ws/chat       WebSocket for streaming chat
  GET  /api/skills    List skills
  GET  /api/config    Get current config
  GET  /api/sessions  List saved sessions
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"
    namespace: str = "default"


class ChatResponse(BaseModel):
    text: str
    tool_calls: list[dict] = []
    session_id: str = "default"


# Global agent instance (lazy initialized)
_agent_instance = None
_agent_lock = asyncio.Lock()

# Session manager — persists conversation history per session_id
_session_manager = None


def _get_session_manager():
    """Get or create the global SessionManager instance."""
    global _session_manager
    if _session_manager is None:
        from k8s_agent.core.session import SessionManager
        _session_manager = SessionManager()
    return _session_manager


async def get_agent():
    """Get or create the global agent instance."""
    global _agent_instance
    if _agent_instance is not None:
        return _agent_instance

    async with _agent_lock:
        if _agent_instance is not None:
            return _agent_instance

        from k8s_agent.core.orchestrator import MasterAgent
        from k8s_agent.core.communication.message_bus import MessageBus
        from k8s_agent.core.communication.context import SharedContext
        from k8s_agent.core.mcp.client_manager import MCPClientManager, MCPServerConfig
        from k8s_agent.llm import create_provider
        from k8s_agent.shared.config import get_settings

        settings = get_settings()
        # Use create_provider to support DeepSeek, Anthropic, etc.
        # Set LLM_PROVIDER=deepseek or LLM_PROVIDER=anthropic env var to switch
        llm = create_provider(provider_type="auto")
        message_bus = MessageBus()
        context = SharedContext()
        mcp_manager = MCPClientManager()

        # Try connecting to K8s MCP server
        try:
            await mcp_manager.connect(MCPServerConfig(
                name="k8s-agent-mcp-server",
                transport="stdio",
                command=sys.executable,
                args=["-m", "k8s_agent.mcp_server.server"],
            ))
            logger.info("connected_to_k8s_mcp")
        except Exception as e:
            logger.warning("mcp_connect_failed", error=str(e))

        master = MasterAgent(
            llm_provider=llm,
            message_bus=message_bus,
            context=context,
            mcp_client_manager=mcp_manager,
        )
        await master.initialize()
        _agent_instance = master
        return _agent_instance


async def _get_or_create_session(session_id: str) -> "Session":
    """Get an existing session or create a new one.

    Returns the session with its conversation history intact.
    New sessions start with a fresh (empty) conversation.
    """
    sm = _get_session_manager()
    session = sm.get_session(session_id)
    if session is None:
        # Try loading from disk first
        session = sm.load_session(session_id)
    if session is None:
        # Brand-new session
        session = sm.create_session(session_id)
    return session


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="K8s Agent Web UI",
        description="LLM-powered Kubernetes Agent with multi-agent collaboration",
        version="0.1.0",
    )

    # Mount static files
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(exist_ok=True)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        """Serve the Web UI."""
        html_path = static_dir / "index.html"
        if html_path.exists():
            return html_path.read_text(encoding="utf-8")
        return HTMLResponse(content="<h1>Web UI not found. Run 'k8s-agent web' from the project directory.</h1>")

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(request: ChatRequest):
        """Non-streaming chat endpoint with session persistence.

        Each session_id maintains its own conversation history across requests.
        """
        agent = await get_agent()
        session = await _get_or_create_session(request.session_id)

        # Load this session's conversation into the agent
        async with _agent_lock:
            agent.conversation = session.conversation
            agent.context.set("current_namespace", request.namespace, scope="session")
            response = await agent.run(request.message)

        # Persist updated conversation back to session storage
        sm = _get_session_manager()
        sm.save_session(request.session_id)

        return ChatResponse(text=response, session_id=request.session_id)

    @app.websocket("/ws/chat")
    async def ws_chat(websocket: WebSocket):
        """WebSocket streaming chat endpoint with session persistence.

        Sends events:
        - {"type": "text_delta", "text": "..."}
        - {"type": "tool_use_start", "tool_name": "..."}
        - {"type": "tool_executing", "tool_name": "...", "tool_input": {...}}
        - {"type": "tool_result", "result": "..."}
        - {"type": "message_stop"}
        - {"type": "error", "message": "..."}

        Each connection uses a session_id to maintain conversation history
        across multiple messages within the same session.
        """
        await websocket.accept()
        logger.info("ws_connected")

        # Track session per connection (can be overridden per message)
        current_session_id = "default"

        try:
            agent = await get_agent()
            sm = _get_session_manager()

            while True:
                # Receive message from client
                data = await websocket.receive_json()
                message = data.get("message", "")
                namespace = data.get("namespace", "default")
                session_id = data.get("session_id", current_session_id)

                if not message:
                    continue

                current_session_id = session_id

                # Get or create session with its conversation history
                session = await _get_or_create_session(session_id)

                async with _agent_lock:
                    # Load this session's conversation into the agent
                    agent.conversation = session.conversation
                    agent.context.set("current_namespace", namespace, scope="session")

                    try:
                        async for event in agent.run_stream(message):
                            # Update session.conversation ref — agent.conversation IS
                            # session.conversation (same object), so no copy needed.
                            if isinstance(event, dict):
                                await websocket.send_json(event)
                            elif hasattr(event, 'type'):
                                payload = {
                                    "type": event.type,
                                    "text": getattr(event, 'text', None),
                                    "tool_name": getattr(event, 'tool_name', None),
                                    "tool_input": getattr(event, 'tool_input', None),
                                    "tool_use_id": getattr(event, 'tool_use_id', None),
                                }
                                # Pass through error message if present
                                error_val = getattr(event, 'error', None)
                                if error_val is not None:
                                    payload["message"] = str(error_val)
                                # Pass through visualization data if present
                                viz_data = getattr(event, 'data', None)
                                if viz_data is not None:
                                    payload["data"] = viz_data
                                await websocket.send_json(payload)
                    except Exception as e:
                        logger.error("stream_error", error=str(e))
                        await websocket.send_json({"type": "error", "message": str(e)})

                # Persist conversation after each message
                sm.save_session(session_id)

        except WebSocketDisconnect:
            logger.info("ws_disconnected")
        except Exception as e:
            logger.error("ws_error", error=str(e))
            try:
                await websocket.send_json({"type": "error", "message": str(e)})
            except Exception:
                pass

    @app.get("/api/skills")
    async def list_skills():
        """List available skills."""
        try:
            from k8s_agent.skills.builtin import BUILTIN_SKILLS
            skills = []
            for skill_cls in BUILTIN_SKILLS:
                skill = skill_cls()
                manifest = skill.get_manifest()
                tools = skill.get_tools()
                skills.append({
                    "name": manifest.name,
                    "version": manifest.version,
                    "description": manifest.description,
                    "capabilities": manifest.capabilities,
                    "tools": [{"name": t.name, "description": t.description} for t in tools],
                })
            return skills
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/config")
    async def get_config():
        """Get current configuration (safe subset)."""
        from k8s_agent.shared.config import get_settings
        settings = get_settings()
        return {
            "llm_model": settings.llm.model,
            "k8s_auth_mode": settings.kubernetes.auth_mode,
            "mcp_transport": settings.mcp_server.transport,
            "agent_max_iterations": settings.agent.max_iterations,
        }

    @app.get("/api/sessions")
    async def list_sessions():
        """List saved sessions."""
        try:
            from k8s_agent.core.session import SessionManager
            sm = SessionManager()
            return sm.list_stored_sessions()
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    return app
