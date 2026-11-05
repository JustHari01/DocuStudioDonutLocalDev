"""Resolve ASR spelling/short-context ambiguity without changing speech."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import wave

from .storage import atomic_write_json
from .voicevox_project import project_boxes, voicevox_project_digest
from .voicevox_qa import _normalized_japanese, _load_model, _TRANSCRIBE_LOCK

G13_GLOSSARY = '工作機械、深穴加工、穴径、真直度、刃先、摩耗、加工時間、工具、送り速度。'


def moras(query):
    phrases = query.get('accentPhrases', query.get('accent_phrases', []))
    return ''.join(str(m['text']) for p in phrases for m in p['moras'])


def contextual_audio(parts, indexes, target):
    chunks, hashes, params = [], [], None
    for index in indexes:
        path = parts / f'{index:04d}.wav'
        hashes.append({'box_index': index, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        with wave.open(str(path), 'rb') as source:
            current = source.getparams()
            format_key = (current.nchannels, current.sampwidth, current.framerate, current.comptype)
            if params and format_key != (params.nchannels, params.sampwidth, params.framerate, params.comptype):
                raise ValueError('Neighbouring audio formats differ')
            params = current
            chunks.append(source.readframes(source.getnframes()))
    with wave.open(str(target), 'wb') as output:
        output.setparams(params)
        output.writeframes(b''.join(chunks))
    return hashes


def adjudicate(project, result, qa, client, *, cancel, cpu_threads):
    if not qa.warning_indexes or len(qa.warning_indexes) > 20:
        return qa
    meta = json.loads(result.metadata_path.read_text(encoding='utf-8-sig'))
    raw = json.loads(qa.report_path.read_text(encoding='utf-8-sig'))
    boxes = project_boxes(project)
    root = result.metadata_path.parent / '.docurandom/asr-context' / voicevox_project_digest(project)
    root.mkdir(parents=True, exist_ok=True)
    rows = {r['box_index']: r for r in raw['lines']}
    resolved, evidence = set(), []
    for index in qa.warning_indexes:
        if cancel.is_set():
            raise InterruptedError('ASR adjudication cancelled')
        row, box = rows[index], boxes[index - 1]
        if row['expected'] != box.text:
            raise ValueError('ASR project text identity differs')
        heard = client.audio_query(row['recognized'], box.style_id) if row['recognized'].strip() else {}
        expected_moras, heard_moras = moras(box.query), moras(heard)
        item = {'box_index': index, 'expected': box.text, 'raw_recognized': row['recognized'],
                'expected_moras': expected_moras, 'recognized_moras': heard_moras, 'resolved': False}
        if expected_moras and expected_moras == heard_moras:
            item.update(resolved=True, method='same-phonemes-different-spelling')
        elif len(_normalized_japanese(box.text)) <= 12:
            indexes = list(range(max(1, index - 1), min(len(boxes), index + 1) + 1))
            audio = root / f'context-{index:04d}.wav'
            item['source_audio'] = contextual_audio(Path(meta['parts_checkpoint']).parent, indexes, audio)
            expected = ''.join(boxes[i - 1].text for i in indexes)
            with _TRANSCRIBE_LOCK:
                model = _load_model(cpu_threads, 'large-v3')
                segments, _ = model.transcribe(str(audio), language='ja', beam_size=3, best_of=1,
                    condition_on_previous_text=False, vad_filter=False, initial_prompt=G13_GLOSSARY)
                recognized = ''.join(s.text.strip() for s in segments)
            item.update(method='original-neighbour-audio-with-domain-glossary', context_expected=expected,
                        context_recognized=recognized, glossary=G13_GLOSSARY,
                        context_audio_sha256=hashlib.sha256(audio.read_bytes()).hexdigest(),
                        resolved=bool(_normalized_japanese(expected)) and
                                 _normalized_japanese(expected) == _normalized_japanese(recognized))
        if item['resolved']:
            resolved.add(index)
        evidence.append(item)
    remaining = tuple(i for i in qa.warning_indexes if i not in resolved)
    report = qa.report_path.with_name(qa.report_path.stem + '-adjudicated.json')
    atomic_write_json(report, {**raw, 'status': 'warning' if remaining else 'passed',
        'warning_indexes': list(remaining), 'original_warning_indexes': list(qa.warning_indexes),
        'raw_report': str(qa.report_path), 'raw_report_sha256': hashlib.sha256(qa.report_path.read_bytes()).hexdigest(),
        'adjudications': evidence, 'audio_changed': False,
        'limitations': 'Resolves ASR ambiguity only; not a native-speaker accent certification.'})
    return replace(qa, report_path=report, status='warning' if remaining else 'passed', warning_indexes=remaining)
