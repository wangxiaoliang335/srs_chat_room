"""Durable authorization records for media connections accepted by SRS."""

import asyncio
import json
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass

from media_auth import MEDIA_STREAM, MediaAuthError


SRS_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
# SRS can wait up to 30 seconds for the authorization callback before registering an RTC player.
REGISTRATION_GRACE_SECONDS = 35
SESSION_CLAIMS = (
    "sub", "uid", "session", "session_iat", "session_iat_us", "session_exp", "app", "device", "revision",
    "room", "speaker", "operation", "stream",
)


@dataclass(frozen=True)
class MediaConnection:
    identity: tuple[str, str, str]
    claims: dict
    accepted_at: float
    pending: bool

    @property
    def client_id(self):
        return self.identity[2]


def callback_identity(event):
    identity = tuple(event.get(field) for field in ("server_id", "service_id", "client_id"))
    if any(not isinstance(value, str) or not SRS_ID.fullmatch(value) for value in identity):
        raise MediaAuthError(403, "invalid SRS connection identity")
    return identity


class MediaSessions:
    def __init__(self, path):
        self.path = path
        self._close_lock = asyncio.Lock()
        self._closing = set()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS media_sessions (
                    server_id TEXT NOT NULL,
                    service_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    claims TEXT NOT NULL,
                    accepted_at REAL NOT NULL,
                    pending INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (server_id, service_id, client_id)
                )
            """)

    def remember(self, event, claims):
        identity = callback_identity(event)
        if identity in self._closing:
            raise MediaAuthError(403, "SRS connection is being closed")
        stored = json.dumps({name: claims[name] for name in SESSION_CLAIMS if name in claims})
        with closing(sqlite3.connect(self.path)) as db, db:
            existing = db.execute("""
                SELECT claims, pending FROM media_sessions
                WHERE (server_id, service_id, client_id) = (?, ?, ?)
            """, identity).fetchone()
            if existing is not None:
                if existing != (stored, 0):
                    raise MediaAuthError(403, "SRS connection already has a different authorization")
                return
            db.execute("INSERT INTO media_sessions VALUES (?, ?, ?, ?, ?, 0)", (*identity, stored, time.time()))

    def forget_callback(self, event, action, operation):
        if not isinstance(event, dict) or (
            event.get("action") != action or event.get("app") != "live"
            or event.get("vhost") != "__defaultVhost__"
        ):
            return
        stream = event.get("stream")
        if not isinstance(stream, str) or not MEDIA_STREAM.fullmatch(stream):
            return
        identity = callback_identity(event)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""
                DELETE FROM media_sessions WHERE (server_id, service_id, client_id) = (?, ?, ?)
                AND json_extract(claims, '$.operation') = ? AND json_extract(claims, '$.stream') = ?
            """, (*identity, operation, stream))

    @staticmethod
    def _connections(db, pending_only=False):
        query = "SELECT server_id, service_id, client_id, claims, accepted_at, pending FROM media_sessions"
        if pending_only:
            query += " WHERE pending = 1"
        return [
            MediaConnection((server, service, client), json.loads(claims), accepted, bool(pending))
            for server, service, client, claims, accepted, pending in db.execute(query)
        ]

    def mark_pending(self, should_close, *, in_scope=None):
        with closing(sqlite3.connect(self.path)) as db, db:
            # Once revoked, an old connection must still close after a rejoin/unmute.
            matched = [
                conn for conn in self._connections(db)
                if (in_scope is None or in_scope(conn.claims)) and (conn.pending or should_close(conn.claims))
            ]
            db.executemany("""
                UPDATE media_sessions SET pending = 1
                WHERE (server_id, service_id, client_id) = (?, ?, ?)
            """, [conn.identity for conn in matched if not conn.pending])
        return {conn.identity for conn in matched}

    def _forget(self, connections):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany("""
                DELETE FROM media_sessions WHERE (server_id, service_id, client_id) = (?, ?, ?)
                AND accepted_at = ?
            """, [(*conn.identity, conn.accepted_at) for conn in connections])

    async def close_pending(self, management, identities=None):
        """Attempt disconnection and return the number still awaiting confirmation."""
        if identities is not None and not identities:
            return 0
        # A room operation must wait if the worker already started closing its connections.
        async with self._close_lock:
            with closing(sqlite3.connect(self.path)) as db:
                pending = [
                    conn for conn in self._connections(db, pending_only=True)
                    if identities is None or conn.identity in identities
                ]
            if not pending:
                return 0
            self._closing.update(conn.identity for conn in pending)
            try:
                closed = await management.close_connections(pending)
                self._forget(closed)
            finally:
                self._closing.clear()
            # A stop/unpublish callback may remove records while management is awaiting SRS.
            with closing(sqlite3.connect(self.path)) as db:
                return sum(
                    identities is None or conn.identity in identities
                    for conn in self._connections(db, pending_only=True)
                )

    async def reconcile(self, management):
        cutoff = time.time() - REGISTRATION_GRACE_SECONDS
        with closing(sqlite3.connect(self.path)) as db:
            connections = [conn for conn in self._connections(db) if conn.accepted_at <= cutoff]
        if not connections:
            return
        active = await management.list_connections()
        if active is not None:
            self._forget([conn for conn in connections if conn.identity not in active])
