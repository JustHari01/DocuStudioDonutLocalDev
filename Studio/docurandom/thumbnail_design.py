"""One thumbnail design shared by transport, OCR and semantic verification."""
from __future__ import annotations
import re
import unicodedata

COLORS = ('yellow', 'cyan', 'red')


def normal(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text).strip('「」"“” ')).casefold()


def extract_headlines(prompt: str) -> list[str]:
    """Read legacy ordinal syntax and labelled colour blocks without rewriting text."""
    found = []
    for ordinal, position, color, vi in zip(('first', 'second', 'third'), ('top', 'middle', 'bottom'), COLORS, ('vàng', 'cyan', 'đỏ')):
        # Preserve the complete line, including quoted text followed by a
        # suffix. Common legacy prompts use colour adjectives and positions.
        explicit = re.search(rf'(?mi)^\s*(?:{position}(?:[- ]left|[- ]right)?|{ordinal})\b[^:\r\n]*\b{color}\b[^:\r\n]*:\s*(?:\r?\n\s*)?([^\r\n]+)', prompt)
        label = rf'(?:{ordinal}\s+line|(?:{position}\s+)?{color}(?:\s+{position})?(?:\s+line)?|{vi})'
        match = explicit or re.search(rf'(?<![\w]){label}\s*:?\s*[「"“]([^」"”\r\n]+)[」"”]', prompt, re.I)
        if not match:
            match = re.search(rf'(?mi)^\s*{label}\s*:\s*(?:\r?\n\s*)?([^\r\n]+)', prompt)
        if not match:
            raise ValueError('THUMBNAIL_CONTRACT_PENDING: missing exact ' + color + ' headline')
        value = match[1].strip().strip('「」"“” ')
        if not re.search(r'[\u3040-\u30ff\u3400-\u9fff]', value):
            raise ValueError('THUMBNAIL_CONTRACT_PENDING: headline is not Japanese')
        found.append(value)
    return found


def extract_micro_labels(prompt: str) -> list[str]:
    """Read two design labels, excluding translations and repeated English clauses."""
    values = {}
    translating = False
    for line in prompt.splitlines():
        heading = unicodedata.normalize('NFKC', line).strip().casefold()
        if re.match(r'^(?:dịch nghĩa|translation)\s*:?\s*$', heading):
            translating = True
            continue
        if re.match(r'^(?:bố cục|prompt thumbnail hoàn chỉnh|complete thumbnail prompt)\s*:?\s*$', heading):
            translating = False
        if translating:
            continue
        match = re.match(r'^\s*(?:micro[_ ]?([12])|micro.label\s*([12]))\s*:\s*(.+)$', line, re.I)
        if not match:
            continue
        index = int(match[1] or match[2])
        quoted = re.search(r'[「"“]([^」"”]+)[」"”]', match[3])
        value = (quoted[1] if quoted else match[3]).strip()
        if index in values and normal(values[index]) != normal(value):
            raise ValueError('THUMBNAIL_CONTRACT_PENDING: conflicting micro label ' + str(index))
        values.setdefault(index, value)
    return [values[i] for i in (1, 2) if i in values]


def design_spec(prompt: str) -> dict:
    return {'schema_version': 1, 'width': 1280, 'height': 720,
            'headlines': [{'text': text, 'color': color} for text, color in zip(extract_headlines(prompt), COLORS)],
            'micro_labels': extract_micro_labels(prompt),
            'layout': prompt}
