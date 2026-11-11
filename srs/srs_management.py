"""Authenticated SRS 6.0.134 management requests."""

import asyncio
import logging
import time

import aiohttp

from media_sessions import REGISTRATION_GRACE_SECONDS


logger = logging.getLogger(__name__)
CLIENT_NOT_FOUND = 2049
MAX_CLIENT_SNAPSHOT = 10000
CLOSE_CONFIRMATION_SECONDS = 2


class SrsManagement:
    def __init__(self, url, username, password):
        self.url = url.rstrip("/")
        self.authorization = aiohttp.encode_basic_auth(username, password) if username and password else None

    def _session(self):
        headers = {"Authorization": self.authorization} if self.authorization else None
        return aiohttp.ClientSession(
            headers=headers, timeout=aiohttp.ClientTimeout(total=2), connector=aiohttp.TCPConnector(limit=16),
        )

    async def _request(self, session, method, path, **kwargs):
        async with session.request(method, f"{self.url}/api/v1/{path}", allow_redirects=False, **kwargs) as response:
            if response.status != 200:
                raise ValueError(f"SRS management HTTP {response.status}")
            data = await response.json()
            if not isinstance(data, dict) or type(data.get("code")) is not int:
                raise ValueError("invalid SRS management response")
            return data

    async def list_streams(self):
        async with self._session() as session:
            data = await self._request(session, "GET", "streams/")
        if data["code"] != 0 or not isinstance(data.get("streams"), list):
            raise ValueError("SRS rejected stream list request")
        return data["streams"]

    async def list_connections(self):
        if self.authorization is None:
            return None
        async with self._session() as session:
            data = await self._request(session, "GET", "clients/", params={"count": MAX_CLIENT_SNAPSHOT})
        clients = data.get("clients")
        server, service = self._process_identity(data)
        if data["code"] != 0 or not isinstance(clients, list):
            raise ValueError("SRS rejected client list request")
        if len(clients) >= MAX_CLIENT_SNAPSHOT:
            return None
        return {(server, service, client["id"]) for client in clients}

    @staticmethod
    def _process_identity(data):
        server, service = data.get("server"), data.get("service")
        if not isinstance(server, str) or not server or not isinstance(service, str) or not service:
            raise ValueError("missing SRS management process identity")
        return server, service

    async def _close_connection(self, session, connection):
        path = f"clients/{connection.client_id}"
        data = await self._request(session, "GET", path)
        if data["code"] == CLIENT_NOT_FOUND:
            # In SRS 6.0.134 the RTC play hook runs before its client statistics are registered.
            return time.time() - connection.accepted_at >= REGISTRATION_GRACE_SECONDS
        if data["code"] != 0:
            raise ValueError(f"SRS client lookup failed with code {data['code']}")
        if self._process_identity(data) != connection.identity[:2]:
            return True

        client = data.get("client")
        if not isinstance(client, dict) or client.get("id") != connection.client_id:
            raise ValueError("invalid SRS client lookup response")
        publishing = connection.claims["operation"] == "publish"
        name = connection.claims["stream"]
        names = (name, f"{name}.flv") if not publishing and client.get("type") == "flv-play" else (name,)
        if client.get("publish") is not publishing or client.get("name") not in names:
            return True
        if client.get("url") != f"/live/{name}":
            return True

        result = await self._request(session, "DELETE", path)
        if result["code"] == CLIENT_NOT_FOUND:
            return True
        if result["code"] != 0:
            raise ValueError(f"SRS client close failed with code {result['code']}")
        # SRS acknowledges the interrupt before the streaming coroutine has stopped.
        async with asyncio.timeout(CLOSE_CONFIRMATION_SECONDS):
            while True:
                current = await self._request(session, "GET", path)
                if current["code"] == CLIENT_NOT_FOUND:
                    return True
                if current["code"] != 0:
                    raise ValueError(f"SRS client lookup failed with code {current['code']}")
                if self._process_identity(current) != connection.identity[:2]:
                    return True
                await asyncio.sleep(0.05)

    async def close_connections(self, connections):
        if self.authorization is None:
            logger.warning("SRS media disconnection pending: management credentials are missing")
            return []
        async with self._session() as session:
            async def attempt(connection):
                try:
                    return await self._close_connection(session, connection)
                except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                    logger.warning("SRS media disconnection pending for client %s: %s", connection.client_id, error)
                    return False

            results = await asyncio.gather(*(attempt(connection) for connection in connections))
        return [conn for conn, closed in zip(connections, results) if closed]
