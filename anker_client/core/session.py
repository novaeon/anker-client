# anker_client/core/session.py
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
        self.is_logged_in = False
        self.username = None

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
        LOGIN_URL = f"{BASE_URL}/login"

        # Step 1: Get the login page to obtain CSRF token
        resp = self._session.get(LOGIN_URL, timeout=10)
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
        resp = self._session.post(LOGIN_URL, data=payload, timeout=10, allow_redirects=True)
        if resp.status_code == 419:
            csrf = self._get_csrf_token_endpoint()
            if csrf:
                payload["_token"] = csrf
                resp = self._session.post(
                    LOGIN_URL, data=payload, timeout=10, allow_redirects=True
                )

        # Step 3: Check if login succeeded (redirected away from /login)
        self.is_logged_in = resp.url != LOGIN_URL and "/login" not in resp.url
        if self.is_logged_in:
            self.username = email
        return self.is_logged_in

    def save_credentials(self, email: str, password: str) -> None:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME_KEY, email)
        keyring.set_password(KEYRING_SERVICE, KEYRING_PASSWORD_KEY, password)

    def load_credentials(self) -> tuple[str | None, str | None]:
        email = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME_KEY)
        password = keyring.get_password(KEYRING_SERVICE, KEYRING_PASSWORD_KEY)
        return email, password

    def get(self, url: str, **kwargs) -> Response:
        return self._session.get(url, **kwargs)

    def post(self, url: str, **kwargs) -> Response:
        return self._session.post(url, **kwargs)
