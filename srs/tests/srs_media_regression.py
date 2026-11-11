#!/usr/bin/env python3
"""Exercise the SRS media patches in private, loopback-only server processes."""

import argparse
import array
import base64
from contextlib import ExitStack
from http.client import IncompleteRead
import json
import math
from pathlib import Path
import resource
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
import wave


RATE = 48000
FREQUENCIES = (431, 787)
HTTP = build_opener(ProxyHandler({}))
MANAGEMENT_USER = "qa-flv-operator"
MANAGEMENT_PASSWORD = "qa-flv-password"
MANAGEMENT_AUTH = "Basic " + base64.b64encode(f"{MANAGEMENT_USER}:{MANAGEMENT_PASSWORD}".encode()).decode()
CASES = ("dotted", "shutdown", "disconnect", "redaction")


def free_tcp_ports():
    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(3)]
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]


def wait_for(label, check, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {label}")


def api(port, endpoint, method="GET"):
    request = Request(f"http://127.0.0.1:{port}/api/v1/{endpoint}",
                      headers={"Authorization": MANAGEMENT_AUTH}, method=method)
    with HTTP.open(request, timeout=1) as response:
        result = json.load(response)
    if result["code"] != 0:
        raise AssertionError(f"SRS API {endpoint} returned code {result['code']}")
    return result


def write_tone(path, frequency):
    pcm = array.array("h", (
        round(12000 * math.sin(2 * math.pi * frequency * i / RATE))
        for i in range(RATE)
    ))
    if sys.byteorder != "little":
        pcm.byteswap()
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        audio.writeframes(pcm.tobytes())


def received_tone(path, expected_hz):
    if not path.exists() or path.stat().st_size < RATE * 2:
        return False
    with path.open("rb") as audio:
        audio.seek(RATE)
        pcm = array.array("h", audio.read(RATE))
    if sys.byteorder != "little":
        pcm.byteswap()
    crossings = sum(a <= 0 < b for a, b in zip(pcm, pcm[1:]))
    measured_hz = crossings * RATE / len(pcm)
    return max(map(abs, pcm)) > 2000 and abs(measured_hz - expected_hz) < 16


def stop(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def verify_flv_disconnect(management, flv, stream, publisher):
    try:
        with HTTP.open(f"http://127.0.0.1:{management}/api/v1/versions", timeout=1):
            raise AssertionError("SRS management accepted an anonymous request")
    except HTTPError as error:
        if error.code != 401:
            raise

    def clients():
        return {client["id"]: client for client in api(management, "clients/")["clients"]}

    retained = clients()
    with HTTP.open(f"http://127.0.0.1:{flv}/live/{stream}.flv", timeout=1) as player:
        if player.read(3) != b"FLV":
            raise AssertionError("SRS did not send an FLV header")
        current = clients()
        added = set(current) - set(retained)
        if len(added) != 1:
            raise AssertionError("SRS did not register exactly one additional FLV player")
        (client_id,) = added
        if current[client_id]["type"] != "flv-play":
            raise AssertionError("Unexpected SRS client type")

        # Keep the publisher connected while its audio queue drains. Otherwise socket writes can hide a missed interrupt.
        publisher.send_signal(signal.SIGSTOP)
        try:
            time.sleep(0.3)
            api(management, f"clients/{client_id}", "DELETE")
            wait_for("the idle FLV player to disconnect", lambda: client_id not in clients(), timeout=1)
            try:
                player.read()
            except IncompleteRead:
                # Closing a live HTTP-FLV connection may interrupt the final HTTP chunk.
                pass
            if not set(retained).issubset(clients()):
                raise AssertionError("SRS disconnected an unrelated media client")
        finally:
            publisher.send_signal(signal.SIGCONT)


def run_case(binary, ffmpeg, case):
    rtmp, management, flv = free_tcp_ports()
    # Plain stream names make the shutdown regression independent of the FLV naming fix.
    names = ("qa_room.speaker_a", "qa_room.speaker_b") if case == "dotted" else ("qa_speaker_a", "qa_speaker_b")
    children = []
    with tempfile.TemporaryDirectory(prefix="srs-media-regression-") as directory, ExitStack() as stack:
        work = Path(directory)
        config = work / "srs.conf"
        config.write_text(f"""listen 127.0.0.1:{rtmp};
daemon off;
srs_log_tank console;
pid ./srs.pid;
http_api {{
    enabled on;
    listen 127.0.0.1:{management};
    auth {{ enabled on; username {MANAGEMENT_USER}; password {MANAGEMENT_PASSWORD}; }}
}}
http_server {{ enabled on; listen 127.0.0.1:{flv}; }}
vhost __defaultVhost__ {{
    play {{ mw_latency 10; }}
    publish {{ normal_timeout 20000; }}
    hls {{ enabled off; }}
    http_remux {{ enabled on; mount [vhost]/[app]/[stream].flv; }}
}}
""", encoding="utf-8")
        sources = [work / f"tone-{i}.wav" for i in range(len(names))]
        for source, hz in zip(sources, FREQUENCIES):
            write_tone(source, hz)
        ffmpeg_args = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]

        def launch(label, command):
            log = stack.enter_context((work / f"{label}.log").open("wb"))
            child = subprocess.Popen(command, cwd=work, stdout=log, stderr=subprocess.STDOUT)
            children.append(child)
            return child

        def start_server(label):
            server = launch(label, [str(binary), "-c", str(config)])
            wait_for("SRS API", lambda: str(api(management, "versions")["pid"]) == str(server.pid))
            return server

        def start_media(label):
            publishers = [
                launch(f"{label}-publish-{i}", ffmpeg_args + [
                    "-re", "-stream_loop", "-1", "-i", str(source),
                    "-vn", "-c:a", "aac", "-f", "flv", f"rtmp://127.0.0.1:{rtmp}/live/{name}",
                ])
                for i, (source, name) in enumerate(zip(sources, names))
            ]

            def streams_registered():
                live = {stream["name"]: stream["id"] for stream in api(management, "streams/")["streams"]
                        if stream["publish"]["active"]}
                return set(live) == set(names) and len(set(live.values())) == len(names)

            wait_for(f"independent published streams {names}", streams_registered)
            output = [work / f"{label}-played-{i}.pcm" for i in range(len(names))]
            players = [
                launch(f"{label}-play-{i}", ffmpeg_args + [
                    "-probesize", "8192", "-analyzeduration", "200000",
                    "-i", f"http://127.0.0.1:{flv}/live/{name}.flv",
                    "-vn", "-ac", "1", "-ar", str(RATE), "-flush_packets", "1",
                    "-f", "s16le", "-y", str(path),
                ])
                for i, (path, name) in enumerate(zip(output, names))
            ]
            wait_for("the correct tone on each HTTP-FLV URL",
                     lambda: all(received_tone(path, hz) for path, hz in zip(output, FREQUENCIES)))
            if any(child.poll() is not None for child in publishers + players):
                raise AssertionError("A media client disconnected before the stop test")
            return publishers + players

        try:
            server = start_server("server")
            media = start_media("before")
            if case == "shutdown":
                # The publishers and players are still connected when SIGTERM is sent.
                server.send_signal(signal.SIGTERM)
                code = server.wait(timeout=12)
                if code != 0:
                    raise AssertionError(f"SRS stopped with active streams and exit code {code}, expected 0")
                for child in reversed(media):
                    stop(child)
                start_server("restarted")
                start_media("after")
            elif case == "disconnect":
                verify_flv_disconnect(management, flv, names[0], media[0])
        finally:
            for child in reversed(children):
                stop(child)
    print(f"PASS {case}", flush=True)


def run_redaction():
    wrapper = Path(__file__).resolve().parents[1] / "deploy/srs-log-redact.sh"
    ticket = "%65yJhbGciOiJIUzI1NiJ9%2Ee30%2Eprobe"
    hook_key = "qa-callback-secret"
    authorization = "cHJvYmU6cHJpdmF0ZQ=="
    jwt = "eyJhbGciOiJIUzI1NiJ9.e30.probe"
    output = subprocess.run(
        ["bash", str(wrapper), "cat"],
        input=(f"on_publish HTTP 401 http://chat-api/on_publish?key={hook_key} "
               f'{{"param":"?srs_ticket={ticket}"}}\n'
               f"Authorization: Basic {authorization}\n"
               f"unexpected token {jwt}\n"),
        capture_output=True, text=True, check=True, timeout=5,
    ).stdout
    if any(value in output for value in (ticket, hook_key, authorization, jwt)):
        raise AssertionError("SRS output contained a raw credential")
    if not all(value in output for value in ("on_publish HTTP 401", "?[redacted]", '"param":"[redacted]"',
                                             "Authorization: Basic [redacted]", "[redacted-jwt]")):
        raise AssertionError("SRS diagnostic output was lost or did not contain the expected redactions")
    print("PASS redaction", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--srs", type=Path, default=Path(__file__).resolve().parents[1] / "trunk/objs/srs")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--case", choices=("all", *CASES), default="all")
    args = parser.parse_args()
    if args.case == "redaction":
        run_redaction()
        return
    ffmpeg = shutil.which(args.ffmpeg)
    if ffmpeg is None:
        parser.error(f"FFmpeg executable not found: {args.ffmpeg}")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    cases = CASES if args.case == "all" else (args.case,)
    for case in cases:
        if case == "redaction":
            run_redaction()
        else:
            run_case(args.srs.resolve(strict=True), str(Path(ffmpeg).resolve()), case)


if __name__ == "__main__":
    main()
