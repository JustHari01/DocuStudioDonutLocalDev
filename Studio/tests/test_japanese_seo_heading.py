from docurandom.youtube_upload import parse_content_package
from docurandom.content_ai import _export_sections, _parse_seo_tags


def test_real_japanese_tags_before_empty_canonical_block(tmp_path):
    text='TIÊU ĐỀ\nAn actual title\nMÔ TẢ\nA useful description.\n■ SEOタグ\n━━━━━━━━\n工作機械,日本の技術\n\nSEO TAGS\n\nPROMPT THUMBNAIL\nCreate an image.\n'
    package=tmp_path/'video.txt';package.write_text(text,encoding='utf-8')
    meta=parse_content_package(package)
    assert meta.tags==('工作機械','日本の技術')
    assert meta.description=='A useful description.'
    assert _parse_seo_tags(_export_sections(text)['seo_tags'])==meta.tags


def test_heading_words_inside_prose_are_not_section_delimiters(tmp_path):
    text='TIÊU ĐỀ\nAn actual title\nMÔ TẢ\nSEOタグについて説明します。\nSEO TAGS\nreal,keywords\nPROMPT THUMBNAIL\nSEOタグ must not replace the image prompt.\n'
    package=tmp_path/'video.txt';package.write_text(text,encoding='utf-8')
    meta=parse_content_package(package)
    assert meta.description=='SEOタグについて説明します。'
    assert meta.tags==('real','keywords')

