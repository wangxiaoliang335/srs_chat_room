#!/usr/bin/env python3
"""Update the existing development media stack from a reviewed, immutable plan."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


BASE = Path("/opt/coupon-srs-dev")
NAMES = {"srs": "coupon-srs-dev", "chat-api": "coupon-srs-dev-chat-api-1",
         "chat-media": "coupon-srs-dev-chat-media-1"}


def candidate_compose(current, images, proxy_config):
    if set(current["services"]) != set(NAMES) or set(images) != {"srs", "chat-api"}:
        raise ValueError("Unexpected deployment service scope")
    result = copy.deepcopy(current)
    for service, image in images.items():
        result["services"][service]["image"] = image
    mounts = [v for v in result["services"]["chat-media"]["volumes"]
              if isinstance(v, dict) and v.get("target") == "/etc/nginx/nginx.conf"]
    if len(mounts) != 1 or mounts[0].get("type") != "bind" or not mounts[0].get("read_only"):
        raise ValueError("Unexpected media configuration mount")
    mounts[0]["source"] = str(proxy_config)
    return result


def wait_for_idle(probe, timeout=300, sleep=time.sleep):
    deadline, consecutive = time.monotonic() + timeout, 0
    while True:
        # 查询失败必须中止，不能把鉴权失败或超时解释为没有推流。
        consecutive = consecutive + 1 if probe() else 0
        if consecutive == 3:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("Media did not become idle; no restart permitted")
        sleep(2)


def require_safe_restart(running, probe, timeout=300):
    # 回滚也必须重新观察活跃连接；只有进程已经停止时，才能跳过不可用的管理接口。
    if running():
        wait_for_idle(probe, timeout=timeout)


def atomic_write(path, text):
    fd, name = tempfile.mkstemp(prefix=".media-rollout-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def activate(compose, backup, contents, apply, check, restore=None):
    original = compose.read_text(encoding="utf-8")
    with backup.open("x", encoding="utf-8") as output:
        output.write(original)
    os.chmod(backup, 0o600)
    atomic_write(compose, contents)
    try:
        apply()
        check()
    except BaseException:
        atomic_write(compose, original)
        (restore or apply)()
        check()
        print("DEV_MEDIA_ROLLBACK_HEALTHY", flush=True)
        raise


def docker(*args, stdin=None):
    result = subprocess.run(["docker", *args], input=stdin, capture_output=True, text=True, timeout=90)
    if result.returncode:
        # Compose 渲染可能包含凭证，不把命令的原始 stdout/stderr 写入部署日志。
        raise RuntimeError(f"Docker {args[0]} failed with exit code {result.returncode}")
    return result.stdout


def inspect(name, kind="container"):
    return json.loads(docker(kind, "inspect", name))[0]


def media_state():
    output = docker("exec", "-i", NAMES["chat-api"], "python", "-", stdin='''
import base64,json,os
from urllib.request import Request,urlopen
base=os.environ['SRS_HTTP_API'].rstrip('/')+'/api/v1/'
auth=base64.b64encode((os.environ['SRS_HTTP_API_USERNAME']+':'+os.environ['SRS_HTTP_API_PASSWORD']).encode()).decode()
def get(path):
    with urlopen(Request(base+path,headers={'Authorization':'Basic '+auth}),timeout=3) as response:
        data=json.load(response)
        assert data['code']==0
        return data
clients=get('clients/?count=10000')['clients']
streams=get('streams/')['streams']
assert isinstance(clients,list) and isinstance(streams,list)
print(json.dumps({'clients':len(clients),'publishers':sum(bool(s.get('publish',{}).get('active')) for s in streams)}))
''')
    return json.loads(output)


def idle():
    state = media_state()
    return state["clients"] == 0 and state["publishers"] == 0


def main():
    import fcntl
    import yaml

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    plan_path = args.plan.resolve(strict=True)
    release = plan_path.parent
    if release.parent != BASE / "releases" or not re.fullmatch(r"[a-zA-Z0-9_-]+", release.name):
        raise ValueError("Release plan must be under the development releases directory")
    plan = json.loads(plan_path.read_text())
    revision = plan["revision"]
    if (not re.fullmatch(r"[a-f0-9]{40}", revision) or set(plan["expected_images"]) != set(NAMES)
            or set(plan["images"]) != {"srs", "chat-api"}
            or any(not re.fullmatch(r"sha256:[a-f0-9]{64}", ref)
                   for ref in [*plan["expected_images"].values(), *plan["images"].values()])):
        raise ValueError("Invalid revision or expected service set")
    proxy = Path(plan["proxy_config"]).resolve(strict=True)
    if proxy.parent != release or hashlib.sha256(proxy.read_bytes()).hexdigest() != plan["proxy_sha256"]:
        raise ValueError("Proxy configuration does not match the release")
    compose = BASE / "compose.yaml"
    with (BASE / ".codex-image-rollout.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original = yaml.safe_load(compose.read_text())
        for service, name in NAMES.items():
            live = inspect(name)
            if live["Image"] != plan["expected_images"][service] or not live["State"]["Running"]:
                raise ValueError(f"Current {service} differs from the reviewed release")
            if original["services"][service]["image"] != live["Config"]["Image"]:
                raise ValueError(f"Compose/runtime image drift: {service}")
            if inspect(live["Config"]["Image"], "image")["Id"] != live["Image"]:
                raise ValueError(f"Rollback image tag drift: {service}")
        for service, ref in plan["images"].items():
            candidate = inspect(ref, "image")
            if candidate["Config"].get("Labels", {}).get("org.opencontainers.image.revision") != revision:
                raise ValueError(f"Candidate {service} has a different source revision")
        next_compose = release / "compose-candidate.yaml"
        next_compose.write_text(json.dumps(candidate_compose(original, plan["images"], proxy), indent=2) + "\n")
        os.chmod(next_compose, 0o600)
        command = ("compose", "-p", "coupon-srs-dev", "-f", str(compose))
        docker("compose", "-p", "coupon-srs-dev", "-f", str(next_compose), "config", "--quiet")
        tls_mount = next(v for v in original["services"]["chat-media"]["volumes"] if v["target"] == "/etc/chat-tls")
        docker("run", "--rm", "--network", "none", "--entrypoint", "nginx",
               "--mount", f"type=bind,src={proxy},dst=/etc/nginx/nginx.conf,readonly",
               "--mount", f"type=bind,src={tls_mount['source']},dst=/etc/chat-tls,readonly",
               original["services"]["chat-media"]["image"], "-t")
        before = media_state()
        print(json.dumps({"phase": "preflight", "revision": revision, "media": before, "apply": args.apply}), flush=True)
        if not args.apply:
            return
        wait_for_idle(idle)
        print("DEV_MEDIA_IDLE_CONFIRMED", flush=True)

        def apply():
            docker(*command, "up", "-d", "--no-deps", "--pull", "never", "--wait", "--wait-timeout", "60", "chat-api")
            # API 切换期间若出现新连接，继续等待，不能带流重建 SRS。
            require_safe_restart(lambda: inspect(NAMES["srs"])["State"]["Running"], idle)
            docker(*command, "up", "-d", "--no-deps", "--pull", "never", "srs", "chat-media")

        def restore():
            for service in NAMES:
                if inspect(original["services"][service]["image"], "image")["Id"] != plan["expected_images"][service]:
                    raise ValueError("Rollback image changed; refusing an unverified recovery image")
            apply()

        def check():
            desired = yaml.safe_load(compose.read_text())
            deadline = time.monotonic() + 45
            while True:
                try:
                    for service, name in NAMES.items():
                        live = inspect(name)
                        if live["Image"] != inspect(desired["services"][service]["image"], "image")["Id"]:
                            raise ValueError("Unexpected deployed image")
                        if not live["State"]["Running"] or live["RestartCount"]:
                            raise ValueError("Service is not stable")
                    if inspect(NAMES["chat-api"])["State"].get("Health", {}).get("Status") != "healthy":
                        raise ValueError("Chat API is not healthy")
                    media_state()
                    docker("exec", NAMES["chat-media"], "nginx", "-t")
                    return
                except (RuntimeError, ValueError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(1)

        activate(compose, release / "compose-before.yaml", next_compose.read_text(), apply, check,
                 restore=restore)
        receipt = {"revision": revision, "images": {s: inspect(n)["Image"] for s, n in NAMES.items()},
                   "proxy_sha256": plan["proxy_sha256"], "verified_at": time.time()}
        (release / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps({"phase": "healthy", **receipt}), flush=True)


if __name__ == "__main__":
    main()
