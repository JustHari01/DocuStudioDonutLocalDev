import asyncio
import hashlib
import json
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from docurandom import flow_agent
from docurandom.flow_agent import (
    FlowAgentCaptchaError,
    FlowAgentManager,
    FlowAgentQuotaError,
    FlowAgentStatus,
    FlowAgentUiError,
    FlowGenerationCoordinator,
)


def test_bundled_extension_connects_to_local_backend():
    project_root = Path(__file__).resolve().parents[1]
    config = (project_root / "flow-extension" / "config.example.js").read_text(
        encoding="utf-8"
    )
    manifest = (project_root / "flow-extension" / "manifest.json").read_text(
        encoding="utf-8"
    )
    assert 'DEFAULT_SERVER_HOST: "127.0.0.1:8001"' in config
    assert "flow.kodelyx.in" not in config
    assert 'FLOW_API_KEY: ""' in config
    assert "flow.kodelyx.in" not in manifest


def test_inspect_project_snapshot_can_restrict_read_to_active_tab(monkeypatch):
    manager = FlowAgentManager("http://127.0.0.1:8001")
    calls = []
    monkeypatch.setattr(
        flow_agent,
        "_json_request",
        lambda url, **kwargs: calls.append((url, kwargs))
        or {"projectId": "active-project"},
    )

    result = manager.inspect_project_snapshot(active_tab_only=True, timeout=17.0)

    assert result["projectId"] == "active-project"
    assert calls[0][1]["payload"]["activeTabOnly"] is True
    assert calls[0][1]["payload"]["workspaceId"] == ""
    assert calls[0][1]["payload"]["flowProjectId"] == ""
    assert calls[0][1]["timeout"] == 17.0


def test_project_snapshot_api_route_forwards_active_tab_only(monkeypatch):
    from flow_server.routes import system

    calls = []

    class Bridge:
        async def ui_project_snapshot(self, **kwargs):
            calls.append(kwargs)
            return {"status": 200, "projectId": "active-project"}

    async def get_bridge():
        return Bridge()

    monkeypatch.setattr(system, "get_active_bridge", get_bridge)
    result = asyncio.run(
        system.inspect_ui_project_snapshot(payload={"activeTabOnly": True})
    )

    assert result["projectId"] == "active-project"
    assert calls[0]["active_tab_only"] is True


def test_extension_bridge_relays_active_tab_only_to_service_worker():
    from flow_engine.bridge import ExtensionBridge

    async def exercise():
        bridge = ExtensionBridge()
        bridge._loop = asyncio.get_running_loop()
        bridge.active_client_ids = lambda: ["edge-client"]
        bridge._capabilities["edge-client"] = {"flow_project_history"}
        sent = []

        async def send_message_to(client_id, message):
            sent.append((client_id, message))
            bridge._pending[message["id"]].set_result(
                {"status": 200, "projectId": "active-project"}
            )
            return True

        bridge.send_message_to = send_message_to
        result = await bridge.ui_project_snapshot(
            client_id="edge-client",
            active_tab_only=True,
        )

        assert result["projectId"] == "active-project"
        assert sent[0][0] == "edge-client"
        assert sent[0][1]["method"] == "ui_project_snapshot"
        assert sent[0][1]["params"]["activeTabOnly"] is True

    asyncio.run(exercise())


def test_bind_flow_project_checkpoint_preserves_state_and_backs_up(tmp_path):
    checkpoint = tmp_path / "flow_history.json"
    original = {
        "version": 2,
        "signature": "signature",
        "run_id": "run-1",
        "prompts": ["keep this prompt"],
        "entries": {"1": {"filename": "Image_0001.png"}},
        "failures": {"2": {"submitted": False, "reason": "retry later"}},
    }
    checkpoint.write_text(json.dumps(original), encoding="utf-8")

    backup = flow_agent.bind_flow_project_checkpoint(
        checkpoint,
        "DaiTa3",
        "project-identifier-123",
        "edge-client-1",
    )

    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    assert {key: saved[key] for key in original} == original
    assert saved["workspace_id"] == "DaiTa3"
    assert saved["flow_project_id"] == "project-identifier-123"
    assert saved["flow_client_id"] == "edge-client-1"


@pytest.mark.parametrize(
    ("saved_workspace", "saved_project", "message"),
    [
        ("DaiTa7", "", "workspace khác"),
        ("DaiTa3", "existing-project-123", "project Flow khác"),
    ],
)
def test_bind_flow_project_checkpoint_refuses_conflicting_identity(
    tmp_path, saved_workspace, saved_project, message
):
    checkpoint = tmp_path / "flow_history.json"
    original = {
        "version": 2,
        "workspace_id": saved_workspace,
        "flow_project_id": saved_project,
        "entries": {},
        "failures": {},
    }
    checkpoint.write_text(json.dumps(original), encoding="utf-8")

    with pytest.raises(flow_agent.FlowAgentError, match=message):
        flow_agent.bind_flow_project_checkpoint(
            checkpoint, "DaiTa3", "new-project-123"
        )

    assert json.loads(checkpoint.read_text(encoding="utf-8")) == original
    assert list(tmp_path.iterdir()) == [checkpoint]


def test_discover_extension_dir_accepts_bundled_manifest(tmp_path, monkeypatch):
    extension = tmp_path / "flow-extension"
    extension.mkdir()
    (extension / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "Flow Agent"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCURANDOM_FLOW_EXTENSION", str(extension))
    assert flow_agent.discover_extension_dir() == extension.resolve()


def test_detect_edge_extension_accepts_unpacked_path_without_embedded_manifest(
    tmp_path, monkeypatch
):
    local = tmp_path / "Local"
    profile = local / "Microsoft" / "Edge" / "User Data" / "Default"
    extension = tmp_path / "installed-app" / "flow-extension"
    profile.mkdir(parents=True)
    extension.mkdir(parents=True)
    (extension / "manifest.json").write_text(
        json.dumps(
            {"manifest_version": 3, "name": "Flow Agent", "version": "1.3.8"}
        ),
        encoding="utf-8",
    )
    (profile / "Preferences").write_text(
        json.dumps(
            {
                "extensions": {
                    "settings": {
                        "unpacked-id": {
                            "path": str(extension),
                            "disable_reasons": [],
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCALAPPDATA", str(local))

    assert flow_agent.detect_edge_extension_installed() is True


def test_flow_extension_reload_targets_only_installed_flow_agent(tmp_path, monkeypatch):
    local = tmp_path / "Local"
    profile = local / "Microsoft" / "Edge" / "User Data" / "Default"
    extension = tmp_path / "installed-app" / "flow-extension"
    extension_id = "abcdefghijklmnopabcdefghijklmnop"
    profile.mkdir(parents=True)
    extension.mkdir(parents=True)
    (extension / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "Flow Agent"}),
        encoding="utf-8",
    )
    (profile / "Secure Preferences").write_text(
        json.dumps({
            "extensions": {
                "settings": {
                    extension_id: {"path": str(extension), "disable_reasons": []},
                    "ponmlkjihgfedcbaponmlkjihgfedcba": {
                        "manifest": {"name": "Unrelated Extension"},
                        "disable_reasons": [],
                    },
                }
            }
        }),
        encoding="utf-8",
    )
    commands = []
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(flow_agent, "discover_edge", lambda: Path("edge.exe"))
    monkeypatch.setattr(
        flow_agent.subprocess,
        "Popen",
        lambda command, **_kwargs: commands.append(command),
    )

    manager = FlowAgentManager()
    monkeypatch.setattr(manager, "_backend_endpoint_open", lambda: False)
    assert manager.reload_installed_extension() is True
    assert manager.reload_installed_extension() is False
    assert commands == [[
        str(Path("edge.exe")),
        (
            f"chrome-extension://{extension_id}/popup.html"
            f"?docurandom_reload={flow_agent.FLOW_EXTENSION_VERSION}"
        ),
    ]]


def test_flow_extension_update_reloads_only_the_current_bundled_path(
    tmp_path, monkeypatch
):
    local = tmp_path / "Local"
    profile = local / "Microsoft" / "Edge" / "User Data" / "Default"
    extension = tmp_path / "installed-app" / "flow-extension"
    extension_id = "abcdefghijklmnopabcdefghijklmnop"
    profile.mkdir(parents=True)
    extension.mkdir(parents=True)
    (extension / "manifest.json").write_text(
        json.dumps({
            "manifest_version": 3,
            "name": "Flow Agent",
            "version": flow_agent.FLOW_EXTENSION_VERSION,
        }),
        encoding="utf-8",
    )
    (profile / "Preferences").write_text(
        json.dumps({
            "extensions": {
                "settings": {
                    extension_id: {
                        "path": str(extension),
                        "disable_reasons": [],
                    },
                },
            },
        }),
        encoding="utf-8",
    )
    commands = []
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("DOCURANDOM_FLOW_EXTENSION", str(extension))
    monkeypatch.setattr(flow_agent, "discover_edge", lambda: Path("edge.exe"))
    monkeypatch.setattr(
        flow_agent.subprocess,
        "Popen",
        lambda command, **_kwargs: commands.append(command),
    )

    manager = FlowAgentManager()
    monkeypatch.setattr(manager, "_backend_endpoint_open", lambda: False)
    assert manager.update_installed_extension() is True
    assert commands and commands[0][1].startswith(
        f"chrome-extension://{extension_id}/popup.html"
    )


def test_flow_extension_update_does_not_adopt_a_different_unpacked_path(
    tmp_path, monkeypatch
):
    local = tmp_path / "Local"
    profile = local / "Microsoft" / "Edge" / "User Data" / "Default"
    bundled = tmp_path / "bundled" / "flow-extension"
    loaded = tmp_path / "other" / "flow-extension"
    extension_id = "abcdefghijklmnopabcdefghijklmnop"
    profile.mkdir(parents=True)
    bundled.mkdir(parents=True)
    loaded.mkdir(parents=True)
    for path in (bundled, loaded):
        (path / "manifest.json").write_text(
            json.dumps({
                "manifest_version": 3,
                "name": "Flow Agent",
                "version": flow_agent.FLOW_EXTENSION_VERSION,
            }),
            encoding="utf-8",
        )
    (profile / "Preferences").write_text(
        json.dumps({
            "extensions": {
                "settings": {
                    extension_id: {
                        "path": str(loaded),
                        "disable_reasons": [],
                    },
                },
            },
        }),
        encoding="utf-8",
    )
    commands = []
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("DOCURANDOM_FLOW_EXTENSION", str(bundled))
    monkeypatch.setattr(flow_agent, "discover_edge", lambda: Path("edge.exe"))
    monkeypatch.setattr(
        flow_agent.subprocess,
        "Popen",
        lambda command, **_kwargs: commands.append(command),
    )

    assert FlowAgentManager().update_installed_extension() is False
    assert commands == []


def test_flow_extension_restart_recovers_worker_without_reloading_flow_page(monkeypatch):
    manager = FlowAgentManager()
    ready = FlowAgentStatus(
        edge_path=Path("edge.exe"),
        extension_dir=Path("extension"),
        extension_installed=True,
        backend_running=True,
        extension_connected=True,
        has_flow_key=False,
        extension_version=flow_agent.FLOW_EXTENSION_VERSION,
        extension_compatible=True,
        browser_open=True,
        flow_tab_count=1,
        ui_image_capable=True,
    )
    requests = []
    monkeypatch.setattr(manager, "start_backend", lambda: None)
    monkeypatch.setattr(manager, "reload_installed_extension", lambda **_kwargs: True)
    monkeypatch.setattr(manager, "check", lambda **_kwargs: ready)
    monkeypatch.setattr(flow_agent.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        flow_agent,
        "_json_request",
        lambda url, **kwargs: requests.append((url, kwargs)) or {"nudged": 1},
    )

    result = manager.restart_extension_session(timeout=15)

    assert result.ready is True
    assert requests[0][0] == (
        f"{manager.api_base}/v1/refresh-tokens?force=true&reload_page=false"
    )
    assert requests[0][1]["method"] == "POST"
    assert 8.0 <= requests[0][1]["timeout"] <= 30.0


def test_start_backend_waits_for_existing_listener_instead_of_spawning_duplicate(
    monkeypatch,
):
    manager = FlowAgentManager()
    not_ready = FlowAgentStatus(
        Path("edge.exe"), Path("extension"), True, False, False, False
    )
    ready = FlowAgentStatus(
        Path("edge.exe"), Path("extension"), True, True, True, True
    )
    statuses = iter((not_ready, ready))
    monkeypatch.setattr(manager, "check", lambda **_kwargs: next(statuses, ready))
    monkeypatch.setattr(manager, "_backend_endpoint_open", lambda: True)
    monkeypatch.setattr(flow_agent.time, "sleep", lambda _seconds: None)

    def unexpected_spawn(*_args, **_kwargs):
        raise AssertionError("would spawn a duplicate backend")

    monkeypatch.setattr(flow_agent.subprocess, "Popen", unexpected_spawn)
    manager.start_backend()


def test_start_backend_reuses_live_process_when_health_probe_is_slow(
    monkeypatch,
):
    manager = FlowAgentManager()

    class LiveProcess:
        def poll(self):
            return None

    manager.process = LiveProcess()
    not_ready = FlowAgentStatus(
        Path("edge.exe"), Path("extension"), True, False, False, False
    )
    monkeypatch.setattr(manager, "check", lambda **_kwargs: not_ready)
    monkeypatch.setattr(manager, "_backend_endpoint_open", lambda: True)

    def unexpected_spawn(*_args, **_kwargs):
        raise AssertionError("would spawn a duplicate backend")

    monkeypatch.setattr(flow_agent.subprocess, "Popen", unexpected_spawn)
    manager.start_backend()


def test_forced_flow_refresh_targets_only_one_existing_ui_client(monkeypatch):
    from flow_server.routes import system

    class Bridge:
        _flow_tab_counts = {"empty": 0, "with-tab": 1, "other-tab": 1}
        _capabilities = {
            "empty": {"ui_text_to_image"},
            "with-tab": {"ui_text_to_image"},
            "other-tab": set(),
        }
        _tokens = {"empty": "old", "with-tab": "old", "other-tab": "old"}

        def active_client_ids(self):
            return ["empty", "other-tab", "with-tab"]

        async def _recover_ui_client(self, client_id):
            recovered.append(client_id)

    recovered = []

    async def active_bridge():
        return Bridge()

    monkeypatch.setattr(system, "get_active_bridge", active_bridge)

    result = asyncio.run(system.refresh_tokens(force=True, x_client_id=None))

    assert result["nudged"] == 1
    assert recovered == ["with-tab"]
    assert result["clients"][0]["page_reloaded"] is False


def test_flow_health_exposes_ui_capability_and_real_tab_state(monkeypatch):
    from flow_server.routes import system

    class Bridge:
        _browser_open = {"edge-client": True}
        _flow_tab_counts = {"edge-client": 1}
        _capabilities = {"edge-client": {"ui_text_to_image"}}
        _extension_versions = {"edge-client": flow_agent.FLOW_EXTENSION_VERSION}
        _tokens = {"edge-client": "token"}
        _states = {"edge-client": "idle"}
        _ui_quota_blocked_until = {"edge-client": 0.0}

        def active_client_ids(self):
            return ["edge-client"]

        async def health_check(self):
            return True

    monkeypatch.setattr(system.state, "get_bridge", lambda: Bridge())

    result = asyncio.run(system.health())

    assert result["extension_connected"] is True
    assert result["browser_open"] is True
    assert result["flow_tab_count"] == 1
    assert result["capabilities"] == ["ui_text_to_image"]
    assert result["clients"][0]["flow_tab_count"] == 1
    assert result["clients"][0]["capabilities"] == ["ui_text_to_image"]


def test_visible_edge_window_probe_does_not_block_health_callers(monkeypatch):
    from flow_engine import bridge as bridge_module

    probe_started = threading.Event()
    release_probe = threading.Event()
    probe_finished = threading.Event()

    def slow_probe():
        probe_started.set()
        release_probe.wait(2.0)
        probe_finished.set()
        return False

    monkeypatch.setattr(bridge_module, "_VISIBLE_EDGE_WINDOW_CACHE", None)
    monkeypatch.setattr(bridge_module, "_VISIBLE_EDGE_WINDOW_PROBE_INFLIGHT", False)
    monkeypatch.setattr(bridge_module, "_has_visible_edge_window_uncached", slow_probe)

    started_at = time.monotonic()
    # The first call must return the conservative cached fallback immediately;
    # the slow ctypes probe is allowed to finish outside the request thread.
    assert bridge_module._has_visible_edge_window() is True
    assert time.monotonic() - started_at < 0.5
    assert probe_started.wait(1.0)

    release_probe.set()
    assert probe_finished.wait(2.0)
    deadline = time.monotonic() + 1.0
    while bridge_module._VISIBLE_EDGE_WINDOW_PROBE_INFLIGHT and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not bridge_module._VISIBLE_EDGE_WINDOW_PROBE_INFLIGHT
    assert bridge_module._VISIBLE_EDGE_WINDOW_CACHE is not None
    assert bridge_module._VISIBLE_EDGE_WINDOW_CACHE[1] is False


@pytest.mark.parametrize(
    "ui_capable,tabs,token,window_open,stale,expected",
    [
        (True, 1, False, True, False, True),
        (True, 1, True, True, False, True),
        (True, 0, True, True, False, False),
        (True, 0, False, True, False, False),
        (True, 1, False, False, False, False),
        (True, 1, False, True, True, False),
        (False, 0, True, True, False, True),
        (False, 1, False, True, False, False),
    ],
)
def test_backend_session_gate_matches_ui_or_legacy_transport(
    monkeypatch, ui_capable, tabs, token, window_open, stale, expected
):
    from flow_engine import bridge as bridge_module

    monkeypatch.setattr(bridge_module, "_has_visible_edge_window", lambda: True)
    bridge = bridge_module.ExtensionBridge()
    bridge._clients["client"] = object()
    bridge._browser_open["client"] = window_open
    bridge._flow_tab_counts["client"] = tabs
    bridge._capabilities["client"] = {"ui_text_to_image"} if ui_capable else set()
    bridge._last_seen["client"] = bridge_module._time.monotonic() - (
        bridge.CLIENT_HEARTBEAT_TIMEOUT + 1 if stale else 0
    )
    if token:
        bridge._tokens["client"] = "cached-token"

    assert asyncio.run(bridge.health_check()) is expected


def test_tokenless_ui_session_passes_desktop_backend_and_image_generation(monkeypatch):
    from flow_engine import bridge as bridge_module
    from flow_engine.generators.t2i import generate_image
    from flow_server import state
    from flow_server.routes import system

    monkeypatch.setattr(bridge_module, "_has_visible_edge_window", lambda: True)
    monkeypatch.setattr(flow_agent, "discover_edge", lambda: Path("edge.exe"))
    monkeypatch.setattr(flow_agent, "discover_extension_dir", lambda: Path("extension"))
    monkeypatch.setattr(flow_agent, "detect_edge_extension_installed", lambda: True)

    async def exercise():
        bridge = bridge_module.ExtensionBridge()
        bridge._loop = asyncio.get_running_loop()
        bridge._clients["client"] = object()
        bridge._browser_open["client"] = True
        bridge._flow_tab_counts["client"] = 1
        bridge._capabilities["client"] = {"ui_text_to_image"}
        bridge._extension_versions["client"] = flow_agent.FLOW_EXTENSION_VERSION
        bridge._last_seen["client"] = bridge_module._time.monotonic()
        monkeypatch.setattr(state, "bridge", bridge)

        async def unexpected(*_args, **_kwargs):
            raise AssertionError("Ready UI must not wait for a token or refresh/open any tab")

        monkeypatch.setattr(bridge_module.asyncio, "sleep", unexpected)
        monkeypatch.setattr(bridge, "_request_flow_tab_for", unexpected)
        monkeypatch.setattr(bridge, "api_request", unexpected)

        report = await system.health()
        assert report["status"] == "healthy"
        assert report["has_flow_key"] is False
        monkeypatch.setattr(flow_agent, "_json_request", lambda *_a, **_kw: report)
        assert FlowAgentManager().check(include_credits=False).ready is True
        assert await bridge.wait_for_extension(timeout=0, auto_fix=False) is True
        assert await state.get_active_bridge() is bridge

        sent = []

        async def send(client_id, message):
            assert client_id == "client"
            assert message["method"] == "ui_generate_images"
            sent.append(message["params"]["prompt"])
            bridge._route_response(message["id"], {
                "id": message["id"],
                "status": 200,
                "result": {"images": [{"imageUrl": "https://example/image.png"}]},
            })
            return True

        monkeypatch.setattr(bridge, "send_message_to", send)
        images = await generate_image(bridge, "test prompt", "landscape", "project")
        assert len(images) == 1
        assert sent == ["test prompt"]
        assert bridge._states["client"] == "idle"

    asyncio.run(exercise())


def test_backend_reconnect_gate_does_not_request_new_tabs(monkeypatch):
    from fastapi import HTTPException
    from flow_server import state

    calls = []

    class DisconnectedBridge:
        async def health_check(self):
            return False

        async def wait_for_extension(self, **kwargs):
            calls.append(kwargs)
            return False

    monkeypatch.setattr(state, "bridge", DisconnectedBridge())
    with pytest.raises(HTTPException) as error:
        asyncio.run(state.get_active_bridge())

    assert error.value.status_code == 503
    assert "extension is not connected" in error.value.detail
    assert calls == [{"timeout": 10, "max_retries": 1, "auto_fix": False}]


def test_explicit_token_refresh_can_still_reload_one_page_for_api_401(monkeypatch):
    from flow_server.routes import system

    class Bridge:
        _flow_tab_counts = {"with-tab": 1}
        _capabilities = {"with-tab": {"ui_text_to_image"}}
        _tokens = {"with-tab": "stale"}

        def active_client_ids(self):
            return ["with-tab"]

        async def _force_refresh_client(self, client_id):
            hard_refreshed.append(client_id)
            return True

    hard_refreshed = []

    async def active_bridge():
        return Bridge()

    monkeypatch.setattr(system, "get_active_bridge", active_bridge)
    result = asyncio.run(
        system.refresh_tokens(
            force=True,
            reload_page=True,
            x_client_id=None,
        )
    )

    assert result["nudged"] == 1
    assert hard_refreshed == ["with-tab"]
    assert result["clients"][0]["page_reloaded"] is True


def test_hard_token_refresh_is_deferred_while_flow_ui_job_is_running(monkeypatch):
    from flow_engine import bridge as bridge_module
    from flow_engine.bridge import ExtensionBridge

    monkeypatch.setattr(bridge_module, "_has_visible_edge_window", lambda: True)

    async def exercise() -> None:
        bridge = ExtensionBridge()
        bridge._loop = asyncio.get_running_loop()
        bridge._clients["edge-client"] = object()
        bridge._browser_open["edge-client"] = True
        bridge._last_seen["edge-client"] = bridge_module._time.monotonic()
        bridge._states["edge-client"] = "running"
        bridge._tokens["edge-client"] = "keep-me"
        sent = []

        async def send(_client_id, message):
            sent.append(message)

        bridge.send_message_to = send
        refreshed = await bridge._force_refresh_client("edge-client")

        assert refreshed is False
        assert sent == []
        assert bridge._tokens["edge-client"] == "keep-me"

    asyncio.run(exercise())


def test_generate_images_uses_youtube_ratio_and_workspace_media(tmp_path, monkeypatch):
    manager = FlowAgentManager()
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(
            edge_path=Path("edge.exe"),
            extension_dir=Path("extension"),
            extension_installed=True,
            backend_running=True,
            extension_connected=True,
            has_flow_key=True,
        ),
    )
    requests = []

    def fake_request(url, **kwargs):
        requests.append((url, kwargs))
        return {"data": [{"url": "http://example/image.png", "media_id": "m1"}]}

    def fake_download(url, destination, timeout=120.0):
        Image.new("RGB", (64, 36), "navy").save(destination)
        return destination

    monkeypatch.setattr(flow_agent, "_json_request", fake_request)
    monkeypatch.setattr(manager, "_download", fake_download)
    results = manager.generate_images(
        ["factory in Japan"],
        tmp_path / "Media",
        model="gem_pix_2",
    )

    assert results[0].path.parent.name == "Media"
    payload = requests[0][1]["payload"]
    assert payload["model"] == "harbor_seal"
    assert payload["n"] == 1
    assert payload["size"] == "1792x1024"

    # A retry/restart validates the existing image and does not spend another
    # Flow generation for an already completed prompt.
    repeated = manager.generate_images(["factory in Japan"], tmp_path / "Media")
    assert repeated[0].path == results[0].path
    assert len(requests) == 1


def test_flow_image_api_contract_rejects_extra_images_and_normalises_old_models():
    from pydantic import ValidationError

    from flow_server.config import _normalise_model
    from flow_server.models import ImageGenerationRequest

    request = ImageGenerationRequest(prompt="factory")
    assert request.model == "harbor_seal"
    assert request.n == 1
    assert _normalise_model("gem_pix_2") == "harbor_seal"
    with pytest.raises(ValidationError):
        ImageGenerationRequest(prompt="factory", n=2)


def test_generate_images_waits_three_to_five_seconds_between_prompts(tmp_path, monkeypatch):
    manager = FlowAgentManager()
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(Path("edge.exe"), Path("extension"), True, True, True, True),
    )
    monkeypatch.setattr(
        flow_agent,
        "_json_request",
        lambda *_args, **_kwargs: {"data": [{"url": "http://example/image.png"}]},
    )

    def fake_download(_url, destination, timeout=120.0):
        Image.new("RGB", (64, 36), "navy").save(destination)
        return destination

    waits = []
    monkeypatch.setattr(manager, "_download", fake_download)
    monkeypatch.setattr(flow_agent.random, "uniform", lambda low, high: 4.0)
    monkeypatch.setattr(
        flow_agent,
        "_wait_cancellable",
        lambda seconds, _cancel: waits.append(seconds),
    )
    manager.generate_images(["one", "two"], tmp_path / "Media")
    assert waits == [4.0]


def test_zero_credits_is_reported_as_daily_quota_not_captcha(tmp_path, monkeypatch):
    manager = FlowAgentManager()
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(
            Path("edge.exe"), Path("extension"), True, True, True, True, credits=0
        ),
    )
    with pytest.raises(FlowAgentQuotaError, match="0 lượt"):
        manager.generate_images(["prompt"], tmp_path)


def test_zero_api_credits_do_not_block_available_flow_ui(tmp_path, monkeypatch):
    manager = FlowAgentManager()
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(
            Path("edge.exe"),
            Path("extension"),
            True,
            True,
            True,
            True,
            credits=0,
            browser_open=True,
            flow_tab_count=1,
            ui_image_capable=True,
        ),
    )
    monkeypatch.setattr(
        flow_agent,
        "_json_request",
        lambda *_args, **_kwargs: {"data": [{"url": "http://example/image.png"}]},
    )

    def fake_download(_url, destination, timeout=120.0):
        Image.new("RGB", (64, 36), "navy").save(destination)
        return destination

    monkeypatch.setattr(manager, "_download", fake_download)

    result = manager.generate_images(["prompt"], tmp_path / "Media")

    assert len(result) == 1
    assert result[0].path.is_file()


def test_both_ui_image_models_exhausted_is_a_quota_error():
    classified = flow_agent._friendly_generation_error(
        RuntimeError("ALL_IMAGE_MODELS_LIMITED")
    )

    assert isinstance(classified, FlowAgentQuotaError)
    assert "checkpoint" in str(classified)


def test_new_flow_host_permission_error_is_retryable_without_marking_submitted():
    classified = flow_agent._friendly_generation_error(
        RuntimeError(
            'Cannot access contents of url "https://flow.google.com/?pli=1". '
            "Extension manifest must request permission to access this host."
        )
    )

    assert isinstance(classified, FlowAgentUiError)
    assert "Prompt chưa được gửi" in str(classified)
    assert "checkpoint" in str(classified)


def test_missing_workspace_project_tab_is_retryable_without_marking_submitted():
    classified = flow_agent._friendly_generation_error(
        RuntimeError("FLOW_WORKSPACE_PROJECT_REOPEN_FAILED")
    )

    assert isinstance(classified, flow_agent.FlowAgentWindowError)
    assert "đúng project" in str(classified)


def test_workspace_flow_tab_timeout_is_classified_as_project_recovery():
    classified = flow_agent._friendly_generation_error(
        RuntimeError(
            "FLOW_UI_COMMAND_TIMEOUT:workspace-flow-tab:phase=get-flow-tab:tab=:"
            "project=project-123"
        )
    )

    assert isinstance(classified, flow_agent.FlowAgentWindowError)
    assert "project" in str(classified).lower()
    assert "Prompt chưa được gửi" in str(classified)


def test_no_progress_uncertainty_is_reclassified_as_safely_unsent():
    classified = flow_agent._friendly_generation_error(
        RuntimeError("FLOW_UI_SUBMISSION_UNCERTAIN: phase=no-progress")
    )

    assert isinstance(classified, FlowAgentUiError)
    assert not isinstance(classified, flow_agent.FlowAgentSubmissionUncertain)
    assert "FLOW_UI_TIMEOUT_UNSENT:phase=no-progress" in str(classified)


def test_flow_check_rejects_stale_resident_extension(monkeypatch):
    manager = FlowAgentManager()
    monkeypatch.setattr(flow_agent, "discover_edge", lambda: Path("edge.exe"))
    monkeypatch.setattr(flow_agent, "discover_extension_dir", lambda: Path("extension"))
    monkeypatch.setattr(flow_agent, "detect_edge_extension_installed", lambda: True)
    monkeypatch.setattr(
        flow_agent,
        "_json_request",
        lambda *_args, **_kwargs: {
            "extension_connected": True,
            "has_flow_key": True,
            "browser_open": True,
            "extension_version": "1.3.1",
        },
    )

    status = manager.check(include_credits=False)

    assert not status.ready
    assert not status.extension_compatible
    assert "đóng toàn bộ Edge" in status.detail


def test_flow_check_keeps_backend_running_when_health_probe_times_out(monkeypatch):
    manager = FlowAgentManager()
    monkeypatch.setattr(flow_agent, "discover_edge", lambda: Path("edge.exe"))
    monkeypatch.setattr(flow_agent, "discover_extension_dir", lambda: Path("extension"))
    monkeypatch.setattr(flow_agent, "detect_edge_extension_installed", lambda: True)
    monkeypatch.setattr(
        flow_agent,
        "_json_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            flow_agent.FlowAgentError("timed out")
        ),
    )
    monkeypatch.setattr(manager, "_backend_endpoint_open", lambda: True)

    status = manager.check(include_credits=False)

    assert status.backend_running
    assert not status.extension_connected
    assert not status.ready
    assert "health-check" in status.detail
    assert "không tạo backend mới" in status.detail


def test_flow_check_accepts_legacy_session_with_blank_extension_version(monkeypatch):
    manager = FlowAgentManager()
    monkeypatch.setattr(flow_agent, "discover_edge", lambda: Path("edge.exe"))
    monkeypatch.setattr(flow_agent, "discover_extension_dir", lambda: Path("extension"))
    monkeypatch.setattr(flow_agent, "detect_edge_extension_installed", lambda: True)
    monkeypatch.setattr(
        flow_agent,
        "_json_request",
        lambda *_args, **_kwargs: {
            "extension_connected": True,
            "has_flow_key": True,
            "browser_open": True,
            "extension_version": "",
        },
    )

    status = manager.check(include_credits=False)

    assert status.ready
    assert status.extension_compatible
    assert "sẵn sàng" in status.detail


def test_captcha_timeout_retries_without_marking_prompt_complete(tmp_path, monkeypatch):
    manager = FlowAgentManager()
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(Path("edge.exe"), Path("extension"), True, True, True, True),
    )
    attempts = []

    def fake_request(*_args, **_kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise flow_agent.FlowAgentError("CAPTCHA_FAILED: CAPTCHA_TIMEOUT")
        return {"data": [{"url": "http://example/image.png"}]}

    def fake_download(_url, destination, timeout=120.0):
        Image.new("RGB", (64, 36), "navy").save(destination)
        return destination

    monkeypatch.setattr(flow_agent, "_json_request", fake_request)
    monkeypatch.setattr(manager, "_download", fake_download)
    monkeypatch.setattr(flow_agent, "_wait_cancellable", lambda *_args: None)
    result = manager.generate_images(["one"], tmp_path / "Media")
    assert len(attempts) == 2
    assert result[0].path.is_file()


def test_missing_edge_window_is_reopened_and_same_prompt_retried(tmp_path, monkeypatch):
    manager = FlowAgentManager()
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(Path("edge.exe"), Path("extension"), True, True, True, True),
    )
    attempts = []
    reopened = []

    def fake_request(*_args, **_kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise flow_agent.FlowAgentError("No current window")
        return {"data": [{"url": "http://example/image.png"}]}

    def fake_download(_url, destination, timeout=120.0):
        Image.new("RGB", (64, 36), "navy").save(destination)
        return destination

    monkeypatch.setattr(flow_agent, "_json_request", fake_request)
    monkeypatch.setattr(manager, "_download", fake_download)
    monkeypatch.setattr(manager, "open_flow", lambda **kwargs: reopened.append(kwargs))
    monkeypatch.setattr(flow_agent, "_wait_cancellable", lambda *_args: None)

    result = manager.generate_images(["one"], tmp_path / "Media")

    assert len(attempts) == 2
    assert reopened == [{}]
    assert result[0].path.is_file()


def test_missing_edge_window_is_not_checkpointed_as_prompt_failure(tmp_path, monkeypatch):
    manager = FlowAgentManager()
    checkpoint = tmp_path / "flow.json"
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(Path("edge.exe"), Path("extension"), True, True, True, True),
    )
    monkeypatch.setattr(
        flow_agent,
        "_json_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            flow_agent.FlowAgentError("No current window")
        ),
    )
    monkeypatch.setattr(manager, "open_flow", lambda: None)
    monkeypatch.setattr(flow_agent, "_wait_cancellable", lambda *_args: None)

    with pytest.raises(flow_agent.FlowAgentWindowError, match="Edge không còn cửa sổ"):
        manager.generate_images(
            ["one", "two"],
            tmp_path / "Media",
            checkpoint_path=checkpoint,
            inter_prompt_delay=(0, 0),
        )

    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["failures"] == {}


def test_disconnected_extension_retries_current_prompt_until_reconnect(
    tmp_path, monkeypatch
):
    manager = FlowAgentManager()
    checkpoint = tmp_path / "flow.json"
    ready = FlowAgentStatus(
        Path("edge.exe"), Path("extension"), True, True, True, True
    )
    monkeypatch.setattr(manager, "check", lambda **_kwargs: ready)
    opened = []
    monkeypatch.setattr(manager, "open_flow", lambda **kwargs: opened.append(kwargs))
    calls = []

    def fake_request(_url, **kwargs):
        calls.append(kwargs["payload"]["prompt"])
        if len(calls) <= 3:
            raise flow_agent.FlowAgentError(
                "Google Flow extension is not connected. Open Google Flow in Edge/Chromium."
            )
        return {"data": [{"url": "http://example/image.png"}]}

    def download(_url, destination):
        Image.new("RGB", (64, 36), "navy").save(destination)
        return destination

    monkeypatch.setattr(flow_agent, "_json_request", fake_request)
    monkeypatch.setattr(manager, "_download", download)
    monkeypatch.setattr(flow_agent, "_wait_cancellable", lambda *_args: None)

    results = manager.generate_images(
        ["one", "two", "three"],
        tmp_path / "Media",
        checkpoint_path=checkpoint,
        inter_prompt_delay=(0, 0),
    )

    assert calls == ["one", "one", "one", "one", "two", "three"]
    assert opened == []
    assert len(results) == 3
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["failures"] == {}
    assert saved["completed"] == 3
    assert saved["incomplete_indices"] == []


def test_connection_loss_keeps_completed_image_and_resumes_same_batch_without_duplicate(
    tmp_path, monkeypatch
):
    manager = FlowAgentManager()
    checkpoint = tmp_path / "flow.json"
    media = tmp_path / "Media"
    monkeypatch.setattr(
        manager, "check",
        lambda: FlowAgentStatus(Path("edge.exe"), Path("extension"), True, True, True, True),
    )
    calls = []
    reconnect_after = 3

    def request(_url, **kwargs):
        prompt = kwargs["payload"]["prompt"]
        calls.append(prompt)
        nonlocal reconnect_after
        if prompt == "two" and reconnect_after > 0:
            reconnect_after -= 1
            raise flow_agent.FlowAgentError("Google Flow extension is not connected.")
        return {"data": [{"url": "http://example/image.png"}]}

    def download(_url, destination):
        Image.new("RGB", (64, 36), "navy").save(destination)
        return destination

    monkeypatch.setattr(flow_agent, "_json_request", request)
    monkeypatch.setattr(manager, "_download", download)
    monkeypatch.setattr(flow_agent, "_wait_cancellable", lambda *_args: None)
    results = manager.generate_images(
        ["one", "two", "three"], media, checkpoint_path=checkpoint,
        inter_prompt_delay=(0, 0),
    )

    assert calls == ["one", "two", "two", "two", "two", "three"]
    assert len(results) == 3
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["completed"] == 3
    assert saved["failures"] == {}
    first_image = media / "Image_0001.png"
    first_modified = first_image.stat().st_mtime_ns
    assert first_image.stat().st_mtime_ns == first_modified


def test_connection_loss_returns_to_checkpoint_after_bounded_wait(tmp_path, monkeypatch):
    manager = FlowAgentManager()
    checkpoint = tmp_path / "flow.json"
    monkeypatch.setattr(
        manager,
        "check",
        lambda **_kwargs: FlowAgentStatus(
            Path("edge.exe"), Path("extension"), True, True, True, True
        ),
    )
    monkeypatch.setattr(flow_agent, "FLOW_CONNECTION_MAX_WAIT_SECONDS", 0.0)
    calls = []

    def request(_url, **kwargs):
        calls.append(kwargs["payload"]["prompt"])
        raise flow_agent.FlowAgentError("Google Flow extension is not connected.")

    monkeypatch.setattr(flow_agent, "_json_request", request)
    monkeypatch.setattr(flow_agent, "_wait_cancellable", lambda *_args: None)

    with pytest.raises(flow_agent.FlowAgentConnectionError, match="checkpoint"):
        manager.generate_images(
            ["one", "two"],
            tmp_path / "Media",
            checkpoint_path=checkpoint,
            inter_prompt_delay=(0, 0),
        )

    assert calls == ["one"]
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["failures"] == {}
    assert saved["completed"] == 0


@pytest.mark.parametrize(
    "error_code",
    [
        "FLOW_UI_SUBMIT_NOT_FOUND",
        (
            'Cannot access contents of url "https://flow.google.com/?pli=1". '
            "Extension manifest must request permission to access this host."
        ),
    ],
)
def test_flow_ui_submit_failure_stops_batch_without_skipping_prompts(
    tmp_path, monkeypatch, error_code
):
    manager = FlowAgentManager()
    checkpoint = tmp_path / "flow.json"
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(Path("edge.exe"), Path("extension"), True, True, True, True),
    )
    calls = []

    def fake_request(_url, **kwargs):
        calls.append(kwargs["payload"]["prompt"])
        raise flow_agent.FlowAgentError(error_code)

    monkeypatch.setattr(flow_agent, "_json_request", fake_request)
    monkeypatch.setattr(flow_agent, "_wait_cancellable", lambda *_args: None)

    with pytest.raises(flow_agent.FlowAgentUiError, match="checkpoint"):
        manager.generate_images(
            ["one", "two"],
            tmp_path / "Media",
            checkpoint_path=checkpoint,
            inter_prompt_delay=(0, 0),
        )

    assert calls == ["one", "one", "one"]
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["failures"] == {}


@pytest.mark.parametrize(
    "error_code",
    ["FLOW_UI_TIMEOUT_UNSENT", "FLOW_UI_CONTENT_TIMEOUT"],
)
def test_flow_ui_unsent_keeps_retrying_current_prompt_until_it_is_accepted(
    tmp_path, monkeypatch, error_code
):
    manager = FlowAgentManager()
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(Path("edge.exe"), Path("extension"), True, True, True, True),
    )
    calls = []

    def fake_request(_url, **kwargs):
        calls.append(kwargs["payload"]["prompt"])
        if len(calls) <= 5:
            raise flow_agent.FlowAgentError(error_code)
        return {"data": [{"url": "http://example/image.png"}]}

    def fake_download(_url, destination, timeout=120.0):
        Image.new("RGB", (64, 36), "navy").save(destination)
        return destination

    monkeypatch.setattr(flow_agent, "_json_request", fake_request)
    monkeypatch.setattr(flow_agent, "_wait_cancellable", lambda *_args: None)
    monkeypatch.setattr(manager, "_download", fake_download)

    results = manager.generate_images(
        ["one"],
        tmp_path / "Media",
        checkpoint_path=tmp_path / "flow.json",
        inter_prompt_delay=(0, 0),
    )

    assert calls == ["one"] * 6
    assert len(results) == 1


def test_flow_ui_busy_waits_for_existing_job_without_skipping_or_resending(
    tmp_path, monkeypatch
):
    manager = FlowAgentManager()
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(Path("edge.exe"), Path("extension"), True, True, True, True),
    )
    calls = []

    def fake_request(_url, **kwargs):
        calls.append(kwargs["payload"]["prompt"])
        if len(calls) <= 4:
            raise flow_agent.FlowAgentError("FLOW_UI_BUSY")
        return {"data": [{"url": "http://example/image.png"}]}

    def fake_download(_url, destination, timeout=120.0):
        Image.new("RGB", (64, 36), "navy").save(destination)
        return destination

    monkeypatch.setattr(flow_agent, "_json_request", fake_request)
    monkeypatch.setattr(flow_agent, "_wait_cancellable", lambda *_args: None)
    monkeypatch.setattr(manager, "_download", fake_download)

    results = manager.generate_images(
        ["one"],
        tmp_path / "Media",
        checkpoint_path=tmp_path / "flow.json",
        inter_prompt_delay=(0, 0),
    )

    assert calls == ["one"] * 5
    assert len(results) == 1


def test_alternating_busy_and_unsent_share_one_recovery_budget(tmp_path, monkeypatch):
    manager = FlowAgentManager()
    monkeypatch.setattr(manager, "check", lambda: FlowAgentStatus(
        Path("edge.exe"), Path("extension"), True, True, True, True))
    clock = [0.0]
    calls = []
    monkeypatch.setattr(flow_agent.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(flow_agent, "_wait_cancellable", lambda *_: None)

    def request(_url, **kwargs):
        calls.append(kwargs["payload"]["prompt"])
        clock[0] += 160
        raise flow_agent.FlowAgentError(
            "FLOW_UI_BUSY" if len(calls) % 2 else "FLOW_UI_TIMEOUT_UNSENT")

    monkeypatch.setattr(flow_agent, "_json_request", request)
    with pytest.raises(FlowAgentUiError, match="FLOW_UI_BUSY_TIMEOUT"):
        manager.generate_images(["one", "two"], tmp_path / "Media")
    assert calls == ["one"] * 4


def test_uncertain_submission_survives_resume_without_resending(tmp_path, monkeypatch):
    manager = FlowAgentManager()
    monkeypatch.setattr(manager, "check", lambda: FlowAgentStatus(
        Path("edge.exe"), Path("extension"), True, True, True, True))
    calls = []
    def request(_url, **kwargs):
        calls.append(kwargs["payload"]["prompt"])
        raise flow_agent.FlowAgentError("FLOW_UI_SUBMISSION_UNCERTAIN")
    monkeypatch.setattr(flow_agent, "_json_request", request)
    for retry_failed in (False, True):
        with pytest.raises(flow_agent.FlowAgentSubmissionUncertain):
            manager.generate_images(["one", "two"], tmp_path / "Media",
                                    checkpoint_path=tmp_path / "flow.json", retry_failed=retry_failed)
    assert calls == ["one"]


def test_channel_closed_uncertainty_can_retry_after_authoritative_empty_snapshot():
    assert flow_agent._uncertainty_is_safe_to_retry_after_no_match({
        "submitted": False,
        "submission_uncertain": True,
        "reason": (
            "FLOW_UI_SUBMISSION_UNCERTAIN: A listener indicated an asynchronous "
            "response by returning true, but the message channel closed before a "
            "response was received:phase=generate-command"
        ),
    })
    assert not flow_agent._uncertainty_is_safe_to_retry_after_no_match({
        "submitted": True,
        "submission_uncertain": True,
        "reason": "FLOW_UI_SUBMISSION_UNCERTAIN: phase=generate-command",
    })
    assert not flow_agent._uncertainty_is_safe_to_retry_after_no_match({
        "submitted": False,
        "submission_uncertain": True,
        "flow_project_id": "project-a",
        "reason": (
            "FLOW_UI_SUBMISSION_UNCERTAIN: message port closed:"
            "phase=generate-command"
        ),
    }, "project-b")


def test_legacy_generic_uncertainty_requires_exact_project_identity():
    failure = {
        "submitted": False,
        "submission_uncertain": True,
        "flow_project_id": "project-a",
        "reason": "FLOW_UI_SUBMISSION_UNCERTAIN:phase=generate-command",
    }
    assert flow_agent._uncertainty_is_safe_to_retry_after_no_match(
        failure,
        "project-a",
    )
    assert not flow_agent._uncertainty_is_safe_to_retry_after_no_match(
        failure,
        "project-b",
    )
    assert not flow_agent._uncertainty_is_safe_to_retry_after_no_match(
        {**failure, "submitted": True},
        "project-a",
    )
    assert not flow_agent._uncertainty_is_safe_to_retry_after_no_match(
        {key: value for key, value in failure.items() if key != "submitted"},
        "project-a",
    )
    assert not flow_agent._uncertainty_is_safe_to_retry_after_no_match(
        {**failure, "flow_project_id": ""},
        "project-a",
    )


def test_flow_identity_is_recovered_only_when_failure_records_agree():
    project, client = flow_agent._flow_identity_from_failures({
        "1": {
            "reason": "FLOW_UI_SUBMISSION_UNCERTAIN:phase=generate-command:project=project-a",
            "flow_client_id": "client-a",
        },
        "2": {
            "flow_project_id": "project-a",
            "flow_client_id": "client-a",
        },
    })
    assert (project, client) == ("project-a", "client-a")
    assert flow_agent._flow_identity_from_failures({
        "1": {"flow_project_id": "project-a"},
        "2": {"flow_project_id": "project-b"},
    }) == ("", "")


def test_pre_submit_uncertainty_is_reconciled_after_complete_snapshot(tmp_path, monkeypatch):
    manager = FlowAgentManager()
    output = tmp_path / "Media"
    checkpoint = tmp_path / "flow.json"
    prompt = "one"
    signature = hashlib.sha256(
        json.dumps(
            {
                "model": flow_agent.FLOW_REQUIRED_IMAGE_MODEL,
                "aspect": "landscape",
                "prompts": [prompt],
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    flow_agent.atomic_write_json(
        checkpoint,
        {
            "version": 2,
            "signature": signature,
            "run_id": "pre-submit-recovery",
            "entries": {},
            "failures": {
                "1": {
                    "prompt": prompt,
                    "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "reason": "FLOW_UI_SUBMISSION_UNCERTAIN:phase=generate-command",
                    "submitted": False,
                    "submission_uncertain": True,
                    "flow_project_id": "flow-project",
                }
            },
        },
    )
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(Path("edge.exe"), Path("extension"), True, True, True, True),
    )
    monkeypatch.setattr(
        manager,
        "inspect_project_snapshot",
        lambda *_args, **_kwargs: {
            "projectId": "flow-project",
            "tileCount": 1,
            "tiles": [{"tileId": "existing", "promptText": "other prompt", "ready": True}],
            "reusedPromptReads": [{
                "tileId": "existing",
                "promptText": "other prompt",
                "reason": "reuse-prompt-read",
            }],
            "matches": [{"promptIndex": 1, "matched": False, "ready": False, "image": None}],
            "composer": {"hasPromptEditor": True, "stopVisible": False},
            "history": {
                "scanRequested": True,
                "scanComplete": True,
                "scannedTileCount": 1,
            },
        },
    )
    calls = []

    def request(_url, **kwargs):
        calls.append(kwargs["payload"]["prompt"])
        return {"data": [{"url": "http://example/retried.png"}], "projectId": "flow-project"}

    def download(_url, destination, timeout=120.0):
        Image.new("RGB", (64, 36), "navy").save(destination)
        return destination

    monkeypatch.setattr(flow_agent, "_json_request", request)
    monkeypatch.setattr(manager, "_download", download)
    result = manager.generate_images(
        [prompt],
        output,
        checkpoint_path=checkpoint,
        workspace_id="workspace",
        flow_project_id="flow-project",
        flow_client_id="client",
        inter_prompt_delay=(0, 0),
    )

    assert calls == [prompt]
    assert [item.prompt_index for item in result] == [1]
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["failures"] == {}
    assert saved["status"] == "complete"


@pytest.mark.parametrize("snapshot_error", [True, False])
def test_uncertain_resume_keeps_identity_when_history_cannot_be_verified(
    tmp_path, monkeypatch, snapshot_error
):
    manager = FlowAgentManager()
    checkpoint = tmp_path / "flow.json"
    prompt = "one"
    signature = hashlib.sha256(json.dumps({
        "model": flow_agent.FLOW_REQUIRED_IMAGE_MODEL,
        "aspect": "landscape", "prompts": [prompt],
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    failure = {
        "prompt": prompt,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "reason": "FLOW_UI_SUBMISSION_UNCERTAIN:phase=generate-command",
        "submitted": False, "submission_uncertain": True,
        "flow_project_id": "flow-project",
    }
    flow_agent.atomic_write_json(checkpoint, {
        "version": 2, "signature": signature, "run_id": "saved-run",
        "flow_project_id": "flow-project", "entries": {},
        "failures": {"1": failure},
    })
    monkeypatch.setattr(manager, "check", lambda: FlowAgentStatus(
        Path("edge.exe"), Path("extension"), True, True, True, True))

    def inspect(*_args, **_kwargs):
        if snapshot_error:
            raise flow_agent.FlowAgentError("FLOW_UI_PROJECT_NOT_READY")
        return {"projectId": "flow-project", "matches": [], "history": {"scanComplete": False}}

    monkeypatch.setattr(manager, "inspect_project_snapshot", inspect)
    requests = []
    monkeypatch.setattr(flow_agent, "_json_request", lambda *args, **kwargs: requests.append(kwargs))
    status = "unavailable" if snapshot_error else "unverified"
    with pytest.raises(flow_agent.FlowAgentSubmissionUncertain, match=f"project-snapshot={status}"):
        manager.generate_images([prompt], tmp_path / "Media", checkpoint_path=checkpoint,
                                workspace_id="workspace", flow_project_id="flow-project")
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert requests == []
    assert saved["run_id"] == "saved-run"
    assert saved["flow_project_id"] == "flow-project"
    assert saved["failures"]["1"] == failure
    assert saved["last_reconciliation"]["status"] == status
    assert saved["last_reconciliation"]["new_generation_dispatched"] is False


@pytest.mark.parametrize("verified_empty", [True, False])
@pytest.mark.parametrize("submitted", [True, False])
def test_empty_project_rebind_requires_proof_and_never_resends_submitted_prompt(
    tmp_path, monkeypatch, verified_empty, submitted
):
    manager = FlowAgentManager()
    checkpoint = tmp_path / "flow.json"
    prompt = "one"
    signature = hashlib.sha256(json.dumps({
        "model": flow_agent.FLOW_REQUIRED_IMAGE_MODEL, "aspect": "landscape",
        "prompts": [prompt],
    }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    failure = {"prompt": prompt, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "submitted": submitted, "submission_uncertain": not submitted,
        "flow_project_id": "old-project", "reason": (
            "FLOW_PROMPT_SUBMITTED_NO_IMAGE" if submitted else "FLOW_UI_SUBMISSION_UNCERTAIN:phase=generate-command")}
    flow_agent.atomic_write_json(checkpoint, {"version":2, "signature":signature,
        "run_id":"saved-run", "flow_project_id":"old-project", "entries":{}, "failures":{"1":failure}})
    monkeypatch.setattr(manager, "check", lambda: FlowAgentStatus(
        Path("edge.exe"), Path("extension"), True, True, True, True))
    monkeypatch.setattr(manager, "inspect_project_snapshot", lambda *_args, **_kwargs: {
        "projectId":"old-project", "tileCount":0, "tiles":[], "reusedPromptReads":[],
        "matches":[{"promptIndex":1,"matched":False}],
        "composer":{"hasPromptEditor":True,"stopVisible":False},
        "history":{"scanComplete":True,"emptyStateVerified":verified_empty}})
    requests=[]
    def request(_url, **kwargs):
        requests.append(kwargs["payload"])
        return {"data":[{"url":"http://example/new.png"}],"projectId":"dedicated-project"}
    def download(_url, destination, timeout=120):
        Image.new("RGB",(64,36),"navy").save(destination)
        return destination
    monkeypatch.setattr(flow_agent,"_json_request",request)
    monkeypatch.setattr(manager,"_download",download)
    if verified_empty and not submitted:
        result=manager.generate_images([prompt],tmp_path/"Media",checkpoint_path=checkpoint,
            workspace_id="workspace",flow_project_id="old-project",inter_prompt_delay=(0,0))
        assert len(result)==1 and len(requests)==1
        assert requests[0]["new_flow_project"] is True
        assert requests[0].get("flow_project_id") in (None, "")
        saved=json.loads(checkpoint.read_text(encoding="utf-8"))
        assert saved["flow_project_id"]=="dedicated-project"
        assert saved["flow_project_rebinds"][0]["from_project_id"]=="old-project"
    else:
        with pytest.raises(flow_agent.FlowAgentError):
            manager.generate_images([prompt],tmp_path/"Media",checkpoint_path=checkpoint,
                workspace_id="workspace",flow_project_id="old-project",inter_prompt_delay=(0,0))
        assert requests==[]
        saved=json.loads(checkpoint.read_text(encoding="utf-8"))
        assert saved["flow_project_id"]=="old-project"
        assert saved["failures"]["1"]==failure


def test_pre_submit_uncertainty_is_retryable():
    classified = flow_agent._friendly_generation_error(
        RuntimeError("FLOW_UI_SUBMISSION_UNCERTAIN: phase=configure")
    )

    assert isinstance(classified, FlowAgentUiError)
    assert not isinstance(classified, flow_agent.FlowAgentSubmissionUncertain)
    assert "FLOW_UI_TIMEOUT_UNSENT:phase=configure" in str(classified)


def test_legacy_no_progress_checkpoint_resumes_same_prompt(tmp_path, monkeypatch):
    manager = FlowAgentManager()
    checkpoint = tmp_path / "flow.json"
    output = tmp_path / "Media"
    prompt = "one"
    signature = hashlib.sha256(
        json.dumps(
            {
                "model": flow_agent.FLOW_REQUIRED_IMAGE_MODEL,
                "aspect": "landscape",
                "prompts": [prompt],
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    flow_agent.atomic_write_json(
        checkpoint,
        {
            "version": 2,
            "signature": signature,
            "run_id": "legacy-run",
            "entries": {},
            "failures": {
                "1": {
                    "prompt": prompt,
                    "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "reason": "FLOW_UI_SUBMISSION_UNCERTAIN: phase=no-progress",
                    "submitted": False,
                    "submission_uncertain": True,
                }
            },
        },
    )
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(Path("edge.exe"), Path("extension"), True, True, True, True),
    )
    calls = []

    def request(_url, **kwargs):
        calls.append(kwargs["payload"]["prompt"])
        return {"data": [{"url": "http://example/image.png"}]}

    def download(_url, destination, timeout=120.0):
        Image.new("RGB", (64, 36), "navy").save(destination)
        return destination

    monkeypatch.setattr(flow_agent, "_json_request", request)
    monkeypatch.setattr(manager, "_download", download)

    result = manager.generate_images(
        [prompt],
        output,
        checkpoint_path=checkpoint,
        inter_prompt_delay=(0, 0),
    )

    assert calls == [prompt]
    assert len(result) == 1
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert "1" not in saved["failures"]


def test_submitted_prompt_is_recovered_from_visible_flow_project_snapshot(tmp_path, monkeypatch):
    manager = FlowAgentManager()
    output = tmp_path / "Media"
    checkpoint = tmp_path / "flow.json"
    prompt = "one"
    signature = hashlib.sha256(
        json.dumps(
            {
                "model": flow_agent.FLOW_REQUIRED_IMAGE_MODEL,
                "aspect": "landscape",
                "prompts": [prompt],
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    flow_agent.atomic_write_json(
        checkpoint,
        {
            "version": 2,
            "signature": signature,
            "run_id": "recovery-run",
            "entries": {},
            "failures": {
                "1": {
                    "prompt": prompt,
                    "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "reason": "FLOW_PROMPT_SUBMITTED_NO_IMAGE: response lost",
                    "submitted": True,
                    "submission_uncertain": False,
                }
            },
        },
    )
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(Path("edge.exe"), Path("extension"), True, True, True, True),
    )
    monkeypatch.setattr(
        manager,
        "inspect_project_snapshot",
        lambda *_args, **_kwargs: {
            "matches": [{
                "promptIndex": 1,
                "matched": True,
                "tileId": "flow-tile-1",
                "ready": True,
                "image": {
                    "tileId": "flow-tile-1",
                    "url": "https://flow-content.google/recovered.png",
                },
            }],
        },
    )

    def fake_download(_url, destination, timeout=120.0):
        Image.new("RGB", (64, 36), "navy").save(destination)
        return destination

    monkeypatch.setattr(manager, "_download", fake_download)
    result = manager.generate_images(
        [prompt],
        output,
        checkpoint_path=checkpoint,
        inter_prompt_delay=(0, 0),
    )
    assert [item.prompt_index for item in result] == [1]
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["entries"]["1"]["source"] == "flow_project_snapshot"
    assert saved["status"] == "complete"


@pytest.mark.parametrize("authoritative", [True, False])
def test_completed_foreign_project_collision_rebinds_only_after_authoritative_no_match(
    tmp_path, monkeypatch, authoritative
):
    manager = FlowAgentManager()
    workspace_root = tmp_path / "workspaces"
    current_root = workspace_root / "DaiTa9"
    owner_root = workspace_root / "DaiTa14"
    current_history = current_root / ".docurandom" / "flow_history.json"
    owner_history = owner_root / ".docurandom" / "flow_history.json"
    prompt = "new workspace prompt"
    old_project_id = "shared-completed-project"
    signature = hashlib.sha256(
        json.dumps(
            {
                "model": flow_agent.FLOW_REQUIRED_IMAGE_MODEL,
                "aspect": "landscape",
                "prompts": [prompt],
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    flow_agent.atomic_write_json(
        current_history,
        {
            "version": 2,
            "signature": signature,
            "run_id": "stale-project-run",
            "workspace_id": "workspace-current",
            "flow_project_id": old_project_id,
            "entries": {},
            "failures": {
                "1": {
                    "prompt": prompt,
                    "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "reason": (
                        "FLOW_UI_SUBMISSION_UNCERTAIN:phase=generate-command:"
                        f"project={old_project_id}"
                    ),
                    "submitted": False,
                    "submission_uncertain": True,
                    "workspace_id": "workspace-current",
                    "flow_project_id": old_project_id,
                }
            },
        },
    )
    flow_agent.atomic_write_json(
        owner_history,
        {
            "version": 2,
            "workspace_id": "workspace-owner",
            "flow_project_id": old_project_id,
            "status": "complete",
            "total": 1,
            "completed": 1,
            "failed": 0,
            "entries": {"1": {"prompt_sha256": "completed-prompt-hash"}},
            "failures": {},
        },
    )
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(
            Path("edge.exe"), Path("extension"), True, True, True, True,
            browser_open=True, flow_tab_count=1, ui_image_capable=True,
        ),
    )
    monkeypatch.setattr(
        manager,
        "inspect_project_snapshot",
        lambda *_args, **_kwargs: {
            "projectId": old_project_id,
            "tileCount": 1,
            "tiles": [{"tileId": "owner-tile", "promptText": "other prompt", "ready": True}],
            "reusedPromptReads": [{
                "tileId": "owner-tile",
                "promptText": "other prompt",
                "reason": "reuse-prompt-read",
            }],
            "matches": [{"promptIndex": 1, "matched": False, "ready": False, "image": None}],
            "composer": {"hasPromptEditor": True, "stopVisible": False},
            "history": {"scanComplete": authoritative, "scannedTileCount": 1},
        },
    )
    generation_requests = []

    def fake_request(_url, **kwargs):
        generation_requests.append(kwargs["payload"])
        return {
            "projectId": "new-workspace-project",
            "data": [{
                "url": "http://example/new-workspace.png",
                "media_id": "new-workspace-image",
                "project_id": "new-workspace-project",
            }],
        }

    def fake_download(_url, destination, timeout=120.0):
        Image.new("RGB", (64, 36), "navy").save(destination)
        return destination

    monkeypatch.setattr(flow_agent, "_json_request", fake_request)
    monkeypatch.setattr(manager, "_download", fake_download)

    if not authoritative:
        with pytest.raises(flow_agent.FlowAgentSubmissionUncertain):
            manager.generate_images(
                [prompt],
                current_root / "Media",
                checkpoint_path=current_history,
                workspace_id="workspace-current",
                inter_prompt_delay=(0, 0),
            )
        saved = json.loads(current_history.read_text(encoding="utf-8"))
        assert saved["flow_project_id"] == old_project_id
        assert generation_requests == []
        return

    result = manager.generate_images(
        [prompt],
        current_root / "Media",
        checkpoint_path=current_history,
        workspace_id="workspace-current",
        inter_prompt_delay=(0, 0),
    )

    assert [item.prompt_index for item in result] == [1]
    assert generation_requests[0]["flow_project_id"] is None
    assert generation_requests[0]["new_flow_project"] is True
    saved = json.loads(current_history.read_text(encoding="utf-8"))
    assert saved["flow_project_id"] == "new-workspace-project"
    assert saved["failures"] == {}
    assert saved["flow_project_rebinds"][-1]["from_project_id"] == old_project_id
    assert saved["flow_project_rebinds"][-1]["owner_workspace_id"] == "workspace-owner"


def test_resume_uses_fresh_idempotency_generation(tmp_path, monkeypatch):
    manager = FlowAgentManager()
    checkpoint = tmp_path / "flow.json"
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(Path("edge.exe"), Path("extension"), True, True, True, True),
    )
    keys = []

    def fake_request(_url, **kwargs):
        keys.append(kwargs["headers"]["Idempotency-Key"])
        raise flow_agent.FlowAgentError("Image blocked by safety policy")

    monkeypatch.setattr(flow_agent, "_json_request", fake_request)

    for retry_failed in (False, True):
        with pytest.raises(flow_agent.FlowAgentError, match="mới xác nhận đã gửi 0/1"):
            manager.generate_images(
                ["blocked"],
                tmp_path / "Media",
                checkpoint_path=checkpoint,
                inter_prompt_delay=(0, 0),
                retry_failed=retry_failed,
            )

    assert len(keys) == 2
    assert keys[0] != keys[1]
    assert "-r0001-" in keys[0]
    assert "-r0002-" in keys[1]


def test_submitted_policy_failure_does_not_block_later_prompts(
    tmp_path, monkeypatch
):
    manager = FlowAgentManager()
    checkpoint = tmp_path / "flow.json"
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(Path("edge.exe"), Path("extension"), True, True, True, True),
    )
    calls = []

    def fake_request(_url, **kwargs):
        prompt = kwargs["payload"]["prompt"]
        calls.append(prompt)
        if prompt == "blocked":
            raise flow_agent.FlowAgentError(
                "FLOW_PROMPT_SUBMITTED_NO_IMAGE: POLICY_REJECTED"
            )
        return {"data": [{"url": f"http://example/{prompt}.png"}]}

    def fake_download(_url, destination, timeout=120.0):
        Image.new("RGB", (64, 36), "navy").save(destination)
        return destination

    monkeypatch.setattr(flow_agent, "_json_request", fake_request)
    monkeypatch.setattr(manager, "_download", fake_download)
    result = manager.generate_images(
        ["first", "blocked", "third"],
        tmp_path / "Media",
        checkpoint_path=checkpoint,
        inter_prompt_delay=(0, 0),
    )
    assert calls == ["first", "blocked", "third"]
    assert [item.prompt_index for item in result] == [1, 3]
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["version"] == 2
    assert saved["prompts"] == ["first", "blocked", "third"]
    assert sorted(saved["entries"]) == ["1", "3"]
    assert saved["failures"]["2"]["prompt"] == "blocked"
    assert saved["failures"]["2"]["submitted"] is True
    assert saved["status"] == "complete_with_image_failures"
    assert saved["submitted"] == 3
    assert saved["incomplete_indices"] == []
    assert saved["missing_image_indices"] == [2]

    repeat_calls = []
    monkeypatch.setattr(
        flow_agent,
        "_json_request",
        lambda *_args, **_kwargs: repeat_calls.append(kwargs["payload"]["prompt"]),
    )
    resumed = manager.generate_images(
        ["first", "blocked", "third"],
        tmp_path / "Media",
        checkpoint_path=checkpoint,
        inter_prompt_delay=(0, 0),
        retry_failed=True,
    )
    assert repeat_calls == []
    assert [item.prompt_index for item in resumed] == [1, 3]


def test_failed_prompt_can_be_retried_without_regenerating_completed_images(
    tmp_path, monkeypatch
):
    manager = FlowAgentManager()
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(Path("edge.exe"), Path("extension"), True, True, True, True),
    )
    first_calls = []

    def first_request(_url, **kwargs):
        prompt = kwargs["payload"]["prompt"]
        first_calls.append(prompt)
        if prompt == "blocked":
            raise flow_agent.FlowAgentError("Image blocked by safety policy")
        return {"data": [{"url": f"http://example/{prompt}.png"}]}

    def fake_download(_url, destination, timeout=120.0):
        Image.new("RGB", (64, 36), "navy").save(destination)
        return destination

    monkeypatch.setattr(flow_agent, "_json_request", first_request)
    monkeypatch.setattr(manager, "_download", fake_download)
    with pytest.raises(
        flow_agent.FlowAgentIncompleteError,
        match="mới xác nhận đã gửi 1/2",
    ):
        manager.generate_images(
            ["first", "blocked"],
            tmp_path / "Media",
            inter_prompt_delay=(0, 0),
        )

    retry_calls = []
    monkeypatch.setattr(
        flow_agent,
        "_json_request",
        lambda _url, **kwargs: (
            retry_calls.append(kwargs["payload"]["prompt"])
            or {"data": [{"url": "http://example/recovered.png"}]}
        ),
    )
    recovered = manager.generate_images(
        ["first", "blocked"],
        tmp_path / "Media",
        inter_prompt_delay=(0, 0),
        retry_failed=True,
    )

    assert retry_calls == ["blocked"]
    assert [item.prompt_index for item in recovered] == [1, 2]


def test_extension_recreates_normal_window_when_edge_has_no_current_window():
    project_root = Path(__file__).resolve().parents[1]
    background = (project_root / "flow-extension" / "background.js").read_text(
        encoding="utf-8"
    )
    flow_ui = (project_root / "flow-extension" / "flow-ui.js").read_text(
        encoding="utf-8"
    )
    manifest = json.loads(
        (project_root / "flow-extension" / "manifest.json").read_text(encoding="utf-8")
    )

    assert "createFlowTabWithWindowFallback" in background
    assert "flowTabCreationLock" in background
    assert "withFlowTabCreationLock" in background
    assert "chrome.windows.create" in background
    assert "focused: false" in background
    assert "state: 'minimized'" in background
    assert "dedicated minimized Edge window" in background
    assert "active: false" in background
    assert "findReusableFlowTab" in background
    assert "prepareFlowTab" in background
    assert "reload_extension" in background
    assert "FLOW_WORK_TAB_SESSION_KEY" in background
    assert "FLOW_AUTOMATION_WINDOW_SESSION_KEY" in background
    assert "rememberAutomationWindow" in background
    assert "parkAutomationWindow" in background
    assert "Parked completed workspace Flow tab" in background
    assert "chrome.storage.session" in background
    assert "findExistingFlowTab" in background
    assert "flowProjectIdFromUrl" in background
    assert "tab?.pendingUrl" in background
    assert "probeFlowTabProjectId" in background
    assert "FLOW_PROJECT_URL_BUILDERS" in background
    assert "flowProjectUrlCandidates" in background
    assert "isExactFlowProjectTab" in background
    assert "reopenWorkspaceFlowTab" in background
    assert "FLOW_WORKSPACE_PROJECT_REOPEN_FAILED" in background
    assert "FLOW_UI_SUBMISSION_UNCERTAIN:phase=generate-command:cause=" in background
    assert "cancelAck=" in background
    assert "workspaceFlowReopenings" in background
    assert "chrome.tabs.onRemoved" in background
    assert "projectUrl" in background
    assert "chrome.scripting.executeScript" in background
    assert "await getOrOpenFlowTab()" in background
    assert manifest["version"] == flow_agent.FLOW_EXTENSION_VERSION
    assert flow_agent.FLOW_EXTENSION_VERSION == manifest["version"]
    assert "https://flow.google.com/*" in manifest["host_permissions"]
    assert "https://labs.google/*" in manifest["host_permissions"]
    content_matches = manifest["content_scripts"][0]["matches"]
    assert "https://flow.google.com/*" in content_matches
    assert "https://labs.google/fx/tools/flow*" in content_matches
    assert "'https://flow.google.com/*'" in background
    assert "'https://labs.google/fx/tools/flow*'" in background
    assert "const FLOW_URL = 'https://flow.google.com/'" in background
    assert "isLegacyFlowMarketingLanding" in background
    assert "await chrome.tabs.update(tab.id, { url: FLOW_URL })" in background
    assert "typeof chrome.windows?.getAll === 'function'" in background
    assert "const tabs = await chrome.tabs.query({});" in background
    assert "location.hostname === 'flow.google.com'" in flow_ui
    assert "chrome.runtime.getManifest().version" in flow_ui
    assert "DOCURANDOM_FLOW_UI_VERSION = '" not in flow_ui


def test_visible_flow_ui_recovery_never_reloads_the_generation_document():
    project_root = Path(__file__).resolve().parents[1]
    background = (project_root / "flow-extension" / "background.js").read_text(
        encoding="utf-8"
    )
    flow_ui = (project_root / "flow-extension" / "flow-ui.js").read_text(
        encoding="utf-8"
    )
    generation_block = background.split(
        "async function handleUiGenerateImages", 1
    )[1].split("async function cancelUiJob", 1)[0]
    stale_reset_block = background.split(
        "async function resetStaleUiJob", 1
    )[1].split("async function rejectIfUiBusy", 1)[0]

    assert "chrome.tabs.reload" not in generation_block
    assert "chrome.tabs.reload" not in stale_reset_block
    assert "Storage.clearDataForOrigin" not in background
    assert "chrome.cookies.remove" not in background
    assert "recoverFlowUiInPlace" in generation_block
    assert "FLOW_UI_TIMEOUT_UNSENT:phase=no-progress" in generation_block
    assert "progressObserved" in generation_block
    assert "enqueueUiOperation" in background
    assert "uiOperationTail" in background
    assert "uiQueueDepth" in background
    assert "DOCURANDOM_UI_RECOVER" in flow_ui
    assert "if (force && activeUiJob)" in background
    assert "matchedTabId" in background
    assert "history" in background
    # The only remaining page reload belongs to the isolated hard token/API
    # refresh path; visible UI generation never calls that path.
    assert background.count("chrome.tabs.reload") == 1


def test_flow_ui_capability_is_ready_without_legacy_api_token(monkeypatch):
    manager = FlowAgentManager()
    monkeypatch.setattr(flow_agent, "discover_edge", lambda: Path("edge.exe"))
    monkeypatch.setattr(flow_agent, "discover_extension_dir", lambda: Path("extension"))
    monkeypatch.setattr(flow_agent, "detect_edge_extension_installed", lambda: True)
    monkeypatch.setattr(
        flow_agent,
        "_json_request",
        lambda *_args, **_kwargs: {
            "extension_connected": True,
            "has_flow_key": False,
            "browser_open": True,
            "flow_tab_count": 1,
            "capabilities": ["ui_text_to_image"],
            "extension_version": flow_agent.FLOW_EXTENSION_VERSION,
        },
    )

    status = manager.check(include_credits=False)

    assert status.ready is True
    assert status.ui_image_capable is True
    assert "giao diện Flow" in status.detail


def test_flow_ui_capability_does_not_accept_cached_token_without_flow_tab(monkeypatch):
    manager = FlowAgentManager()
    monkeypatch.setattr(flow_agent, "discover_edge", lambda: Path("edge.exe"))
    monkeypatch.setattr(flow_agent, "discover_extension_dir", lambda: Path("extension"))
    monkeypatch.setattr(flow_agent, "detect_edge_extension_installed", lambda: True)
    monkeypatch.setattr(
        flow_agent,
        "_json_request",
        lambda *_args, **_kwargs: {
            "extension_connected": True,
            "has_flow_key": True,
            "browser_open": True,
            "flow_tab_count": 0,
            "capabilities": ["ui_text_to_image"],
            "extension_version": flow_agent.FLOW_EXTENSION_VERSION,
        },
    )

    status = manager.check(include_credits=False)

    assert status.ready is False
    assert status.has_flow_key is True
    assert status.ui_image_capable is True
    assert "chưa có tab Google Flow" in status.detail


def test_flow_status_reports_multiple_ui_profiles_for_safe_failover(monkeypatch):
    manager = FlowAgentManager()
    monkeypatch.setattr(flow_agent, "discover_edge", lambda: Path("edge.exe"))
    monkeypatch.setattr(flow_agent, "discover_extension_dir", lambda: Path("extension"))
    monkeypatch.setattr(flow_agent, "detect_edge_extension_installed", lambda: True)
    monkeypatch.setattr(
        flow_agent,
        "_json_request",
        lambda *_args, **_kwargs: {
            "extension_connected": True,
            "has_flow_key": False,
            "browser_open": True,
            "flow_tab_count": 2,
            "capabilities": ["ui_text_to_image"],
            "extension_version": flow_agent.FLOW_EXTENSION_VERSION,
            "clients": [
                {
                    "id": "profile-a",
                    "capabilities": ["ui_text_to_image"],
                    "flow_tab_count": 1,
                },
                {
                    "id": "profile-b",
                    "capabilities": ["ui_text_to_image"],
                    "flow_tab_count": 1,
                },
            ],
        },
    )

    status = manager.check(include_credits=False)

    assert status.connected_client_count == 2
    assert status.ui_client_count == 2
    assert status.flow_tab_count == 2
    assert status.flow_client_count_with_tabs == 2
    assert "tự chuyển profile" in status.detail


def test_flow_tab_diagnostic_distinguishes_total_tabs_from_clients(monkeypatch):
    manager = FlowAgentManager()
    clients = [
        {
            "id": f"profile-{index}",
            "capabilities": ["ui_text_to_image"],
            "flow_tab_count": 2 if index == 0 else 1,
        }
        for index in range(12)
    ]
    monkeypatch.setattr(flow_agent, "discover_edge", lambda: Path("edge.exe"))
    monkeypatch.setattr(flow_agent, "discover_extension_dir", lambda: Path("extension"))
    monkeypatch.setattr(flow_agent, "detect_edge_extension_installed", lambda: True)
    monkeypatch.setattr(
        flow_agent,
        "_json_request",
        lambda *_args, **_kwargs: {
            "extension_connected": True,
            "has_flow_key": False,
            "browser_open": True,
            "flow_tab_count": 13,
            "capabilities": ["ui_text_to_image"],
            "extension_version": flow_agent.FLOW_EXTENSION_VERSION,
            "clients": clients,
        },
    )

    status = manager.check(include_credits=False)

    assert status.flow_tab_count == 13
    assert status.connected_client_count == 12
    assert status.flow_client_count_with_tabs == 12


def test_flow_browser_launch_is_shared_and_throttled_across_workspaces(monkeypatch):
    calls = []
    monkeypatch.setattr(flow_agent, "_LAST_EDGE_LAUNCH_AT", 0.0)
    monkeypatch.setattr(flow_agent, "discover_edge", lambda: Path("edge.exe"))
    # Keep this fallback test isolated from a real Flow backend that may be
    # running on the developer machine during the full suite.
    monkeypatch.setattr(
        flow_agent,
        "_json_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            flow_agent.FlowAgentError("fixture: Flow backend unavailable")
        ),
    )
    monkeypatch.setattr(flow_agent.FlowAgentManager, "_backend_endpoint_open", lambda self: False)
    monkeypatch.setattr(
        flow_agent.subprocess,
        "Popen",
        lambda command, *_args, **_kwargs: calls.append(command),
    )

    FlowAgentManager().open_flow()
    FlowAgentManager().open_flow()

    assert calls == [[str(Path("edge.exe")), "--start-minimized", flow_agent.FLOW_WEB_URL]]


def test_flow_browser_launch_uses_resident_extension_without_native_spawn(monkeypatch):
    requests = []
    monkeypatch.setattr(flow_agent, "discover_edge", lambda: Path("edge.exe"))

    def fake_request(url, **kwargs):
        requests.append((url, kwargs))
        return {"status": 200, "result": {"tabId": 42, "active": False}}

    monkeypatch.setattr(flow_agent, "_json_request", fake_request)

    def unexpected_spawn(*_args, **_kwargs):
        raise AssertionError("resident extension path must not spawn Edge")

    monkeypatch.setattr(flow_agent.subprocess, "Popen", unexpected_spawn)

    FlowAgentManager().open_flow()

    assert requests == [(
        "http://127.0.0.1:8001/v1/prepare-flow-tab",
        {"method": "POST", "timeout": 20.0},
    )]


def test_flow_browser_prepare_waits_for_extension_reconnect(monkeypatch):
    manager = FlowAgentManager()
    monkeypatch.setattr(flow_agent, "discover_edge", lambda: Path("edge.exe"))
    monkeypatch.setattr(manager, "_backend_endpoint_open", lambda: True)
    monkeypatch.setattr(flow_agent, "_FLOW_OPEN_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(flow_agent, "_FLOW_OPEN_POLL_SECONDS", 0.01)
    requests = []
    prepare_calls = 0

    def fake_request(url, **_kwargs):
        nonlocal prepare_calls
        requests.append(url)
        if url.endswith("/v1/prepare-flow-tab"):
            prepare_calls += 1
            if prepare_calls == 1:
                return {"status": 503, "error": "FLOW_UI_EXTENSION_NOT_AVAILABLE"}
            return {"status": 200, "result": {"tabId": 42, "active": False}}
        return {"extension_connected": True, "flow_tab_count": 0}

    monkeypatch.setattr(flow_agent, "_json_request", fake_request)
    monkeypatch.setattr(
        flow_agent.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reconnecting resident extension must not spawn Edge")
        ),
    )

    manager.open_flow()

    assert requests == [
        "http://127.0.0.1:8001/v1/prepare-flow-tab",
        "http://127.0.0.1:8001/health",
        "http://127.0.0.1:8001/v1/prepare-flow-tab",
    ]


def test_flow_browser_prepare_is_serialized_across_workspaces(monkeypatch):
    monkeypatch.setattr(flow_agent, "discover_edge", lambda: Path("edge.exe"))
    entered = threading.Event()
    release = threading.Event()
    guard = threading.Lock()
    calls = []
    active = 0
    max_active = 0
    errors = []

    def fake_request(url, **_kwargs):
        nonlocal active, max_active
        assert url.endswith("/v1/prepare-flow-tab")
        with guard:
            calls.append(url)
            active += 1
            max_active = max(max_active, active)
        entered.set()
        try:
            assert release.wait(2.0)
            return {"status": 200, "result": {"tabId": 42, "active": False}}
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(flow_agent, "_json_request", fake_request)

    def run_open():
        try:
            FlowAgentManager().open_flow()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run_open) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        assert entered.wait(1.0)
        time.sleep(0.05)
        assert len(calls) == 1
    finally:
        release.set()
        for thread in threads:
            thread.join(2.0)

    assert errors == []
    assert len(calls) == 2
    assert max_active == 1


def test_flow_extension_setup_uses_one_edge_invocation_for_two_setup_tabs(monkeypatch):
    calls = []
    monkeypatch.setattr(flow_agent, "discover_edge", lambda: Path("edge.exe"))
    monkeypatch.setattr(flow_agent, "discover_extension_dir", lambda: Path("extension"))
    monkeypatch.setattr(flow_agent, "_claim_edge_launch", lambda force=False: True)
    monkeypatch.setattr(
        flow_agent.subprocess,
        "Popen",
        lambda command, *_args, **_kwargs: calls.append(command),
    )

    FlowAgentManager().open_extension_setup(force=True)

    edge_calls = [command for command in calls if command[0] == str(Path("edge.exe"))]
    assert edge_calls == [[
        str(Path("edge.exe")),
        "edge://extensions/",
        flow_agent.FLOW_WEB_URL,
    ]]


def test_bridge_prepare_flow_tab_does_not_require_an_existing_flow_tab(monkeypatch):
    from flow_engine import bridge as bridge_module
    from flow_engine.bridge import ExtensionBridge

    monkeypatch.setattr(bridge_module, "_has_visible_edge_window", lambda: True)

    async def exercise():
        bridge = ExtensionBridge()
        bridge._loop = asyncio.get_running_loop()
        bridge._clients["edge-client"] = object()
        bridge._browser_open["edge-client"] = True
        bridge._flow_tab_counts["edge-client"] = 0
        bridge._capabilities["edge-client"] = {"ui_text_to_image"}
        bridge._last_seen["edge-client"] = bridge_module._time.monotonic()
        sent = []

        async def send(client_id, message):
            sent.append((client_id, message))
            bridge._route_response(message["id"], {
                "id": message["id"],
                "status": 200,
                "result": {"tabId": 99, "windowId": 7, "active": False},
            })
            return True

        monkeypatch.setattr(bridge, "send_message_to", send)
        result = await bridge.prepare_flow_tab()

        assert result["status"] == 200
        assert result["result"]["tabId"] == 99
        assert result["_client_id"] == "edge-client"
        assert sent[0][1]["method"] == "prepare_flow_tab"
        assert sent[0][1]["existingOnly"] is False

    asyncio.run(exercise())


def test_extension_contains_project_backed_flow_ui_automation():
    project_root = Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (project_root / "flow-extension" / "manifest.json").read_text(encoding="utf-8")
    )
    background = (project_root / "flow-extension" / "background.js").read_text(
        encoding="utf-8"
    )
    content = (project_root / "flow-extension" / "flow-ui.js").read_text(
        encoding="utf-8"
    )
    selectors = (project_root / "flow-extension" / "flow-selectors.js").read_text(
        encoding="utf-8"
    )

    scripts = manifest["content_scripts"][0]["js"]
    assert "flow-selectors.js" in scripts
    assert "flow-ui.js" in scripts
    assert "downloads" in manifest["permissions"]
    assert "debugger" in manifest["permissions"]
    assert "cookies" not in manifest["permissions"]
    assert "ui_text_to_image" in background
    assert "Input.insertText" in background
    assert "Input.dispatchKeyEvent" in background
    assert "UI_TRUSTED_PRESS_ENTER" in background
    assert "UI_TRUSTED_PRESS_ESCAPE" in background
    assert "ensureUiAutomationVersion" in background
    assert "recoverFlowUiInPlace" in background
    assert "UI_BACKGROUND_SLEEP" in background
    assert "UI_BACKGROUND_SLEEP" in content
    assert "DOCURANDOM_UI_PULSE" in background
    assert "DOCURANDOM_UI_PULSE" in content
    assert "Page.setWebLifecycleState" in background
    assert "Emulation.setFocusEmulationEnabled" in background
    assert "startUiWakePulse" in background
    assert "stopUiWakePulse" in background
    assert "Page.bringToFront" not in background
    assert "UI_JOB_STALE_IDLE_TIMEOUT_MS" in background
    assert "resetStaleUiJob" in background
    assert "lastProgressAt" in background
    assert "FLOW_UI_SUBMIT_NOT_FOUND" in background
    assert "FLOW_UI_SUBMIT_NOT_CONFIRMED" in background
    assert "recover-after-submission" in background
    assert "promptOutcomes" in background
    assert "Storage.clearDataForOrigin" not in background
    assert "Network.setCacheDisabled" not in background
    assert "FLOW_AUTH_COOKIE_PREFIXES" not in background
    assert "DOCURANDOM_UI_RECOVER" in content
    assert "DOCURANDOM_UI_GENERATE" in content
    assert "DOCURANDOM_FLOW_UI_VERSION" in content
    assert "FLOW_UI_SUBMIT_NOT_FOUND" in content
    assert "FLOW_UI_SUBMIT_NOT_CONFIRMED" in content
    assert "findSubmitButton" in content
    assert "findComposerControl" in content
    assert "configuredImageOptions" not in content
    assert "const configButton = buttonWithIcon(['crop', 'tune']);" not in content
    assert "roots.push(document)" not in content
    assert "normalizePrompt" in content
    assert "promptMatchesEditor" in content
    assert "submitBeforeEnter" in content
    assert "submittedPromptNumbers" in content
    assert "submitted-no-image" in content
    assert "promptOutcomes" in content
    assert "if (prefix && !editorText.includes(prefix)) return true;" not in content
    assert "div[role=\"textbox\"]" in content
    assert "focusPromptEditor" in content
    assert "recoverPromptEditor" in content
    assert "stableClickPoint" not in content
    assert "document.elementFromPoint" not in content
    assert "UI_TRUSTED_CLICK" not in content
    assert "UI_TRUSTED_CLICK" not in background
    assert "Input.dispatchMouseEvent" not in background
    assert "await click(textbox, job, 'prompt-editor')" not in content
    assert "stabilizeFlowWorkspaceUi" in content
    assert "settings_2" in content
    assert "grid-layout" in content
    assert "show-prompt-text" in content
    assert "clear-old-prompt" in content
    assert "randomizedComposerSettle" in content
    assert "FLOW_SELECTORS.promptEditors" in content
    assert "__DOCURANDOM_FLOW_SELECTORS__" in selectors
    assert "schemaVersion: 1" in selectors
    assert "div[role=\"textbox\"]" in selectors
    assert "CAPTCHA_REQUIRED" in content
    assert "MODEL_DAILY_LIMIT" in content
    assert "REQUIRED_IMAGE_MODEL_LIMITED" in content
    assert "exhaustedModels" in content
    assert "REQUIRED_IMAGE_MODEL = 'harbor_seal'" in content
    assert "REQUIRED_IMAGE_COUNT = 1" in content
    assert "FLOW_UI_IMAGE_COUNT_NOT_CONFIRMED" in content
    assert "FLOW_UI_IMAGE_MODEL_NOT_CONFIRMED" in content
    assert "DOCURANDOM_UI_INSPECT_CONFIGURATION" in content
    assert "selectedStateOnNode" in content
    assert "input[type=\"radio\"]" in content
    assert "rovingControlIsSelected" in content
    assert "UI_TRUSTED_PRESS_ENTER" in content
    assert "withActiveTargetTab" not in background
    assert "chrome.tabs.update(tabId, { active: true })" not in background
    assert "chrome.tabs.update(tabs[0].id, { active: true })" not in background
    assert "active: false" in background
    assert "windowId: rememberedWindow.id" in background
    assert "prepare_flow_tab" in background
    assert "new PointerEvent('pointerdown'" in content
    assert "harbor_seal: ['Nano Banana 2 Lite']" in content
    assert "value.includes('nano banana 2 lite')" in content
    assert "arrow_drop_down" in content
    assert "total -= 150" in content
    assert "button[aria-haspopup=\"menu\"]" in content
    assert "findRequiredModelOption" in content
    assert "activateWithTrustedEnter" in content
    assert "openRequiredModelOption" in content
    assert "selectRequiredModelOption" in content
    assert "required-model-option-dom" in content
    assert "harbor_seal: ['Nano Banana 2 Lite', 'Nano Banana 2']" not in content
    assert "promptTouched: false" in content
    assert "promptSubmitted: false" in content
    assert "observedModelText: textOf(modelConfirmed)" in content
    assert "model-fallback" not in content
    assert "count: REQUIRED_IMAGE_COUNT" in background
    assert "model: REQUIRED_IMAGE_MODEL" in background
    assert "ui_inspect_configuration" in background
    assert "FLOW_UI_IMAGE_COUNT_NOT_CONFIRMED" in background


def test_text_to_image_prefers_visible_flow_project_ui():
    from flow_engine.generators.t2i import generate_image

    class UiBridge:
        def supports_capability(self, capability):
            return capability == "ui_text_to_image"

        async def ui_generate_images(self, **kwargs):
            assert kwargs["prompt"] == "Japanese factory"
            assert kwargs["aspect"] == "landscape"
            assert kwargs["model"] == "harbor_seal"
            assert kwargs["count"] == 1
            return {
                "status": 200,
                "_client_id": "edge-ui",
                "result": {
                    "projectId": "project-123",
                    "projectUrl": "https://labs.google/fx/tools/flow/project/project-123",
                    "images": [{
                        "tileId": "tile-1",
                        "imageUrl": "https://storage.googleapis.com/generated.png",
                        "localPath": "C:/Downloads/Image_0001.png",
                        "model": "harbor_seal",
                    }],
                    "modelUsed": "harbor_seal",
                    "exhaustedModels": [],
                },
            }

        async def api_request(self, *_args, **_kwargs):
            raise AssertionError("private API path must not be used")

    results = asyncio.run(
        generate_image(
            UiBridge(),
            prompt="Japanese factory",
            aspect="landscape",
            project_id="ignored-fixed-id",
            count=4,
            model="gem_pix_2",
        )
    )

    assert results[0]["media_id"] == "tile-1"
    assert results[0]["project_id"] == "project-123"
    assert results[0]["local_path"].endswith("Image_0001.png")
    assert results[0]["_model_key"] == "harbor_seal"
    assert results[0]["_model"] == "HARBOR_SEAL"


def test_text_to_image_reports_confirmed_submission_without_an_image():
    from flow_engine.generators.t2i import generate_image

    class SubmittedWithoutImageBridge:
        def supports_capability(self, capability):
            return capability == "ui_text_to_image"

        async def ui_generate_images(self, **_kwargs):
            return {
                "status": 200,
                "result": {
                    "images": [],
                    "promptOutcomes": [
                        {
                            "promptIndex": 1,
                            "submitted": True,
                            "status": "submitted-no-image",
                            "error": "POLICY_REJECTED",
                        }
                    ],
                },
            }

    with pytest.raises(ValueError, match="FLOW_PROMPT_SUBMITTED_NO_IMAGE"):
        asyncio.run(
            generate_image(
                SubmittedWithoutImageBridge(),
                prompt="blocked prompt",
                aspect="landscape",
                project_id="project",
                count=1,
                model="gem_pix_2",
            )
        )


def test_extension_disconnects_when_last_edge_window_closes():
    project_root = Path(__file__).resolve().parents[1]
    background = (project_root / "flow-extension" / "background.js").read_text(
        encoding="utf-8"
    )

    assert "chrome.windows.onRemoved.addListener" in background
    assert "EDGE_WINDOWS_CLOSED" in background
    assert "chrome.windows.onCreated.addListener" in background
    assert "type: 'browser_state'" in background


def test_bridge_rejects_background_worker_after_edge_window_closes(monkeypatch):
    from flow_engine import bridge as bridge_module
    from flow_engine.bridge import ExtensionBridge

    monkeypatch.setattr(bridge_module, "_has_visible_edge_window", lambda: True)

    async def exercise() -> None:
        bridge = ExtensionBridge()
        websocket = object()
        client_id = await bridge._handle_message_with_client(
            {
                "type": "extension_ready",
                "clientId": "edge-client",
                "flowKeyPresent": True,
                "browserOpen": True,
                "normalWindowCount": 1,
                "flowTabCount": 1,
                "capabilities": ["ui_text_to_image"],
            },
            websocket,
            "temporary-client",
        )
        await bridge._handle_message_with_client(
            {"type": "token_captured", "flowKey": "secret"},
            websocket,
            client_id,
        )

        assert bridge.active_client_ids() == ["edge-client"]
        assert bridge.supports_capability("ui_text_to_image") is True
        assert await bridge.health_check() is True

        await bridge._handle_message_with_client(
            {"type": "browser_state", "browserOpen": False, "flowTabCount": 0},
            websocket,
            client_id,
        )

        assert bridge.active_client_ids() == []
        assert bridge._select_client() is None
        assert bridge._flow_key is None
        assert await bridge.health_check() is False

    asyncio.run(exercise())


def test_bridge_live_configuration_inspection_never_sends_a_prompt(monkeypatch):
    from flow_engine import bridge as bridge_module
    from flow_engine.bridge import ExtensionBridge

    monkeypatch.setattr(bridge_module, "_has_visible_edge_window", lambda: True)

    async def exercise() -> None:
        bridge = ExtensionBridge()
        bridge._loop = asyncio.get_running_loop()
        bridge._clients["edge-client"] = object()
        bridge._browser_open["edge-client"] = True
        bridge._last_seen["edge-client"] = bridge_module._time.monotonic()
        bridge._capabilities["edge-client"] = {"ui_text_to_image"}
        bridge._states["edge-client"] = "idle"
        sent = []

        async def send(client_id, message):
            sent.append((client_id, message))
            bridge._route_response(message["id"], {
                "id": message["id"],
                "status": 200,
                "result": {
                    "verified": True,
                    "model": "harbor_seal",
                    "modelName": "Nano Banana 2 Lite",
                    "imageCount": 1,
                    "promptTouched": False,
                    "promptSubmitted": False,
                },
            })
            return True

        bridge.send_message_to = send
        result = await bridge.ui_inspect_configuration(client_id="edge-client")

        assert result["status"] == 200
        assert result["result"]["imageCount"] == 1
        assert result["result"]["promptSubmitted"] is False
        assert sent[0][1]["method"] == "ui_inspect_configuration"
        assert "prompt" not in sent[0][1]["params"]
        assert bridge._states["edge-client"] == "idle"

    asyncio.run(exercise())


def test_bridge_serializes_ui_jobs_per_flow_client(monkeypatch):
    from flow_engine import bridge as bridge_module
    from flow_engine.bridge import ExtensionBridge

    monkeypatch.setattr(bridge_module, "_has_visible_edge_window", lambda: True)

    async def exercise() -> None:
        bridge = ExtensionBridge()
        bridge._loop = asyncio.get_running_loop()
        bridge._clients["edge-client"] = object()
        bridge._browser_open["edge-client"] = True
        bridge._last_seen["edge-client"] = bridge_module._time.monotonic()
        bridge._capabilities["edge-client"] = {"ui_text_to_image"}
        bridge._states["edge-client"] = "idle"
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        sent_prompts = []

        async def send(_client_id, message):
            if message.get("method") != "ui_generate_images":
                return True
            prompt = message["params"]["prompt"]
            sent_prompts.append(prompt)
            if prompt == "first":
                first_started.set()
                await release_first.wait()
            bridge._route_response(message["id"], {
                "id": message["id"],
                "status": 200,
                "result": {"images": []},
            })
            return True

        bridge.send_message_to = send
        first = asyncio.create_task(
            bridge._ui_generate_images_on_client(
                client_id="edge-client",
                prompt="first",
                aspect="landscape",
                count=1,
                model="harbor_seal",
                timeout=2.0,
                download_folder="",
            )
        )
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        second = asyncio.create_task(
            bridge._ui_generate_images_on_client(
                client_id="edge-client",
                prompt="second",
                aspect="landscape",
                count=1,
                model="harbor_seal",
                timeout=2.0,
                download_folder="",
            )
        )
        await asyncio.sleep(0)
        assert sent_prompts == ["first"]
        assert bridge.ui_queue_snapshot()["edge-client"]["waiting"] == 1

        release_first.set()
        await asyncio.gather(first, second)
        assert sent_prompts == ["first", "second"]
        assert bridge.ui_queue_snapshot()["edge-client"] == {
            "active": "",
            "waiting": 0,
        }
        assert bridge._states["edge-client"] == "idle"

    asyncio.run(exercise())


def test_bridge_timeout_waits_for_cancelled_job_outcome(monkeypatch):
    from flow_engine import bridge as bridge_module
    from flow_engine.bridge import ExtensionBridge

    monkeypatch.setattr(bridge_module, "_has_visible_edge_window", lambda: True)

    async def exercise() -> None:
        bridge = ExtensionBridge()
        bridge._loop = asyncio.get_running_loop()
        bridge._clients["edge-client"] = object()
        bridge._browser_open["edge-client"] = True
        bridge._last_seen["edge-client"] = bridge_module._time.monotonic()
        bridge._capabilities["edge-client"] = {"ui_text_to_image"}
        bridge._states["edge-client"] = "idle"
        job_id = ""
        sent = []

        async def send(_client_id, message):
            nonlocal job_id
            sent.append(message)
            if message.get("method") == "ui_generate_images":
                job_id = message["params"]["jobId"]
            elif message.get("method") == "cancel_ui_job":
                bridge._route_response(
                    job_id,
                    {
                        "id": job_id,
                        "status": 200,
                        "result": {
                            "images": [],
                            "promptOutcomes": [
                                {
                                    "promptIndex": 1,
                                    "submitted": True,
                                    "status": "submitted-no-image",
                                    "error": "CANCELLED_AFTER_WATCHDOG",
                                }
                            ],
                        },
                    },
                )
            return True

        bridge.send_message_to = send
        response = await bridge.ui_generate_images(
            prompt="demo",
            aspect="landscape",
            count=1,
            model="gem_pix_2",
            timeout=0.001,
        )

        assert response["status"] == 200
        assert response["result"]["promptOutcomes"][0]["submitted"] is True
        assert any(item.get("method") == "cancel_ui_job" for item in sent)
        assert bridge._states["edge-client"] == "idle"

    asyncio.run(exercise())


def test_bridge_hard_timeout_preserves_submitted_no_resend_state(monkeypatch):
    from flow_engine import bridge as bridge_module
    from flow_engine.bridge import ExtensionBridge

    monkeypatch.setattr(bridge_module, "_has_visible_edge_window", lambda: True)

    async def exercise() -> None:
        bridge = ExtensionBridge()
        bridge.UI_CANCEL_GRACE = 0.001
        bridge._loop = asyncio.get_running_loop()
        bridge._clients["edge-client"] = object()
        bridge._browser_open["edge-client"] = True
        bridge._last_seen["edge-client"] = bridge_module._time.monotonic()
        bridge._capabilities["edge-client"] = {"ui_text_to_image"}
        bridge._states["edge-client"] = "idle"

        async def send(_client_id, message):
            if message.get("method") == "ui_generate_images":
                await bridge._handle_message_with_client(
                    {
                        "type": "ui_progress",
                        "requestId": message["id"],
                        "jobId": message["id"],
                        "phase": "submitted",
                        "progress": 0.2,
                    },
                    object(),
                    "edge-client",
                )
            return True

        bridge.send_message_to = send
        response = await bridge.ui_generate_images(
            prompt="demo",
            aspect="landscape",
            count=1,
            model="gem_pix_2",
            timeout=0.001,
        )

        assert response["status"] == 408
        assert response["error"].startswith("FLOW_PROMPT_SUBMITTED_NO_IMAGE")
        assert bridge._states["edge-client"] == "idle"

    asyncio.run(exercise())


def test_bridge_timeout_in_configure_phase_is_unsent(monkeypatch):
    from flow_engine import bridge as bridge_module
    from flow_engine.bridge import ExtensionBridge

    monkeypatch.setattr(bridge_module, "_has_visible_edge_window", lambda: True)

    async def exercise() -> None:
        bridge = ExtensionBridge()
        bridge.UI_CANCEL_GRACE = 0.001
        bridge._loop = asyncio.get_running_loop()
        bridge._clients["edge-client"] = object()
        bridge._browser_open["edge-client"] = True
        bridge._last_seen["edge-client"] = bridge_module._time.monotonic()
        bridge._capabilities["edge-client"] = {"ui_text_to_image"}
        bridge._states["edge-client"] = "idle"

        async def send(_client_id, message):
            if message.get("method") == "ui_generate_images":
                await bridge._handle_message_with_client(
                    {
                        "type": "ui_progress",
                        "requestId": message["id"],
                        "jobId": message["params"]["jobId"],
                        "phase": "configure",
                        "progress": 0.1,
                    },
                    object(),
                    "edge-client",
                )
            return True

        bridge.send_message_to = send
        response = await bridge.ui_generate_images(
            prompt="demo",
            aspect="landscape",
            count=1,
            model="harbor_seal",
            timeout=0.001,
        )

        assert response["status"] == 408
        assert response["error"].startswith("FLOW_UI_TIMEOUT_UNSENT")

    asyncio.run(exercise())


def test_bridge_fails_over_to_another_ui_profile_only_on_explicit_quota(monkeypatch):
    from flow_engine import bridge as bridge_module
    from flow_engine.bridge import ExtensionBridge

    monkeypatch.setattr(bridge_module, "_has_visible_edge_window", lambda: True)
    monkeypatch.setattr(bridge_module.random, "choice", lambda values: values[0])

    async def exercise() -> None:
        bridge = ExtensionBridge()
        bridge._loop = asyncio.get_running_loop()
        for client_id in ("profile-a", "profile-b"):
            bridge._clients[client_id] = object()
            bridge._browser_open[client_id] = True
            bridge._last_seen[client_id] = bridge_module._time.monotonic()
            bridge._capabilities[client_id] = {"ui_text_to_image"}
            bridge._states[client_id] = "idle"
        routed: list[str] = []

        async def send(client_id, message):
            if message.get("method") != "ui_generate_images":
                return True
            routed.append(client_id)
            request_id = message["id"]
            if client_id == "profile-a":
                response = {
                    "id": request_id,
                    "status": 400,
                    "error": "ALL_IMAGE_MODELS_LIMITED",
                }
            else:
                response = {
                    "id": request_id,
                    "status": 200,
                    "result": {"images": [{"imageUrl": "https://example/image.png"}]},
                }
            bridge._route_response(request_id, response)
            return True

        bridge.send_message_to = send
        response = await bridge.ui_generate_images(
            prompt="demo",
            aspect="landscape",
            count=1,
            model="gem_pix_2",
        )

        assert routed == ["profile-a", "profile-b"]
        assert response["status"] == 200
        assert response["_client_id"] == "profile-b"
        assert bridge._ui_quota_blocked_until["profile-a"] > bridge_module._time.monotonic()

    asyncio.run(exercise())


def test_bridge_reconnect_without_flow_tab_never_opens_a_new_tab(monkeypatch):
    from flow_engine import bridge as bridge_module
    from flow_engine.bridge import ExtensionBridge

    monkeypatch.setattr(bridge_module, "_has_visible_edge_window", lambda: True)

    async def exercise() -> None:
        bridge = ExtensionBridge()
        requested = []

        async def request(client_id, *, existing_only=False):
            requested.append((client_id, existing_only))

        bridge._request_flow_tab_for = request
        await bridge._handle_message_with_client(
            {
                "type": "extension_ready",
                "clientId": "edge-client",
                "flowKeyPresent": False,
                "browserOpen": True,
                "normalWindowCount": 1,
                "flowTabCount": 0,
                "capabilities": ["ui_text_to_image"],
            },
            object(),
            "temporary-client",
        )
        await asyncio.sleep(0)

        assert requested == []

    asyncio.run(exercise())


@pytest.mark.parametrize("ui_capable", [True, False])
def test_bridge_reconnect_refreshes_existing_tab_only_for_legacy_token_client(
    monkeypatch, ui_capable
):
    from flow_engine import bridge as bridge_module
    from flow_engine.bridge import ExtensionBridge

    monkeypatch.setattr(bridge_module, "_has_visible_edge_window", lambda: True)

    async def exercise() -> None:
        bridge = ExtensionBridge()
        requested = []

        async def request(client_id, *, existing_only=False):
            requested.append((client_id, existing_only))

        bridge._request_flow_tab_for = request
        await bridge._handle_message_with_client(
            {
                "type": "extension_ready",
                "clientId": "edge-client",
                "flowKeyPresent": False,
                "browserOpen": True,
                "normalWindowCount": 1,
                "flowTabCount": 1,
                "capabilities": ["ui_text_to_image"] if ui_capable else [],
            },
            object(),
            "temporary-client",
        )
        await asyncio.sleep(0)

        assert requested == ([] if ui_capable else [("edge-client", True)])

    asyncio.run(exercise())


def test_bridge_rejects_legacy_extension_socket_when_edge_has_no_visible_window(
    monkeypatch,
):
    from flow_engine import bridge as bridge_module
    from flow_engine.bridge import ExtensionBridge

    bridge = ExtensionBridge()
    bridge._clients["legacy-client"] = object()
    bridge._tokens["legacy-client"] = "cached-token"
    bridge._browser_open["legacy-client"] = True
    monkeypatch.setattr(bridge_module, "_has_visible_edge_window", lambda: False)

    assert bridge.active_client_ids() == []
    assert bridge._select_client() is None
    assert bridge._flow_key is None


def test_lightweight_flow_monitor_reports_closed_edge_without_credit_request(
    monkeypatch,
):
    manager = FlowAgentManager()
    manager._environment_cache = (
        flow_agent.time.monotonic(),
        Path("edge.exe"),
        Path("extension"),
        True,
    )
    requests = []

    def fake_request(url, **_kwargs):
        requests.append(url)
        return {
            "extension_connected": False,
            "has_flow_key": False,
            "browser_open": False,
        }

    monkeypatch.setattr(flow_agent, "_json_request", fake_request)
    status = manager.check(include_credits=False, refresh_environment=False)

    assert status.backend_running is True
    assert status.extension_connected is False
    assert status.ready is False
    assert "Edge đã đóng" in status.detail
    assert requests == [f"{manager.api_base}/health"]


def test_passive_flow_health_checks_coalesce_concurrent_requests(monkeypatch):
    manager = FlowAgentManager("http://127.0.0.1:18999")
    manager._environment_cache = (
        flow_agent.time.monotonic(),
        Path("edge.exe"),
        Path("extension"),
        True,
    )
    request_started = threading.Event()
    release_request = threading.Event()
    requests = []

    def fake_request(url, **_kwargs):
        requests.append(url)
        request_started.set()
        assert release_request.wait(2.0)
        return {
            "extension_connected": True,
            "has_flow_key": False,
            "browser_open": True,
            "flow_tab_count": 1,
            "capabilities": ["ui_text_to_image"],
        }

    monkeypatch.setattr(flow_agent, "_json_request", fake_request)
    barrier = threading.Barrier(6)
    statuses = []
    errors = []

    def run_check():
        try:
            barrier.wait(2.0)
            statuses.append(
                manager.check(
                    include_credits=False,
                    refresh_environment=False,
                    passive=True,
                )
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run_check) for _ in range(6)]
    for thread in threads:
        thread.start()
    assert request_started.wait(2.0)
    release_request.set()
    for thread in threads:
        thread.join(2.0)

    assert errors == []
    assert len(statuses) == 6
    assert requests == [f"{manager.api_base}/health"]


def test_workspace_image_batches_run_fifo() -> None:
    coordinator = FlowGenerationCoordinator()
    release_first = threading.Event()
    first_started = threading.Event()
    order = []

    def first_operation():
        order.append("first-start")
        first_started.set()
        assert release_first.wait(2.0)
        order.append("first-end")
        return "first"

    def second_operation():
        order.append("second-start")
        return "second"

    results = []
    first = threading.Thread(
        target=lambda: results.append(coordinator.execute("workspace-1", first_operation))
    )
    second = threading.Thread(
        target=lambda: results.append(coordinator.execute("workspace-2", second_operation))
    )
    first.start()
    assert first_started.wait(1.0)
    second.start()
    release_first.set()
    first.join(2.0)
    second.join(2.0)
    assert order == ["first-start", "first-end", "second-start"]
    assert set(results) == {"first", "second"}


def test_fifo_batches_keep_late_images_inside_their_own_workspace(
    tmp_path, monkeypatch
) -> None:
    coordinator = FlowGenerationCoordinator()
    manager = FlowAgentManager()
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(
            Path("edge.exe"), Path("extension"), True, True, True, True
        ),
    )

    def fake_request(_url, **kwargs):
        prompt = kwargs["payload"]["prompt"]
        return {
            "data": [
                {
                    "url": f"http://example/{prompt}.png",
                    "media_id": f"media-{prompt}",
                }
            ]
        }

    def fake_download(url, destination, timeout=120.0):
        color = "navy" if "workspace-a" in url else "gold"
        Image.new("RGB", (64, 36), color).save(destination)
        return destination

    monkeypatch.setattr(flow_agent, "_json_request", fake_request)
    monkeypatch.setattr(manager, "_download", fake_download)
    media_a = tmp_path / "workspace-a" / "Media"
    media_b = tmp_path / "workspace-b" / "Media"

    result_a = coordinator.execute(
        "workspace-a",
        lambda: manager.generate_images(
            ["workspace-a-1", "workspace-a-2"],
            media_a,
            inter_prompt_delay=(0.0, 0.0),
        ),
    )
    result_b = coordinator.execute(
        "workspace-b",
        lambda: manager.generate_images(
            ["workspace-b-1"],
            media_b,
            inter_prompt_delay=(0.0, 0.0),
        ),
    )

    assert {item.path.parent for item in result_a} == {media_a.resolve()}
    assert {item.path.parent for item in result_b} == {media_b.resolve()}
    assert [item.media_id for item in result_a] == [
        "media-workspace-a-1",
        "media-workspace-a-2",
    ]
    assert [item.media_id for item in result_b] == ["media-workspace-b-1"]
    assert not (media_a / "Image_0003.png").exists()
    assert not (media_b / "Image_0002.png").exists()


def test_flow_workspace_persists_project_and_client_affinity(
    tmp_path, monkeypatch
) -> None:
    manager = FlowAgentManager()
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(
            edge_path=Path("edge.exe"),
            extension_dir=Path("extension"),
            extension_installed=True,
            backend_running=True,
            extension_connected=True,
            has_flow_key=False,
            browser_open=True,
            flow_tab_count=1,
            ui_image_capable=True,
        ),
    )
    requests = []

    def fake_request(_url, **kwargs):
        requests.append(kwargs["payload"])
        return {
            "projectId": "flow-project-a",
            "clientId": "edge-client-a",
            "data": [
                {
                    "url": "http://example/workspace-a.png",
                    "media_id": "media-a",
                    "project_id": "flow-project-a",
                    "client_id": "edge-client-a",
                }
            ],
        }

    def fake_download(_url, destination, timeout=120.0):
        Image.new("RGB", (64, 36), "navy").save(destination)
        return destination

    monkeypatch.setattr(flow_agent, "_json_request", fake_request)
    monkeypatch.setattr(manager, "_download", fake_download)
    checkpoint_path = tmp_path / "flow_history.json"
    media_dir = tmp_path / "Media"

    result = manager.generate_images(
        ["workspace-a-prompt"],
        media_dir,
        checkpoint_path=checkpoint_path,
        workspace_id="workspace-a",
    )

    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert result[0].media_id == "media-a"
    assert requests[0]["workspace_id"] == "workspace-a"
    assert requests[0]["new_flow_project"] is True
    assert checkpoint["workspace_id"] == "workspace-a"
    assert checkpoint["flow_project_id"] == "flow-project-a"
    assert checkpoint["flow_client_id"] == "edge-client-a"

    # A restart/resume restores the local artifact and does not dispatch a
    # second request into another Flow project.
    repeated = manager.generate_images(
        ["workspace-a-prompt"],
        media_dir,
        checkpoint_path=checkpoint_path,
        workspace_id="workspace-a",
    )
    assert repeated[0].path == result[0].path
    assert len(requests) == 1


def test_legacy_workspace_without_project_id_fails_closed(
    tmp_path, monkeypatch
) -> None:
    manager = FlowAgentManager()
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(
            Path("edge.exe"),
            Path("extension"),
            True,
            True,
            True,
            False,
            browser_open=True,
            flow_tab_count=1,
            ui_image_capable=True,
        ),
    )
    dispatched = []
    monkeypatch.setattr(
        flow_agent,
        "_json_request",
        lambda *args, **kwargs: dispatched.append((args, kwargs)),
    )
    checkpoint = tmp_path / "flow_history.json"
    signature = hashlib.sha256(
        json.dumps(
            {
                "model": "harbor_seal",
                "aspect": "landscape",
                "prompts": ["one", "two"],
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    checkpoint.write_text(
        json.dumps(
            {
                "version": 2,
                "signature": signature,
                "run_id": "legacy-run",
                "model": "harbor_seal",
                "aspect": "landscape",
                "total": 2,
                "prompts": ["one", "two"],
                "entries": {
                    "1": {
                        "prompt_sha256": hashlib.sha256(b"one").hexdigest(),
                        "filename": "Image_0001.png",
                    }
                },
                "failures": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        flow_agent.FlowAgentSubmissionUncertain,
        match="FLOW_WORKSPACE_PROJECT_ID_REQUIRED",
    ):
        manager.generate_images(
            ["one", "two"],
            tmp_path / "Media",
            checkpoint_path=checkpoint,
            workspace_id="legacy-workspace",
        )
    assert dispatched == []


def test_legacy_workspace_rebinds_from_exact_visible_history(
    tmp_path, monkeypatch
) -> None:
    manager = FlowAgentManager()
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(
            Path("edge.exe"),
            Path("extension"),
            True,
            True,
            True,
            False,
            browser_open=True,
            flow_tab_count=1,
            ui_image_capable=True,
        ),
    )
    output = tmp_path / "Media"
    output.mkdir()
    Image.new("RGB", (64, 36), "navy").save(output / "Image_0001.png")
    prompts = ["already generated", "continue this"]
    signature = hashlib.sha256(
        json.dumps(
            {
                "model": "harbor_seal",
                "aspect": "landscape",
                "prompts": prompts,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    checkpoint = tmp_path / "flow_history.json"
    checkpoint.write_text(
        json.dumps({
            "version": 2,
            "signature": signature,
            "run_id": "legacy-run",
            "entries": {
                "1": {
                    "prompt_sha256": hashlib.sha256(
                        prompts[0].encode("utf-8")
                    ).hexdigest(),
                    "filename": "Image_0001.png",
                },
            },
            "failures": {},
        }),
        encoding="utf-8",
    )
    snapshot_calls = []
    monkeypatch.setattr(
        manager,
        "inspect_project_snapshot",
        lambda prompts, **kwargs: (
            snapshot_calls.append((list(prompts), kwargs))
            or {
                "projectId": "legacy-flow-project",
                "clientId": "legacy-edge-client",
                "matches": [{"promptIndex": 1, "matched": True}],
            }
        ),
    )
    generation_calls = []

    def fake_request(_url, **kwargs):
        generation_calls.append(kwargs["payload"])
        return {
            "data": [{
                "url": "http://example/new.png",
                "media_id": "new-media",
            }],
        }

    def fake_download(_url, destination, timeout=120.0):
        Image.new("RGB", (64, 36), "navy").save(destination)
        return destination

    monkeypatch.setattr(flow_agent, "_json_request", fake_request)
    monkeypatch.setattr(manager, "_download", fake_download)

    result = manager.generate_images(
        prompts,
        output,
        checkpoint_path=checkpoint,
        workspace_id="legacy-workspace",
        inter_prompt_delay=(0, 0),
    )

    assert [item.prompt_index for item in result] == [1, 2]
    assert snapshot_calls and snapshot_calls[0][0] == [prompts[0]]
    assert generation_calls[0]["flow_project_id"] == "legacy-flow-project"
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["flow_project_id"] == "legacy-flow-project"
    assert saved["flow_client_id"] == "legacy-edge-client"


def test_generate_images_requires_ready_extension(tmp_path, monkeypatch):
    manager = FlowAgentManager()
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(None, None, False, False, False, False, detail="not ready"),
    )
    with pytest.raises(flow_agent.FlowAgentError, match="not ready"):
        manager.generate_images(["prompt"], tmp_path)


def test_generate_images_honours_cancel_after_current_download(tmp_path, monkeypatch):
    manager = FlowAgentManager()
    cancel = threading.Event()
    monkeypatch.setattr(
        manager,
        "check",
        lambda: FlowAgentStatus(
            Path("edge.exe"), Path("extension"), True, True, True, True
        ),
    )
    monkeypatch.setattr(
        flow_agent,
        "_json_request",
        lambda *_args, **_kwargs: {"data": [{"url": "http://example/image.png"}]},
    )

    def fake_download(_url, destination, timeout=120.0):
        destination.write_bytes(b"partial")
        cancel.set()
        return destination

    monkeypatch.setattr(manager, "_download", fake_download)
    with pytest.raises(flow_agent.FlowAgentCancelled):
        manager.generate_images(["one", "two"], tmp_path, cancel_event=cancel)
    assert not (tmp_path / "Image_0001.png").exists()


def test_pinned_ui_client_never_falls_through_to_another_profile(monkeypatch):
    from flow_engine import bridge as module
    bridge = module.ExtensionBridge()
    monkeypatch.setattr(module, "_has_visible_edge_window", lambda: True)
    for client in ("test-profile", "other-profile"):
        bridge._clients[client] = object()
        bridge._capabilities[client] = {"ui_text_to_image"}
    token = module.target_client_id_var.set("test-profile")
    try:
        assert bridge._select_capable_client("ui_text_to_image") == "test-profile"
        assert bridge._select_capable_client("ui_text_to_image", exclude_ids={"test-profile"}) is None
    finally:
        module.target_client_id_var.reset(token)


def test_timeout_without_submission_evidence_is_not_reported_as_unsent(monkeypatch):
    from flow_engine.bridge import ExtensionBridge
    async def exercise():
        bridge = ExtensionBridge()
        bridge._loop = asyncio.get_running_loop()
        bridge.UI_CANCEL_GRACE = .001
        bridge._states['test-profile'] = 'idle'
        async def send(*_):
            return True
        bridge.send_message_to = send
        result = await bridge._ui_generate_images_on_client(
            client_id='test-profile', prompt='one', aspect='landscape', count=1,
            model='harbor_seal', timeout=.001, download_folder='')
        assert 'FLOW_UI_SUBMISSION_UNCERTAIN' in result['error']
        assert not bridge._pending
    asyncio.run(exercise())


def test_flow_javascript_runtime_regressions():
    import shutil
    import subprocess
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for the extension runtime regression tests')
    result = subprocess.run([node, '--test', str(Path(__file__).with_name('flow_driver.test.cjs'))],
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('detail', [
    'FLOW_UI_LOGIN_REQUIRED',
    'Cannot access contents of url "https://accounts.google.com/v3/signin/confirmidentifier". '
    'Extension manifest must request permission to access this host.',
])
def test_flow_signin_redirect_is_not_misreported_as_missing_host_permission(detail):
    error = flow_agent._friendly_generation_error(RuntimeError(detail))
    assert isinstance(error, flow_agent.FlowAgentUiError)
    assert 'FLOW_UI_LOGIN_REQUIRED' in str(error)
    assert 'Prompt chưa được gửi' in str(error)
    assert 'không cần cấp quyền' in str(error)
