"""Verify requests signed by an enrolled Android installation."""

import base64
from contextlib import closing
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import threading
import time

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from media_auth import MediaAuthError


MAX_CLOCK_SKEW = 300
KEY_ID = re.compile(r"[a-f0-9]{64}")
TIMESTAMP = re.compile(r"[1-9][0-9]{0,10}")
NONCE = re.compile(r"[A-Za-z0-9_-]{24}")
SIGNATURE = re.compile(r"[A-Za-z0-9_-]{40,110}")
PUBLIC_KEY = re.compile(r"[A-Za-z0-9_-]{80,180}")
logger = logging.getLogger(__name__)


def decode_base64url(value):
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def proof_message(host, method, path, key_id, issued_at, nonce, body, authorization):
    return "\n".join((
        "alove-chat-device-proof-v1", host, method, path, key_id, issued_at, nonce,
        hashlib.sha256(body).hexdigest(),
        hashlib.sha256(authorization.encode("utf-8")).hexdigest(),
    )).encode("utf-8")


class AndroidDeviceProof:
    def __init__(self, registry_path, nonce_database, audience):
        self.registry_path = Path(registry_path) if registry_path else None
        self.nonce_database = nonce_database
        self.audience = audience.strip().lower()
        self._lock = threading.RLock()
        self._registry_version = None
        self._public_keys = {}

    def _keys(self):
        if not self.registry_path or not self.audience:
            return {}
        with self._lock:
            try:
                stat = self.registry_path.stat()
            except OSError:
                self._registry_version, self._public_keys = None, {}
                return self._public_keys
            version = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            if self._registry_version == version:
                return self._public_keys
            keys = {}
            try:
                if stat.st_size > 1024 * 1024:
                    raise ValueError("registry exceeds size limit")
                entries = json.loads(self.registry_path.read_text(encoding="utf-8"))
                if not isinstance(entries, dict):
                    raise ValueError("registry must be an object")
                for key_id, entry in entries.items():
                    if not KEY_ID.fullmatch(key_id) or not isinstance(entry, dict):
                        raise ValueError("invalid device entry")
                    enabled = entry.get("enabled", True)
                    if type(enabled) is not bool:
                        raise ValueError("invalid device status")
                    if not enabled:
                        continue
                    encoded_key = entry.get("public_key", "")
                    if not isinstance(encoded_key, str) or not PUBLIC_KEY.fullmatch(encoded_key):
                        raise ValueError("invalid device public key")
                    der = decode_base64url(encoded_key)
                    key = serialization.load_der_public_key(der)
                    if (
                        not isinstance(key, ec.EllipticCurvePublicKey)
                        or not isinstance(key.curve, ec.SECP256R1)
                        or hashlib.sha256(der).hexdigest() != key_id
                    ):
                        raise ValueError("device public key does not match")
                    keys[key_id] = key
            except (OSError, UnsupportedAlgorithm, ValueError):
                logger.warning("Android device registry could not be validated")
                keys = {}
            self._registry_version, self._public_keys = version, keys
            return keys

    def is_enabled(self, key_id):
        return isinstance(key_id, str) and key_id in self._keys()

    def verify(self, header, *, method, path, body, authorization, expected_key=None):
        if not header or len(header) > 256:
            raise MediaAuthError(401, "Android device proof required")
        parts = header.split(".")
        if len(parts) != 5 or parts[0] != "v1":
            raise MediaAuthError(401, "invalid Android device proof")
        _, key_id, issued_at, nonce, signature = parts
        if not (
            KEY_ID.fullmatch(key_id) and TIMESTAMP.fullmatch(issued_at)
            and NONCE.fullmatch(nonce) and SIGNATURE.fullmatch(signature)
        ):
            raise MediaAuthError(401, "invalid Android device proof")
        if expected_key is not None and key_id != expected_key:
            raise MediaAuthError(403, "Android device does not match the session")
        now = int(time.time())
        if abs(now - int(issued_at)) > MAX_CLOCK_SKEW:
            raise MediaAuthError(401, "Android device proof expired")
        key = self._keys().get(key_id)
        if key is None:
            raise MediaAuthError(401, "Android device is not enrolled")
        try:
            key.verify(
                decode_base64url(signature),
                proof_message(self.audience, method, path, key_id, issued_at, nonce, body, authorization),
                ec.ECDSA(hashes.SHA256()),
            )
        except (InvalidSignature, ValueError):
            raise MediaAuthError(401, "invalid Android device signature") from None
        if not self._consume_nonce(key_id, nonce, now, int(issued_at) + MAX_CLOCK_SKEW + 1):
            raise MediaAuthError(401, "Android device proof already used")
        return key_id

    def _consume_nonce(self, key_id, nonce, now, expires_at):
        with closing(sqlite3.connect(self.nonce_database, timeout=5)) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS android_device_nonces (
                    key_id TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    PRIMARY KEY (key_id, nonce)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS android_device_nonce_expiry
                ON android_device_nonces (expires_at)
            """)
            conn.execute("DELETE FROM android_device_nonces WHERE expires_at <= ?", (now,))
            result = conn.execute(
                "INSERT OR IGNORE INTO android_device_nonces VALUES (?, ?, ?)",
                (key_id, nonce, expires_at),
            )
            return result.rowcount == 1
