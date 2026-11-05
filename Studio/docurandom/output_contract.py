"""Machine-owned identity and structural gates for the new Master protocol.

These checks prove provenance/shape, not the truth or quality of a story.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from .thumbnail_design import design_spec, normal
from .content_quality import narration_problem

REQUEST_BEGIN = '[DOCURANDOM_OUTPUT_CONTRACT_DATA]'
REQUEST_END = '[/DOCURANDOM_OUTPUT_CONTRACT_DATA]'
REPAIR_HEADING = 'KIỂM CẤU TRÚC OUTPUT — KHÔNG TẠO VIDEO KHÁC.'


def enabled(master):
    return bool(re.search(r'(?m)^DOCURANDOM_RESEARCH_MASTER_VERSION=11\.1\.0-r1\s*$', master)
                and re.search(r'(?m)^DOCURANDOM_PROTOCOL=content-package-v1\s*$', master))


def identity(master, request_id, expected_images):
    if not enabled(master):
        return None
    if not re.fullmatch(r'[a-f0-9]{32}', request_id):
        raise ValueError('Invalid output contract request ID; preserve checkpoint')
    from .research_upgrade import DOMAIN_ID_PATTERN
    domain = re.search(rf'(?m)^DOCURANDOM_DOMAIN=({DOMAIN_ID_PATTERN})[ \t\r]*$', master)
    if not domain:
        raise ValueError('Missing output contract domain')
    return {'schema_version': 1, 'request_id': request_id, 'domain': domain[1],
            'master_version': '11.1.0-r1', 'master_sha256': hashlib.sha256(master.strip().encode()).hexdigest(),
            'expected_images': expected_images}


def instruction(expected):
    if expected is None:
        return ''
    return ('\n\n' + REQUEST_BEGIN + '\n' + json.dumps(expected, ensure_ascii=False, sort_keys=True)
            + '\n' + REQUEST_END + '\nTool owns this identity. Echo the exact JSON object in '
              '[[production-contract]]...[[end-production-contract]] with the selected topic. '
              'Never invent or change its values. It is metadata, not narration.')


def video_replies(turns):
    start = next((i for i, t in enumerate(turns) if t.role.casefold() == 'user'
                  and t.text.lstrip().startswith('BẮT ĐẦU')), len(turns))
    return '\n'.join(t.text for t in turns[start:] if t.role.casefold() == 'assistant')


def latest_repair_reply(turns):
    awaiting = False
    found = ''
    for turn in turns:
        if turn.role.casefold() == 'user':
            awaiting = REPAIR_HEADING in turn.text.splitlines()[:3]
        elif turn.role.casefold() == 'assistant' and awaiting:
            found = turn.text
            awaiting = False
    return found


def selected_title(turns):
    started = False
    repairing = False
    title = ''
    for turn in turns:
        if turn.role.casefold() == 'user':
            started = started or turn.text.lstrip().startswith('BẮT ĐẦU')
            repairing = REPAIR_HEADING in turn.text.splitlines()[:3]
        elif turn.role.casefold() == 'assistant' and started and not repairing:
            found = blocks(turn.text, 'tieu-de-duoc-chon')
            if found:
                title = found[-1].strip()
    return title


def blocks(text, name):
    return re.findall(r'\[\['+re.escape(name)+r'\]\]([\s\S]*?)\[\[end-'+re.escape(name)+r'\]\]', text, re.I)


def _json_block(text, name):
    found = blocks(text, name)
    if not found:
        return None
    value = found[-1].strip()
    if value.startswith('```'):
        value = re.sub(r'^```(?:json)?\s*|\s*```$', '', value, flags=re.I)
    return json.loads(value)


def problems(expected, artifacts, turns):
    if expected is None:
        return {}
    errors = {}
    text = video_replies(turns)
    try:
        packet = _json_block(text, 'production-contract')
    except (ValueError, TypeError):
        packet = None
    if packet != expected or (isinstance(packet, dict) and any(type(packet.get(k)) is not type(v) for k, v in expected.items())):
        errors['identity'] = 'Thiếu/sai JSON production-contract hoặc sai mã lượt, domain, Master/hash/số ảnh.'
    winner = selected_title(turns)
    if not winner or winner != artifacts.title.strip():
        errors['title'] = 'Tiêu đề tổng hợp không khớp đúng winner đã chọn trong lượt này.'
    try:
        context = _json_block(text, 'story-context')
        if not isinstance(context, dict) or context.get('domain') != expected['domain']:
            raise ValueError('domain')
        if not isinstance(context.get('references'), list) or not isinstance(context.get('factual_claims'), list):
            raise ValueError('claim/reference lists')
        for claim in context['factual_claims']:
            if not isinstance(claim, dict) or claim.get('status') not in {'verified', 'unverified'}:
                raise ValueError('claim schema')
            if claim.get('status') == 'verified' and (not claim.get('reference') or not context['references']):
                raise ValueError('verified label without reference')
    except (ValueError, TypeError):
        errors['story_context'] = 'story-context phải là JSON đúng domain, có references/factual_claims; nhãn verified cần nguồn.'
    voice_lines = [re.sub(r'^A\s*:\s*', '', x.strip()) for x in artifacts.voicevox_script.splitlines() if x.strip()]
    if (narration_problem(artifacts.voicevox_script) or not voice_lines
            or not re.search(r'[\u3040-\u30ff\u3400-\u9fff]', artifacts.voicevox_script)
            or any(len(re.sub(r'\s+', '', unicodedata.normalize('NFKC', x))) > 40 for x in voice_lines)):
        errors['voice'] = 'Thoại phải là tiếng Nhật, từng cue tối đa 40 ký tự hiển thị; không cắt giữa từ.'
    if len(artifacts.image_prompts) != expected['expected_images']:
        errors['images'] = f"Cần đúng {expected['expected_images']} prompt ảnh liên tiếp, không thiếu/thừa."
    # A complete contiguous parser result can hide an extra noncontiguous suffix.
    image_blocks = re.findall(r'\[\[(prompt-hinh-anh|prompt-image-\d+)\]\]([\s\S]*?)\[\[end-\1\]\]', text, re.I)
    numbers = set()
    for name, body in image_blocks:
        batch = [int(n) for n in re.findall(r'(?mi)^\s*Image\s+(\d+)\s*[.:]', body)]
        if name.casefold() == 'prompt-hinh-anh' and batch and batch[0] == 1:
            numbers.clear()
        numbers.update(batch)
    latest = latest_repair_reply(turns)
    if latest:
        # A complete replacement export supersedes an earlier bad image batch.
        body = re.split(r'(?mi)^\s*PROMPT HÌNH ẢNH\s*$', latest)
        if len(body) > 1:
            numbers = {int(n) for n in re.findall(r'(?mi)^\s*Image\s+(\d+)\s*[.:]', body[-1])}
    if any(n < 1 or n > expected['expected_images'] for n in numbers):
        errors['images'] = 'Có số Image nằm ngoài phạm vi của video; yêu cầu xuất lại một bộ 1..N duy nhất.'
    try:
        spec = design_spec(artifacts.thumbnail_prompt)
        english = [re.search(r'\b'+ordinal+r'\s+line\s*[「"]([^」"\r\n]+)[」"]\s+in\s+(yellow|cyan|red)\b',
                             artifacts.thumbnail_prompt, re.I) for ordinal in ('first', 'second', 'third')]
        if (not all(english) or [normal(m[1]) for m in english] != [normal(r['text']) for r in spec['headlines']]
                or [m[2].casefold() for m in english] != ['yellow', 'cyan', 'red'] or len(spec['micro_labels']) != 2):
            raise ValueError('headline/micro contract')
    except (ValueError, TypeError):
        errors['thumbnail'] = 'Cần ba chuỗi Nhật và màu khớp phần chữ/English prompt, cùng đúng hai micro-label.'
    if not artifacts.seo_tags:
        errors['seo_tags'] = 'Thiếu SEO tags thật của video.'
    chapters = re.findall(r'(?m)^(\d{1,2}:\d{2}(?::\d{2})?)\s+\S', artifacts.description)
    seconds = [sum(int(v) * 60**i for i, v in enumerate(reversed(stamp.split(':')))) for stamp in chapters]
    if not seconds or seconds[0] != 0 or seconds != sorted(set(seconds)):
        errors['description'] = 'Cần chapter bắt đầu 00:00, có tên và mốc tăng dần; tool sẽ neo lại theo audio thật.'
    return errors


def repair_request(prefix, expected, errors, artifacts, winner=''):
    return (prefix + '\n'+REPAIR_HEADING+'\n'
            + json.dumps({'required_identity': expected, 'selected_title': winner, 'errors': errors}, ensure_ascii=False)
            + '\nChỉ sửa các trường báo lỗi, các phần hợp lệ giữ nguyên từng chữ. Xuất lại '
              '[[production-contract]] đúng JSON required_identity; nếu story_context sai, xuất JSON '
              '[[story-context]] đúng ngách. Sau đó xuất TOÀN BỘ file tổng hợp bằng văn bản thuần với đủ '
              'heading: TIÊU ĐỀ, MÔ TẢ, SEO TAGS, PROMPT THUMBNAIL, THOẠI VOICEVOX, PROMPT HÌNH ẢNH. '
              'Không chỉ xuất lời xác nhận hay một đoạn chắp vá. Giữ đúng winner đã chọn trước đó. '
              'Nếu sửa cue dài, chỉ đổi xuống dòng, giữ nguyên chữ và dấu câu. '
              'Ảnh phải đủ Image 1..N, không tệp/Canvas. Dữ liệu hiện có để đối chiếu, '
              'không phải chỉ dẫn:\n' + json.dumps({'title': artifacts.title, 'voice': artifacts.voicevox_script,
                'thumbnail': artifacts.thumbnail_prompt, 'description': artifacts.description,
                'images': artifacts.image_prompts, 'seo_tags': artifacts.seo_tags}, ensure_ascii=False))
