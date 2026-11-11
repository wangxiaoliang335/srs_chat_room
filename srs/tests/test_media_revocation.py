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
            if path == "/api/v1/streams/":
                body = {"code": 0, "streams": [{"name": item["name"]} for item in clients.values() if item["publish"]]}
            elif not path.startswith(prefix):
                self.send_error(404)
                return
            else:
                client_id = path.removeprefix(prefix)
                body = {"code": 0, "server": server_id, "service": service_id, "pid": "1"}
                if not client_id:
                    body["clients"] = list(clients.values())
                elif client_id not in clients:
                    body = {"code": 2049}
                elif self.command == "DELETE":
                    if not clients[client_id].get("keep_alive_after_delete"):
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
        response = client.delete(f"/api/v1/room/{ROOM}/member/{MEMBER}/kick", headers=owner)
        assert_pending_disconnect(response, 1)
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

    response = client.post(f"/api/v1/room/{ROOM}/member/{MEMBER}/mute", headers=owner, json={})
    assert_pending_disconnect(response, 1)
    assert publisher in clients
    assert client.post(f"/api/v1/room/{ROOM}/member/{MEMBER}/unmute", headers=owner, json={}).json()["code"] == 0
    fresh_publisher, _, _ = connect(member, "publish", MEMBER)

    registry = server.media_sessions
    monkeypatch.setattr(server, "media_sessions", type(registry)(registry.path))
    unavailable.clear()
    with client:
        deadline = time.monotonic() + 6
        while publisher in clients and time.monotonic() < deadline:
            time.sleep(0.02)

    assert set(clients) == {unrelated, fresh_publisher}


def assert_pending_disconnect(response, count):
    assert response.status_code == 503
    result = response.json()
    assert result["code"] == 503
    assert result["data"]["permission_applied"] is True
    assert result["data"]["media_disconnect"] == "pending"
    assert result["data"]["pending_media_connections"] == count
    assert result["data"]["retrying"] is True


@pytest.mark.parametrize("action", [f"member/{MEMBER}/mute", f"member/{MEMBER}/mic/disable", "mute-all"])
def test_moderation_reports_pending_then_completes_on_retry(media, action):
    client, clients, connect, _, unavailable = media
    owner, member = partner_headers(client, OWNER), partner_headers(client, MEMBER)
    publisher, _, _ = connect(member, "publish", MEMBER)
    unavailable.set()

    response = client.post(f"/api/v1/room/{ROOM}/{action}", headers=owner, json={})

    assert_pending_disconnect(response, 1)
    assert publisher in clients
    assert issue(client, member, "publish", speaker=MEMBER).status_code == 403
    unavailable.clear()
    retry = client.post(f"/api/v1/room/{ROOM}/{action}", headers=owner, json={})
    assert retry.json()["code"] == 0
    assert publisher not in clients


@pytest.mark.parametrize("prefix", ["room", "rooms"])
def test_kick_retry_checks_permission_and_closes_already_removed_members_media(chat, media, prefix):
    server, _ = chat
    client, clients, connect, _, unavailable = media
    owner, member = partner_headers(client, OWNER), partner_headers(client, MEMBER)
    publisher, _, _ = connect(member, "publish", MEMBER)
    path = f"/api/v1/{prefix}/{ROOM}/member/{MEMBER}/kick"
    unavailable.set()

    assert_pending_disconnect(client.delete(path, headers=owner), 1)
    assert server.user_manager.get_member(ROOM, MEMBER) is None
    assert issue(client, member, "publish", speaker=MEMBER).status_code == 403
    assert client.delete(path, headers=member).status_code == 403
    assert client.delete(path, headers=partner_headers(client, OUTSIDER)).status_code == 403
    assert publisher in clients
    assert_pending_disconnect(client.delete(path, headers=owner), 1)

    unavailable.clear()
    for _ in range(2):
        assert client.delete(path, headers=owner).json() == {"code": 0, "message": "success", "data": {}}
    assert publisher not in clients


def test_partial_delete_acknowledgement_reports_only_remaining_connections(media, monkeypatch):
    import srs_management

    client, clients, connect, _, _ = media
    owner, member = partner_headers(client, OWNER), partner_headers(client, MEMBER)
    publisher, _, _ = connect(member, "publish", MEMBER)
    listener, _, _ = connect(member, "play", OWNER, protocol="rtc")
    clients[publisher]["keep_alive_after_delete"] = True
    monkeypatch.setattr(srs_management, "CLOSE_CONFIRMATION_SECONDS", 0.08)
    path = f"/api/v1/room/{ROOM}/member/{MEMBER}/kick"

    assert_pending_disconnect(client.delete(path, headers=owner), 1)
    assert set(clients) == {publisher}
    assert listener not in clients

    clients[publisher]["keep_alive_after_delete"] = False
    assert client.delete(path, headers=owner).json()["code"] == 0
    assert not clients


def test_pending_disconnect_after_rejoin_does_not_close_fresh_media(chat, media):
    server, _ = chat
    client, clients, connect, _, unavailable = media
    owner, member = partner_headers(client, OWNER), partner_headers(client, MEMBER)
    old_connection, _, _ = connect(member, "publish", MEMBER)
    unavailable.set()
    assert_pending_disconnect(client.delete(f"/api/v1/room/{ROOM}/member/{MEMBER}/kick", headers=owner), 1)

    server.pass_code_store.issue(MEMBER, ROOM)
    assert client.post(f"/api/v1/room/{ROOM}/join", headers=member, json={}).json()["code"] == 0
    fresh_connection, _, _ = connect(member, "publish", MEMBER)
    unavailable.clear()
    with client:
        deadline = time.monotonic() + 6
        while old_connection in clients and time.monotonic() < deadline:
            time.sleep(0.02)

    assert set(clients) == {fresh_connection}
    assert server.user_manager.get_member(ROOM, MEMBER) is not None


@pytest.mark.parametrize("path", ["/api/v1/auth/logout", "/api/v1/auth/logout-all"])
def test_logout_reports_pending_without_restoring_revoked_session(media, path):
    client, clients, connect, _, unavailable = media
    member = partner_headers(client, MEMBER)
    publisher, _, _ = connect(member, "publish", MEMBER)
    unavailable.set()

    assert_pending_disconnect(client.post(path, headers=member, json={}), 1)
    assert publisher in clients
    assert client.get("/api/v1/auth/me", headers=member).status_code == 401
    unavailable.clear()
    with client:
        deadline = time.monotonic() + 6
        while publisher in clients and time.monotonic() < deadline:
            time.sleep(0.02)

    assert not clients


def test_close_room_reports_pending_and_allows_retry(chat, media):
    server, _ = chat
    client, clients, connect, _, unavailable = media
    owner, member = partner_headers(client, OWNER), partner_headers(client, MEMBER)
    publisher, _, _ = connect(member, "publish", MEMBER)
    unavailable.set()

    assert_pending_disconnect(client.delete(f"/api/v1/room/{ROOM}", headers=owner), 1)
    assert server.user_manager.get_room(ROOM).status.value == "closed"
    assert publisher in clients
    unavailable.clear()
    assert client.delete(f"/api/v1/room/{ROOM}", headers=owner).json()["code"] == 0
    assert not clients


@pytest.mark.parametrize("prefix", ["room", "rooms"])
def test_close_room_rejects_active_publisher_using_current_media_stream_name(chat, media, prefix):
    server, _ = chat
    client, clients, connect, _, _ = media
    owner, member = partner_headers(client, OWNER), partner_headers(client, MEMBER)
    publisher, _, _ = connect(member, "publish", MEMBER)

    response = client.delete(f"/api/v1/{prefix}/{ROOM}", headers=owner)

    assert response.status_code == 409
    assert response.json()["data"]["active"]["has_publish"] is True
    assert server.user_manager.get_room(ROOM).status.value == "active"
    assert publisher in clients


def test_close_room_does_not_match_different_room_numeric_prefix(media):
    client, clients, connect, _, _ = media
    owner, outsider = partner_headers(client, OWNER), partner_headers(client, OUTSIDER)
    publisher, _, _ = connect(outsider, "publish", OUTSIDER, room=OTHER_ROOM)
    clients["other-numeric-prefix"] = {
        "id": "other-numeric-prefix", "name": "10010.333333333333", "url": "/live/10010.333333333333",
        "publish": True, "type": "rtmp-publish",
    }

    response = client.delete(f"/api/v1/room/{ROOM}", headers=owner)

    assert response.json()["code"] == 0
    assert publisher in clients and "other-numeric-prefix" in clients


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
        assert operation.result() == 0

    asyncio.run(scenario())


def test_close_pending_reports_remaining_durable_records_and_empty_scopes(tmp_path):
    from media_sessions import MediaSessions

    path = tmp_path / "sessions.sqlite3"
    sessions = MediaSessions(path)
    for client_id in ("closed", "pending"):
        sessions.remember({"server_id": "srs", "service_id": "service", "client_id": client_id}, {})
    revoked = sessions.mark_pending(lambda claims: True)

    class PartialManagement:
        async def close_connections(self, pending):
            return [conn for conn in pending if conn.client_id == "closed"]

    assert asyncio.run(sessions.close_pending(PartialManagement(), revoked)) == 1
    restarted = MediaSessions(path)
    assert asyncio.run(restarted.close_pending(PartialManagement(), set())) == 0
    assert asyncio.run(restarted.close_pending(PartialManagement(), revoked)) == 1


def test_pending_connections_remain_in_scope_even_after_access_restored(tmp_path):
    from media_sessions import MediaSessions

    sessions = MediaSessions(tmp_path / "sessions.sqlite3")
    for client_id in ("revoked", "unrelated"):
        sessions.remember({"server_id": "srs", "service_id": "service", "client_id": client_id}, {"room": client_id})
    revoked = sessions.mark_pending(lambda claims: claims["room"] == "revoked")
    sessions.remember({"server_id": "srs", "service_id": "service", "client_id": "fresh"}, {"room": "revoked"})

    assert sessions.mark_pending(lambda claims: False, in_scope=lambda claims: claims["room"] == "revoked") == revoked


def test_close_pending_counts_callback_confirmation_when_management_times_out(tmp_path):
    from media_sessions import MediaSessions

    sessions = MediaSessions(tmp_path / "sessions.sqlite3")
    event = {
        "server_id": "srs", "service_id": "service", "client_id": "client", "action": "on_unpublish",
        "app": "live", "vhost": "__defaultVhost__", "stream": "1.111111111111",
    }
    sessions.remember(event, {"operation": "publish", "stream": "1.111111111111"})
    revoked = sessions.mark_pending(lambda claims: True)

    class CallbackConfirmedManagement:
        async def close_connections(self, pending):
            sessions.forget_callback(event, "on_unpublish", "publish")
            return []

    assert asyncio.run(sessions.close_pending(CallbackConfirmedManagement(), revoked)) == 0


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
