# SPDX-License-Identifier: MIT
"""Pull a 16 kHz mono WAV out of a video, using ffmpeg.

16 kHz mono PCM is what :mod:`douyin2text.asr` wants and what every speech model
is trained on, so the conversion happens once here rather than inside the model
loader.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

#: SenseVoice is non-streaming and processes the waveform in one pass. Very long
#: audio costs memory for no accuracy gain, so anything past this is split.
CHUNK_SECONDS: float = 180.0

#: Where ffmpeg hides when ``PATH`` cannot see it. An app launched from Finder
#: inherits a minimal ``PATH`` (``/usr/bin:/bin:/usr/sbin:/sbin``) and the user's
#: shell profile is never sourced, so a Homebrew or pip-installed ffmpeg is
#: invisible even though it works perfectly in Terminal.
FALLBACK_BIN_DIRS: tuple[str, ...] = (
    "/opt/homebrew/bin",  # Homebrew on Apple silicon
    "/usr/local/bin",     # Homebrew on Intel, and most manual installs
    "/opt/local/bin",     # MacPorts
    str(Path.home() / ".local" / "bin"),
    str(Path.home() / "bin"),
    "/usr/bin",
    "/bin",
)


class AudioError(RuntimeError):
    """ffmpeg is missing or could not read the file."""


def find_executable(name: str) -> str | None:
    """Locate ``name`` on ``PATH``, then in the usual install directories."""
    found = shutil.which(name)
    if found:
        return found
    for directory in FALLBACK_BIN_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def require_ffmpeg() -> str:
    path = find_executable("ffmpeg")
    if not path:
        raise AudioError(
            "找不到 ffmpeg。macOS 上可以运行：brew install ffmpeg\n"
            f"已查找：PATH，以及 {'、'.join(FALLBACK_BIN_DIRS)}"
        )
    return path


def probe_duration(path: Path) -> float:
    """Duration in seconds, via ``ffprobe`` when present, else ``ffmpeg -i``."""
    ffprobe = find_executable("ffprobe")
    if ffprobe:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json", str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            try:
                return float(json.loads(result.stdout)["format"]["duration"])
            except (KeyError, ValueError, json.JSONDecodeError):
                pass

    # ffmpeg without ffprobe still reports the duration on stderr.
    result = subprocess.run(
        [require_ffmpeg(), "-i", str(path)], capture_output=True, text=True, check=False
    )
    import re

    match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
    if match:
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return 0.0


def extract_wav(video: Path, dest: Path, *, start: float | None = None,
                duration: float | None = None) -> Path:
    """Convert ``video`` to 16 kHz mono PCM WAV at ``dest``."""
    require_ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)

    command = [require_ffmpeg(), "-y", "-loglevel", "error"]
    if start is not None:
        command += ["-ss", f"{start:.3f}"]
    command += ["-i", str(video)]
    if duration is not None:
        command += ["-t", f"{duration:.3f}"]
    command += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)]

    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        raise AudioError(
            f"ffmpeg 提取音频失败：{result.stderr.strip()[:300] or '没有输出'}\n"
            "如果视频没有音轨（纯音乐/无声作品），这里就会失败。"
        )
    return dest


def split_wav(video: Path, workdir: Path, *, seconds: float = CHUNK_SECONDS) -> list[Path]:
    """Extract one WAV per chunk, for audio longer than a single pass should take.

    Returns a single-element list for anything short enough, so callers do not
    branch on length.
    """
    total = probe_duration(video)
    if total <= seconds:
        return [extract_wav(video, workdir / "audio_000.wav")]

    chunks: list[Path] = []
    start = 0.0
    index = 0
    while start < total:
        span = min(seconds, total - start)
        chunks.append(
            extract_wav(video, workdir / f"audio_{index:03d}.wav", start=start, duration=span)
        )
        start += span
        index += 1
    return chunks


__all__ = ["AudioError", "CHUNK_SECONDS", "extract_wav", "find_executable",
           "probe_duration", "require_ffmpeg", "split_wav"]
