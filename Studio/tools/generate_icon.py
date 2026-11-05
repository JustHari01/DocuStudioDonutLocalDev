from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw


def create_icon(destination: Path) -> None:
    size = 512
    image = Image.new("RGBA", (size, size), (8, 15, 29, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (30, 30, size - 30, size - 30),
        radius=104,
        fill=(20, 31, 52, 255),
        outline=(87, 134, 255, 255),
        width=14,
    )
    # Film strip with three deliberately different frames.
    draw.rounded_rectangle((88, 120, 424, 392), radius=35, fill=(11, 20, 36, 255))
    draw.rectangle((88, 166, 424, 346), fill=(94, 140, 255, 255))
    draw.polygon(((88, 322), (188, 218), (278, 310), (340, 252), (424, 337), (424, 346), (88, 346)), fill=(22, 36, 61, 255))
    draw.ellipse((250, 197, 323, 270), fill=(252, 190, 85, 255))
    for x in (111, 175, 239, 303, 367):
        draw.rounded_rectangle((x, 134, x + 28, 153), radius=5, fill=(196, 215, 255, 255))
        draw.rounded_rectangle((x, 359, x + 28, 378), radius=5, fill=(196, 215, 255, 255))
    # A small sparkle represents a new random variation.
    draw.polygon(((400, 72), (410, 98), (438, 108), (410, 118), (400, 145), (390, 118), (362, 108), (390, 98)), fill=(61, 220, 151, 255))
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(
        destination,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("assets/docurandom.ico")
    create_icon(target)
