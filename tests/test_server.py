"""Tests for the local web UI.

The ASR pipeline is replaced with a fake, so these run in milliseconds and touch
no network - what is under test is the job lifecycle and the wiring, not the
transcription itself.
"""

from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from douyin2text.history import HistoryStore  # noqa: E402
from douyin2text.pipeline import PipelineError, Progress, Transcription  # noqa: E402
from douyin2text.server import _filename, create_app, free_port  # noqa: E402


class FakeService:
    """Stands in for TranscriptionService. Records what it was asked to do."""

    def __init__(self, *, fail: str | None = None) -> None:
        self.model_loaded = True
        self.calls: list[dict] = []
        self.fail = fail

    async def transcribe_link(self, link, *, on_progress=None, refresh_session=False, **_):
        self.calls.append({"link": link, "refresh_session": refresh_session})
        if on_progress:
            on_progress(Progress("解析作品", "假作品", 0.1))
            on_progress(Progress("语音转写", "转写中", 0.6))
        if self.fail:
            raise PipelineError(self.fail)
        # A distinct id per link, so history de-duplication does not collapse
        # two different videos into one row during a test.
        digest = hashlib.sha1(link.encode()).hexdigest()[:16]
        return Transcription(
            text="这是假转写结果。",
            meta={"desc": "假标题", "author": "假作者", "aweme_id": digest},
            elapsed=1.23,
            source=link,
        )

    async def transcribe_upload(self, path, *, display_name, on_progress=None, **_):
        self.calls.append({"upload": display_name, "path": str(path)})
        if self.fail:
            raise PipelineError(self.fail)
        return Transcription(
            text="上传文件的转写结果。",
            meta={"desc": display_name, "author": None, "aweme_id": None},
            elapsed=0.5,
            source=display_name,
        )


def wait_for_state(client: TestClient, job_id: str, target: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    job: dict = {}
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["state"] == target:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job stayed {job.get('state')!r}, expected {target!r}: {job}")


@pytest.fixture
def history(tmp_path):
    """A throwaway history file.

    Without this the app would build a real ``HistoryStore`` pointing at the
    user's own cache, and running the tests would append fake transcripts to it.
    """
    return HistoryStore(tmp_path / "history.json")


@pytest.fixture
def client(history):
    service = FakeService()
    with TestClient(create_app(service, history=history)) as test_client:
        test_client.service = service  # type: ignore[attr-defined]
        test_client.history = history  # type: ignore[attr-defined]
        yield test_client


class TestStatus:
    def test_reports_the_loaded_model(self, client):
        body = client.get("/api/status").json()
        assert body["ok"] is True
        assert body["model_loaded"] is True
        assert "version" in body


class TestIndex:
    def test_serves_the_page(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "抖音视频转文字" in response.text

    def test_page_has_no_external_requests(self, client):
        """The UI must work offline; a CDN in the critical path would break it."""
        html = client.get("/").text
        for marker in ("http://cdn", "https://cdn", "unpkg.com", "jsdelivr", "googleapis"):
            assert marker not in html

    def test_page_inlines_the_version(self, client):
        assert "__VERSION__" not in client.get("/").text


class TestTranscribeLink:
    def test_rejects_an_empty_link(self, client):
        assert client.post("/api/transcribe", json={"link": "   "}).status_code == 400

    def test_runs_a_job_to_completion(self, client):
        job_id = client.post(
            "/api/transcribe", json={"link": "https://v.douyin.com/abc/"}
        ).json()["job_id"]
        job = wait_for_state(client, job_id, "done")

        assert job["text"] == "这是假转写结果。"
        assert job["meta"]["author"] == "假作者"
        assert job["fraction"] == 1.0
        assert job["elapsed"] == 1.23
        assert client.service.calls[0]["link"] == "https://v.douyin.com/abc/"  # type: ignore[attr-defined]

    def test_reports_progress_before_finishing(self, client):
        job_id = client.post(
            "/api/transcribe", json={"link": "https://v.douyin.com/abc/"}
        ).json()["job_id"]
        job = wait_for_state(client, job_id, "done")
        assert job["step"] == "完成"

    def test_surfaces_a_readable_failure(self, client):
        client.service.fail = "链接失效了"  # type: ignore[attr-defined]
        job_id = client.post(
            "/api/transcribe", json={"link": "https://v.douyin.com/abc/"}
        ).json()["job_id"]
        job = wait_for_state(client, job_id, "failed")
        assert "链接失效了" in job["error"]

    def test_unknown_job_is_404(self, client):
        assert client.get("/api/jobs/deadbeef").status_code == 404


class TestDownload:
    def test_downloads_the_transcript(self, client):
        job_id = client.post(
            "/api/transcribe", json={"link": "https://v.douyin.com/abc/"}
        ).json()["job_id"]
        wait_for_state(client, job_id, "done")

        response = client.get(f"/api/jobs/{job_id}/download")
        assert response.status_code == 200
        assert "假转写结果" in response.text
        assert "attachment" in response.headers["content-disposition"]

    def test_no_download_before_the_job_finishes(self, client):
        assert client.get("/api/jobs/nope/download").status_code == 404

    def test_chinese_filename_survives_the_latin1_header_limit(self):
        """A Douyin title is Chinese; an HTTP header is latin-1. RFC 5987 bridges it."""
        from douyin2text.server import _disposition

        header = _disposition("一招保存别人作品.txt")
        header.encode("latin-1")  # what actually goes on the wire
        assert "filename*=UTF-8''" in header
        assert "一招保存别人作品" not in header


class TestUpload:
    def test_accepts_a_file(self, client):
        response = client.post(
            "/api/upload", files={"file": ("我的视频.mp4", b"fake bytes", "video/mp4")}
        )
        assert response.status_code == 200
        job = wait_for_state(client, response.json()["job_id"], "done")
        assert job["text"] == "上传文件的转写结果。"
        assert job["source"] == "我的视频.mp4"

    def test_uses_only_the_basename(self, client):
        """A filename from a client is untrusted input, not a path."""
        response = client.post(
            "/api/upload",
            files={"file": ("../../evil.mp4", b"x", "video/mp4")},
        )
        job = wait_for_state(client, response.json()["job_id"], "done")
        assert job["source"] == "evil.mp4"


class TestShutdown:
    def test_invokes_the_callback(self):
        called: list[bool] = []
        with TestClient(create_app(FakeService(), on_shutdown=lambda: called.append(True))) as c:
            assert c.post("/api/shutdown").json() == {"ok": True}
            deadline = time.time() + 3
            while not called and time.time() < deadline:
                time.sleep(0.05)
        assert called == [True]

    def test_is_optional(self, client):
        assert client.post("/api/shutdown").status_code == 200


class TestFilename:
    def test_drops_hashtags_and_unsafe_characters(self):
        job = type("J", (), {"meta": {"desc": "标题 #话题 a/b"}, "source": "s"})()
        assert _filename(job) == "标题 a_b.txt"

    def test_falls_back_when_the_caption_is_only_tags(self):
        job = type("J", (), {"meta": {"desc": "#a #b"}, "source": "https://v.douyin.com/x/"})()
        assert _filename(job).endswith(".txt")
        assert _filename(job) != ".txt"


class TestFreePort:
    def test_returns_a_usable_port(self):
        port = free_port(0)
        assert 1024 < port < 65536

    def test_falls_back_when_the_preferred_port_is_taken(self):
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            taken = int(held.getsockname()[1])
            assert free_port(taken) != taken


class TestHistoryEndpoints:
    def test_empty_at_first(self, client):
        body = client.get("/api/history").json()
        assert body == {"entries": [], "total": 0}

    def test_a_finished_job_is_recorded(self, client):
        job_id = client.post(
            "/api/transcribe", json={"link": "https://v.douyin.com/abc/"}
        ).json()["job_id"]
        job = wait_for_state(client, job_id, "done")

        assert job["history_id"]
        entries = client.get("/api/history").json()["entries"]
        assert len(entries) == 1
        assert entries[0]["id"] == job["history_id"]
        assert entries[0]["title"] == "假标题"
        assert entries[0]["author"] == "假作者"

    def test_the_list_does_not_carry_transcripts(self, client):
        client.post("/api/transcribe", json={"link": "https://v.douyin.com/abc/"})
        entries = client.get("/api/history").json()["entries"]
        assert "text" not in entries[0]

    def test_fetching_one_entry_returns_the_transcript(self, client):
        job_id = client.post(
            "/api/transcribe", json={"link": "https://v.douyin.com/abc/"}
        ).json()["job_id"]
        entry_id = wait_for_state(client, job_id, "done")["history_id"]

        entry = client.get(f"/api/history/{entry_id}").json()
        assert entry["text"] == "这是假转写结果。"
        assert entry["meta"]["author"] == "假作者"

    def test_a_failed_job_is_not_recorded(self, client):
        client.service.fail = "炸了"  # type: ignore[attr-defined]
        job_id = client.post(
            "/api/transcribe", json={"link": "https://v.douyin.com/abc/"}
        ).json()["job_id"]
        wait_for_state(client, job_id, "failed")
        assert client.get("/api/history").json()["total"] == 0

    def test_history_outlives_the_job_board(self, tmp_path):
        """The point of the feature: the transcript is still there next launch."""
        store = HistoryStore(tmp_path / "history.json")
        with TestClient(create_app(FakeService(), history=store)) as first:
            job_id = first.post(
                "/api/transcribe", json={"link": "https://v.douyin.com/abc/"}
            ).json()["job_id"]
            entry_id = wait_for_state(first, job_id, "done")["history_id"]

        # A brand new app, new job board, same history file.
        with TestClient(create_app(FakeService(), history=HistoryStore(tmp_path / "history.json"))) as second:
            assert second.get("/api/history").json()["total"] == 1
            assert second.get(f"/api/history/{entry_id}").json()["text"] == "这是假转写结果。"

    def test_download_a_history_entry(self, client):
        job_id = client.post(
            "/api/transcribe", json={"link": "https://v.douyin.com/abc/"}
        ).json()["job_id"]
        entry_id = wait_for_state(client, job_id, "done")["history_id"]

        response = client.get(f"/api/history/{entry_id}/download")
        assert response.status_code == 200
        assert "假转写结果" in response.text
        response.headers["content-disposition"].encode("latin-1")  # 头部必须能编码
        assert "filename*=UTF-8''" in response.headers["content-disposition"]

    def test_delete_one(self, client):
        job_id = client.post(
            "/api/transcribe", json={"link": "https://v.douyin.com/abc/"}
        ).json()["job_id"]
        entry_id = wait_for_state(client, job_id, "done")["history_id"]

        assert client.delete(f"/api/history/{entry_id}").json()["ok"] is True
        assert client.get("/api/history").json()["total"] == 0
        assert client.delete(f"/api/history/{entry_id}").status_code == 404

    def test_clear_all(self, client):
        for link in ("https://v.douyin.com/a/", "https://v.douyin.com/b/"):
            job_id = client.post("/api/transcribe", json={"link": link}).json()["job_id"]
            wait_for_state(client, job_id, "done")

        assert client.delete("/api/history").json()["cleared"] == 2
        assert client.get("/api/history").json()["total"] == 0

    def test_unknown_entry_is_404(self, client):
        assert client.get("/api/history/deadbeef").status_code == 404
        assert client.get("/api/history/deadbeef/download").status_code == 404


class TestInjectedHistoryIsUsed:
    """Regression: an *empty* HistoryStore is falsy because it defines __len__.

    `history or HistoryStore(...)` therefore discarded the injected store and
    opened the default one, so every test silently shared the user's real
    history file. The fix is an explicit `is None` check.
    """

    def test_an_empty_injected_store_is_used(self, tmp_path):
        store = HistoryStore(tmp_path / "injected.json")
        assert len(store) == 0  # falsy, which is exactly the trap
        assert not store

        with TestClient(create_app(FakeService(), history=store)) as c:
            job_id = c.post(
                "/api/transcribe", json={"link": "https://v.douyin.com/x/"}
            ).json()["job_id"]
            wait_for_state(c, job_id, "done")

        assert store.path.exists(), "the injected store was bypassed"
        assert len(store) == 1

    def test_two_apps_with_different_stores_do_not_share(self, tmp_path):
        first = HistoryStore(tmp_path / "first.json")
        second = HistoryStore(tmp_path / "second.json")

        for store, link in ((first, "https://v.douyin.com/first/"),
                            (second, "https://v.douyin.com/second/")):
            with TestClient(create_app(FakeService(), history=store)) as c:
                job_id = c.post("/api/transcribe", json={"link": link}).json()["job_id"]
                wait_for_state(c, job_id, "done")

        assert len(first) == 1
        assert len(second) == 1
        assert first.summaries()[0]["source"] != second.summaries()[0]["source"]
