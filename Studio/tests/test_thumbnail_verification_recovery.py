import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from docurandom import thumbnail_contract as c
from docurandom.quality_chat import review_retry_delay


def fixture(monkeypatch, tmp_path):
    workspace = SimpleNamespace(state_dir=tmp_path)
    monkeypatch.setattr(c, 'contract', lambda _: {'line_colors': ['yellow', 'cyan', 'red']})
    return workspace


def test_local_failure_backoff_survives_reentry_and_restart(monkeypatch, tmp_path):
    w = fixture(monkeypatch, tmp_path)
    expensive = Mock(side_effect=MemoryError('OCR allocation failed'))
    monkeypatch.setattr(c, '_review_image', expensive)
    with pytest.raises(ValueError, match='MemoryError.*quality review wait') as exc:
        c.review_image(w, 'exact prompt', b'candidate')
    assert 110 <= review_retry_delay(str(exc.value)) <= 120
    for _ in range(5):
        with pytest.raises(ValueError, match='quality review wait'):
            c.review_image(SimpleNamespace(state_dir=tmp_path), 'exact prompt', b'candidate')
    assert expensive.call_count == 1
    row = json.loads(next((tmp_path/'thumbnail-contract/verification-recovery').glob('*.json')).read_text())
    assert row['failures'] == 1


def test_local_failure_budget_stops_without_resetting_or_sending(monkeypatch, tmp_path):
    w = fixture(monkeypatch, tmp_path)
    expensive = Mock(side_effect=ValueError('invalid local evidence'))
    monkeypatch.setattr(c, '_review_image', expensive)
    for attempt in range(4):
        with pytest.raises(ValueError, match='invalid local evidence'):
            c.review_image(w, 'exact prompt', b'candidate')
        path = next((tmp_path/'thumbnail-contract/verification-recovery').glob('*.json'))
        row = json.loads(path.read_text())
        assert row['failures'] == attempt + 1
        row['next_probe_at'] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match='quality review exhausted') as exc:
        c.review_image(w, 'exact prompt', b'candidate')
    assert review_retry_delay(str(exc.value)) == -1
    assert expensive.call_count == 4
    # A different video/candidate has its own budget, never the old thread.
    with pytest.raises(ValueError, match='invalid local evidence'):
        c.review_image(w, 'new prompt', b'new candidate')
    assert expensive.call_count == 5


def test_remote_read_budget_is_not_replaced_by_a_local_budget(monkeypatch, tmp_path):
    w = fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(c, '_review_image', Mock(side_effect=RuntimeError(
        'quality review exhausted; next-probe-at=2026-10-27T00:00:00+00:00; preserve receipt')))
    with pytest.raises(RuntimeError, match='exhausted'):
        c.review_image(w, 'prompt', b'candidate')
    assert not list(tmp_path.rglob('*-recovery/*.json'))


def test_cached_ocr_never_skips_remote_review_or_accepts_a_different_prompt(monkeypatch, tmp_path):
    from docurandom import thumbnail_ocr, quality_chat
    w = fixture(monkeypatch, tmp_path)
    (tmp_path/'thumbnail-ai.json').write_text('{}')
    prompt = 'first line 「日本」 in yellow\nsecond line 「工場」 in cyan\nthird line 「検査」 in red'
    buf = io.BytesIO(); Image.new('RGB', (1280, 720)).save(buf, format='PNG')
    ocr = Mock(return_value={'passed': True})
    monkeypatch.setattr(thumbnail_ocr, 'inspect', ocr)
    review = {'passed': True, 'observed_headlines': ['日本', '工場', '検査'],
              'observed_colors': ['yellow', 'cyan', 'red'], 'flags_correct': True,
              'subject_correct': True, 'text_uncropped': True}
    remote = Mock(return_value=review)
    monkeypatch.setattr(quality_chat, 'ask_json', remote)
    for _ in range(3):
        assert c.review_image(w, prompt, buf.getvalue())
    assert ocr.call_count == 1 and remote.call_count == 3
    review['observed_headlines'][-1] = '試験'
    assert c.review_image(w, prompt.replace('検査', '試験'), buf.getvalue())
    assert ocr.call_count == 2 and remote.call_count == 4


def test_approval_is_scoped_to_one_candidate_and_keeps_old_receipt(monkeypatch, tmp_path):
    w = fixture(monkeypatch, tmp_path)
    old = tmp_path/'thumbnail-contract/reviews/old.json'; old.parent.mkdir(parents=True)
    old.write_text('{"status":"uncertain"}')
    before = old.read_bytes()
    request = 'old exact request'
    thread = 'thread_docurandom_'+'a'*32
    approval = {'max_requests': 1, 'image_generation_permitted': False,
                'image_sha256': 'a'*64, 'prompt_sha256': hashlib.sha256(request.encode()).hexdigest(),
                'new_thread_id': thread, 'old_receipt': str(old),
                'old_receipt_sha256': hashlib.sha256(before).hexdigest()}
    path = tmp_path/'thumbnail-contract/review-recovery.json'; path.write_text(json.dumps(approval))
    saved = path.read_bytes()
    assert c.approved_review_thread(tmp_path, request, 'a'*64) == thread
    assert c.approved_review_thread(tmp_path, 'new video request', 'b'*64) is None
    assert old.read_bytes() == before and path.read_bytes() == saved
    old.write_text('tampered')
    with pytest.raises(ValueError, match='no longer matches'):
        c.approved_review_thread(tmp_path, request, 'a'*64)
