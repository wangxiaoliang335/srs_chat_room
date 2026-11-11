import base64
import hashlib
import importlib
import json
import logging
import secrets
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient


ROOM = "alove_room_1001"
OTHER_ROOM = "alove_room_1002"
OWNER = "user_111111111111"
MEMBER = "user_222222222222"
OUTSIDER = "user_333333333333"
APP_ID = "qa-partner"
APP_KEY = "qa-app-key-" + "a" * 40
SIGNATURE = "b" * 64
HOOK_KEY = "qa-hook-key-" + "c" * 40
DEVICE_HOST = "chat-dev.example.test"
DEVICE_KEY = ec.generate_private_key(ec.SECP256R1())
DEVICE_DER = DEVICE_KEY.public_key().public_bytes(
    serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
)
DEVICE_ID = hashlib.sha256(DEVICE_DER).hexdigest()


@pytest.fixture
def chat(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    for name, value in {
        "MYSQL_DISABLE": "1",
        "REDIS_DISABLE": "1",
        "SYNC_ENABLED": "false",
        "CHAT_DATA_DIR": str(tmp_path / "state"),
        "BUSINESS_APP_KEY": "test-business-key",
        "JWT_SECRET": "test-identity-secret-" + "d" * 40,
        "SRS_MEDIA_HOOK_SECRET": HOOK_KEY,
        "SRS_THIRD_PARTY_APPS": json.dumps({
            APP_ID: {
                "key_sha256": hashlib.sha256(APP_KEY.encode()).hexdigest(),
                "package_name": "qa.partner",
                "signature_sha256": SIGNATURE,
            }
        }),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setitem(sys.modules, "redis", None)
    server = importlib.import_module("server_fastapi")
    from android_device_proof import AndroidDeviceProof
    from media_sessions import MediaSessions
    from pass_code_store import PassCodeStore
    from user_manager import UserManager

    rooms = UserManager(tmp_path / "rooms.json")
    rooms.create_room(ROOM, OWNER, "QA room")
    rooms.create_room(OTHER_ROOM, OUTSIDER, "Other room")
    rooms.join_room(ROOM, MEMBER)
    monkeypatch.setattr(server, "user_manager", rooms)
    monkeypatch.setattr(server, "media_sessions", MediaSessions(tmp_path / "media-sessions.sqlite3"))
    passes = PassCodeStore(tmp_path / "passes.json")
    passes.issue(MEMBER, ROOM)
    monkeypatch.setattr(server, "pass_code_store", passes)
    registry = tmp_path / "android-devices.json"
    registry.write_text(json.dumps({
        DEVICE_ID: {"public_key": base64.urlsafe_b64encode(DEVICE_DER).decode().rstrip("=")}
    }), encoding="utf-8")
    monkeypatch.setattr(server, "android_devices", AndroidDeviceProof(
        registry, tmp_path / "android-proofs.sqlite3", DEVICE_HOST,
    ))
    return server, TestClient(server.app)


def user_headers(uid, app_id="default"):
    from auth import issue_token

    token, _, _ = issue_token(
        username=uid, user_id=uid, app_id=app_id,
        device_id=DEVICE_ID if app_id == "default" else None,
    )
    return {"Authorization": f"Bearer {token}"}


def app_credentials(**updates):
    return {
        "app_id": APP_ID, "app_key": APP_KEY,
        "package_name": "qa.partner", "signature_sha256": SIGNATURE,
        **updates,
    }


def android_proof(path, body, authorization="", *, key=DEVICE_KEY, issued_at=None):
    from android_device_proof import proof_message

    device_der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_id = hashlib.sha256(device_der).hexdigest()
    issued_at = str(int(time.time()) if issued_at is None else issued_at)
    nonce = secrets.token_urlsafe(18)
    message = proof_message(DEVICE_HOST, "POST", path, key_id, issued_at, nonce, body, authorization)
    signature = base64.urlsafe_b64encode(key.sign(message, ec.ECDSA(hashes.SHA256()))).decode().rstrip("=")
    return f"v1.{key_id}.{issued_at}.{nonce}.{signature}"


def issue(client, headers, operation, room=ROOM, speaker=OWNER):
    path = f"/api/v1/room/{room}/media-ticket"
    body = json.dumps({"operation": operation, "speaker_id": speaker}).encode()
    proof = android_proof(path, body, headers.get("Authorization", ""))
    return client.post(path, content=body, headers={
        "Content-Type": "application/json", "X-Chat-Device-Proof": proof, **headers,
    })


def hook(client, action, stream, ticket=None, key=HOOK_KEY, app="live"):
    return client.post(
        f"/api/v1/streams/{action}", params={"key": key},
        json={
            "action": action, "app": app, "vhost": "__defaultVhost__", "stream": stream,
            "param": f"?srs_ticket={ticket}" if ticket else "",
            "server_id": "qa-server", "service_id": "qa-service", "client_id": secrets.token_hex(8),
        },
    )


def test_srs_denies_anonymous_publish_and_play(chat):
    _, client = chat
    for action in ("on_publish", "on_play"):
        response = hook(client, action, "1001.111111111111")
        assert response.status_code >= 400 or response.json()["code"] != 0


def test_callback_log_rotates_with_bounded_retention(chat, monkeypatch):
    server, _ = chat
    handler = server.file_handler
    assert 0 < getattr(handler, "maxBytes", 0) <= 10 * 1024 * 1024
    assert 0 < getattr(handler, "backupCount", 0) <= 5
    # Exercise the configured handler with small files rather than writing megabytes in CI.
    monkeypatch.setattr(handler, "maxBytes", 1024)
    monkeypatch.setattr(handler, "backupCount", 2)
    for index in range(8):
        handler.emit(logging.LogRecord("callback-test", logging.INFO, __file__, 1, f"entry-{index} " + "x" * 500, (), None))
    handler.flush()

    path = Path(handler.baseFilename)
    assert path.stat().st_size < 1024
    assert (path.parent / f"{path.name}.1").stat().st_size < 1024
    assert (path.parent / f"{path.name}.2").stat().st_size < 1024
    assert not (path.parent / f"{path.name}.3").exists()
    assert "entry-7" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", ["/api/v1/room", "/api/v1/rooms"])
@pytest.mark.parametrize("room_id", ["partner-demo", "", "alove_room_", "alove_room_١", "alove_room_123456789012345678901", "alove_room_123\n"])
def test_create_room_rejects_ids_that_cannot_receive_media_before_mutation(chat, path, room_id):
    server, client = chat
    before = {room.room_id: room.name for room in server.user_manager.get_all_rooms()}

    response = client.post(path, headers=user_headers(MEMBER), json={"room_id": room_id})

    assert response.status_code == 400
    assert response.json()["code"] == 400
    assert {room.room_id: room.name for room in server.user_manager.get_all_rooms()} == before
    assert server.user_manager.find_owned_room(MEMBER) is None


@pytest.mark.parametrize("path", ["/api/v1/room", "/api/v1/rooms"])
def test_invalid_create_id_cannot_overwrite_existing_room(chat, path):
    server, client = chat

    response = client.post(path, headers=user_headers(OWNER), json={"room_id": "partner-demo", "name": "changed"})

    assert response.status_code == 400
    assert server.user_manager.get_room(ROOM).name == "QA room"


@pytest.mark.parametrize("path", ["/api/v1/room", "/api/v1/rooms"])
@pytest.mark.parametrize("room_id", ["alove_room_0", "alove_room_1794379200123", "alove_room_12345678901234567890"])
def test_create_room_accepts_android_ids_and_issues_media_ticket(chat, path, room_id):
    _, client = chat
    creator = "user_444444444444"
    headers = user_headers(creator)

    response = client.post(path, headers=headers, json={"room_id": room_id})

    assert response.json()["code"] == 0
    assert response.json()["data"]["room_id"] == room_id
    grant = issue(client, headers, "publish", room=room_id, speaker=creator)
    assert grant.json()["code"] == 0


def test_coupon_bearer_alone_cannot_select_first_party_login(chat, monkeypatch):
    server, client = chat
    monkeypatch.setattr(server, "verify_external_token", lambda _: {"userId": "71342", "username": "QA user"})

    response = client.post("/api/v1/auth/login", json={"external_token": "valid-coupon-user-token"})

    assert response.status_code == 401


def test_enrolled_android_login_binds_key_and_rejects_replay_and_body_tampering(chat, monkeypatch):
    server, client = chat
    monkeypatch.setattr(server, "verify_external_token", lambda _: {"userId": "71342", "username": "QA user"})
    path = "/api/v1/auth/login"
    body = b'{"external_token":"valid-coupon-user-token"}'
    header = {"Content-Type": "application/json", "X-Chat-Device-Proof": android_proof(path, body)}

    assert client.post(path, content=b'{"external_token":"changed-token"}', headers=header).status_code == 401
    result = client.post(path, content=body, headers=header)
    assert result.status_code == 200 and result.json()["code"] == 0
    payload = jwt.decode(result.json()["data"]["token"], server.JWT_SECRET, algorithms=["HS256"])
    assert payload["app"] == "default" and payload["device"] == DEVICE_ID
    assert client.post(path, content=body, headers=header).status_code == 401

    for proof in (
        android_proof(path, body, key=ec.generate_private_key(ec.SECP256R1())),
        android_proof(path, body, issued_at=int(time.time()) - 301),
    ):
        assert client.post(path, content=body, headers={**header, "X-Chat-Device-Proof": proof}).status_code == 401


def test_default_ticket_requires_proof_bound_to_user_session(chat):
    server, client = chat
    from auth import issue_token

    old_token, _, _ = issue_token(username=OWNER, user_id=OWNER)
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old_token}"}).status_code == 401
    path = f"/api/v1/room/{ROOM}/media-ticket"
    headers = user_headers(OWNER)
    body = b'{"operation":"publish"}'
    common = {"Content-Type": "application/json", **headers}
    assert client.post(path, content=body, headers=common).status_code == 401
    bad = android_proof(path, body, authorization=user_headers(OWNER)["Authorization"])
    assert client.post(path, content=body, headers={**common, "X-Chat-Device-Proof": bad}).status_code == 401
    good = android_proof(path, body, authorization=headers["Authorization"])
    result = client.post(path, content=body, headers={**common, "X-Chat-Device-Proof": good})
    assert result.status_code == 200 and result.json()["code"] == 0
    ticket = result.json()["data"]
    server.android_devices.registry_path.write_text("{}", encoding="utf-8")
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401
    assert hook(client, "on_publish", ticket["stream_name"], ticket["ticket"]).status_code == 401


def test_registered_partner_with_coupon_user_token_stays_in_its_own_application(chat, monkeypatch):
    server, client = chat
    monkeypatch.setattr(server, "verify_external_token", lambda _: {"userId": "71343", "username": "QA user"})
    app_token = client.post("/api/v1/app/verify", json=app_credentials()).json()["data"]["app_token"]
    result = client.post("/api/v1/auth/login", json={"external_token": "valid-coupon-user-token"},
                         headers={"X-App-Token": app_token})
    assert result.status_code == 200 and result.json()["code"] == 0
    payload = jwt.decode(result.json()["data"]["token"], server.JWT_SECRET, algorithms=["HS256"])
    assert payload["app"] == APP_ID and "device" not in payload


def test_logout_all_revokes_same_user_before_rename_and_preserves_equal_display_names(chat):
    server, client = chat
    from auth import issue_token

    def headers(name, uid):
        token, _, _ = issue_token(username=name, user_id=uid, device_id=DEVICE_ID)
        return {"Authorization": f"Bearer {token}"}

    old = headers("Shared display name", OWNER)
    other = headers("Renamed display name", MEMBER)
    current = headers("Renamed display name", OWNER)
    ticket = issue(client, old, "publish").json()["data"]
    assert client.post("/api/v1/auth/logout-all", headers=current, json={}).json()["code"] == 0
    assert client.get("/api/v1/auth/me", headers=old).status_code == 401
    assert client.get("/api/v1/auth/me", headers=other).status_code == 200
    assert hook(client, "on_publish", ticket["stream_name"], ticket["ticket"]).status_code == 401
    assert client.get("/api/v1/auth/me", headers=headers("Renamed display name", OWNER)).status_code == 200


def test_ticket_is_bound_to_room_stream_and_operation(chat):
    _, client = chat
    owner = user_headers(OWNER)
    ticket = issue(client, owner, "publish").json()["data"]
    assert ticket["stream_name"] == "1001.111111111111"
    assert hook(client, "on_publish", ticket["stream_name"], ticket["ticket"]).json()["code"] == 0
    for action, stream, key, app in (
        ("on_play", ticket["stream_name"], HOOK_KEY, "live"),
        ("on_publish", "1002.111111111111", HOOK_KEY, "live"),
        ("on_publish", ticket["stream_name"], "wrong-callback-key", "live"),
        ("on_publish", ticket["stream_name"], HOOK_KEY, "another-app"),
    ):
        response = hook(client, action, stream, ticket["ticket"], key=key, app=app)
        assert response.status_code >= 400 or response.json()["code"] != 0


def test_ticket_rechecks_current_room_permissions(chat):
    server, client = chat
    headers = user_headers(MEMBER)
    denied = issue(client, user_headers(OUTSIDER), "play")
    assert denied.status_code == 403
    denied = issue(client, headers, "publish", speaker=OWNER)
    assert denied.status_code == 403
    granted = issue(client, headers, "publish", speaker=MEMBER).json()["data"]
    assert server.user_manager.mute_user(ROOM, MEMBER)
    denied = hook(client, "on_publish", granted["stream_name"], granted["ticket"])
    assert denied.status_code >= 400 or denied.json()["code"] != 0


def test_third_party_needs_both_app_and_user_identity(chat):
    _, client = chat
    assert client.post("/api/v1/app/verify", json=app_credentials(app_key="wrong")).status_code == 401
    assert client.post("/api/v1/app/verify", json=app_credentials(package_name="other")).status_code == 401
    app_token = client.post("/api/v1/app/verify", json=app_credentials()).json()["data"]["app_token"]
    assert issue(client, {"X-App-Token": app_token}, "play").status_code == 401
    headers = user_headers(MEMBER, app_id=APP_ID)
    assert issue(client, headers, "play").status_code == 401
    headers["X-App-Token"] = app_token
    ticket = issue(client, headers, "play").json()["data"]
    assert hook(client, "on_play", ticket["stream_name"], ticket["ticket"]).json()["code"] == 0
    assert issue(client, headers, "play", room=OTHER_ROOM, speaker=OUTSIDER).status_code == 403


def test_join_does_not_trust_requested_owner_role(chat):
    _, client = chat
    result = client.post(
        f"/api/v1/room/{ROOM}/join", headers=user_headers(OUTSIDER), json={"role": "owner"},
    )
    assert result.status_code == 403 or result.json()["code"] == 403


def test_revoked_room_pass_blocks_rejoin_and_previously_issued_media_ticket(chat):
    server, client = chat
    headers = user_headers(MEMBER)
    ticket = issue(client, headers, "play").json()["data"]
    assert server.pass_code_store.revoke(MEMBER, ROOM, operator_id=OWNER)
    result = client.post(f"/api/v1/room/{ROOM}/join", headers=headers, json={})
    assert result.status_code == 403 or result.json()["code"] == 403
    assert issue(client, headers, "play").status_code == 403
    assert hook(client, "on_play", ticket["stream_name"], ticket["ticket"]).status_code == 403


def test_public_rtc_checks_ticket_before_forwarding_sdp(chat, monkeypatch):
    server, client = chat
    calls = []

    async def forward(action, payload):
        calls.append((action, payload))
        return {"code": 0, "sdp": "test-answer"}

    monkeypatch.setattr(server, "forward_rtc_offer", forward, raising=False)
    ticket = issue(client, user_headers(OWNER), "publish").json()["data"]
    offer = {"sdp": "test-offer", "streamurl": f"webrtc://api.example.test/live/{ticket['stream_name']}"}
    path = "/rtc/v1/publish/"
    assert client.post(path, json=offer).status_code == 401
    assert not calls
    offer["streamurl"] += f"?srs_ticket={ticket['ticket']}"
    assert client.post("/rtc/v1/play/", json=offer).status_code == 403
    assert not calls
    assert client.post(path, json=offer).json()["sdp"] == "test-answer"
    assert calls == [("publish", {
        "sdp": "test-offer",
        "streamurl": f"webrtc://localhost/live/{ticket['stream_name']}?srs_ticket={ticket['ticket']}",
    })]


def test_flv_access_check_requires_play_ticket(chat):
    _, client = chat
    play = issue(client, user_headers(MEMBER), "play").json()["data"]
    publish = issue(client, user_headers(OWNER), "publish").json()["data"]
    path = "/internal/media/flv-auth"
    headers = {"X-Media-Stream": play["stream_name"]}
    assert client.get(path, headers=headers).status_code == 401
    headers["X-Media-Query"] = f"srs_ticket={publish['ticket']}"
    assert client.get(path, headers=headers).status_code == 403
    headers["X-Media-Query"] = f"srs_ticket={play['ticket']}"
    assert client.get(path, headers=headers).status_code == 204


def test_expired_and_logged_out_credentials_cannot_reuse_media_ticket(chat):
    server, client = chat
    headers = user_headers(OWNER)
    granted = issue(client, headers, "publish").json()["data"]
    claims = jwt.decode(granted["ticket"], options={"verify_signature": False})
    expired = jwt.encode({**claims, "exp": int(time.time()) - 1}, server.JWT_SECRET, algorithm="HS256")
    assert hook(client, "on_publish", granted["stream_name"], expired).status_code == 401
    assert client.post("/api/v1/auth/logout", headers=headers).json()["code"] == 0
    assert hook(client, "on_publish", granted["stream_name"], granted["ticket"]).status_code == 401


def test_disabling_partner_revokes_its_existing_media_tickets(chat, monkeypatch):
    server, client = chat
    app_token = client.post("/api/v1/app/verify", json=app_credentials()).json()["data"]["app_token"]
    headers = {**user_headers(MEMBER, app_id=APP_ID), "X-App-Token": app_token}
    granted = issue(client, headers, "play").json()["data"]
    registered = server.media_authorization.applications[APP_ID]
    monkeypatch.setitem(server.media_authorization.applications, APP_ID, {**registered, "enabled": False})
    assert issue(client, headers, "play").status_code == 401
    assert hook(client, "on_play", granted["stream_name"], granted["ticket"]).status_code == 401
    assert issue(client, user_headers(OWNER), "play").json()["code"] == 0


def test_unregistered_partner_session_cannot_use_http_or_websocket(chat):
    server, client = chat
    headers = user_headers(MEMBER, app_id="removed-application")
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401
    token = headers["Authorization"].removeprefix("Bearer ")
    payload = jwt.decode(token, server.JWT_SECRET, algorithms=["HS256"])
    assert not server.can_access_room_socket(payload, ROOM, MEMBER)


def test_current_speakers_only_returns_active_streams_in_authorized_room(chat, monkeypatch):
    server, client = chat

    async def active_streams(**_):
        return [
            {"app": "live", "name": "1001.111111111111", "publish": {"active": True}},
            {"app": "live", "name": "1001.222222222222", "publish": {"active": False}},
            {"app": "live", "name": "1002.333333333333", "publish": {"active": True}},
            {"app": "other", "name": "1001.222222222222", "publish": {"active": True}},
        ]

    monkeypatch.setattr(server, "_srs_list_streams", active_streams)
    path = f"/api/v1/room/{ROOM}/speaking"
    assert client.get(path, headers=user_headers(OUTSIDER)).status_code == 403
    assert client.get(path, headers=user_headers(MEMBER)).json()["data"]["speaking_users"] == [OWNER]


def room_socket(client, user_id, headers):
    token = headers["Authorization"].removeprefix("Bearer ")
    return client.websocket_connect(f"/ws?room={ROOM}&user={user_id}&token={token}")


def assert_room_socket_closed(socket):
    assert socket.receive() == {
        "type": "websocket.close", "code": 1008, "reason": "Room access denied",
    }


def test_kick_closes_room_socket_and_notifies_remaining_members(chat, monkeypatch):
    server, client = chat

    def notice_unavailable(*args, **kwargs):
        raise OSError("notice server unavailable in test")

    monkeypatch.setattr("requests.post", notice_unavailable)
    with room_socket(client, OWNER, user_headers(OWNER)) as owner:
        assert owner.receive_json()["type"] == "connected"
        assert owner.receive_json()["data"]["online_status"] == "online"
        with room_socket(client, MEMBER, user_headers(MEMBER)) as member:
            assert member.receive_json()["type"] == "connected"
            assert member.receive_json()["data"]["online_status"] == "online"
            assert owner.receive_json()["user_id"] == MEMBER
            response = client.delete(
                f"/api/v1/room/{ROOM}/member/{MEMBER}/kick", headers=user_headers(OWNER),
            )
            assert response.json()["code"] == 0
            owner_event = owner.receive_json()
            assert owner_event["type"] == "member_kicked"
            assert MEMBER not in server.manager.user_connections
            owner.portal.call(server.manager.broadcast_to_room, ROOM, {"type": "later_room_event"})
            assert owner.receive_json()["type"] == "later_room_event"
            terminal_event = member.receive_json()
            assert terminal_event == owner_event
            assert terminal_event["type"] == "member_kicked"
            assert terminal_event["room_id"] == ROOM
            assert terminal_event["user_id"] == MEMBER
            assert terminal_event["operator_id"] == OWNER
            assert_room_socket_closed(member)
            owner.send_json({"type": "history_sync"})
            assert owner.receive_json()["type"] == "history_sync"


@pytest.mark.parametrize("path", ["logout", "logout-all"])
def test_logout_closes_established_room_socket(chat, path):
    server, client = chat
    headers = user_headers(MEMBER)
    logout_headers = headers if path == "logout" else user_headers(MEMBER)
    with room_socket(client, MEMBER, headers) as socket:
        assert socket.receive_json()["type"] == "connected"
        assert socket.receive_json()["data"]["online_status"] == "online"
        assert client.post(f"/api/v1/auth/{path}", headers=logout_headers, json={}).json()["code"] == 0
        assert MEMBER not in server.manager.user_connections
        assert_room_socket_closed(socket)


def test_room_socket_rejects_history_after_token_expires(chat, monkeypatch):
    _, client = chat
    headers = user_headers(MEMBER)
    claims = jwt.decode(headers["Authorization"][7:], options={"verify_signature": False})
    with room_socket(client, MEMBER, headers) as socket:
        assert socket.receive_json()["type"] == "connected"
        assert socket.receive_json()["data"]["online_status"] == "online"
        with monkeypatch.context() as expired:
            expired.setattr(time, "time", lambda: claims["exp"] + 1)
            socket.send_json({"type": "history_sync"})
            assert_room_socket_closed(socket)



@pytest.mark.parametrize("trigger", ["broadcast", "excluded_broadcast", "direct", "heartbeat", "chat_message"])
def test_room_pass_revocation_blocks_established_socket(chat, trigger):
    server, client = chat
    manager = server.manager
    with room_socket(client, MEMBER, user_headers(MEMBER)) as socket:
        assert socket.receive_json()["type"] == "connected"
        assert socket.receive_json()["data"]["online_status"] == "online"
        assert server.pass_code_store.revoke(MEMBER, ROOM, operator_id=OWNER)

        event = {"type": "test_room_event"}
        if trigger == "broadcast":
            socket.portal.call(manager.broadcast_to_room, ROOM, event)
        elif trigger == "excluded_broadcast":
            socket.portal.call(manager.broadcast_to_room_exclude, ROOM, event, {OWNER})
        elif trigger == "direct":
            socket.portal.call(manager.send_to_user, MEMBER, event)
        elif trigger == "heartbeat":
            socket.portal.call(manager.check_heartbeats)
        else:
            socket.send_json({
                "type": "chat_message", "client_msg_id": "revoked-room-session", "content": "denied",
            })

        assert_room_socket_closed(socket)
        assert MEMBER not in manager.user_connections
        assert all(
            item.get("content") != "denied"
            for item in server.message_store.history(ROOM, after_seq=0, limit=200)
        )


@pytest.mark.parametrize("revoked_first", [True, False])
def test_logout_only_closes_sockets_for_the_revoked_token(chat, revoked_first):
    server, client = chat
    old_headers = user_headers(MEMBER)
    new_headers = user_headers(MEMBER, app_id=APP_ID)
    first_headers, second_headers = (old_headers, new_headers) if revoked_first else (new_headers, old_headers)
    with ExitStack() as stack:
        first = stack.enter_context(room_socket(client, MEMBER, first_headers))
        assert first.receive_json()["type"] == "connected"
        assert first.receive_json()["data"]["online_status"] == "online"
        second = stack.enter_context(room_socket(client, MEMBER, second_headers))
        assert second.receive_json()["type"] == "connected"
        assert first.receive_json()["data"]["online_status"] == "online"
        assert second.receive_json()["data"]["online_status"] == "online"

        revoked, replacement = (first, second) if revoked_first else (second, first)
        assert client.post("/api/v1/auth/logout", headers=old_headers).json()["code"] == 0
        assert_room_socket_closed(revoked)

        replacement.portal.call(server.manager.send_to_user, MEMBER, {"type": "test_direct_event"})
        assert replacement.receive_json()["type"] == "test_direct_event"
        replacement.send_json({"type": "history_sync"})
        assert replacement.receive_json()["type"] == "history_sync"
