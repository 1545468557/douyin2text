# SPDX-License-Identifier: MIT
"""Persistent transcript history.

A small JSON file rather than a database: the workload is one row per video, read
whole on start and written whole on change, and adding SQLite to a local
single-user tool would buy nothing. The file is rewritten atomically so a crash
mid-save cannot leave a half-written history behind.

Entries are capped and the oldest are dropped first. Transcripts are small text
but they are unbounded in principle, and a history that grows without limit is a
history that eventually fails to load.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: How many transcripts to keep. At a few KB each this is a few MB at most.
DEFAULT_LIMIT: int = 200

#: Bumped if the on-disk shape ever changes in a way that needs migrating.
SCHEMA_VERSION: int = 1


@dataclass(slots=True)
class HistoryEntry:
    """One saved transcript."""

    id: str
    kind: str
    source: str
    title: str
    text: str
    author: str | None = None
    aweme_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    elapsed: float = 0.0
    created_at: float = field(default_factory=time.time)

    def summary(self) -> dict[str, Any]:
        """The list-view shape: everything except the transcript itself.

        The list is fetched on every page load, so it must not carry the text of
        two hundred transcripts.
        """
        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "title": self.title,
            "author": self.author,
            "aweme_id": self.aweme_id,
            "elapsed": round(self.elapsed, 2),
            "created_at": self.created_at,
            "chars": len(self.text),
            "digg_count": self.meta.get("digg_count"),
            "duration_ms": self.meta.get("duration_ms"),
        }

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_job(cls, job: Any) -> HistoryEntry:
        """Build an entry from a finished server-side job."""
        meta = dict(job.meta or {})
        return cls(
            id=uuid.uuid4().hex[:12],
            kind=job.kind,
            source=job.source,
            title=(meta.get("desc") or job.source or "未命名").strip(),
            text=job.text,
            author=meta.get("author"),
            aweme_id=meta.get("aweme_id"),
            meta=meta,
            elapsed=job.elapsed,
        )


class HistoryStore:
    """Reads and writes the history file. One instance per process."""

    def __init__(self, path: Path, *, limit: int = DEFAULT_LIMIT) -> None:
        self.path = path
        self.limit = limit
        self._entries: list[HistoryEntry] = self._read()

    # -- persistence ------------------------------------------------------

    def _read(self) -> list[HistoryEntry]:
        """Load the file, treating anything unreadable as an empty history.

        A corrupt or unreadable history is a nuisance, not a failure: the user
        came here to transcribe something, and refusing to start because a cache
        file is bad would be the wrong trade.
        """
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict):
            return []

        entries: list[HistoryEntry] = []
        for raw in payload.get("entries") or []:
            if not isinstance(raw, dict):
                continue
            try:
                entries.append(
                    HistoryEntry(
                        id=str(raw["id"]),
                        kind=str(raw.get("kind") or "link"),
                        source=str(raw.get("source") or ""),
                        title=str(raw.get("title") or "未命名"),
                        text=str(raw.get("text") or ""),
                        author=raw.get("author"),
                        aweme_id=raw.get("aweme_id"),
                        meta=raw.get("meta") if isinstance(raw.get("meta"), dict) else {},
                        elapsed=float(raw.get("elapsed") or 0.0),
                        created_at=float(raw.get("created_at") or 0.0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                # Skip the bad row rather than losing the whole file.
                continue
        entries.sort(key=lambda entry: entry.created_at, reverse=True)
        return entries[: self.limit]

    def _write(self) -> None:
        """Replace the file atomically, so a crash cannot truncate it."""
        payload = {
            "version": SCHEMA_VERSION,
            "entries": [entry.as_dict() for entry in self._entries],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.path)

    # -- queries ----------------------------------------------------------

    def summaries(self) -> list[dict[str, Any]]:
        return [entry.summary() for entry in self._entries]

    def get(self, entry_id: str) -> HistoryEntry | None:
        return next((entry for entry in self._entries if entry.id == entry_id), None)

    def __len__(self) -> int:
        return len(self._entries)

    # -- mutations --------------------------------------------------------

    def add(self, entry: HistoryEntry) -> HistoryEntry:
        """Store an entry, replacing an earlier transcript of the same work.

        Re-running a video should move it back to the top, not produce a second
        row: the history is a list of things you transcribed, not of times you
        pressed the button.
        """
        if entry.aweme_id:
            self._entries = [
                existing
                for existing in self._entries
                if existing.aweme_id != entry.aweme_id
            ]
        self._entries.insert(0, entry)
        del self._entries[self.limit :]
        self._write()
        return entry

    def delete(self, entry_id: str) -> bool:
        remaining = [entry for entry in self._entries if entry.id != entry_id]
        if len(remaining) == len(self._entries):
            return False
        self._entries = remaining
        self._write()
        return True

    def clear(self) -> int:
        count = len(self._entries)
        self._entries = []
        self._write()
        return count


__all__ = [
    "DEFAULT_LIMIT",
    "SCHEMA_VERSION",
    "HistoryEntry",
    "HistoryStore",
]
