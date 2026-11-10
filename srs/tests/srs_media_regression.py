#!/usr/bin/env python3
"""Exercise the SRS media patches in private, loopback-only server processes."""

import argparse
import array
from contextlib import ExitStack
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
from urllib.request import ProxyHandler, build_opener
import wave


RATE = 48000
FREQUENCIES = (431, 787)
HTTP = build_opener(ProxyHandler({}))


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


def api(port, endpoint):
    with HTTP.open(f"http://127.0.0.1:{port}/api/v1/{endpoint}", timeout=1) as response:
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
http_api {{ enabled on; listen 127.0.0.1:{management}; }}
http_server {{ enabled on; listen 127.0.0.1:{flv}; }}
vhost __defaultVhost__ {{
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
        finally:
            for child in reversed(children):
                stop(child)
    print(f"PASS {case}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--srs", type=Path, default=Path(__file__).resolve().parents[1] / "trunk/objs/srs")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--case", choices=("all", "dotted", "shutdown"), default="all")
    args = parser.parse_args()
    ffmpeg = shutil.which(args.ffmpeg)
    if ffmpeg is None:
        parser.error(f"FFmpeg executable not found: {args.ffmpeg}")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    cases = ("dotted", "shutdown") if args.case == "all" else (args.case,)
    for case in cases:
        run_case(args.srs.resolve(strict=True), str(Path(ffmpeg).resolve()), case)


if __name__ == "__main__":
    main()