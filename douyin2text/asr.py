# SPDX-License-Identifier: MIT
"""Speech to text with SenseVoice-Small, running locally.

SenseVoice is Alibaba's multilingual ASR model. The int8 ONNX build used here is
about 230 MB and runs on the CPU through ``sherpa-onnx`` - no PyTorch, no GPU,
no network at inference time. On an M4 it transcribes roughly seventy times
faster than real time.

The model is downloaded on first use from an HF mirror, because
``huggingface.co`` itself is unreachable from a lot of mainland networks.
"""

from __future__ import annotations

import os
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np

#: Where the model comes from. ``hf-mirror.com`` is the mirror maintained for
#: mainland access; the upstream repo is ``csukuangfj/sherpa-onnx-sense-voice-...``.
MODEL_REPO: str = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
MODEL_MIRRORS: tuple[str, ...] = (
    "https://hf-mirror.com",
    "https://huggingface.co",
)
MODEL_FILES: tuple[str, ...] = ("model.int8.onnx", "tokens.txt")

#: Rough size of the download, for the progress line.
MODEL_SIZE_HINT: str = "约 230 MB"


class AsrError(RuntimeError):
    """The model could not be loaded or the audio could not be read."""


def project_root() -> Path:
    """The checkout this package is running from.

    For an editable install this is the project directory; for a normal
    site-packages install it is wherever the package was unpacked, and the
    ``models/`` lookup below simply misses.
    """
    return Path(__file__).resolve().parent.parent


def default_model_dir() -> Path:
    """Where the model lives.

    Precedence: the environment variable, then a ``models/`` directory shipped
    beside the checkout (so an already-downloaded model is found with no setup
    at all), then the user cache.
    """
    override = os.environ.get("DOUYIN2TEXT_MODEL_DIR")
    if override:
        return Path(override).expanduser()
    local = project_root() / "models" / "sense-voice"
    if local.exists():
        return local
    return Path.home() / ".cache" / "douyin2text" / "sense-voice"


def _download(url: str, dest: Path) -> None:
    """Fetch ``url`` to ``dest``, resuming a partial file and verifying the size."""
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    existing = partial.stat().st_size if partial.exists() else 0

    request = urllib.request.Request(url, headers={"User-Agent": "douyin2text/0.1"})
    if existing:
        request.add_header("Range", f"bytes={existing}-")

    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        expected = response.headers.get("Content-Length")
        expected = int(expected) + existing if expected else None
        mode = "ab" if existing and response.status == 206 else "wb"
        if mode == "wb":
            existing = 0
        done = existing
        with open(partial, mode) as handle:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if expected:
                    percent = done * 100 // expected
                    print(f"\r  下载中 {dest.name} {percent}%", end="", flush=True)
    print()

    if expected and partial.stat().st_size != expected:
        raise AsrError(
            f"{dest.name} 下载不完整（{partial.stat().st_size}/{expected} 字节），请重跑一次续传。"
        )
    partial.replace(dest)


def ensure_model(model_dir: Path | None = None) -> Path:
    """Download the model if it is not already there, and return its directory."""
    directory = model_dir or default_model_dir()
    missing = [name for name in MODEL_FILES if not (directory / name).exists()]
    if not missing:
        return directory

    print(f"首次运行需要下载 SenseVoice 模型（{MODEL_SIZE_HINT}）到 {directory}")
    errors: list[str] = []
    for mirror in MODEL_MIRRORS:
        try:
            for name in missing:
                print(f"  从 {mirror} 获取 {name}")
                _download(f"{mirror}/{MODEL_REPO}/resolve/main/{name}", directory / name)
            return directory
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            errors.append(f"{mirror}: {exc}")
            # A file that arrived half-written is worse than none: drop it so
            # the next mirror starts clean rather than resuming a bad prefix.
            for name in missing:
                partial = directory / f"{name}.part"
                if partial.exists():
                    partial.unlink()

    raise AsrError(
        "模型下载失败。可以手动下载到 "
        f"{directory}：\n  " + "\n  ".join(errors) + "\n"
        f"文件列表：{', '.join(MODEL_FILES)}\n"
        f"来源：https://hf-mirror.com/{MODEL_REPO}"
    )


class Transcriber:
    """A loaded SenseVoice recognizer. Reuse it across files."""

    def __init__(self, model_dir: Path | None = None, *, threads: int = 4) -> None:
        try:
            import sherpa_onnx
        except ImportError as exc:  # pragma: no cover - dependency guidance
            raise AsrError("需要 sherpa-onnx：pip install sherpa-onnx") from exc

        directory = ensure_model(model_dir)
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(directory / "model.int8.onnx"),
            tokens=str(directory / "tokens.txt"),
            # Inverse text normalization turns "一千二百" into "1200" and adds
            # punctuation; for a transcript meant to be read, that is the point.
            use_itn=True,
            language="zh",
            num_threads=threads,
            debug=False,
        )

    def transcribe_file(self, wav: Path) -> str:
        """Transcribe one 16 kHz mono WAV."""
        with wave.open(str(wav)) as handle:
            rate = handle.getframerate()
            frames = handle.getnframes()
            samples = (
                np.frombuffer(handle.readframes(frames), dtype=np.int16).astype(np.float32)
                / 32768.0
            )
        return self.transcribe_samples(samples, rate)

    def transcribe_samples(self, samples: "np.ndarray", rate: int) -> str:
        stream = self._recognizer.create_stream()
        stream.accept_waveform(rate, samples)
        self._recognizer.decode_stream(stream)
        return str(stream.result.text).strip()

    def transcribe_chunks(
        self, chunks: list[Path], *, progress: Callable[[int, int, float], None] | None = None
    ) -> str:
        """Transcribe each chunk in order and join the text."""
        parts: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            started = time.time()
            text = self.transcribe_file(chunk)
            if text:
                parts.append(text)
            if progress:
                progress(index, len(chunks), time.time() - started)
        return "\n".join(parts)


__all__ = [
    "AsrError",
    "MODEL_FILES",
    "MODEL_MIRRORS",
    "MODEL_REPO",
    "Transcriber",
    "default_model_dir",
    "ensure_model",
]
