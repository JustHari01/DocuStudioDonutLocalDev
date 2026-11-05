"""Measured PCM/SRT integrity; localized repairs never rewrite narration."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import wave
import numpy as np

from .storage import atomic_write_json
from .channel_learning import now_iso


def pcm_metrics(path: Path) -> dict:
    with wave.open(str(path), "rb") as wav:
        rate, channels, count = wav.getframerate(), wav.getnchannels(), wav.getnframes()
        width = wav.getsampwidth()
        if width != 2: raise ValueError("PCM16 required for measured voice safety")
        raw = wav.readframes(count)
    samples = np.frombuffer(raw, dtype="<i2").reshape(-1, channels).astype(np.float32) / 32768
    mono = samples.mean(axis=1)
    frame = max(1, int(rate * .02)); windows = len(mono) // frame
    rms = np.sqrt(np.mean(mono[:windows * frame].reshape(-1, frame) ** 2, axis=1)) if windows else np.array([])
    active = np.flatnonzero(rms > .003)
    clipped = np.any(np.abs(samples) >= .999, axis=1)
    edges = np.diff(np.r_[False, clipped, False].astype(np.int8))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    longest = int(max(ends - starts, default=0))
    return {"sha256": hashlib.sha256(raw).hexdigest(), "sample_rate": rate, "channels": channels,
            "frames": count, "duration_seconds": count / rate, "peak": float(np.abs(samples).max(initial=0)),
            "rms": float(np.sqrt(np.mean(samples ** 2))), "clipped_fraction": float(clipped.mean()),
            "longest_clipped_seconds": longest / rate,
            "leading_silence_seconds": float(active[0] * .02) if len(active) else count / rate,
            "trailing_silence_seconds": max(0., count / rate - (int(active[-1]) + 1) * .02) if len(active) else count / rate,
            "silent": len(active) == 0,
            "first_sample": float(mono[0]) if len(mono) else 0, "last_sample": float(mono[-1]) if len(mono) else 0}


def audit_signal(metadata_path: Path) -> dict:
    from .subtitles import read_srt
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    parts = Path(metadata["parts_checkpoint"]).parent
    lines = metadata["script_lines"]
    rows = []; issues = []; seen = {}
    for index, text in enumerate(lines, 1):
        path = parts / f"{index:04d}.wav"
        if not path.is_file(): issues.append({"box": index, "kind": "missing_box"}); continue
        row = dict(pcm_metrics(path), box=index, text=text)
        if row["silent"]: issues.append({"box": index, "kind": "silent_box"})
        if row["clipped_fraction"] > .0001 or row["longest_clipped_seconds"] >= .003:
            issues.append({"box": index, "kind": "clipping"})
        if row["leading_silence_seconds"] > 3 or row["trailing_silence_seconds"] > 3:
            issues.append({"box": index, "kind": "excessive_edge_pause"})
        if row["sha256"] in seen and seen[row["sha256"]][1] != text:
            issues.append({"box": index, "kind": "identical_audio_for_different_text", "other_box": seen[row["sha256"]][0]})
        seen[row["sha256"]] = (index, text); rows.append(row)
    wav = metadata_path.parent / metadata["audio_file"]
    srt = metadata_path.parent / metadata["subtitle_file"]
    cues = read_srt(srt)
    measured = pcm_metrics(wav)
    if [c.text.replace("\n", "") for c in cues] != lines: issues.append({"kind": "subtitle_text_mismatch"})
    if not cues or abs(cues[-1].end - measured["duration_seconds"]) > .05:
        issues.append({"kind": "subtitle_end_mismatch"})
    if any(c.end <= c.start or (i and c.start < cues[i-1].end - .002) for i,c in enumerate(cues)):
        issues.append({"kind": "subtitle_overlap_or_invalid_time"})
    report = {"schema_version": 1, "checked_at": now_iso(), "status": "passed" if not issues else "warning",
              "metadata_sha256": hashlib.sha256(metadata_path.read_bytes()).hexdigest(), "audio": measured,
              "subtitle_sha256": hashlib.sha256(srt.read_bytes()).hexdigest(), "box_count": len(lines),
              "checked_count": len(rows), "issues": issues, "boxes": rows,
              "limits": "Signal/word correspondence is not a native-speaker accent or prosody certification."}
    atomic_write_json(metadata_path.with_name(metadata_path.stem + ".signal.json"), report)
    return report


def bounded_repair(root: Path, initial, *, assess, repair, identity: str, max_rounds=2):
    """Repair callback has a durable identity. An uncertain round is resumed, never replaced."""
    if not 0 <= max_rounds <= 2: raise ValueError("At most two post-QA repair rounds")
    current = initial
    last = assess(current)
    for number in range(1, max_rounds + 1):
        if last["status"] == "passed": return current, last
        path = root / f"{identity}-round-{number}.json"
        old = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        stable = {k:v for k,v in last.items() if k not in {'checked_at'}}
        expected = hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if old and old["source_report_sha256"] != expected:
            raise ValueError("Repair source changed; retain the previous round")
        state = old or {"identity": identity, "round": number, "source_report_sha256": expected,
                        "before": last, "status": "reserved"}
        atomic_write_json(path, state)
        # Callback must retain/recover its own AI receipt using this stable round key.
        current = repair(current, last, number)
        new = assess(current)
        state.update(status="complete", after=new)
        atomic_write_json(path, state)
        if new["status"] != "passed" and new.get("issues") == last.get("issues"):
            raise ValueError("VOICE_REPAIR_NO_PROGRESS: preserve unresolved quality evidence")
        last = new
    if last["status"] != "passed": raise ValueError("VOICE_REPAIR_EXHAUSTED: quality gate remains closed")
    return current, last
