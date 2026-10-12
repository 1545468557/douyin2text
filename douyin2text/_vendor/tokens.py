# SPDX-License-Identifier: Apache-2.0
# Vendored from Douyin_TikTok_Download_API (https://github.com/Evil0ctal/Douyin_TikTok_Download_API).
# See NOTICE at the repository root; the rest of this project is MIT.
"""Guest-token generation for Douyin's web API.

Vendored subset of ``dtk.signing.native.tokens`` from
https://github.com/Evil0ctal/Douyin_TikTok_Download_API (Apache-2.0).
Only the pure, IO-free generators are kept here: ``ttwid`` is minted by an
explicit HTTP call in :mod:`douyin2text.douyin`, and the real ``msToken``
endpoint is not used at all.

Upstream reference: ``src/dtk/signing/native/tokens.py``.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from typing import Final

#: Alphabet a fake msToken draws from. Copied character for character from the
#: upstream project so the token keeps the shape of a real one.
MS_TOKEN_ALPHABET: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-"

#: Douyin's fake msToken length before the ``==`` suffix.
DOUYIN_MS_TOKEN_LENGTH: Final = 126

#: Alphabet for the random half of ``verify_fp`` / ``s_v_web_id``.
VERIFY_FP_ALPHABET: Final = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

#: Fixed layout of the 36 character tail of ``verify_fp``: a UUID v4 skeleton.
_VERIFY_FP_SEPARATORS: Final[tuple[int, ...]] = (8, 13, 18, 23)
_VERIFY_FP_VERSION_INDEX: Final = 14
_VERIFY_FP_VARIANT_INDEX: Final = 19
_VERIFY_FP_LENGTH: Final = 36


@dataclass(frozen=True, slots=True)
class TtwidSpec:
    """Registration call that mints a ``ttwid`` cookie."""

    url: str
    data: str


#: Public registration endpoint. Same host for both platforms; the body differs.
TTWID_REGISTER_URL: Final = "https://ttwid.bytedance.com/ttwid/union/register/"

#: Douyin guest ttwid registration. Contains no credential.
DOUYIN_TTWID: Final = TtwidSpec(
    url=TTWID_REGISTER_URL,
    data=json.dumps(
        {
            "region": "cn",
            "aid": 1768,
            "needFid": False,
            "service": "www.ixigua.com",
            "migrate_info": {"ticket": "", "source": "node"},
            "cbUrlProtocol": "https",
            "union": True,
        }
    ),
)


def gen_false_ms_token(
    length: int = DOUYIN_MS_TOKEN_LENGTH,
    *,
    rng: random.Random | None = None,
    suffix: str = "==",
) -> str:
    """A locally generated msToken.

    The platform accepts it for many endpoints and rejects it for some; the
    caller is expected to fall back to the real endpoint when it is rejected.
    """
    source = rng or random.Random()
    return "".join(source.choice(MS_TOKEN_ALPHABET) for _ in range(length)) + suffix


def _to_base36(value: int) -> str:
    digits: list[str] = []
    while value:
        value, remainder = divmod(value, 36)
        digits.append("0123456789abcdefghijklmnopqrstuvwxyz"[remainder])
    return "".join(reversed(digits))


def gen_verify_fp(*, now_ms: int | None = None, rng: random.Random | None = None) -> str:
    """Generate a ``verify_fp``.

    Shape: ``verify_<base36 milliseconds>_<uuid4-like 36 characters>``, where the
    tail keeps a UUID v4 skeleton - underscores at 8/13/18/23, ``4`` at 14, and
    the variant nibble at 19.
    """
    source = rng or random.Random()
    milliseconds = now_ms if now_ms is not None else round(time.time() * 1000)
    alphabet_size = len(VERIFY_FP_ALPHABET)

    tail: list[str] = [""] * _VERIFY_FP_LENGTH
    for index in _VERIFY_FP_SEPARATORS:
        tail[index] = "_"
    tail[_VERIFY_FP_VERSION_INDEX] = "4"
    for index in range(_VERIFY_FP_LENGTH):
        if tail[index]:
            continue
        position = int(source.random() * alphabet_size)
        if index == _VERIFY_FP_VARIANT_INDEX:
            position = 3 & position | 8
        tail[index] = VERIFY_FP_ALPHABET[position]

    return "verify_" + _to_base36(milliseconds) + "_" + "".join(tail)


def gen_s_v_web_id(*, now_ms: int | None = None, rng: random.Random | None = None) -> str:
    """``s_v_web_id`` has the same shape as ``verify_fp`` and is minted the same way."""
    return gen_verify_fp(now_ms=now_ms, rng=rng)


__all__ = [
    "DOUYIN_MS_TOKEN_LENGTH",
    "DOUYIN_TTWID",
    "MS_TOKEN_ALPHABET",
    "TTWID_REGISTER_URL",
    "TtwidSpec",
    "gen_false_ms_token",
    "gen_s_v_web_id",
    "gen_verify_fp",
]
