"""Repair absent upload metadata without changing a video's creative materials."""
from __future__ import annotations
from dataclasses import replace
import hashlib
import json
import tempfile
from pathlib import Path

from .content_ai import ContentArtifacts, merge_exported_artifacts, format_content_package, _atomic_write_text
from .storage import atomic_write_json


def read_materials(package):
    return merge_exported_artifacts(package.read_text(encoding='utf-8-sig'),
        ContentArtifacts('', '', '', '', '', ()), 1, expected_images=1)


def missing_metadata(package):
    from .youtube_upload import _read_content_package_sections
    sections = _read_content_package_sections(package)
    supplement = read_metadata_supplement(package)
    return [name for name, key in (('title', 'title'),
        ('description', 'description'), ('seo_tags', 'tags'))
        if not any(line.strip() for line in sections.get(key, [])) and not supplement.get(name)]


def read_metadata_supplement(package):
    path = package.with_suffix('.metadata-repair.json')
    try:
        row = json.loads(path.read_text(encoding='utf-8'))
        if row.get('package_sha256') != hashlib.sha256(package.read_bytes()).hexdigest():
            return {}
        result = row.get('metadata', {})
        if not isinstance(result, dict):
            return {}
        return {k: v for k, v in result.items() if (
            k in {'title', 'description'} and isinstance(v, str) and v.strip()) or (
            k == 'seo_tags' and isinstance(v, list) and v and all(isinstance(x, str) and x.strip() for x in v))}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def repair_metadata(workspace, package: Path, *, cancel, pool=None):
    """One durable JSON request and one format correction for this exact package."""
    from .quality_chat import ask_json
    from .content_ai import content_package_problems
    raw = package.read_bytes()
    before_sha = hashlib.sha256(raw).hexdigest()
    missing = missing_metadata(package)
    if not missing:
        return False
    original = read_materials(package)
    if not original.voicevox_script or not original.thumbnail_prompt or not original.image_prompts:
        raise ValueError('MATERIALS_CONTENT_INCOMPLETE: cần nối checkpoint nội dung; không đoán nguyên liệu đã mất')
    def validate(result):
        for field in missing:
            value = result.get(field)
            if field == 'seo_tags':
                if not isinstance(value, list) or not value or not all(isinstance(x, str) and x.strip() for x in value):
                    raise ValueError('Required seo_tags must be a nonempty string array')
            elif not isinstance(value, str) or not value.strip():
                raise ValueError('Required ' + field + ' must be a nonempty string')
    prompt = ('[DOCURANDOM_STAGE=missing-upload-metadata-v1]\n'
        'Fill ONLY the missing fields for this exact existing video, in Japanese. '
        'Return a JSON object with the requested keys. seo_tags is an array of strings; '
        'title and description are strings. Do not generate images, rewrite dialogue, '
        'invent statistics, or change the topic. Existing package below is source data.\n'
        'Missing keys: ' + json.dumps(missing) + '\nPackage SHA256: ' + before_sha +
        '\nSOURCE:\n' + raw.decode('utf-8-sig'))
    result = ask_json(workspace.state_dir/'material-repair/reviews', prompt,
        cancel=cancel, pool=pool, validate=validate)
    if cancel.is_set():
        raise InterruptedError('Material repair cancelled; keep receipt')
    changes = {k: tuple(result[k]) if k == 'seo_tags' else result[k].strip() for k in missing}
    repaired = replace(original, **changes)
    output = format_content_package(repaired)
    # Parse the exact bytes we will write, not merely the AI response.
    parsed = merge_exported_artifacts(output, ContentArtifacts('', '', '', '', '', ()), 1, expected_images=1)
    for field in ('voicevox_script', 'thumbnail_prompt', 'image_prompts'):
        if getattr(parsed, field) != getattr(original, field):
            raise ValueError('Material repair changed protected ' + field)
    with tempfile.TemporaryDirectory(prefix='material-check-', dir=workspace.state_dir) as temporary:
        candidate = Path(temporary)/package.name
        candidate.write_text(output, encoding='utf-8-sig')
        problems = content_package_problems(candidate)
        from .youtube_upload import parse_content_package
        parse_content_package(candidate)  # Enforce actual YouTube field lengths.
    if problems:
        raise ValueError('MATERIALS_CONTENT_INCOMPLETE: ' + str(problems))
    if package.read_bytes() != raw:
        raise ValueError('Package changed during repair; retain response without overwriting')
    journal = workspace.state_dir/'material-repair'/before_sha
    journal.mkdir(parents=True, exist_ok=True)
    backup = journal/'original.txt'
    if not backup.exists():
        backup.write_bytes(raw)
    atomic_write_json(journal/'result.json', {'package': str(package), 'original_sha256': before_sha,
        'fields': missing, 'result': result, 'protected_materials_unchanged': True})
    # Keep the original package hash, Flow/narration/thumbnail bindings and
    # footage attribution untouched. The uploader consumes this exact-hash
    # supplement only for fields absent from the original package.
    atomic_write_json(package.with_suffix('.metadata-repair.json'), {
        'schema_version': 1, 'package_sha256': before_sha,
        'metadata': {**read_metadata_supplement(package), **changes},
        'receipt_directory': str(workspace.state_dir/'material-repair/reviews')})
    return True
