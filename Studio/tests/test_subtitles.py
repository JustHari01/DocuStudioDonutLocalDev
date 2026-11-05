from pathlib import Path

from docurandom.subtitles import (
    _display_units,
    prepare_ass_for_render,
    prepare_srt_for_render,
    read_srt,
    split_subtitle_text,
    subtitle_force_style,
    subtitle_line_units,
    subtitle_panel_geometry,
)


def test_japanese_subtitles_never_exceed_two_safe_width_lines() -> None:
    text = (
        "日本の工作機械産業は長い歴史の中で精度と信頼性を磨き続け、"
        "世界の製造現場を静かに支えてきました。しかし課題も残っています。"
    )
    chunks = split_subtitle_text(text)
    assert len(chunks) >= 2
    assert "".join(chunk.replace("\n", "") for chunk in chunks) == text
    for chunk in chunks:
        lines = chunk.splitlines()
        assert 1 <= len(lines) <= 2
        assert all(_display_units(line) <= 52 for line in lines)


def test_prepare_srt_splits_long_cue_and_preserves_full_timeline(tmp_path: Path) -> None:
    source = tmp_path / "voice.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:08,000\n"
        + "日本の技術です。" * 18
        + "\n",
        encoding="utf-8-sig",
    )
    destination = tmp_path / "render.srt"
    count = prepare_srt_for_render(source, destination)
    cues = read_srt(destination)
    assert count == len(cues)
    assert count > 1
    assert cues[0].start == 0.0
    assert cues[-1].end == 8.0
    assert all(len(cue.text.splitlines()) <= 2 for cue in cues)


def test_prepare_ass_uses_output_playres_so_font_is_not_scaled_up(tmp_path: Path) -> None:
    source = tmp_path / "voice.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:04,000\n"
        "工具を戻し、次の工具を呼び出し、再び加工へ戻るまでの時間。\n",
        encoding="utf-8-sig",
    )
    destination = tmp_path / "render.ass"

    count = prepare_ass_for_render(
        source,
        destination,
        width=1920,
        height=1080,
        line_units=subtitle_line_units(1920, 1080),
    )

    document = destination.read_text(encoding="utf-8-sig")
    assert count == 1
    assert "PlayResX: 1920" in document
    assert "PlayResY: 1080" in document
    assert "Style: Default,Yu Gothic,80," in document
    assert ",-1,0,0,0,100,100,0,0,1,5,0,5,144,144,86,1" in document
    assert r"{\an5\pos(960,918)\be1}" in document
    assert r"\N" in document


def test_one_and_two_line_cues_share_the_same_panel_center(tmp_path: Path) -> None:
    source = tmp_path / "voice.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:05,000\n"
        "日本のものづくりは、精度へのこだわりを大切にします。\n\n"
        "2\n00:00:05,000 --> 00:00:10,000\n"
        "現場の知恵と技術が、一つひとつの品質を支えます。"
        "小さな改善の積み重ねが、世界の信頼につながります。\n",
        encoding="utf-8-sig",
    )
    destination = tmp_path / "render.ass"

    count = prepare_ass_for_render(
        source,
        destination,
        width=1920,
        height=1080,
        line_units=52,
        foreground_scale=0.90,
    )

    dialogues = [
        line
        for line in destination.read_text(encoding="utf-8-sig").splitlines()
        if line.startswith("Dialogue:")
    ]
    assert count == 2
    assert len(dialogues) == 2
    assert all(r"{\an5\pos(960,918)\be1}" in line for line in dialogues)
    assert r"\N" not in dialogues[0]
    assert dialogues[1].count(r"\N") == 1
    assert subtitle_panel_geometry(1920, 1080) == (96, 810, 1728, 216)


def test_subtitle_style_matches_white_outline_translucent_panel_safe_area() -> None:
    style = subtitle_force_style(1920, 1080)
    assert "PrimaryColour=&H00FFFFFF" in style
    assert "OutlineColour=&H00000000" in style
    assert "BackColour=&H00000000" in style
    assert "BorderStyle=1" in style
    assert "Bold=-1" in style
    assert "Alignment=5" in style
    assert "MarginL=144" in style
    assert "MarginR=144" in style
    assert "MarginV=86" in style
    assert subtitle_line_units(1920, 1080) == 52
    assert subtitle_line_units(1080, 1920) < 24
