"""Account session management.

* Credentials: ``keyring`` service ``KEYRING_SERVICE`` with keys
  ``KEYRING_EMAIL_KEY``/``KEYRING_PASSWORD_KEY`` (same as the legacy client, so
  remembered sign-ins carry over). Keyring failures are logged, never fatal.
  ``login(remember=…)`` stores or forgets them and mirrors the choice into
  ``settings.remember_login``; turning that setting off forgets the stored
  credentials and the saved session. ``logout`` forgets the password (the email
  stays for pre-filling the sign-in form while ``remember_login`` is on).
  A remembered password the site rejects during ``restore`` is forgotten.
* Session cookies (only those for the site's domain) persist in
  ``paths.cookies_file`` encrypted with Windows DPAPI
  (``win32crypt.CryptProtectData``, user scope, app-specific entropy; file =
  ``MAGIC`` + blob, written atomically); on non-Windows or failure, cookies are
  not persisted (logged once). ``save_session`` never overwrites a saved
  session while nobody is signed in (e.g. ``restore`` failed offline) — only
  ``logout``, an expired session or ``remember_login=False`` delete it.
* ``restore`` (startup, background): import persisted cookies → ``client.current_user()``;
  if that returns None (session expired: the file is deleted) and credentials
  are remembered → ``login``. Called while signed in, it re-verifies the live
  session the same way (and signs out locally when it expired and cannot be
  renewed). Never raises for network/site errors (logs and returns the
  unchanged state: None at startup); ``OperationCancelled`` propagates.
* ``login``/``login_with_cookies``/``logout`` publish ``AuthChanged``
  (``restore`` publishes when it signs someone in, or signs a stale user out).
* Every sign-in state change bumps an internal epoch. A ``restore`` running in
  the background only applies its outcome (user, deleting an expired session
  file, signing in with remembered credentials) when no ``login``/``logout``
  happened since it started, so it can never sign out a user who just signed
  in, nor revive one who just signed out. ``save_session`` likewise never
  writes cookies captured before a ``logout``. ``logout`` drops the local
  user first, then signs out on the site.
* ``login_with_cookies``: used by the UI's "Sign in with Discord" browser flow —
  import cookies captured from the embedded browser (site domain only), verify
  with ``current_user``; ``AuthError`` when the site does not see a signed-in user.
* ``user`` is safe to read from any thread (returns a copy).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import keyring

from anker_client.constants import KEYRING_EMAIL_KEY, KEYRING_PASSWORD_KEY, KEYRING_SERVICE, SITE_HOST
from anker_client.core.errors import AnkerError, AuthError, LoginFailedError, OperationCancelled
from anker_client.core.events import AuthChanged, EventBus, SettingsChanged
from anker_client.core.models import UserInfo, utc_now_iso
from anker_client.core.paths import AppPaths
from anker_client.core.settings import SettingsStore
from anker_client.core.tasks import CancelToken
from anker_client.services import _auth_dpapi
from anker_client.site.client import AnkerGamesClient

log = logging.getLogger(__name__)

SESSION_MAGIC = b"ACSESS1\n"
_SESSION_VERSION = 1


def _check(token: CancelToken | None) -> None:
    if token is not None:
        token.raise_if_cancelled()


def _is_site_cookie(cookie: dict[str, Any]) -> bool:
    domain = str(cookie.get("domain") or "").lstrip(".").lower()
    return not domain or domain == SITE_HOST or domain.endswith("." + SITE_HOST)


def _is_expired(cookie: dict[str, Any], now: float) -> bool:
    expires = cookie.get("expires")
    if expires in (None, "", 0):
        return False  # session cookie
    try:
        return float(expires) < now
    except (TypeError, ValueError):
        return False


def _clean_cookies(cookies: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    now = time.time()
    return [
        dict(c)
        for c in cookies or []
        if isinstance(c, dict) and c.get("name") and _is_site_cookie(c) and not _is_expired(c, now)
    ]


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class AuthService:
    def __init__(self, client: AnkerGamesClient, settings: SettingsStore, events: EventBus, paths: AppPaths) -> None:
        self._client = client
        self._settings = settings
        self._events = events
        self._session_file = Path(paths.cookies_file)
        self._lock = threading.RLock()  # guards _user and _epoch
        self._file_lock = threading.Lock()  # serialises session file writes/deletes
        self._user: UserInfo | None = None
        self._epoch = 0  # bumped by every sign-in state change (see the module docstring)
        self._persist_warned = False
        self._unsubscribe = events.subscribe(SettingsChanged, self._on_settings_changed)

    @property
    def user(self) -> UserInfo | None:
        with self._lock:
            return self._user.copy() if self._user is not None else None

    @property
    def is_logged_in(self) -> bool:
        with self._lock:
            return self._user is not None

    def remembered_email(self) -> str:
        return self._keyring_get(KEYRING_EMAIL_KEY)

    def restore(self, *, token: CancelToken | None = None) -> UserInfo | None:
        epoch = self._current_epoch()
        remember = self._settings.get().remember_login
        try:
            _check(token)
            # A live session (signed in earlier in this run) is re-verified; otherwise use the saved one.
            if self.is_logged_in or (remember and self._import_saved_cookies()):
                user = self._client.current_user(token=token)
                if user is not None:
                    user = self._keep_known_email(user)
                    changed = self._set_user(user, expected_epoch=epoch)
                    if changed is None:
                        return self._superseded()
                    self.save_session()  # the site may have rotated the session cookie
                    if changed:
                        self._publish()
                    log.info("Session restored")
                    return user.copy()
                log.info("The session has expired")
                if not self._delete_session_file(expected_epoch=epoch):
                    return self._superseded()
            _check(token)
            email = self._keyring_get(KEYRING_EMAIL_KEY) if remember else ""
            password = self._keyring_get(KEYRING_PASSWORD_KEY) if email else ""
            if email and password:
                if self._current_epoch() != epoch:
                    return self._superseded()
                try:
                    signed_in = self._login(email, password, remember=True, token=token, expected_epoch=epoch)
                except LoginFailedError:
                    log.warning("The remembered password was rejected; forgetting it")
                    self._keyring_delete(KEYRING_PASSWORD_KEY)
                else:
                    return signed_in if signed_in is not None else self._superseded()
        except OperationCancelled:
            raise
        except AnkerError as exc:
            log.warning("Could not restore the session: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
            return self.user  # unchanged: None at startup, the live user when re-verifying offline
        changed = self._set_user(None, expected_epoch=epoch)
        if changed is None:
            return self._superseded()
        if changed:
            self._publish()
        return None

    def login(self, email: str, password: str, *, remember: bool = True, token: CancelToken | None = None) -> UserInfo:
        user = self._login(email, password, remember=remember, token=token, expected_epoch=None)
        assert user is not None  # only a background sign-in (expected_epoch) can be superseded
        return user

    def login_with_cookies(self, cookies: list[dict[str, Any]], *, token: CancelToken | None = None) -> UserInfo:
        site_cookies = _clean_cookies(cookies)
        if not site_cookies:
            raise AuthError("The sign-in did not complete. Please try again.", detail="no site cookies captured")
        _check(token)
        self._client.http.import_cookies(site_cookies)
        user = self._client.current_user(token=token)
        if user is None:
            raise AuthError("The sign-in did not complete. Please try again.", detail="site reports no user")
        self._set_user(user)
        self.save_session()
        self._publish()
        log.info("Signed in with browser cookies")
        return user.copy()

    def logout(self, *, token: CancelToken | None = None) -> None:
        # Drop the local user (and bump the epoch) before anything else, so a restore() or
        # save_session() running concurrently can no longer revive the session.
        self._set_user(None)
        try:
            self._client.logout(token=token)
        except AnkerError as exc:  # signing out locally must always work (offline, cancelled...)
            log.info("Remote sign-out failed (%s); signing out locally", exc)
        except Exception:
            log.warning("Remote sign-out failed unexpectedly; signing out locally", exc_info=True)
        try:
            self._client.http.clear_cookies()
        except Exception:
            log.warning("Could not clear cookies", exc_info=True)
        self._delete_session_file()
        self._keyring_delete(KEYRING_PASSWORD_KEY)
        self._publish()
        log.info("Signed out")

    def save_session(self) -> None:
        """Persist current cookies (called after login and at shutdown)."""
        if not self._settings.get().remember_login:
            self._delete_session_file()
            return
        with self._lock:
            if self._user is None:
                return  # never replace a possibly valid saved session with guest cookies
            epoch = self._epoch
        try:
            cookies = _clean_cookies(self._client.http.export_cookies())
        except Exception:
            log.warning("Could not read cookies to save the session", exc_info=True)
            return
        if not cookies:
            return
        payload = json.dumps(
            {"version": _SESSION_VERSION, "saved_at": utc_now_iso(), "cookies": cookies}, separators=(",", ":")
        ).encode("utf-8")
        blob = _auth_dpapi.protect(payload)
        if blob is None:
            if not self._persist_warned:
                self._persist_warned = True
                log.warning("Session cookies cannot be encrypted on this system; the session will not be saved")
            return
        with self._file_lock:
            if self._current_epoch() != epoch:
                log.debug("Not saving the session: the user signed in or out meanwhile")
                return
            try:
                _atomic_write_bytes(self._session_file, SESSION_MAGIC + blob)
            except OSError:
                log.warning("Could not save the session to %s", self._session_file, exc_info=True)

    # --- internals --------------------------------------------------------------------
    def _login(
        self,
        email: str,
        password: str,
        *,
        remember: bool,
        token: CancelToken | None,
        expected_epoch: int | None,
    ) -> UserInfo | None:
        """Sign in; ``None`` (nothing applied) when ``expected_epoch`` is stale once the site answered."""
        email = (email or "").strip()
        if not email or not password:
            raise LoginFailedError("Enter your email address and password.")
        _check(token)
        user = self._client.login(email, password, remember=remember, token=token)
        if not user.email:
            user = user.copy()
            user.email = email
        if self._set_user(user, expected_epoch=expected_epoch) is None:
            return None
        self._remember_choice(remember)
        if remember:
            self._keyring_set(KEYRING_EMAIL_KEY, email)
            self._keyring_set(KEYRING_PASSWORD_KEY, password)
        else:
            self._forget_credentials()
        self.save_session()
        self._publish()
        log.info("Signed in")
        return user.copy()

    def _remember_choice(self, remember: bool) -> None:
        """Mirror the sign-in form's "remember me" into settings (``_on_settings_changed`` forgets when off)."""
        try:
            if self._settings.get().remember_login != remember:
                self._settings.update(remember_login=remember)
        except OSError:  # the settings file could not be written: the sign-in itself still succeeded
            log.warning("Could not save the remember-sign-in setting", exc_info=True)

    def _current_epoch(self) -> int:
        with self._lock:
            return self._epoch

    def _superseded(self) -> UserInfo | None:
        log.info("A sign-in or sign-out happened while restoring the session; keeping it")
        return self.user

    def _set_user(self, user: UserInfo | None, *, expected_epoch: int | None = None) -> bool | None:
        """Store a copy of ``user`` and bump the epoch.

        Returns True when the signed-in state or identity changed, False when not, and ``None``
        (nothing stored) when ``expected_epoch`` is given and another change happened since.
        """
        with self._lock:
            if expected_epoch is not None and expected_epoch != self._epoch:
                return None
            before = self._user
            self._user = user.copy() if user is not None else None
            self._epoch += 1
            return (before is None) != (user is None) or (before is not None and before != user)

    def _keep_known_email(self, user: UserInfo) -> UserInfo:
        """The site rarely shows the email; keep the one we know for the same account."""
        current = self.user
        if user.email or current is None or current.display_name != user.display_name:
            return user
        merged = user.copy()
        merged.email = current.email
        return merged

    def _publish(self) -> None:
        self._events.publish(AuthChanged(user=self.user))

    def _import_saved_cookies(self) -> bool:
        cookies = self._load_session_file()
        if not cookies:
            return False
        try:
            self._client.http.import_cookies(cookies)
        except Exception:
            log.warning("Could not import the saved session cookies", exc_info=True)
            return False
        return True

    def _load_session_file(self) -> list[dict[str, Any]]:
        try:
            raw = self._session_file.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as exc:
            log.warning("Could not read the saved session: %s", exc)
            return []
        if not raw.startswith(SESSION_MAGIC):
            log.warning("Ignoring an unrecognised session file")
            return []
        data = _auth_dpapi.unprotect(raw[len(SESSION_MAGIC) :])
        if data is None:
            return []
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            log.warning("Ignoring a corrupt session file")
            return []
        cookies = payload.get("cookies") if isinstance(payload, dict) else None
        return _clean_cookies(cookies if isinstance(cookies, list) else [])

    def _delete_session_file(self, *, expected_epoch: int | None = None) -> bool:
        """Delete the saved session; False (kept) when ``expected_epoch`` is stale."""
        with self._file_lock:
            if expected_epoch is not None and self._current_epoch() != expected_epoch:
                return False
            try:
                self._session_file.unlink(missing_ok=True)
            except OSError:
                log.warning("Could not delete the saved session %s", self._session_file, exc_info=True)
        return True

    def _forget_credentials(self) -> None:
        self._keyring_delete(KEYRING_PASSWORD_KEY)
        self._keyring_delete(KEYRING_EMAIL_KEY)

    def _on_settings_changed(self, event: SettingsChanged) -> None:
        if "remember_login" in event.keys and not self._settings.get().remember_login:
            self._forget_credentials()
            self._delete_session_file()

    # --- keyring (failures are never fatal) ---------------------------------------------
    @staticmethod
    def _keyring_get(key: str) -> str:
        try:
            return keyring.get_password(KEYRING_SERVICE, key) or ""
        except Exception as exc:
            log.warning("Could not read %r from the credential store: %s", key, exc)
            return ""

    @staticmethod
    def _keyring_set(key: str, value: str) -> None:
        try:
            keyring.set_password(KEYRING_SERVICE, key, value)
        except Exception as exc:
            log.warning("Could not save %r to the credential store: %s", key, exc)

    @staticmethod
    def _keyring_delete(key: str) -> None:
        try:
            if keyring.get_password(KEYRING_SERVICE, key) is not None:
                keyring.delete_password(KEYRING_SERVICE, key)
        except Exception as exc:
            log.warning("Could not remove %r from the credential store: %s", key, exc)

