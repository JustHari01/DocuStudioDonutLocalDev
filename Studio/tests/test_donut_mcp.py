from __future__ import annotations

import json
import multiprocessing
import unittest
from pathlib import Path
import tempfile
import time
from unittest import mock

import docurandom.donut_mcp as donut_mcp
from docurandom.donut_mcp import (
    DOCURANDOM_LOCAL_REQUIRED_TOOLS,
    DonutMcpAuthenticationError,
    DonutMcpClient,
    DonutMcpEntitlementError,
    DonutMcpEndpoint,
    DonutMcpError,
    DonutMcpLaunchUncertain,
    DonutMcpTimeoutError,
    load_donut_mcp_endpoint,
    save_donut_mcp_endpoint,
)


def _racing_profile_launch(data_root, start, launches, outcomes) -> None:
    endpoint = DonutMcpEndpoint.parse(
        "http://127.0.0.1:51091/mcp/abcdefghijklmnopqrstuv"
    )
    with mock.patch.object(donut_mcp, "app_data_dir", return_value=Path(data_root)):
        client = DonutMcpClient(endpoint, shared_session=False)
        client._has_live_session = lambda: True  # type: ignore[method-assign]
        client.get_profile = lambda profile_id: donut_mcp.DonutProfile(  # type: ignore[method-assign]
            profile_id, "DaiTa", "wayfern", process_id=None
        )

        def launch(profile_id: str, url: str) -> str:
            launches.put((profile_id, url))
            time.sleep(0.35)
            raise DonutMcpTimeoutError("timed out")

        client._run_profile_in_current_session = launch  # type: ignore[method-assign]
        start.wait(5)
        try:
            client.run_profile("profile-race", "about:blank")
            outcomes.put("unexpected-success")
        except DonutMcpLaunchUncertain:
            outcomes.put("uncertain")
        except Exception as exc:
            outcomes.put(type(exc).__name__)


class DonutMcpEndpointTests(unittest.TestCase):
    def test_accepts_only_loopback_streamable_http_endpoint(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
        )

        self.assertEqual(endpoint.origin, "http://127.0.0.1:51080")
        self.assertNotIn("abcdefghijklmnopqrstuv", endpoint.safe_label)
        self.assertIn("stuv", endpoint.safe_label)

    def test_rejects_remote_or_short_token_endpoint(self) -> None:
        with self.assertRaises(DonutMcpError):
            DonutMcpEndpoint.parse("https://example.com/mcp/abcdefghijklmnopqrstuv")
        with self.assertRaises(DonutMcpError):
            DonutMcpEndpoint.parse("http://127.0.0.1:51080/mcp/short")

    def test_windows_secret_store_round_trip_is_not_plaintext(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutSecret_") as temp:
            path = Path(temp) / "endpoint.dat"
            token = "abcdefghijklmnopqrstuv"
            save_donut_mcp_endpoint(
                f"http://127.0.0.1:51080/mcp/{token}", path
            )

            restored = load_donut_mcp_endpoint(path)

            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.origin, "http://127.0.0.1:51080")
            self.assertNotIn(token.encode("utf-8"), path.read_bytes())

    def test_debug_connection_requires_a_loopback_target_list(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
        )
        client = DonutMcpClient(endpoint)
        client.call_tool_json = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "targets_url": "https://remote.example/json"
        }

        with self.assertRaisesRegex(DonutMcpError, "không nằm trên máy này"):
            client.get_debug_connection("profile-test")

    def test_local_runtime_validation_accepts_only_docurandom_server(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
        )
        client = DonutMcpClient(endpoint)
        client.server_info = {
            "serverInfo": {"name": "donut-browser-local", "version": "0.30.0"}
        }
        client.list_tool_names = lambda: DOCURANDOM_LOCAL_REQUIRED_TOOLS  # type: ignore[method-assign]

        result = client.require_docurandom_local()

        self.assertEqual(result["serverInfo"]["name"], "donut-browser-local")

    def test_local_runtime_validation_rejects_official_donut(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
        )
        client = DonutMcpClient(endpoint)
        client.server_info = {
            "serverInfo": {"name": "donut-browser", "version": "0.30.0"}
        }

        with self.assertRaisesRegex(DonutMcpError, "Donut chính thức"):
            client.require_docurandom_local()

    def test_local_runtime_validation_rejects_missing_tools(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
        )
        client = DonutMcpClient(endpoint)
        client.server_info = {
            "serverInfo": {"name": "donut-browser-local", "version": "0.30.0"}
        }
        client.list_tool_names = lambda: frozenset({"list_profiles"})  # type: ignore[method-assign]

        with self.assertRaisesRegex(DonutMcpError, "thiếu công cụ"):
            client.require_docurandom_local()

    def test_structured_extension_tools_may_return_json_directly(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51080/mcp/abcdefghijklmnopqrstuv"
        )
        client = DonutMcpClient(endpoint)
        client.session_id = "test-session"
        client._request = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "result": [{"id": "extension-test", "name": "Bridge"}]
        }

        result = client.call_tool_json("list_extensions")

        self.assertEqual(result[0]["id"], "extension-test")

    def test_clients_share_one_initialize_session_and_tool_catalog(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51081/mcp/abcdefghijklmnopqrstuv"
        )
        calls: list[str] = []

        class Response:
            def __init__(self, payload: dict[str, object], session_id: str = "") -> None:
                self.headers = {"Content-Type": "application/json"}
                if session_id:
                    self.headers["Mcp-Session-Id"] = session_id
                self._body = json.dumps(payload).encode("utf-8")

            def read(self) -> bytes:
                return self._body

            def close(self) -> None:
                return None

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        def urlopen(request: object, **_kwargs: object) -> Response:
            data = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
            method = str(data.get("method") or "DELETE")
            calls.append(method)
            if method == "initialize":
                return Response(
                    {
                        "jsonrpc": "2.0",
                        "id": data.get("id"),
                        "result": {
                            "serverInfo": {
                                "name": "donut-browser-local",
                                "version": "0.30.0",
                            }
                        },
                    },
                    session_id="shared-session",
                )
            if method == "tools/list":
                return Response(
                    {
                        "jsonrpc": "2.0",
                        "id": data.get("id"),
                        "result": {"tools": [{"name": "list_profiles"}]},
                    }
                )
            return Response({})

        try:
            with mock.patch.object(donut_mcp.urllib.request, "urlopen", side_effect=urlopen):
                first = DonutMcpClient(endpoint)
                second = DonutMcpClient(endpoint)
                with first:
                    first.connect()
                    self.assertEqual(first.list_tool_names(), frozenset({"list_profiles"}))
                with second:
                    second.connect()
                    self.assertEqual(second.list_tool_names(), frozenset({"list_profiles"}))
                self.assertEqual(calls.count("initialize"), 1)
                self.assertEqual(calls.count("notifications/initialized"), 1)
                self.assertEqual(calls.count("tools/list"), 1)
        finally:
            donut_mcp.close_shared_donut_mcp_sessions()

    def test_long_run_profile_uses_an_isolated_session(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51083/mcp/abcdefghijklmnopqrstuv"
        )
        calls: list[tuple[str, str]] = []
        initialize_count = 0

        class Response:
            def __init__(self, payload: dict[str, object], session_id: str = "") -> None:
                self.headers = {"Content-Type": "application/json"}
                if session_id:
                    self.headers["Mcp-Session-Id"] = session_id
                self._body = json.dumps(payload).encode("utf-8")

            def read(self) -> bytes:
                return self._body

            def close(self) -> None:
                return None

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        def urlopen(request: object, **_kwargs: object) -> Response:
            nonlocal initialize_count
            method = request.get_method()  # type: ignore[attr-defined]
            session = str(
                request.headers.get("Mcp-session-id", "")  # type: ignore[attr-defined]
            )
            if method == "DELETE":
                calls.append((method, session))
                return Response({})
            data = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
            rpc_method = str(data.get("method") or "")
            calls.append((rpc_method, session))
            if rpc_method == "initialize":
                initialize_count += 1
                return Response(
                    {
                        "jsonrpc": "2.0",
                        "id": data.get("id"),
                        "result": {
                            "serverInfo": {
                                "name": "donut-browser-local",
                                "version": "0.30.0",
                            }
                        },
                    },
                    session_id=f"session-{initialize_count}",
                )
            if rpc_method == "tools/call":
                return Response(
                    {
                        "jsonrpc": "2.0",
                        "id": data.get("id"),
                        "result": {"content": [{"type": "text", "text": "started"}]},
                    }
                )
            return Response({})

        try:
            with tempfile.TemporaryDirectory(prefix="DocuRandomDonutLaunch_") as temp:
                with (
                    mock.patch.object(donut_mcp, "app_data_dir", return_value=Path(temp)),
                    mock.patch.object(donut_mcp.urllib.request, "urlopen", side_effect=urlopen),
                ):
                    with DonutMcpClient(endpoint) as shared:
                        self.assertEqual(shared.run_profile("profile-1", "about:blank"), "started")

                    tool_sessions = [session for method, session in calls if method == "tools/call"]
                    self.assertEqual(len(tool_sessions), 1)
                    self.assertEqual(tool_sessions[0], "session-2")
                    self.assertEqual(initialize_count, 2)
                    state = donut_mcp._get_profile_launch_state(endpoint, "profile-1")
                    self.assertEqual(state["state"] if state else "", "accepted")
        finally:
            donut_mcp.close_shared_donut_mcp_sessions()

    def test_uncertain_profile_launch_survives_client_restart_and_never_relaunches(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51085/mcp/abcdefghijklmnopqrstuv"
        )
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutLaunch_") as temp:
            with mock.patch.object(donut_mcp, "app_data_dir", return_value=Path(temp)):
                launches: list[tuple[str, str]] = []
                first = DonutMcpClient(endpoint, shared_session=False)
                first._has_live_session = lambda: True  # type: ignore[method-assign]

                def timeout_launch(profile_id: str, url: str) -> str:
                    launches.append((profile_id, url))
                    raise DonutMcpTimeoutError("timed out")

                first._run_profile_in_current_session = timeout_launch  # type: ignore[method-assign]
                with self.assertRaises(DonutMcpLaunchUncertain):
                    first.run_profile("profile-uncertain", "https://studio.youtube.com/")
                persisted = donut_mcp._get_profile_launch_state(endpoint, "profile-uncertain")
                self.assertEqual(persisted["state"], "uncertain")

                restarted = DonutMcpClient(endpoint, shared_session=False)
                restarted._has_live_session = lambda: True  # type: ignore[method-assign]
                restarted.get_profile = lambda profile_id: donut_mcp.DonutProfile(  # type: ignore[method-assign]
                    profile_id, "DaiTa", "wayfern", process_id=None
                )
                restarted.get_debug_connection = lambda _profile_id: (_ for _ in ()).throw(  # type: ignore[method-assign]
                    DonutMcpError("debug chưa sẵn sàng")
                )
                restarted._run_profile_in_current_session = timeout_launch  # type: ignore[method-assign]
                with (
                    mock.patch.object(
                        donut_mcp, "_local_donut_restart_proof", return_value=None
                    ),
                    self.assertRaises(DonutMcpLaunchUncertain),
                ):
                    restarted.run_profile("profile-uncertain", "https://studio.youtube.com/")
                self.assertEqual(len(launches), 1)

                # Once read-only status proves the exact profile has a debug
                # endpoint, the marker is cleared and no second launch occurs.
                restarted.get_profile = lambda profile_id: donut_mcp.DonutProfile(  # type: ignore[method-assign]
                    profile_id, "DaiTa", "wayfern", process_id=1234
                )
                restarted.get_debug_connection = lambda _profile_id: {  # type: ignore[method-assign]
                    "targets_url": "http://127.0.0.1:9222/json/list"
                }
                with (
                    mock.patch.object(donut_mcp, "_profile_process_alive", return_value=True),
                    mock.patch.object(donut_mcp, "_debug_target_reachable", return_value=True),
                ):
                    self.assertEqual(
                        restarted.run_profile("profile-uncertain", "https://studio.youtube.com/"),
                        "already-running",
                    )
                self.assertIsNone(
                    donut_mcp._get_profile_launch_state(endpoint, "profile-uncertain")
                )
                self.assertEqual(len(launches), 1)

    def test_uncertain_launch_is_retried_after_verified_donut_restart(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51085/mcp/abcdefghijklmnopqrstuv"
        )
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutLaunch_") as temp:
            with mock.patch.object(donut_mcp, "app_data_dir", return_value=Path(temp)):
                with mock.patch.object(donut_mcp.time, "time", return_value=1_000):
                    donut_mcp._set_profile_launch_state(
                        endpoint, "profile-old", "uncertain"
                    )
                client = DonutMcpClient(endpoint, shared_session=False)
                client._has_live_session = lambda: True  # type: ignore[method-assign]
                client.get_profile = lambda profile_id: donut_mcp.DonutProfile(  # type: ignore[method-assign]
                    profile_id, "DaiTa", "wayfern", process_id=None
                )
                launches: list[tuple[str, str]] = []

                def launch(profile_id: str, url: str) -> str:
                    launches.append((profile_id, url))
                    return "started"

                client._run_profile_in_current_session = launch  # type: ignore[method-assign]
                with mock.patch.object(
                    donut_mcp, "_local_donut_restart_proof", return_value=(2_000, 0)
                ) as proof:
                    self.assertEqual(client.run_profile("profile-old", "about:blank"), "started")
                proof.assert_called_once_with(endpoint, "profile-old")
                self.assertEqual(launches, [("profile-old", "about:blank")])
                self.assertEqual(
                    donut_mcp._get_profile_launch_state(endpoint, "profile-old")["state"],
                    "accepted",
                )

    def test_uncertain_launch_stays_blocked_without_restart_and_no_browser_proof(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51085/mcp/abcdefghijklmnopqrstuv"
        )
        for proof in ((900, 0), (2_000, 1), None):
            with self.subTest(proof=proof):
                with tempfile.TemporaryDirectory(prefix="DocuRandomDonutLaunch_") as temp:
                    with mock.patch.object(
                        donut_mcp, "app_data_dir", return_value=Path(temp)
                    ):
                        with mock.patch.object(donut_mcp.time, "time", return_value=1_000):
                            donut_mcp._set_profile_launch_state(
                                endpoint, "profile-old", "uncertain"
                            )
                        client = DonutMcpClient(endpoint, shared_session=False)
                        client._has_live_session = lambda: True  # type: ignore[method-assign]
                        client.get_profile = lambda profile_id: donut_mcp.DonutProfile(  # type: ignore[method-assign]
                            profile_id, "DaiTa", "wayfern", process_id=None
                        )
                        client._run_profile_in_current_session = lambda *_args: self.fail(  # type: ignore[method-assign]
                            "An unresolved launch must not be sent again"
                        )
                        with (
                            mock.patch.object(
                                donut_mcp, "_local_donut_restart_proof", return_value=proof
                            ),
                            self.assertRaises(DonutMcpLaunchUncertain),
                        ):
                            client.run_profile("profile-old", "about:blank")
                        self.assertEqual(
                            donut_mcp._get_profile_launch_state(
                                endpoint, "profile-old"
                            )["state"],
                            "uncertain",
                        )

    def test_stale_debug_url_does_not_clear_uncertain_launch(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51089/mcp/abcdefghijklmnopqrstuv"
        )
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutLaunch_") as temp:
            with mock.patch.object(donut_mcp, "app_data_dir", return_value=Path(temp)):
                donut_mcp._set_profile_launch_state(endpoint, "profile-stale", "uncertain")
                client = DonutMcpClient(endpoint, shared_session=False)
                client.get_profile = lambda profile_id: donut_mcp.DonutProfile(  # type: ignore[method-assign]
                    profile_id, "DaiTa", "wayfern", process_id=1234
                )
                client.call_tool_json = lambda *_args: {  # type: ignore[method-assign]
                    "targets_url": "http://127.0.0.1:9222/json/list"
                }
                self.assertTrue(client.get_debug_connection("profile-stale"))
                self.assertIsNotNone(donut_mcp._get_profile_launch_state(endpoint, "profile-stale"))
                with (
                    mock.patch.object(donut_mcp, "_profile_process_alive", return_value=True),
                    mock.patch.object(donut_mcp, "_debug_target_reachable", return_value=False),
                ):
                    with self.assertRaises(DonutMcpLaunchUncertain):
                        client.run_profile("profile-stale", "about:blank")
                self.assertIsNotNone(donut_mcp._get_profile_launch_state(endpoint, "profile-stale"))

    def test_verified_profile_clears_accepted_launch_marker(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51090/mcp/abcdefghijklmnopqrstuv"
        )
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutLaunch_") as temp:
            with mock.patch.object(donut_mcp, "app_data_dir", return_value=Path(temp)):
                donut_mcp._set_profile_launch_state(endpoint, "profile-ready", "accepted")
                client = DonutMcpClient(endpoint, shared_session=False)
                client.get_debug_connection = lambda _profile_id: {  # type: ignore[method-assign]
                    "targets_url": "http://127.0.0.1:9222/json/list"
                }
                profile = donut_mcp.DonutProfile(
                    "profile-ready", "DaiTa", "wayfern", process_id=1234
                )
                with (
                    mock.patch.object(donut_mcp, "_profile_process_alive", return_value=True),
                    mock.patch.object(donut_mcp, "_debug_target_reachable", return_value=True),
                ):
                    self.assertTrue(client.profile_debug_ready("profile-ready", profile=profile))
                self.assertIsNone(
                    donut_mcp._get_profile_launch_state(endpoint, "profile-ready")
                )

    def test_debug_connection_clears_accepted_marker_only_with_live_target(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51090/mcp/abcdefghijklmnopqrstuv"
        )
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutLaunch_") as temp:
            with mock.patch.object(donut_mcp, "app_data_dir", return_value=Path(temp)):
                donut_mcp._set_profile_launch_state(endpoint, "profile-ready", "accepted")
                client = DonutMcpClient(endpoint, shared_session=False)
                client.call_tool_json = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
                    "targets_url": "http://127.0.0.1:9222/json/list"
                }
                client.get_profile = lambda profile_id: donut_mcp.DonutProfile(  # type: ignore[method-assign]
                    profile_id, "DaiTa", "wayfern", process_id=1234
                )
                with (
                    mock.patch.object(donut_mcp, "_profile_process_alive", return_value=True),
                    mock.patch.object(donut_mcp, "_debug_target_reachable", return_value=False),
                ):
                    client.get_debug_connection("profile-ready")
                self.assertIsNotNone(
                    donut_mcp._get_profile_launch_state(endpoint, "profile-ready")
                )
                with (
                    mock.patch.object(donut_mcp, "_profile_process_alive", return_value=True),
                    mock.patch.object(donut_mcp, "_debug_target_reachable", return_value=True),
                ):
                    client.get_debug_connection("profile-ready")
                self.assertIsNone(
                    donut_mcp._get_profile_launch_state(endpoint, "profile-ready")
                )

    def test_confirmed_stop_clears_only_stopped_profile_marker(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51090/mcp/abcdefghijklmnopqrstuv"
        )
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutLaunch_") as temp:
            with mock.patch.object(donut_mcp, "app_data_dir", return_value=Path(temp)):
                donut_mcp._set_profile_launch_state(endpoint, "profile-stop", "accepted")
                client = DonutMcpClient(endpoint, shared_session=False)
                running = donut_mcp.DonutProfile(
                    "profile-stop", "DaiTa", "wayfern", process_id=1234
                )
                stopped = donut_mcp.DonutProfile(
                    "profile-stop", "DaiTa", "wayfern", process_id=None
                )
                self.assertFalse(
                    client.confirm_profile_stopped("profile-stop", profile=running)
                )
                self.assertIsNotNone(
                    donut_mcp._get_profile_launch_state(endpoint, "profile-stop")
                )
                self.assertTrue(
                    client.confirm_profile_stopped("profile-stop", profile=stopped)
                )
                self.assertIsNone(
                    donut_mcp._get_profile_launch_state(endpoint, "profile-stop")
                )

    def test_two_processes_never_send_two_launches_for_one_profile(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutRace_") as temp:
            ctx = multiprocessing.get_context("spawn")
            start = ctx.Event()
            launches = ctx.Queue()
            outcomes = ctx.Queue()
            workers = [
                ctx.Process(
                    target=_racing_profile_launch,
                    args=(temp, start, launches, outcomes),
                )
                for _ in range(2)
            ]
            for worker in workers:
                worker.start()
            start.set()
            for worker in workers:
                worker.join(10)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual([outcomes.get(timeout=2) for _ in workers], ["uncertain"] * 2)
            self.assertEqual(launches.qsize(), 1)

    def test_entitlement_refusal_is_terminal_and_blocks_relaunch(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51086/mcp/abcdefghijklmnopqrstuv"
        )
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutLaunch_") as temp:
            with mock.patch.object(donut_mcp, "app_data_dir", return_value=Path(temp)):
                client = DonutMcpClient(endpoint, shared_session=False)
                donut_mcp._set_profile_launch_state(endpoint, "profile-plan", "launching")
                client._run_profile_in_current_session = lambda *_args: (_ for _ in ()).throw(  # type: ignore[method-assign]
                    DonutMcpEntitlementError(
                        "Custom fingerprints require a Donut Browser plan that includes them"
                    )
                )
                with self.assertRaises(DonutMcpEntitlementError):
                    client._launch_profile_once(
                        client,
                        "profile-plan",
                        "https://studio.youtube.com/",
                    )
                self.assertEqual(
                    donut_mcp._get_profile_launch_state(endpoint, "profile-plan")["state"],
                    "terminal_entitlement",
                )
                client.get_profile = lambda profile_id: donut_mcp.DonutProfile(  # type: ignore[method-assign]
                    profile_id, "DaiTa", "wayfern", process_id=None
                )
                client._run_profile_in_current_session = lambda *_args: self.fail(  # type: ignore[method-assign]
                    "A denied profile must not be launched again"
                )
                with self.assertRaises(DonutMcpEntitlementError):
                    client.run_profile("profile-plan", "about:blank")

    def test_user_upload_can_retry_entitlement_once_through_donut(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51086/mcp/abcdefghijklmnopqrstuv"
        )
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutLaunch_") as temp:
            with mock.patch.object(donut_mcp, "app_data_dir", return_value=Path(temp)):
                donut_mcp._set_profile_launch_state(
                    endpoint, "profile-plan", "terminal_entitlement"
                )
                client = DonutMcpClient(endpoint, shared_session=False)
                client._has_live_session = lambda: True  # type: ignore[method-assign]
                client.get_profile = lambda profile_id: donut_mcp.DonutProfile(  # type: ignore[method-assign]
                    profile_id, "DaiTa", "wayfern", process_id=None
                )
                launched: list[str] = []

                def launch(profile_id: str, _url: str) -> str:
                    launched.append(profile_id)
                    return "started"

                client._run_profile_in_current_session = launch  # type: ignore[method-assign]
                self.assertEqual(
                    client.run_profile(
                        "profile-plan",
                        "about:blank",
                        retry_entitlement_after_user_action=True,
                    ),
                    "started",
                )
                self.assertEqual(launched, ["profile-plan"])
                self.assertEqual(
                    donut_mcp._get_profile_launch_state(endpoint, "profile-plan")["state"],
                    "accepted",
                )

    def test_tool_entitlement_response_is_classified_as_terminal(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51087/mcp/abcdefghijklmnopqrstuv"
        )
        client = DonutMcpClient(endpoint, shared_session=False)
        client._has_live_session = lambda: True  # type: ignore[method-assign]
        client._request = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "result": {
                "isError": True,
                "content": [{
                    "type": "text",
                    "text": (
                        '{"code":"WAYFERN_FINGERPRINT_APPLY_FAILED",'
                        '"message":"Custom fingerprints require a Donut Browser plan that includes them"}'
                    ),
                }],
            }
        }
        with self.assertRaises(DonutMcpEntitlementError):
            client.call_tool_value("run_profile", {"profile_id": "p"})

    def test_jsonrpc_entitlement_error_is_terminal_and_blocks_relaunch(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51091/mcp/abcdefghijklmnopqrstuv"
        )
        with tempfile.TemporaryDirectory(prefix="DocuRandomDonutLaunch_") as temp:
            with mock.patch.object(donut_mcp, "app_data_dir", return_value=Path(temp)):
                client = DonutMcpClient(endpoint, shared_session=False)
                client._has_live_session = lambda: True  # type: ignore[method-assign]
                donut_mcp._set_profile_launch_state(endpoint, "profile-plan", "launching")
                payload = json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {
                        "code": -32000,
                        "message": (
                            'Failed to launch browser: {"code":"WAYFERN_FINGERPRINT_APPLY_FAILED",'
                            '"params":{"detail":"Custom fingerprints require a Donut Browser '
                            'plan that includes them."}}'
                        ),
                    },
                }).encode("utf-8")
                client._request = lambda *_args, **_kwargs: client._decode_response(  # type: ignore[method-assign]
                    payload, "application/json"
                )
                with self.assertRaises(DonutMcpEntitlementError):
                    client._launch_profile_once(client, "profile-plan", "about:blank")
                self.assertEqual(
                    donut_mcp._get_profile_launch_state(endpoint, "profile-plan")["state"],
                    "terminal_entitlement",
                )

    def test_tool_authentication_response_is_terminal(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51088/mcp/abcdefghijklmnopqrstuv"
        )
        client = DonutMcpClient(endpoint, shared_session=False)
        client._has_live_session = lambda: True  # type: ignore[method-assign]
        client._request = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": "authentication required"}],
            }
        }
        with self.assertRaises(DonutMcpAuthenticationError):
            client.call_tool_value("run_profile", {"profile_id": "p"})

    def test_timeout_is_not_reported_as_a_disconnected_bridge(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51084/mcp/abcdefghijklmnopqrstuv"
        )
        client = DonutMcpClient(endpoint, timeout=2.0)

        with mock.patch.object(
            donut_mcp.urllib.request,
            "urlopen",
            side_effect=TimeoutError("timed out"),
        ):
            with self.assertRaises(DonutMcpTimeoutError):
                client._request(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}
                )

    def test_transport_invalidation_preserves_other_active_leases(self) -> None:
        endpoint = DonutMcpEndpoint.parse(
            "http://127.0.0.1:51082/mcp/abcdefghijklmnopqrstuv"
        )
        state = donut_mcp._shared_session_for(endpoint)
        with state.handshake_lock:
            state.session_id = "shared-session"
            state.server_info = {"serverInfo": {"name": "donut-browser-local"}}
            state.active_clients = 2

        try:
            donut_mcp._invalidate_shared_session(state, "shared-session")

            self.assertEqual(state.active_clients, 2)
            self.assertEqual(state.session_id, "")
        finally:
            donut_mcp.close_shared_donut_mcp_sessions()


if __name__ == "__main__":
    unittest.main()
