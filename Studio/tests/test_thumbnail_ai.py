from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
from datetime import timedelta
import pytest

from docurandom.thumbnail_ai import (
    claim_thumbnail_ai_generation,
    extract_complete_thumbnail_prompt,
    is_thumbnail_confirmation,
    is_thumbnail_negative_confirmation,
    read_thumbnail_ai_state,
    thumbnail_generation_request,
    update_thumbnail_ai_state,
    reserve_thumbnail_ai_automatic_retry,
    thumbnail_ai_terminal_retry_remaining_seconds,
)
from docurandom.thumbnail_manager import install_workspace_thumbnail
from docurandom.workspace import Workspace


def _workspace(root: Path) -> Workspace:
    now = datetime.now(timezone.utc).isoformat()
    workspace = Workspace("thumbnail-ai", "Thumbnail AI", str(root), now, now)
    workspace.ensure_layout()
    return workspace


def _package(path: Path, prompt: str = "A complete cinematic thumbnail prompt") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "TIÊU ĐỀ\nDemo\n\n"
        "MÔ TẢ\nDemo description\n\n"
        "PROMPT THUMBNAIL\n"
        "CHỮ THUMBNAIL\n日本の技術\n\n"
        "BỐ CỤC\nSubject on the right\n\n"
        "PROMPT THUMBNAIL HOÀN CHỈNH\n"
        f"{prompt}\n\n"
        "THOẠI VOICEVOX LIỀN MẠCH\nA: demo\n",
        encoding="utf-8-sig",
    )
    return path


def test_operator_readonly_collection_preserves_terminal_budget_and_receipt(tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    package = _package(workspace.output_dir / "package.txt")
    claim = claim_thumbnail_ai_generation(workspace, package)
    update_thumbnail_ai_state(workspace, package, claim, status="terminal_no_image", submitted=False,
                              thread_id="saved-image-thread", response_id="original-response", automatic_retry_count=2)
    path = workspace.state_dir / "thumbnail-ai.json"
    state = read_thumbnail_ai_state(workspace)
    state["readonly_collection"] = True
    path.write_text(json.dumps(state), encoding="utf-8")
    recovered = claim_thumbnail_ai_generation(workspace, package)
    assert recovered.action == "recover" and recovered.read_only_collection
    unchanged = read_thumbnail_ai_state(workspace)
    assert unchanged["automatic_retry_count"] == 2
    assert unchanged["submitted"] is False
    assert unchanged["response_id"] == "original-response"
    update_thumbnail_ai_state(workspace, package, recovered, status="error", detail="Read only: image not found")
    assert read_thumbnail_ai_state(workspace)["readonly_collection"] is True
    assert claim_thumbnail_ai_generation(workspace, package).read_only_collection


def test_readonly_collection_does_not_apply_to_another_video_chat(tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    package = _package(workspace.output_dir / "package.txt")
    claim = claim_thumbnail_ai_generation(workspace, package)
    update_thumbnail_ai_state(workspace, package, claim, status="terminal_no_image", submitted=False,
                              thread_id="old-image-thread", automatic_retry_count=2)
    path = workspace.state_dir / "thumbnail-ai.json"
    state = read_thumbnail_ai_state(workspace)
    state["readonly_collection"] = True
    path.write_text(json.dumps(state), encoding="utf-8")
    package.with_name(package.stem + "_AI_CHECKPOINT.json").write_text(json.dumps({"thread_id":"new-video-thread"}), encoding="utf-8")
    assert claim_thumbnail_ai_generation(workspace, package).action == "start"
    assert read_thumbnail_ai_state(workspace)["readonly_collection"] is False


def test_exhausted_generation_budget_still_collects_late_images_without_resend(tmp_path):
    from docurandom.thumbnail_ai import thumbnail_ai_automatic_recovery_remaining_seconds, reserve_thumbnail_readonly_probe
    workspace = _workspace(tmp_path / "workspace")
    package = _package(workspace.output_dir / "package.txt")
    original = claim_thumbnail_ai_generation(workspace, package)
    update_thumbnail_ai_state(workspace, package, original, status="submitted", submitted=True,
                              thread_id="accepted-image-thread", response_id="accepted-response",
                              automatic_retry_count=3)
    path = workspace.state_dir / "thumbnail-ai.json"
    for expected_count in range(1, 5):
        state = read_thumbnail_ai_state(workspace)
        state['source_read_version']=2
        state["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        path.write_text(json.dumps(state), encoding="utf-8")
        assert thumbnail_ai_automatic_recovery_remaining_seconds(state) == 0
        claim = claim_thumbnail_ai_generation(workspace, package)
        assert claim.action == "recover"
        assert claim.read_only_collection is True
        saved = read_thumbnail_ai_state(workspace)
        assert saved["submitted"] is True
        assert saved["thread_id"] == "accepted-image-thread"
        assert saved["response_id"] == "accepted-response"
        assert saved["automatic_retry_count"] == 3
        assert saved["readonly_probe_count"] == expected_count-1
        assert reserve_thumbnail_readonly_probe(workspace,claim,'accepted-image-thread')
        assert read_thumbnail_ai_state(workspace)["readonly_probe_count"] == expected_count
        assert not reserve_thumbnail_ai_automatic_retry(
            workspace, package, claim, previous_thread_id="accepted-image-thread",
            new_thread_id="must-not-create")
        update_thumbnail_ai_state(workspace, package, claim, status="error", submitted=True,
                                  detail="Saved page is still loading")
        # The budget survives the failed read and a new claim after restart.
        assert read_thumbnail_ai_state(workspace)["readonly_probe_count"] == expected_count
        assert claim_thumbnail_ai_generation(workspace, package).action == "blocked"
    assert thumbnail_ai_automatic_recovery_remaining_seconds(read_thumbnail_ai_state(workspace)) is None


def test_late_collection_budget_never_unlocks_an_unaccepted_or_terminal_turn(tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    package = _package(workspace.output_dir / "package.txt")
    claim = claim_thumbnail_ai_generation(workspace, package)
    update_thumbnail_ai_state(workspace, package, claim, status="terminal_no_image", submitted=False,
                              thread_id="old-thread", automatic_retry_count=3,
                              detail="CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: KHÔNG TẠO ĐƯỢC")
    assert claim_thumbnail_ai_generation(workspace, package).action == "blocked"


def test_unreadable_terminal_chat_backs_off_without_spending_generation_budget(tmp_path):
    from docurandom.thumbnail_ai import record_thumbnail_ai_loading_failure
    workspace = _workspace(tmp_path / "workspace")
    package = _package(workspace.output_dir / "package.txt")
    claim, _ = terminal_failure(workspace, package)
    for expected in range(1, 5):
        assert record_thumbnail_ai_loading_failure(workspace, package, claim,
                                                   "CHATGPT_CONVERSATION_LOAD_FAILED: keep same chat")
        state = read_thumbnail_ai_state(workspace)
        assert state["automatic_retry_count"] == 0
        assert state["readonly_probe_count"] == expected
        assert state["submitted"] is False
        assert state["thread_id"] == "old-thread"
        if expected < 4:
            assert thumbnail_ai_terminal_retry_remaining_seconds(state) > 0
        else:
            assert thumbnail_ai_terminal_retry_remaining_seconds(state) >= 29 * 60
        assert claim_thumbnail_ai_generation(workspace, package).action == "blocked"


def test_extracts_only_complete_thumbnail_prompt(tmp_path: Path) -> None:
    package = _package(tmp_path / "package.txt")

    assert extract_complete_thumbnail_prompt(package) == "A complete cinematic thumbnail prompt"


@pytest.mark.parametrize("message", [
    "Lượt tạo ảnh trong đúng hội thoại vẫn chưa xác nhận hoàn tất; chờ thu hồi, không gửi lại.",
    "Hội thoại cũ chưa rảnh để thử lại thumbnail an toàn.",
    "CHATGPT_SUBMISSION_UNCONFIRMED: keep receipt",
])
def test_inconclusive_terminal_probes_back_off_even_when_the_chat_loaded(tmp_path, message):
    from docurandom.thumbnail_ai import record_thumbnail_ai_loading_failure
    workspace = _workspace(tmp_path / "workspace")
    package = _package(workspace.output_dir / "package.txt")
    claim, _ = terminal_failure(workspace, package)
    before = read_thumbnail_ai_state(workspace)
    assert record_thumbnail_ai_loading_failure(workspace, package, claim, message)
    after = read_thumbnail_ai_state(workspace)
    assert after["updated_at"] != before["updated_at"]
    assert after["readonly_probe_count"] == 1
    assert after["automatic_retry_count"] == 0
    assert after["thread_id"] == "old-thread"
    assert after["status"] == "terminal_no_image"
    assert after["submitted"] is False
    assert thumbnail_ai_terminal_retry_remaining_seconds(after) >= 14 * 60
    assert claim_thumbnail_ai_generation(workspace, package).action == "blocked"


def test_read_probe_backoff_cannot_relabel_an_accepted_receipt_or_changed_package(tmp_path):
    from docurandom.thumbnail_ai import record_thumbnail_ai_loading_failure
    workspace = _workspace(tmp_path / "workspace")
    package = _package(workspace.output_dir / "package.txt")
    claim, _ = terminal_failure(workspace, package)
    _package(package, "different prompt")
    path = workspace.state_dir / "thumbnail-ai.json"
    original = path.read_bytes()
    assert not record_thumbnail_ai_loading_failure(workspace, package, claim, "no confirmation")
    assert path.read_bytes() == original
    package = _package(package)
    state = read_thumbnail_ai_state(workspace)
    state["submitted"] = True
    path.write_text(json.dumps(state), encoding="utf-8")
    original = path.read_bytes()
    assert not record_thumbnail_ai_loading_failure(workspace, package, claim, "no confirmation")
    assert path.read_bytes() == original


@pytest.mark.parametrize("legacy", [False, True])
def test_bridge_pending_lock_never_becomes_a_fresh_thumbnail_claim_after_restart(tmp_path, legacy):
    from docurandom.thumbnail_ai import thumbnail_ai_automatic_recovery_remaining_seconds
    workspace = _workspace(tmp_path / "workspace")
    package = _package(workspace.output_dir / "package.txt")
    claim = claim_thumbnail_ai_generation(workspace, package)
    message = "ChatGPT đang giữ một lượt chưa thu hồi. Tool sẽ đọc lại đúng cuộc hội thoại."
    update_thumbnail_ai_state(workspace, package, claim, status="error", submitted=False,
                              thread_id="same-video-chat", response_id="old-receipt", detail=message)
    path = workspace.state_dir / "thumbnail-ai.json"
    if legacy:
        state = read_thumbnail_ai_state(workspace)
        state.update(status="error", submitted=False)
        path.write_text(json.dumps(state), encoding="utf-8")
    before = read_thumbnail_ai_state(workspace)
    assert claim_thumbnail_ai_generation(workspace, package).action == "blocked"
    state = read_thumbnail_ai_state(workspace)
    assert state["status"] == "uncertain"
    assert state["submitted"] is True
    assert state["thread_id"] == "same-video-chat"
    assert state["response_id"] == "old-receipt"
    assert state["updated_at"] == before["updated_at"]
    assert state["automatic_retry_count"] == 0
    assert thumbnail_ai_automatic_recovery_remaining_seconds(state) >= 14 * 60
    state["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
    path.write_text(json.dumps(state), encoding="utf-8")
    assert claim_thumbnail_ai_generation(workspace, package).action == "recover"
    assert read_thumbnail_ai_state(workspace)["submitted"] is True


def test_operator_followup_is_consumed_once_and_stays_in_saved_thread(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    package = _package(workspace.output_dir / "package.txt")
    claim = claim_thumbnail_ai_generation(workspace, package)
    update_thumbnail_ai_state(workspace, package, claim, status="error", submitted=True, thread_id="saved-thread")
    receipt = tmp_path / "retired-receipt.json"
    receipt.write_text('{"submissionAccepted":true}', encoding="utf-8")
    state = read_thumbnail_ai_state(workspace)
    state.update(status="operator_retry_authorized", operator_followup={
        "authorization":"retry-once-in-same-conversation", "thread_id":"saved-thread",
        "retired_receipt_path":str(receipt),
    })
    (workspace.state_dir / "thumbnail-ai.json").write_text(json.dumps(state), encoding="utf-8")
    followup = claim_thumbnail_ai_generation(workspace, package)
    assert followup.action == "start_checked"
    # An image can arrive after authorization: the retry must collect it first.
    from types import SimpleNamespace
    from docurandom.ui import _read_completed_thumbnail_for_claim
    image = object()
    reader = SimpleNamespace(read_existing_generated_image=lambda **_kwargs: image)
    assert _read_completed_thumbnail_for_claim(reader, followup.action, lambda *_args: None, "exact prompt") is image
    saved = read_thumbnail_ai_state(workspace)
    assert saved["thread_id"] == "saved-thread"
    assert saved["automatic_retry_count"] == 3
    assert "operator_followup" not in saved
    assert claim_thumbnail_ai_generation(workspace, package).action != "start"
    assert json.loads(receipt.read_text())["submissionAccepted"] is True


def test_operator_followup_cannot_retire_another_thread(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    package = _package(workspace.output_dir / "package.txt")
    claim = claim_thumbnail_ai_generation(workspace, package)
    update_thumbnail_ai_state(workspace, package, claim, status="error", submitted=True, thread_id="saved-thread")
    state = read_thumbnail_ai_state(workspace)
    state.update(status="operator_retry_authorized", operator_followup={
        "authorization":"retry-once-in-same-conversation", "thread_id":"different-thread",
        "retired_receipt_path":str(tmp_path / "missing.json"),
    })
    (workspace.state_dir / "thumbnail-ai.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(Exception, match="Không xác minh"):
        claim_thumbnail_ai_generation(workspace, package)
    assert read_thumbnail_ai_state(workspace)["submitted"] is True


def test_confirmation_requires_exact_success_phrase() -> None:
    assert is_thumbnail_confirmation("ĐÃ TẠO")
    assert is_thumbnail_confirmation('“ĐÃ TẠO”')
    assert not is_thumbnail_confirmation("ĐÃ TẠO rồi nhé")
    assert not is_thumbnail_confirmation("KHÔNG TẠO ĐƯỢC: lỗi mạng")


def test_negative_confirmation_is_explicit_and_bounded() -> None:
    assert is_thumbnail_negative_confirmation("KHÔNG TẠO ĐƯỢC: lượt trước chưa hiển thị ảnh")
    assert is_thumbnail_negative_confirmation('“KHÔNG TẠO ĐƯỢC”')
    assert not is_thumbnail_negative_confirmation("ChatGPT chưa xác nhận")
    assert not is_thumbnail_negative_confirmation("ĐÃ TẠO")


def test_thumbnail_request_explicitly_requires_new_image_without_source() -> None:
    request = thumbnail_generation_request("factory documentary")
    assert "TẠO ẢNH MỚI TỪ ĐẦU" in request
    assert "không cần ảnh nguồn" in request
    assert "factory documentary" in request


def test_thumbnail_request_rewrites_editing_language_but_keeps_exact_text() -> None:
    prompt = (
        "Create exactly a 1280×720 true 16:9 YouTube thumbnail for a Global Japan Comparison documentary, "
        "using a hand-built real-photo Photoshop editorial composite with authentic industrial photography, "
        "believable factory lighting, natural imperfections, mild compression, used metal surfaces, oil stains, "
        "chips, scratched paint, realistic skin texture, and credible shadows. Unified scene only, no hard "
        "split-screen divider, no news collage, no browser UI, no phone UI, no glossy AI look, no CGI, no "
        "polished luxury poster. HYBRID composition: add exactly 日本製を「高すぎる」と排除."
    )
    request = thumbnail_generation_request(prompt)
    assert "Photoshop" not in request
    assert "editorial composite" not in request
    assert "TẠO ẢNH MỚI TỪ ĐẦU" in request
    assert "日本製を「高すぎる」と排除" in request


def test_thumbnail_checkpoint_keeps_video_chat_identity_across_stage_updates(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path / "workspace")
    package = _package(workspace.output_dir / "package.txt")
    claim = claim_thumbnail_ai_generation(workspace, package)

    update_thumbnail_ai_state(
        workspace,
        package,
        claim,
        status="submitted",
        detail="ảnh đã nhận",
        thread_id="video-thread",
        response_id="resp-image",
        submitted=True,
    )
    update_thumbnail_ai_state(
        workspace,
        package,
        claim,
        status="confirming",
        detail="đang xác nhận",
        submitted=True,
    )

    state = read_thumbnail_ai_state(workspace)
    assert state["thread_id"] == "video-thread"
    assert state["response_id"] == "resp-image"


def test_same_package_is_claimed_once_and_uncertain_state_blocks_retry(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    package = _package(workspace.output_dir / "package.txt")

    first = claim_thumbnail_ai_generation(workspace, package)
    second = claim_thumbnail_ai_generation(workspace, package)

    assert first.action == "start"
    assert second.action == "blocked"
    assert read_thumbnail_ai_state(workspace)["status"] == "running"


def test_completed_image_is_reused_without_second_generation(tmp_path: Path) -> None:
    from PIL import Image
    import io

    workspace = _workspace(tmp_path / "workspace")
    package = _package(workspace.output_dir / "package.txt")
    claim = claim_thumbnail_ai_generation(workspace, package)
    buffer = io.BytesIO()
    Image.new("RGB", (1280, 720), (20, 30, 40)).save(buffer, format="PNG")
    installed = install_workspace_thumbnail(workspace, buffer.getvalue())
    update_thumbnail_ai_state(
        workspace,
        package,
        claim,
        status="complete",
        detail="Đã tạo và xác nhận.",
        thumbnail_path=installed.path,
    )

    repeated = claim_thumbnail_ai_generation(workspace, package)

    assert repeated.action == "reuse"
    assert repeated.existing_path == installed.path


def test_changed_prompt_creates_a_new_one_shot_claim(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    package = _package(workspace.output_dir / "package.txt", "Prompt version one")
    assert claim_thumbnail_ai_generation(workspace, package).action == "start"


def terminal_failure(workspace, package):
    claim = claim_thumbnail_ai_generation(workspace, package)
    update_thumbnail_ai_state(
        workspace, package, claim, status="terminal_no_image", submitted=False,
        thread_id="old-thread",
        detail="CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: KHÔNG TẠO ĐƯỢC: công cụ tạo ảnh không khởi tạo được ảnh mới trong lượt này.",
    )
    path = workspace.state_dir / "thumbnail-ai.json"
    state = read_thumbnail_ai_state(workspace)
    state["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    path.write_text(json.dumps(state), encoding="utf-8")
    return claim, state


def test_legacy_retry_budget_survives_restarts_without_spending_fresh_chat_budget(tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    package = _package(workspace.output_dir / "package.txt")
    claim, _ = terminal_failure(workspace, package)
    assert claim_thumbnail_ai_generation(workspace, package).action == "recover_new_chat"
    assert reserve_thumbnail_ai_automatic_retry(
        workspace, package, claim, previous_thread_id="old-thread", new_thread_id="new-thread",
        terminal_no_image=True,
    )
    # Model another confirmed failure after a restart of the owning UI.
    update_thumbnail_ai_state(
        workspace, package, claim, status="terminal_no_image", submitted=False,
        detail="CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: công cụ tạo ảnh không khởi tạo được ảnh mới trong lượt này.",
    )
    state = read_thumbnail_ai_state(workspace)
    assert state["automatic_retry_count"] == 1
    assert thumbnail_ai_terminal_retry_remaining_seconds(state) is not None
    for count in (2, 3):
        state["updated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        (workspace.state_dir / "thumbnail-ai.json").write_text(json.dumps(state), encoding="utf-8")
        previous = str(state["thread_id"])
        assert reserve_thumbnail_ai_automatic_retry(
            workspace, package, claim, previous_thread_id=previous,
            new_thread_id=f"retry-{count}", terminal_no_image=True,
        )
        update_thumbnail_ai_state(workspace, package, claim, status="terminal_no_image", submitted=False,
                                  detail="CHATGPT_TERMINAL_RESPONSE_NO_IMAGE: lỗi tạo ảnh.")
        state = read_thumbnail_ai_state(workspace)
        assert state["automatic_retry_count"] == count
    assert thumbnail_ai_terminal_retry_remaining_seconds(state) is None
    assert claim_thumbnail_ai_generation(workspace, package).action == "blocked"


@pytest.mark.parametrize("change", ["policy", "quota", "image", "uncertain", "recent"])
def test_terminal_image_retry_rejects_policy_images_ambiguity_and_early_retry(tmp_path, change):
    workspace = _workspace(tmp_path / "workspace")
    package = _package(workspace.output_dir / "package.txt")
    claim, state = terminal_failure(workspace, package)
    if change in {"policy", "quota"}:
        state["detail"] += " " + change
    elif change == "image":
        state["thumbnail_path"] = "existing.png"
    elif change == "uncertain":
        state["submitted"] = True
    else:
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
    (workspace.state_dir / "thumbnail-ai.json").write_text(json.dumps(state), encoding="utf-8")
    assert not reserve_thumbnail_ai_automatic_retry(
        workspace, package, claim, previous_thread_id="old-thread", new_thread_id="new-thread",
        terminal_no_image=True,
    )
    assert read_thumbnail_ai_state(workspace)["automatic_retry_count"] == 0


def test_terminal_retry_rechecks_package_hash_at_reservation(tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    package = _package(workspace.output_dir / "package.txt")
    claim, _ = terminal_failure(workspace, package)
    _package(package, "a different request")
    assert not reserve_thumbnail_ai_automatic_retry(
        workspace, package, claim, previous_thread_id="old-thread", new_thread_id="new-thread",
        terminal_no_image=True,
    )


@pytest.mark.parametrize("change", [None, "busy", "image", "foreign", "login", "pending"])
def test_terminal_retry_requires_exact_idle_authenticated_image_free_thread(change):
    from docurandom.ui import _thumbnail_terminal_retry_idle_signature
    url = "https://chatgpt.com/c/exact-server-conversation"
    diagnostic = {
        "thread_id": "thread:old-thread", "saved_url": url, "current_url": url,
        "context_busy": False, "active_controller": False,
        "pending_submission": False, "pending_attachment": False,
        "snapshot": {"url": url, "users": [{"key": "u"}], "assistants": [{"key": "a"}],
                     "stopControlCount": 0, "streamingMarkerCount": 0},
        "page_snapshot": {"authenticated": True, "ready": True,
                          "errorMarkerCount": 0, "generatedImageCandidateCount": 0},
    }
    if change == "busy": diagnostic["context_busy"] = True
    if change == "image": diagnostic["page_snapshot"]["generatedImageCandidateCount"] = 1
    if change == "foreign": diagnostic["current_url"] += "-other"
    if change == "login": diagnostic["page_snapshot"]["authenticated"] = False
    if change == "pending": diagnostic["pending_submission"] = True
    assert (_thumbnail_terminal_retry_idle_signature(diagnostic, "old-thread") is not None) == (change is None)


def test_terminal_retry_accepts_real_bridge_1094_without_optional_page_counts():
    from docurandom.ui import _thumbnail_terminal_retry_idle_signature
    url = "https://chatgpt.com/c/exact-server-conversation"
    diagnostic = {
        "thread_id": "thread:old-thread", "saved_url": url, "current_url": url,
        "context_busy": False, "active_controller": False,
        "pending_submission": False, "pending_attachment": False,
        "snapshot": {"url": url, "users": [{"key": "u", "text": "Create one thumbnail"}],
                     "assistants": [{"key": "a", "text": "KHÔNG TẠO ĐƯỢC", "htmlLength": 100}],
                     "stopControlCount": 0, "streamingMarkerCount": 0},
        "page_snapshot": {"authenticated": True, "ready": True},
    }
    first = _thumbnail_terminal_retry_idle_signature(diagnostic, "old-thread")
    assert first is not None
    diagnostic["snapshot"]["assistants"][0]["htmlLength"] = 200
    assert _thumbnail_terminal_retry_idle_signature(diagnostic, "old-thread") == first
    diagnostic["snapshot"]["assistants"][0]["text"] = "New response arrived"
    assert _thumbnail_terminal_retry_idle_signature(diagnostic, "old-thread") != first


def test_error_before_submit_is_retryable_but_submitted_error_stays_blocked(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    package = _package(workspace.output_dir / "package.txt")
    first = claim_thumbnail_ai_generation(workspace, package)
    update_thumbnail_ai_state(
        workspace,
        package,
        first,
        status="error",
        detail="Bridge not ready",
        submitted=False,
    )
    second = claim_thumbnail_ai_generation(workspace, package)
    assert second.action == "start"
    update_thumbnail_ai_state(
        workspace,
        package,
        second,
        status="error",
        detail="response uncertain",
        submitted=True,
    )
    assert claim_thumbnail_ai_generation(workspace, package).action == "blocked"

    _package(package, "Prompt version two")

    assert claim_thumbnail_ai_generation(workspace, package).action == "start"
