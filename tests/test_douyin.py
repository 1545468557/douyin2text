"""Unit tests for link parsing and request signing. No network required."""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from douyin2text.douyin import (  # noqa: E402
    DouyinError,
    _split_pair,
    build_url,
    describe,
    extract_aweme_id,
    pick_play_url,
)
from douyin2text.session import Session  # noqa: E402


def make_session(**overrides) -> Session:
    cookies = {
        "ttwid": "1%7Cabc%7C1791739204",
        "UIFID_TEMP": "0173c8f974a1c07577a40e1ddb6347b1804450ad458bea7bd35bf3d7a8ab",
        "s_v_web_id": "verify_mv439o89_X1YD5KGH_UUPE_4z6c_8Upw_K2Lf10LMTF6K",
    }
    cookies.update(overrides.pop("cookies", {}))
    return Session(
        cookies=cookies,
        user_agent=overrides.pop(
            "user_agent",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        ),
        minted_at=overrides.pop("minted_at", 1_791_739_204.0),
    )


class TestExtractAwemeId:
    def test_desktop_video_url(self):
        assert extract_aweme_id("https://www.douyin.com/video/7185413072142568737") == (
            "7185413072142568737"
        )

    def test_note_url(self):
        assert extract_aweme_id("https://www.douyin.com/note/7384305715260689716") == (
            "7384305715260689716"
        )

    def test_bare_id(self):
        assert extract_aweme_id("7185413072142568737") == "7185413072142568737"

    def test_share_text_with_short_link_returns_link(self):
        """The app's own copy-link output, which is a sentence, not a URL."""
        text = (
            "7.32 复制打开抖音，看看【一心的作品】一招保存别人作品 "
            "https://v.douyin.com/iRNBho6G/ 复制此链接，打开Dou音搜索"
        )
        assert extract_aweme_id(text) == "https://v.douyin.com/iRNBho6G"

    def test_modal_id_url(self):
        assert extract_aweme_id(
            "https://www.douyin.com/discover?modal_id=7185413072142568737"
        ) == "7185413072142568737"

    def test_rejects_junk(self):
        with pytest.raises(DouyinError):
            extract_aweme_id("今天天气不错")


class TestSplitPair:
    def test_unquotes_both_sides(self):
        assert _split_pair("browser_name=Chrome") == ("browser_name", "Chrome")
        assert _split_pair("a_bogus=AA%2FB%3D") == ("a_bogus", "AA/B=")

    def test_value_containing_equals(self):
        assert _split_pair("k=a=b") == ("k", "a=b")


class TestBuildUrl:
    def test_carries_both_signatures_and_visitor_params(self):
        session = make_session()
        url, headers = build_url(session, "7185413072142568737")

        assert url.startswith("https://www.douyin.com/aweme/v1/web/aweme/detail/?")
        assert "a_bogus=" in url
        assert "x-secsdk-web-signature=" in url
        assert "uifid=" in url
        assert "timestamp=" in url
        assert "verifyFp=" in url
        assert "fp=" in url
        assert "aweme_id=7185413072142568737" in url
        assert headers["uifid"] == session.uifid
        assert headers["Cookie"].startswith("ttwid=")

    def test_signature_is_a_real_md5_of_the_documented_preimage(self):
        """Recompute the platform's own formula over what actually went out."""
        from douyin2text.douyin import WEBSIGN_SALT

        session = make_session()
        url, headers = build_url(session, "7185413072142568737")

        query, _, signature = url.partition("?")[2].rpartition("&x-secsdk-web-signature=")
        stamp = re.search(r"(?:^|&)timestamp=([^&]+)", query).group(1)

        expected = hashlib.md5(
            f"{session.uifid}_{stamp}_{WEBSIGN_SALT}_{query}".encode()
        ).hexdigest()
        assert signature == expected
        assert headers["x-secsdk-web-signature"] == expected
        assert headers["x-secsdk-web-expire"] == stamp

    def test_browser_version_follows_the_user_agent(self):
        session = make_session(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/127.0.0.0 Safari/537.36"
        )
        url, _ = build_url(session, "7185413072142568737")
        assert "browser_version=127.0.0.0" in url

    def test_refuses_a_jar_without_a_visitor_id(self):
        from douyin2text.douyin import SignatureRefused

        session = make_session(cookies={"UIFID_TEMP": ""})
        with pytest.raises(SignatureRefused):
            build_url(session, "7185413072142568737")


class TestPickPlayUrl:
    def test_prefers_play_addr(self):
        detail = {
            "video": {
                "play_addr": {"url_list": ["https://cdn/play.mp4"]},
                "download_addr": {"url_list": ["https://cdn/watermarked.mp4"]},
            }
        }
        assert pick_play_url(detail) == "https://cdn/play.mp4"

    def test_falls_back_to_bit_rate_renditions(self):
        detail = {
            "video": {
                "play_addr": {"url_list": []},
                "bit_rate": [{"play_addr": {"url_list": ["https://cdn/br.mp4"]}}],
            }
        }
        assert pick_play_url(detail) == "https://cdn/br.mp4"

    def test_rewrites_playwm_to_play(self):
        detail = {"video": {"play_addr": {"url_list": ["https://cdn/playwm/x.mp4"]}}}
        assert pick_play_url(detail) == "https://cdn/play/x.mp4"

    def test_image_post_raises_readable_error(self):
        with pytest.raises(DouyinError, match="没有可用的视频地址"):
            pick_play_url({"video": {}})


class TestDescribe:
    def test_flattens_the_fields_we_print(self):
        detail = {
            "aweme_id": "1",
            "desc": " 标题 ",
            "author": {"nickname": "一心", "unique_id": "1xin11888"},
            "create_time": 1672984363,
            "video": {"duration": 57050},
            "statistics": {"digg_count": 5, "comment_count": 1},
        }
        meta = describe(detail)
        assert meta["desc"] == "标题"
        assert meta["author"] == "一心"
        assert meta["duration_ms"] == 57050
        assert meta["digg_count"] == 5
        assert meta["comment_count"] == 1


class TestSafeStem:
    """The filename comes from a caption written for the feed, not the filesystem."""

    def test_drops_hashtags(self):
        from douyin2text.cli import _safe_stem

        assert _safe_stem("教你保存视频 #教程 #抖音", "fb") == "教你保存视频"

    def test_replaces_path_separators(self):
        from douyin2text.cli import _safe_stem

        assert "/" not in _safe_stem("a/b:c*d?e", "fb")

    def test_truncates_long_captions(self):
        from douyin2text.cli import _safe_stem

        assert len(_safe_stem("啊" * 200, "fb")) <= 40

    def test_falls_back_when_nothing_is_left(self):
        from douyin2text.cli import _safe_stem

        assert _safe_stem("###", "douyin_123") == "douyin_123"
        assert _safe_stem("", "douyin_123") == "douyin_123"

    def test_strips_trailing_punctuation(self):
        from douyin2text.cli import _safe_stem

        assert _safe_stem("标题，", "fb") == "标题"
