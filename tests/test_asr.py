# SPDX-License-Identifier: MIT
"""Model download and resume.

These lock down the first-run path. Every case here failed in practice once:
the download of a 230 MB model over a mirror that keeps dropping the connection.
The bugs were deleting the partial file on failure (so resume never happened)
and computing the expected size before checking whether the server honoured the
Range request (so a *complete* download looked short).

A local HTTP server stands in for the mirror, so no test touches the network.
"""

from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from douyin2text import asr  # noqa: E402

PAYLOAD = bytes(range(256)) * 40  # 10240 bytes


class _Handler(BaseHTTPRequestHandler):
    """Serves ``PAYLOAD``. Class attributes steer the behaviour per test."""

    honour_range = True
    truncate_at: int | None = None
    seen_ranges: list[str] = []

    def log_message(self, *args):  # noqa: D102 - silence the test output
        pass

    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        type(self).seen_ranges.append(self.headers.get("Range", ""))
        start = 0
        range_header = self.headers.get("Range", "")
        if self.honour_range and range_header.startswith("bytes="):
            start = int(range_header.removeprefix("bytes=").split("-")[0])
            body = PAYLOAD[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
        else:
            body = PAYLOAD
            self.send_response(200)

        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        if self.truncate_at is not None:
            # Declare the full length, then hang up early: what a dropping
            # mirror actually looks like on the wire.
            self.wfile.write(body[: self.truncate_at])
            self.wfile.flush()
            self.connection.close()
            return
        self.wfile.write(body)


@pytest.fixture
def server():
    _Handler.honour_range = True
    _Handler.truncate_at = None
    _Handler.seen_ranges = []

    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


class TestDownload:
    def test_downloads_a_complete_file(self, server, tmp_path):
        dest = tmp_path / "model.onnx"
        asr._download(f"{server}/model.onnx", dest)
        assert dest.read_bytes() == PAYLOAD
        assert not (tmp_path / "model.onnx.part").exists()

    def test_resumes_from_a_partial_file(self, server, tmp_path):
        dest = tmp_path / "model.onnx"
        partial = tmp_path / "model.onnx.part"
        partial.write_bytes(PAYLOAD[:4000])

        asr._download(f"{server}/model.onnx", dest)

        assert dest.read_bytes() == PAYLOAD
        assert any(r == "bytes=4000-" for r in _Handler.seen_ranges), _Handler.seen_ranges

    def test_a_server_that_ignores_range_still_verifies(self, server, tmp_path):
        """The size check must not add the resumed offset to a full-body reply."""
        _Handler.honour_range = False
        dest = tmp_path / "model.onnx"
        (tmp_path / "model.onnx.part").write_bytes(PAYLOAD[:4000])

        asr._download(f"{server}/model.onnx", dest)

        assert dest.read_bytes() == PAYLOAD

    def test_a_truncated_download_raises_and_keeps_the_partial(self, server, tmp_path):
        _Handler.truncate_at = 3000
        dest = tmp_path / "model.onnx"

        with pytest.raises(Exception):  # noqa: B017 - urllib raises several types
            asr._download(f"{server}/model.onnx", dest)

        partial = tmp_path / "model.onnx.part"
        assert partial.exists(), "半成品被删掉了，断点续传就没了"
        assert not dest.exists()

    def test_progress_is_not_printed_on_every_chunk(self, server, tmp_path, capsys, monkeypatch):
        """A bare \\r becomes thousands of lines when output is a file."""
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        asr._download(f"{server}/model.onnx", tmp_path / "model.onnx")
        assert capsys.readouterr().out.count("\n") < 25


class TestEnsureModel:
    def test_returns_immediately_when_the_files_are_present(self, tmp_path, monkeypatch):
        for name in asr.MODEL_FILES:
            (tmp_path / name).write_bytes(b"x")
        # Any download attempt would blow up, which is the point of the test.
        monkeypatch.setattr(asr, "_download", lambda *a, **k: pytest.fail("不该下载"))

        assert asr.ensure_model(tmp_path, attempts=1) == tmp_path

    def test_retries_the_same_mirror_before_moving_on(self, tmp_path, monkeypatch):
        calls: list[str] = []

        def fake_download(url, dest):
            calls.append(url)
            raise OSError("connection dropped")

        monkeypatch.setattr(asr, "_download", fake_download)
        monkeypatch.setattr(asr, "RETRY_DELAY_SECONDS", 0)
        monkeypatch.setattr(asr, "MODEL_FILES", ("model.onnx",))

        with pytest.raises(asr.AsrError):
            asr.ensure_model(tmp_path, attempts=3)

        # 2 mirrors x 3 attempts
        assert len(calls) == 6
        assert calls[0] == calls[1] == calls[2]
        assert calls[3] != calls[0]

    def test_a_failed_attempt_does_not_delete_the_partial(self, tmp_path, monkeypatch):
        """The actual regression: resume was impossible because .part was removed."""
        partial = tmp_path / "model.onnx.part"
        partial.write_bytes(b"half")

        def fake_download(url, dest):
            raise OSError("connection dropped")

        monkeypatch.setattr(asr, "_download", fake_download)
        monkeypatch.setattr(asr, "RETRY_DELAY_SECONDS", 0)
        monkeypatch.setattr(asr, "MODEL_FILES", ("model.onnx",))

        with pytest.raises(asr.AsrError):
            asr.ensure_model(tmp_path, attempts=2)

        assert partial.exists(), "半成品被删掉了"
        assert partial.read_bytes() == b"half"

    def test_the_error_tells_the_user_progress_is_kept(self, tmp_path, monkeypatch):
        monkeypatch.setattr(asr, "_download", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
        monkeypatch.setattr(asr, "RETRY_DELAY_SECONDS", 0)
        monkeypatch.setattr(asr, "MODEL_FILES", ("model.onnx",))

        with pytest.raises(asr.AsrError) as excinfo:
            asr.ensure_model(tmp_path, attempts=1)
        assert "断点" in str(excinfo.value)

    def test_a_file_that_finished_is_not_fetched_again(self, tmp_path, monkeypatch):
        """Recomputed between attempts: attempt two skips what attempt one got."""
        fetched: list[str] = []
        monkeypatch.setattr(asr, "MODEL_FILES", ("a.onnx", "b.txt"))
        monkeypatch.setattr(asr, "RETRY_DELAY_SECONDS", 0)

        state = {"first": True}

        def fake_download(url, dest):
            name = dest.name
            fetched.append(name)
            if name == "b.txt" and state["first"]:
                state["first"] = False
                raise OSError("dropped on the second file")
            dest.write_bytes(b"ok")

        monkeypatch.setattr(asr, "_download", fake_download)
        assert asr.ensure_model(tmp_path, attempts=2) == tmp_path
        assert fetched == ["a.onnx", "b.txt", "b.txt"]
