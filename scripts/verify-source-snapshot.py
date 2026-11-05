"""Verify the imported source baseline; does not execute either application."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVED = {
    'README.md': 'docs/source-import/README-before-monorepo.md',
    '.gitignore': 'docs/source-import/root.gitignore.original.txt',
    'Studio/.gitignore': 'docs/source-import/studio.gitignore.original.txt',
    'Studio/.gitattributes': 'docs/source-import/studio.gitattributes.original.txt',
}
manifest = json.loads((ROOT / 'docs/source-import/source-manifest.json').read_text(encoding='utf-8'))
failures = []
for row in manifest['files']:
    path = ROOT / ARCHIVED.get(row['path'], row['path'])
    if not path.is_file():
        failures.append({'path': row['path'], 'reason': 'missing'})
    elif hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
        failures.append({'path': row['path'], 'reason': 'changed_from_import'})
print(json.dumps({'verified_files': len(manifest['files']) - len(failures), 'failures': failures}, ensure_ascii=False, indent=2))
raise SystemExit(bool(failures))
