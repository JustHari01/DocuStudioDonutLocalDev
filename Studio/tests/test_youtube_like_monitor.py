from __future__ import annotations

import io
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from docurandom.donut_mcp import DonutMcpPriorLaunchUnresolved
from docurandom.smm_api import SmmProviderRejectedError, SmmService
from docurandom.telegram_bot import format_telegram_notification
from docurandom.ui import DocuRandomApp
from docurandom.youtube_like_monitor import (
    DonutYouTubeRealtimeSource,
    RealtimeYouTubeVideo,
    YouTubeLikeMonitor,
    YouTubeLikeMonitorResult,
    YouTubeLikePolicy,
    YouTubeRealtimeDeferred,
    YouTubeRealtimeError,
    _is_video_analytics_error,
    _is_transient_checkview_transport_error,
    _parse_realtime_views,
)
import docurandom.youtube_like_monitor as youtube_like_monitor
from docurandom.youtube_upload import ProfileUploadCoordinator, YouTubeUploadCancelled


def _service(
    service_id: str,
    rate: str,
    *,
    minimum: int = 10,
    maximum: int = 10_000,
) -> SmmService:
    return SmmService(
        service_id=service_id,
        name=f"YouTube Likes {service_id}",
        service_type="Default",
        category="YouTube",
        rate=Decimal(rate),
        minimum=minimum,
        maximum=maximum,
        refill=True,
    )


class _FakeRealtimeSource:
    def __init__(self, videos: tuple[RealtimeYouTubeVideo, ...]) -> None:
        self.videos = videos

    def scan_profile(self, profile_id: str, **_kwargs):
        return tuple(
            video for video in self.videos if video.profile_id == profile_id
        )


class _FakeSmmClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, int]] = []

    def add_order(self, service_id: str, link: str, *, quantity: int) -> str:
        self.calls.append((service_id, link, quantity))
        if self.fail:
            raise RuntimeError("mất kết nối sau khi gửi")
        return str(9000 + len(self.calls))


class _RejectFirstVideoSmmClient:
    def __init__(self, rejected_link: str) -> None:
        self.rejected_link = rejected_link
        self.calls: list[tuple[str, str, int]] = []

    def add_order(self, service_id: str, link: str, *, quantity: int) -> str:
        self.calls.append((service_id, link, quantity))
        if link == self.rejected_link:
            raise SmmProviderRejectedError("Not enough funds.")
        return str(9000 + len(self.calls))


class YouTubeLikeMonitorTests(unittest.TestCase):
    def test_video_links_from_dom_reads_thumbnail_id_when_title_has_no_href(self) -> None:
        class FakeCdp:
            def command(self, method: str, params: dict[str, object] | None = None):
                selector = (params or {}).get("selector")
                node_id = (params or {}).get("nodeId")
                if method == "DOM.getDocument":
                    return {"root": {"nodeId": 1}}
                if method == "DOM.querySelectorAll" and node_id == 1:
                    return {"nodeIds": [10]}
                if method == "DOM.querySelectorAll" and node_id == 10 and selector == "img":
                    return {"nodeIds": [11]}
                if method == "DOM.querySelectorAll" and node_id == 10 and selector == "a#video-title":
                    return {"nodeIds": [12]}
                if method == "DOM.describeNode" and node_id == 11:
                    return {
                        "node": {
                            "attributes": [
                                "src",
                                "https://i9.ytimg.com/vi_webp/abc123XYZ/mqdefault.webp",
                                "alt",
                                "Hình thu nhỏ",
                            ]
                        }
                    }
                if method == "DOM.describeNode" and node_id == 12:
                    return {
                        "node": {
                            "attributes": [
                                "id",
                                "video-title",
                                "role",
                                "button",
                                "aria-label",
                                "A video without href",
                            ]
                        }
                    }
                raise AssertionError((method, params))

        actual = DonutYouTubeRealtimeSource._video_links_from_dom(FakeCdp())  # type: ignore[arg-type]
        self.assertEqual(actual, (("abc123XYZ", "A video without href"),))

    @patch("docurandom.youtube_like_monitor.time.sleep", return_value=None)
    def test_video_links_from_dom_retries_with_a_fresh_snapshot_after_stale_node(
        self,
        _sleep,
    ) -> None:
        class FakeCdp:
            def __init__(self) -> None:
                self.snapshot = 0

            def command(self, method: str, params=None):
                node_id = (params or {}).get("nodeId")
                selector = (params or {}).get("selector")
                if method == "DOM.getDocument":
                    self.snapshot += 1
                    return {"root": {"nodeId": 100 + self.snapshot}}
                root_id = 100 + self.snapshot
                row_id = 10 + self.snapshot
                thumbnail_id = 20 + self.snapshot
                title_id = 30 + self.snapshot
                if (
                    method == "DOM.querySelectorAll"
                    and node_id == root_id
                    and selector == "ytcp-video-row"
                ):
                    return {"nodeIds": [row_id]}
                if (
                    method == "DOM.querySelectorAll"
                    and node_id == row_id
                    and selector == "img"
                ):
                    if self.snapshot == 1:
                        raise RuntimeError(
                            "Donut CDP lỗi DOM.querySelectorAll: "
                            "{'message': 'Could not find node with given id'}"
                        )
                    return {"nodeIds": [thumbnail_id]}
                if (
                    method == "DOM.querySelectorAll"
                    and node_id == row_id
                    and selector == "a#video-title"
                ):
                    return {"nodeIds": [title_id]}
                if method == "DOM.describeNode" and node_id == thumbnail_id:
                    return {
                        "node": {
                            "attributes": [
                                "src",
                                "https://i9.ytimg.com/vi/abc123XYZ/mqdefault.jpg",
                            ]
                        }
                    }
                if method == "DOM.describeNode" and node_id == title_id:
                    return {
                        "node": {
                            "attributes": [
                                "aria-label",
                                "Video after Studio re-render",
                            ]
                        }
                    }
                raise AssertionError((method, params))

        cdp = FakeCdp()
        actual = DonutYouTubeRealtimeSource._video_links_from_dom(cdp)  # type: ignore[arg-type]

        self.assertEqual(
            actual,
            (("abc123XYZ", "Video after Studio re-render"),),
        )
        self.assertEqual(cdp.snapshot, 2)

    def test_wait_for_profile_reopens_stale_process_id_until_cdp_is_live(self) -> None:
        class Profile:
            browser = "wayfern"
            process_id = 1234

        class Client:
            def __init__(self) -> None:
                self.debug_calls = 0
                self.run_calls = 0

            def get_profile(self, _profile_id: str):
                return Profile()

            def get_debug_connection(self, _profile_id: str):
                self.debug_calls += 1
                if self.debug_calls < 3:
                    from docurandom.donut_mcp import DonutMcpError

                    raise DonutMcpError("No local CDP endpoint")
                return {"targets_url": "http://127.0.0.1:1/json"}

            def profile_debug_ready(self, profile_id: str, *, profile=None) -> bool:
                return bool(self.get_debug_connection(profile_id))

            def run_profile(self, _profile_id: str, _url: str) -> str:
                self.run_calls += 1
                return "started"

        source = DonutYouTubeRealtimeSource("http://127.0.0.1:21690")
        client = Client()
        progress: list[str] = []
        with patch("docurandom.youtube_like_monitor.time.sleep"):
            source._wait_for_profile(client, "profile", threading.Event(), progress.append)  # type: ignore[arg-type]

        self.assertEqual(client.run_calls, 1)
        self.assertEqual(client.debug_calls, 3)
        self.assertTrue(any("PID cũ" in message for message in progress))

    def test_wait_for_profile_polls_after_launch_timeout_without_relaunching(self) -> None:
        class Profile:
            browser = "wayfern"
            process_id = None

        class Client:
            def __init__(self) -> None:
                self.running = False
                self.run_calls = 0

            def get_profile(self, _profile_id: str):
                profile = Profile()
                profile.process_id = 1234 if self.running else None
                return profile

            def get_debug_connection(self, _profile_id: str):
                if not self.running:
                    from docurandom.donut_mcp import DonutMcpError

                    raise DonutMcpError("No local CDP endpoint")
                return {"targets_url": "http://127.0.0.1:1/json"}

            def profile_debug_ready(self, profile_id: str, *, profile=None) -> bool:
                return bool(self.get_debug_connection(profile_id))

            def run_profile(self, _profile_id: str, _url: str) -> str:
                from docurandom.donut_mcp import DonutMcpTimeoutError

                self.run_calls += 1
                self.running = True
                raise DonutMcpTimeoutError("launch still in progress")

        source = DonutYouTubeRealtimeSource("http://127.0.0.1:21690")
        client = Client()
        progress: list[str] = []
        with patch("docurandom.youtube_like_monitor.time.sleep"):
            source._wait_for_profile(client, "profile", threading.Event(), progress.append)  # type: ignore[arg-type]

        self.assertEqual(client.run_calls, 1)
        self.assertTrue(any("không mở lại lệnh" in message for message in progress))

    def test_wait_for_profile_does_not_poll_an_old_unresolved_launch(self) -> None:
        class Profile:
            browser = "wayfern"
            process_id = None

        class Client:
            def get_profile(self, _profile_id: str):
                return Profile()

            def run_profile(self, _profile_id: str, _url: str) -> str:
                raise DonutMcpPriorLaunchUnresolved("Lệnh mở profile trước chưa xác nhận.")

        source = DonutYouTubeRealtimeSource("http://127.0.0.1:21690")
        with (
            patch(
                "docurandom.youtube_like_monitor.time.sleep",
                side_effect=AssertionError("Old launch must not hold the scan lane"),
            ),
            self.assertRaises(DonutMcpPriorLaunchUnresolved),
        ):
            source._wait_for_profile(Client(), "profile", threading.Event())  # type: ignore[arg-type]

    def test_public_visibility_fails_closed_for_unlisted_and_unavailable_video(self) -> None:
        def response(*, video_id: str, is_unlisted: bool) -> bytes:
            return json.dumps(
                {
                    "videoDetails": {"videoId": video_id},
                    "microformat": {"isUnlisted": is_unlisted},
                },
                separators=(",", ":"),
            ).encode("utf-8")

        with patch(
            "docurandom.youtube_like_monitor.urllib.request.urlopen",
            side_effect=lambda *_args, **_kwargs: io.BytesIO(
                response(video_id="public01", is_unlisted=False)
            ),
        ):
            self.assertEqual(
                DonutYouTubeRealtimeSource._public_visibility("public01"),
                "PUBLIC",
            )
        with patch(
            "docurandom.youtube_like_monitor.urllib.request.urlopen",
            side_effect=lambda *_args, **_kwargs: io.BytesIO(
                response(video_id="unlist01", is_unlisted=True)
            ),
        ):
            self.assertEqual(
                DonutYouTubeRealtimeSource._public_visibility("unlist01"),
                "UNLISTED",
            )
        with patch(
            "docurandom.youtube_like_monitor.urllib.request.urlopen",
            side_effect=lambda *_args, **_kwargs: io.BytesIO(b"{}"),
        ):
            self.assertEqual(
                DonutYouTubeRealtimeSource._public_visibility("private1"),
                "PRIVATE_OR_UNAVAILABLE",
            )

    def test_scan_is_deferred_when_upload_owns_profile(self) -> None:
        coordinator = ProfileUploadCoordinator()
        upload = coordinator.acquire("profile-a", threading.Event())
        source = DonutYouTubeRealtimeSource(
            "http://127.0.0.1:21690",
            coordinator=coordinator,
        )

        try:
            with self.assertRaisesRegex(YouTubeRealtimeDeferred, "hoãn"):
                source.scan_profile("profile-a")
        finally:
            upload.__exit__(None, None, None)

    def test_checkview_transport_close_is_retryable_but_page_errors_are_not(self) -> None:
        self.assertTrue(
            _is_transient_checkview_transport_error(
                RuntimeError("no close frame received or sent")
            )
        )
        self.assertTrue(
            _is_transient_checkview_transport_error(
                RuntimeError("websocket connection reset by peer")
            )
        )
        self.assertTrue(
            _is_transient_checkview_transport_error(
                RuntimeError("Could not find node with given id")
            )
        )
        self.assertTrue(
            _is_transient_checkview_transport_error(
                RuntimeError("Danh sách video YouTube chưa tải đầy đủ")
            )
        )
        self.assertFalse(
            _is_transient_checkview_transport_error(
                RuntimeError("Donut Browser requires a paid Donut Browser plan")
            )
        )
        self.assertFalse(
            _is_transient_checkview_transport_error(
                RuntimeError("Không xác minh được bộ lọc realtime 60 phút")
            )
        )
        analytics_error = RuntimeError(
            "Danh sách video YouTube chưa tải đầy đủ: "
            "chưa đọc được realtime 60 phút của video123 "
            "(YouTube Studio báo lỗi khi tải Analytics của video video123; "
            "trang không có nút thử lại.)."
        )
        self.assertTrue(_is_video_analytics_error(analytics_error))
        self.assertFalse(_is_transient_checkview_transport_error(analytics_error))

    def test_checkview_uses_minimized_window_state_for_background_renderer(self) -> None:
        self.assertEqual(youtube_like_monitor._CHECKVIEW_WINDOW_STATE, "minimized")

    def test_checkview_scan_lane_is_fifo_and_supports_cancellation(self) -> None:
        lane = youtube_like_monitor._CheckviewScanLane(capacity=1)
        entered: list[str] = []
        errors: list[BaseException] = []
        entered_lock = threading.Lock()
        release = {
            name: threading.Event() for name in ("first", "second", "third")
        }
        started = {
            name: threading.Event() for name in ("first", "second", "third")
        }
        waiting = {
            name: threading.Event() for name in ("second", "third")
        }

        def worker(name: str) -> None:
            acquired = False
            try:
                lane.acquire(
                    threading.Event(),
                    lambda _message: waiting.get(name, threading.Event()).set(),
                    profile_id=name,
                )
                acquired = True
                with entered_lock:
                    entered.append(name)
                started[name].set()
                self.assertTrue(release[name].wait(2.0))
            except BaseException as exc:
                errors.append(exc)
            finally:
                if acquired:
                    lane.release()

        first = threading.Thread(target=worker, args=("first",))
        second = threading.Thread(target=worker, args=("second",))
        third = threading.Thread(target=worker, args=("third",))
        first.start()
        self.assertTrue(started["first"].wait(1.0))
        second.start()
        self.assertTrue(waiting["second"].wait(1.0))
        third.start()
        self.assertTrue(waiting["third"].wait(1.0))

        release["first"].set()
        self.assertTrue(started["second"].wait(1.0))
        self.assertEqual(entered, ["first", "second"])
        release["second"].set()
        self.assertTrue(started["third"].wait(1.0))
        self.assertEqual(entered, ["first", "second", "third"])
        release["third"].set()
        for thread in (first, second, third):
            thread.join(1.0)

        self.assertEqual(errors, [])
        self.assertEqual(lane.snapshot(), (0, 0, 1))

        cancel_lane = youtube_like_monitor._CheckviewScanLane(capacity=1)
        holder_release = threading.Event()
        holder_started = threading.Event()
        cancelled = threading.Event()
        cancel_errors: list[BaseException] = []

        def holder() -> None:
            cancel_lane.acquire(threading.Event(), lambda _message: None)
            holder_started.set()
            holder_release.wait(2.0)
            cancel_lane.release()

        def waiter() -> None:
            cancel = threading.Event()
            try:
                cancel_lane.acquire(
                    cancel,
                    lambda _message: (cancelled.set(), cancel.set()),
                )
            except BaseException as exc:
                cancel_errors.append(exc)

        holder_thread = threading.Thread(target=holder)
        waiter_thread = threading.Thread(target=waiter)
        holder_thread.start()
        self.assertTrue(holder_started.wait(1.0))
        waiter_thread.start()
        self.assertTrue(cancelled.wait(1.0))
        holder_release.set()
        holder_thread.join(1.0)
        waiter_thread.join(1.0)
        self.assertEqual(len(cancel_errors), 1)
        self.assertIsInstance(cancel_errors[0], YouTubeUploadCancelled)
        self.assertEqual(cancel_lane.snapshot(), (0, 0, 1))

    def test_checkview_reopens_after_dead_transport_and_retries_with_new_target(self) -> None:
        class FastCancel:
            def is_set(self) -> bool:
                return False

            def wait(self, _timeout: float) -> bool:
                return False

        class Lease:
            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        class Coordinator:
            def try_acquire(self, *_args, **_kwargs):
                return Lease()

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def require_docurandom_local(self) -> None:
                return None

        source = DonutYouTubeRealtimeSource(
            "http://127.0.0.1:21690",
            coordinator=Coordinator(),  # type: ignore[arg-type]
        )
        expected = (RealtimeYouTubeVideo("profile", "abc123XYZ", "Video", 123),)
        attempts = [RuntimeError("no close frame received or sent"), expected]
        source._scan_profile_once = Mock(side_effect=attempts)  # type: ignore[method-assign]
        progress: list[str] = []

        with (
            patch(
                "docurandom.youtube_like_monitor.DonutMcpClient",
                return_value=Client(),
            ),
            patch.object(source, "_invalidate_checkview_target") as invalidate,
        ):
            actual = source.scan_profile(
                "profile",
                cancel_event=FastCancel(),  # type: ignore[arg-type]
                progress=progress.append,
            )

        self.assertEqual(actual, expected)
        self.assertEqual(source._scan_profile_once.call_count, 2)  # type: ignore[attr-defined]
        invalidate.assert_called_once()
        self.assertTrue(any("tự mở lại" in message for message in progress))

    def test_checkview_reopens_after_incomplete_scan_and_does_not_order_partial_data(self) -> None:
        class FastCancel:
            def is_set(self) -> bool:
                return False

            def wait(self, _timeout: float) -> bool:
                return False

        class Lease:
            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        class Coordinator:
            def try_acquire(self, *_args, **_kwargs):
                return Lease()

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def require_docurandom_local(self) -> None:
                return None

        source = DonutYouTubeRealtimeSource(
            "http://127.0.0.1:21690",
            coordinator=Coordinator(),  # type: ignore[arg-type]
        )
        expected = (RealtimeYouTubeVideo("profile", "abc123XYZ", "Video", 123),)
        source._scan_profile_once = Mock(  # type: ignore[method-assign]
            side_effect=[
                RuntimeError("Danh sách video YouTube chưa tải đầy đủ"),
                expected,
            ]
        )
        progress: list[str] = []

        with (
            patch(
                "docurandom.youtube_like_monitor.DonutMcpClient",
                return_value=Client(),
            ),
            patch.object(source, "_invalidate_checkview_target") as invalidate,
        ):
            actual = source.scan_profile(
                "profile",
                cancel_event=FastCancel(),  # type: ignore[arg-type]
                progress=progress.append,
            )

        self.assertEqual(actual, expected)
        self.assertEqual(source._scan_profile_once.call_count, 2)  # type: ignore[attr-defined]
        invalidate.assert_called_once()
        self.assertTrue(any("chưa tải đầy đủ" in message for message in progress))

    def test_semantic_links_accept_content_and_channel_analytics_video_urls(self) -> None:
        def link(name: str, url: str) -> dict[str, object]:
            return {
                "role": {"value": "link"},
                "name": {"value": name},
                "properties": [
                    {"name": "url", "value": {"value": url}},
                ],
            }

        links = DonutYouTubeRealtimeSource._video_links_from_ax(
            [
                link(
                    "Recent video",
                    "https://studio.youtube.com/video/abc123XYZ/edit",
                ),
                link(
                    "Older hot video",
                    "https://studio.youtube.com/video/hot987XYZ/analytics/tab-overview",
                ),
                link("Dashboard", "https://studio.youtube.com/channel/example"),
            ]
        )
        self.assertEqual(
            links,
            (("abc123XYZ", "Recent video"), ("hot987XYZ", "Older hot video")),
        )

    def test_next_page_button_is_semantic_localized_and_must_be_enabled(self) -> None:
        def button(label: str, backend_id: int, *, disabled: bool = False):
            return {
                "ignored": False,
                "role": {"value": "button"},
                "name": {"value": label},
                "backendDOMNodeId": backend_id,
                "properties": [
                    {"name": "disabled", "value": {"value": disabled}},
                ],
            }

        selected = DonutYouTubeRealtimeSource._next_page_button_from_ax(
            [
                button("Trang tiếp theo", 10, disabled=True),
                button("次のページ", 11),
            ]
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected["backendDOMNodeId"], 11)

    def test_pagination_scroll_stays_inside_background_target(self) -> None:
        calls: list[str] = []
        activations: list[str] = []

        class Activation:
            def __enter__(self):
                activations.append("enter")

            def __exit__(self, *_args):
                activations.append("exit")

        class FakeCdp:
            def command(self, method: str, _params=None):
                calls.append(method)
                return {}

        DonutYouTubeRealtimeSource._expose_pagination_controls(  # type: ignore[arg-type]
            FakeCdp(),
            lambda: Activation(),  # type: ignore[arg-type]
        )

        self.assertNotIn("Runtime.evaluate", calls)
        self.assertNotIn("Input.dispatchMouseEvent", calls)
        self.assertNotIn("Page.bringToFront", calls)
        self.assertEqual(calls.count("Input.dispatchKeyEvent"), 6)
        self.assertEqual(activations, ["enter", "exit"])

    @patch("docurandom.youtube_like_monitor.time.sleep", return_value=None)
    def test_paginated_reader_collects_multiple_video_pages(
        self,
        _sleep,
    ) -> None:
        def link(video_id: str, title: str) -> dict[str, object]:
            return {
                "ignored": False,
                "role": {"value": "link"},
                "name": {"value": title},
                "properties": [
                    {
                        "name": "url",
                        "value": {
                            "value": (
                                f"https://studio.youtube.com/video/{video_id}/edit"
                            )
                        },
                    }
                ],
            }

        class FakeCdp:
            def __init__(self) -> None:
                self.page = 0

            def command(self, method: str, _params=None):
                if method == "Accessibility.getFullAXTree":
                    nodes = [
                        [
                            link("first01", "Video 1"),
                            {
                                "ignored": False,
                                "role": {"value": "button"},
                                "name": {"value": "Trang tiếp theo"},
                                "backendDOMNodeId": 99,
                                "properties": [],
                            },
                        ],
                        [link("second2", "Video 2")],
                    ][self.page]
                    return {"nodes": nodes}
                if method == "DOM.getDocument":
                    return {"root": {"nodeId": 1}}
                if method == "DOM.querySelectorAll":
                    return {"nodeIds": []}
                if (
                    method == "Input.dispatchKeyEvent"
                    and _params.get("type") == "keyUp"
                    and _params.get("key") == "Enter"
                ):
                    self.page = 1
                return {}

        source = DonutYouTubeRealtimeSource(
            "http://127.0.0.1:21690",
            max_videos=100,
            navigation_timeout=0.01,
        )
        links = source._paginated_video_links(  # type: ignore[arg-type]
            FakeCdp(),
            threading.Event(),
        )

        self.assertEqual(
            links,
            (("first01", "Video 1"), ("second2", "Video 2")),
        )

    @patch("docurandom.youtube_like_monitor.time.sleep", return_value=None)
    def test_paginated_reader_has_no_arbitrary_page_or_video_cap(self, _sleep) -> None:
        pages = 23

        def link(index: int) -> dict[str, object]:
            video_id = f"video{index:05d}"
            return {
                "ignored": False,
                "role": {"value": "link"},
                "name": {"value": f"Video {index}"},
                "properties": [
                    {
                        "name": "url",
                        "value": {
                            "value": f"https://studio.youtube.com/video/{video_id}/edit"
                        },
                    }
                ],
            }

        class FakeCdp:
            def __init__(self) -> None:
                self.page = 0

            def command(self, method: str, params=None):
                if method == "Accessibility.getFullAXTree":
                    nodes: list[dict[str, object]] = [link(self.page)]
                    if self.page < pages - 1:
                        nodes.append(
                            {
                                "ignored": False,
                                "role": {"value": "button"},
                                "name": {"value": "Next page"},
                                "backendDOMNodeId": 1000 + self.page,
                                "properties": [],
                            }
                        )
                    return {"nodes": nodes}
                if method == "DOM.getDocument":
                    return {"root": {"nodeId": 1}}
                if method == "DOM.querySelectorAll":
                    return {"nodeIds": []}
                if (
                    method == "Input.dispatchKeyEvent"
                    and params.get("type") == "keyUp"
                    and params.get("key") == "Enter"
                ):
                    self.page += 1
                return {}

        source = DonutYouTubeRealtimeSource(
            "http://127.0.0.1:21690",
            max_videos=None,
            navigation_timeout=0.01,
        )
        links = source._paginated_video_links(  # type: ignore[arg-type]
            FakeCdp(),
            threading.Event(),
        )

        self.assertEqual(len(links), pages)
        self.assertEqual(links[-1], ("video00022", "Video 22"))

    @patch("docurandom.youtube_like_monitor.time.sleep", return_value=None)
    def test_paginated_reader_rejects_an_unconfirmed_partial_page(self, _sleep) -> None:
        class FakeCdp:
            def __init__(self) -> None:
                self.page = 0

            def command(self, method: str, params=None):
                if method == "Accessibility.getFullAXTree":
                    if self.page == 0:
                        return {
                            "nodes": [
                                {
                                    "ignored": False,
                                    "role": {"value": "link"},
                                    "name": {"value": "First video"},
                                    "properties": [
                                        {
                                            "name": "url",
                                            "value": {
                                                "value": (
                                                    "https://studio.youtube.com/"
                                                    "video/first01/edit"
                                                )
                                            },
                                        }
                                    ],
                                },
                                {
                                    "ignored": False,
                                    "role": {"value": "button"},
                                    "name": {"value": "Next page"},
                                    "backendDOMNodeId": 99,
                                    "properties": [],
                                },
                            ]
                        }
                    return {"nodes": []}
                if method == "DOM.getDocument":
                    return {"root": {"nodeId": 1}}
                if method == "DOM.querySelectorAll":
                    return {"nodeIds": []}
                if (
                    method == "Input.dispatchKeyEvent"
                    and (params or {}).get("type") == "keyUp"
                    and (params or {}).get("key") == "Enter"
                ):
                    self.page = 1
                return {}

        source = DonutYouTubeRealtimeSource(
            "http://127.0.0.1:21690",
            navigation_timeout=0.01,
        )
        source.navigation_timeout = 0.01
        with self.assertRaisesRegex(YouTubeRealtimeError, "chưa tải đầy đủ"):
            source._paginated_video_links(  # type: ignore[arg-type]
                FakeCdp(),
                threading.Event(),
            )

    def test_extension_page_reader_rejects_incomplete_or_sample_only_response(self) -> None:
        source = DonutYouTubeRealtimeSource("http://127.0.0.1:21690")
        with self.assertRaisesRegex(YouTubeRealtimeError, "chưa tải đầy đủ"):
            source._video_links_from_extension_page(  # type: ignore[arg-type]
                {
                    "complete": False,
                    "rows": [
                        {"videoId": "sample01", "title": "Sample"},
                    ],
                    "videoCount": 1,
                }
            )

    def test_extension_paginated_reader_collects_all_pages_and_deduplicates(self) -> None:
        pages = {
            "snapshot": {
                "complete": True,
                "rows": [
                    {"videoId": "first01", "title": "First"},
                    {"videoId": "banrk8eWaEI", "title": "Eligible video"},
                ],
                "videoCount": 2,
                "pageSignature": "first01\x1fbanrk8eWaEI",
                "nextFound": True,
                "nextEnabled": True,
            },
            "next": {
                "complete": True,
                "transitioned": True,
                "rows": [
                    {"videoId": "banrk8eWaEI", "title": "Eligible video"},
                    {"videoId": "second02", "title": "Second"},
                ],
                "videoCount": 2,
                "pageSignature": "banrk8eWaEI\x1fsecond02",
                "nextFound": True,
                "nextEnabled": False,
            },
        }

        class FakeBridge:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def command(self, profile_id, command, params=None, *, timeout=30.0):
                self.calls.append(
                    {
                        "profile_id": profile_id,
                        "command": command,
                        "params": params or {},
                        "timeout": timeout,
                    }
                )
                return pages[str((params or {}).get("action") or "snapshot")]

        bridge = FakeBridge()
        source = DonutYouTubeRealtimeSource(
            "http://127.0.0.1:21690",
            max_videos=None,
        )
        links = source._paginated_video_links_via_extension(  # type: ignore[arg-type]
            bridge,  # type: ignore[arg-type]
            "profile-daita3",
            77,
            threading.Event(),
        )

        self.assertEqual(
            links,
            (
                ("first01", "First"),
                ("banrk8eWaEI", "Eligible video"),
                ("second02", "Second"),
            ),
        )
        self.assertEqual(len(bridge.calls), 2)
        self.assertEqual(
            bridge.calls[1]["params"]["before_signature"],  # type: ignore[index]
            "first01\x1fbanrk8eWaEI",
        )

    def test_checkview_reconciles_lost_next_reply_without_clicking_twice(self) -> None:
        first = {
            "complete": True,
            "rows": [{"videoId": "first01", "title": "First"}],
            "videoCount": 1,
            "pageSignature": "first01",
            "nextEnabled": True,
        }
        second = {
            "complete": True,
            "rows": [{"videoId": "second02", "title": "Second"}],
            "videoCount": 1,
            "pageSignature": "second02",
            "nextEnabled": False,
        }

        class FakeBridge:
            def __init__(self) -> None:
                self.actions: list[str] = []

            def command(self, _profile_id, _command, params=None, *, timeout=30.0):
                action = str((params or {}).get("action"))
                self.actions.append(action)
                if action == "next":
                    return None  # Wayfern lost the reply after clicking Next.
                return first if len(self.actions) == 1 else second

        bridge = FakeBridge()
        source = DonutYouTubeRealtimeSource("http://127.0.0.1:21690")
        links = source._paginated_video_links_via_extension(  # type: ignore[arg-type]
            bridge, "profile", 77, threading.Event()
        )
        self.assertEqual(links, (("first01", "First"), ("second02", "Second")))
        self.assertEqual(bridge.actions, ["snapshot", "next", "snapshot"])

    def test_checkview_stops_when_lost_next_reply_cannot_be_reconciled(self) -> None:
        first = {
            "complete": True,
            "rows": [{"videoId": "first01", "title": "First"}],
            "videoCount": 1,
            "pageSignature": "first01",
            "nextEnabled": True,
        }

        class FakeBridge:
            def __init__(self) -> None:
                self.actions: list[str] = []

            def command(self, _profile_id, _command, params=None, *, timeout=30.0):
                action = str((params or {}).get("action"))
                self.actions.append(action)
                return None if action == "next" else first

        bridge = FakeBridge()
        source = DonutYouTubeRealtimeSource("http://127.0.0.1:21690")
        with patch.object(youtube_like_monitor.time, "sleep"):
            with self.assertRaisesRegex(YouTubeRealtimeError, "không bấm lại"):
                source._paginated_video_links_via_extension(  # type: ignore[arg-type]
                    bridge, "profile", 77, threading.Event()
                )
        self.assertEqual(bridge.actions.count("next"), 1)
        self.assertEqual(bridge.actions.count("snapshot"), 4)

    def test_checkview_retries_only_read_only_snapshot_after_empty_reply(self) -> None:
        page = {
            "complete": True,
            "rows": [{"videoId": "first01", "title": "First"}],
            "videoCount": 1,
            "pageSignature": "first01",
            "nextEnabled": False,
        }

        class FakeBridge:
            def __init__(self) -> None:
                self.actions: list[str] = []

            def command(self, _profile_id, _command, params=None, *, timeout=30.0):
                self.actions.append(str((params or {}).get("action")))
                return None if len(self.actions) == 1 else page

        bridge = FakeBridge()
        source = DonutYouTubeRealtimeSource("http://127.0.0.1:21690")
        with patch.object(youtube_like_monitor.time, "sleep"):
            links = source._paginated_video_links_via_extension(  # type: ignore[arg-type]
                bridge, "profile", 77, threading.Event()
            )
        self.assertEqual(links, (("first01", "First"),))
        self.assertEqual(bridge.actions, ["snapshot", "snapshot"])

    def test_service_filter_keeps_only_direct_youtube_video_likes(self) -> None:
        direct = SmmService(
            "direct",
            "YouTube Likes | Video",
            "Default",
            "Youtube | Likes",
            Decimal("0.25"),
            10,
            10_000,
            True,
        )
        views_bonus = SmmService(
            "views",
            "YouTube Views | Bonus Likes",
            "Default",
            "Youtube | Views",
            Decimal("0.10"),
            10,
            10_000,
            True,
        )
        comment_likes = SmmService(
            "comments",
            "YouTube Comment Likes",
            "Default",
            "Youtube | Likes For Comments",
            Decimal("0.10"),
            10,
            10_000,
            True,
        )
        shorts_only = SmmService(
            "shorts",
            "YouTube Short Likes",
            "Default",
            "Youtube | Likes",
            Decimal("0.10"),
            10,
            10_000,
            True,
        )
        self.assertTrue(DocuRandomApp._is_youtube_like_service(direct))
        self.assertFalse(DocuRandomApp._is_youtube_like_service(views_bonus))
        self.assertFalse(DocuRandomApp._is_youtube_like_service(comment_likes))
        self.assertFalse(DocuRandomApp._is_youtube_like_service(shorts_only))

    def test_realtime_parser_reads_live_and_localized_accessibility_labels(self) -> None:
        def node(label: str) -> dict[str, object]:
            return {
                "ignored": False,
                "role": {"value": "StaticText"},
                "name": {"value": label},
            }

        self.assertEqual(
            _parse_realtime_views(
                [node("Latest activity, Views: Last 60 minutes, 1,234.")]
            ),
            1234,
        )
        self.assertEqual(
            _parse_realtime_views(
                [node("Hoạt động mới nhất, Lượt xem: 60 phút qua, 278")]
            ),
            278,
        )
        self.assertIsNone(
            _parse_realtime_views(
                [node("Latest activity, Views: Last 48 hours, 9,999.")]
            )
        )

    def test_realtime_views_require_visible_60_minute_control_and_report(self) -> None:
        def node(role: str, label: str, *, backend_id: int | None = None):
            result: dict[str, object] = {
                "ignored": False,
                "role": {"value": role},
                "name": {"value": label},
            }
            if backend_id is not None:
                result["backendDOMNodeId"] = backend_id
                result["properties"] = [
                    {"name": "focusable", "value": {"value": True}}
                ]
            return result

        report = node(
            "StaticText",
            "Latest activity, Views: Last 60 minutes, 321.",
        )
        selected_range = node(
            "button",
            "Views · Last 60 minutes",
            backend_id=42,
        )
        wrong_range = node(
            "button",
            "Views · Last 48 hours",
            backend_id=43,
        )
        menu_48 = node("option", "Last 48 hours", backend_id=44)
        menu_60 = node("option", "Last 60 minutes", backend_id=45)

        self.assertEqual(
            DonutYouTubeRealtimeSource._verified_60_minute_views_from_ax(
                [selected_range, report]
            ),
            321,
        )
        self.assertIsNone(
            DonutYouTubeRealtimeSource._verified_60_minute_views_from_ax([report])
        )
        self.assertIsNone(
            DonutYouTubeRealtimeSource._verified_60_minute_views_from_ax(
                [wrong_range, report]
            )
        )
        self.assertEqual(
            DonutYouTubeRealtimeSource._realtime_48_control_from_ax([wrong_range]),
            wrong_range,
        )
        self.assertIsNone(
            DonutYouTubeRealtimeSource._realtime_48_control_from_ax([selected_range])
        )
        self.assertTrue(
            DonutYouTubeRealtimeSource._realtime_range_menu_is_open(
                [selected_range, menu_48, menu_60]
            )
        )
        self.assertIsNone(
            DonutYouTubeRealtimeSource._verified_60_minute_views_from_ax(
                [selected_range, report, menu_48, menu_60]
            )
        )

    def test_video_discovery_skips_channel_level_48_hour_activity_report(self) -> None:
        source = DonutYouTubeRealtimeSource("http://127.0.0.1:21690")
        expected = (("video123", "Video"),)
        with (
            patch.object(
                source,
                "_latest_activity_video_links",
                side_effect=AssertionError("must not open 48-hour activity report"),
            ),
            patch.object(source, "_recent_video_links", return_value=expected),
        ):
            actual = source._video_links(  # type: ignore[arg-type]
                object(),
                "channel",
                threading.Event(),
            )
        self.assertEqual(actual, expected)

    def test_current_60_minute_control_is_not_pressed_while_report_loads(self) -> None:
        def node(role: str, label: str, *, backend_id: int | None = None):
            result: dict[str, object] = {
                "ignored": False,
                "role": {"value": role},
                "name": {"value": label},
            }
            if backend_id is not None:
                result["backendDOMNodeId"] = backend_id
                result["properties"] = [
                    {"name": "focusable", "value": {"value": True}}
                ]
            return result

        selected_range = node(
            "button",
            "Views · Last 60 minutes",
            backend_id=42,
        )
        report = node(
            "StaticText",
            "Latest activity, Views: Last 60 minutes, 321.",
        )
        source = DonutYouTubeRealtimeSource(
            "http://127.0.0.1:21690",
            navigation_timeout=5,
        )
        source._navigate = Mock()  # type: ignore[method-assign]
        source._ax_nodes = Mock(  # type: ignore[method-assign]
            side_effect=[[selected_range], [selected_range, report]]
        )
        source._activate_ax_button = Mock()  # type: ignore[method-assign]

        with patch("docurandom.youtube_like_monitor.time.sleep"):
            views = source._video_realtime_views(  # type: ignore[arg-type]
                object(),
                "video123",
                threading.Event(),
            )

        self.assertEqual(views, 321)
        source._activate_ax_button.assert_not_called()

    def test_48_hour_range_uses_live_dom_click_then_real_60_minute_option(self) -> None:
        def node(role: str, label: str, *, backend_id: int):
            return {
                "ignored": False,
                "role": {"value": role},
                "name": {"value": label},
                "backendDOMNodeId": backend_id,
                "properties": [
                    {"name": "focusable", "value": {"value": True}}
                ],
            }

        current_48 = node("button", "Views · Last 48 hours", backend_id=40)
        menu_48 = node("option", "Last 48 hours", backend_id=41)
        menu_60 = node("option", "Last 60 minutes", backend_id=42)
        current_60 = node("button", "Views · Last 60 minutes", backend_id=43)
        report = {
            "ignored": False,
            "role": {"value": "StaticText"},
            "name": {
                "value": "Latest activity, Views: Last 60 minutes, 321."
            },
        }
        source = DonutYouTubeRealtimeSource(
            "http://127.0.0.1:21690",
            navigation_timeout=5,
        )
        source._navigate = Mock()  # type: ignore[method-assign]
        source._ax_nodes = Mock(  # type: ignore[method-assign]
            side_effect=[
                [current_48],
                [current_48, menu_48, menu_60],
                [current_60, report],
            ]
        )
        source._click_ax_control = Mock()  # type: ignore[method-assign]

        with patch("docurandom.youtube_like_monitor.time.sleep"):
            views = source._video_realtime_views(  # type: ignore[arg-type]
                object(),
                "video123",
                threading.Event(),
            )

        self.assertEqual(views, 321)
        self.assertEqual(
            [call.args[1]["backendDOMNodeId"] for call in source._click_ax_control.call_args_list],
            [40, 42],
        )

    def test_analytics_page_error_retries_once_then_reports_actual_failure(self) -> None:
        error = {
            "ignored": False,
            "role": {"value": "StaticText"},
            "name": {"value": "Rất tiếc, đã xảy ra lỗi."},
        }
        retry = {
            "ignored": False,
            "role": {"value": "button"},
            "name": {"value": "Thử lại"},
            "backendDOMNodeId": 52,
        }
        source = DonutYouTubeRealtimeSource(
            "http://127.0.0.1:21690", navigation_timeout=5
        )
        source._navigate = Mock()  # type: ignore[method-assign]
        source._ax_nodes = Mock(  # type: ignore[method-assign]
            side_effect=[[error, retry], [], [error, retry]]
        )
        source._click_ax_control = Mock()  # type: ignore[method-assign]

        with patch("docurandom.youtube_like_monitor.time.sleep"):
            with self.assertRaisesRegex(
                YouTubeRealtimeError, "YouTube Studio báo lỗi khi tải Analytics"
            ):
                source._video_realtime_views(  # type: ignore[arg-type]
                    object(), "video123", threading.Event()
                )

        source._click_ax_control.assert_called_once_with(unittest.mock.ANY, retry)

    def test_analytics_page_error_can_recover_to_verified_60_minute_report(self) -> None:
        error = {
            "ignored": False,
            "role": {"value": "StaticText"},
            "name": {"value": "Rất tiếc, đã xảy ra lỗi."},
        }
        retry = {
            "ignored": False,
            "role": {"value": "button"},
            "name": {"value": "Thử lại"},
            "backendDOMNodeId": 52,
        }
        selected_range = {
            "ignored": False,
            "role": {"value": "button"},
            "name": {"value": "Views · Last 60 minutes"},
            "backendDOMNodeId": 42,
        }
        report = {
            "ignored": False,
            "role": {"value": "StaticText"},
            "name": {"value": "Latest activity, Views: Last 60 minutes, 321."},
        }
        source = DonutYouTubeRealtimeSource(
            "http://127.0.0.1:21690", navigation_timeout=5
        )
        source._navigate = Mock()  # type: ignore[method-assign]
        source._ax_nodes = Mock(  # type: ignore[method-assign]
            side_effect=[[error, retry], [], [selected_range, report]]
        )
        source._click_ax_control = Mock()  # type: ignore[method-assign]

        with patch("docurandom.youtube_like_monitor.time.sleep"):
            views = source._video_realtime_views(  # type: ignore[arg-type]
                object(), "video123", threading.Event()
            )

        self.assertEqual(views, 321)
        source._click_ax_control.assert_called_once_with(unittest.mock.ANY, retry)

    def test_analytics_page_error_without_retry_reload_is_recoverable(self) -> None:
        error = {
            "ignored": False,
            "role": {"value": "StaticText"},
            "name": {"value": "Something went wrong"},
        }
        selected_range = {
            "ignored": False,
            "role": {"value": "button"},
            "name": {"value": "Views · Last 60 minutes"},
            "backendDOMNodeId": 42,
        }
        report = {
            "ignored": False,
            "role": {"value": "StaticText"},
            "name": {"value": "Latest activity, Views: Last 60 minutes, 17."},
        }
        source = DonutYouTubeRealtimeSource(
            "http://127.0.0.1:21690", navigation_timeout=5
        )
        source._navigate = Mock()  # type: ignore[method-assign]
        source._ax_nodes = Mock(  # type: ignore[method-assign]
            side_effect=[[error], [error], [selected_range, report]]
        )

        with patch("docurandom.youtube_like_monitor.time.sleep"):
            views = source._video_realtime_views(  # type: ignore[arg-type]
                object(),
                "video123",
                threading.Event(),
            )

        self.assertEqual(views, 17)
        self.assertEqual(source._navigate.call_count, 3)  # type: ignore[attr-defined]

    def test_policy_is_stable_and_between_five_and_ten_percent(self) -> None:
        policy = YouTubeLikePolicy()
        first = policy.quantity_for("profile:video", 10_000)
        second = policy.quantity_for("profile:video", 10_000)
        self.assertEqual(first, second)
        quantity, basis_points = first
        self.assertGreaterEqual(quantity, 500)
        self.assertLessEqual(quantity, 1000)
        self.assertGreaterEqual(basis_points, 500)
        self.assertLessEqual(basis_points, 1000)

    def test_read_only_mode_never_calls_order_api(self) -> None:
        video = RealtimeYouTubeVideo("profile", "abc123XYZ", "Video", 1000)
        client = _FakeSmmClient()
        with TemporaryDirectory() as temporary:
            monitor = YouTubeLikeMonitor(
                state_path=Path(temporary) / "state.json",
                smm_client=client,  # type: ignore[arg-type]
                realtime_source=_FakeRealtimeSource((video,)),  # type: ignore[arg-type]
            )
            results = monitor.scan_once(
                profile_id="profile",
                services=(_service("cheap", "0.50"),),
                allow_orders=False,
            )
        self.assertEqual(client.calls, [])
        self.assertEqual(results[0].state, "dry_run_eligible")

    def test_cheapest_service_and_thirty_minute_cooldown(self) -> None:
        video = RealtimeYouTubeVideo("profile", "abc123XYZ", "Video", 1000)
        client = _FakeSmmClient()
        start = datetime(2026, 8, 29, 1, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as temporary:
            monitor = YouTubeLikeMonitor(
                state_path=Path(temporary) / "state.json",
                smm_client=client,  # type: ignore[arg-type]
                realtime_source=_FakeRealtimeSource((video,)),  # type: ignore[arg-type]
            )
            first = monitor.scan_once(
                profile_id="profile",
                services=(
                    _service("expensive", "1.00"),
                    _service("cheap", "0.25"),
                ),
                now=start,
            )
            second = monitor.scan_once(
                profile_id="profile",
                services=(_service("cheap", "0.25"),),
                now=start + timedelta(minutes=29),
            )
            third = monitor.scan_once(
                profile_id="profile",
                services=(_service("cheap", "0.25"),),
                now=start + timedelta(minutes=30),
            )
        self.assertEqual(first[0].state, "ordered")
        self.assertEqual(first[0].video_title, "Video")
        self.assertEqual(second[0].state, "cooldown")
        self.assertEqual(third[0].state, "ordered")
        self.assertEqual([call[0] for call in client.calls], ["cheap", "cheap"])

    def test_telegram_order_text_is_readable_and_identifies_video(self) -> None:
        result = YouTubeLikeMonitorResult(
            identity="profile:abc123XYZ",
            youtube_url="https://youtu.be/abc123XYZ",
            realtime_views=1234,
            state="ordered",
            message="ordered",
            video_title="Tin tức Nhật Bản & thế giới",
            quantity=87,
            ratio_basis_points=704,
            service_id="4068",
            order_id="99999",
        )

        message = DocuRandomApp._smm_monitor_telegram_text((result,))

        self.assertIn("✅ YOUTUBE LIKE · ĐÃ ĐẶT 1 ĐƠN", message)
        self.assertIn("🎬 Video 1: Tin tức Nhật Bản & thế giới", message)
        self.assertIn("🔗 https://youtu.be/abc123XYZ", message)
        self.assertIn("👁 View trong 60 phút: 1.234", message)
        self.assertIn("🎯 Tỷ lệ Like: 7,04%", message)
        self.assertIn("👍 Like đã đặt: 87", message)
        self.assertIn("📦 Gói rẻ nhất: #4068", message)
        self.assertIn("🧾 Mã đơn: #99999", message)
        self.assertIn("xét lại sau ít nhất 30 phút", message)
        formatted = format_telegram_notification("status", f"[Kênh Nhật] {message}")
        self.assertIn("<pre>https://youtu.be/abc123XYZ</pre>", formatted)
        self.assertIn("<b>👍 Like đã đặt:</b> 87", formatted)
        self.assertIn("<b>🧾 Mã đơn:</b> <code>#99999</code>", formatted)

    def test_fresh_catalog_replaces_cached_prices_before_selection(self) -> None:
        video = RealtimeYouTubeVideo("profile", "abc123XYZ", "Video", 1000)
        client = _FakeSmmClient()
        loader_calls = 0

        def load_services() -> tuple[SmmService, ...]:
            nonlocal loader_calls
            loader_calls += 1
            return (
                _service("fresh-expensive", "1.00"),
                _service("fresh-cheapest", "0.20"),
            )

        with TemporaryDirectory() as temporary:
            monitor = YouTubeLikeMonitor(
                state_path=Path(temporary) / "state.json",
                smm_client=client,  # type: ignore[arg-type]
                realtime_source=_FakeRealtimeSource((video,)),  # type: ignore[arg-type]
            )
            results = monitor.scan_once(
                profile_id="profile",
                services=(_service("cached-old-price", "0.01"),),
                service_loader=load_services,
            )
        self.assertEqual(loader_calls, 1)
        self.assertEqual(results[0].service_id, "fresh-cheapest")
        self.assertEqual(client.calls[0][0], "fresh-cheapest")

    def test_calculated_quantity_below_ten_waits(self) -> None:
        video = RealtimeYouTubeVideo("profile", "abc123XYZ", "Video", 30)
        client = _FakeSmmClient()
        with TemporaryDirectory() as temporary:
            monitor = YouTubeLikeMonitor(
                state_path=Path(temporary) / "state.json",
                smm_client=client,  # type: ignore[arg-type]
                realtime_source=_FakeRealtimeSource((video,)),  # type: ignore[arg-type]
            )
            results = monitor.scan_once(
                profile_id="profile",
                services=(_service("cheap", "0.25"),),
            )
        self.assertEqual(results[0].state, "waiting_minimum")
        self.assertEqual(client.calls, [])

    def test_cancelled_scan_stops_before_order_api(self) -> None:
        video = RealtimeYouTubeVideo("profile", "abc123XYZ", "Video", 1000)
        client = _FakeSmmClient()
        cancel = threading.Event()
        cancel.set()
        with TemporaryDirectory() as temporary:
            monitor = YouTubeLikeMonitor(
                state_path=Path(temporary) / "state.json",
                smm_client=client,  # type: ignore[arg-type]
                realtime_source=_FakeRealtimeSource((video,)),  # type: ignore[arg-type]
            )
            with self.assertRaises(YouTubeUploadCancelled):
                monitor.scan_once(
                    profile_id="profile",
                    services=(_service("cheap", "0.25"),),
                    cancel_event=cancel,
                )
        self.assertEqual(client.calls, [])

    def test_ambiguous_failure_locks_video_against_duplicate(self) -> None:
        video = RealtimeYouTubeVideo("profile", "abc123XYZ", "Video", 1000)
        client = _FakeSmmClient(fail=True)
        start = datetime(2026, 8, 29, 1, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as temporary:
            monitor = YouTubeLikeMonitor(
                state_path=Path(temporary) / "state.json",
                smm_client=client,  # type: ignore[arg-type]
                realtime_source=_FakeRealtimeSource((video,)),  # type: ignore[arg-type]
            )
            first = monitor.scan_once(
                profile_id="profile",
                services=(_service("cheap", "0.25"),),
                now=start,
            )
            second = monitor.scan_once(
                profile_id="profile",
                services=(_service("cheap", "0.25"),),
                now=start + timedelta(hours=1),
            )
        self.assertEqual(first[0].state, "uncertain")
        self.assertEqual(second, ())
        self.assertEqual(len(client.calls), 1)

    def test_provider_rejection_is_retryable_and_does_not_stop_other_videos(self) -> None:
        rejected = RealtimeYouTubeVideo("profile", "rejected123", "Rejected", 1000)
        accepted = RealtimeYouTubeVideo("profile", "accepted123", "Accepted", 900)
        client = _RejectFirstVideoSmmClient(rejected.youtube_url)
        start = datetime(2026, 8, 29, 1, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as temporary:
            monitor = YouTubeLikeMonitor(
                state_path=Path(temporary) / "state.json",
                smm_client=client,  # type: ignore[arg-type]
                realtime_source=_FakeRealtimeSource((rejected, accepted)),  # type: ignore[arg-type]
            )
            first = monitor.scan_once(
                profile_id="profile",
                services=(_service("cheap", "0.25"),),
                now=start,
            )
            second = monitor.scan_once(
                profile_id="profile",
                services=(_service("cheap", "0.25"),),
                now=start + timedelta(hours=1),
            )
            state = json.loads((Path(temporary) / "state.json").read_text())

        self.assertEqual([result.state for result in first], ["retryable", "ordered"])
        self.assertEqual([result.state for result in second], ["retryable", "ordered"])
        self.assertEqual(
            [call[1] for call in client.calls],
            [rejected.youtube_url, accepted.youtube_url]
            + [rejected.youtube_url, accepted.youtube_url],
        )
        self.assertEqual(state["videos"][rejected.identity]["status"], "retryable")
        self.assertEqual(state["videos"][accepted.identity]["status"], "active")

    def test_legacy_uncertain_insufficient_funds_record_is_reopened(self) -> None:
        video = RealtimeYouTubeVideo("profile", "legacy123", "Legacy", 1000)
        client = _FakeSmmClient()
        start = datetime(2026, 8, 29, 1, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "videos": {
                            video.identity: {
                                "status": "uncertain",
                                "detail": "Not enough funds.",
                            }
                        },
                    }
                )
            )
            monitor = YouTubeLikeMonitor(
                state_path=state_path,
                smm_client=client,  # type: ignore[arg-type]
                realtime_source=_FakeRealtimeSource((video,)),  # type: ignore[arg-type]
            )
            results = monitor.scan_once(
                profile_id="profile",
                services=(_service("cheap", "0.25"),),
                now=start,
            )

        self.assertEqual(results[0].state, "ordered")
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
