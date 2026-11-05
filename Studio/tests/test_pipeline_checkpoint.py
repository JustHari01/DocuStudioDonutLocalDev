from pathlib import Path

from docurandom.pipeline_checkpoint import (
    inspect_workspace_pipeline,
    pipeline_resume_summary,
    read_pipeline_checkpoint,
    update_pipeline_stage,
)
from docurandom.storage import atomic_write_json


def test_pipeline_checkpoint_preserves_every_completed_stage(tmp_path: Path) -> None:
    path = tmp_path / "pipeline.json"
    identity = {
        "workspace_id": "workspace-a",
        "project_name": "Video A",
        "minutes": 15.0,
        "master_prompt_sha256": "abc",
    }
    update_pipeline_stage(
        path,
        identity=identity,
        stage="chatgpt",
        status="complete",
        artifacts={"checkpoint": "chat.json"},
    )
    update_pipeline_stage(
        path,
        identity=identity,
        stage="flow_images",
        status="error",
        detail="dừng ở ảnh 21",
        artifacts={"valid_images": 20},
    )
    document = read_pipeline_checkpoint(path)
    assert document["stages"]["chatgpt"]["status"] == "complete"
    assert document["stages"]["flow_images"]["artifacts"]["valid_images"] == 20
    resume_stage, report = pipeline_resume_summary(path)
    # Missing precheck is the earliest incomplete stage and is intentionally
    # rechecked before continuing any external engine.
    assert resume_stage == "precheck"
    assert any("dừng ở ảnh 21" in line for line in report)


def test_new_project_identity_does_not_reuse_another_projects_stage_state(tmp_path: Path) -> None:
    path = tmp_path / "pipeline.json"
    first = {"workspace_id": "one", "project_name": "A"}
    second = {"workspace_id": "two", "project_name": "B"}
    update_pipeline_stage(path, identity=first, stage="render", status="complete")
    update_pipeline_stage(path, identity=second, stage="precheck", status="running")
    document = read_pipeline_checkpoint(path)
    assert document["identity"] == second
    assert "render" not in document["stages"]


def test_workspace_outcome_recovers_completed_video_after_restart(tmp_path: Path) -> None:
    checkpoint = tmp_path / ".docurandom" / "pipeline_checkpoint.json"
    history = tmp_path / ".docurandom" / "render_history.json"
    output = tmp_path / "Output"
    output.mkdir(parents=True)
    first = output / "tin-nhat-ban.mp4"
    first.write_bytes(b"video")
    identity = {"workspace_id": "workspace-a", "project_name": "Tin Nhật"}
    update_pipeline_stage(
        checkpoint,
        identity=identity,
        stage="flow_images",
        status="error",
        detail="lỗi cũ đã được sửa",
    )
    update_pipeline_stage(
        checkpoint,
        identity=identity,
        stage="render",
        status="complete",
        detail=first.name,
        artifacts={"video": str(first)},
    )
    atomic_write_json(
        history,
        {"version": 1, "renders": [{"output_file": str(first)}]},
    )

    outcome = inspect_workspace_pipeline(checkpoint, history, output)

    assert outcome.state == "completed"
    assert outcome.progress == 100.0
    assert outcome.video_name == first.name
    assert outcome.video_count == 1
    assert outcome.resume_stage == "complete"


def test_workspace_outcome_keeps_error_and_resume_stage(tmp_path: Path) -> None:
    checkpoint = tmp_path / ".docurandom" / "pipeline_checkpoint.json"
    history = tmp_path / ".docurandom" / "render_history.json"
    output = tmp_path / "Output"
    output.mkdir(parents=True)
    identity = {"workspace_id": "workspace-b", "project_name": "Video B"}
    for stage in ("precheck", "chatgpt", "package"):
        update_pipeline_stage(
            checkpoint,
            identity=identity,
            stage=stage,
            status="complete",
        )
    update_pipeline_stage(
        checkpoint,
        identity=identity,
        stage="flow_images",
        status="error",
        detail="CAPTCHA_TIMEOUT",
    )

    outcome = inspect_workspace_pipeline(checkpoint, history, output)

    assert outcome.state == "error"
    assert outcome.resume_stage == "flow_images"
    assert "CAPTCHA_TIMEOUT" in outcome.status
    assert outcome.video_name == ""


def test_workspace_outcome_detects_existing_video_without_checkpoint(tmp_path: Path) -> None:
    checkpoint = tmp_path / ".docurandom" / "pipeline_checkpoint.json"
    history = tmp_path / ".docurandom" / "render_history.json"
    output = tmp_path / "Output"
    output.mkdir(parents=True)
    video = output / "video-da-render.mkv"
    video.write_bytes(b"video")

    outcome = inspect_workspace_pipeline(checkpoint, history, output)

    assert outcome.state == "completed"
    assert outcome.video_name == video.name
