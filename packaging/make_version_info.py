"""Write build/version_info.txt (Windows VERSIONINFO resource) from anker_client.__version__."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from anker_client import __version__  # noqa: E402
from anker_client.constants import APP_NAME  # noqa: E402

TEMPLATE = """# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({v0}, {v1}, {v2}, 0),
    prodvers=({v0}, {v1}, {v2}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', '{name}'),
        StringStruct('FileDescription', '{name} - unofficial AnkerGames client'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', '{name}'),
        StringStruct('LegalCopyright', 'MIT License'),
        StringStruct('OriginalFilename', '{name}.exe'),
        StringStruct('ProductName', '{name}'),
        StringStruct('ProductVersion', '{version}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main() -> int:
    parts = [int(p) for p in re.findall(r"\d+", __version__)[:3]]
    parts += [0] * (3 - len(parts))
    out = ROOT / "build" / "version_info.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        TEMPLATE.format(v0=parts[0], v1=parts[1], v2=parts[2], version=__version__, name=APP_NAME),
        encoding="utf-8",
    )
    print(f"Wrote {out} for version {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
