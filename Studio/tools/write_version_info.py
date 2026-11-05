from __future__ import annotations

import argparse
import re
from pathlib import Path


SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def render_version_info(
    version: str,
    *,
    internal_name: str,
    description: str,
) -> str:
    match = SEMVER.fullmatch(version)
    if match is None:
        raise ValueError("version must use X.Y.Z")
    major, minor, patch = (int(value) for value in match.groups())
    dotted = f"{version}.0"
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({major}, {minor}, {patch}, 0),
    prodvers=({major}, {minor}, {patch}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        u'040904B0',
        [
          StringStruct(u'CompanyName', u'DocuRandom Studio'),
          StringStruct(u'FileDescription', {description!r}),
          StringStruct(u'FileVersion', u'{dotted}'),
          StringStruct(u'InternalName', {internal_name!r}),
          StringStruct(u'OriginalFilename', {internal_name!r} + u'.exe'),
          StringStruct(u'ProductName', u'DocuRandom Studio'),
          StringStruct(u'ProductVersion', u'{dotted}')
        ]
      )
    ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--internal-name", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        render_version_info(
            args.version,
            internal_name=args.internal_name,
            description=args.description,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
