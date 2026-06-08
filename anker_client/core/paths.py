# anker_client/core/paths.py
import string

_INVALID_FILENAME_CHARS = set('<>:"/\\|?*')
_RESERVED_DEVICE_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_COVER_EXTRA_INVALID_CHARS = "".join(ch for ch in string.punctuation if ch not in "-_")


def sanitize_windows_name(
    name: str,
    fallback: str | None = "Game",
    *,
    extra_invalid_chars: str = "",
    max_length: int = 120,
) -> str:
    """Return a readable file or folder name that is safe on Windows."""
    invalid_chars = _INVALID_FILENAME_CHARS | set(extra_invalid_chars)
    cleaned = "".join(
        " " if ord(ch) < 32 or ch in invalid_chars else ch
        for ch in str(name)
    )
    cleaned = " ".join(cleaned.split()).strip(" .")
    if not cleaned:
        return fallback or ""

    stem = cleaned.split(".", 1)[0].upper()
    if stem in _RESERVED_DEVICE_NAMES:
        cleaned = f"_{cleaned}"

    return cleaned[:max_length].rstrip(" .") or (fallback or "")


def sanitize_cover_name(name: str) -> str:
    """Return the legacy cover-cache filename stem for a game title."""
    return sanitize_windows_name(
        name,
        fallback=None,
        extra_invalid_chars=_COVER_EXTRA_INVALID_CHARS,
    )
