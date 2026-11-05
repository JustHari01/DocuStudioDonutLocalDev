"""Render complete standalone Masters from one transport contract and twenty domain charters."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'assets/research'
VERSION = '11.1.0-r1'
DOMAIN_CATALOG = json.loads((ASSETS / 'domains.json').read_text(encoding='utf-8'))
DOMAINS = {key: row['name'] for key, row in DOMAIN_CATALOG.items()}


def build():
    source = ASSETS / 'source'
    contract = (source / 'protocol.txt').read_text(encoding='utf-8')
    entries = {}
    for domain, name in DOMAINS.items():
        module = (source / (domain.replace('-', '') + '.txt')).read_text(encoding='utf-8')
        text = contract.replace('@DOMAIN_ID@', domain).replace('@DOMAIN_NAME@', name).replace('@DOMAIN_MODULE@', module)
        if '@DOMAIN_' in text:
            raise ValueError('Unrendered Master template')
        filename = f'master-{domain.replace("-", "")}-{VERSION}.txt'
        raw = text.encode('utf-8')
        (ASSETS / filename).write_bytes(raw)
        entries[domain] = {'version': VERSION, 'domain': domain, 'name': name, 'file': filename,
                           'sha256': hashlib.sha256(raw).hexdigest(), 'characters': len(text)}
    legacy_file = 'master-G13-11.0.0-r1.txt'
    legacy = (ASSETS / legacy_file).read_bytes()
    manifest = {'schema_version': 2, **entries['G-13'], 'protocol': 'content-package-v1',
                'image_interval_seconds': 30, 'subtitle_max_characters': 40, 'masters': entries,
                'archives': {'G-13': {'11.0.0-r1': {'domain': 'G-13', 'version': '11.0.0-r1',
                    'file': legacy_file, 'sha256': hashlib.sha256(legacy).hexdigest()}}},
                'scope': 'Explicit next-run selection; no change to in-flight drafts, schedules or queues',
                'research_evidence_sha256': hashlib.sha256((ASSETS / 'evidence.json').read_bytes()).hexdigest(),
                'domain_catalog_sha256': hashlib.sha256((ASSETS / 'domains.json').read_bytes()).hexdigest(),
                'source_files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(source.glob('*.txt'))}}
    (ASSETS / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return entries


if __name__ == '__main__':
    print(json.dumps(build(), ensure_ascii=False, indent=2))
