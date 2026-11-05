from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image, ImageOps

from .models import MediaInfo
from .storage import app_data_dir


SUPPORTED_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
WATERMARK_IMAGE_TIMEOUT_SECONDS = 300.0
# Flow's current sparkle mark sits inside the lower-right safe area.  The
# supplied clean reference is 1376x768 -> 1256x768, i.e. exactly 120 pixels
# removed from the right while the full height is retained.  Keep this as one
# shared template so every Flow image is framed consistently.
RIGHT_CROP_REFERENCE_WIDTH = 1376
RIGHT_CROP_REFERENCE_PIXELS = 120
VISIBLE_CLEAN_DIR_NAME = "_watermark_clean"


class WatermarkRuntimeUnavailable(RuntimeError):
    """The bundled/local Gemini watermark remover cannot be started."""


class WatermarkRemovalCancelled(InterruptedError):
    """Watermark preprocessing was cancelled by the owning render job."""


@dataclass(frozen=True)
class WatermarkRuntime:
    node_path: Path
    cli_path: Path
    root_path: Path
    version: str


@dataclass(frozen=True)
class WatermarkPreparation:
    media: list[MediaInfo]
    version: str
    processed_count: int
    cache_hits: int
    skipped_count: int
    warnings: list[str]


_CACHE_LOCKS_GUARD = threading.Lock()
_CACHE_LOCKS: dict[str, threading.Lock] = {}


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _unique_existing_paths(paths: Sequence[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        if resolved.exists():
            result.append(resolved)
    return result


def _watermark_root_candidates(extra_roots: Sequence[Path] = ()) -> list[Path]:
    resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    executable_root = Path(sys.executable).resolve().parent
    project_root = Path(__file__).resolve().parents[1]
    workspace_root = project_root.parent
    configured = os.environ.get("DOCURANDOM_WATERMARK_ROOT", "").strip()
    candidates = [Path(value) for value in extra_roots]
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        (
            resource_root / "watermark-remover",
            executable_root / "watermark-remover",
            executable_root / "_internal" / "watermark-remover",
            workspace_root / "watermark-remover-main",
            Path.cwd() / "watermark-remover-main",
            Path.cwd().parent / "watermark-remover-main",
        )
    )
    return _unique_existing_paths(candidates)


def _node_candidates(resource_root: Path, tool_root: Path) -> list[Path]:
    configured = os.environ.get("DOCURANDOM_NODE_PATH", "").strip()
    executable_root = Path(sys.executable).resolve().parent
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    program_files = Path(os.environ.get("ProgramFiles", ""))
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        (
            resource_root / "watermark-node" / "node.exe",
            executable_root / "watermark-node" / "node.exe",
            executable_root / "_internal" / "watermark-node" / "node.exe",
            tool_root / "node" / "node.exe",
        )
    )
    discovered = shutil.which("node")
    if discovered:
        candidates.append(Path(discovered))
    if str(local_app_data):
        candidates.append(local_app_data / "Programs" / "nodejs" / "node.exe")
    if str(program_files):
        candidates.append(program_files / "nodejs" / "node.exe")

    # Codex Desktop supplies a private Node runtime. This keeps source-tree
    # development usable without turning that location into a packaging
    # requirement; portable builds use watermark-node/node.exe above.
    candidates.extend(
        (
            Path.home()
            / ".cache"
            / "codex-runtimes"
            / "codex-primary-runtime"
            / "dependencies"
            / "node"
            / "bin"
            / "node.exe",
            Path.home()
            / ".cache"
            / "codex-runtimes"
            / "codex-primary-runtime"
            / "dependencies"
            / "node"
            / "node.exe",
        )
    )
    return _unique_existing_paths(candidates)


def _read_runtime_version(root: Path) -> str:
    try:
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        version = str(package.get("version", "")).strip()
        return version or "source"
    except Exception:
        return "source"


def discover_watermark_runtime(
    extra_roots: Sequence[Path] = (),
) -> WatermarkRuntime:
    roots = _watermark_root_candidates(extra_roots)
    valid_roots = [root for root in roots if (root / "bin" / "gwr.mjs").is_file()]
    if not valid_roots:
        raise WatermarkRuntimeUnavailable(
            "Không tìm thấy mã nguồn Gemini Watermark Remover. Bản portable cần "
            "được đóng gói kèm watermark-remover; khi chạy mã nguồn, hãy đặt "
            "watermark-remover-main cạnh DocuRandomStudio."
        )

    resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    missing_codec = False
    for root in valid_roots:
        sharp_package = root / "node_modules" / "sharp" / "package.json"
        if not sharp_package.is_file():
            missing_codec = True
            continue
        node_paths = _node_candidates(resource_root, root)
        if node_paths:
            return WatermarkRuntime(
                node_path=node_paths[0],
                cli_path=root / "bin" / "gwr.mjs",
                root_path=root,
                version=_read_runtime_version(root),
            )

    if missing_codec:
        if getattr(sys, "frozen", False):
            raise WatermarkRuntimeUnavailable(
                "Bộ gỡ watermark tích hợp bị thiếu Sharp. Đây là lỗi của gói cài đặt; "
                "hãy cập nhật DocuRandom Studio lên bản mới thay vì tự cài pnpm."
            )
        raise WatermarkRuntimeUnavailable(
            "Gemini Watermark Remover chưa có bộ giải mã ảnh Sharp. Hãy chạy "
            "`pnpm install` trong watermark-remover-main hoặc tạo lại bản portable."
        )
    raise WatermarkRuntimeUnavailable(
        "Không tìm thấy Node.js để chạy Gemini Watermark Remover. Hãy tạo lại "
        "bản portable có kèm Node hoặc đặt DOCURANDOM_NODE_PATH tới node.exe."
    )


def _cache_key(
    path: Path,
    runtime: WatermarkRuntime | None,
    *,
    remove_watermark: bool,
    crop_right_template: bool,
) -> str:
    info = path.stat()
    payload = json.dumps(
        {
            "path": os.path.normcase(str(path.resolve())),
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "remover_version": runtime.version if runtime is not None else "template-only",
            "remove_watermark": remove_watermark,
            "crop_right_template": crop_right_template,
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _cache_lock(key: str) -> threading.Lock:
    with _CACHE_LOCKS_GUARD:
        return _CACHE_LOCKS.setdefault(key, threading.Lock())


def _acquire_cache_lock(lock: threading.Lock, cancel_event: threading.Event | None) -> None:
    while not lock.acquire(timeout=0.1):
        if cancel_event is not None and cancel_event.is_set():
            raise WatermarkRemovalCancelled()


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2.0)


def _run_remover(
    runtime: WatermarkRuntime,
    source: Path,
    destination: Path,
    cancel_event: threading.Event | None,
) -> dict[str, object]:
    command = [
        str(runtime.node_path),
        str(runtime.cli_path),
        "remove",
        str(source),
        "--output",
        str(destination),
        "--overwrite",
        "--json",
    ]
    environment = os.environ.copy()
    pnpm_fallback = runtime.root_path / "node_modules" / ".pnpm" / "node_modules"
    if pnpm_fallback.is_dir():
        node_paths = [str(pnpm_fallback)]
        existing_node_path = environment.get("NODE_PATH", "").strip()
        if existing_node_path:
            node_paths.append(existing_node_path)
        # PyInstaller materializes pnpm junctions as regular directories. The
        # shared .pnpm/node_modules fallback keeps Sharp's transitive packages
        # resolvable in that flattened portable layout.
        environment["NODE_PATH"] = os.pathsep.join(node_paths)
    process = subprocess.Popen(
        command,
        cwd=str(runtime.root_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_creation_flags(),
        env=environment,
    )
    deadline = time.monotonic() + WATERMARK_IMAGE_TIMEOUT_SECONDS
    try:
        # ``--json`` can return a detailed quality report larger than the
        # Windows anonymous-pipe buffer. Waiting for the child to exit before
        # reading that report deadlocks: the child cannot exit while its full
        # stdout pipe is not drained. ``communicate(timeout=...)`` starts the
        # pipe readers immediately and can safely be retried after a timeout,
        # while this loop still keeps cancellation responsive.
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _terminate_process(process)
                raise WatermarkRemovalCancelled()
            if time.monotonic() >= deadline:
                _terminate_process(process)
                raise RuntimeError(
                    f"Gỡ watermark {source.name} quá 5 phút; đã bỏ qua ảnh này để tác vụ tiếp tục."
                )
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        _terminate_process(process)
        raise

    if process.returncode != 0:
        detail = (stderr or stdout or "Lỗi không xác định").strip()
        if len(detail) > 2000:
            detail = detail[-2000:]
        raise RuntimeError(f"Gemini Watermark Remover thất bại với {source.name}: {detail}")
    try:
        value = json.loads(stdout.strip()) if stdout.strip() else {}
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _verify_image(path: Path) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError("Bộ gỡ watermark không tạo được ảnh đầu ra.")
    with Image.open(path) as image:
        image.verify()


def _visible_clean_name(source: Path, key: str) -> str:
    """Return an inspectable, collision-safe filename for a cleaned image."""

    stem = source.stem.strip() or "image"
    return f"{stem[:80]}__clean_{key[:12]}{source.suffix.casefold()}"


def _crop_right_template(source: Path, destination: Path) -> None:
    """Apply the shared Flow watermark crop: remove the right edge only."""

    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened)
        width, height = image.size
        crop_pixels = max(
            1,
            round(
                width
                * RIGHT_CROP_REFERENCE_PIXELS
                / RIGHT_CROP_REFERENCE_WIDTH
            ),
        )
        target_width = max(2, width - crop_pixels)
        cropped = image.crop((0, 0, target_width, height))
        if cropped.mode in {"RGBA", "LA", "P"} and destination.suffix.casefold() in {
            ".jpg",
            ".jpeg",
        }:
            cropped = cropped.convert("RGB")
        save_kwargs: dict[str, object] = {}
        if destination.suffix.casefold() in {".jpg", ".jpeg"}:
            save_kwargs = {"quality": 95, "optimize": True}
        cropped.save(destination, **save_kwargs)


def _prepare_one_image(
    item: MediaInfo,
    runtime: WatermarkRuntime | None,
    cache_dir: Path,
    cancel_event: threading.Event | None,
    *,
    remove_watermark: bool,
    crop_right_template: bool,
) -> tuple[MediaInfo, bool]:
    key = _cache_key(
        item.path,
        runtime,
        remove_watermark=remove_watermark,
        crop_right_template=crop_right_template,
    )
    suffix = item.path.suffix.casefold()
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / _visible_clean_name(item.path, key)
    if target.is_file() and target.stat().st_size > 0:
        return replace(item, path=target.absolute()), True

    lock = _cache_lock(key)
    _acquire_cache_lock(lock, cancel_event)
    temporary: Path | None = None
    try:
        if target.is_file() and target.stat().st_size > 0:
            return replace(item, path=target.absolute()), True
        if cancel_event is not None and cancel_event.is_set():
            raise WatermarkRemovalCancelled()
        temporary = cache_dir / f".{key}.{uuid.uuid4().hex}.source{suffix}"
        if remove_watermark:
            if runtime is None:
                raise WatermarkRuntimeUnavailable(
                    "Không có runtime Gemini Watermark Remover để xử lý ảnh."
                )
            _run_remover(runtime, item.path, temporary, cancel_event)
            _verify_image(temporary)
            source_for_crop = temporary
        else:
            source_for_crop = item.path
        # The remover output is kept in the workspace-visible clean-media
        # directory.  Cropping is deliberately applied after the remover so
        # even a partially removed sparkle cannot reach the final render.
        if crop_right_template:
            _crop_right_template(source_for_crop, target)
        else:
            shutil.copy2(source_for_crop, target)
        _verify_image(target)
        temporary.unlink(missing_ok=True)
        temporary = None
        return replace(item, path=target.absolute()), False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock.release()


def prepare_image_watermarks(
    media: Sequence[MediaInfo],
    *,
    runtime: WatermarkRuntime | None = None,
    remove_watermark: bool = True,
    crop_right_template: bool = True,
    cache_dir: Path | None = None,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[[float, str], None] | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> WatermarkPreparation:
    """Return media pointing at cached, de-watermarked Gemini images.

    Source files are never modified. Unsupported image types and individual
    processing failures stay on their original paths and are reported as
    non-blocking warnings so the owning render can still produce a result.
    """

    output = list(media)
    eligible = [
        (index, item)
        for index, item in enumerate(media)
        if item.media_type == "image" and item.path.suffix.casefold() in SUPPORTED_IMAGE_SUFFIXES
    ]
    unsupported = [
        item
        for item in media
        if item.media_type == "image" and item.path.suffix.casefold() not in SUPPORTED_IMAGE_SUFFIXES
    ]
    warnings: list[str] = []
    if unsupported:
        suffixes = ", ".join(sorted({item.path.suffix.upper() or "không rõ" for item in unsupported}))
        warnings.append(
            f"Bỏ qua gỡ watermark cho {len(unsupported)} ảnh định dạng {suffixes}; "
            "chỉ hỗ trợ JPG, PNG và WebP."
        )
    if not eligible:
        if progress_callback is not None:
            progress_callback(1.0, "Không có ảnh JPG/PNG/WebP cần gỡ watermark")
        return WatermarkPreparation(output, "", 0, 0, len(unsupported), warnings)

    if not remove_watermark and not crop_right_template:
        return WatermarkPreparation(output, "", 0, 0, len(unsupported), warnings)
    active_runtime = (runtime or discover_watermark_runtime()) if remove_watermark else None
    resolved_cache = (
        cache_dir or (app_data_dir() / "watermark_cache" / VISIBLE_CLEAN_DIR_NAME)
    ).expanduser().absolute()
    processed_count = 0
    cache_hits = 0
    for position, (index, item) in enumerate(eligible, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise WatermarkRemovalCancelled()
        if progress_callback is not None:
            progress_callback(
                (position - 1) / len(eligible),
                (
                    f"Gỡ watermark Gemini {position}/{len(eligible)}: {item.path.name}"
                    if remove_watermark
                    else f"Cắt template Flow {position}/{len(eligible)}: {item.path.name}"
                ),
            )
        try:
            prepared, cache_hit = _prepare_one_image(
                item,
                active_runtime,
                resolved_cache,
                cancel_event,
                remove_watermark=remove_watermark,
                crop_right_template=crop_right_template,
            )
            output[index] = prepared
            if cache_hit:
                cache_hits += 1
            else:
                processed_count += 1
            if log_callback is not None:
                log_callback(
                    f"Ảnh sạch dùng để render: {prepared.path}"
                    f" · crop phải {RIGHT_CROP_REFERENCE_PIXELS}px/{RIGHT_CROP_REFERENCE_WIDTH}px"
                )
        except WatermarkRemovalCancelled:
            raise
        except Exception as exc:
            warning = f"Không thể gỡ watermark {item.path.name}; dùng ảnh gốc. Chi tiết: {exc}"
            warnings.append(warning)
            if log_callback is not None:
                log_callback(warning)
        if progress_callback is not None:
            progress_callback(
                position / len(eligible),
                (
                    "Đã chuẩn bị "
                    f"{position}/{len(eligible)} ảnh qua Gemini Watermark Remover"
                    if remove_watermark
                    else f"Đã cắt template Flow {position}/{len(eligible)} ảnh"
                ),
            )

    return WatermarkPreparation(
        media=output,
        version=active_runtime.version if active_runtime is not None else "template-crop",
        processed_count=processed_count,
        cache_hits=cache_hits,
        skipped_count=len(unsupported),
        warnings=warnings,
    )
