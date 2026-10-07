"""Windows DPAPI helpers for :mod:`anker_client.services.auth` (user-scope encryption at rest).

``protect``/``unprotect`` return ``None`` instead of raising when DPAPI is not
available (non-Windows, pywin32 missing) or when the blob cannot be processed
(e.g. it was encrypted by another Windows user). ``win32crypt`` is imported
lazily, once.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Extra secret mixed into the encryption so other programs running as the same
# user cannot decrypt the blob by calling CryptUnprotectData without it.
_ENTROPY = b"AnkerClient/session-cookies/v1"
_DESCRIPTION = "AnkerClient session"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1

_UNSET: Any = object()
_module: Any = _UNSET


def _crypt() -> Any | None:
    global _module
    if _module is _UNSET:
        try:
            import win32crypt  # type: ignore[import-not-found]

            _module = win32crypt
        except Exception as exc:  # ImportError, or a broken pywin32 install (DLL load failures)
            log.info("Windows DPAPI is unavailable (%s); sign-in sessions will not be remembered", exc)
            _module = None
    return _module


def available() -> bool:
    return _crypt() is not None


def protect(data: bytes) -> bytes | None:
    crypt = _crypt()
    if crypt is None:
        return None
    try:
        return bytes(crypt.CryptProtectData(data, _DESCRIPTION, _ENTROPY, None, None, _CRYPTPROTECT_UI_FORBIDDEN))
    except Exception as exc:
        log.warning("Could not encrypt the session with DPAPI: %s", exc)
        return None


def unprotect(blob: bytes) -> bytes | None:
    crypt = _crypt()
    if crypt is None:
        return None
    try:
        _description, data = crypt.CryptUnprotectData(blob, _ENTROPY, None, None, _CRYPTPROTECT_UI_FORBIDDEN)
        return bytes(data)
    except Exception as exc:
        log.warning("Could not decrypt the saved session: %s", exc)
        return None
