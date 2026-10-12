# SPDX-License-Identifier: MIT
"""The visitor cookie jar, and where it comes from.

Douyin's ``/aweme/v1/web/aweme/detail/`` is one of fourteen paths the platform
sign-protects with ``x-secsdk-web-signature``. That signature is bound to the
jar's ``UIFID_TEMP``, and ``UIFID_TEMP`` is issued by the platform to a browser
that actually loaded one of its pages - it cannot be computed, which is why no
pure-HTTP client works here.

So a browser is opened exactly once, the jar it earns is cached on disk, and
every subsequent lookup is a plain signed HTTP request. When the platform starts
refusing the signature the jar is re-minted; see :func:`Session.load`.

The browser is driven with Playwright against a browser already installed on
the machine (Chrome, Edge, or Chromium), so nothing large is downloaded.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Cookies worth keeping. The rest are analytics noise, and a jar that carries
#: them is not obviously more convincing than one that does not.
KEPT_COOKIES: tuple[str, ...] = (
    "ttwid",
    "UIFID_TEMP",
    "uifid",
    "s_v_web_id",
    "msToken",
    "odin_tt",
    "x-web-secsdk-uid",
    "web_sign_token",
    "__ac_nonce",
    "__ac_signature",
)

#: A visitor id is not a session token; it is meant to outlive a browsing
#: session. Re-minting daily is cheap insurance against quiet staleness.
DEFAULT_TTL_SECONDS: int = 24 * 60 * 60

#: Browsers tried in order. Chrome first because Douyin's SDK reports a Chrome
#: fingerprint, and the TLS profile we send afterwards is a Chrome one.
BROWSER_CHANNELS: tuple[str | None, ...] = ("chrome", "msedge", "chromium", None)


class SessionError(RuntimeError):
    """The visitor jar could not be obtained."""


@dataclass(slots=True)
class Session:
    """A cached visitor jar plus the browser fingerprint it was minted with."""

    cookies: dict[str, str]
    user_agent: str
    minted_at: float
    ttl: int = DEFAULT_TTL_SECONDS

    @property
    def uifid(self) -> str:
        for name in ("UIFID_TEMP", "uifid", "uifid_temp", "UIFID"):
            if self.cookies.get(name):
                return self.cookies[name]
        return ""

    @property
    def expired(self) -> bool:
        return (time.time() - self.minted_at) > self.ttl

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items() if k and v)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "cookies": self.cookies,
                    "user_agent": self.user_agent,
                    "minted_at": self.minted_at,
                    "ttl": self.ttl,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        # The jar is a credential: it authenticates us to Douyin as this visitor.
        os.chmod(path, 0o600)

    @classmethod
    def load(cls, path: Path, *, ttl: int = DEFAULT_TTL_SECONDS) -> Session | None:
        """The cached jar, or ``None`` when there is none or it is stale."""
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            session = cls(
                cookies={str(k): str(v) for k, v in payload["cookies"].items()},
                user_agent=str(payload["user_agent"]),
                minted_at=float(payload["minted_at"]),
                ttl=ttl,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not session.uifid or session.expired:
            return None
        return session


def cache_dir() -> Path:
    """Where this app keeps its state.

    Precedence: ``DOUYIN2TEXT_CACHE_DIR``, then a ``.cache/`` directory beside
    the checkout (so state created during setup keeps being used), then the user
    cache. Everything that writes state goes through here, so one override moves
    all of it.
    """
    override = os.environ.get("DOUYIN2TEXT_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    local = Path(__file__).resolve().parent.parent / ".cache"
    if local.exists():
        return local
    return Path.home() / ".cache" / "douyin2text"


def default_cache_path() -> Path:
    """Where the visitor jar lives. ``DOUYIN2TEXT_CACHE`` points at the file."""
    override = os.environ.get("DOUYIN2TEXT_CACHE")
    if override:
        return Path(override).expanduser()
    return cache_dir() / "session.json"


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


async def mint(*, headless: bool = True, timeout_ms: int = 45_000) -> Session:
    """Open Douyin in a real browser and keep the visitor cookies it earns.

    The page does not need to finish loading: ``UIFID_TEMP`` is set by the
    security SDK early, so the wait is bounded and the navigation is abandoned
    once the cookie is present rather than waiting on ads and video preloads.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - dependency guidance
        raise SessionError(
            "需要 playwright：pip install playwright\n"
            "本机已有的 Chrome 会被直接使用，不需要 playwright install 下载浏览器。"
        ) from exc

    last_error: Exception | None = None
    async with async_playwright() as playwright:
        for channel in BROWSER_CHANNELS:
            try:
                browser = await playwright.chromium.launch(
                    channel=channel,
                    headless=headless,
                    args=["--disable-blink-features=AutomationControlled"],
                )
            except Exception as exc:  # noqa: BLE001 - try the next channel
                last_error = exc
                continue

            try:
                context = await browser.new_context(
                    user_agent=DEFAULT_USER_AGENT,
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    viewport={"width": 1440, "height": 900},
                )
                page = await context.new_page()
                try:
                    await page.goto(
                        "https://www.douyin.com/",
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                except Exception:  # noqa: BLE001 - the cookie may already be set
                    pass

                cookies = await _wait_for_uifid(context, timeout_ms)
                user_agent = await page.evaluate("() => navigator.userAgent")
                await browser.close()

                if cookies.get("UIFID_TEMP") or cookies.get("uifid"):
                    return Session(
                        cookies=cookies,
                        user_agent=str(user_agent or DEFAULT_USER_AGENT),
                        minted_at=time.time(),
                    )
                last_error = SessionError(
                    f"{channel or 'chromium'} 打开了抖音但没有下发 UIFID_TEMP"
                )
            except Exception as exc:  # noqa: BLE001 - try the next channel
                last_error = exc
                await browser.close()

    raise SessionError(
        "无法获取抖音访客 cookie。请确认本机装有 Chrome 或 Edge，"
        f"或改用 --headed 观察页面。最后一次错误：{last_error}"
    )


async def _wait_for_uifid(context: Any, timeout_ms: int) -> dict[str, str]:
    """Poll the jar until the visitor id appears, then take a final snapshot."""
    deadline = time.time() + timeout_ms / 1000
    cookies: dict[str, str] = {}
    while time.time() < deadline:
        cookies = {
            c["name"]: c["value"]
            for c in await context.cookies()
            if c["name"] in KEPT_COOKIES and c["value"]
        }
        if cookies.get("UIFID_TEMP") or cookies.get("uifid"):
            # Give the SDK a moment to finish writing the companion cookies
            # (s_v_web_id and ttwid land in the same burst).
            await context.pages[0].wait_for_timeout(1200)
            return {
                c["name"]: c["value"]
                for c in await context.cookies()
                if c["name"] in KEPT_COOKIES and c["value"]
            }
        await context.pages[0].wait_for_timeout(400)
    return cookies


async def get_session(
    *,
    cache_path: Path | None = None,
    refresh: bool = False,
    headless: bool = True,
    ttl: int = DEFAULT_TTL_SECONDS,
) -> Session:
    """The cached jar if it is usable, otherwise a freshly minted one."""
    path = cache_path or default_cache_path()
    if not refresh:
        cached = Session.load(path, ttl=ttl)
        if cached is not None:
            return cached

    session = await mint(headless=headless)
    session.ttl = ttl
    try:
        session.save(path)
    except OSError:
        # An unwritable cache is not a reason to fail a lookup that already
        # has a usable jar in hand.
        pass
    return session


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "cache_dir",
    "DEFAULT_USER_AGENT",
    "KEPT_COOKIES",
    "Session",
    "SessionError",
    "default_cache_path",
    "get_session",
    "mint",
]
