from __future__ import annotations

import io
import hashlib
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .subtitles import SubtitleCue, read_srt, split_timed_cue, write_srt
from .storage import app_data_dir, atomic_write_json


DEFAULT_ENDPOINT = "http://127.0.0.1:50021"
VOICEVOX_RELEASES_URL = "https://github.com/VOICEVOX/voicevox/releases/latest"
VOICEVOX_ENGINE_VERSION = "0.25.2"
MAX_SCRIPT_LINES = 2000
MAX_LINE_CHARACTERS = 5000
MAX_SCRIPT_CHARACTERS = 250_000
DOWNLOAD_CHUNK_SIZE = 256 * 1024
EXTRACT_CHUNK_SIZE = 1024 * 1024
INSTALL_DISK_RESERVE = 512 * 1024 * 1024
VOICEVOX_DIALOGUE_PREFIX = re.compile(r"^[AＡ]\s*[:：]\s*", re.IGNORECASE)
VOICEVOX_SCRIPT_SEPARATOR = re.compile(r"(?:[-_*=─━═]\s*){3,}\Z")


class VoicevoxError(RuntimeError):
    pass


class VoicevoxKanaError(VoicevoxError):
    """VOICEVOX rejected an AquesTalk-style pronunciation override.

    This is deliberately separate from transport/engine failures.  A bad
    model-suggested kana patch can therefore be discarded for one box without
    hiding a real VOICEVOX outage.
    """

    def __init__(self, message: str, *, error_name: str = "") -> None:
        super().__init__(message)
        self.error_name = str(error_name).strip()


class VoicevoxCancelled(VoicevoxError):
    pass


@dataclass(frozen=True)
class VoicevoxEnginePackage:
    version: str
    variant: str
    filename: str
    url: str
    size: int
    sha256: str


# Pinned official VOICEVOX release assets. Pinning both the URL and GitHub's
# published SHA-256 digest prevents a mutable latest-release lookup from
# silently changing what DocuRandom installs.
VOICEVOX_ENGINE_PACKAGES = {
    "cpu": VoicevoxEnginePackage(
        version=VOICEVOX_ENGINE_VERSION,
        variant="cpu",
        filename=f"voicevox_engine-windows-cpu-{VOICEVOX_ENGINE_VERSION}.vvpp",
        url=(
            "https://github.com/VOICEVOX/voicevox_engine/releases/download/"
            f"{VOICEVOX_ENGINE_VERSION}/voicevox_engine-windows-cpu-"
            f"{VOICEVOX_ENGINE_VERSION}.vvpp"
        ),
        size=1_894_411_533,
        sha256="cae07cb718866708d8c6148988769966168a2282610f53f002b19778ebea38e9",
    ),
    "directml": VoicevoxEnginePackage(
        version=VOICEVOX_ENGINE_VERSION,
        variant="directml",
        filename=(
            f"voicevox_engine-windows-directml-{VOICEVOX_ENGINE_VERSION}.vvpp"
        ),
        url=(
            "https://github.com/VOICEVOX/voicevox_engine/releases/download/"
            f"{VOICEVOX_ENGINE_VERSION}/voicevox_engine-windows-directml-"
            f"{VOICEVOX_ENGINE_VERSION}.vvpp"
        ),
        size=1_903_499_910,
        sha256="e3fa809397f2f771b29dd04452c9ede255dd894436ab8af764ac486f987297f1",
    ),
}


@dataclass(frozen=True)
class VoicevoxStyle:
    speaker_name: str
    speaker_uuid: str
    style_name: str
    style_id: int

    @property
    def label(self) -> str:
        return f"{self.speaker_name} · {self.style_name} (ID {self.style_id})"


@dataclass(frozen=True)
class VoicevoxSynthesisSettings:
    speed_scale: float = 1.0
    pitch_scale: float = 0.0
    intonation_scale: float = 1.0
    volume_scale: float = 1.0
    pre_phoneme_length: float = 0.1
    post_phoneme_length: float = 0.1
    pause_length_scale: float = 1.0
    line_pause_seconds: float = 0.25
    output_sampling_rate: int = 24000
    output_stereo: bool = False
    save_line_files: bool = False

    def validate(self) -> None:
        ranges = {
            "Tốc độ": (self.speed_scale, 0.5, 2.0),
            "Cao độ": (self.pitch_scale, -0.15, 0.15),
            "Ngữ điệu": (self.intonation_scale, 0.0, 2.0),
            "Âm lượng": (self.volume_scale, 0.0, 2.0),
            "Im lặng đầu": (self.pre_phoneme_length, 0.0, 1.5),
            "Im lặng cuối": (self.post_phoneme_length, 0.0, 1.5),
            "Ngắt câu": (self.pause_length_scale, 0.0, 2.0),
            "Khoảng cách dòng": (self.line_pause_seconds, 0.0, 5.0),
        }
        for name, (value, minimum, maximum) in ranges.items():
            if not minimum <= float(value) <= maximum:
                raise ValueError(f"{name} phải từ {minimum:g} đến {maximum:g}.")
        if not 8000 <= int(self.output_sampling_rate) <= 192000:
            raise ValueError("Tần số lấy mẫu phải từ 8000 đến 192000 Hz.")


@dataclass(frozen=True)
class VoicevoxSynthesisResult:
    audio_path: Path
    subtitle_path: Path
    metadata_path: Path
    line_files_dir: Path | None
    line_count: int
    duration_seconds: float
    engine_version: str
    style: VoicevoxStyle


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0
    )


def normalize_endpoint(endpoint: str) -> str:
    value = endpoint.strip().rstrip("/") or DEFAULT_ENDPOINT
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError(
            "VOICEVOX endpoint phải là HTTP cục bộ: 127.0.0.1, localhost hoặc ::1."
        )
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("VOICEVOX endpoint không được chứa đường dẫn hoặc query.")
    try:
        port = parsed.port or 80
    except ValueError as exc:
        raise ValueError("Cổng VOICEVOX không hợp lệ.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Cổng VOICEVOX phải từ 1 đến 65535.")
    host = f"[{parsed.hostname}]" if ":" in str(parsed.hostname) else parsed.hostname
    return f"http://{host}:{port}"


def endpoint_host_port(endpoint: str) -> tuple[str, int]:
    parsed = urllib.parse.urlsplit(normalize_endpoint(endpoint))
    return str(parsed.hostname), int(parsed.port or 80)


def parse_script_lines(script: str) -> list[str]:
    normalized = script.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if len(normalized) > MAX_SCRIPT_CHARACTERS:
        raise ValueError(
            f"Kịch bản tối đa {MAX_SCRIPT_CHARACTERS:,} ký tự cho một lượt tạo giọng."
        )
    lines: list[str] = []
    for source_line in normalized.split("\n"):
        line = source_line.strip()
        if not line:
            continue
        # The consolidated export uses ``A: text`` as a dialogue marker.  It is
        # metadata for the script, not spoken content.  Accept scripts both with
        # and without the marker and also accept Japanese/full-width punctuation.
        line = VOICEVOX_DIALOGUE_PREFIX.sub("", line, count=1).strip()
        # ChatGPT may leave a Markdown/box-drawing divider after the final
        # narration line. It is layout, not speech: the engine returns an empty
        # accent-phrase list for it and the project audit cannot accept it.
        if VOICEVOX_SCRIPT_SEPARATOR.fullmatch(line):
            continue
        if line:
            lines.append(line)
    if not lines:
        raise ValueError("Hãy dán kịch bản; mỗi câu hoặc đoạn nằm trên một dòng.")
    if len(lines) > MAX_SCRIPT_LINES:
        raise ValueError(f"Một lượt hỗ trợ tối đa {MAX_SCRIPT_LINES:,} dòng.")
    too_long = next((index for index, line in enumerate(lines, 1) if len(line) > MAX_LINE_CHARACTERS), None)
    if too_long is not None:
        raise ValueError(
            f"Dòng {too_long} dài quá {MAX_LINE_CHARACTERS:,} ký tự; hãy tách thành nhiều dòng."
        )
    return lines


class VoicevoxClient:
    def __init__(self, endpoint: str = DEFAULT_ENDPOINT) -> None:
        self.endpoint = normalize_endpoint(endpoint)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        json_body: object | None = None,
        timeout: float = 30.0,
        cancel_event: threading.Event | None = None,
    ) -> tuple[bytes, str]:
        if cancel_event is not None and cancel_event.is_set():
            raise VoicevoxCancelled("Đã hủy tạo giọng VOICEVOX.")
        query = urllib.parse.urlencode(
            {key: str(value).lower() if isinstance(value, bool) else value for key, value in (params or {}).items()}
        )
        url = f"{self.endpoint}{path}{'?' + query if query else ''}"
        payload = None
        headers = {"Accept": "application/json, audio/wav"}
        if json_body is not None:
            payload = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        def perform() -> tuple[bytes, str]:
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return response.read(), str(
                        response.headers.get("Content-Type") or ""
                    )
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace").strip()
                if exc.code == 400 and detail:
                    try:
                        error_payload = json.loads(detail)
                    except json.JSONDecodeError:
                        error_payload = None
                    if isinstance(error_payload, dict):
                        nested = error_payload.get("detail")
                        if isinstance(nested, dict):
                            error_payload = nested
                        error_name = str(error_payload.get("error_name") or "").strip()
                        if error_name in {
                            "UNKNOWN_TEXT",
                            "ACCENT_TOP",
                            "ACCENT_TWICE",
                            "ACCENT_NOTFOUND",
                            "EMPTY_PHRASE",
                            "INTERROGATION_MARK_NOT_AT_END",
                            "INFINITE_LOOP",
                        }:
                            message = str(error_payload.get("text") or detail).strip()
                            raise VoicevoxKanaError(
                                f"VOICEVOX từ chối kana sửa phát âm ({error_name}): {message}",
                                error_name=error_name,
                            ) from exc
                raise VoicevoxError(
                    f"VOICEVOX trả lỗi HTTP {exc.code}: {detail or exc.reason}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                raise VoicevoxError(
                    f"Không kết nối được VOICEVOX Engine: {reason}"
                ) from exc

        if cancel_event is None:
            data, content_type = perform()
        else:
            # urllib has no portable cross-thread socket cancellation API. Keep
            # the request in a daemon helper and let the calling synthesis task
            # stop within 100 ms. The helper owns no output file and dies on its
            # normal network timeout even when an external engine is wedged.
            outcomes: queue.Queue[tuple[bytes, str] | BaseException] = queue.Queue(
                maxsize=1
            )

            def request_worker() -> None:
                try:
                    outcomes.put_nowait(perform())
                except BaseException as exc:
                    outcomes.put_nowait(exc)

            threading.Thread(
                target=request_worker,
                daemon=True,
                name="VoicevoxHttpRequest",
            ).start()
            while True:
                if cancel_event.wait(0.1):
                    raise VoicevoxCancelled("Đã hủy tạo giọng VOICEVOX.")
                try:
                    outcome = outcomes.get_nowait()
                except queue.Empty:
                    continue
                if isinstance(outcome, BaseException):
                    raise outcome
                data, content_type = outcome
                break
        if cancel_event is not None and cancel_event.is_set():
            raise VoicevoxCancelled("Đã hủy tạo giọng VOICEVOX.")
        return data, content_type

    def _json_request(
        self,
        method: str,
        path: str,
        **kwargs: object,
    ) -> object:
        data, _content_type = self._request(method, path, **kwargs)
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VoicevoxError("VOICEVOX trả dữ liệu JSON không hợp lệ.") from exc

    def version(
        self,
        *,
        timeout: float = 3.0,
        cancel_event: threading.Event | None = None,
    ) -> str:
        value = self._json_request(
            "GET", "/version", timeout=timeout, cancel_event=cancel_event
        )
        if not isinstance(value, str) or not value.strip():
            raise VoicevoxError("Không xác định được phiên bản VOICEVOX Engine.")
        return value.strip()

    def styles(
        self,
        *,
        timeout: float = 15.0,
        cancel_event: threading.Event | None = None,
    ) -> list[VoicevoxStyle]:
        payload = self._json_request(
            "GET", "/speakers", timeout=timeout, cancel_event=cancel_event
        )
        if not isinstance(payload, list):
            raise VoicevoxError("Danh sách giọng VOICEVOX không hợp lệ.")
        result: list[VoicevoxStyle] = []
        for speaker in payload:
            if not isinstance(speaker, dict):
                continue
            speaker_name = str(speaker.get("name") or "VOICEVOX")
            speaker_uuid = str(speaker.get("speaker_uuid") or "")
            styles = speaker.get("styles") or []
            if not isinstance(styles, list):
                continue
            for style in styles:
                if not isinstance(style, dict) or style.get("type", "talk") != "talk":
                    continue
                try:
                    style_id = int(style["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                result.append(
                    VoicevoxStyle(
                        speaker_name=speaker_name,
                        speaker_uuid=speaker_uuid,
                        style_name=str(style.get("name") or "Normal"),
                        style_id=style_id,
                    )
                )
        if not result:
            raise VoicevoxError("VOICEVOX Engine không có style đọc văn bản khả dụng.")
        return sorted(result, key=lambda item: (item.speaker_name.casefold(), item.style_id))

    def audio_query(
        self,
        text: str,
        style_id: int,
        *,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, object]:
        payload = self._json_request(
            "POST",
            "/audio_query",
            params={"text": text, "speaker": int(style_id)},
            timeout=60.0,
            cancel_event=cancel_event,
        )
        if not isinstance(payload, dict):
            raise VoicevoxError("VOICEVOX audio_query không hợp lệ.")
        return payload

    def accent_phrases(
        self,
        text: str,
        style_id: int,
        *,
        is_kana: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> list[dict[str, object]]:
        payload = self._json_request(
            "POST",
            "/accent_phrases",
            params={
                "text": text,
                "speaker": int(style_id),
                "is_kana": bool(is_kana),
            },
            timeout=60.0,
            cancel_event=cancel_event,
        )
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise VoicevoxError("VOICEVOX accent_phrases không hợp lệ.")
        return payload

    def audited_audio_query(
        self,
        text: str,
        style_id: int,
        *,
        corrected_kana: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, object]:
        query = self.audio_query(text, style_id, cancel_event=cancel_event)
        if corrected_kana:
            query["accent_phrases"] = self.accent_phrases(
                corrected_kana,
                style_id,
                is_kana=True,
                cancel_event=cancel_event,
            )
            query["kana"] = corrected_kana
        return query

    def synthesis(
        self,
        query: dict[str, object],
        style_id: int,
        *,
        cancel_event: threading.Event | None = None,
    ) -> bytes:
        data, content_type = self._request(
            "POST",
            "/synthesis",
            params={"speaker": int(style_id), "enable_interrogative_upspeak": True},
            json_body=query,
            timeout=300.0,
            cancel_event=cancel_event,
        )
        if not data.startswith(b"RIFF") or b"WAVE" not in data[:16]:
            raise VoicevoxError(
                f"VOICEVOX không trả WAV hợp lệ ({content_type or 'không rõ kiểu dữ liệu'})."
            )
        return data


def apply_synthesis_settings(
    query: dict[str, object],
    settings: VoicevoxSynthesisSettings,
) -> dict[str, object]:
    settings.validate()
    updated = dict(query)
    updated.update(
        {
            "speedScale": float(settings.speed_scale),
            "pitchScale": float(settings.pitch_scale),
            "intonationScale": float(settings.intonation_scale),
            "volumeScale": float(settings.volume_scale),
            "prePhonemeLength": float(settings.pre_phoneme_length),
            "postPhonemeLength": float(settings.post_phoneme_length),
            "pauseLengthScale": float(settings.pause_length_scale),
            "outputSamplingRate": int(settings.output_sampling_rate),
            "outputStereo": bool(settings.output_stereo),
        }
    )
    return updated


def _write_combined_wave(
    segments: Iterable[bytes],
    destination: Path,
    line_pause_seconds: float,
) -> tuple[float, list[tuple[float, float]]]:
    segment_list = list(segments)
    parameters: tuple[int, int, int, str] | None = None
    total_frames = 0
    cue_timings: list[tuple[float, float]] = []
    with wave.open(str(destination), "wb") as output:
        for index, data in enumerate(segment_list):
            with wave.open(io.BytesIO(data), "rb") as source:
                current = (
                    source.getnchannels(),
                    source.getsampwidth(),
                    source.getframerate(),
                    source.getcomptype(),
                )
                if parameters is None:
                    parameters = current
                    output.setnchannels(current[0])
                    output.setsampwidth(current[1])
                    output.setframerate(current[2])
                    output.setcomptype(current[3], source.getcompname())
                elif current != parameters:
                    raise VoicevoxError("Các dòng VOICEVOX có định dạng WAV không đồng nhất.")
                cue_start = total_frames / parameters[2]
                frames = source.readframes(source.getnframes())
                output.writeframesraw(frames)
                total_frames += source.getnframes()
                cue_end = total_frames / parameters[2]
                cue_timings.append((cue_start, cue_end))
            if (
                parameters is not None
                and line_pause_seconds > 0
                and index < len(segment_list) - 1
            ):
                gap_frames = round(parameters[2] * line_pause_seconds)
                output.writeframesraw(b"\0" * gap_frames * parameters[0] * parameters[1])
                total_frames += gap_frames
        if parameters is None:
            raise VoicevoxError("Không có dữ liệu giọng nói để ghép.")
    return total_frames / parameters[2], cue_timings


def _format_srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(float(seconds) * 1000.0))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def _write_srt(
    lines: list[str],
    cue_timings: list[tuple[float, float]],
    destination: Path,
) -> list[SubtitleCue]:
    if len(lines) != len(cue_timings):
        raise VoicevoxError("Không thể đồng bộ số dòng kịch bản với audio VOICEVOX.")
    cues: list[SubtitleCue] = []
    for line, timing in zip(lines, cue_timings, strict=True):
        start, end = timing
        # Some players ignore zero-length cues. VOICEVOX normally always
        # returns audio, but one millisecond keeps even a degenerate response valid.
        if end <= start:
            end = start + 0.001
        cues.extend(split_timed_cue(line, start, end))
    write_srt(cues, destination)
    return cues


def synthesize_script(
    *,
    client: VoicevoxClient,
    script: str,
    style: VoicevoxStyle,
    settings: VoicevoxSynthesisSettings,
    output_path: Path,
    cancel_event: threading.Event | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    audited_items: Sequence[tuple[str, int, dict[str, object]]] | None = None,
    audited_project_path: Path | None = None,
) -> VoicevoxSynthesisResult:
    lines = parse_script_lines(script)
    prepared_items: list[tuple[str, int, dict[str, object]]] | None = None
    if audited_items is not None:
        prepared_items = [
            (str(text), int(style_id), dict(query))
            for text, style_id, query in audited_items
        ]
        if [text for text, _style_id, _query in prepared_items] != lines:
            raise VoicevoxError(
                "Nội dung project VOICEVOX FINAL không khớp kịch bản render."
            )
    settings.validate()
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.suffix.casefold() != ".wav":
        output_path = output_path.with_suffix(".wav")
    version = client.version(cancel_event=cancel_event)
    generated: list[bytes] = []
    line_dir: Path | None = None
    subtitle_path = output_path.with_suffix(".srt")
    metadata_path = output_path.with_suffix(".voicevox.json")
    voice_signature = hashlib.sha256(
        json.dumps(
            {
                "engine_version": version,
                "style": asdict(style),
                "settings": asdict(settings),
                "lines": lines,
                "audited_items": prepared_items,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    parts_dir = (
        output_path.parent
        / ".docurandom"
        / "voicevox_parts"
        / output_path.stem
        / voice_signature[:20]
    )
    parts_dir.mkdir(parents=True, exist_ok=True)
    parts_checkpoint = parts_dir / "checkpoint.json"

    def valid_wave_bytes(value: bytes) -> bool:
        try:
            with wave.open(io.BytesIO(value), "rb") as audio_file:
                return audio_file.getframerate() > 0 and audio_file.getnframes() > 0
        except (EOFError, OSError, wave.Error):
            return False

    with tempfile.TemporaryDirectory(
        prefix="DocuRandomVoicevox_", dir=output_path.parent
    ) as temporary:
        temporary_dir = Path(temporary)
        for index, line in enumerate(lines, 1):
            if cancel_event is not None and cancel_event.is_set():
                raise VoicevoxCancelled("Đã hủy tạo giọng VOICEVOX.")
            if progress:
                progress(index - 1, len(lines), f"Đang tạo dòng {index}/{len(lines)}…")
            cached_part = parts_dir / f"{index:04d}.wav"
            query_cache = None
            if prepared_items is not None:
                item_text, item_style, item_query = prepared_items[index - 1]
                query_key = hashlib.sha256(json.dumps(
                    [version, item_text, item_style, item_query], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                query_cache = output_path.parent / ".docurandom/voicevox_query_cache" / (query_key + ".wav")
            try:
                audio = cached_part.read_bytes()
            except OSError:
                audio = b""
            if valid_wave_bytes(audio):
                if progress:
                    progress(
                        index - 1,
                        len(lines),
                        f"Phục hồi dòng {index}/{len(lines)} từ checkpoint…",
                    )
            else:
                if prepared_items is None:
                    query = client.audio_query(
                        line,
                        style.style_id,
                        cancel_event=cancel_event,
                    )
                    query = apply_synthesis_settings(query, settings)
                    synthesis_style_id = style.style_id
                else:
                    prepared_text, synthesis_style_id, prepared_query = prepared_items[index - 1]
                    if prepared_text != line:
                        raise VoicevoxError(
                            f"Box {index} của project FINAL không khớp kịch bản."
                        )
                    query = dict(prepared_query)
                if query_cache is not None and query_cache.is_file():
                    manifest = query_cache.with_suffix(".json")
                    try:
                        saved = query_cache.read_bytes()
                        proof = json.loads(manifest.read_text(encoding="utf-8"))
                        if proof["sha256"] == hashlib.sha256(saved).hexdigest() and valid_wave_bytes(saved):
                            audio = saved
                    except (OSError, ValueError, KeyError): pass
                if not valid_wave_bytes(audio):
                    audio = client.synthesis(query, synthesis_style_id, cancel_event=cancel_event)
                if not valid_wave_bytes(audio):
                    raise VoicevoxError(f"VOICEVOX trả audio hỏng ở dòng {index}.")
                part = cached_part.with_suffix(".wav.part")
                part.write_bytes(audio)
                os.replace(part, cached_part)
            if query_cache is not None:
                query_cache.parent.mkdir(parents=True, exist_ok=True)
                temporary_cache = query_cache.with_suffix(f".{os.getpid()}.tmp")
                temporary_cache.write_bytes(audio); os.replace(temporary_cache, query_cache)
                atomic_write_json(query_cache.with_suffix(".json"), {"sha256": hashlib.sha256(audio).hexdigest(), "engine_version": version})
            generated.append(audio)
            atomic_write_json(
                parts_checkpoint,
                {
                    "version": 1,
                    "signature": voice_signature,
                    "completed_lines": index,
                    "total_lines": len(lines),
                },
            )

        part_path = temporary_dir / "combined.wav"
        duration, cue_timings = _write_combined_wave(
            generated,
            part_path,
            0.0 if prepared_items is not None else float(settings.line_pause_seconds),
        )
        subtitle_part = temporary_dir / "combined.srt"
        subtitle_cues = _write_srt(lines, cue_timings, subtitle_part)
        if cancel_event is not None and cancel_event.is_set():
            raise VoicevoxCancelled("Đã hủy tạo giọng VOICEVOX.")
        os.replace(part_path, output_path)
        os.replace(subtitle_part, subtitle_path)

        if settings.save_line_files:
            line_dir = output_path.parent / f"{output_path.stem}_lines"
            line_dir.mkdir(parents=False, exist_ok=True)
            for line_file in sorted(parts_dir.glob("*.wav")):
                shutil.copy2(line_file, line_dir / line_file.name)

    metadata = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "engine_endpoint": client.endpoint,
        "engine_version": version,
        "style": asdict(style),
        "settings": asdict(settings),
        "line_count": len(lines),
        "duration_seconds": round(duration, 6),
        "audio_file": output_path.name,
        "subtitle_file": subtitle_path.name,
        "parts_checkpoint": str(parts_checkpoint),
        "line_files_dir": str(line_dir) if line_dir is not None else "",
        "subtitle_cues": [
            {
                "index": index,
                "start_seconds": round(cue.start, 6),
                "end_seconds": round(cue.end, 6),
                "text": cue.text,
            }
            for index, cue in enumerate(subtitle_cues, 1)
        ],
        "script_lines": lines,
        "audited_project": str(audited_project_path or ""),
        "audited_query_digest": (
            hashlib.sha256(
                json.dumps(
                    prepared_items,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if prepared_items is not None
            else ""
        ),
    }
    metadata_part = metadata_path.with_suffix(metadata_path.suffix + ".part")
    metadata_part.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(metadata_part, metadata_path)
    if progress:
        progress(len(lines), len(lines), "Đã tạo giọng VOICEVOX.")
    return VoicevoxSynthesisResult(
        audio_path=output_path,
        subtitle_path=subtitle_path,
        metadata_path=metadata_path,
        line_files_dir=line_dir,
        line_count=len(lines),
        duration_seconds=duration,
        engine_version=version,
        style=style,
    )


def load_valid_synthesis_result(
    output_path: Path,
    *,
    script: str,
    style: VoicevoxStyle,
    settings: VoicevoxSynthesisSettings,
    audited_items: Sequence[tuple[str, int, dict[str, object]]] | None = None,
) -> VoicevoxSynthesisResult | None:
    """Validate and reuse a complete WAV/SRT/metadata checkpoint."""

    output_path = output_path.expanduser().resolve(strict=False).with_suffix(".wav")
    subtitle_path = output_path.with_suffix(".srt")
    metadata_path = output_path.with_suffix(".voicevox.json")
    if not all(path.is_file() for path in (output_path, subtitle_path, metadata_path)):
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("script_lines") != parse_script_lines(script):
            return None
        if metadata.get("style") != asdict(style):
            return None
        if metadata.get("settings") != asdict(settings):
            return None
        expected_audit_digest = (
            hashlib.sha256(
                json.dumps(
                    [
                        (str(text), int(style_id), dict(query))
                        for text, style_id, query in audited_items
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if audited_items is not None
            else ""
        )
        if str(metadata.get("audited_query_digest") or "") != expected_audit_digest:
            return None
        cues = read_srt(subtitle_path)
        if not cues:
            return None
        with wave.open(str(output_path), "rb") as audio:
            frame_rate = audio.getframerate()
            frame_count = audio.getnframes()
            if frame_rate <= 0 or frame_count <= 0:
                return None
            duration = frame_count / frame_rate
        recorded_duration = float(metadata.get("duration_seconds", 0.0))
        if recorded_duration <= 0 or abs(recorded_duration - duration) > 0.05:
            return None
        raw_line_dir = metadata.get("line_files_dir")
        line_dir = Path(str(raw_line_dir)) if raw_line_dir else None
        if line_dir is not None and not line_dir.is_dir():
            line_dir = None
        return VoicevoxSynthesisResult(
            audio_path=output_path,
            subtitle_path=subtitle_path,
            metadata_path=metadata_path,
            line_files_dir=line_dir,
            line_count=len(parse_script_lines(script)),
            duration_seconds=duration,
            engine_version=str(metadata.get("engine_version", "checkpoint")),
            style=style,
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, wave.Error):
        return None


def make_output_path(output_dir: Path) -> Path:
    output_dir = output_dir.expanduser().resolve()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = output_dir / f"VOICEVOX_{timestamp}.wav"
    sequence = 2
    while candidate.exists():
        candidate = output_dir / f"VOICEVOX_{timestamp}_{sequence}.wav"
        sequence += 1
    return candidate


def managed_engine_root() -> Path:
    return app_data_dir() / "VOICEVOX Engine"


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise VoicevoxCancelled("Đã hủy cài đặt VOICEVOX Engine.")


def _safe_remove_managed_tree(path: Path, root: Path) -> None:
    resolved_root = root.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    if resolved_path == resolved_root or not resolved_path.is_relative_to(resolved_root):
        raise VoicevoxError(f"Từ chối dọn thư mục engine ngoài vùng quản lý: {path}")
    if resolved_path.exists():
        shutil.rmtree(resolved_path)


def _engine_from_install_dir(install_dir: Path) -> Path | None:
    marker = install_dir / ".docurandom-engine.json"
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
        relative = Path(str(value["engine_relative_path"]))
        candidate = (install_dir / relative).resolve(strict=False)
        if candidate.is_relative_to(install_dir.resolve(strict=False)) and candidate.is_file():
            return candidate
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        pass

    manifests = sorted(
        install_dir.rglob("engine_manifest.json"),
        key=lambda path: len(path.relative_to(install_dir).parts),
    )
    for manifest in manifests:
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        command = value.get("command") if isinstance(value, dict) else None
        if not isinstance(command, str) or not command.strip():
            continue
        candidate = manifest.parent / command.strip()
        if candidate.suffix.casefold() != ".exe":
            candidate = candidate.with_suffix(".exe")
        if candidate.is_file():
            return candidate.resolve()
    try:
        return next(install_dir.rglob("run.exe")).resolve()
    except (OSError, StopIteration):
        return None


def discover_managed_engine_path(*, prefer_gpu: bool = True) -> Path | None:
    root = managed_engine_root()
    variants = ("directml", "cpu") if prefer_gpu else ("cpu", "directml")
    version_dirs: list[Path] = [root / VOICEVOX_ENGINE_VERSION]
    try:
        version_dirs.extend(
            path
            for path in sorted(root.iterdir(), reverse=True)
            if path.is_dir()
            and path.name != "downloads"
            and path.name != VOICEVOX_ENGINE_VERSION
            and not path.name.startswith(".")
        )
    except OSError:
        pass
    for version_dir in version_dirs:
        for variant in variants:
            candidate = _engine_from_install_dir(version_dir / variant)
            if candidate is not None:
                return candidate
    return None


def _format_download_size(value: int) -> str:
    return f"{max(0, value) / (1024**3):.2f} GB"


def _download_engine_package(
    package: VoicevoxEnginePackage,
    destination: Path,
    *,
    cancel_event: threading.Event | None,
    progress: Callable[[float, str], None] | None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    part_path = destination.with_suffix(destination.suffix + ".part")
    if destination.is_file():
        if destination.stat().st_size == package.size:
            digest = hashlib.sha256()
            with destination.open("rb") as source:
                while chunk := source.read(DOWNLOAD_CHUNK_SIZE):
                    _raise_if_cancelled(cancel_event)
                    digest.update(chunk)
            if digest.hexdigest().casefold() == package.sha256.casefold():
                if progress:
                    progress(82.0, "Đã xác minh gói VOICEVOX đã tải trước đó.")
                return
        destination.unlink(missing_ok=True)

    existing = part_path.stat().st_size if part_path.is_file() else 0
    if existing > package.size:
        part_path.unlink(missing_ok=True)
        existing = 0
    remaining = package.size - existing
    free = shutil.disk_usage(destination.parent).free
    if free < remaining + INSTALL_DISK_RESERVE:
        raise VoicevoxError(
            "Không đủ dung lượng để tải VOICEVOX Engine. "
            f"Cần thêm khoảng {_format_download_size(remaining + INSTALL_DISK_RESERVE)}."
        )

    digest = hashlib.sha256()
    if existing:
        if progress:
            progress(
                82.0 * existing / package.size,
                "Đang kiểm tra phần VOICEVOX đã tải dở…",
            )
        with part_path.open("rb") as partial:
            while chunk := partial.read(DOWNLOAD_CHUNK_SIZE):
                _raise_if_cancelled(cancel_event)
                digest.update(chunk)
        if existing == package.size:
            if digest.hexdigest().casefold() == package.sha256.casefold():
                os.replace(part_path, destination)
                if progress:
                    progress(84.0, "Gói VOICEVOX hợp lệ · đang chuẩn bị giải nén…")
                return
            part_path.unlink(missing_ok=True)
            existing = 0
            digest = hashlib.sha256()

    headers = {
        "Accept": "application/octet-stream",
        "User-Agent": "DocuRandomStudio/1.0",
    }
    if existing:
        headers["Range"] = f"bytes={existing}-"
    request = urllib.request.Request(package.url, headers=headers, method="GET")
    try:
        response = urllib.request.urlopen(request, timeout=30.0)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise VoicevoxError(f"Không thể tải VOICEVOX Engine: {exc}") from exc

    with response:
        status = getattr(response, "status", None)
        appending = bool(existing and status == 206)
        if not appending:
            existing = 0
            digest = hashlib.sha256()
        mode = "ab" if appending else "wb"
        downloaded = existing
        with part_path.open(mode) as output:
            while True:
                _raise_if_cancelled(cancel_event)
                try:
                    chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                except (TimeoutError, OSError) as exc:
                    raise VoicevoxError(
                        "Kết nối tải VOICEVOX bị gián đoạn; bấm Khởi động để tiếp tục tải."
                    ) from exc
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
                if downloaded > package.size:
                    raise VoicevoxError("Gói VOICEVOX tải về có kích thước không hợp lệ.")
                if progress:
                    progress(
                        82.0 * downloaded / package.size,
                        "Đang tải VOICEVOX chính thức · "
                        f"{_format_download_size(downloaded)}/"
                        f"{_format_download_size(package.size)}",
                    )

    if downloaded != package.size:
        raise VoicevoxError(
            "VOICEVOX chưa tải xong; bấm Khởi động để ứng dụng tiếp tục phần còn lại."
        )
    if digest.hexdigest().casefold() != package.sha256.casefold():
        part_path.unlink(missing_ok=True)
        raise VoicevoxError("Kiểm tra SHA-256 của gói VOICEVOX không khớp; đã bỏ tệp lỗi.")
    os.replace(part_path, destination)
    if progress:
        progress(84.0, "Gói VOICEVOX hợp lệ · đang chuẩn bị giải nén…")


def _extract_engine_package(
    archive_path: Path,
    staging_dir: Path,
    *,
    cancel_event: threading.Event | None,
    progress: Callable[[float, str], None] | None,
) -> Path:
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise VoicevoxError("Gói VOICEVOX không phải tệp VVPP/ZIP hợp lệ.") from exc
    with archive:
        files = [info for info in archive.infolist() if not info.is_dir()]
        total_uncompressed = sum(max(0, info.file_size) for info in files)
        if total_uncompressed <= 0 or total_uncompressed > 20 * 1024**3:
            raise VoicevoxError("Dung lượng giải nén của VOICEVOX không hợp lệ.")
        free = shutil.disk_usage(staging_dir.parent).free
        if free < total_uncompressed + INSTALL_DISK_RESERVE:
            raise VoicevoxError(
                "Không đủ dung lượng để giải nén VOICEVOX Engine. "
                f"Cần thêm khoảng {_format_download_size(total_uncompressed + INSTALL_DISK_RESERVE)}."
            )
        staging_root = staging_dir.resolve(strict=False)
        extracted = 0
        for info in archive.infolist():
            _raise_if_cancelled(cancel_event)
            relative_name = info.filename.replace("\\", "/")
            target = (staging_dir / relative_name).resolve(strict=False)
            if target == staging_root and info.is_dir():
                continue
            if target == staging_root or not target.is_relative_to(staging_root):
                raise VoicevoxError("Gói VOICEVOX chứa đường dẫn không an toàn.")
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(unix_mode):
                raise VoicevoxError("Gói VOICEVOX chứa liên kết không được hỗ trợ.")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                while chunk := source.read(EXTRACT_CHUNK_SIZE):
                    _raise_if_cancelled(cancel_event)
                    output.write(chunk)
                    extracted += len(chunk)
                    if progress:
                        progress(
                            84.0 + 15.0 * extracted / total_uncompressed,
                            "Đang cài VOICEVOX Engine · "
                            f"{round(100 * extracted / total_uncompressed)}% giải nén",
                        )
    engine_path = _engine_from_install_dir(staging_dir)
    if engine_path is None:
        raise VoicevoxError("Gói VOICEVOX không chứa chương trình engine chạy được.")
    return engine_path


def install_official_engine(
    *,
    use_gpu: bool,
    cancel_event: threading.Event | None = None,
    progress: Callable[[float, str], None] | None = None,
    install_root: Path | None = None,
    package: VoicevoxEnginePackage | None = None,
) -> Path:
    _raise_if_cancelled(cancel_event)
    selected = package or VOICEVOX_ENGINE_PACKAGES[
        "directml" if use_gpu else "cpu"
    ]
    root = (install_root or managed_engine_root()).expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / selected.version / selected.variant
    existing = _engine_from_install_dir(destination)
    if existing is not None:
        if progress:
            progress(100.0, "VOICEVOX Engine đã được cài sẵn.")
        return existing

    downloads = root / "downloads"
    archive_path = downloads / selected.filename
    _download_engine_package(
        selected,
        archive_path,
        cancel_event=cancel_event,
        progress=progress,
    )
    _raise_if_cancelled(cancel_event)

    staging = destination.parent / (
        f".installing-{selected.variant}-{os.getpid()}-{threading.get_ident()}"
    )
    if staging.exists():
        _safe_remove_managed_tree(staging, root)
    staging.mkdir(parents=True)
    try:
        staging_engine = _extract_engine_package(
            archive_path,
            staging,
            cancel_event=cancel_event,
            progress=progress,
        )
        relative_engine = staging_engine.relative_to(staging.resolve())
        marker = {
            "version": selected.version,
            "variant": selected.variant,
            "source": selected.url,
            "sha256": selected.sha256,
            "engine_relative_path": str(relative_engine),
        }
        (staging / ".docurandom-engine.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _raise_if_cancelled(cancel_event)
        if destination.exists():
            _safe_remove_managed_tree(destination, root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, destination)
    except BaseException:
        if staging.exists():
            _safe_remove_managed_tree(staging, root)
        raise
    archive_path.unlink(missing_ok=True)
    installed = _engine_from_install_dir(destination)
    if installed is None:
        raise VoicevoxError("VOICEVOX đã giải nén nhưng không tìm thấy run.exe.")
    if progress:
        progress(100.0, "Đã cài và xác minh VOICEVOX Engine.")
    return installed


def discover_engine_path(
    configured: str | Path | None = None,
    *,
    extra_roots: Iterable[Path] = (),
) -> Path | None:
    candidates: list[Path] = []

    def add(value: str | Path | None) -> None:
        if not value:
            return
        path = Path(value).expanduser()
        if path.suffix.casefold() == ".exe":
            candidates.append(path)
        else:
            candidates.extend(
                (path / "run.exe", path / "vv-engine" / "run.exe", path / "engine" / "run.exe")
            )

    add(configured)
    add(os.environ.get("VOICEVOX_ENGINE_PATH"))
    add(discover_managed_engine_path())
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        add(Path(local_app_data) / "Programs" / "VOICEVOX" / "vv-engine" / "run.exe")
    for root in extra_roots:
        add(root)
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate.absolute()
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        if resolved.is_file():
            return resolved
    return None


class VoicevoxEngineController:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Only engine discovery/startup is serialized. Once the local endpoint
        # is alive, synthesis requests from different workspaces may run in
        # parallel through the shared engine.
        self._startup_lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def owns_process(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def ensure_running(
        self,
        *,
        endpoint: str,
        engine_path: Path | None,
        use_gpu: bool,
        cpu_threads: int,
        cancel_event: threading.Event | None = None,
        startup_timeout: float = 90.0,
    ) -> tuple[VoicevoxClient, str, bool]:
        while not self._startup_lock.acquire(timeout=0.1):
            if cancel_event is not None and cancel_event.is_set():
                raise VoicevoxCancelled("Đã hủy kết nối VOICEVOX.")
        try:
            return self._ensure_running_locked(
                endpoint=endpoint,
                engine_path=engine_path,
                use_gpu=use_gpu,
                cpu_threads=cpu_threads,
                cancel_event=cancel_event,
                startup_timeout=startup_timeout,
            )
        finally:
            self._startup_lock.release()

    def _ensure_running_locked(
        self,
        *,
        endpoint: str,
        engine_path: Path | None,
        use_gpu: bool,
        cpu_threads: int,
        cancel_event: threading.Event | None = None,
        startup_timeout: float = 90.0,
    ) -> tuple[VoicevoxClient, str, bool]:
        endpoint = normalize_endpoint(endpoint)
        client = VoicevoxClient(endpoint)
        try:
            return client, client.version(timeout=1.5, cancel_event=cancel_event), False
        except (VoicevoxError, VoicevoxCancelled):
            if cancel_event is not None and cancel_event.is_set():
                raise VoicevoxCancelled("Đã hủy kết nối VOICEVOX.")

        if engine_path is None or not engine_path.is_file():
            raise VoicevoxError(
                "Chưa tìm thấy VOICEVOX Engine chạy được. Hãy bấm Khởi động "
                "VOICEVOX để ứng dụng tự tải và cài gói chính thức."
            )
        host, port = endpoint_host_port(endpoint)
        attempts = [bool(use_gpu)]
        if use_gpu:
            attempts.append(False)
        last_error = ""
        for gpu_attempt in attempts:
            command = [
                str(engine_path),
                "--host",
                host,
                "--port",
                str(port),
                "--cpu_num_threads",
                str(max(1, int(cpu_threads))),
                "--output_log_utf8",
            ]
            if gpu_attempt:
                command.append("--use_gpu")
            process = subprocess.Popen(
                command,
                cwd=str(engine_path.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_creation_flags(),
            )
            with self._lock:
                self._process = process
            deadline = time.monotonic() + max(5.0, float(startup_timeout))
            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    self.stop()
                    raise VoicevoxCancelled("Đã hủy khởi động VOICEVOX Engine.")
                if process.poll() is not None:
                    last_error = f"Engine thoát với mã {process.returncode}."
                    break
                try:
                    version = client.version(timeout=0.75, cancel_event=cancel_event)
                    return client, version, True
                except VoicevoxCancelled:
                    self.stop()
                    raise
                except VoicevoxError:
                    time.sleep(0.15)
            timed_out = process.poll() is None
            self.stop()
            if timed_out:
                last_error = "Quá thời gian chờ VOICEVOX Engine khởi động."
        raise VoicevoxError(
            f"Không khởi động được VOICEVOX Engine{': ' + last_error if last_error else '.'}"
        )

    def stop(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=3.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                pass
