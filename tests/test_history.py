"""The history file.

The important properties: it survives a restart, a corrupt file never takes the
app down with it, and the list view never ships two hundred transcripts to the
browser just to draw a list.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from douyin2text.history import HistoryEntry, HistoryStore  # noqa: E402


def entry(**overrides) -> HistoryEntry:
    base = {
        "id": "abc123",
        "kind": "link",
        "source": "https://v.douyin.com/x/",
        "title": "标题",
        "text": "转写内容",
        "author": "作者",
        "aweme_id": "7185413072142568737",
        "meta": {"desc": "标题", "author": "作者", "digg_count": 5},
        "elapsed": 1.5,
        "created_at": 1_700_000_000.0,
    }
    base.update(overrides)
    return HistoryEntry(**base)


class TestPersistence:
    def test_missing_file_is_an_empty_history(self, tmp_path):
        assert len(HistoryStore(tmp_path / "nope.json")) == 0
        assert HistoryStore(tmp_path / "nope.json").summaries() == []

    def test_round_trip_across_instances(self, tmp_path):
        path = tmp_path / "history.json"
        HistoryStore(path).add(entry())

        reloaded = HistoryStore(path)
        assert len(reloaded) == 1
        assert reloaded.get("abc123").text == "转写内容"
        assert reloaded.get("abc123").author == "作者"

    def test_corrupt_file_does_not_raise(self, tmp_path):
        path = tmp_path / "history.json"
        path.write_text("{not json", encoding="utf-8")
        assert len(HistoryStore(path)) == 0

    def test_wrong_shape_does_not_raise(self, tmp_path):
        path = tmp_path / "history.json"
        path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        assert len(HistoryStore(path)) == 0

    def test_a_bad_row_is_skipped_not_fatal(self, tmp_path):
        path = tmp_path / "history.json"
        path.write_text(
            json.dumps({"entries": [{"no_id": True}, entry().as_dict()]}), encoding="utf-8"
        )
        store = HistoryStore(path)
        assert len(store) == 1
        assert store.get("abc123") is not None

    def test_saved_file_is_utf8_and_not_escaped(self, tmp_path):
        """Chinese must be readable in the file, not \\uXXXX soup."""
        path = tmp_path / "history.json"
        HistoryStore(path).add(entry())
        assert "转写内容" in path.read_text(encoding="utf-8")

    def test_no_temporary_file_is_left_behind(self, tmp_path):
        path = tmp_path / "history.json"
        HistoryStore(path).add(entry())
        assert list(tmp_path.glob("*.tmp")) == []


class TestOrdering:
    def test_newest_first(self, tmp_path):
        store = HistoryStore(tmp_path / "history.json")
        store.add(entry(id="old", aweme_id="1", created_at=100.0))
        store.add(entry(id="new", aweme_id="2", created_at=200.0))
        assert [e["id"] for e in store.summaries()] == ["new", "old"]

    def test_entries_are_sorted_on_load_too(self, tmp_path):
        path = tmp_path / "history.json"
        path.write_text(
            json.dumps(
                {"entries": [entry(id="a", created_at=1.0).as_dict(),
                             entry(id="b", created_at=9.0).as_dict()]}
            ),
            encoding="utf-8",
        )
        assert [e["id"] for e in HistoryStore(path).summaries()] == ["b", "a"]


class TestDeduplication:
    def test_same_video_moves_to_the_top_instead_of_duplicating(self, tmp_path):
        """Re-transcribing a video is one row, not two."""
        store = HistoryStore(tmp_path / "history.json")
        store.add(entry(id="first", text="旧结果", created_at=100.0))
        store.add(entry(id="second", text="新结果", created_at=200.0))

        assert len(store) == 1
        assert store.get("second").text == "新结果"
        assert store.get("first") is None

    def test_a_different_video_is_a_separate_row(self, tmp_path):
        store = HistoryStore(tmp_path / "history.json")
        store.add(entry(id="a", aweme_id="111"))
        store.add(entry(id="b", aweme_id="222"))
        assert len(store) == 2

    def test_uploads_never_deduplicate(self, tmp_path):
        """A file has no stable identity, so two uploads are two entries."""
        store = HistoryStore(tmp_path / "history.json")
        store.add(entry(id="a", kind="file", aweme_id=None, source="我的视频.mp4"))
        store.add(entry(id="b", kind="file", aweme_id=None, source="我的视频.mp4"))
        assert len(store) == 2


class TestLimit:
    def test_oldest_entries_are_dropped(self, tmp_path):
        store = HistoryStore(tmp_path / "history.json", limit=3)
        for i in range(5):
            store.add(entry(id=f"e{i}", aweme_id=str(i), created_at=float(i)))
        assert len(store) == 3
        assert {e["id"] for e in store.summaries()} == {"e2", "e3", "e4"}

    def test_the_limit_survives_a_reload(self, tmp_path):
        path = tmp_path / "history.json"
        store = HistoryStore(path, limit=2)
        for i in range(4):
            store.add(entry(id=f"e{i}", aweme_id=str(i), created_at=float(i)))
        assert len(HistoryStore(path, limit=2)) == 2


class TestMutations:
    def test_delete(self, tmp_path):
        store = HistoryStore(tmp_path / "history.json")
        store.add(entry())
        assert store.delete("abc123") is True
        assert store.delete("abc123") is False
        assert len(HistoryStore(tmp_path / "history.json")) == 0

    def test_clear_reports_how_many_went(self, tmp_path):
        path = tmp_path / "history.json"
        store = HistoryStore(path)
        store.add(entry(id="a", aweme_id="1"))
        store.add(entry(id="b", aweme_id="2"))
        assert store.clear() == 2
        assert len(HistoryStore(path)) == 0


class TestSummary:
    def test_summary_carries_no_transcript(self, tmp_path):
        """The list is fetched on every load; it must not carry the texts."""
        store = HistoryStore(tmp_path / "history.json")
        store.add(entry())
        summary = store.summaries()[0]
        assert "text" not in summary
        assert summary["chars"] == len("转写内容")
        assert summary["title"] == "标题"
        assert summary["digg_count"] == 5

    def test_full_entry_does_carry_the_transcript(self, tmp_path):
        store = HistoryStore(tmp_path / "history.json")
        store.add(entry())
        assert store.get("abc123").as_dict()["text"] == "转写内容"


class TestFromJob:
    def test_builds_from_a_finished_job(self):
        class FakeJob:
            kind = "link"
            source = "https://v.douyin.com/x/"
            text = "内容"
            meta = {"desc": "标题", "author": "作者", "aweme_id": "123"}
            elapsed = 2.0

        built = HistoryEntry.from_job(FakeJob())
        assert built.title == "标题"
        assert built.author == "作者"
        assert built.aweme_id == "123"
        assert built.text == "内容"
        assert built.id

    def test_falls_back_to_the_source_when_there_is_no_title(self):
        class FakeJob:
            kind = "file"
            source = "我的视频.mp4"
            text = "内容"
            meta = {}
            elapsed = 1.0

        assert HistoryEntry.from_job(FakeJob()).title == "我的视频.mp4"
