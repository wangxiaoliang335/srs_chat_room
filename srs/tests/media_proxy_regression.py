#!/usr/bin/env python3
"""Run the deployed media proxy config against local Docker TCP/HTTP fixtures.

Requires Docker, Python 3.10+ and OpenSSL. No host ports are published; all
connections stay in a temporary internal Docker network. The fixtures check
proxy admission and forwarding, not RTMP/FLV codec correctness or real tickets.
"""

import argparse
from collections import Counter
from contextlib import ExitStack, closing
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socket
from socketserver import BaseRequestHandler, ThreadingTCPServer
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs
import uuid


CASES = ("rtmps-source", "rtmps-global", "flv-global", "tls-handshake")
STREAM = "42.012345abcdef"
PROBE = b"media-proxy-regression"


def wait_for(label, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {label}")


def run_fixture():
    counts = Counter()
    lock = threading.Lock()

    def record(name, delta):
        with lock:
            counts[name] += delta

    class RtmpHandler(BaseRequestHandler):
        def handle(self):
            record("rtmp_accepted", 1)
            record("rtmp_active", 1)
            try:
                while data := self.request.recv(4096):
                    self.request.sendall(data)
            except OSError:
                pass
            finally:
                record("rtmp_active", -1)

    class HttpHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def reply(self, status, body=b""):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.server.server_port == 9999:
                with lock:
                    body = json.dumps(counts).encode()
                self.reply(200, body)
                return
            if self.server.server_port == 8085:
                record("auth_requests", 1)
                query = parse_qs(self.headers.get("X-Media-Query", ""))
                allowed = (self.path == "/internal/media/flv-auth"
                           and self.headers.get("X-Media-Stream") == STREAM
                           and query.get("srs_ticket") == ["fixture-authorized"])
                self.reply(204 if allowed else 403)
                return
            if self.path != f"/live/{STREAM}.flv?srs_ticket=fixture-authorized":
                self.reply(400)
                return
            record("flv_accepted", 1)
            record("flv_active", 1)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "video/x-flv")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"FLV")
                while True:
                    self.wfile.write(bytes(256))
                    self.wfile.flush()
                    time.sleep(0.02)
            except OSError:
                pass
            finally:
                record("flv_active", -1)

    class RtmpServer(ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with ExitStack() as stack:
        servers = [stack.enter_context(RtmpServer(("0.0.0.0", 1935), RtmpHandler))]
        servers += [stack.enter_context(ThreadingHTTPServer(("0.0.0.0", port), HttpHandler))
                    for port in (8080, 8085, 9999)]
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        threading.Event().wait()


def stats():
    with closing(HTTPConnection("srs", 9999, timeout=2)) as connection:
        connection.request("GET", "/")
        return Counter(json.loads(connection.getresponse().read()))


def open_rtmps(source):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection(("127.0.0.1", 1936), timeout=2,
                                   source_address=(source, 0))
    try:
        connection = context.wrap_socket(raw, server_hostname="fixture.test")
        try:
            connection.sendall(PROBE)
            if connection.recv(len(PROBE)) != PROBE:
                raise AssertionError("RTMPS payload did not reach the upstream intact")
        except BaseException:
            connection.close()
            raise
        return connection
    except BaseException:
        raw.close()
        raise


def reject_rtmps(source):
    before = stats()["rtmp_accepted"]
    try:
        with open_rtmps(source):
            raise AssertionError("Excess RTMPS connection reached the upstream")
    except TimeoutError:
        raise AssertionError("Excess RTMPS connection hung instead of being rejected") from None
    except (ConnectionError, ssl.SSLError):
        pass
    if stats()["rtmp_accepted"] != before:
        raise AssertionError("Rejected RTMPS connection consumed an upstream connection")


def verify_rtmps_clients(connections):
    for connection in connections:
        connection.sendall(PROBE)
        if connection.recv(len(PROBE)) != PROBE:
            raise AssertionError("Existing RTMPS client was disrupted by excess load")


def hold_rtmps(stack, sources):
    return [stack.enter_context(open_rtmps(source)) for source in sources]


def test_rtmps_source():
    with ExitStack() as stack:
        held = hold_rtmps(stack, ["127.0.0.2"] * 16)
        reject_rtmps("127.0.0.2")
        # An unrelated source can still connect below the global capacity.
        held += hold_rtmps(stack, ["127.0.0.3"])
        verify_rtmps_clients(held)
        held.pop().close()
        held.pop().close()
        wait_for("two RTMPS slots to be released", lambda: stats()["rtmp_active"] == 15)
        held += hold_rtmps(stack, ["127.0.0.2"])
        verify_rtmps_clients(held)


def test_rtmps_global():
    with ExitStack() as stack:
        held = hold_rtmps(stack, ["127.0.0.2"] * 16 + ["127.0.0.3"] * 16 + ["127.0.0.4"] * 8)
        reject_rtmps("127.0.0.5")
        verify_rtmps_clients(held)
        held.pop().close()
        wait_for("one RTMPS slot to be released", lambda: stats()["rtmp_active"] == 39)
        held += hold_rtmps(stack, ["127.0.0.5"])
        verify_rtmps_clients(held)


def open_flv(stack, *, authorized=True, host="fixture.test", forwarded="192.0.2.1"):
    connection = stack.enter_context(closing(HTTPConnection("127.0.0.1", 8080, timeout=2)))
    ticket = "fixture-authorized" if authorized else "invalid"
    connection.request("GET", f"/live/{STREAM}.flv?srs_ticket={ticket}",
                       headers={"Host": host, "X-Forwarded-For": forwarded, "X-Real-IP": forwarded})
    response = stack.enter_context(closing(connection.getresponse()))
    return connection, response


def require_flv(stack, **kwargs):
    connection, response = open_flv(stack, **kwargs)
    if response.status != 200 or response.read(3) != b"FLV":
        raise AssertionError(f"Authorized FLV below capacity failed with HTTP {response.status}")
    return connection, response


def test_flv_global():
    with ExitStack() as stack:
        before = stats()
        _, denied = open_flv(stack, authorized=False)
        if denied.status != 403 or stats()["flv_accepted"] != before["flv_accepted"]:
            raise AssertionError("Unauthorized FLV was not rejected before reaching upstream")
        # Keep both public media paths at capacity together: 40 + 40 = 80.
        rtmp = hold_rtmps(stack, ["127.0.0.2"] * 16 + ["127.0.0.3"] * 16 + ["127.0.0.4"] * 8)
        held = [require_flv(stack, host=f"client-{i}.test", forwarded=f"192.0.2.{i + 1}")
                for i in range(40)]
        before = stats()
        if before["rtmp_active"] != 40 or before["flv_active"] != 40:
            raise AssertionError("The proxy did not sustain 80 authorized media connections")
        _, excess = open_flv(stack, host="another-host.test", forwarded="203.0.113.99")
        if excess.status != 429:
            raise AssertionError(f"Excess FLV returned HTTP {excess.status}, expected 429")
        if stats()["flv_accepted"] != before["flv_accepted"]:
            raise AssertionError("Excess FLV reached the upstream")
        verify_rtmps_clients(rtmp)
        for _, response in held:
            if not response.read(1):
                raise AssertionError("Existing FLV client was disconnected by excess load")
        connection, response = held.pop()
        response.close()
        connection.close()
        wait_for("one FLV slot to be released", lambda: stats()["flv_active"] == 39)
        require_flv(stack, host="replacement.test")


def test_tls_handshake():
    before = stats()["rtmp_accepted"]
    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", 1936), timeout=2,
                                  source_address=("127.0.0.2", 0)) as idle:
        idle.settimeout(7)
        try:
            if idle.recv(1):
                raise AssertionError("Idle TLS socket unexpectedly received a payload")
        except TimeoutError:
            raise AssertionError("Idle TLS handshake was not closed within 7 seconds") from None
        except ConnectionResetError:
            pass
    if time.monotonic() - started < 4:
        raise AssertionError("TLS listener rejected a below-capacity socket before its handshake deadline")
    if stats()["rtmp_accepted"] != before:
        raise AssertionError("Incomplete TLS handshake reached the upstream")
    with open_rtmps("127.0.0.2"):
        pass


def run_clients(case):
    def ready():
        try:
            with closing(HTTPConnection("127.0.0.1", 8080, timeout=1)) as connection:
                connection.request("GET", "/")
                return connection.getresponse().status == 404 and not stats()["rtmp_active"]
        except OSError:
            return False

    wait_for("Nginx and fixture startup", ready, timeout=10)
    failed = []
    for name in CASES if case == "all" else (case,):
        wait_for("previous clients to disconnect",
                 lambda: stats()["rtmp_active"] == stats()["flv_active"] == 0)
        try:
            globals()["test_" + name.replace("-", "_")]()
        except (AssertionError, OSError) as error:
            failed.append(name)
            print(f"FAIL {name}: {error}", flush=True)
        else:
            print(f"PASS {name}", flush=True)
    if failed:
        raise SystemExit(1)


def run_docker(args):
    for executable in ("docker", args.openssl):
        if not shutil.which(executable):
            raise SystemExit(f"Required executable not found: {executable}")
    config = args.config.resolve(strict=True)
    script = Path(__file__).resolve()
    name = "media-proxy-regression-" + uuid.uuid4().hex[:12]

    def command(*argv, check=True):
        result = subprocess.run(argv, text=True, capture_output=True)
        if check and result.returncode:
            raise RuntimeError(result.stderr or result.stdout or f"Command failed: {argv[0]}")
        return result

    def bind(source, target):
        return f"type=bind,src={source},dst={target},readonly"

    with tempfile.TemporaryDirectory(prefix=name + "-") as directory:
        tls = Path(directory)
        command(args.openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                "-subj", "/CN=fixture.test", "-keyout", str(tls / "privkey.pem"),
                "-out", str(tls / "fullchain.pem"))
        network_created = False
        try:
            command("docker", "network", "create", "--internal", name)
            network_created = True
            command("docker", "run", "-d", "--name", name + "-backend", "--network", name,
                    "--network-alias", "srs", "--network-alias", "chat-api",
                    "--mount", bind(script, "/probe.py"), args.python_image,
                    "python", "/probe.py", "--fixture")
            command("docker", "run", "-d", "--name", name + "-nginx", "--network", name,
                    "--memory", "64m", "--pids-limit", "32",
                    "--mount", bind(config, "/etc/nginx/nginx.conf"),
                    "--mount", bind(tls, "/etc/chat-tls"),
                    args.nginx_image, "nginx", "-g", "daemon off;")
            result = command("docker", "run", "--rm", "--name", name + "-client",
                             "--network", "container:" + name + "-nginx",
                             "--mount", bind(script, "/probe.py"), args.python_image,
                             "python", "/probe.py", "--client", "--case", args.case, check=False)
            print(result.stdout, end="", flush=True)
            if result.returncode:
                print(result.stderr, end="", file=sys.stderr)
                print(command("docker", "logs", name + "-nginx", check=False).stderr,
                      end="", file=sys.stderr)
                raise SystemExit(result.returncode)
        finally:
            for suffix in ("-client", "-nginx", "-backend"):
                command("docker", "rm", "-f", name + suffix, check=False)
            if network_created:
                command("docker", "network", "rm", name, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).resolve().parent.parent / "deploy/dev-chat-media.conf")
    parser.add_argument("--case", choices=("all", *CASES), default="all")
    parser.add_argument("--nginx-image", default="nginx:1.27-alpine")
    parser.add_argument("--python-image", default="python:3.12-alpine")
    parser.add_argument("--openssl", default="openssl")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fixture", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--client", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.fixture:
        run_fixture()
    elif args.client:
        run_clients(args.case)
    else:
        run_docker(args)


if __name__ == "__main__":
    main()
