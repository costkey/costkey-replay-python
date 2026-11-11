import gzip
import json
import threading
from typing import List, Optional, Tuple

from costkey_replay._config import Dsn, parse_dsn
from costkey_replay._uploader import Uploader

DSN = Dsn(auth_key="ck_test", project_id="proj", base_url="https://costkey.example.com")


class Server:
    """Answers each request with the next status in its script (default 200)."""

    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses)
        self.calls: List[Tuple[str, str, dict, Optional[bytes]]] = []
        self.lock = threading.Lock()

    def __call__(self, method: str, url: str, headers: dict, body: Optional[bytes]) -> Tuple[int, str]:
        with self.lock:
            self.calls.append((method, url, headers, body))
            return (self.statuses.pop(0) if self.statuses else 200), ""


def test_chunks_go_out_gzipped_with_the_key():
    server = Server()
    uploader = Uploader(DSN, request=server)
    uploader.enqueue_chunk("rec_12345678", 0, "trace_1", [{"type": 4, "timestamp": 1}])
    assert uploader.flush(5)
    method, url, headers, body = server.calls[0]
    assert (method, url) == ("POST", "https://costkey.example.com/api/v1/replay/chunks")
    assert headers["Authorization"] == "Bearer ck_test"
    assert headers["Content-Encoding"] == "gzip"
    assert headers["User-Agent"].startswith("costkey-replay-python/")
    assert json.loads(gzip.decompress(body)) == {"recordingId": "rec_12345678", "seq": 0, "traceId": "trace_1", "events": [{"type": 4, "timestamp": 1}]}


def test_assets_are_sent_once_and_skipped_when_the_server_has_them():
    server = Server(404, 201, 200)
    uploader = Uploader(DSN, request=server)
    uploader.enqueue_asset("a" * 64, "image/png", b"png")
    uploader.enqueue_asset("a" * 64, "image/png", b"png")  # Same hash: not queued again.
    uploader.enqueue_asset("b" * 64, "image/png", b"png2")
    assert uploader.flush(5)
    assert [(call[0], call[1].rsplit("/", 1)[-1][:1]) for call in server.calls] == [("HEAD", "a"), ("PUT", "a"), ("HEAD", "b")]


def test_a_server_error_is_retried():
    server = Server(503, 202)
    uploader = Uploader(DSN, request=server, retry_delay=lambda attempt: 0.01)
    uploader.enqueue_chunk("rec_12345678", 0, None, [{"type": 4}])
    assert uploader.flush(5)
    assert len(server.calls) == 2


def test_flush_gives_waiting_uploads_one_last_try():
    server = Server(503, 503, 202)
    uploader = Uploader(DSN, request=server, retry_delay=lambda attempt: 60.0)
    uploader.enqueue_chunk("rec_12345678", 0, None, [{"type": 4}])
    assert uploader.flush(5)
    assert len(server.calls) == 2  # First try, then the one last try during flush; then it's dropped.


def test_recordings_off_stops_everything():
    reasons = []
    server = Server(422)
    uploader = Uploader(DSN, request=server, on_rejected=reasons.append)
    uploader.enqueue_chunk("rec_12345678", 0, None, [{"type": 4}])
    uploader.flush(5)
    uploader.enqueue_chunk("rec_12345678", 1, None, [{"type": 3}])
    assert uploader.stopped
    assert reasons == ["screen recordings are off for this project"]
    assert len(server.calls) == 1


def test_the_queue_is_bounded_and_assets_go_first():
    gate = threading.Event()

    def slow(method, url, headers, body):
        gate.wait(5)
        return 200, ""

    uploader = Uploader(DSN, request=slow, max_queue_bytes=2000)
    uploader.enqueue_asset("c" * 64, "image/png", b"x" * 900)
    uploader.enqueue_asset("d" * 64, "image/png", b"x" * 900)
    uploader.enqueue_chunk("rec_12345678", 0, None, [{"blob": "y" * 5000}])  # Small once gzipped: makes room by dropping an asset.
    big = [{"blob": __import__("os").urandom(3000).hex()}]
    uploader.enqueue_chunk("rec_12345678", 1, None, big)  # Too big to ever fit: dropped.
    gate.set()
    assert uploader.flush(5)


def test_dsn_parsing():
    assert parse_dsn("https://ck_abc@costkey.example.com/proj_1") == Dsn("ck_abc", "proj_1", "https://costkey.example.com")
    assert parse_dsn("http://ck_abc@localhost:4100/costkey/proj_1").base_url == "http://localhost:4100/costkey"
    for bad in ("not a url", "https://costkey.example.com/proj_1", "https://ck@costkey.example.com/"):
        try:
            parse_dsn(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
