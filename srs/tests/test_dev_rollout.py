import importlib.util
import json
from pathlib import Path

import pytest


def rollout():
    path = Path(__file__).resolve().parents[1] / "deploy/rollout-dev-media.py"
    assert path.exists(), "缺少受版本管理的开发服媒体部署入口"
    spec = importlib.util.spec_from_file_location("dev_media_rollout", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_candidate_preserves_auth_ports_and_all_other_service_settings():
    module = rollout()
    original = {"services": {
        "srs": {"image": "srs-old", "ports": [{"target": 8000, "protocol": "udp"}]},
        "chat-api": {"image": "chat-old", "env_file": ["private.env"], "volumes": ["data:/data"]},
        "chat-media": {"image": "nginx", "volumes": [
            {"type": "bind", "source": "/old.conf", "target": "/etc/nginx/nginx.conf", "read_only": True},
            {"type": "bind", "source": "/tls", "target": "/etc/chat-tls", "read_only": True},
        ]},
    }, "volumes": {"data": {}}, "networks": {"default": {}}}
    result = module.candidate_compose(original, {"srs": "srs-new", "chat-api": "chat-new"}, "/new.conf")
    assert result["services"]["srs"]["image"] == "srs-new"
    assert result["services"]["chat-api"]["image"] == "chat-new"
    assert result["services"]["chat-media"]["volumes"][0]["source"] == "/new.conf"
    result["services"]["srs"]["image"] = "srs-old"
    result["services"]["chat-api"]["image"] = "chat-old"
    result["services"]["chat-media"]["volumes"][0]["source"] = "/old.conf"
    assert result == original
    assert original["services"]["srs"]["image"] == "srs-old"


def test_active_clients_prevent_rollout_without_being_disconnected():
    module = rollout()
    with pytest.raises(TimeoutError, match="idle"):
        module.wait_for_idle(lambda: False, timeout=0)


def test_failed_management_probe_cannot_be_treated_as_idle():
    module = rollout()
    def rejected():
        raise ValueError("management HTTP 401")
    with pytest.raises(ValueError, match="401"):
        module.wait_for_idle(rejected, timeout=30)


def test_idle_must_be_consecutive_after_any_activity():
    module = rollout()
    observations = iter([True, True, False, True, True, True])
    seen = []
    def probe():
        seen.append(next(observations))
        return seen[-1]
    module.wait_for_idle(probe, timeout=10, sleep=lambda _: None)
    assert seen == [True, True, False, True, True, True]


def test_failed_rollout_restores_and_verifies_original_compose(tmp_path):
    module = rollout()
    compose, backup = tmp_path / "compose.yaml", tmp_path / "before.yaml"
    compose.write_text("old", encoding="utf-8")
    applied, checked = [], []
    def apply():
        applied.append(compose.read_text(encoding="utf-8"))
    def check():
        checked.append(compose.read_text(encoding="utf-8"))
        if checked[-1] == "new":
            raise RuntimeError("unhealthy candidate")
    with pytest.raises(RuntimeError, match="unhealthy candidate"):
        module.activate(compose, backup, "new", apply, check)
    assert applied == ["new", "old"]
    assert checked == ["new", "old"]
    assert compose.read_text(encoding="utf-8") == backup.read_text(encoding="utf-8") == "old"


def test_successful_rollout_keeps_recovery_snapshot(tmp_path):
    module = rollout()
    compose, backup = tmp_path / "compose.yaml", tmp_path / "before.yaml"
    compose.write_text("old", encoding="utf-8")
    module.activate(compose, backup, "new", lambda: None, lambda: None)
    assert compose.read_text(encoding="utf-8") == "new"
    assert backup.read_text(encoding="utf-8") == "old"


def test_restore_uses_separate_recovery_callback(tmp_path):
    module = rollout()
    compose, backup = tmp_path / "compose.yaml", tmp_path / "before.yaml"
    compose.write_text("old", encoding="utf-8")
    recovered = []
    def failed_apply():
        raise RuntimeError("candidate management unavailable")
    with pytest.raises(RuntimeError, match="candidate management unavailable"):
        module.activate(compose, backup, "new", failed_apply, lambda: None,
                        restore=lambda: recovered.append(compose.read_text(encoding="utf-8")))
    assert recovered == ["old"]


def test_recovery_of_stopped_process_does_not_require_its_management_endpoint():
    module = rollout()
    def unavailable():
        raise AssertionError("Stopped process cannot answer management requests")
    module.require_safe_restart(lambda: False, unavailable, timeout=0)


@pytest.mark.parametrize("failure", ["busy", "unavailable"])
def test_recovery_refuses_to_restart_running_srs_without_fresh_idle_confirmation(failure):
    module = rollout()
    def probe():
        if failure == "unavailable":
            raise ValueError("management unavailable")
        return False
    with pytest.raises((TimeoutError, ValueError)):
        module.require_safe_restart(lambda: True, probe, timeout=0)
