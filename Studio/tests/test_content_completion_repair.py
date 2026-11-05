import hashlib
import json
from pathlib import Path

import pytest

from docurandom.content_ai import (
    CONTENT_REPAIR_PREFIX,
    FINAL_EXPORT_REQUEST,
    IMAGE_COMPLETION_QUESTION,
    ChatTurn,
    ContentAIError,
    MasterPromptRunner,
    format_content_package,
    extract_content_artifacts,
    merge_exported_artifacts,
)


MASTER = "MASTER; cứ mỗi 30 giây = 1 prompt ảnh"


class RepairChat:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.prompts = []
        self.thread_id = "same-saved-video-conversation"
        self.recovered = None
        self.recovery_calls = 0

    def send(self, prompt, **kwargs):
        self.prompts.append(prompt)
        assert self.responses, "unexpected extra prompt"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def recover_pending(self, **kwargs):
        self.recovery_calls += 1
        assert self.recovered is not None
        return self.recovered


def content(missing="images", images=20):
    fields = {
        "title": "[[tieu-de-duoc-chon]]original title[[end-tieu-de-duoc-chon]]",
        "description": "[[mo-ta]]original description[[end-mo-ta]]",
        "thumbnail": "[[prompt-thumbnail]]original thumbnail[[end-prompt-thumbnail]]",
        "voice": "[[part-1]]first sentence\nsecond sentence[[end-part-1]]",
        "images": "[[prompt-hinh-anh]]\n"
        + "\n".join(f"Image {i}. original image {i}" for i in range(1, images + 1))
        + "\n[[end-prompt-hinh-anh]]",
    }
    if missing != "images":
        fields.pop(missing, None)
    return "\n".join(fields.values())


def seed_checkpoint(output, missing="images", exported=True):
    runner = MasterPromptRunner(
        RepairChat(), output_dir=output, project_name="video", minutes=10
    )
    runner.master_prompt_sha256 = hashlib.sha256(MASTER.encode()).hexdigest()
    runner.expected_images = 20
    partial = content(missing, images=10 if missing == "images" else 20)
    runner.turns = [
        ChatTurn("user", MASTER),
        ChatTurn("assistant", "Đã nhận."),
        ChatTurn("user", 'Nếu đã đọc toàn bộ Master Prompt thì hãy trả lời "ĐÃ NẠP" và không nói gì thêm.'),
        ChatTurn("assistant", "ĐÃ NẠP"),
        ChatTurn("user", "BẮT ĐẦU\n\nThời lượng yêu cầu: 10 phút."),
        ChatTurn("assistant", partial),
        ChatTurn("user", IMAGE_COMPLETION_QUESTION),
        ChatTurn("assistant", "ĐÃ XONG"),
    ]
    package = format_content_package(extract_content_artifacts(runner.turns))
    if exported:
        runner.turns += [ChatTurn("user", FINAL_EXPORT_REQUEST), ChatTurn("assistant", package)]
    runner._save_checkpoint()
    return runner, package


@pytest.mark.parametrize("missing", ["title", "description", "thumbnail", "voice", "images"])
def test_old_done_export_requests_missing_fields_and_preserves_existing_content(tmp_path, missing):
    seed, _ = seed_checkpoint(tmp_path, missing)
    # An overly broad repair must not replace valid narration or metadata.
    reply = content("none").replace("original title", "replacement title").replace(
        "first sentence\nsecond sentence", "replacement voice"
    )
    chat = RepairChat([reply])
    runner = MasterPromptRunner(chat, output_dir=tmp_path, project_name="video", minutes=10)
    result = runner.run(MASTER)
    assert len(chat.prompts) == 1
    assert chat.prompts[0].startswith(CONTENT_REPAIR_PREFIX)
    assert chat.thread_id == seed.chat.thread_id
    assert result.artifacts.title == ("replacement title" if missing == "title" else "original title")
    assert result.artifacts.voicevox_script == (
        "replacement voice" if missing == "voice" else "first sentence\nsecond sentence"
    )
    assert len(result.artifacts.image_prompts) == 20
    if missing == "images":
        assert "[[part-1]]" not in chat.prompts[0]
        assert "[[tieu-de-duoc-chon]]" not in chat.prompts[0]
        assert "10/20" in chat.prompts[0]
    state = json.loads(runner.checkpoint_path.read_text(encoding="utf-8"))
    assert state["content_repair_attempts"] == 1
    # Reloading a repaired old export cannot lose the repaired fields or retry.
    resumed_chat = RepairChat()
    resumed = MasterPromptRunner(resumed_chat, output_dir=tmp_path, project_name="video", minutes=10)
    assert len(resumed.run(MASTER).artifacts.image_prompts) == 20
    assert resumed_chat.prompts == []


def test_first_final_export_missing_content_is_repaired_without_continue(tmp_path):
    _, partial_export = seed_checkpoint(tmp_path, exported=False)
    chat = RepairChat([partial_export, content("none")])
    runner = MasterPromptRunner(chat, output_dir=tmp_path, project_name="video", minutes=10)
    assert len(runner.run(MASTER).artifacts.image_prompts) == 20
    assert chat.prompts[0] == FINAL_EXPORT_REQUEST
    assert chat.prompts[1].startswith(CONTENT_REPAIR_PREFIX)
    assert "TIẾP" not in chat.prompts


def test_done_repair_limit_survives_restart_without_more_requests(tmp_path):
    seed_checkpoint(tmp_path)
    chat = RepairChat(["ĐÃ XONG"] * 3)
    runner = MasterPromptRunner(chat, output_dir=tmp_path, project_name="video", minutes=10)
    with pytest.raises(ContentAIError, match="hết số lần khôi phục"):
        runner.run(MASTER)
    assert len(chat.prompts) == 3
    assert all(p.startswith(CONTENT_REPAIR_PREFIX) for p in chat.prompts)
    assert not runner.package_path.exists()
    assert json.loads(runner.checkpoint_path.read_text(encoding="utf-8"))["content_repair_attempts"] == 3
    resumed_chat = RepairChat()
    resumed = MasterPromptRunner(resumed_chat, output_dir=tmp_path, project_name="video", minutes=10)
    with pytest.raises(ContentAIError, match="hết số lần khôi phục"):
        resumed.run(MASTER)
    assert resumed_chat.prompts == []


def test_interrupted_repair_recovers_exact_pending_response_without_resending(tmp_path):
    seed_checkpoint(tmp_path)
    chat = RepairChat([ContentAIError("connection closed after submission")])
    runner = MasterPromptRunner(chat, output_dir=tmp_path, project_name="video", minutes=10)
    with pytest.raises(ContentAIError, match="connection closed"):
        runner.run(MASTER)
    state = json.loads(runner.checkpoint_path.read_text(encoding="utf-8"))
    assert state["content_repair_attempts"] == 1
    assert state["turns"][-1]["role"] == "user"
    resumed_chat = RepairChat()
    resumed_chat.recovered = content("none")
    resumed = MasterPromptRunner(resumed_chat, output_dir=tmp_path, project_name="video", minutes=10)
    assert len(resumed.run(MASTER).artifacts.image_prompts) == 20
    assert resumed_chat.recovery_calls == 1
    assert resumed_chat.prompts == []
    assert resumed.content_repair_attempts == 1


def test_shorter_old_export_cannot_erase_recovered_image_prompts():
    complete = extract_content_artifacts([ChatTurn("assistant", content("none"))])
    shorter = format_content_package(extract_content_artifacts([
        ChatTurn("assistant", content("none", images=17))
    ]))
    assert len(merge_exported_artifacts(
        shorter, complete, 10, expected_images=20, allowed_missing_images=3
    ).image_prompts) == 20
