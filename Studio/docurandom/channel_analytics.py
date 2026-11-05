"""Read-only Studio analytics via accessibility; no Runtime.evaluate or orders."""
from __future__ import annotations

from datetime import datetime, timezone
import re
import threading
import time
import unicodedata
from urllib.parse import urlsplit

from .channel_learning import CHANNEL_RE
from .donut_mcp import DonutMcpClient
from .youtube_like_monitor import _read_debug_targets
from .youtube_upload import _CdpSession, ProfileUploadCoordinator


def ax_texts(nodes: list[dict]) -> list[dict]:
    """Document order, main region only. Ignore duplicate InlineTextBox nodes."""
    by = {n.get("nodeId"): n for n in nodes}
    root = next((n for n in nodes if n.get("role", {}).get("value") == "main"), None)
    if root is None:
        return []
    result = []
    seen = set()
    def walk(key):
        if key in seen:
            return
        seen.add(key)
        node = by.get(key, {})
        role = node.get("role", {}).get("value", "")
        if not node.get("ignored") and role in {"StaticText", "heading", "link", "tab", "button"}:
            name = str(node.get("name", {}).get("value", "")).strip()
            if name:
                item = {"role": role, "text": name}
                props = {p.get("name"): p.get("value", {}).get("value") for p in node.get("properties", [])}
                if role == "link" and isinstance(props.get("url"), str):
                    item["url"] = props["url"]
                result.append(item)
        for child in node.get("childIds", []):
            walk(child)
    walk(root["nodeId"])
    return result


def parse_number(raw: str, locale: str) -> float | None:
    value = unicodedata.normalize("NFKC", raw).strip().lower().replace("\u2009", " ")
    if value in {"—", "-", "", "n/a", "(—)"}:
        return None
    value = value.strip("() ")
    multiplier = 1
    for suffix, scale in [("triệu", 1e6), ("nghìn", 1e3), ("億", 1e8), ("万", 1e4), ("m", 1e6), ("k", 1e3)]:
        if value.endswith(suffix):
            value = value[:-len(suffix)].strip(); multiplier = scale; break
    value = value.rstrip("% ").replace(" ", "")
    if locale.startswith("vi"):
        value = value.replace(".", "").replace(",", ".")
    else:
        value = value.replace(",", "")
    if not re.fullmatch(r"\d+(?:\.\d+)?", value):
        return None
    return float(value) * multiplier


METRICS = {
    "monthly_audience": ("khán giả hằng tháng", "monthly audience", "月間視聴者数"),
    "views": ("số lượt xem", "lượt xem", "views", "視聴回数"),
    "watch_hours": ("thời gian xem (giờ)", "watch time (hours)", "総再生時間（単位: 時間）"),
    "subscribers_net": ("số người đăng ký", "subscribers", "チャンネル登録者"),
    "impressions": ("lượt hiển thị", "số lượt hiển thị", "impressions", "インプレッション数"),
    "ctr_percent": ("tỷ lệ nhấp của số lượt hiển thị", "impressions click-through rate", "インプレッションのクリック率"),
    "unique_viewers": ("số người xem riêng biệt", "unique viewers", "ユニーク視聴者数"),
    "returning_viewers": ("người xem cũ", "returning viewers", "リピーター"),
}


def snapshot_from_ax(nodes: list[dict], *, channel_id: str, url: str, report: str) -> dict:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "studio.youtube.com" or not parsed.path.startswith("/channel/" + channel_id + "/analytics/"):
        raise ValueError("Analytics channel mismatch or login required")
    rows = ax_texts(nodes)
    texts = [r["text"] for r in rows if r["role"] == "StaticText"]
    period = next((t for t in texts if re.fullmatch(r"(?:\d+ ngày qua|Last \d+ days|過去 \d+ 日間)", t)), None)
    if not period or not any(r["role"] == "tab" for r in rows):
        raise ValueError("Analytics report/range not ready")
    locale = "vi" if "ngày qua" in period else ("ja" if "過去" in period else "en")
    metrics = {}; availability = {}
    # Only primary KPIs. Secondary tables/realtime/latest-upload have different scopes.
    stop = next((i for i, t in enumerate(texts) if t in {
        "Nội dung hàng đầu trong khoảng thời gian này", "Thời gian thực", "Your top content in this period", "Realtime"
    }), len(texts))
    primary = texts[:stop]
    for metric, labels in METRICS.items():
        candidates = []
        for i, text in enumerate(primary[:-1]):
            if text.casefold() in labels:
                raw = primary[i + 1]
                if raw.casefold() in labels and i + 2 < len(primary):
                    raw = primary[i + 2]
                number = parse_number(raw, locale)
                if number is not None:
                    candidates.append((number, raw))
        values = {c[0] for c in candidates}
        metrics[metric] = next(iter(values)) if len(values) == 1 else None
        availability[metric] = ("observed" if len(values) == 1 else "not_exposed_or_ambiguous")
    dates = next((t for t in texts if re.search(r"\d.*(?:thg|202\d|年)", t)
                  and ("–" in t or " - " in t or "～" in t)), "")
    for name in ["retention_curve", "retention_30s", "traffic_sources", "geography", "age_gender", "devices", "viewer_activity", "audience_interests"]:
        availability[name] = "raw_report_only_not_structured"
    return {"channel_id": channel_id, "collected_at": datetime.now(timezone.utc).isoformat(),
            "source_url": url, "report_range": {"label": period, "dates": dates},
            "report": report, "metrics": metrics, "availability": availability,
            "observations": rows, "locale": locale,
            "engagement_provenance": "may_include_assisted_engagement_do_not_infer_organic_quality"}


class StudioAnalyticsReader:
    def __init__(self, endpoint, coordinator=None):
        self.endpoint = endpoint
        self.coordinator = coordinator or ProfileUploadCoordinator()

    def collect(self, profile_id: str, channel_id: str, *, cancel_event=None, progress=None) -> list[dict]:
        if not CHANNEL_RE.fullmatch(channel_id):
            raise ValueError("Exact channel required")
        cancel = cancel_event or threading.Event(); report = progress or (lambda _: None)
        lease = self.coordinator.try_acquire(profile_id, cancel, job_kind="monitor", priority=0)
        if lease is None:
            raise RuntimeError("Analytics deferred: profile reserved for upload/Public")
        snapshots = []; owned_targets = []; started = False
        with lease, DonutMcpClient(self.endpoint, timeout=15, shared_session=False) as client:
            client.require_docurandom_local()
            try:
                if not client.get_profile(profile_id).process_id:
                    # Launch blank; create a separate owned target instead of navigating a user's tab.
                    client.run_profile(profile_id, "about:blank"); started = True
                deadline = time.monotonic() + 90
                for tab in ("overview", "content", "build_audience"):
                    if cancel.is_set() or time.monotonic() >= deadline:
                        break
                    url = f"https://studio.youtube.com/channel/{channel_id}/analytics/tab-{tab}/period-default"
                    created = client.cdp_command(profile_id, "Target.createTarget", {"url": url})
                    target_id = str(created["targetId"]); owned_targets.append(target_id)
                    target = next(t for t in _read_debug_targets(client.get_debug_connection(profile_id)) if t.get("id") == target_id)
                    with _CdpSession(target["webSocketDebuggerUrl"]) as cdp:
                        tab_started = time.monotonic()
                        while time.monotonic() < min(deadline, tab_started + 25) and not cancel.is_set():
                            frame = cdp.command("Page.getFrameTree", timeout=5)["frameTree"]["frame"]
                            current_url = frame.get("url", "")
                            if "accounts.google.com" in current_url:
                                raise RuntimeError("YouTube login required; analytics stopped")
                            data = cdp.command("Accessibility.getFullAXTree", timeout=5)
                            try:
                                snapshot = snapshot_from_ax(data.get("nodes", []), channel_id=channel_id, url=current_url, report=tab)
                            except ValueError:
                                if cancel.wait(0.75): break
                                continue
                            if (time.monotonic() - tab_started < 3
                                    or sum(r["role"] == "StaticText" for r in snapshot["observations"]) < 20):
                                if cancel.wait(0.75): break
                                continue
                            snapshots.append(snapshot); report(f"Analytics {tab}: saved observed report and missing-data flags")
                            break
                    client.cdp_command(profile_id, "Target.closeTarget", {"targetId": target_id})
                    owned_targets.remove(target_id)
                if not snapshots:
                    raise RuntimeError("No verified analytics report available")
                return snapshots
            finally:
                for target_id in owned_targets:
                    try: client.cdp_command(profile_id, "Target.closeTarget", {"targetId": target_id})
                    except Exception: pass
                # Close a profile only if we opened it AND it contains no user's page.
                if started:
                    try:
                        pages = [t for t in _read_debug_targets(client.get_debug_connection(profile_id))
                                 if t.get("type") == "page" and t.get("url") not in {"about:blank", "chrome://newtab/"}]
                        if not pages:
                            client.call_tool("batch_stop_profiles", {"profile_ids": [profile_id]}, timeout=15)
                    except Exception: pass
