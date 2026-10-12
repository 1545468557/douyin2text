# SPDX-License-Identifier: MIT
"""Resolve a Douyin link to a watermark-free video URL.

Two signatures go on the request:

* ``a_bogus`` over the query string - the pure-Python port vendored in
  :mod:`douyin2text._vendor.abogus`.
* ``x-secsdk-web-signature`` - Douyin's own SDK signature, which the platform
  requires on the fourteen paths it sign-protects (``/aweme/v1/web/aweme/detail/``
  among them). It is an MD5 over the query plus the jar's visitor id, so it also
  needs the browser-minted cookies from :mod:`douyin2text.session`.

TLS is impersonated by ``wreq`` so the handshake matches the Chrome version the
signature and User-Agent claim.

Parameter names and default values, the signature algorithms and the protected
path list are ported from
https://github.com/Evil0ctal/Douyin_TikTok_Download_API (Apache-2.0) -
``src/dtk/platforms/douyin/{params,endpoints}.py`` and
``src/dtk/signing/native/{signer,websign}.py``.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from datetime import timedelta
from typing import Any, Final
from urllib.parse import quote, unquote, urlencode

import wreq
from wreq import Emulation

from ._vendor.abogus import DEFAULT_BROWSER_INFO, ABogus
from ._vendor.tokens import gen_false_ms_token
from .session import Session

#: Web client build identifiers. Douyin rejects requests whose ``version_code``
#: is far behind the live web build, so these need refreshing when the platform
#: ships a major web release.
AID: Final = "6383"
CHANNEL: Final = "channel_pc_web"
VERSION_CODE: Final = "290100"
VERSION_NAME: Final = "29.1.0"
UPDATE_VERSION_CODE: Final = "170400"

#: The Chrome major this client claims. ``wreq`` picks the nearest TLS profile
#: at or below it, so the handshake and the User-Agent never disagree.
CHROME_MAJOR: Final = 130

#: Salt for ``x-secsdk-web-signature``. String #39 of the VM's table in
#: ``runtime_bundler_34.js`` from ``lf-security.bytegoofy.com``; per project, and
#: this one is douyin_web. Ported verbatim.
WEBSIGN_SALT: Final = "A96D855A08C0A9707F8BEF0D9A527E4E"

DETAIL_URL: Final = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

#: Recognised inputs, most specific first. A pattern tagged ``id`` captures the
#: numeric work id in group 1; one tagged ``link`` captures a URL that still has
#: to be followed to find out what it points at.
_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    (r"https?://v\.douyin\.com/[\w-]+", "link"),
    (r"https?://www\.iesdouyin\.com/share/video/(\d+)", "id"),
    (r"https?://www\.douyin\.com/video/(\d+)", "id"),
    (r"https?://www\.douyin\.com/note/(\d+)", "id"),
    (r"https?://www\.douyin\.com/discover\?modal_id=(\d+)", "id"),
    (r"\b(\d{19})\b", "id"),
)


class DouyinError(RuntimeError):
    """A lookup failed for a reason worth showing the user."""


class SignatureRefused(DouyinError):
    """The platform rejected the signature - the jar is probably stale."""


def extract_aweme_id(text: str) -> str:
    """Pull a video id, or a link to resolve, out of share text.

    Accepts what the Douyin app's "copy link" produces - a sentence with a
    ``v.douyin.com`` short link embedded in it - as well as a desktop URL or a
    bare 19-digit id.
    """
    text = text.strip()
    if text.isdigit() and len(text) >= 15:
        return text
    for pattern, kind in _PATTERNS:
        match = re.search(pattern, text)
        if match:
            return match.group(1) if kind == "id" else match.group(0)
    raise DouyinError(f"没能从这段文本里找到抖音链接或视频 ID：{text[:80]}")


def _pick_emulation(major: int) -> Any:
    """The newest ``ChromeNN`` TLS profile at or below ``major``."""
    versions: list[tuple[int, Any]] = []
    for name in dir(Emulation):
        match = re.fullmatch(r"Chrome(\d+)", name)
        if match:
            versions.append((int(match.group(1)), getattr(Emulation, name)))
    if not versions:
        raise DouyinError("wreq 没有提供任何 Chrome TLS 指纹")
    versions.sort()
    candidates = [v for v in versions if v[0] <= major]
    return candidates[-1][1] if candidates else versions[0][1]


def _base_params(user_agent: str) -> dict[str, str]:
    """The parameters every Douyin web API call carries.

    Values echoed back to the platform must agree with the User-Agent, so the
    browser version is read out of it rather than hardcoded.
    """
    match = re.search(r"Chrome/(\d+)", user_agent)
    version = f"{match.group(1)}.0.0.0" if match else "130.0.0.0"
    return {
        "device_platform": "webapp",
        "aid": AID,
        "channel": CHANNEL,
        "pc_client_type": "1",
        "version_code": VERSION_CODE,
        "version_name": VERSION_NAME,
        "cookie_enabled": "true",
        "screen_width": "1920",
        "screen_height": "1080",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": version,
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": version,
        "os_name": "Windows",
        "os_version": "10",
        "cpu_core_num": "12",
        "device_memory": "8",
        "platform": "PC",
        "downlink": "10",
        "effective_type": "4g",
        "round_trip_time": "0",
        "update_version_code": UPDATE_VERSION_CODE,
    }


def _client() -> Any:
    """A TLS-impersonating client. Fresh per call: one jar, one connection pool.

    ``redirect`` is set explicitly because ``wreq`` does *not* follow redirects
    by default - and following them is the whole job of resolving a
    ``v.douyin.com`` share link.
    """
    return wreq.Client(
        emulation=_pick_emulation(CHROME_MAJOR),
        redirect=wreq.redirect.Policy.limited(10),
        cookie_store=False,
        connect_timeout=timedelta(seconds=10),
        pool_idle_timeout=timedelta(seconds=90),
        gzip=True,
        brotli=True,
        deflate=True,
        zstd=True,
        no_proxy=True,
    )


def _split_pair(part: str) -> tuple[str, str]:
    """One ``k=v`` back into a pair, unquoted so it can be re-serialized.

    The query arrives percent-encoded by the a_bogus layer, and the websign layer
    hashes a fresh serialization of the whole thing - so every value is decoded
    here and encoded once, consistently, or the bytes hashed and the bytes sent
    differ.
    """
    name, _, value = part.partition("=")
    return unquote(name), unquote(value)


def build_url(session: Session, aweme_id: str) -> tuple[str, dict[str, str]]:
    """The fully signed detail URL and the headers that go with it.

    Mirrors the platform's own parameter order, captured from a live page:
    ``...&a_bogus=&verifyFp=&fp=&uifid=&timestamp=&x-secsdk-web-signature=``.
    """
    uifid = session.uifid
    if not uifid:
        raise SignatureRefused("访客 cookie 里没有 uifid，无法生成 x-secsdk-web-signature")

    user_agent = session.user_agent
    verify_fp = session.cookies.get("s_v_web_id", "")
    ms_token = session.cookies.get("msToken") or gen_false_ms_token()

    params = {
        **_base_params(user_agent),
        "aweme_id": aweme_id,
        "msToken": ms_token,
    }
    query = urlencode(params)
    bogus = ABogus(
        user_agent, browser_info=DEFAULT_BROWSER_INFO, rng=random.Random()
    ).get_value(query)

    pairs = [
        _split_pair(part)
        for part in f"{query}&a_bogus={quote(bogus, safe='')}".split("&")
        if part
    ]
    if verify_fp:
        pairs.append(("verifyFp", verify_fp))
        pairs.append(("fp", verify_fp))

    stamp = str(int(time.time()))
    covered = [*pairs, ("uifid", uifid), ("timestamp", stamp)]
    signed_query = "&".join(
        f"{quote(key, safe='*-._')}={quote(value, safe='*-._')}" for key, value in covered
    )
    signature = hashlib.md5(
        f"{uifid}_{stamp}_{WEBSIGN_SALT}_{signed_query}".encode()
    ).hexdigest()

    url = f"{DETAIL_URL}?{signed_query}&x-secsdk-web-signature={signature}"
    headers = {
        "User-Agent": user_agent,
        "Referer": "https://www.douyin.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "uifid": uifid,
        "x-secsdk-web-signature": signature,
        "x-secsdk-web-expire": stamp,
        "Cookie": session.cookie_header(),
    }
    return url, headers


async def _resolve_short_link(url: str, session: Session) -> str:
    """Follow a ``v.douyin.com`` redirect to the canonical video URL."""
    async with _client() as client:
        response = await client.get(
            url,
            headers={"User-Agent": session.user_agent, "Cookie": session.cookie_header()},
        )
    final = str(response.url)
    if "/video/" not in final and "/note/" not in final:
        raise DouyinError(f"短链没有跳转到视频页：{final}")
    return final


async def fetch_aweme(share_text: str, session: Session) -> dict[str, Any]:
    """Resolve a link or share text to the platform's ``aweme_detail``."""
    target = extract_aweme_id(share_text)
    if target.startswith("http") and "/video/" not in target and "/note/" not in target:
        target = await _resolve_short_link(target, session)

    match = re.search(r"/(?:video|note)/(\d+)", target)
    aweme_id = match.group(1) if match else target

    url, headers = build_url(session, aweme_id)
    async with _client() as client:
        response = await client.get(url, headers=headers)
        status = response.status.as_int()
        body = await response.text()

    if status == 403:
        raise SignatureRefused(
            f"抖音拒绝了签名（HTTP 403）：{body[:200]}\n"
            "访客 cookie 可能已失效，用 --refresh-session 重新获取。"
        )
    if status != 200:
        raise DouyinError(f"抖音返回 HTTP {status}：{body[:200]}")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DouyinError(f"抖音返回的不是 JSON：{body[:200]}") from exc

    detail = payload.get("aweme_detail")
    if not detail:
        raise DouyinError(
            f"抖音没有返回作品详情（status_code={payload.get('status_code')}）。"
            "常见原因：链接失效、作品已删除，或该作品仅在 App 内可见。"
        )
    return detail


def pick_play_url(detail: dict[str, Any]) -> str:
    """The best clean video URL in an ``aweme_detail`` payload.

    ``play_addr`` is the watermark-free stream the web player itself uses, so it
    is preferred over ``download_addr``, which carries the watermark.
    """
    video = detail.get("video") or {}

    for candidate in (video.get("play_addr") or {}).get("url_list") or []:
        if candidate.startswith("http"):
            return candidate.replace("playwm", "play")

    for rendition in video.get("bit_rate") or []:
        for candidate in (rendition.get("play_addr") or {}).get("url_list") or []:
            if candidate.startswith("http"):
                return candidate

    for candidate in (video.get("download_addr") or {}).get("url_list") or []:
        if candidate.startswith("http"):
            return candidate

    raise DouyinError("作品详情里没有可用的视频地址（可能是图文作品）")


def describe(detail: dict[str, Any]) -> dict[str, Any]:
    """The fields worth printing alongside the transcript."""
    author = detail.get("author") or {}
    stats = detail.get("statistics") or {}
    return {
        "aweme_id": detail.get("aweme_id"),
        "desc": (detail.get("desc") or "").strip(),
        "author": author.get("nickname"),
        "author_id": author.get("unique_id") or author.get("short_id"),
        "create_time": detail.get("create_time"),
        "duration_ms": (detail.get("video") or {}).get("duration"),
        "digg_count": stats.get("digg_count"),
        "comment_count": stats.get("comment_count"),
        "share_count": stats.get("share_count"),
        "collect_count": stats.get("collect_count"),
    }


async def download_video(
    url: str, dest: str, *, user_agent: str, referer: str = "https://www.douyin.com/"
) -> int:
    """Fetch the video to ``dest``. Returns the byte count.

    The CDN refuses a request without a Douyin ``Referer``, so one is sent even
    though the URL itself is already signed and time-limited.
    """
    async with wreq.Client(no_proxy=True, gzip=True) as client:
        response = await client.get(
            url,
            headers={
                "User-Agent": user_agent,
                "Referer": referer,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        status = response.status.as_int()
        if status != 200:
            raise DouyinError(f"下载视频失败：HTTP {status}")
        data = await response.bytes()
    if not data:
        raise DouyinError("下载到的视频是空文件")
    with open(dest, "wb") as handle:
        handle.write(data)
    return len(data)


__all__ = [
    "DouyinError",
    "SignatureRefused",
    "build_url",
    "describe",
    "download_video",
    "extract_aweme_id",
    "fetch_aweme",
    "pick_play_url",
]
