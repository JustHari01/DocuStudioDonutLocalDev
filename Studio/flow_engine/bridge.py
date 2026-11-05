"""Flow Engine — ExtensionBridge.

WebSocket + HTTP server that communicates with the Chrome extension.
Handles auth token capture, API proxying, and request/response routing.
Supports multiple concurrent extension clients (multi-PC/multi-browser setup).
"""

import time as _time
import contextvars

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
import random
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websockets

target_client_id_var = contextvars.ContextVar("target_client_id", default=None)
_PRE_SUBMISSION_UI_PHASES = frozenset({"project", "configure-ui", "configure"})

from .config import (
    WS_PORT, HTTP_PORT, API_BASE, API_KEY,
    CLIENT_CTX, USER_AGENTS, API_REQUEST_TIMEOUT,
    MAX_CONCURRENT_REQUESTS, REQUEST_MIN_INTERVAL,
)

log = logging.getLogger("flow_engine.bridge")


_VISIBLE_EDGE_WINDOW_CACHE_LOCK = threading.Lock()
_VISIBLE_EDGE_WINDOW_CACHE: tuple[float, bool] | None = None
_VISIBLE_EDGE_WINDOW_CACHE_TTL_SECONDS = 2.0
_VISIBLE_EDGE_WINDOW_PROBE_INFLIGHT = False


def _has_visible_edge_window_uncached() -> bool:
    """Return whether a supported Chromium browser has a normal window.

    Edge can leave extension workers and their sockets alive after the final
    browser window closes. This OS-level check also protects users whose older
    extension worker stayed resident across a DocuRandom update.
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        import ntpath
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        process_query_limited_information = 0x1000
        found = False

        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        @callback_type
        def inspect_window(hwnd, _lparam):
            nonlocal found
            if found or not user32.IsWindowVisible(hwnd):
                return not found
            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if not process_id.value:
                return True
            handle = kernel32.OpenProcess(
                process_query_limited_information, False, process_id.value
            )
            if not handle:
                return True
            try:
                capacity = wintypes.DWORD(32768)
                executable = ctypes.create_unicode_buffer(capacity.value)
                if kernel32.QueryFullProcessImageNameW(
                    handle, 0, executable, ctypes.byref(capacity)
                ) and ntpath.basename(executable.value).casefold() in {
                    "msedge.exe", "chrome.exe", "chromium.exe", "wayfern.exe",
                }:
                    found = True
                    return False
            finally:
                kernel32.CloseHandle(handle)
            return True

        user32.EnumWindows(inspect_window, 0)
        return found
    except Exception as exc:
        # Never make Flow unusable just because a Windows API probe is blocked.
        log.debug("Could not inspect visible Edge windows: %s", exc)
        return True


def _has_visible_edge_window() -> bool:
    """Return the cached Windows window probe for a short interval.

    /health is queried by every workspace monitor and also checks the bridge
    session a second time. The Windows API probe is intentionally dispatched to
    a daemon thread instead of running under the request thread's lock. A hung
    or slow ``EnumWindows``/``QueryFullProcessImageNameW`` call must never make
    every local ``/health`` request wait behind it. The extension heartbeat and
    its reported ``browserOpen`` state remain the authoritative fast path; the
    OS probe is only a short-lived safety cache for stale workers.
    """

    global _VISIBLE_EDGE_WINDOW_PROBE_INFLIGHT
    now = _time.monotonic()
    launch_probe = False
    with _VISIBLE_EDGE_WINDOW_CACHE_LOCK:
        cached = _VISIBLE_EDGE_WINDOW_CACHE
        if (
            cached is not None
            and now - cached[0] <= _VISIBLE_EDGE_WINDOW_CACHE_TTL_SECONDS
        ):
            return cached[1]
        # Before the first probe completes, an open socket/extension heartbeat
        # is safer than declaring every client offline. After a result exists,
        # keep using the last value while the next probe refreshes it.
        fallback = cached[1] if cached is not None else True
        if not _VISIBLE_EDGE_WINDOW_PROBE_INFLIGHT:
            _VISIBLE_EDGE_WINDOW_PROBE_INFLIGHT = True
            launch_probe = True

    if launch_probe:
        try:
            threading.Thread(
                target=_refresh_visible_edge_window_cache,
                name="flow-edge-window-probe",
                daemon=True,
            ).start()
        except Exception:
            # Thread creation itself must not turn a fast health request into
            # a permanent "probe in flight" state.
            with _VISIBLE_EDGE_WINDOW_CACHE_LOCK:
                _VISIBLE_EDGE_WINDOW_PROBE_INFLIGHT = False
    return fallback


def _refresh_visible_edge_window_cache() -> None:
    """Refresh the OS-level window cache without blocking HTTP/WebSocket work."""

    global _VISIBLE_EDGE_WINDOW_CACHE, _VISIBLE_EDGE_WINDOW_PROBE_INFLIGHT
    try:
        result = _has_visible_edge_window_uncached()
    except Exception as exc:
        # The OS probe is only a stale-worker safeguard. If Windows refuses or
        # stalls the probe, do not turn a healthy extension socket into an
        # outage; the next heartbeat will still update browserOpen.
        log.debug("Could not refresh visible Edge window probe: %s", exc)
        result = True
    with _VISIBLE_EDGE_WINDOW_CACHE_LOCK:
        _VISIBLE_EDGE_WINDOW_CACHE = (_time.monotonic(), bool(result))
        _VISIBLE_EDGE_WINDOW_PROBE_INFLIGHT = False


class ExtensionBridge:
    """WebSocket server that Chrome extensions connect to."""

    # Seconds to wait after server boot before allowing generation requests.
    # Prevents UNUSUAL_ACTIVITY errors caused by 40+ browsers reconnecting
    # simultaneously and the first request landing before Google stabilises.
    STARTUP_COOLDOWN = 10
    # The extension reports the presence of a real browser window with every
    # keepalive. Edge may keep Manifest V3 workers alive after its final window
    # is closed, so an open WebSocket alone is not proof that Flow is usable.
    CLIENT_HEARTBEAT_TIMEOUT = 75
    # Edge may report a new normal window a few event-loop turns after the
    # Flow worker reconnects.  Give that reconnect a short control-plane
    # window before returning the misleading "extension unavailable" error.
    CLIENT_RECONNECT_WAIT = 8.0
    # If the visible Flow job reaches its watchdog, request cancellation and
    # keep listening briefly for the extension's authoritative outcome.  This
    # is long enough for the content script to report whether the prompt was
    # already submitted, without allowing a stale UI lock to leak into the
    # next workspace.
    UI_CANCEL_GRACE = 30.0
    # A snapshot may need to inject the current content script and wake a
    # minimized renderer before it can answer. Keep its deadline above that
    # bounded recovery path; the desktop HTTP caller has a larger timeout.
    UI_PROJECT_SNAPSHOT_TIMEOUT = 180.0
    UI_QUOTA_BLOCK_SECONDS = 6 * 60 * 60

    def __init__(self):
        self._clients: dict[str, websockets.WebSocketServerProtocol] = {}  # client_id -> WebSocket
        self._tokens: dict[str, str] = {}         # client_id -> flowKey
        self._states: dict[str, str] = {}         # client_id -> state ('idle' | 'running')
        self._tiers: dict[str, str] = {}          # client_id -> sku ('G1_FREEMIUM' free | 'G1_TIER1' pro)
        self._credits: dict[str, int] = {}        # client_id -> last-known credit balance
        self._browser_open: dict[str, bool] = {}  # client_id -> at least one normal browser window
        self._flow_tab_counts: dict[str, int] = {}
        self._capabilities: dict[str, set[str]] = {}
        self._extension_versions: dict[str, str] = {}
        self._last_seen: dict[str, float] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self._connected = asyncio.Event()
        self._loop = None
        self._boot_time = _time.monotonic()  # for startup cooldown

        # Late/orphan-response reconciliation
        self._req_meta: dict[str, dict] = {}
        self._orphan_handler = None
        self._seen_ids: dict[str, bool] = {}

        # Client-specific rate limiting
        self._client_sems: dict[str, asyncio.Semaphore] = {}
        self._client_locks: dict[str, asyncio.Lock] = {}
        self._client_last_request_at: dict[str, float] = {}
        self._ui_quota_blocked_until: dict[str, float] = {}

        # A Flow client owns one visible project. Serialize every UI command
        # sent to that client so a second workspace cannot race a still
        # unwinding command and turn a recoverable wait into FLOW_UI_BUSY.
        # The lock is intentionally kept after disconnects: queued tasks may
        # still be unwinding and must be allowed to release it safely.
        self._ui_operation_locks: dict[str, asyncio.Lock] = {}
        self._ui_operation_queues: dict[str, list[tuple[str, str]]] = {}
        self._ui_operation_active: dict[str, str] = {}

    @property
    def _ws(self):
        """Compatibility property for legacy code that accesses bridge._ws.
        Returns the first connected WebSocket client."""
        for client_id in self.active_client_ids():
            ws = self._clients.get(client_id)
            if ws:
                return ws
        return None

    @property
    def _flow_key(self):
        """Compatibility property for legacy code that accesses bridge._flow_key.
        Returns the first captured token in the pool."""
        for client_id in self.active_client_ids():
            tok = self._tokens.get(client_id)
            if tok:
                return tok
        return None

    def _client_is_active(self, client_id: str, now: float | None = None) -> bool:
        """A usable client needs a live socket, fresh heartbeat and Edge window."""
        if client_id not in self._clients or not self._browser_open.get(client_id, True):
            return False
        last_seen = self._last_seen.get(client_id)
        if last_seen is None:
            # Compatibility for a connection registered by older code in tests.
            return True
        current = _time.monotonic() if now is None else now
        return current - last_seen <= self.CLIENT_HEARTBEAT_TIMEOUT

    def active_client_ids(self) -> list[str]:
        if not _has_visible_edge_window():
            return []
        now = _time.monotonic()
        return [cid for cid in self._clients if self._client_is_active(cid, now)]

    def _client_has_session(self, client_id: str) -> bool:
        """UI generation needs a Flow tab, not a captured legacy API token.

        Keep token readiness for legacy clients, but never let a cached token
        disguise a missing tab on the UI path selected by text-to-image.
        Callers also check socket/heartbeat/browser liveness.
        """
        if "ui_text_to_image" in self._capabilities.get(client_id, set()):
            return self._flow_tab_counts.get(client_id, 0) > 0
        return bool(self._tokens.get(client_id))

    def _forget_client(self, client_id: str, ws=None) -> None:
        if ws is not None and self._clients.get(client_id) is not ws:
            # A freshly reopened Edge window may already have re-registered
            # the same stable client id. Never let the old socket's finally
            # block erase that replacement connection.
            return
        self._clients.pop(client_id, None)
        self._tokens.pop(client_id, None)
        self._states.pop(client_id, None)
        self._tiers.pop(client_id, None)
        self._credits.pop(client_id, None)
        self._browser_open.pop(client_id, None)
        self._flow_tab_counts.pop(client_id, None)
        self._capabilities.pop(client_id, None)
        self._extension_versions.pop(client_id, None)
        self._last_seen.pop(client_id, None)
        self._client_sems.pop(client_id, None)
        self._client_locks.pop(client_id, None)
        self._client_last_request_at.pop(client_id, None)
        self._ui_quota_blocked_until.pop(client_id, None)
        if not self.active_client_ids():
            self._connected.clear()

    def _ui_lock_for(self, client_id: str) -> asyncio.Lock:
        return self._ui_operation_locks.setdefault(client_id, asyncio.Lock())

    @asynccontextmanager
    async def _ui_operation_slot(self, client_id: str, operation: str):
        """Run one operation at a time for a single Flow browser client.

        ``FlowGenerationCoordinator`` serializes complete workspace batches in
        the desktop process. This second boundary protects the browser client
        itself from direct API/MCP calls, recovery snapshots, and late
        handlers that arrive outside that desktop coordinator.
        """
        lock = self._ui_lock_for(client_id)
        queue = self._ui_operation_queues.setdefault(client_id, [])
        ticket = uuid.uuid4().hex
        was_busy = lock.locked()
        position = len(queue) + (1 if was_busy else 0)
        queue.append((ticket, operation))
        acquired = False
        try:
            if position > 1:
                log.info(
                    "Flow UI queue client=%s operation=%s position=%d",
                    client_id,
                    operation,
                    position,
                )
            await lock.acquire()
            acquired = True
            try:
                queue.remove((ticket, operation))
            except ValueError:
                pass
            self._ui_operation_active[client_id] = operation
            self._states[client_id] = "running"
            if position > 1:
                log.info(
                    "Flow UI dequeued client=%s operation=%s position=%d",
                    client_id,
                    operation,
                    position,
                )
            yield
        finally:
            try:
                queue.remove((ticket, operation))
            except ValueError:
                pass
            if self._ui_operation_active.get(client_id) == operation:
                self._ui_operation_active.pop(client_id, None)
            if acquired:
                # The inner request cleans up its own state before this
                # context exits. Keep the client marked busy if another
                # workspace is already waiting, so selection cannot leapfrog
                # the FIFO just after the first request releases its future.
                if client_id in self._clients or client_id in self._states:
                    self._states[client_id] = "running" if queue else "idle"
                lock.release()

    def ui_queue_snapshot(self) -> dict[str, dict[str, object]]:
        """Return safe, non-sensitive per-client Flow queue diagnostics."""
        client_ids = set(self._ui_operation_queues)
        client_ids.update(self._ui_operation_active)
        return {
            client_id: {
                "active": self._ui_operation_active.get(client_id, ""),
                "waiting": len(self._ui_operation_queues.get(client_id, [])),
            }
            for client_id in sorted(client_ids)
        }

    def _get_client_rate_limit(self, client_id):
        """Build or retrieve client-specific concurrency semaphore and spacing lock."""
        if client_id not in self._client_sems:
            self._client_sems[client_id] = asyncio.Semaphore(max(1, MAX_CONCURRENT_REQUESTS))
        if client_id not in self._client_locks:
            self._client_locks[client_id] = asyncio.Lock()
        return self._client_sems[client_id], self._client_locks[client_id]

    def supports_capability(self, capability: str) -> bool:
        """Whether a live browser client advertises a local capability."""
        wanted = str(capability or "").strip()
        if not wanted:
            return False
        return any(
            wanted in self._capabilities.get(client_id, set())
            for client_id in self.active_client_ids()
        )

    def _select_capable_client(
        self,
        capability: str,
        *,
        exclude_ids: set[str] | None = None,
    ) -> str | None:
        current = _time.monotonic()
        excluded = exclude_ids or set()
        candidates = [
            client_id
            for client_id in self.active_client_ids()
            if capability in self._capabilities.get(client_id, set())
            and client_id not in excluded
            and self._ui_quota_blocked_until.get(client_id, 0.0) <= current
        ]
        pinned = target_client_id_var.get()
        if pinned:
            candidates = [client_id for client_id in candidates if client_id == pinned]
        if not candidates:
            return None
        idle = [client_id for client_id in candidates if self._states.get(client_id) == "idle"]
        return random.choice(idle or candidates)

    async def _wait_for_capable_client(
        self,
        capability: str,
        *,
        client_id: str | None = None,
        timeout: float = CLIENT_RECONNECT_WAIT,
    ) -> str | None:
        """Wait briefly for an extension reconnect before failing control-plane work."""
        deadline = asyncio.get_running_loop().time() + min(
            max(0.0, float(timeout)),
            self.CLIENT_RECONNECT_WAIT,
        )
        while True:
            selected = client_id or self._select_capable_client(capability)
            if (
                selected
                and capability in self._capabilities.get(selected, set())
                and self._client_is_active(selected)
            ):
                return selected
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.2)

    @staticmethod
    def _ui_result_is_quota_limited(result: object) -> bool:
        try:
            payload = json.dumps(result, ensure_ascii=False).upper()
        except (TypeError, ValueError):
            payload = str(result).upper()
        return any(
            marker in payload
            for marker in (
                "ALL_IMAGE_MODELS_LIMITED",
                "MODEL_DAILY_LIMIT",
                "REQUIRED_IMAGE_MODEL_LIMITED",
                "FLOW_QUOTA",
                "USAGE LIMIT",
            )
        )

    def _mark_seen(self, req_id, max_keep=256):
        self._seen_ids[req_id] = True
        while len(self._seen_ids) > max_keep:
            oldest = next(iter(self._seen_ids))
            self._seen_ids.pop(oldest, None)

    def set_orphan_handler(self, handler):
        self._orphan_handler = handler

    def _remember_request(self, req_id, meta, max_keep=64):
        self._req_meta[req_id] = meta
        while len(self._req_meta) > max_keep:
            oldest = next(iter(self._req_meta))
            self._req_meta.pop(oldest, None)

    def _is_free(self, cid) -> bool:
        """Free-tier client? Freemium accounts are spent first so paid/pro
        credits are preserved. Unknown tier is treated as free (spend it early)."""
        return self._tiers.get(cid, "G1_FREEMIUM") != "G1_TIER1"

    def _select_client(self) -> str | None:
        """Selects the best connected client to route a request to.
        Prioritizes:
          1. Idle clients with captured tokens (free tier before pro).
          2. Any connected client with a captured token (free before pro).
          3. Any connected client.
        Free (G1_FREEMIUM) clients are always drained before paid (G1_TIER1)
        ones so pro credits are only touched once free credits run out.
        """
        connected_ids = self.active_client_ids()
        if not connected_ids:
            return None

        def _pick(pool):
            """Prefer free-tier clients in the pool; fall back to pro."""
            free = [c for c in pool if self._is_free(c)]
            if free:
                return random.choice(free)
            return random.choice(pool) if pool else None

        # Filter clients with active tokens
        with_tokens = [cid for cid in connected_ids if self._tokens.get(cid)]

        # Filter idle clients with tokens
        idle_with_tokens = [cid for cid in with_tokens if self._states.get(cid) == "idle"]
        if idle_with_tokens:
            return _pick(idle_with_tokens)

        if with_tokens:
            return _pick(with_tokens)

        return random.choice(connected_ids)

    def _select_client_for_cost(self, cost: int) -> str | None:
        """Pick a client that can actually afford a `cost`-credit job.

        Same free-before-pro priority as _select_client, but additionally skips
        any client whose last-known balance is below `cost`. This prevents
        routing a 15-credit video to a browser that only has 5 credits left
        while a 50-credit browser sits idle. Clients with no cached balance yet
        are treated as affordable (optimistic — the real credit gate re-checks
        on the pinned client before generating).
        """
        if cost <= 0:
            return self._select_client()

        connected_ids = self.active_client_ids()
        if not connected_ids:
            return None

        def _affordable(cid):
            bal = self._credits.get(cid)
            return bal is None or bal >= cost

        def _pick(pool):
            pool = [c for c in pool if _affordable(c)]
            if not pool:
                return None
            free = [c for c in pool if self._is_free(c)]
            if free:
                # Prefer the free client with the MOST credits so it drains cleanly.
                known = [c for c in free if self._credits.get(c) is not None]
                if known:
                    return max(known, key=lambda c: self._credits[c])
                return random.choice(free)
            known = [c for c in pool if self._credits.get(c) is not None]
            if known:
                return max(known, key=lambda c: self._credits[c])
            return random.choice(pool)

        with_tokens = [cid for cid in connected_ids if self._tokens.get(cid)]
        idle_with_tokens = [cid for cid in with_tokens if self._states.get(cid) == "idle"]

        pick = _pick(idle_with_tokens)
        if pick:
            return pick
        pick = _pick(with_tokens)
        if pick:
            return pick
        # Nobody provably affords it → fall back to normal selection (gate will 402).
        return self._select_client()

    async def send_message_to(self, client_id, msg):
        """Send message to a specific client in the pool."""
        ws = self._clients.get(client_id)
        if not ws:
            log.warning("Client %s not connected", client_id)
            return False
        try:
            if hasattr(ws, "send_text"):
                await ws.send_text(json.dumps(msg))
            else:
                await ws.send(json.dumps(msg))
            return True
        except Exception as e:
            log.warning("Failed to send message to client %s: %s", client_id, e)
            return False

    async def send_message(self, msg):
        """Broadcast message to all connected clients (legacy fallback)."""
        for cid in list(self._clients.keys()):
            await self.send_message_to(cid, msg)

    async def handle_fastapi_ws(self, ws):
        """Handle FastAPI WebSocket connection session."""
        # Setup temporary client ID until extension self-registers
        client_id = f"client_{uuid.uuid4().hex[:6]}"

        import os
        space_id = os.environ.get("SPACE_ID")
        if space_id:
            author, name = space_id.split("/")
            subdomain = f"{author.lower()}-{name.lower()}".replace("_", "-")
            callback_url = f"https://{subdomain}.hf.space/api/ext/callback"
        else:
            callback_url = f"http://{ws.url.netloc}/api/ext/callback"

        # Send callback config
        try:
            await ws.send_text(json.dumps({
                "type": "callback_config",
                "secret": "flow_secret",
                "callback_url": callback_url
            }))
        except Exception as e:
            log.warning("FastAPI WS initial setup failed: %s", e)
            return

        try:
            while True:
                raw = await ws.receive_text()
                data = json.loads(raw)
                client_id = await self._handle_message_with_client(data, ws, client_id)
        except Exception as e:
            log.warning("FastAPI WebSocket disconnected for client %s: %s", client_id, e)
        finally:
            self._forget_client(client_id, ws)

    async def start(self):
        """Start WS server and HTTP callback server."""
        self._loop = asyncio.get_event_loop()
        self._start_http_server()

        # Bind to 0.0.0.0 to allow other PCs on local network to connect
        self._ws_server = await websockets.serve(
            self._on_connect, "0.0.0.0", WS_PORT
        )
        log.info("WebSocket server on ws://0.0.0.0:%d", WS_PORT)
        log.info("HTTP callback on http://0.0.0.0:%d", HTTP_PORT)
        log.info("Waiting for Chrome extensions to connect...")

    async def wait_for_extension(self, timeout=90, max_retries=3, auto_fix=True):
        """Wait for a live UI session or a legacy token-based client."""
        start_time = self._loop.time()
        while not self.active_client_ids():
            if self._loop.time() - start_time > timeout:
                log.error("No extensions connected within %ds", timeout)
                return False
            await asyncio.sleep(0.5)

        # The UI client may never capture a legacy bearer token. Use the same
        # session rule as health_check, including after reconnect/registration.
        start_time = self._loop.time()
        while not await self.health_check():
            if self._loop.time() - start_time > 2.0:
                break
            await asyncio.sleep(0.2)

        if await self.health_check():
            return True

        if not auto_fix:
            return False

        log.info("Extensions connected but no usable session — requesting one Flow tab...")
        # One live client is enough; do not open a tab in every profile.
        clients = self.active_client_ids()
        if clients:
            await self._request_flow_tab_for(clients[0])

        # Wait for either the real Flow tab or a legacy token to arrive.
        start_time = self._loop.time()
        while not await self.health_check():
            if self._loop.time() - start_time > 20.0:
                break
            await asyncio.sleep(0.5)

        return await self.health_check()

    async def _wait_for_ws(self):
        while not self.active_client_ids():
            await asyncio.sleep(0.5)

    async def _wait_for_token(self, timeout):
        self._connected.clear()
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return any(self._tokens.get(cid) for cid in self.active_client_ids())

    async def _request_flow_tab_for(self, client_id, *, existing_only=False):
        """Ask a client to prepare Flow without creating reconnect tab storms.

        ``existing_only`` is used by the passive WebSocket reconnect path.  A
        reconnect is not user intent to open a browser tab: if the user is in
        the middle of Google sign-in, repeatedly creating Flow tabs can both
        interrupt authentication and make every workspace appear stuck in
        precheck.  Real work may still request one on-demand tab.
        """
        if not self._client_is_active(client_id):
            return
        if self._client_has_session(client_id):
            return
        try:
            log.info("Requesting client %s to open/refresh Flow tab...", client_id)
            await self.send_message_to(
                client_id,
                {
                    "method": "open_flow_tab",
                    "existingOnly": bool(existing_only),
                },
            )
            await asyncio.sleep(8)
            if self._client_has_session(client_id):
                return
            await self.send_message_to(
                client_id,
                {
                    "method": "refresh_flow_tab",
                    "existingOnly": bool(existing_only),
                },
            )
        except Exception as e:
            log.debug("Failed to request flow tab for client %s: %s", client_id, e)

    async def prepare_flow_tab(
        self,
        *,
        client_id: str | None = None,
        existing_only: bool = False,
        timeout: float = 20.0,
    ) -> dict:
        """Prepare one Flow tab without activating it or opening a native window.

        This is the control-plane path used by the desktop Flow button. It is
        deliberately separate from ``health_check``: a client with zero Flow
        tabs is still a valid client because the extension can create a
        background tab in an existing Edge window.
        """
        selected_client = await self._wait_for_capable_client(
            "ui_text_to_image",
            client_id=client_id,
            timeout=timeout,
        )
        if (
            not selected_client
            or "ui_text_to_image" not in self._capabilities.get(selected_client, set())
            or not self._client_is_active(selected_client)
        ):
            return {"status": 503, "error": "FLOW_UI_EXTENSION_NOT_AVAILABLE"}

        req_id = str(uuid.uuid4())
        future = self._loop.create_future()
        self._pending[req_id] = future
        self._remember_request(req_id, {
            "kind": "prepare_flow_tab",
            "client_id": selected_client,
        })
        try:
            sent = await self.send_message_to(
                selected_client,
                {
                    "id": req_id,
                    "method": "prepare_flow_tab",
                    "existingOnly": bool(existing_only),
                },
            )
            if not sent:
                return {
                    "status": 503,
                    "error": "FLOW_UI_EXTENSION_NOT_AVAILABLE",
                    "_client_id": selected_client,
                }
            response = await asyncio.wait_for(
                asyncio.shield(future), timeout=max(0.1, float(timeout))
            )
            if isinstance(response, dict):
                response["_client_id"] = selected_client
            return response
        except asyncio.TimeoutError:
            return {
                "status": 408,
                "error": "FLOW_PREPARE_TAB_TIMEOUT",
                "_client_id": selected_client,
            }
        finally:
            self._pending.pop(req_id, None)
            self._req_meta.pop(req_id, None)

    async def reload_extension(
        self,
        *,
        client_id: str | None = None,
        timeout: float = 10.0,
    ) -> dict:
        """Reload the resident MV3 worker without opening a chrome-extension tab."""
        selected_client = client_id or self._select_capable_client("ui_text_to_image")
        if (
            not selected_client
            or "ui_text_to_image" not in self._capabilities.get(selected_client, set())
            or not self._client_is_active(selected_client)
        ):
            return {"status": 503, "error": "FLOW_UI_EXTENSION_NOT_AVAILABLE"}

        req_id = str(uuid.uuid4())
        future = self._loop.create_future()
        self._pending[req_id] = future
        self._remember_request(req_id, {
            "kind": "reload_extension",
            "client_id": selected_client,
        })
        try:
            sent = await self.send_message_to(
                selected_client,
                {"id": req_id, "method": "reload_extension"},
            )
            if not sent:
                return {
                    "status": 503,
                    "error": "FLOW_UI_EXTENSION_NOT_AVAILABLE",
                    "_client_id": selected_client,
                }
            response = await asyncio.wait_for(
                asyncio.shield(future), timeout=max(0.1, float(timeout))
            )
            if isinstance(response, dict):
                response["_client_id"] = selected_client
            return response
        except asyncio.TimeoutError:
            return {
                "status": 408,
                "error": "FLOW_EXTENSION_RELOAD_TIMEOUT",
                "_client_id": selected_client,
            }
        finally:
            self._pending.pop(req_id, None)
            self._req_meta.pop(req_id, None)

    async def _force_refresh_client(self, client_id):
        """Force a client to reload its Flow tab and re-capture a fresh token,
        bypassing the extension's 50-min freshness cache. Used to self-heal a
        stale token (Google invalidates via inactivity before the cache expires).
        """
        if not self._client_is_active(client_id):
            return False
        if self._states.get(client_id) == "running":
            log.info(
                "Deferring hard Flow token refresh for %s because a UI/API job is active",
                client_id,
            )
            return False
        try:
            log.info("Force-refreshing Flow tab for client %s (stale token self-heal)...", client_id)
            self._tokens.pop(client_id, None)
            await self.send_message_to(client_id, {"method": "force_refresh", "force": True})
            await asyncio.sleep(6)
            return True
        except Exception as e:
            log.debug("Force-refresh failed for client %s: %s", client_id, e)
            return False

    async def _recover_ui_client(self, client_id):
        """Re-arm the visible Flow UI driver without reloading the page.

        This is the safe recovery path for UI timeouts and extension restarts:
        it preserves the current Flow project and any generation still running
        in the document. Hard token refresh remains isolated in
        ``_force_refresh_client`` for an explicit API 401 only.
        """
        if not self._client_is_active(client_id):
            return
        try:
            log.info("Recovering Flow UI in place for client %s...", client_id)
            await self.send_message_to(client_id, {"method": "recover_flow_ui"})
            await asyncio.sleep(1)
        except Exception as e:
            log.debug("Flow UI in-place recovery failed for client %s: %s", client_id, e)

    async def _request_flow_tab(self):
        """Fallback that triggers open_flow_tab on all connected clients."""
        for cid in self.active_client_ids():
            await self._request_flow_tab_for(cid)

    async def health_check(self):
        """Quick check using the same session requirements as generation."""
        return any(self._client_has_session(cid) for cid in self.active_client_ids())

    async def ui_client_status(
        self,
        *,
        client_id: str | None = None,
        timeout: float = 5.0,
    ) -> dict:
        """Read one extension's UI job state without entering the UI queue."""
        selected_client = client_id or self._select_capable_client("ui_text_to_image")
        if not selected_client or selected_client not in self.active_client_ids():
            return {"status": 503, "error": "FLOW_UI_EXTENSION_NOT_AVAILABLE"}

        req_id = str(uuid.uuid4())
        future = self._loop.create_future()
        self._pending[req_id] = future
        self._remember_request(req_id, {
            "kind": "ui_client_status",
            "client_id": selected_client,
        })
        try:
            sent = await self.send_message_to(
                selected_client,
                {"id": req_id, "method": "get_status"},
            )
            if not sent:
                return {
                    "status": 503,
                    "error": "FLOW_UI_EXTENSION_NOT_AVAILABLE",
                    "_client_id": selected_client,
                }
            response = await asyncio.wait_for(
                asyncio.shield(future), timeout=max(0.1, float(timeout))
            )
            if isinstance(response, dict):
                response["_client_id"] = selected_client
            return response
        except asyncio.TimeoutError:
            return {
                "status": 408,
                "error": "FLOW_UI_STATUS_TIMEOUT",
                "_client_id": selected_client,
            }
        finally:
            self._pending.pop(req_id, None)
            self._req_meta.pop(req_id, None)

    async def _on_connect(self, ws):
        """Handle raw WebSocket connections."""
        client_id = f"client_{uuid.uuid4().hex[:6]}"
        log.info("Extension connected via raw WebSocket!")
        try:
            async for raw in ws:
                data = json.loads(raw)
                client_id = await self._handle_message_with_client(data, ws, client_id)
        except Exception as e:
            log.warning("Raw WebSocket disconnected for client %s: %s", client_id, e)
        finally:
            self._forget_client(client_id, ws)

    async def _handle_message_with_client(self, data, ws, current_client_id):
        msg_type = data.get("type")
        self._last_seen[current_client_id] = _time.monotonic()

        if msg_type == "extension_ready":
            client_id = data.get("clientId") or current_client_id
            if client_id != current_client_id:
                self._clients.pop(current_client_id, None)
                self._tokens.pop(current_client_id, None)
                self._states.pop(current_client_id, None)
                self._tiers.pop(current_client_id, None)
                self._credits.pop(current_client_id, None)
                self._browser_open.pop(current_client_id, None)
                self._flow_tab_counts.pop(current_client_id, None)
                self._capabilities.pop(current_client_id, None)
                self._extension_versions.pop(current_client_id, None)
                self._last_seen.pop(current_client_id, None)
            self._clients[client_id] = ws
            self._states[client_id] = "idle"
            self._browser_open[client_id] = bool(data.get("browserOpen", True))
            self._flow_tab_counts[client_id] = int(data.get("flowTabCount") or 0)
            raw_capabilities = data.get("capabilities") or []
            self._capabilities[client_id] = {
                str(value).strip()
                for value in raw_capabilities
                if str(value).strip()
            }
            self._extension_versions[client_id] = str(
                data.get("extensionVersion") or ""
            ).strip()
            self._last_seen[client_id] = _time.monotonic()
            log.info("Extension client ready: %s (total: %d)", client_id, len(self._clients))

            # Reconnecting the extension is not permission to create a new tab.
            # Refresh only a Flow tab the browser explicitly reports as open.
            # When there is no such tab the UI/pipeline can open one exactly
            # once through its guarded launch path.
            if self._client_has_session(client_id):
                log.info("Client %s already has a usable Flow session", client_id)
            elif not data.get("flowKeyPresent") and self._flow_tab_counts[client_id] > 0:
                log.info(
                    "Client %s has no cached token; refreshing its existing Flow tab...",
                    client_id,
                )
                asyncio.create_task(
                    self._request_flow_tab_for(client_id, existing_only=True)
                )
            elif not data.get("flowKeyPresent"):
                log.info(
                    "Client %s has no token and no Flow tab; passive reconnect will not open one",
                    client_id,
                )
            return client_id

        elif msg_type == "token_captured":
            flow_key = data.get("flowKey")
            self._tokens[current_client_id] = flow_key
            log.info("Token captured for client: %s", current_client_id)
            self._connected.set()
            return current_client_id

        elif msg_type == "browser_state":
            browser_open = bool(data.get("browserOpen"))
            self._browser_open[current_client_id] = browser_open
            self._flow_tab_counts[current_client_id] = int(data.get("flowTabCount") or 0)
            if not browser_open:
                # Do not reuse an auth token captured by an Edge background
                # worker after the final normal browser window has closed.
                self._tokens.pop(current_client_id, None)
                self._states[current_client_id] = "off"
                if not self.active_client_ids():
                    self._connected.clear()
                log.info("Edge window closed for client %s; Flow session is inactive", current_client_id)
            return current_client_id

        elif msg_type == "ui_progress":
            request_id = str(data.get("requestId") or "").strip()
            meta = self._req_meta.get(request_id)
            if isinstance(meta, dict):
                phase = str(data.get("phase") or "").strip()
                if phase != meta.get("last_phase"):
                    log.info("Flow UI job=%s client=%s phase=%s progress=%s",
                             request_id, current_client_id, phase, data.get("progress"))
                meta["last_phase"] = phase
                meta["last_progress"] = data.get("progress")
                project_id = str(data.get("projectId") or "").strip()
                if (
                    8 <= len(project_id) <= 200
                    and project_id[0].isascii()
                    and project_id[0].isalnum()
                    and all(
                        char.isascii() and (char.isalnum() or char in "_-")
                        for char in project_id
                    )
                ):
                    meta["flow_project_id"] = project_id
                if phase in {"submitted", "submitted-no-image", "recover-after-submission"}:
                    meta["submitted"] = True
            return current_client_id

        elif msg_type in ("pong", "ping"):
            if msg_type == "ping":
                try:
                    await ws.send(json.dumps({"type": "pong"}))
                except Exception:
                    pass
            return current_client_id

        else:
            req_id = data.get("id")
            self._route_response(req_id, data)
            return current_client_id

    def _resolve_pending(self, req_id, data):
        self._route_response(req_id, data)

    def _route_response(self, req_id, data):
        """Route an extension response. Fast path resolves the waiting future;
        if the caller already timed out, hand the response to the orphan
        handler so a late-but-successful generation isn't lost. Delivery is
        idempotent: a redelivered id is acknowledged but not acted on twice."""
        if not req_id:
            return
        fut = self._pending.get(req_id)
        if fut is not None:
            if not fut.done():
                self._mark_seen(req_id)
                fut.set_result(data)
            return
        # Duplicate of an already-handled response (extension retried after the
        # ack was lost) — acknowledge silently, don't recover it again.
        if req_id in self._seen_ids:
            return
        # No waiting future: caller already gave up. Try to recover it.
        self._mark_seen(req_id)
        meta = self._req_meta.pop(req_id, None)
        if self._orphan_handler is not None:
            handler = self._orphan_handler
            coro = handler(data, meta or {})
            if self._loop is not None:
                asyncio.ensure_future(coro, loop=self._loop)
        else:
            log.warning("Dropped orphan response for %s (no handler)", req_id)

    def handle_http_callback(self, data):
        """Called from HTTP thread when extension sends callback."""
        req_id = data.get("id")
        if req_id:
            # Ack known ids (waiting, recoverable, or already-seen duplicates)
            # so the extension's durable outbox stops retrying. Route it on the
            # loop thread; _route_response dedups and recovers as needed.
            if (req_id in self._pending or req_id in self._req_meta
                    or req_id in self._seen_ids):
                self._loop.call_soon_threadsafe(
                    self._resolve_pending, req_id, data
                )
                return True
        if data.get("type") == "token_captured":
            client_id = data.get("clientId") or next(iter(self._clients.keys()), None)
            if client_id:
                self._tokens[client_id] = data.get("flowKey")
                self._loop.call_soon_threadsafe(self._connected.set)
            return True
        return False

    def _get_global_lock(self):
        """Build or retrieve global concurrency lock for IP-wide spacing."""
        if not hasattr(self, "_global_lock"):
            self._global_lock = asyncio.Lock()
            self._last_global_req_at = 0.0
        return self._global_lock

    async def _space_out_global_requests(self):
        """Ensure a minimum gap between requests across the entire client pool (IP level)."""
        import os
        global_interval = float(os.environ.get("GLOBAL_REQUEST_MIN_INTERVAL", "3.5"))
        lock = self._get_global_lock()
        async with lock:
            now = self._loop.time()
            wait = self._last_global_req_at + global_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_global_req_at = self._loop.time()

    def _get_global_sem(self):
        """Build or retrieve global request semaphore to limit parallel execution across all nodes."""
        if not hasattr(self, "_global_sem"):
            self._global_sem = asyncio.Semaphore(max(1, MAX_CONCURRENT_REQUESTS))
        return self._global_sem

    async def _wait_startup_cooldown(self):
        """Block until the startup cooldown has elapsed.
        Prevents burst requests right after server boot when all browsers
        reconnect simultaneously."""
        elapsed = _time.monotonic() - self._boot_time
        remaining = self.STARTUP_COOLDOWN - elapsed
        if remaining > 0:
            log.info("Startup cooldown: waiting %.1fs before first request...", remaining)
            await asyncio.sleep(remaining)

    async def api_request(self, url_path, body, captcha_action="VIDEO_GENERATION", method="POST", timeout=None, meta=None, client_id=None):
        """Send API request through a selected Chrome extension."""
        if not client_id:
            client_id = target_client_id_var.get() or self._select_client()
        if not client_id:
            return {"error": "No extensions connected"}

        log.info("Routing request to client: %s (total connected: %d)", client_id, len(self._clients))

        result = await self._run_api_request(client_id, url_path, body, captcha_action, method, timeout, meta)

        # Self-heal on a stale token: a 401 means Google invalidated this client's
        # token via inactivity. Force-refresh the tab and retry once on the same
        # client; if it still 401s, fail over to a different client.
        if self._is_unauthenticated(result):
            log.warning("Client %s returned 401 UNAUTHENTICATED — force-refreshing and retrying", client_id)
            refreshed = await self._force_refresh_client(client_id)
            if refreshed:
                result = await self._run_api_request(
                    client_id,
                    url_path,
                    body,
                    captcha_action,
                    method,
                    timeout,
                    meta,
                )

            if self._is_unauthenticated(result):
                fallback = self._select_client_excluding(client_id)
                if fallback:
                    log.warning("Client %s still 401 after refresh — failing over to %s", client_id, fallback)
                    result = await self._run_api_request(fallback, url_path, body, captcha_action, method, timeout, meta)
        return result

    async def ui_generate_images(
        self,
        *,
        prompt: str,
        aspect: str,
        count: int,
        model: str,
        timeout: float = 330.0,
        download_folder: str = "",
        workspace_id: str = "",
        flow_project_id: str = "",
        client_id: str | None = None,
        new_project: bool = False,
    ) -> dict:
        """Generate through the visible Flow project UI.

        Unlike ``api_request``, this path never needs an OAuth token or a
        reCAPTCHA token. The extension types into the signed-in Flow page and
        returns the browser download path and recoverable image URL.
        """
        attempted: set[str] = set()
        last_limited: dict | None = None
        while True:
            selected_client = client_id or self._select_capable_client(
                "ui_text_to_image",
                exclude_ids=attempted,
            )
            if not selected_client:
                return last_limited or {
                    "status": 503,
                    "error": "FLOW_UI_EXTENSION_NOT_AVAILABLE",
                }
            result = await self._ui_generate_images_on_client(
                client_id=selected_client,
                prompt=prompt,
                aspect=aspect,
                count=count,
                model=model,
                timeout=timeout,
                download_folder=download_folder,
                workspace_id=workspace_id,
                flow_project_id=flow_project_id,
                new_project=new_project,
            )
            if not self._ui_result_is_quota_limited(result):
                return result
            # A project-bound workspace must never fail over to another browser
            # profile: that profile cannot see the same Flow project. Fail closed
            # instead of generating into a different account.
            if client_id:
                return result
            attempted.add(selected_client)
            self._ui_quota_blocked_until[selected_client] = (
                _time.monotonic() + self.UI_QUOTA_BLOCK_SECONDS
            )
            last_limited = result
            log.warning(
                "Flow UI client %s exhausted both image models; trying another browser profile",
                selected_client,
            )

    async def ui_inspect_configuration(
        self,
        *,
        client_id: str | None = None,
        aspect: str = "landscape",
        timeout: float = 210.0,
    ) -> dict:
        """Verify the live Flow image controls without entering or submitting a prompt."""
        selected_client = client_id or self._select_capable_client("ui_text_to_image")
        if not selected_client or selected_client not in self.active_client_ids():
            return {"status": 503, "error": "FLOW_UI_EXTENSION_NOT_AVAILABLE"}
        if "ui_text_to_image" not in self._capabilities.get(selected_client, set()):
            return {"status": 503, "error": "FLOW_UI_EXTENSION_NOT_AVAILABLE"}
        async with self._ui_operation_slot(selected_client, "inspect_configuration"):
            return await self._ui_inspect_configuration_on_client(
                client_id=selected_client,
                aspect=aspect,
                timeout=timeout,
            )

    async def _ui_inspect_configuration_on_client(
        self,
        *,
        client_id: str,
        aspect: str,
        timeout: float,
    ) -> dict:
        """Run configuration inspection after the per-client queue grants a slot."""
        selected_client = client_id or self._select_capable_client("ui_text_to_image")
        if not selected_client or selected_client not in self.active_client_ids():
            return {"status": 503, "error": "FLOW_UI_EXTENSION_NOT_AVAILABLE"}
        if "ui_text_to_image" not in self._capabilities.get(selected_client, set()):
            return {"status": 503, "error": "FLOW_UI_EXTENSION_NOT_AVAILABLE"}

        req_id = str(uuid.uuid4())
        future = self._loop.create_future()
        self._pending[req_id] = future
        self._remember_request(req_id, {
            "kind": "ui_configuration_inspection",
            "client_id": selected_client,
        })
        self._states[selected_client] = "running"
        await self.send_message_to(
            selected_client,
            {
                "id": req_id,
                "method": "ui_inspect_configuration",
                "params": {
                    "jobId": req_id,
                    "aspect": aspect,
                },
            },
        )
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            if isinstance(result, dict):
                result["_client_id"] = selected_client
            return result
        except asyncio.TimeoutError:
            return {
                "status": 408,
                "error": "FLOW_UI_CONFIGURATION_INSPECTION_TIMEOUT",
                "_client_id": selected_client,
            }
        finally:
            self._pending.pop(req_id, None)
            if selected_client in self._states:
                self._states[selected_client] = "idle"

    async def ui_project_snapshot(
        self,
        *,
        client_id: str | None = None,
        prompts: list[str] | tuple[str, ...] | None = None,
        debug: bool = False,
        inspect_tile_prompts: bool = False,
        active_tab_only: bool = False,
        timeout: float = 180.0,
        workspace_id: str = "",
        flow_project_id: str = "",
    ) -> dict:
        """Read the currently visible Flow project without submitting anything.

        This is used to reconcile a durable ``submission_uncertain`` marker.
        The request is intentionally pinned to one live UI client and never
        opens a tab, reloads the page, or sends a prompt.
        """
        selected_client = client_id or self._select_capable_client("flow_project_history")
        if not selected_client or selected_client not in self.active_client_ids():
            return {"status": 503, "error": "FLOW_UI_EXTENSION_NOT_AVAILABLE"}
        if "flow_project_history" not in self._capabilities.get(selected_client, set()):
            return {"status": 503, "error": "FLOW_PROJECT_HISTORY_UNAVAILABLE"}
        async with self._ui_operation_slot(selected_client, "project_snapshot"):
            return await self._ui_project_snapshot_on_client(
                client_id=selected_client,
                prompts=prompts,
                debug=debug,
                inspect_tile_prompts=inspect_tile_prompts,
                active_tab_only=active_tab_only,
                timeout=timeout,
                workspace_id=workspace_id,
                flow_project_id=flow_project_id,
            )

    async def _ui_project_snapshot_on_client(
        self,
        *,
        client_id: str,
        prompts: list[str] | tuple[str, ...] | None,
        debug: bool,
        inspect_tile_prompts: bool,
        active_tab_only: bool,
        timeout: float,
        workspace_id: str = "",
        flow_project_id: str = "",
    ) -> dict:
        """Read a project after the per-client queue grants a slot."""
        selected_client = client_id or self._select_capable_client("flow_project_history")
        if not selected_client or selected_client not in self.active_client_ids():
            return {"status": 503, "error": "FLOW_UI_EXTENSION_NOT_AVAILABLE"}
        if "flow_project_history" not in self._capabilities.get(selected_client, set()):
            return {"status": 503, "error": "FLOW_PROJECT_HISTORY_UNAVAILABLE"}

        clean_prompts = [
            str(value).strip()
            for value in (prompts or [])
            if str(value).strip()
        ][:8]
        req_id = str(uuid.uuid4())
        future = self._loop.create_future()
        self._pending[req_id] = future
        self._remember_request(req_id, {
            "kind": "ui_project_snapshot",
            "client_id": selected_client,
        })
        self._states[selected_client] = "running"
        try:
            sent = await self.send_message_to(
                selected_client,
                {
                    "id": req_id,
                    "method": "ui_project_snapshot",
                    "params": {
                        "jobId": req_id,
                        "prompts": clean_prompts,
                        "debug": bool(debug),
                        "inspectTilePrompts": bool(inspect_tile_prompts),
                        "activeTabOnly": bool(active_tab_only),
                        "workspaceId": str(workspace_id or "").strip(),
                        "flowProjectId": str(flow_project_id or "").strip(),
                    },
                },
            )
            if not sent:
                return {
                    "status": 503,
                    "error": "FLOW_UI_EXTENSION_NOT_AVAILABLE",
                    "_client_id": selected_client,
                }
            result = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            if isinstance(result, dict):
                result["_client_id"] = selected_client
            return result
        except asyncio.TimeoutError:
            # Do not release the per-client queue immediately while the
            # extension may still be unwinding its read-only handler. Give it
            # the same cancellation window used by generation so the next
            # workspace cannot collide with a late response.
            cancel_id = str(uuid.uuid4())
            await self.send_message_to(
                selected_client,
                {
                    "id": cancel_id,
                    "method": "cancel_ui_job",
                    "params": {"jobId": req_id},
                },
            )
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(future), timeout=self.UI_CANCEL_GRACE
                )
                if isinstance(result, dict):
                    result["_client_id"] = selected_client
                return result
            except asyncio.TimeoutError:
                pass
            return {
                "status": 408,
                "error": "FLOW_UI_PROJECT_SNAPSHOT_TIMEOUT",
                "_client_id": selected_client,
            }
        finally:
            self._pending.pop(req_id, None)
            self._req_meta.pop(req_id, None)
            if selected_client in self._states:
                self._states[selected_client] = "idle"

    async def _ui_generate_images_on_client(
        self,
        *,
        client_id: str,
        prompt: str,
        aspect: str,
        count: int,
        model: str,
        timeout: float,
        download_folder: str,
        workspace_id: str = "",
        flow_project_id: str = "",
        new_project: bool = False,
    ) -> dict:
        """Queue one UI attempt behind all work for the same Flow client."""
        async with self._ui_operation_slot(client_id, "generate_images"):
            return await self._ui_generate_images_on_client_unlocked(
                client_id=client_id,
                prompt=prompt,
                aspect=aspect,
                count=count,
                model=model,
                timeout=timeout,
                download_folder=download_folder,
                workspace_id=workspace_id,
                flow_project_id=flow_project_id,
                new_project=new_project,
            )

    async def _ui_generate_images_on_client_unlocked(
        self,
        *,
        client_id: str,
        prompt: str,
        aspect: str,
        count: int,
        model: str,
        timeout: float,
        download_folder: str,
        workspace_id: str = "",
        flow_project_id: str = "",
        new_project: bool = False,
    ) -> dict:
        """Run one UI attempt pinned to one browser profile."""

        req_id = str(uuid.uuid4())
        future = self._loop.create_future()
        self._pending[req_id] = future
        self._remember_request(req_id, {
            "kind": "ui_text_to_image",
            "prompt": prompt,
            "client_id": client_id,
            "flow_project_id": "",
        })
        message = {
            "id": req_id,
            "method": "ui_generate_images",
                "params": {
                    "jobId": req_id,
                    "prompt": prompt,
                    "aspect": aspect,
                    "count": max(1, min(4, int(count or 1))),
                    "model": model,
                    "downloadFolder": download_folder,
                    "workspaceId": str(workspace_id or "").strip(),
                    "flowProjectId": str(flow_project_id or "").strip(),
                    "newProject": bool(new_project),
                    "delayMinSeconds": 3,
                    "delayMaxSeconds": 5,
                },
        }
        self._states[client_id] = "running"
        try:
            sent = await self.send_message_to(client_id, message)
            if not sent:
                return {"status": 503, "error": "FLOW_UI_EXTENSION_NOT_AVAILABLE", "_client_id": client_id}
            # Shield the future so the timeout does not erase a late but vital
            # "submitted" result.  Without this, the bridge used to tell the
            # desktop that a prompt was unsent, mark the client idle too early,
            # and the next workspace immediately hit FLOW_UI_BUSY.
            result = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            if isinstance(result, dict):
                result["_client_id"] = client_id
            return result
        except asyncio.TimeoutError:
            cancel_id = str(uuid.uuid4())
            await self.send_message_to(
                client_id,
                {
                    "id": cancel_id,
                    "method": "cancel_ui_job",
                    "params": {"jobId": req_id},
                },
            )
            try:
                # The original job response, rather than the cancellation ack,
                # carries promptOutcomes and is therefore the only safe source
                # for the no-resend decision.
                result = await asyncio.wait_for(
                    asyncio.shield(future), timeout=self.UI_CANCEL_GRACE
                )
                if isinstance(result, dict):
                    result["_client_id"] = client_id
                return result
            except asyncio.TimeoutError:
                meta = self._req_meta.get(req_id) or {}
                submitted = bool(meta.get("submitted"))
                last_phase = str(meta.get("last_phase") or "no-progress").strip()
                safely_unsent = (
                    not submitted
                    and last_phase.casefold() in _PRE_SUBMISSION_UI_PHASES
                )
                error = (
                    "FLOW_PROMPT_SUBMITTED_NO_IMAGE: FLOW_UI_TIMEOUT_AFTER_SUBMIT"
                    if submitted
                    else "FLOW_UI_TIMEOUT_UNSENT"
                    if safely_unsent
                    else "FLOW_UI_SUBMISSION_UNCERTAIN"
                )
                error += f": phase={last_phase}"
                project_id = str(meta.get("flow_project_id") or "").strip()
                if project_id:
                    error += f":project={project_id}"
                log.warning("Flow watchdog job=%s client=%s error=%s", req_id, client_id, error)
                return {
                    "status": 408,
                    "error": error,
                    "projectId": project_id,
                    "_client_id": client_id,
                }
        finally:
            self._pending.pop(req_id, None)
            if client_id in self._states:
                self._states[client_id] = "idle"

    @staticmethod
    def _is_unauthenticated(result) -> bool:
        """True if an extension response indicates a 401/expired-token error."""
        if not isinstance(result, dict):
            return False
        if result.get("status") == 401:
            return True
        data = result.get("data")
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and (err.get("code") == 401 or err.get("status") == "UNAUTHENTICATED"):
                return True
        return False

    def _select_client_excluding(self, exclude_id) -> str | None:
        """Pick another connected client with a token, skipping exclude_id."""
        candidates = [c for c in self.active_client_ids() if c != exclude_id and self._tokens.get(c)]
        if not candidates:
            return None
        free = [c for c in candidates if self._is_free(c)]
        return random.choice(free) if free else random.choice(candidates)

    async def _run_api_request(self, client_id, url_path, body, captcha_action, method, timeout, meta):
        """Execute a single request attempt against one client, applying the
        global + per-client rate limiting when a captcha action is involved."""
        if captcha_action:
            # Wait for startup cooldown if server just booted
            await self._wait_startup_cooldown()
            # Space out requests globally to protect against IP rate-limiting
            await self._space_out_global_requests()

            global_sem = self._get_global_sem()
            # Enforce global active slots (e.g. max 5 generation requests in flight)
            async with global_sem:
                sem, lock = self._get_client_rate_limit(client_id)
                async with sem:
                    await self._space_out_client_requests(client_id, lock)
                    return await self._do_api_request(client_id, url_path, body, captcha_action, method, timeout, meta)
        return await self._do_api_request(client_id, url_path, body, captcha_action, method, timeout, meta)

    async def _space_out_client_requests(self, client_id, lock):
        """Enforce a minimum gap between consecutive requests per client."""
        if REQUEST_MIN_INTERVAL <= 0:
            return
        async with lock:
            now = self._loop.time()
            last_req = self._client_last_request_at.get(client_id, 0.0)
            wait = last_req + REQUEST_MIN_INTERVAL - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._client_last_request_at[client_id] = self._loop.time()

    async def _do_api_request(self, client_id, url_path, body, captcha_action, method, timeout, meta):
        req_id = str(uuid.uuid4())
        future = self._loop.create_future()
        self._pending[req_id] = future
        self._remember_request(req_id, {
            "captcha_action": captcha_action,
            "url_path": url_path,
            "client_id": client_id,
            **(meta or {}),
        })

        url = f"{API_BASE}{url_path}?key={API_KEY}"
        ua = random.choice(USER_AGENTS)
        platform = '"macOS"' if "Macintosh" in ua else '"Windows"'

        msg = {
            "id": req_id,
            "method": "api_request",
            "params": {
                "url": url,
                "method": method,
                "headers": {
                    "accept": "*/*",
                    "content-type": "text/plain;charset=UTF-8",
                    "origin": CLIENT_CTX["origin"],
                    "referer": CLIENT_CTX["origin"] + "/",
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": platform,
                    "sec-fetch-dest": "empty",
                    "sec-fetch-mode": "cors",
                    "sec-fetch-site": "cross-site",
                    "user-agent": ua,
                },
                "body": body,
                "captchaAction": captcha_action,
            },
        }

        self._states[client_id] = "running"
        await self.send_message_to(client_id, msg)

        try:
            result = await asyncio.wait_for(future, timeout=timeout or API_REQUEST_TIMEOUT)
            if isinstance(result, dict):
                result["_client_id"] = client_id
                # Cache the account tier from any response that carries it (e.g.
                # a credits check) so _select_client can drain free before pro.
                rdata = result.get("data")
                if isinstance(rdata, dict) and rdata.get("sku"):
                    self._tiers[client_id] = rdata["sku"]
                if isinstance(rdata, dict) and "credits" in rdata:
                    try:
                        self._credits[client_id] = int(rdata["credits"])
                    except (TypeError, ValueError):
                        pass
            return result
        except asyncio.TimeoutError:
            return {"error": "TIMEOUT", "_client_id": client_id}
        finally:
            self._pending.pop(req_id, None)
            if client_id in self._states:
                self._states[client_id] = "idle"

    def _start_http_server(self):
        """Start HTTP server for extension callbacks on 0.0.0.0."""
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path == "/api/ext/callback":
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length)) if length else {}
                    bridge.handle_http_callback(body)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_GET(self):
                if self.path == "/health":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    # Calculate active clients status
                    active_clients = bridge.active_client_ids()
                    browser_open = any(
                        bridge._browser_open.get(cid, True) for cid in active_clients
                    )
                    flow_tab_count = sum(
                        max(0, int(bridge._flow_tab_counts.get(cid, 0)))
                        for cid in active_clients
                    )
                    capabilities = sorted({
                        capability
                        for cid in active_clients
                        for capability in bridge._capabilities.get(cid, set())
                    })
                    self.wfile.write(json.dumps({
                        "status": "healthy" if len(active_clients) > 0 else "offline",
                        "extension_connected": len(active_clients) > 0,
                        "has_flow_key": any(bridge._tokens.get(cid) for cid in active_clients),
                        "browser_open": browser_open,
                        "flow_tab_count": flow_tab_count,
                        "capabilities": capabilities,
                        "ui_queue": bridge.ui_queue_snapshot(),
                        "extension_version": next(
                            (
                                bridge._extension_versions.get(cid, "")
                                for cid in active_clients
                                if bridge._extension_versions.get(cid, "")
                            ),
                            "",
                        ),
                        "clients": [{
                            "id": cid,
                            "state": bridge._states.get(cid),
                            "has_token": bridge._tokens.get(cid) is not None,
                            "browser_open": bridge._browser_open.get(cid, True),
                            "flow_tab_count": bridge._flow_tab_counts.get(cid, 0),
                            "ui_queue": bridge.ui_queue_snapshot().get(
                                cid,
                                {"active": "", "waiting": 0},
                            ),
                            "capabilities": sorted(bridge._capabilities.get(cid, set())),
                            "ui_quota_blocked": (
                                bridge._ui_quota_blocked_until.get(cid, 0.0)
                                > _time.monotonic()
                            ),
                        } for cid in active_clients]
                    }).encode())
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_OPTIONS(self):
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def log_message(self, *args):
                pass

        # Bind to 0.0.0.0 to enable callback routing from different devices
        # Health is polled by every workspace monitor.  A single slow Windows
        # window probe or a burst of simultaneous probes must not queue all
        # later requests behind one handler and make the desktop report a
        # false backend timeout.  Keep the same loopback/API contract, but let
        # independent health/callback requests progress concurrently.
        server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

    async def close(self):
        self._ws_server.close()
        await self._ws_server.wait_closed()
