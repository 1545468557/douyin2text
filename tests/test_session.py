"""The cookie jar cache: it has to fail safe, never half-load."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from douyin2text.session import Session  # noqa: E402

COOKIES = {"ttwid": "t", "UIFID_TEMP": "u" * 60, "s_v_web_id": "verify_x"}


def make(tmp_path: Path, **overrides) -> Session:
    return Session(
        cookies=overrides.pop("cookies", dict(COOKIES)),
        user_agent=overrides.pop("user_agent", "UA/1.0"),
        minted_at=overrides.pop("minted_at", time.time()),
        **overrides,
    )


def test_round_trip(tmp_path):
    path = tmp_path / "session.json"
    make(tmp_path).save(path)
    loaded = Session.load(path)
    assert loaded is not None
    assert loaded.cookies == COOKIES
    assert loaded.user_agent == "UA/1.0"


def test_saved_file_is_owner_only(tmp_path):
    path = tmp_path / "session.json"
    make(tmp_path).save(path)
    assert path.stat().st_mode & 0o777 == 0o600


def test_missing_file_is_not_an_error(tmp_path):
    assert Session.load(tmp_path / "nope.json") is None


def test_a_jar_without_a_visitor_id_is_not_reused(tmp_path):
    """Without UIFID_TEMP nothing can be signed, so the cache must not be honoured."""
    path = tmp_path / "session.json"
    make(tmp_path, cookies={"ttwid": "t"}).save(path)
    assert Session.load(path) is None


def test_an_expired_jar_is_not_reused(tmp_path):
    path = tmp_path / "session.json"
    make(tmp_path, minted_at=time.time() - 10_000).save(path)
    assert Session.load(path, ttl=3600) is None


def test_corrupt_file_is_not_an_error(tmp_path):
    path = tmp_path / "session.json"
    path.write_text("{not json", encoding="utf-8")
    assert Session.load(path) is None


def test_partial_file_is_not_an_error(tmp_path):
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"cookies": {}}), encoding="utf-8")
    assert Session.load(path) is None


def test_cookie_header_skips_empty_values():
    session = Session(cookies={"a": "1", "b": "", "c": "3"}, user_agent="UA",
                      minted_at=time.time())
    assert session.cookie_header() == "a=1; c=3"
