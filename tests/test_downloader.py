from anker_client.core.downloader import DownloadTask


class _Response:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks
        self.headers = {
            "Content-Length": str(sum(len(chunk) for chunk in chunks)),
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

    def get(self, url: str, **kwargs):
        assert url == "https://cdn.example/game.zip"
        assert kwargs["stream"] is True
        return self.response


def test_download_task_publishes_complete_file_atomically(tmp_path):
    response = _Response([b"abc", b"def"])
    destination = tmp_path / "game.zip"
    completed = []
    finished = []

    task = DownloadTask(
        _Session(response),
        "https://cdn.example/game.zip",
        str(destination),
    )
    task.signals.completed.connect(completed.append)
    task.signals.finished.connect(lambda: finished.append(True))
    task.run()

    assert destination.read_bytes() == b"abcdef"
    assert not (tmp_path / "game.zip.part").exists()
    assert completed == [str(destination)]
    assert finished == [True]
    assert response.closed is True


def test_download_cancellation_removes_partial_file(tmp_path):
    response = _Response([])
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
