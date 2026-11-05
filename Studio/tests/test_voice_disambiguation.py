import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from threading import Event
from unittest.mock import Mock, patch
import wave

import pytest

from docurandom.voice_disambiguation import adjudicate, contextual_audio, moras
from docurandom.voicevox_qa import VoicevoxQaResult


def test_moras_accept_engine_and_project_casing():
    assert moras({'accentPhrases':[{'moras':[{'text':'ア'}]}]}) == 'ア'
    assert moras({'accent_phrases':[{'moras':[{'text':'ア'}]}]}) == 'ア'
    assert moras({}) == ''


@pytest.mark.parametrize('heard,context,resolved', [('マモオ','',True), ('アラケ','違う内容。',False), ('アラケ','穴径。',True)])
def test_adjudication_preserves_raw_audio_and_negative_gate(tmp_path, heard, context, resolved):
    source=tmp_path/'0001.wav'
    with wave.open(str(source),'wb') as f:
        f.setparams((1,2,24000,0,'NONE','not compressed'));f.writeframes(b'\x00\x00'*24000)
    source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    meta=tmp_path/'voice.json';meta.write_text(json.dumps({'parts_checkpoint':str(tmp_path/'checkpoint.json')}))
    raw=tmp_path/'raw.json';raw.write_text(json.dumps({'lines':[{'box_index':1,'expected':'穴径。','recognized':'誤字'}]}))
    raw_bytes=raw.read_bytes()
    qa=VoicevoxQaResult(raw,'warning',1,(1,))
    box=SimpleNamespace(text='穴径。',style_id=53,query={'accentPhrases':[{'moras':[{'text':'マモオ'}]}]})
    client=Mock();client.audio_query.return_value={'accent_phrases':[{'moras':[{'text':heard}]}]}
    model=Mock();model.transcribe.return_value=([SimpleNamespace(text=context)],None)
    with patch('docurandom.voice_disambiguation.project_boxes',return_value=[box]), patch('docurandom.voice_disambiguation.voicevox_project_digest',return_value='digest'), patch('docurandom.voice_disambiguation._load_model',return_value=model):
        result=adjudicate({},SimpleNamespace(metadata_path=meta),qa,client,cancel=Event(),cpu_threads=4)
    assert (result.status=='passed') == resolved
    assert result.warning_indexes == (() if resolved else (1,))
    assert raw.read_bytes()==raw_bytes
    assert hashlib.sha256(source.read_bytes()).hexdigest()==source_hash
    proof=json.loads(result.report_path.read_text(encoding='utf-8'))
    assert proof['audio_changed'] is False and proof['original_warning_indexes']==[1]
    if heard=='マモオ':model.transcribe.assert_not_called()


def test_context_audio_rejects_mismatched_formats(tmp_path):
    for i,rate in [(1,24000),(2,48000)]:
        with wave.open(str(tmp_path/f'{i:04d}.wav'),'wb') as f:
            f.setparams((1,2,rate,0,'NONE','not compressed'));f.writeframes(b'\x00\x00')
    with pytest.raises(ValueError,match='formats differ'):
        contextual_audio(tmp_path,[1,2],tmp_path/'context.wav')
