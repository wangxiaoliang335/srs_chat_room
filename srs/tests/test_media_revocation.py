import asyncio
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from test_media_auth import (
    APP_ID, HOOK_KEY, MEMBER, OTHER_ROOM, OUTSIDER, OWNER, ROOM,
    app_credentials, chat, issue, user_headers,
)


def partner_headers(client, uid):
    response = client.post("/api/v1/app/verify", json=app_credentials())
    return {**user_headers(uid, app_id=APP_ID), "X-App-Token": response.json()["data"]["app_token"]}


@pytest.fixture
def media(chat, monkeypatch):
    server, client = chat
    server_id, service_id = "vid-test", uuid4().hex[:8]
    clients = {}
    unavailable = threading.Event()
    basic_auth = "Basic " + base64.b64encode(b"media-operator:media-test-password").decode()

    class ManagementHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def handle_management(self):
            prefix = "/api/v1/clients/"
            path = urlsplit(self.path).path
            if self.headers.get("Authorization") != basic_auth:
                self.send_error(401)
                return
            if unavailable.is_set():
                self.send_error(503)
                return
            if not path.startswith(prefix):
                self.send_error(404)
                return
            client_id = path.removeprefix(prefix)
            body = {"code": 0, "server": server_id, "service": service_id, "pid": "1"}
            if not client_id:
                body["clients"] = list(clients.values())
            elif client_id not in clients:
                body = {"code": 2049}
            elif self.command == "DELETE":
                del clients[client_id]
            else:
                body["client"] = clients[client_id]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        do_GET = handle_management
        do_DELETE = handle_management

    with ThreadingHTTPServer(("127.0.0.1", 0), ManagementHandler) as management:
        thread = threading.Thread(target=management.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setattr(server, "SRS_HTTP_API", f"http://127.0.0.1:{management.server_port}")
        monkeypatch.setenv("SRS_HTTP_API_USERNAME", "media-operator")
        monkeypatch.setenv("SRS_HTTP_API_PASSWORD", "media-test-password")

        def send_hook(action, stream, client_id, ticket=None):
            return client.post(
                f"/api/v1/streams/{action}", params={"key": HOOK_KEY},
                json={
                    "action": action, "app": "live", "vhost": "__defaultVhost__", "stream": stream,
                    "param": f"?srs_ticket={ticket}" if ticket else "", "client_id": client_id,
                    "server_id": server_id, "service_id": service_id,
                },
            )

        def connect(headers, operation, speaker, *, room=ROOM, protocol="rtmp", ready=True):
            response = issue(client, headers, operation, room=room, speaker=speaker)
            assert response.json()["code"] == 0, response.json()
            grant = response.json()["data"]
            connection = uuid4().hex[:8]
            stream = grant["stream_name"]
            name = f"{stream}.flv" if protocol == "flv" else stream

            def add_client():
                clients[connection] = {
                    "id": connection, "name": name, "url": f"/live/{stream}",
                    "publish": operation == "publish", "type": f"{protocol}-{operation}",
                }

            if ready:
                add_client()
            response = send_hook(f"on_{operation}", stream, connection, grant["ticket"])
            assert response.status_code == 200 and response.json()["code"] == 0
            return connection, stream, add_client

        try:
            yield client, clients, connect, send_hook, unavailable
        finally:
            management.shutdown()
            thread.join(timeout=2)


@pytest.mark.parametrize("action", [f"member/{MEMBER}/mute", f"member/{MEMBER}/mic/disable", "mute-all"])
def test_moderation_disconnects_only_disallowed_publishers(media, action):
    client, clients, connect, _, _ = media
    owner, member = partner_headers(client, OWNER), partner_headers(client, MEMBER)
    publisher, _, _ = connect(member, "publish", MEMBER)
    owner_publisher, _, _ = connect(owner, "publish", OWNER)
    member_listener, _, _ = connect(member, "play", OWNER, protocol="flv")

    response = client.post(f"/api/v1/room/{ROOM}/{action}", headers=owner, json={})

    assert response.json()["code"] == 0
    assert publisher not in clients
    assert set(clients) == {owner_publisher, member_listener}


def test_kick_disconnects_members_media_and_listeners_to_their_stream(media):
    client, clients, connect, _, _ = media
    owner, member, outsider = (partner_headers(client, uid) for uid in (OWNER, MEMBER, OUTSIDER))
    connect(member, "publish", MEMBER)
    connect(member, "play", OWNER, protocol="flv")
    connect(owner, "play", MEMBER, protocol="rtc")
    owner_publisher, _, _ = connect(owner, "publish", OWNER)
    unrelated, _, _ = connect(outsider, "publish", OUTSIDER, room=OTHER_ROOM)

    response = client.delete(f"/api/v1/room/{ROOM}/member/{MEMBER}/kick", headers=owner)

    assert response.json()["code"] == 0
    assert set(clients) == {owner_publisher, unrelated}


def test_logout_disconnects_media_for_only_its_user_session(media):
    client, clients, connect, _, _ = media
    member, other_session = partner_headers(client, MEMBER), partner_headers(client, MEMBER)
    connect(member, "publish", MEMBER)
    connect(member, "play", OWNER, protocol="flv")
    unrelated, _, _ = connect(other_session, "play", OWNER, protocol="rtc")

    response = client.post("/api/v1/auth/logout", headers=member)

    assert response.json()["code"] == 0
    assert set(clients) == {unrelated}


def test_srs_cannot_start_media_without_a_managed_client_identity(media):
    client, _, _, send_hook, _ = media
    grant = issue(client, partner_headers(client, OWNER), "publish").json()["data"]

    response = send_hook("on_publish", grant["stream_name"], "", grant["ticket"])

    assert response.status_code == 403


def test_rtc_play_created_after_kick_is_retried(media):
    client, clients, connect, _, _ = media
    owner, member = partner_headers(client, OWNER), partner_headers(client, MEMBER)
    with client:
        connection, _, add_client = connect(member, "play", OWNER, protocol="rtc", ready=False)
        assert client.delete(f"/api/v1/room/{ROOM}/member/{MEMBER}/kick", headers=owner).json()["code"] == 0
        add_client()

        deadline = time.monotonic() + 6
        while connection in clients and time.monotonic() < deadline:
            time.sleep(0.02)

    assert connection not in clients


def test_pending_disconnect_survives_chat_restart_and_unmute(chat, media, monkeypatch):
    server, _ = chat
    client, clients, connect, _, unavailable = media
    owner, member = partner_headers(client, OWNER), partner_headers(client, MEMBER)
    publisher, _, _ = connect(member, "publish", MEMBER)
    unrelated, _, _ = connect(owner, "publish", OWNER)
    unavailable.set()

    assert client.post(f"/api/v1/room/{ROOM}/member/{MEMBER}/mute", headers=owner, json={}).json()["code"] == 0
    assert publisher in clients
    assert client.post(f"/api/v1/room/{ROOM}/member/{MEMBER}/unmute", headers=owner, json={}).json()["code"] == 0

    registry = server.media_sessions
    monkeypatch.setattr(server, "media_sessions", type(registry)(registry.path))
    unavailable.clear()
    with client:
        deadline = time.monotonic() + 6
        while publisher in clients and time.monotonic() < deadline:
            time.sleep(0.02)

    assert set(clients) == {unrelated}


def test_room_operation_waits_for_disconnect_already_started_by_worker(tmp_path):
    from media_sessions import MediaSessions

    async def scenario():
        sessions = MediaSessions(tmp_path / "sessions.sqlite3")
        sessions.remember({"server_id": "srs", "service_id": "service", "client_id": "client"}, {})
        revoked = sessions.mark_pending(lambda claims: True)
        started, allow_close = asyncio.Event(), asyncio.Event()

        class Management:
            async def close_connections(self, pending):
                started.set()
                await allow_close.wait()
                return pending

        management = Management()
        worker = asyncio.create_task(sessions.close_pending(management))
        await started.wait()
        operation = asyncio.create_task(sessions.close_pending(management, revoked))
        await asyncio.sleep(0)
        try:
            assert not operation.done()
        finally:
            allow_close.set()
            await asyncio.gather(worker, operation)

    asyncio.run(scenario())


def test_srs_close_waits_until_client_is_removed(monkeypatch):
    from media_sessions import MediaConnection
    from srs_management import SrsManagement

    active = {"code": 0, "server": "srs", "service": "service",
              "client": {"id": "client", "name": "1.111111111111.flv", "type": "flv-play",
                         "publish": False, "url": "/live/1.111111111111"}}
    expected = iter((("GET", active), ("DELETE", {"code": 0}), ("GET", active), ("GET", {"code": 2049})))
    connection = MediaConnection(("srs", "service", "client"),
                                 {"stream": "1.111111111111", "operation": "play"}, time.time(), True)
    manager = SrsManagement("http://srs:1985", "control", "password")

    async def request(session, method, path):
        assert path == "clients/client"
        expected_method, response = next(expected)
        assert method == expected_method
        return response

    monkeypatch.setattr(manager, "_request", request)
    assert asyncio.run(manager.close_connections([connection])) == [connection]
    assert next(expected, None) is None
