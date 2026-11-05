from __future__ import annotations

import json

from docurandom.content_history import (
    ContentHistoryStore,
    find_duplicate_title,
    format_title_history_for_prompt,
    normalize_content_title,
    select_prompt_title_history,
)


def test_normalize_and_duplicate_detection_cover_exact_and_near_titles():
    old = "日本のコンビニが社会の命綱になった理由"

    assert normalize_content_title(f"  {old}！ ") == normalize_content_title(old)
    assert find_duplicate_title(f"{old}。", [old]) == (old, 1.0)
    near = find_duplicate_title(
        "日本のコンビニが社会の命綱となった理由",
        [old],
    )
    assert near is not None
    assert near[0] == old
    assert find_duplicate_title("日本の新幹線技術", [old]) is None


def test_prompt_history_is_bounded_and_keeps_the_newest_titles():
    titles = [f"Tiêu đề lịch sử số {index:03d}" for index in range(500)]

    selected = select_prompt_title_history(
        titles,
        max_items=25,
        max_characters=900,
    )

    assert len(selected) <= 25
    assert selected[-1] == titles[-1]
    assert sum(len(item) + 12 for item in selected) <= 900
    prompt = format_title_history_for_prompt(selected, total_count=len(titles))
    assert "trong tổng số 500" in prompt
    assert titles[-1] in prompt


def test_store_is_idempotent_and_writes_readable_history(tmp_path):
    path = tmp_path / ".docurandom" / "content_history.json"
    text_path = tmp_path / "LICH_SU_TIEU_DE.txt"
    store = ContentHistoryStore(path, text_path)

    assert store.add("Tiêu đề đầu tiên", video_path=tmp_path / "video.mp4")
    assert not store.add("  Tiêu đề đầu tiên!  ")
    assert store.add("Tiêu đề thứ hai")

    assert store.titles() == ("Tiêu đề đầu tiên", "Tiêu đề thứ hai")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert len(raw["titles"]) == 2
    readable = text_path.read_text(encoding="utf-8-sig")
    assert "- [x] Tiêu đề đầu tiên" in readable
    assert "- [x] Tiêu đề thứ hai" in readable


def test_corrupt_history_is_quarantined_instead_of_crashing(tmp_path):
    path = tmp_path / "content_history.json"
    path.write_text("not-json", encoding="utf-8")
    store = ContentHistoryStore(path)

    assert store.titles() == ()
    assert (tmp_path / "content_history.corrupt.json").is_file()


def test_unuploaded_entry_can_be_removed_without_touching_confirmed_entries(tmp_path):
    store = ContentHistoryStore(
        tmp_path / "content_history.json",
        tmp_path / "CHECKLIST.txt",
    )
    old_video = tmp_path / "old.mp4"
    old_package = tmp_path / "old.txt"
    new_video = tmp_path / "new.mp4"
    assert store.add("Phiên cũ chưa upload", video_path=old_video, package_path=old_package)
    assert store.add("Video đã đăng", video_path=new_video)

    assert store.remove(package_path=old_package) == 1
    assert store.titles() == ("Video đã đăng",)
    assert "Phiên cũ chưa upload" not in (tmp_path / "CHECKLIST.txt").read_text(encoding="utf-8")
