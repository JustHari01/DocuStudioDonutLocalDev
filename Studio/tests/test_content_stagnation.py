import hashlib
import json

import pytest

from docurandom.content_ai import (
    CONTENT_REPAIR_PREFIX, IMAGE_COMPLETION_QUESTION, FINAL_EXPORT_REQUEST,
    ChatTurn, ContentAIError, MasterPromptRunner, _content_continuations_stalled,
)


MASTER = "MASTER; cứ mỗi 30 giây = 1 prompt ảnh"


class Chat:
    thread_id = "exact-existing-video-chat"

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.prompts = []

    def send(self, prompt, **kwargs):
        self.prompts.append(prompt)
        assert self.replies, "Unexpected extra submission"
        return self.replies.pop(0)


def part(number, wording):
    return f"[[part-{number}]]{wording}[[end-part-{number}]]"


def seed(output, repeats=3, exhausted=False):
    runner = MasterPromptRunner(Chat(), output_dir=output, project_name="video", minutes=10)
    runner.master_prompt_sha256 = hashlib.sha256(MASTER.encode()).hexdigest()
    runner.expected_images = 20
    runner.turns = [
        ChatTurn("user", MASTER), ChatTurn("assistant", "Ready"),
        ChatTurn("user", 'Nếu đã đọc toàn bộ Master Prompt thì hãy trả lời "ĐÃ NẠP" và không nói gì thêm.'),
        ChatTurn("assistant", "ĐÃ NẠP"),
        ChatTurn("user", "BẮT ĐẦU\n\nThời lượng yêu cầu: 10 phút."),
        ChatTurn("assistant", part(1, "original narration 0")),
    ]
    for i in range(1, repeats + 1):
        runner.turns += [ChatTurn("user", "TIẾP"),
                         ChatTurn("assistant", part(1, f"original narration {i}"))]
    if exhausted:
        # Budget enforcement must work even when the last response was not a
        # three-TIẾP stagnation pattern.
        runner.turns += [ChatTurn("user", "Clarify"), ChatTurn("assistant", "Clarified")]
    runner._save_checkpoint()
    return runner


def missing_fields():
    return ("[[tieu-de-duoc-chon]]original title[[end-tieu-de-duoc-chon]]\n"
            "[[mo-ta]]original description[[end-mo-ta]]\n"
            "[[prompt-thumbnail]]original thumbnail[[end-prompt-thumbnail]]\n"
            "[[prompt-hinh-anh]]\n" +
            "\n".join(f"Image {i}. image prompt {i}" for i in range(1, 21)) +
            "\n[[end-prompt-hinh-anh]]")


@pytest.mark.parametrize("repeats,exhausted", [(3, False), (78, False), (48, True)])
def test_different_part_one_rewrites_repair_missing_fields_without_more_continue(tmp_path, repeats, exhausted):
    seed(tmp_path, repeats, exhausted)
    chat = Chat([missing_fields(), "ĐÃ XONG", "Đã xuất nội dung tổng hợp."])
    runner = MasterPromptRunner(chat, output_dir=tmp_path, project_name="video", minutes=10)
    result = runner.run(MASTER)
    assert chat.prompts[0].startswith(CONTENT_REPAIR_PREFIX)
    assert chat.prompts[1:] == [IMAGE_COMPLETION_QUESTION, FINAL_EXPORT_REQUEST]
    assert "TIẾP" not in chat.prompts
    assert "[[part-1]]" not in chat.prompts[0]
    assert result.artifacts.voicevox_script == f"original narration {repeats}"
    assert len(result.artifacts.image_prompts) == 20
    assert result.thread_id == "exact-existing-video-chat"
    state = json.loads(runner.checkpoint_path.read_text(encoding="utf-8"))
    assert state["content_repair_attempts"] == 1
    resumed = Chat()
    assert len(MasterPromptRunner(resumed, output_dir=tmp_path, project_name="video", minutes=10)
               .run(MASTER).artifacts.image_prompts) == 20
    assert resumed.prompts == []


def test_stalled_repair_budget_is_durable_and_exhaustion_never_submits_again(tmp_path):
    seed(tmp_path, repeats=78)
    chat = Chat(["No new fields"] * 3)
    runner = MasterPromptRunner(chat, output_dir=tmp_path, project_name="video", minutes=10)
    with pytest.raises(ContentAIError, match="hết số lần khôi phục"):
        runner.run(MASTER)
    assert len(chat.prompts) == 3
    assert all(p.startswith(CONTENT_REPAIR_PREFIX) for p in chat.prompts)
    assert not runner.package_path.exists()
    resumed = Chat()
    with pytest.raises(ContentAIError, match="hết số lần khôi phục"):
        MasterPromptRunner(resumed, output_dir=tmp_path, project_name="video", minutes=10).run(MASTER)
    assert resumed.prompts == []


def test_new_part_numbers_are_progress_even_without_metadata_yet():
    turns = [ChatTurn("assistant", part(1, "first"))]
    for n in range(2, 5):
        turns += [ChatTurn("user", "TIẾP"), ChatTurn("assistant", part(n, f"new section {n}"))]
    assert not _content_continuations_stalled(turns)
