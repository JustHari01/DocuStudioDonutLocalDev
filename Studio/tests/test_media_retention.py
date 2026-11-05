from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import MagicMock

import pytest

from docurandom.media_retention import prune_workspace_media
from docurandom.session_archive import archive_skipped_session, archive_uploaded_session, confirmed_uploaded_session_matches
from docurandom.workspace import Workspace
from docurandom.workspace_scheduler import VIETNAM_TIMEZONE


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def workspace(tmp_path):
    ws = Workspace("test-ws", "Test", str(tmp_path / "workspace"), "now", "now")
    ws.ensure_layout()
    write(ws.state_dir / "uploaded_sessions.json", {"sessions": {}})
    write(ws.state_dir / "deferred_public_queue.json", {"items": []})
    return ws


def archive(ws, identity, hour, *, public=True, skipped=False):
    directory = ws.root / ("Skipped" if skipped else "Uploaded") / identity
    for name, body in {"Output/video.mp4": b"video", "Media/image.png": b"image",
                       "VOICEVOX/voice.wav": b"voice", "Selected Thumbnail/current.png": b"thumb",
                       "Output/package.txt": b"metadata", "State/youtube_upload.json": b"{}",
                       "Prompts/master.txt": b"prompt", "VOICEVOX/subtitle.srt": b"subtitle"}.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    stamp = f"2026-10-05T{hour:02}:00:00+07:00"
    url = "https://youtu.be/" + identity.ljust(11, "x")[:11]
    manifest = {"schema": "docurandom-skipped-session-v1" if skipped else "docurandom-uploaded-session-v1",
                "archive_id": identity, "archived_at_vietnam": stamp, "youtube_url": url,
                "visibility_status": "PUBLIC" if public else "UNLISTED_PENDING_PUBLIC"}
    write(directory / "archive_manifest.json", manifest)
    if not skipped:
        registry_path = ws.state_dir / "uploaded_sessions.json"
        registry = json.loads(registry_path.read_text())
        registry["sessions"][identity] = {"status": "complete" if public else "pending_public",
            "archive_dir": str(directory), "video_path": str(directory / "Output/video.mp4"),
            "package_path": str(directory / "Output/package.txt"), "youtube_url": url,
            "visibility_status": manifest["visibility_status"], "archived_at": stamp, "title": identity}
        write(registry_path, registry)
        if not public:
            queue_path = ws.state_dir / "deferred_public_queue.json"
            queue = json.loads(queue_path.read_text())
            queue["items"].append({"identity": identity, "archive_dir": str(directory),
                "youtube_url": url, "privacy": "UNLISTED", "final_privacy": "PUBLIC"})
            write(queue_path, queue)
    return directory


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_public_waits_for_confirmed_successor_and_metadata_survives(tmp_path):
    ws = workspace(tmp_path)
    a = archive(ws, "a", 10)
    active = ws.output_dir / "B.mp4"
    active.write_bytes(b"incomplete successor")
    write(ws.state_dir / "youtube_upload.json", {"identity": "b", "status": "uncertain", "video_path": str(active)})
    assert prune_workspace_media(ws).removed_files == 0
    assert (a / "Output/video.mp4").is_file()
    b = archive(ws, "b", 12, public=False)
    (ws.state_dir / "youtube_upload.json").unlink()
    small_files = [a / name for name in ("Output/package.txt", "State/youtube_upload.json",
                  "Prompts/master.txt", "VOICEVOX/subtitle.srt")]
    before = {p: digest(p) for p in small_files}
    registry_hash = digest(ws.state_dir / "uploaded_sessions.json")
    queue_hash = digest(ws.state_dir / "deferred_public_queue.json")
    result = prune_workspace_media(ws)
    assert result.removed_files == 4 and result.removed_bytes == 20
    assert not (a / "Output/video.mp4").exists()
    assert (b / "Output/video.mp4").is_file() and active.read_bytes() == b"incomplete successor"
    assert {p: digest(p) for p in small_files} == before
    assert digest(ws.state_dir / "uploaded_sessions.json") == registry_hash
    assert digest(ws.state_dir / "deferred_public_queue.json") == queue_hash
    assert confirmed_uploaded_session_matches(ws, "a", "https://youtu.be/axxxxxxxxxx")
    assert prune_workspace_media(ws).removed_files == 0


def test_local_render_and_upload_failure_do_not_release_public(tmp_path):
    ws = workspace(tmp_path)
    a = archive(ws, "a", 10)
    write(ws.pipeline_checkpoint_path, {"stages": {"render": {"status": "complete"}}})
    write(ws.state_dir / "youtube_upload.json", {"identity": "b", "status": "failed"})
    assert prune_workspace_media(ws).removed_files == 0
    assert (a / "Output/video.mp4").exists()


def test_public_kept_until_upload_after_its_public_confirmation(tmp_path):
    ws = workspace(tmp_path)
    a = archive(ws, "a", 10)
    archive(ws, "b", 12, public=False)
    registry_path = ws.state_dir / "uploaded_sessions.json"
    registry = json.loads(registry_path.read_text())
    registry["sessions"]["a"]["published_at"] = "2026-10-05T13:30:00+07:00"
    write(registry_path, registry)
    assert prune_workspace_media(ws).removed_files == 0
    archive(ws, "c", 14, public=False)
    assert prune_workspace_media(ws).removed_files == 4
    assert not (a / "Output/video.mp4").exists()


def test_keeps_newest_public_without_successor_and_timestamp_ties(tmp_path):
    ws = workspace(tmp_path)
    archive(ws, "a", 10)
    b = archive(ws, "b", 12)
    c = archive(ws, "c", 12)
    assert prune_workspace_media(ws).removed_files == 4
    assert (b / "Output/video.mp4").exists() and (c / "Output/video.mp4").exists()


def test_legacy_policy_never_overrides_active_unlisted_queue(tmp_path):
    ws = workspace(tmp_path)
    a = archive(ws, "a", 10, public=False)
    b = archive(ws, "b", 12, public=False)
    write(ws.state_dir / "media_retention_policy.json", {"discard_archives_before": "2026-10-05T15:00:00+07:00"})
    assert prune_workspace_media(ws).removed_files == 0
    assert (a / "Output/video.mp4").exists() and (b / "Output/video.mp4").exists()


@pytest.mark.parametrize("protection", ["identity", "youtube_url", "path", "archived-recovery"])
def test_live_upload_and_recovery_references_override_cleanup(tmp_path, protection):
    ws = workspace(tmp_path)
    a = archive(ws, "a", 10)
    archive(ws, "b", 12, public=False)
    filename = "youtube_archived_recovery.json" if protection == "archived-recovery" else "youtube_upload.json"
    checkpoint = {"status": "uncertain"}
    if protection in {"identity", "archived-recovery"}:
        checkpoint["identity"] = "a"
    elif protection == "youtube_url":
        checkpoint["youtube_url"] = "https://youtu.be/axxxxxxxxxx"
    else:
        checkpoint["video_path"] = str(a / "Output/video.mp4")
    write(ws.state_dir / filename, checkpoint)
    assert prune_workspace_media(ws).removed_files == 0
    assert (a / "Output/video.mp4").exists()


@pytest.mark.parametrize("filename,body", [
    ("uploaded_sessions.json", "{"), ("deferred_public_queue.json", "{"),
    ("deferred_public_queue.json", '{"items":[{}]}'),
    ("deferred_public_queue.json", '{"items":"bad"}'),
    ("youtube_archived_recovery.json", "{"), ("pipeline_checkpoint.json", "[]"),
    ("settings.json", "{"), ("media_retention_policy.json", '{"discard_archives_before":"bad"}'),
])
def test_corrupt_evidence_fails_closed(tmp_path, filename, body):
    ws = workspace(tmp_path)
    a = archive(ws, "a", 10)
    archive(ws, "b", 12, public=False)
    (ws.state_dir / filename).write_text(body, encoding="utf-8")
    result = prune_workspace_media(ws)
    assert result.blocked_reason and result.removed_files == 0
    assert (a / "Output/video.mp4").exists()


def test_uncertain_or_unqueued_unlisted_archive_is_retained(tmp_path):
    ws = workspace(tmp_path)
    a = archive(ws, "a", 10, public=False)
    write(ws.state_dir / "deferred_public_queue.json", {"items": []})
    archive(ws, "b", 12)
    assert prune_workspace_media(ws).removed_files == 0
    assert (a / "Output/video.mp4").exists()


def test_only_explicit_legacy_discard_cleans_old_stale_unlisted(tmp_path):
    ws = workspace(tmp_path)
    a = archive(ws, "a", 10, public=False)
    write(ws.state_dir / "deferred_public_queue.json", {"items": []})
    assert prune_workspace_media(ws).removed_files == 0
    write(ws.state_dir / "media_retention_policy.json", {"discard_archives_before": "2026-10-05T11:00:00+07:00"})
    assert prune_workspace_media(ws).removed_files == 4
    assert not (a / "Output/video.mp4").exists()


def test_skipped_payload_deleted_but_active_reference_and_small_state_kept(tmp_path):
    ws = workspace(tmp_path)
    old = archive(ws, "skip-old", 10, skipped=True)
    active = archive(ws, "skip-active", 12, skipped=True)
    write(ws.pipeline_checkpoint_path, {"stages": {"render": {"artifacts": {"source": str(active / "Media")}}}})
    assert prune_workspace_media(ws).removed_files == 4
    assert not (old / "Output/video.mp4").exists()
    assert (old / "State/youtube_upload.json").exists()
    assert (active / "Output/video.mp4").exists()


def test_outside_registry_path_never_deleted(tmp_path):
    ws = workspace(tmp_path)
    a = archive(ws, "a", 10)
    archive(ws, "b", 12)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "video.mp4").write_bytes(b"external")
    registry_path = ws.state_dir / "uploaded_sessions.json"
    registry = json.loads(registry_path.read_text())
    registry["sessions"]["a"]["archive_dir"] = str(outside)
    write(registry_path, registry)
    result = prune_workspace_media(ws)
    assert result.errors and result.removed_files == 0
    assert (a / "Output/video.mp4").exists()
    assert (outside / "video.mp4").read_bytes() == b"external"


def test_junction_or_symlink_in_payload_retains_archive(tmp_path):
    ws = workspace(tmp_path)
    a = archive(ws, "a", 10)
    archive(ws, "b", 12)
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "external.mp4").write_bytes(b"do not delete")
    link = a / "Media" / "linked"
    if os.name == "nt":
        def psquote(p):
            return "'" + str(p).replace("'", "''") + "'"
        subprocess.run(["powershell.exe", "-NoProfile", "-Command",
            f"New-Item -ItemType Junction -Path {psquote(link)} -Target {psquote(outside)} | Out-Null"], check=True)
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        result = prune_workspace_media(ws)
        assert result.errors and result.removed_files == 0
        assert (a / "Output/video.mp4").exists()
        assert (outside / "external.mp4").read_bytes() == b"do not delete"
    finally:
        assert link.absolute().is_relative_to(ws.root.absolute())
        if os.name == "nt":
            link.rmdir()  # remove the junction itself, never its target
        else:
            link.unlink()


def test_dry_run_and_disabled_policy_never_write_or_delete(tmp_path):
    ws = workspace(tmp_path)
    archive(ws, "a", 10)
    archive(ws, "b", 12)
    before = {str(p): digest(p) for p in ws.root.rglob("*") if p.is_file()}
    result = prune_workspace_media(ws, dry_run=True)
    assert result.removed_files == 4 and result.removed_bytes == 20
    assert before == {str(p): digest(p) for p in ws.root.rglob("*") if p.is_file()}
    write(ws.state_dir / "media_retention_policy.json", {"enabled": False})
    assert prune_workspace_media(ws).removed_files == 0


def test_locked_file_does_not_lose_confirmation_and_cleanup_retries(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    a = archive(ws, "a", 10)
    archive(ws, "b", 12, public=False)
    video = a / "Output/video.mp4"
    original = Path.unlink
    def locked(path, *args, **kwargs):
        if path == video:
            raise PermissionError("locked")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", locked)
    first = prune_workspace_media(ws)
    assert first.removed_files == 3 and first.errors and video.exists()
    monkeypatch.setattr(Path, "unlink", original)
    second = prune_workspace_media(ws)
    assert second.removed_files == 1 and not second.errors and not video.exists()
    cleanup = json.loads((a / "archive_manifest.json").read_text())["local_media_cleanup"]
    assert cleanup["status"] == "complete" and cleanup["removed_files"] == 4 and cleanup["removed_bytes"] == 20
    assert confirmed_uploaded_session_matches(ws, "a", "https://youtu.be/axxxxxxxxxx")


def test_real_archive_commit_triggers_retention_only_after_successor_commit(tmp_path):
    ws = workspace(tmp_path)
    def commit(identity, hour, deferred):
        video = ws.output_dir / "video.mp4"
        video.write_bytes(b"video")
        package = ws.output_dir / "package.txt"
        package.write_text("TIÊU ĐỀ\nTitle " + identity, encoding="utf-8")
        url = "https://youtu.be/" + identity[:11]
        write(ws.state_dir / "youtube_upload.json", {"identity": identity,
            "status": "deferred_public" if deferred else "complete", "profile_id": "profile",
            "privacy": "UNLISTED" if deferred else "PUBLIC", "final_privacy": "PUBLIC",
            "deferred_public": deferred, "youtube_url": url})
        return archive_uploaded_session(ws, video_path=video, package_path=package, thumbnail_path=None,
            youtube_url=url, profile_name="Test", channel_name="Channel",
            pending_public=deferred,
            now=datetime(2026, 10, 5, hour, tzinfo=VIETNAM_TIMEZONE))
    a = commit("a" * 64, 10, False)
    assert a.video_path.exists()
    b = commit("b" * 64, 12, True)
    assert b.video_path.exists() and b.package_path.exists()
    assert not a.video_path.exists() and a.package_path.exists()
    assert confirmed_uploaded_session_matches(ws, "a" * 64, "https://youtu.be/aaaaaaaaaaa")


def test_explicit_skip_ui_transition_reclaims_payload_immediately(tmp_path):
    from docurandom.ui import DocuRandomApp

    ws = workspace(tmp_path)
    (ws.output_dir / "abandoned.mp4").write_bytes(b"discarded video")
    (ws.media_dir / "image.png").write_bytes(b"discarded image")
    (ws.voicevox_dir / "audio.wav").write_bytes(b"discarded voice")
    write(ws.pipeline_checkpoint_path, {"stages": {"render": {"status": "cancelled"}}})
    skipped = archive_skipped_session(ws, reason="User Skip", shift_id="manual-skip", occurrence_id="manual")
    assert (skipped.archive_dir / "Output/abandoned.mp4").exists()
    app = MagicMock()
    app.workspace = ws
    app.audio_var.get.return_value = ""
    DocuRandomApp._apply_skipped_session_archive(app, skipped)
    assert not (skipped.archive_dir / "Output/abandoned.mp4").exists()
    assert not (skipped.archive_dir / "Media/image.png").exists()
    assert not (skipped.archive_dir / "VOICEVOX/audio.wav").exists()
    assert (skipped.archive_dir / "State/pipeline_checkpoint.json").exists()
    assert json.loads((skipped.archive_dir / "archive_manifest.json").read_text())["reason"] == "User Skip"
