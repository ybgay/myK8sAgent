"""Session management — conversation persistence and restoration."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from k8s_agent.llm.conversation import ConversationManager
from k8s_agent.shared.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Session:
    """A user interaction session with conversation history."""

    session_id: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    conversation: ConversationManager = field(default_factory=ConversationManager)
    metadata: dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        """Update the last-modified timestamp."""
        self.updated_at = time.time()

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    @property
    def message_count(self) -> int:
        return len(self.conversation)


class SessionManager:
    """Manages user sessions — create, save, load, list.

    Sessions are persisted as JSON files for durability across restarts.
    """

    def __init__(self, storage_dir: str = "~/.k8s-agent/sessions") -> None:
        self.storage_dir = Path(storage_dir).expanduser().resolve()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._active_sessions: dict[str, Session] = {}

    def create_session(
        self,
        session_id: str | None = None,
        system_prompt: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        """Create a new session.

        Args:
            session_id: Optional ID. Auto-generated if not provided.
            system_prompt: Initial system prompt.
            metadata: Arbitrary session metadata.

        Returns:
            The new Session.
        """
        import uuid
        sid = session_id or f"session-{uuid.uuid4().hex[:12]}"

        session = Session(
            session_id=sid,
            metadata=metadata or {},
        )
        session.conversation.system_prompt = system_prompt
        self._active_sessions[sid] = session
        logger.info("session_created", session_id=sid)
        return session

    def get_session(self, session_id: str) -> Session | None:
        """Get an active session by ID."""
        return self._active_sessions.get(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all active sessions."""
        return [
            {
                "session_id": s.session_id,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
                "message_count": s.message_count,
                "metadata": s.metadata,
            }
            for s in self._active_sessions.values()
        ]

    def save_session(self, session_id: str) -> str:
        """Persist a session to disk.

        Args:
            session_id: The session to save.

        Returns:
            Path to the saved file.
        """
        session = self._active_sessions.get(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")

        session.touch()
        file_path = self.storage_dir / f"{session_id}.json"

        data = {
            "session_id": session.session_id,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "conversation": session.conversation.to_dict(),
            "metadata": session.metadata,
        }

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)

        logger.info("session_saved", session_id=session_id, path=str(file_path))
        return str(file_path)

    def load_session(self, session_id: str) -> Session | None:
        """Load a session from disk.

        Args:
            session_id: The session ID to load.

        Returns:
            The loaded Session, or None if not found.
        """
        file_path = self.storage_dir / f"{session_id}.json"
        if not file_path.exists():
            logger.warning("session_file_not_found", session_id=session_id)
            return None

        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)

        session = Session(
            session_id=data["session_id"],
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            metadata=data.get("metadata", {}),
        )
        session.conversation = ConversationManager.from_dict(data.get("conversation", {}))

        self._active_sessions[session_id] = session
        logger.info("session_loaded", session_id=session_id)
        return session

    def delete_session(self, session_id: str) -> bool:
        """Delete a session from memory and disk.

        Args:
            session_id: The session to delete.

        Returns:
            True if deleted, False if not found.
        """
        if session_id in self._active_sessions:
            del self._active_sessions[session_id]

        file_path = self.storage_dir / f"{session_id}.json"
        if file_path.exists():
            file_path.unlink()
            logger.info("session_deleted", session_id=session_id)
            return True

        return False

    def list_stored_sessions(self) -> list[dict[str, Any]]:
        """List all sessions stored on disk."""
        sessions = []
        for file_path in self.storage_dir.glob("*.json"):
            try:
                with open(file_path, encoding="utf-8") as f:
                    data = json.load(f)
                sessions.append({
                    "session_id": data.get("session_id", file_path.stem),
                    "created_at": data.get("created_at", 0),
                    "message_count": len(data.get("conversation", {}).get("messages", [])),
                })
            except Exception:
                pass
        sessions.sort(key=lambda s: s.get("created_at", 0), reverse=True)
        return sessions

    def clear_all(self) -> None:
        """Clear all active sessions from memory."""
        self._active_sessions.clear()
        logger.info("all_sessions_cleared")
