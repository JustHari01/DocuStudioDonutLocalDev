"""Validate narration rather than instruction examples or placeholders."""
from __future__ import annotations

import math
import re
import unicodedata

MIN_NARRATION_CHARACTERS_PER_MINUTE = 150
MIN_NARRATION_DURATION_RATIO = 0.70
MAX_NARRATION_LINES = 2000

# Only explicit, contradictory location anchors are used. A generic photo,
# a reference to Japan, or a comparison mentioning both countries is allowed.
_COUNTRY_ANCHORS = (
    ('uzbekistan', ('ウズベキスタン', 'uzbekistan', 'uzbek')),
    ('costa-rica', ('コスタリカ', 'costa rica', 'costa rican')),
    ('portugal', ('ポルトガル', 'portugal', 'portuguese')),
    ('egypt', ('エジプト', 'egypt', 'egyptian')),
    ('hungary', ('ハンガリー', 'hungary', 'hungarian')),
    ('brazil', ('ブラジル', 'brazil', 'brazilian')),
    ('vietnam', ('ベトナム', 'vietnam', 'vietnamese')),
    ('indonesia', ('インドネシア', 'indonesia', 'indonesian')),
    ('mexico', ('メキシコ', 'mexico', 'mexican')),
    ('turkey', ('トルコ', 'turkey', 'turkish', 'türkiye')),
    ('poland', ('ポーランド', 'poland', 'polish ')),
    ('germany', ('ドイツ', 'germany', 'german ')),
    ('france', ('フランス', 'france', 'french ')),
    ('usa', ('アメリカ', '米国', 'united states', 'american ')),
)


def _countries(text: str) -> set[str]:
    value=unicodedata.normalize('NFKC',str(text or '')).casefold()
    return {key for key, aliases in _COUNTRY_ANCHORS if any(
        (alias in value if not alias.isascii() else re.search(r'(?<![a-z])'+re.escape(alias.strip())+r'(?![a-z])',value))
        for alias in aliases)}


def thumbnail_prompt_problem(prompt: str) -> str:
    value=str(prompt or '').strip()
    if not value:return 'thiếu prompt thumbnail'
    if re.search(r'(?im)^\s*(?:KHÔNG TẠO ĐƯỢC\s*:|CHATGPT_(?:RECOVERY|CONVERSATION|TERMINAL|CONTENT)\w*\s*:)',value):
        return 'prompt thumbnail là thông báo lỗi, chưa phải yêu cầu thiết kế ảnh'
    if any(marker in value.casefold() for marker in (
        'chưa thể khôi phục chính xác hai khối', 'chưa tìm thấy tiêu đề winner',
        'thông báo lỗi khôi phục dữ liệu', 'không phải prompt thiết kế thumbnail')):
        return 'prompt thumbnail chứa lời báo chưa khôi phục được nội dung'
    return ''


def artifact_field_problems(title: str, description: str, voice: str,
                            thumbnail: str, images: tuple[str,...] | list[str]) -> dict[str,str]:
    problems={}
    if thumbnail and (problem:=thumbnail_prompt_problem(thumbnail)):problems['thumbnail']=problem
    reaction=re.search(r'【([^】]+?)の反応】',title)
    expected=_countries(reaction.group(1) if reaction else title)
    if len(expected)!=1:return problems
    # Require the selected location to be corroborated by the actual script.
    if not (expected & (_countries(voice)|_countries(description))):return problems
    wrong=lambda text:bool(_countries(text)-expected) and not bool(_countries(text)&expected)
    if thumbnail and wrong(thumbnail):problems['thumbnail']='thumbnail khác quốc gia/bối cảnh của tiêu đề và lời thoại'
    if description and wrong(description):problems['description']='mô tả khác bối cảnh của tiêu đề và lời thoại'
    if voice and wrong(voice):problems['voice']='lời thoại khác quốc gia/bối cảnh của tiêu đề và mô tả'
    if len(images)>=3 and not any(expected&_countries(p)for p in images) and sum(wrong(p)for p in images)>=max(3,len(images)*0.6):
        problems['images']='bộ prompt ảnh thuộc quốc gia khác với tiêu đề và lời thoại'
    return problems


def minimum_narration_characters(minutes: float) -> int:
    return max(0, math.ceil(float(minutes) * MIN_NARRATION_CHARACTERS_PER_MINUTE))


def narration_problem(script: str, minimum_characters: int = 0) -> str:
    value = unicodedata.normalize("NFKC", str(script or "")).strip()
    if not value:
        return "thiếu lời thoại"
    if re.search(
        r"\[\s*(?:nội dung kịch bản|thoại VOICEVOX|script content|insert (?:script|narration)|"
        r"narration (?:here|part)|your script|lời thoại ở đây)[^\]\n]*\]"
        r"|\[\[(?:end-)?(?:part-\d+|continue|done)\]\]",
        value, re.IGNORECASE,
    ):
        return "lời thoại chứa dòng mẫu/marker chưa được thay bằng nội dung thật"
    if len([line for line in value.splitlines() if line.strip()]) > MAX_NARRATION_LINES:
        return f"lời thoại vượt {MAX_NARRATION_LINES:,} dòng; cần tổng hợp lại đúng một kịch bản"
    characters = len(re.sub(r"\s+", "", value))
    if minimum_characters and characters < minimum_characters:
        return f"lời thoại quá ngắn ({characters}/{minimum_characters} ký tự tối thiểu)"
    return ""


def narration_duration_problem(seconds: float, minutes: float) -> str:
    requested = float(minutes) * 60
    actual = float(seconds)
    if requested <= 0:
        return ""
    if not math.isfinite(actual) or actual < requested * MIN_NARRATION_DURATION_RATIO:
        return (f"NARRATION_CONTENT_INCOMPLETE: Giọng đọc chỉ dài {actual:.1f} giây "
                f"cho yêu cầu {float(minutes):g} phút; phải bổ sung đúng kịch bản "
                "trước render/upload/Public, không kéo dài audio hoặc dùng giọng cũ.")
    return ""
