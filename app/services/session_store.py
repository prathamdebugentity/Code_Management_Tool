from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass


@dataclass
class UserSession:
    sid: str
    username: str
    csrf_token: str
    expires_at: float
    github_token: str | None = None
    github_login: str | None = None


class SessionStore:
    """Server-side, in-memory sessions; the cookie only carries an opaque id so the PAT never leaves the server."""

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._sessions: dict[str, UserSession] = {}
        self._lock = threading.Lock()

    def create(self, username: str) -> UserSession:
        session = UserSession(
            sid=secrets.token_urlsafe(32),
            username=username,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=time.time() + self._ttl,
        )
        with self._lock:
            self._purge_expired()
            self._sessions[session.sid] = session
        return session

    def get(self, sid: str) -> UserSession | None:
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                return None
            if session.expires_at < time.time():
                del self._sessions[sid]
                return None
            session.expires_at = time.time() + self._ttl
            return session

    def delete(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    def _purge_expired(self) -> None:
        now = time.time()
        for sid in [sid for sid, s in self._sessions.items() if s.expires_at < now]:
            del self._sessions[sid]
