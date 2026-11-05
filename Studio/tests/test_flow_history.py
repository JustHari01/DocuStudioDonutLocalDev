from __future__ import annotations

import random

from flow_server.history import (
    _deduplicate,
    _merge_entries,
    _normalise_entry,
    _clean_string,
    derive_filename,
)


def _reference_deduplicate(entries):
    """The pre-optimization algorithm, used only as a semantic oracle."""
    result = []
    for raw_entry in reversed(list(entries)):
        entry = _normalise_entry(raw_entry)
        if entry is None:
            continue
        filename = derive_filename(entry)
        filename_key = str(filename).casefold() if filename else None
        media_id = _clean_string(entry.get("media_id"))
        matches = []
        remaining = []
        for existing in result:
            existing_filename = derive_filename(existing)
            same_filename = bool(
                filename_key
                and existing_filename
                and str(existing_filename).casefold() == filename_key
            )
            same_media_id = bool(
                media_id and _clean_string(existing.get("media_id")) == media_id
            )
            if same_filename or same_media_id:
                matches.append(existing)
            else:
                remaining.append(existing)
        merged = {}
        for existing in reversed(matches):
            merged = _merge_entries(merged, existing)
        merged = _merge_entries(merged, entry)
        result = [merged, *remaining]
    return result


def test_deduplicate_matches_previous_merge_semantics():
    rng = random.Random(42)
    for _ in range(100):
        entries = []
        for index in range(rng.randrange(1, 40)):
            filename = f"image-{rng.randrange(8)}.png"
            entry = {
                "filename": filename,
                "url": f"/download/{filename}",
                "prompt": f"prompt-{index}",
                "timestamp": index,
            }
            if rng.random() < 0.6:
                entry["media_id"] = f"media-{rng.randrange(10)}"
            if rng.random() < 0.2:
                entry["project_id"] = f"project-{rng.randrange(3)}"
            entries.append(entry)
        assert _deduplicate(entries) == _reference_deduplicate(entries)


def test_deduplicate_scales_with_large_flow_history():
    entries = [
        {
            "filename": f"flow-{index}.png",
            "url": f"/download/flow-{index}.png",
            "media_id": f"media-{index}",
            "prompt": "large-history",
        }
        for index in range(3000)
    ]
    result = _deduplicate(entries)
    assert len(result) == len(entries)
