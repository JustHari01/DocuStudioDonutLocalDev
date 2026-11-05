import json
from unittest.mock import patch

import pytest

from docurandom.ui import _SharedChatGPTTurnPool


READY = dict(schema_version=1, complete=True, authenticated=True,
             ready=True, hard_rate_limited=False, capabilities_reduced=True)


def saved_pool(tmp_path, version=1, remaining=3600):
    path = tmp_path / 'cooldown.json'
    path.write_text(json.dumps(dict(version=version, expires_at_epoch=1000 + remaining)))
    pool = _SharedChatGPTTurnPool(3, configured_limit=3, wall_clock=lambda: 1000,
                                 monotonic=lambda: 100, cooldown_path=path)
    return pool, path


def test_legacy_advisory_migration_is_durable_and_resumes_serially(tmp_path):
    pool, path = saved_pool(tmp_path)
    assert pool.current_limit == 0
    assert pool.reconcile_legacy_cooldown(READY)
    assert pool.current_limit == 1
    assert not pool.reconcile_legacy_cooldown(READY)
    saved = json.loads(path.read_text())
    assert saved['kind'] == 'legacy-advisory-reconciled'
    restarted = _SharedChatGPTTurnPool(3, configured_limit=3, wall_clock=lambda: 1001,
                                     cooldown_path=path)
    assert restarted.current_limit == 3


@pytest.mark.parametrize('field,value', [('schema_version', 0), ('complete', False),
    ('authenticated', False), ('ready', False), ('hard_rate_limited', True)])
def test_incomplete_or_hard_limit_evidence_keeps_saved_pause(tmp_path, field, value):
    pool, path = saved_pool(tmp_path)
    before = path.read_bytes()
    assert not pool.reconcile_legacy_cooldown({**READY, field: value})
    assert pool.current_limit == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize('version,remaining', [(2, 3600), (1, 300)])
def test_explicit_hard_or_short_legacy_limit_is_not_migrated(tmp_path, version, remaining):
    pool, path = saved_pool(tmp_path, version, remaining)
    before = path.read_bytes()
    assert not pool.reconcile_legacy_cooldown(READY)
    assert pool.current_limit == 0
    assert path.read_bytes() == before


def test_new_hard_limit_wins_race_with_advisory_probe(tmp_path):
    pool, path = saved_pool(tmp_path)
    pool.mark_rate_limited(cooldown_seconds=300)
    assert not pool.reconcile_legacy_cooldown(READY)
    assert json.loads(path.read_text())['kind'] == 'hard-rate-limit'
    assert pool.current_limit == 0


def test_persistence_failure_keeps_queue_closed(tmp_path):
    pool, path = saved_pool(tmp_path)
    with patch('docurandom.ui.atomic_write_json', side_effect=OSError('locked')):
        assert not pool.reconcile_legacy_cooldown(READY)
    assert pool.current_limit == 0
    assert json.loads(path.read_text())['version'] == 1
