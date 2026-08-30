# anker_client/core/session.py
import copy
import threading

import requests
from requests import Response
import keyring
from bs4 import BeautifulSoup
from anker_client.config import BASE_URL, KEYRING_SERVICE, KEYRING_USERNAME_KEY, KEYRING_PASSWORD_KEY


class AnkerSession:
    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/139.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",  # brotli chunked responses corrupt on large payloads
        })
        # ``requests.Session`` is not thread-safe.  UI work is deliberately
        # concurrent, so each worker gets its own connection pool seeded from
        # this canonical authenticated session.
        self._thread_local = threading.local()
        self._state_lock = threading.RLock()
        self._auth_lock = threading.Lock()
        self._session_version = 0
        self.is_logged_in = False
        self.username = None

    def _mark_session_changed(self) -> None:
        """Invalidate per-thread clients after authentication state changes."""

        lock = getattr(self, "_state_lock", None)
        if lock is None:  # Keeps lightweight ``__new__`` test doubles valid.
            return
        with lock:
            self._session_version += 1

    def _thread_client(self) -> requests.Session:
        local = self._thread_local
        with self._state_lock:
            version = self._session_version
            if getattr(local, "version", None) == version:
                return local.session
            headers = dict(self._session.headers)
            cookies = copy.copy(self._session.cookies)

        client = requests.Session()
        client.headers.update(headers)
        client.cookies.update(cookies)
        local.session = client
        local.version = version
        return client

    def _publish_cookies(self, client: requests.Session) -> None:
        """Make cookies learned through redirects available to future tasks."""

        with self._state_lock:
            before = requests.utils.dict_from_cookiejar(self._session.cookies)
            self._session.cookies.update(client.cookies)
            after = requests.utils.dict_from_cookiejar(self._session.cookies)
            if after != before:
                self._session_version += 1
                # Merge cookies another worker may have published since this
                # client was created, then mark its snapshot current.
                client.cookies.update(self._session.cookies)
                self._thread_local.version = self._session_version

    def _extract_csrf(self, html: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("meta", {"name": "csrf-token"})
        return tag.get("content") if tag else None

    def _get_csrf(self) -> str | None:
        """Fetch the home page to get a fresh CSRF token."""
        resp = self._session.get(BASE_URL, timeout=10)
        resp.raise_for_status()
        return self._extract_csrf(resp.text)

    def _get_csrf_token_endpoint(self) -> str | None:
        """Fetch the JSON CSRF endpoint when available."""
        try:
            resp = self._session.get(f"{BASE_URL}/csrf-token", timeout=10)
            resp.raise_for_status()
            return resp.json().get("token")
        except Exception:
            return None

    def login(self, email: str, password: str) -> bool:
        """
        Attempt login. Returns True on success.
        NOTE: AnkerGames uses Livewire for its login form.
        Current best-guess endpoint: POST /login with _token, email, password.
        Update LOGIN_URL and payload keys if this fails.
        """
        login_url = f"{BASE_URL}/login"

        # Only one authentication attempt may update the canonical session at
        # a time.  Normal reads still use independent worker sessions.
        auth_lock = getattr(self, "_auth_lock", None)
        lock_context = auth_lock or _NullLock()
        with lock_context:
            # Step 1: Get the login page to obtain CSRF token
            resp = self._session.get(login_url, timeout=(5, 10))
            resp.raise_for_status()
            csrf = self._get_csrf_token_endpoint() or self._extract_csrf(resp.text)
            if not csrf:
                raise RuntimeError("Could not find CSRF token on login page")

            # Step 2: POST credentials
            payload = {
                "_token": csrf,
                "email": email,
                "password": password,
            }
            resp = self._session.post(
                login_url,
                data=payload,
                timeout=(5, 10),
                allow_redirects=True,
            )
            if resp.status_code == 419:
                csrf = self._get_csrf_token_endpoint()
                if csrf:
                    payload["_token"] = csrf
                    resp = self._session.post(
                        login_url,
                        data=payload,
                        timeout=(5, 10),
                        allow_redirects=True,
                    )

            # Step 3: Check if login succeeded (redirected away from /login)
            self.is_logged_in = resp.url != login_url and "/login" not in resp.url
            self.username = email if self.is_logged_in else None
            self._mark_session_changed()
            return self.is_logged_in

    def save_credentials(self, email: str, password: str) -> None:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME_KEY, email)
        keyring.set_password(KEYRING_SERVICE, KEYRING_PASSWORD_KEY, password)

    def load_credentials(self) -> tuple[str | None, str | None]:
        email = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME_KEY)
        password = keyring.get_password(KEYRING_SERVICE, KEYRING_PASSWORD_KEY)
        return email, password

    def get(self, url: str, **kwargs) -> Response:
        client = self._thread_client()
        response = client.get(url, **kwargs)
        self._publish_cookies(client)
        return response

    def post(self, url: str, **kwargs) -> Response:
        client = self._thread_client()
        response = client.post(url, **kwargs)
        self._publish_cookies(client)
        return response

    def close(self) -> None:
        self._session.close()
        client = getattr(self._thread_local, "session", None)
        if client is not None:
            client.close()


class _NullLock:
    """Compatibility context for tests constructing AnkerSession via __new__."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False
