from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pytest

from docurandom.content_history import ContentHistoryStore
from docurandom.session_archive import (
    SessionArchiveError,
    archive_confirmed_youtube_session,
    archive_skipped_session,
    archive_uploaded_session,
    deferred_public_item_is_old_enough,
    is_deferred_public_not_found_error,
    load_deferred_public_queue,
    load_schedulable_deferred_public_queue,
    mark_deferred_public_published,
    quarantine_deferred_public_item,
    reconcile_deferred_public_queue_for_schedule_start,
    reconcile_deleted_workspace_session,
    reconcile_uploaded_content_history,
    reset_workspace_session_state,
)
from docurandom.workspace import Workspace
from docurandom.workspace_scheduler import VIETNAM_TIMEZONE


def _workspace(root: Path) -> Workspace:
    created = datetime.now(timezone.utc).isoformat()
    workspace = Workspace("workspace-archive", "Archive", str(root), created, created)
    workspace.ensure_layout()
    return workspace


def _write_upload_package(path: Path, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"TIÊU ĐỀ\n{title}\n\nMÔ TẢ\nMô tả đã xác nhận.\n\nTỪ KHÓA SEO\nkiểm tra\n",
        encoding="utf-8",
    )


def test_confirmed_upload_archives_full_session_and_leaves_clean_layout(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    video = workspace.output_dir / "Video.mp4"
    package = workspace.output_dir / "Tong hop.txt"
    thumbnail = workspace.media_dir / "thumbnail.png"
    voice = workspace.voicevox_dir / "voice.wav"
    prompt = workspace.prompt_dir / "master.txt"
    video.write_bytes(b"video")
    created_at = datetime(2026, 8, 28, 18, 45, tzinfo=VIETNAM_TIMEZONE)
    os.utime(video, (created_at.timestamp(), created_at.timestamp()))
    package.write_text("package", encoding="utf-8")
    thumbnail.write_bytes(b"image")
    voice.write_bytes(b"voice")
    prompt.write_text("keep this template", encoding="utf-8")
    history = ContentHistoryStore(
        workspace.content_history_path,
        workspace.content_history_text_path,
    )
    history.add("Nội dung đã làm")
    workspace.pipeline_checkpoint_path.write_text("{}", encoding="utf-8")
    workspace.flow_history_path.write_text("{}", encoding="utf-8")
    (workspace.state_dir / "youtube_upload.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "identity": "a" * 64,
                "youtube_url": "https://youtu.be/example",
            }
        ),
        encoding="utf-8",
    )
    (workspace.state_dir / "thumbnail-ai.json").write_text(
        json.dumps({"status": "complete", "submitted": True}),
        encoding="utf-8",
    )

    archive = archive_uploaded_session(
        workspace,
        video_path=video,
        package_path=package,
        thumbnail_path=thumbnail,
        youtube_url="https://youtu.be/example",
        profile_name="Test",
        channel_name="Channel",
        now=datetime(2026, 8, 28, 19, 30, tzinfo=VIETNAM_TIMEZONE),
    )

    assert archive.archive_dir.name.startswith("2026-08-28_18-45-00_Video_")
    assert archive.video_path.name == "2026-08-28_18-45-00_Video.mp4"
    assert archive.video_path.read_bytes() == b"video"
    assert archive.package_path.read_text(encoding="utf-8") == "package"
    assert archive.thumbnail_path is not None
    assert archive.thumbnail_path.read_bytes() == b"image"
    assert (archive.archive_dir / "VOICEVOX" / "voice.wav").is_file()
    assert (archive.archive_dir / "Prompts" / "master.txt").is_file()
    assert (archive.archive_dir / "State" / "youtube_upload.json").is_file()
    assert (archive.archive_dir / "State" / "thumbnail-ai.json").is_file()
    assert (archive.archive_dir / "archive_manifest.json").is_file()
    assert not tuple(workspace.output_dir.iterdir())
    assert not tuple(workspace.media_dir.iterdir())
    assert not tuple(workspace.voicevox_dir.iterdir())
    assert prompt.is_file()
    assert not workspace.pipeline_checkpoint_path.exists()
    assert not workspace.flow_history_path.exists()
    assert not (workspace.state_dir / "youtube_upload.json").exists()
    assert not (workspace.state_dir / "thumbnail-ai.json").exists()
    assert (workspace.state_dir / "settings.json").is_file() is False
    assert (workspace.state_dir / "uploaded_sessions.json").is_file()
    assert history.titles() == ("Nội dung đã làm",)
    assert workspace.content_history_text_path.is_file()
    assert (archive.archive_dir / "State" / "content_history.json").is_file()


def test_uploaded_history_reconciliation_backfills_confirmed_sessions_and_survives_cleanup(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    first_archive = workspace.uploaded_dir / "first-session"
    first_package = first_archive / "Output" / "first.txt"
    _write_upload_package(first_package, "Tiêu đề upload thứ nhất")

    second_archive = workspace.uploaded_dir / "second-session"
    second_archive.mkdir(parents=True)
    second_checkpoint = second_archive / "State" / "youtube_upload.json"
    second_checkpoint.parent.mkdir(parents=True)
    second_checkpoint.write_text(
        json.dumps({"title": "Tiêu đề upload thứ hai"}),
        encoding="utf-8",
    )

    (workspace.state_dir / "uploaded_sessions.json").write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": {
                    "first": {
                        "status": "complete",
                        "archive_dir": str(first_archive),
                        "package_path": str(first_package),
                        "youtube_url": "https://youtu.be/first",
                    },
                    "second": {
                        "status": "pending_public",
                        "archive_dir": str(second_archive),
                        "youtube_url": "https://youtu.be/second",
                    },
                    "uncertain": {
                        "status": "uploading",
                        "archive_dir": str(workspace.uploaded_dir / "uncertain"),
                        "title": "Không được ghi",
                        "youtube_url": "https://youtu.be/uncertain",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    result = reconcile_uploaded_content_history(workspace)

    assert result.added_titles == (
        "Tiêu đề upload thứ nhất",
        "Tiêu đề upload thứ hai",
    )
    assert ContentHistoryStore(
        workspace.content_history_path,
        workspace.content_history_text_path,
    ).titles() == result.added_titles
    registry = json.loads(
        (workspace.state_dir / "uploaded_sessions.json").read_text(encoding="utf-8")
    )
    assert registry["sessions"]["first"]["title"] == "Tiêu đề upload thứ nhất"
    assert registry["sessions"]["second"]["title"] == "Tiêu đề upload thứ hai"

    second_archive_contents = tuple(second_archive.rglob("*"))
    for path in sorted(second_archive_contents, reverse=True):
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    first_package.unlink()
    (first_archive / "Output").rmdir()
    first_archive.rmdir()

    rerun = reconcile_uploaded_content_history(workspace)
    assert rerun.added_titles == ()
    assert ContentHistoryStore(
        workspace.content_history_path,
        workspace.content_history_text_path,
    ).titles() == (
        "Tiêu đề upload thứ nhất",
        "Tiêu đề upload thứ hai",
    )


def test_archive_refuses_to_move_anything_without_confirmed_upload(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    video = workspace.output_dir / "Video.mp4"
    package = workspace.output_dir / "Tong hop.txt"
    video.write_bytes(b"video")
    package.write_text("package", encoding="utf-8")
    (workspace.state_dir / "youtube_upload.json").write_text(
        json.dumps({"status": "uncertain", "identity": "b" * 64}),
        encoding="utf-8",
    )

    with pytest.raises(SessionArchiveError, match="chưa hoàn tất"):
        archive_uploaded_session(
            workspace,
            video_path=video,
            package_path=package,
            thumbnail_path=None,
            youtube_url="https://youtu.be/unconfirmed",
            profile_name="Test",
            channel_name="Channel",
        )

    assert video.is_file()
    assert package.is_file()
    assert not tuple(workspace.uploaded_dir.iterdir())


def test_deferred_public_session_is_packaged_and_promoted_from_durable_queue(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    video = workspace.output_dir / "Evening.mp4"
    package = workspace.output_dir / "Evening.txt"
    video.write_bytes(b"video")
    package.write_text("package", encoding="utf-8")
    identity = "c" * 64
    attempt_id = "attempt-evening"
    (workspace.state_dir / "youtube_upload.json").write_text(
        json.dumps(
            {
                "status": "deferred_public",
                "identity": identity,
                "attempt_id": attempt_id,
                "workspace_id": workspace.workspace_id,
                "profile_id": "profile-evening",
                "profile_name": "Evening profile",
                "channel_name": "Evening channel",
                "video_path": str(video),
                "package_path": str(package),
                "youtube_url": "https://youtu.be/Evening123",
                "privacy": "UNLISTED",
                "final_privacy": "PUBLIC",
                "title": "Evening title",
                "description": "Evening description",
                "seo_tags": ["evening"],
            }
        ),
        encoding="utf-8",
    )
    workspace.settings_path.write_text(
        json.dumps(
            {
                "schedule_active_shift_id": "daily-15-05",
                "schedule_active_occurrence_id": "old-occurrence",
                "schedule_active_stage": "deferred-public",
                "schedule_deferred_public": True,
                "pipeline_render_pending": True,
            }
        ),
        encoding="utf-8",
    )

    archive = archive_confirmed_youtube_session(workspace)

    assert archive is not None
    assert archive.video_path.is_file()
    assert not video.exists()
    assert not package.exists()
    queue = load_deferred_public_queue(workspace)
    assert len(queue) == 1
    assert queue[0]["youtube_url"] == "https://youtu.be/Evening123"
    assert queue[0]["privacy"] == "UNLISTED"
    assert queue[0]["final_privacy"] == "PUBLIC"
    settings = json.loads(workspace.settings_path.read_text(encoding="utf-8"))
    assert settings["schedule_active_stage"] == ""
    assert settings["schedule_deferred_public"] is False
    registry = json.loads(
        (workspace.state_dir / "uploaded_sessions.json").read_text(encoding="utf-8")
    )
    assert registry["sessions"][identity]["status"] == "pending_public"

    assert mark_deferred_public_published(
        workspace,
        identity,
        now=datetime(2026, 8, 29, 4, 30, tzinfo=VIETNAM_TIMEZONE),
    )
    assert load_deferred_public_queue(workspace) == ()
    registry = json.loads(
        (workspace.state_dir / "uploaded_sessions.json").read_text(encoding="utf-8")
    )
    assert registry["sessions"][identity]["status"] == "complete"
    assert registry["sessions"][identity]["visibility_status"] == "PUBLIC"
    manifest = json.loads(
        (archive.archive_dir / "archive_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["visibility_status"] == "PUBLIC"


def test_schedule_start_quarantines_legacy_pending_public_items(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    old_item = {
        "identity": "old" * 16,
        "youtube_url": "https://youtu.be/old",
        "privacy": "UNLISTED",
        "final_privacy": "PUBLIC",
        "queued_at": "2026-09-18T14:00:00+07:00",
    }
    current_item = {
        "identity": "current" * 8,
        "youtube_url": "https://youtu.be/current",
        "privacy": "UNLISTED",
        "final_privacy": "PUBLIC",
        "queued_at": "2026-09-21T00:45:00+07:00",
    }
    (workspace.state_dir / "deferred_public_queue.json").write_text(
        json.dumps({"version": 1, "items": [old_item, current_item]}),
        encoding="utf-8",
    )

    active = reconcile_deferred_public_queue_for_schedule_start(
        workspace,
        started_at=datetime(2026, 9, 21, 0, 40, tzinfo=VIETNAM_TIMEZONE),
    )

    assert [item["identity"] for item in active] == [current_item["identity"]]
    assert [item["identity"] for item in load_deferred_public_queue(workspace)] == [
        current_item["identity"]
    ]
    stale = json.loads(
        (workspace.state_dir / "deferred_public_stale.json").read_text(
            encoding="utf-8"
        )
    )
    assert stale["items"][0]["item"]["identity"] == old_item["identity"]


def test_invalid_deferred_queue_entry_is_quarantined_and_does_not_block(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    invalid = {
        "identity": "invalid" * 10,
        "youtube_url": "https://youtu.be/missing-profile",
        "privacy": "UNLISTED",
        "final_privacy": "PUBLIC",
    }
    valid = {
        "identity": "valid" * 12,
        "profile_id": "profile-1",
        "youtube_url": "https://youtu.be/valid-video",
        "privacy": "UNLISTED",
        "final_privacy": "PUBLIC",
    }
    (workspace.state_dir / "deferred_public_queue.json").write_text(
        json.dumps({"version": 1, "items": [invalid, valid]}),
        encoding="utf-8",
    )

    active = load_schedulable_deferred_public_queue(workspace)

    assert [item["identity"] for item in active] == [valid["identity"]]
    assert [item["identity"] for item in load_deferred_public_queue(workspace)] == [
        valid["identity"]
    ]
    stale = json.loads(
        (workspace.state_dir / "deferred_public_stale.json").read_text(
            encoding="utf-8"
        )
    )
    assert stale["items"][0]["reason"] == (
        "queue-entry-invalid-no-confirmed-unlisted-proof"
    )


def test_old_remote_missing_queue_item_is_recoverably_quarantined(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    identity = "remote-stale" * 8
    archive_dir = workspace.uploaded_dir / "archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "archive_manifest.json").write_text(
        json.dumps({"visibility_status": "UNLISTED_PENDING_PUBLIC"}),
        encoding="utf-8",
    )
    item = {
        "identity": identity,
        "profile_id": "profile-1",
        "youtube_url": "https://youtu.be/remote-missing",
        "privacy": "UNLISTED",
        "final_privacy": "PUBLIC",
        "queued_at": "2026-09-21T08:00:00+07:00",
        "archive_dir": str(archive_dir),
    }
    (workspace.state_dir / "deferred_public_queue.json").write_text(
        json.dumps({"version": 1, "items": [item]}),
        encoding="utf-8",
    )
    (workspace.state_dir / "uploaded_sessions.json").write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": {
                    identity: {
                        "status": "pending_public",
                        "archive_dir": str(archive_dir),
                        "youtube_url": item["youtube_url"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert deferred_public_item_is_old_enough(
        item,
        now=datetime(2026, 9, 21, 8, 20, tzinfo=VIETNAM_TIMEZONE),
    )
    assert quarantine_deferred_public_item(
        workspace,
        identity,
        reason="remote-exact-video-not-found: test",
        now=datetime(2026, 9, 21, 8, 20, tzinfo=VIETNAM_TIMEZONE),
    )
    assert load_deferred_public_queue(workspace) == ()
    stale = json.loads(
        (workspace.state_dir / "deferred_public_stale.json").read_text(
            encoding="utf-8"
        )
    )
    assert stale["items"][0]["item"]["identity"] == identity
    registry = json.loads(
        (workspace.state_dir / "uploaded_sessions.json").read_text(encoding="utf-8")
    )
    assert registry["sessions"][identity]["status"] == "stale_pending_public"
    manifest = json.loads(
        (archive_dir / "archive_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["visibility_status"] == "STALE_UNVERIFIED"


def test_remote_missing_classifier_is_strict_and_age_guard_is_fail_closed() -> None:
    assert is_deferred_public_not_found_error(
        "YouTube chưa thấy đúng video ID abc trong Nội dung trước thời hạn"
    )
    assert not is_deferred_public_not_found_error(
        "Donut MCP hết thời gian khi mở profile"
    )
    assert not deferred_public_item_is_old_enough(
        {"queued_at": "2026-09-21T08:00:00+07:00"},
        now=datetime(2026, 9, 21, 8, 14, tzinfo=VIETNAM_TIMEZONE),
    )


def test_failed_schedule_is_moved_to_skipped_and_workspace_is_clean(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    (workspace.output_dir / "partial.txt").write_text("partial", encoding="utf-8")
    (workspace.media_dir / "image.png").write_bytes(b"image")
    (workspace.voicevox_dir / "voice.wav").write_bytes(b"voice")
    prompt = workspace.prompt_dir / "master.txt"
    prompt.write_text("keep this template", encoding="utf-8")
    workspace.pipeline_checkpoint_path.write_text("{}", encoding="utf-8")
    workspace.flow_history_path.write_text("{}", encoding="utf-8")
    (workspace.state_dir / "youtube_upload.json").write_text("{}", encoding="utf-8")
    (workspace.state_dir / "thumbnail-ai.json").write_text("{}", encoding="utf-8")

    # A discarded next video must not remove the previous confirmed upload
    # or the durable Unlisted queue awaiting its Public slot.
    protected = {
        workspace.state_dir / "deferred_public_queue.json": b'{"items":[{"youtube_url":"https://youtu.be/Queued123"}]}',
        workspace.state_dir / "uploaded_sessions.json": b'{"sessions":[{"youtube_url":"https://youtu.be/Public123"}]}',
        workspace.uploaded_dir / "published.mp4": b"confirmed published media",
    }
    workspace.uploaded_dir.mkdir(exist_ok=True)
    for path, content in protected.items():
        path.write_bytes(content)

    archive = archive_skipped_session(
        workspace,
        reason="VOICEVOXERROR",
        shift_id="daily-15-05",
        occurrence_id="youtube-upload:2026-08-30T05:00+07:00",
        now=datetime(2026, 8, 30, 5, 0, tzinfo=VIETNAM_TIMEZONE),
    )

    assert archive.archive_dir.parent.name == "Skipped"
    assert (archive.archive_dir / "Output" / "partial.txt").is_file()
    assert (archive.archive_dir / "Media" / "image.png").is_file()
    assert (archive.archive_dir / "VOICEVOX" / "voice.wav").is_file()
    assert (archive.archive_dir / "Prompts" / "master.txt").is_file()
    manifest = json.loads(
        (archive.archive_dir / "archive_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["reason"] == "VOICEVOXERROR"
    assert not tuple(workspace.output_dir.iterdir())
    assert not tuple(workspace.media_dir.iterdir())
    assert not tuple(workspace.voicevox_dir.iterdir())
    assert prompt.is_file()
    assert not workspace.pipeline_checkpoint_path.exists()
    assert not workspace.flow_history_path.exists()
    assert not (workspace.state_dir / "youtube_upload.json").exists()
    assert not (workspace.state_dir / "thumbnail-ai.json").exists()
    assert all(path.read_bytes() == content for path, content in protected.items())


def test_checkpoint_reset_preserves_workspace_configuration_and_user_files(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    media = workspace.media_dir / "keep.png"
    output = workspace.output_dir / "keep.txt"
    voice = workspace.voicevox_dir / "keep.wav"
    media.write_bytes(b"image")
    output.write_text("package", encoding="utf-8")
    voice.write_bytes(b"voice")
    workspace.pipeline_checkpoint_path.write_text("{}", encoding="utf-8")
    workspace.flow_history_path.write_text("{}", encoding="utf-8")
    (workspace.state_dir / "youtube_upload.json").write_text(
        json.dumps({"status": "error"}),
        encoding="utf-8",
    )
    (workspace.state_dir / "thumbnail-ai.json").write_text("{}", encoding="utf-8")
    settings = {
        "schedule_armed": True,
        "schedule_resume_after_manual_continue": True,
        "schedule_shifts": [{"shift_id": "daily-05-10"}],
        "schedule_active_shift_id": "daily-05-10",
        "schedule_active_occurrence_id": "workflow:today",
        "schedule_active_stage": "voicevox",
        "pipeline_render_pending": True,
        "youtube_profile_id": "profile-1",
        "youtube_group_id": "group-1",
        "source_dir": str(workspace.media_dir),
        "output_dir": str(workspace.output_dir),
    }
    workspace.settings_path.write_text(json.dumps(settings), encoding="utf-8")
    checklist_before = workspace.content_history_text_path.read_bytes()

    result = reset_workspace_session_state(workspace)

    assert result.changed
    assert set(result.removed_state_files) == {
        "pipeline_checkpoint.json",
        "flow_history.json",
        "youtube_upload.json",
        "thumbnail-ai.json",
    }
    restored = json.loads(workspace.settings_path.read_text(encoding="utf-8"))
    assert restored["schedule_armed"] is True
    assert restored["schedule_resume_after_manual_continue"] is True
    assert restored["schedule_shifts"] == settings["schedule_shifts"]
    assert restored["youtube_profile_id"] == "profile-1"
    assert restored["youtube_group_id"] == "group-1"
    assert restored["schedule_active_shift_id"] == ""
    assert restored["schedule_active_occurrence_id"] == ""
    assert restored["schedule_active_stage"] == ""
    assert restored["pipeline_render_pending"] is False
    assert media.read_bytes() == b"image"
    assert output.read_text(encoding="utf-8") == "package"
    assert voice.read_bytes() == b"voice"
    assert workspace.content_history_text_path.read_bytes() == checklist_before


def test_manual_file_deletion_reconciles_orphaned_material_checkpoint(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    workspace.settings_path.write_text(
        json.dumps(
            {
                "schedule_active_shift_id": "daily-15-05",
                "schedule_active_occurrence_id": "workflow:today",
                "schedule_active_stage": "flow",
                "pipeline_render_pending": True,
                "youtube_profile_id": "profile-1",
            }
        ),
        encoding="utf-8",
    )
    workspace.pipeline_checkpoint_path.write_text(
        json.dumps(
            {
                "stages": {
                    "chatgpt": {"status": "complete"},
                    "package": {"status": "complete"},
                    "flow_images": {"status": "error"},
                }
            }
        ),
        encoding="utf-8",
    )

    result = reconcile_deleted_workspace_session(workspace)

    assert result.changed
    assert not workspace.pipeline_checkpoint_path.exists()
    restored = json.loads(workspace.settings_path.read_text(encoding="utf-8"))
    assert restored["youtube_profile_id"] == "profile-1"
    assert restored["schedule_active_shift_id"] == ""
    assert restored["pipeline_render_pending"] is False


def test_empty_new_chatgpt_error_remains_available_for_continue(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    workspace.pipeline_checkpoint_path.write_text(
        json.dumps({"stages": {"chatgpt": {"status": "error"}}}),
        encoding="utf-8",
    )

    result = reconcile_deleted_workspace_session(workspace)

    assert not result.changed
    assert workspace.pipeline_checkpoint_path.is_file()


def test_deleting_managed_folder_is_unambiguous_external_reset(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    workspace.pipeline_checkpoint_path.write_text(
        json.dumps({"stages": {"chatgpt": {"status": "error"}}}),
        encoding="utf-8",
    )
    workspace.media_dir.rmdir()

    result = reconcile_deleted_workspace_session(workspace)

    assert result.changed
    assert workspace.media_dir.is_dir()
    assert not workspace.pipeline_checkpoint_path.exists()
