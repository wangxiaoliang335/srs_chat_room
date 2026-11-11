"""聊天室应用身份和 SRS 媒体凭证。"""

import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qs

import jwt


APP_AUDIENCE = "srs-chat-application"
MEDIA_AUDIENCE = "srs-chat-media"
ISSUER = "srs-chat-access"
APP_TTL_SECONDS = 900
MEDIA_TTL_SECONDS = 90
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,96}")
ROOM_ID = re.compile(r"alove_room_[0-9]{1,20}")
SPEAKER_ID = re.compile(r"user_[a-f0-9]{12}")
SHA256_HEX = re.compile(r"[a-f0-9]{64}")
MEDIA_STREAM = re.compile(r"[0-9]{1,20}\.[a-f0-9]{12}")


class MediaAuthError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def original_stream_name(room_id, speaker_id):
    if not ROOM_ID.fullmatch(room_id) or not SPEAKER_ID.fullmatch(speaker_id):
        raise MediaAuthError(400, "invalid room or speaker identifier")
    return f"{room_id.removeprefix('alove_room_')}.{speaker_id.removeprefix('user_')}"


class MediaAuthorization:
    def __init__(self, signing_key, hook_key, applications, can_access_room, is_revoked, is_device_enabled):
        self.signing_key = signing_key
        self.hook_key = hook_key
        self.applications = applications
        self.can_access_room = can_access_room
        self.is_revoked = is_revoked
        self.is_device_enabled = is_device_enabled

    def _sign(self, audience, claims, expires_at):
        return jwt.encode(
            {**claims, "aud": audience, "iss": ISSUER, "iat": int(time.time()), "exp": expires_at},
            self.signing_key,
            algorithm="HS256",
        )

    def _decode(self, audience, token):
        if not token or len(token) > 4096:
            raise MediaAuthError(401, "missing or invalid credential")
        try:
            return jwt.decode(
                token, self.signing_key, algorithms=["HS256"], audience=audience, issuer=ISSUER,
                options={"require": ["exp", "iat", "aud", "iss"]},
            )
        except jwt.InvalidTokenError:
            raise MediaAuthError(401, "invalid or expired credential") from None

    def _registered_app(self, app_id):
        app = self.applications.get(app_id) if app_id != "default" else None
        if not app or not app.get("enabled", True):
            raise MediaAuthError(401, "application is not authorized")
        return app

    def is_session_application_enabled(self, app_id):
        if app_id == "default":
            return True
        app = self.applications.get(app_id) if isinstance(app_id, str) else None
        return bool(app and app.get("enabled", True))

    def is_user_session_enabled(self, user):
        app_id = user.get("app", "default")
        if app_id == "default":
            return self.is_device_enabled(user.get("device"))
        return self.is_session_application_enabled(app_id)

    @staticmethod
    def _app_revision(app):
        fields = (app["key_sha256"], app["package_name"], app["signature_sha256"])
        return hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()

    def authenticate_app(self, app_id, app_key, package_name, signature):
        app = self._registered_app(app_id)
        key_digest = hashlib.sha256(app_key.encode()).hexdigest()
        if (
            not hmac.compare_digest(key_digest, app["key_sha256"])
            or package_name != app["package_name"]
            or signature.lower() != app["signature_sha256"]
        ):
            raise MediaAuthError(401, "invalid application credentials")
        expires_at = int(time.time()) + APP_TTL_SECONDS
        token = self._sign(APP_AUDIENCE, {"app": app_id, "revision": self._app_revision(app)}, expires_at)
        return {"app_token": token, "expires_at": expires_at, "app_name": app.get("name", app_id), "status": "active"}

    def verify_app(self, token):
        claims = self._decode(APP_AUDIENCE, token)
        app = self._registered_app(claims.get("app"))
        if claims.get("revision") != self._app_revision(app):
            raise MediaAuthError(401, "application credential has changed")
        return claims

    def verify_hook_key(self, supplied):
        if not self.hook_key or not hmac.compare_digest(self.hook_key.encode(), supplied.encode()):
            raise MediaAuthError(403, "invalid callback credential")

    def _require_access(self, operation, room_id, user_id, speaker_id):
        if not self.can_access_room(operation, room_id, user_id, speaker_id):
            raise MediaAuthError(403, "room media access denied")

    def issue_ticket(self, user, app_token, room_id, operation, speaker_id):
        if not self.is_user_session_enabled(user):
            raise MediaAuthError(401, "user session client is not authorized")
        app_id = user.get("app", "default")
        now = int(time.time())
        expires_at = min(now + MEDIA_TTL_SECONDS, user["exp"])
        app_revision = None
        if app_token or app_id != "default":
            app = self.verify_app(app_token)
            if app["app"] != app_id:
                raise MediaAuthError(403, "application does not match user session")
            app_revision = app["revision"]
            expires_at = min(expires_at, app["exp"])
        if expires_at <= now:
            raise MediaAuthError(401, "user session expired")

        user_id = user["uid"]
        speaker_id = speaker_id or user_id
        stream = original_stream_name(room_id, speaker_id)
        self._require_access(operation, room_id, user_id, speaker_id)
        ticket = self._sign(
            MEDIA_AUDIENCE,
            {
                "sub": user["sub"], "uid": user_id, "session": user["jti"], "session_iat": user["iat"],
                "session_iat_us": user.get("iat_us"),
                "session_exp": user["exp"], "device": user.get("device"),
                "app": app_id, "revision": app_revision, "room": room_id,
                "speaker": speaker_id, "operation": operation, "stream": stream,
            },
            expires_at,
        )
        return {"ticket": ticket, "stream_name": stream, "expires_at": expires_at}

    def require_ticket_session(self, claims):
        if claims.get("session_exp", float("inf")) <= int(time.time()):
            raise MediaAuthError(401, "user session expired")
        if not self.is_user_session_enabled(claims):
            raise MediaAuthError(401, "user session client is not authorized")
        if claims["app"] != "default":
            app = self._registered_app(claims["app"])
            if claims["revision"] != self._app_revision(app):
                raise MediaAuthError(401, "application credential has changed")
        if self.is_revoked({
            "uid": claims["uid"], "jti": claims["session"],
            "iat": claims["session_iat"], "iat_us": claims.get("session_iat_us"),
        }):
            raise MediaAuthError(401, "user session revoked")

    def authorize_callback(self, operation, event):
        action = f"on_{operation}"
        if (
            not isinstance(event, dict) or event.get("action") != action
            or event.get("app") != "live" or event.get("vhost") != "__defaultVhost__"
        ):
            raise MediaAuthError(403, "invalid media callback scope")
        _, claims = self._authorize_stream(operation, event.get("stream"), event.get("param", ""))
        return claims

    def authorize_stream(self, operation, stream, param):
        token, _ = self._authorize_stream(operation, stream, param)
        return token

    def _authorize_stream(self, operation, stream, param):
        if not isinstance(param, str) or len(param) > 8192:
            raise MediaAuthError(401, "invalid media credential")
        try:
            query = parse_qs(param.removeprefix("?"), keep_blank_values=True, max_num_fields=32)
        except ValueError:
            raise MediaAuthError(401, "invalid media credential") from None
        tickets = query.get("srs_ticket", [])
        if len(tickets) != 1:
            raise MediaAuthError(401, "missing or ambiguous media credential")
        ticket = self._decode(MEDIA_AUDIENCE, tickets[0])
        if ticket["operation"] != operation or ticket["stream"] != stream:
            raise MediaAuthError(403, "media credential scope does not match")
        self.require_ticket_session(ticket)
        self._require_access(operation, ticket["room"], ticket["uid"], ticket["speaker"])
        return tickets[0], ticket


def load_applications(raw):
    applications = json.loads(raw or "{}")
    if not isinstance(applications, dict):
        raise ValueError("SRS_THIRD_PARTY_APPS must be a JSON object")
    for app_id, app in applications.items():
        if (
            app_id == "default" or not IDENTIFIER.fullmatch(app_id) or not isinstance(app, dict)
            or not isinstance(app.get("package_name"), str) or not app["package_name"]
            or any(not isinstance(app.get(key), str) or not SHA256_HEX.fullmatch(app[key])
                   for key in ("key_sha256", "signature_sha256"))
        ):
            raise ValueError("invalid SRS_THIRD_PARTY_APPS entry")
    return applications
