"""The achievement sound: ``resources/achievement.wav``, played for every pop-up (rare ones too).

Playback uses ``winsound.PlaySound(..., SND_FILENAME | SND_ASYNC)`` on Windows:
non-blocking, no Qt Multimedia/FFmpeg dependency, and the file (16-bit PCM WAV)
plays as shipped. Elsewhere, or when the file is missing, it is silent.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from anker_client.core.paths import resource_path

log = logging.getLogger(__name__)

BUNDLED_SOUND = "achievement.wav"  # in anker_client/resources


def _play_file(path: Path) -> None:  # patched by tests
    if sys.platform != "win32":
        return
    import winsound

    winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)


class AchievementSound:
    """``play`` never blocks the GUI thread."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = resource_path(BUNDLED_SOUND) if path is None else Path(path)

    @property
    def available(self) -> bool:
        return self.path.is_file()

    def play(self) -> None:
        if not self.available:
            log.debug("Achievement sound %s is missing", self.path)
            return
        try:
            _play_file(self.path)
        except Exception:
            log.debug("Could not play the achievement sound", exc_info=True)
