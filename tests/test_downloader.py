from anker_client.core.downloader import DownloadTask


class _Response:
    def __init__(
        self,
        chunks: list[bytes],
        content_type: str = "application/zip",
    ):
        self._chunks = chunks
        self.headers = {
            "Content-Length": str(sum(len(chunk) for chunk in chunks)),
            "Content-Type": content_type,
        }
        self.closed = False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size: int):
        assert chunk_size == DownloadTask.CHUNK_SIZE
        yield from self._chunks

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, response: _Response):
        self.response = response
        self.request_kwargs = None
        self.request_url = None

    def get(self, url: str, **kwargs):
        assert kwargs["stream"] is True
        self.request_url = url
        self.request_kwargs = kwargs
        return self.response


def test_download_task_publishes_complete_file_atomically(tmp_path):
    response = _Response([b"PK\x03\x04", b"abcdef"])
    destination = tmp_path / "game.zip"
    completed = []
    finished = []

    session = _Session(response)
    task = DownloadTask(
        session,
        "https://cdn.example/game.zip",
        str(destination),
        headers={"Referer": "https://ankergames.net/download/token/hash"},
    )
    task.signals.completed.connect(completed.append)
    task.signals.finished.connect(lambda: finished.append(True))
    task.run()

    assert destination.read_bytes() == b"PK\x03\x04abcdef"
    assert not (tmp_path / "game.zip.part").exists()
    assert completed == [str(destination)]
    assert finished == [True]
    assert response.closed is True
    assert session.request_url == "https://cdn.example/game.zip"
    assert session.request_kwargs["headers"]["Referer"].startswith(
        "https://ankergames.net/download/"
    )


def test_download_task_rejects_html_response(tmp_path):
    response = _Response(
        [b"<!doctype html><html><body>Sign in</body></html>"],
        content_type="text/html; charset=UTF-8",
    )
    destination = tmp_path / "game.zip"
    errors = []

    task = DownloadTask(
        _Session(response),
        "https://ankergames.net/download-file/hash",
        str(destination),
    )
    task.signals.error.connect(errors.append)
    task.run()

    assert len(errors) == 1
    assert "web page instead of the game archive" in errors[0]
    assert not destination.exists()
    assert not (tmp_path / "game.zip.part").exists()


def test_download_task_rejects_non_archive_binary_response(tmp_path):
    response = _Response(
        [b"not really an archive"],
        content_type="application/octet-stream",
    )
    destination = tmp_path / "game.zip"
    errors = []

    task = DownloadTask(
        _Session(response),
        "https://ankergames.net/download-file/hash",
        str(destination),
    )
    task.signals.error.connect(errors.append)
    task.run()

    assert len(errors) == 1
    assert "not a supported ZIP, 7z, or RAR archive" in errors[0]
    assert not destination.exists()
    assert not (tmp_path / "game.zip.part").exists()


def test_download_cancellation_removes_partial_file(tmp_path):
    response = _Response([], content_type="application/zip")
    destination = tmp_path / "game.zip"
    cancelled = []

    task = DownloadTask(
        _Session(response),
        "https://cdn.example/game.zip",
        str(destination),
    )

    def chunks(chunk_size: int):
        assert chunk_size == DownloadTask.CHUNK_SIZE
        yield b"first"
        task.cancel()
        yield b"second"

    response.iter_content = chunks
    response.headers["Content-Length"] = "11"
    task.signals.cancelled.connect(lambda: cancelled.append(True))
    task.run()

    assert cancelled == [True]
    assert not destination.exists()
    assert not (tmp_path / "game.zip.part").exists()
