from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Collection, Literal


GIB = 1024**3
HARDWARE_H264_ENCODERS = ("h264_nvenc", "h264_amf", "h264_qsv")
EncoderStage = Literal["scene", "final"]
GLOBAL_RENDER_LIMIT_MIN = 1
# The queue is shared by all workspaces.  The host recommendation remains
# conservative, but the user can deliberately choose a larger queue when
# several finished workspaces should progress together.
GLOBAL_RENDER_LIMIT_MAX = 12
GLOBAL_RENDER_LIMIT_FILENAME = "global_render_limit.json"
GLOBAL_CHATGPT_QUEUE_FILENAME = "global_chatgpt_queue.json"
GLOBAL_CHATGPT_LIMIT_MIN = 1
GLOBAL_CHATGPT_LIMIT_MAX = 20
# Uploads share Donut's local browser-session limit. Keep one Studio profile
# active at a time across all workspaces.
YOUTUBE_UPLOAD_LIMIT_MIN = 1
YOUTUBE_UPLOAD_LIMIT_MAX = 1


@dataclass(frozen=True)
class HostResources:
    """CPU and physical-memory capacity visible to this process."""

    logical_cpus: int
    total_ram_bytes: int
    available_ram_bytes: int
    # These fields are optional for backwards compatibility with callers that
    # construct HostResources directly.  They let the scheduler protect a
    # local Minecraft/Java server without changing any server configuration.
    minecraft_running: bool = False
    java_process_count: int = 0
    pagefile_total_bytes: int = 0
    pagefile_available_bytes: int = 0

    @property
    def total_ram_gib(self) -> float:
        return self.total_ram_bytes / GIB

    @property
    def available_ram_gib(self) -> float:
        return self.available_ram_bytes / GIB

    @property
    def pagefile_used_bytes(self) -> int:
        if self.pagefile_total_bytes <= 0:
            return 0
        return max(0, self.pagefile_total_bytes - self.pagefile_available_bytes)

    @property
    def pagefile_pressure(self) -> bool:
        """Whether paging headroom is low enough to hurt render stability."""

        total = max(0, self.pagefile_total_bytes)
        available = max(0, min(total, self.pagefile_available_bytes))
        if total <= 0:
            return False
        return available < max(1 * GIB, total // 10)

    @property
    def memory_pressure(self) -> bool:
        """Conservative physical/pagefile pressure signal for all schedulers."""

        total = max(0, self.total_ram_bytes)
        available = max(0, min(total, self.available_ram_bytes))
        if total <= 0:
            return True
        # Keep at least 4 GiB and roughly 12% of RAM free for the OS, browser
        # profiles and native allocations (including modded Java servers).
        physical_pressure = available < max(4 * GIB, total * 0.12)
        return bool(physical_pressure or self.pagefile_pressure)


@dataclass(frozen=True)
class RenderRuntimeProfile:
    """A conservative high-quality profile for concurrent FFmpeg renders."""

    host: HostResources
    encoder: str
    hardware_accelerated: bool
    recommended_jobs: int
    threads_per_job: int
    encoder_label: str

    @property
    def recommended_concurrency(self) -> int:
        """Alias used by queue/UI code."""

        return self.recommended_jobs

    def encoding_args(self, stage: EncoderStage = "final") -> list[str]:
        return encoder_args(self, stage)


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _windows_memory() -> tuple[int, int] | None:
    try:
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel32.GlobalMemoryStatusEx
        function.argtypes = [ctypes.POINTER(_MemoryStatusEx)]
        function.restype = ctypes.c_int
        if not function(ctypes.byref(status)):
            return None
        return int(status.ullTotalPhys), int(status.ullAvailPhys)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _windows_pagefile() -> tuple[int, int] | None:
    """Read committed/pagefile capacity without changing _windows_memory()."""

    try:
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel32.GlobalMemoryStatusEx
        function.argtypes = [ctypes.POINTER(_MemoryStatusEx)]
        function.restype = ctypes.c_int
        if not function(ctypes.byref(status)):
            return None
        return int(status.ullTotalPageFile), int(status.ullAvailPageFile)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _proc_memory() -> tuple[int, int] | None:
    try:
        values: dict[str, int] = {}
        with Path("/proc/meminfo").open("r", encoding="ascii") as stream:
            for line in stream:
                key, separator, remainder = line.partition(":")
                if not separator:
                    continue
                match = re.search(r"\d+", remainder)
                if match:
                    values[key] = int(match.group()) * 1024
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        if not available:
            available = sum(
                values.get(key, 0) for key in ("MemFree", "Buffers", "Cached")
            )
        if total > 0:
            return total, min(total, max(0, available))
    except (OSError, ValueError):
        pass
    return None


def _sysconf_memory() -> tuple[int, int] | None:
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total_pages = int(os.sysconf("SC_PHYS_PAGES"))
        try:
            available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        except (OSError, ValueError):
            available_pages = total_pages
        total = page_size * total_pages
        available = page_size * available_pages
        if total > 0:
            return total, min(total, max(0, available))
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return None


def _proc_pagefile() -> tuple[int, int] | None:
    try:
        values: dict[str, int] = {}
        with Path("/proc/meminfo").open("r", encoding="ascii") as stream:
            for line in stream:
                key, separator, remainder = line.partition(":")
                if not separator:
                    continue
                match = re.search(r"\d+", remainder)
                if match:
                    values[key] = int(match.group()) * 1024
        total = values.get("SwapTotal", 0)
        free = values.get("SwapFree", 0)
        if total > 0:
            return total, min(total, max(0, free))
    except (OSError, ValueError):
        pass
    return None


_MINECRAFT_MARKERS = (
    "minecraft",
    "server.jar",
    "neoforge",
    "forge",
    "fabric",
    "quilt",
    "paper.jar",
    "purpur.jar",
    "folia",
)
_PROCESS_PROBE_LOCK = threading.Lock()
_PROCESS_PROBE_CACHE: tuple[float, int, bool] | None = None


def _process_rows_from_json(raw: str) -> list[dict[str, str]]:
    try:
        decoded = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(decoded, dict):
        decoded = [decoded]
    if not isinstance(decoded, list):
        return []
    rows: list[dict[str, str]] = []
    for item in decoded:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "name": str(item.get("Name") or item.get("name") or ""),
                "command_line": str(
                    item.get("CommandLine") or item.get("command_line") or ""
                ),
            }
        )
    return rows


def _detect_java_processes() -> tuple[int, bool]:
    """Return (Java process count, Minecraft-like Java process present).

    This probe is deliberately read-only.  It uses a short cache because the
    UI asks for a runtime profile from several worker threads while a render
    is starting.  A custom Java launcher is still counted, but is only treated
    as Minecraft when its command line contains a known server/client marker.
    """

    global _PROCESS_PROBE_CACHE
    now = time.monotonic()
    with _PROCESS_PROBE_LOCK:
        cached = _PROCESS_PROBE_CACHE
        if cached is not None and now - cached[0] < 5.0:
            return cached[1], cached[2]

    rows: list[dict[str, str]] = []
    if os.name == "nt":
        command = (
            "$items=@(Get-CimInstance Win32_Process "
            "-Filter \"Name='java.exe' OR Name='javaw.exe'\" | "
            "Select-Object Name,CommandLine); "
            "ConvertTo-Json -Compress -InputObject $items"
        )
        try:
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=2.5,
                creationflags=_creation_flags(),
            )
            rows = _process_rows_from_json(completed.stdout)
        except (OSError, subprocess.SubprocessError):
            rows = []
        if not rows:
            # Some hardened Windows images disable CIM/PowerShell process
            # queries.  tasklist cannot expose command lines, so classify any
            # Java process conservatively as a protected workload rather than
            # risking a render burst beside a Minecraft server.
            fallback_count = 0
            for image in ("java.exe", "javaw.exe"):
                try:
                    completed = subprocess.run(
                        ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        check=False,
                        timeout=1.5,
                        creationflags=_creation_flags(),
                    )
                except (OSError, subprocess.SubprocessError):
                    continue
                for line in (completed.stdout or "").splitlines():
                    if f'"{image.casefold()}"' in line.casefold():
                        fallback_count += 1
            if fallback_count:
                rows = [
                    {"name": "java", "command_line": "minecraft-fallback"}
                    for _ in range(fallback_count)
                ]
    else:
        try:
            completed = subprocess.run(
                ["ps", "-eo", "comm=,args="],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=1.5,
            )
            for line in (completed.stdout or "").splitlines():
                name, _, command_line = line.strip().partition(" ")
                if name.casefold() in {"java", "javaw"}:
                    rows.append({"name": name, "command_line": command_line})
        except (OSError, subprocess.SubprocessError):
            rows = []

    count = len(rows)
    minecraft = any(
        any(marker in row["command_line"].casefold() for marker in _MINECRAFT_MARKERS)
        for row in rows
    )
    result = (count, minecraft)
    with _PROCESS_PROBE_LOCK:
        _PROCESS_PROBE_CACHE = (time.monotonic(), count, minecraft)
    return result


def detect_host_resources() -> HostResources:
    logical_cpus = max(1, int(os.cpu_count() or 1))
    memory: tuple[int, int] | None = None
    pagefile: tuple[int, int] | None = None
    if os.name == "nt":
        memory = _windows_memory()
        pagefile = _windows_pagefile()
    else:
        memory = _proc_memory() or _sysconf_memory()
        pagefile = _proc_pagefile()

    # An unknown memory capacity must not unlock unsafe parallelism. Eight GiB
    # is a deliberately conservative fallback that still permits one render.
    total, available = memory or (8 * GIB, 8 * GIB)
    pagefile_total, pagefile_available = pagefile or (0, 0)
    java_count, minecraft_running = _detect_java_processes()
    return HostResources(
        logical_cpus,
        total,
        min(total, available),
        minecraft_running=minecraft_running,
        java_process_count=java_count,
        pagefile_total_bytes=max(0, pagefile_total),
        pagefile_available_bytes=max(0, min(pagefile_total, pagefile_available)),
    )


def clamp_global_render_limit(value: object, *, default: int = 1) -> int:
    """Normalize the one process-wide render limit stored outside workspaces."""

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(GLOBAL_RENDER_LIMIT_MIN, min(GLOBAL_RENDER_LIMIT_MAX, parsed))


def global_render_limit_path() -> Path:
    # Import lazily so resource detection remains a small, standalone module.
    from .storage import app_data_dir

    return app_data_dir() / GLOBAL_RENDER_LIMIT_FILENAME


def load_global_render_limit(
    default: int,
    *,
    path: str | Path | None = None,
) -> int:
    """Load the shared limit, falling back safely when the file is absent/bad."""

    target = Path(path) if path is not None else global_render_limit_path()
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return clamp_global_render_limit(value.get("parallel_videos"), default=default)
    except (OSError, TypeError, ValueError):
        pass
    return clamp_global_render_limit(default)


def save_global_render_limit(
    value: object,
    *,
    path: str | Path | None = None,
) -> int:
    """Persist the shared render limit atomically and return its normalized value."""

    from .storage import atomic_write_json

    normalized = clamp_global_render_limit(value)
    target = Path(path) if path is not None else global_render_limit_path()
    atomic_write_json(
        target,
        {
            "version": 1,
            "parallel_videos": normalized,
        },
    )
    return normalized


def global_chatgpt_queue_path() -> Path:
    """Return the machine-wide ChatGPT queue preference outside workspaces."""

    from .storage import app_data_dir

    return app_data_dir() / GLOBAL_CHATGPT_QUEUE_FILENAME


def load_global_chatgpt_serial(
    default: bool = True,
    *,
    path: str | Path | None = None,
) -> bool:
    """Load whether every workspace must share one serial ChatGPT turn."""

    target = Path(path) if path is not None else global_chatgpt_queue_path()
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("serial_workspaces"), bool):
            return bool(value["serial_workspaces"])
    except (OSError, TypeError, ValueError):
        pass
    return bool(default)


def save_global_chatgpt_serial(
    enabled: object,
    *,
    path: str | Path | None = None,
) -> bool:
    """Persist the machine-wide ChatGPT queue preference atomically."""

    from .storage import atomic_write_json

    normalized = bool(enabled)
    target = Path(path) if path is not None else global_chatgpt_queue_path()
    atomic_write_json(
        target,
        {
            "version": 1,
            "serial_workspaces": normalized,
        },
    )
    return normalized


def clamp_global_chatgpt_limit(value: object, *, default: int = 1) -> int:
    """Normalize the shared number of ChatGPT turns allowed at once."""

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(GLOBAL_CHATGPT_LIMIT_MIN, min(GLOBAL_CHATGPT_LIMIT_MAX, parsed))


def load_global_chatgpt_limit(
    default: int = 1,
    *,
    path: str | Path | None = None,
) -> int:
    """Load the numeric ChatGPT fan-out, including the old boolean format."""

    target = Path(path) if path is not None else global_chatgpt_queue_path()
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            if "parallel_sessions" in value:
                return clamp_global_chatgpt_limit(
                    value.get("parallel_sessions"),
                    default=default,
                )
            if isinstance(value.get("serial_workspaces"), bool):
                if value["serial_workspaces"]:
                    return GLOBAL_CHATGPT_LIMIT_MIN
                return clamp_global_chatgpt_limit(default)
    except (OSError, TypeError, ValueError):
        pass
    return clamp_global_chatgpt_limit(default)


def save_global_chatgpt_limit(
    value: object,
    *,
    path: str | Path | None = None,
) -> int:
    """Persist the shared numeric ChatGPT fan-out atomically."""

    from .storage import atomic_write_json

    normalized = clamp_global_chatgpt_limit(value)
    target = Path(path) if path is not None else global_chatgpt_queue_path()
    atomic_write_json(
        target,
        {
            "version": 2,
            "parallel_sessions": normalized,
            # Keep the legacy field so older builds continue to fail safe.
            "serial_workspaces": normalized == GLOBAL_CHATGPT_LIMIT_MIN,
        },
    )
    return normalized


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def parse_ffmpeg_encoders(output: str) -> set[str]:
    """Extract encoder names from ``ffmpeg -encoders`` output."""

    names: set[str] = set()
    pattern = re.compile(r"^\s*[VAS][A-Z.]{5}\s+([A-Za-z0-9][A-Za-z0-9_.-]*)\b")
    for line in output.splitlines():
        match = pattern.match(line)
        if match:
            names.add(match.group(1))
    return names


def list_ffmpeg_encoders(ffmpeg_path: str | Path, timeout: float = 10.0) -> set[str]:
    try:
        completed = subprocess.run(
            [str(ffmpeg_path), "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    return parse_ffmpeg_encoders(completed.stdout or "")


def smoke_probe_encoder(
    ffmpeg_path: str | Path,
    encoder: str,
    timeout: float = 12.0,
) -> bool:
    """Actually initialize an encoder and encode frames; listing is insufficient."""

    if encoder not in HARDWARE_H264_ENCODERS:
        return False
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=128x72:r=24:d=0.15,format=nv12",
        "-an",
        "-frames:v",
        "3",
        "-c:v",
        encoder,
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
            creationflags=_creation_flags(),
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def choose_h264_encoder(
    ffmpeg_path: str | Path,
    available_encoders: Collection[str] | None = None,
    probe: Callable[[str | Path, str], bool] | None = None,
) -> tuple[str, bool]:
    """Choose the first working hardware encoder, or safely use libx264."""

    encoders = (
        set(available_encoders)
        if available_encoders is not None
        else list_ffmpeg_encoders(ffmpeg_path)
    )
    probe_function = probe or smoke_probe_encoder
    for encoder in HARDWARE_H264_ENCODERS:
        if encoder in encoders and probe_function(ffmpeg_path, encoder):
            return encoder, True
    return "libx264", False


def recommend_parallelism(resources: HostResources) -> tuple[int, int]:
    """Return a quality-preserving limit that leaves room for a local game server.

    DocuRandom commonly runs beside a 16-GiB modded Minecraft server.  Twenty
    workspaces must therefore increase queue depth, not multiply heavyweight
    FFmpeg processes.  Two renders with bounded filter/x264 workers keep the
    pipeline busy while preserving CPU time for Minecraft, VOICEVOX and the UI.
    """

    cpus = max(1, resources.logical_cpus)
    if cpus < 8:
        cpu_jobs = 1
    else:
        cpu_jobs = 2

    # Preserve at least 8 GiB and one third of physical RAM. On a 64-GiB host
    # this leaves over 21 GiB outside DocuRandom for a 16-GiB Minecraft heap,
    # Windows, native mod memory and disk cache. A 1080p render receives 4 GiB.
    reserve = max(8 * GIB, resources.total_ram_bytes // 3)
    usable = max(0, resources.available_ram_bytes - reserve)
    ram_jobs = max(1, usable // (4 * GIB))
    jobs = max(1, min(GLOBAL_RENDER_LIMIT_MAX, cpu_jobs, int(ram_jobs)))

    reserved_cpus = max(4, cpus // 3) if cpus >= 8 else (1 if cpus >= 2 else 0)
    threads = max(1, (cpus - reserved_cpus) // jobs)
    # Six workers per process is enough to feed NVENC/QSV and keeps a software
    # fallback from monopolizing all 32 logical CPUs on an i9-14900K.
    threads = min(6, threads)

    # A running modded Java server is much more sensitive to CPU contention
    # than a normal desktop workload.  Keep one render lane and four FFmpeg
    # workers in that case; the remaining workspaces stay queued and retain
    # their checkpoints instead of competing with the server tick thread.
    if resources.minecraft_running or resources.memory_pressure:
        jobs = 1
        threads = min(4, threads)
    return jobs, threads


def recommend_voicevox_parallelism(resources: HostResources) -> int:
    """Choose safe concurrent VOICEVOX synthesis streams for this machine.

    VOICEVOX requests are lighter than full 1080p renders, but concurrent
    models still consume CPU/GPU memory. Keep a small bounded pool so separate
    workspaces can progress together without allowing an unbounded request
    burst against the single local engine.
    """

    if resources.minecraft_running or resources.memory_pressure:
        return 1
    cpus = max(1, resources.logical_cpus)
    available = max(0, resources.available_ram_bytes)
    if cpus < 6 or available < 6 * GIB:
        return 1
    return 2


def recommend_youtube_upload_parallelism(resources: HostResources) -> int:
    """Keep all workspaces on one Donut Studio upload lane."""

    return YOUTUBE_UPLOAD_LIMIT_MIN


def recommend_chatgpt_parallelism(resources: HostResources) -> int:
    """Bound web submissions by account pressure as well as local resources."""

    if resources.minecraft_running or resources.memory_pressure:
        return 1
    if resources.logical_cpus < 6 or resources.available_ram_bytes < 6 * GIB:
        return 1
    if resources.logical_cpus >= 12 and resources.available_ram_bytes >= 12 * GIB:
        return 3
    return 2


def detect_runtime_profile(
    ffmpeg_path: str | Path,
    resources: HostResources | None = None,
) -> RenderRuntimeProfile:
    host = resources or detect_host_resources()
    encoder, hardware = choose_h264_encoder(ffmpeg_path)
    jobs, threads = recommend_parallelism(host)
    labels = {
        "h264_nvenc": "NVIDIA NVENC",
        "h264_amf": "AMD AMF",
        "h264_qsv": "Intel Quick Sync",
        "libx264": "CPU / x264",
    }
    return RenderRuntimeProfile(
        host=host,
        encoder=encoder,
        hardware_accelerated=hardware,
        recommended_jobs=jobs,
        threads_per_job=threads,
        encoder_label=labels[encoder],
    )


def encoder_args(
    profile: RenderRuntimeProfile | str,
    stage: EncoderStage = "final",
    *,
    threads_per_job: int | None = None,
) -> list[str]:
    """High-quality, broadly compatible H.264 arguments for a render stage."""

    if stage not in ("scene", "final"):
        raise ValueError("stage must be 'scene' or 'final'")
    if isinstance(profile, RenderRuntimeProfile):
        encoder = profile.encoder
        threads = profile.threads_per_job
    else:
        encoder = profile
        threads = max(1, int(threads_per_job or 1))

    is_scene = stage == "scene"
    if encoder == "h264_nvenc":
        args = [
            "-c:v",
            encoder,
            "-preset",
            "p4" if is_scene else "p6",
            "-tune",
            "hq",
            "-rc",
            "vbr",
            "-cq",
            "14" if is_scene else "17",
            "-b:v",
            "0",
            "-spatial-aq",
            "1",
            "-aq-strength",
            "8",
        ]
    elif encoder == "h264_amf":
        quality = "14" if is_scene else "18"
        args = [
            "-c:v",
            encoder,
            "-usage",
            "transcoding",
            "-quality",
            "quality",
            "-rc",
            "cqp",
            "-qp_i",
            quality,
            "-qp_p",
            quality,
            "-qp_b",
            str(int(quality) + 2),
        ]
    elif encoder == "h264_qsv":
        args = [
            "-c:v",
            encoder,
            "-preset",
            "medium" if is_scene else "slow",
            "-global_quality",
            "14" if is_scene else "17",
        ]
    else:
        args = [
            "-c:v",
            "libx264",
            "-preset",
            "faster" if is_scene else "medium",
            "-crf",
            "12" if is_scene else "17",
            "-threads",
            str(max(1, threads)),
        ]

    args.extend(("-profile:v", "high", "-pix_fmt", "yuv420p"))
    if not is_scene:
        args.extend(("-movflags", "+faststart"))
    return args


def ffmpeg_thread_args(
    profile: RenderRuntimeProfile,
    *,
    complex_filter: bool = False,
) -> list[str]:
    """Explicitly cap FFmpeg filter threads so concurrent jobs stay balanced."""

    option = "-filter_complex_threads" if complex_filter else "-filter_threads"
    return [option, str(max(1, profile.threads_per_job))]
