# tests/test_session.py
from unittest.mock import patch, MagicMock
import threading
from anker_client.core.session import AnkerSession
from anker_client.config import BASE_URL

def test_csrf_extracted_from_html():
    html = '<html><head><meta name="csrf-token" content="abc123"></head></html>'
    session = AnkerSession.__new__(AnkerSession)
    token = session._extract_csrf(html)
    assert token == "abc123"

def test_csrf_returns_none_when_missing():
    html = "<html><head></head></html>"
    session = AnkerSession.__new__(AnkerSession)
    token = session._extract_csrf(html)
    assert token is None

def test_is_logged_in_false_by_default():
    session = AnkerSession()
    assert session.is_logged_in is False


def test_login_prefers_fresh_csrf_endpoint_token():
    session = AnkerSession.__new__(AnkerSession)
    session._session = MagicMock()
    session.is_logged_in = False
    session.username = None

    login_page = MagicMock()
    login_page.text = '<meta name="csrf-token" content="stale-token">'
    login_page.raise_for_status = MagicMock()

    csrf_resp = MagicMock()
    csrf_resp.json.return_value = {"token": "fresh-token"}
    csrf_resp.raise_for_status = MagicMock()

    post_resp = MagicMock()
    post_resp.status_code = 302
    post_resp.url = BASE_URL

    session._session.get.side_effect = [login_page, csrf_resp]
    session._session.post.return_value = post_resp

    assert session.login("user@example.com", "secret") is True
    assert session._session.post.call_args.kwargs["data"]["_token"] == "fresh-token"


def test_worker_threads_receive_independent_requests_sessions():
    session = AnkerSession()
    session._session.cookies.set("anker", "cookie")
    clients = []
    barrier = threading.Barrier(3)

    def capture_client():
        client = session._thread_client()
        clients.append(client)
        barrier.wait()

    threads = [threading.Thread(target=capture_client) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert len(clients) == 2
    assert clients[0] is not clients[1]
    assert all(client is not session._session for client in clients)
    assert all(client.cookies.get("anker") == "cookie" for client in clients)
