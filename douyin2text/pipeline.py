# SPDX-License-Identifier: MIT
"""The transcription pipeline, shared by the CLI and the local web UI.

Everything that touches the network or the CPU lives here so both front ends
report the same steps and the same errors. The CPU-bound part (SenseVoice) is
pushed onto a worker thread: the web UI has to stay responsive while a video is
being transcribed, and ``sherpa-onnx`` releases the GIL while it decodes.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .asr import Transcriber
from .audio import split_wav
from .douyin import (
    DouyinError,
    SignatureRefused,
    describe,
    download_video,
    fetch_aweme,
    pick_play_url,
)
from .session import Session, get_session

#: Human-readable stage names, in order. The UI shows these verbatim.
STEPS: tuple[str, ...] = ("解析作品", "下载视频", "提取音频", "语音转写", "写出文字稿")


@dataclass(slots=True)
class Progress:
    """Where a job currently is. ``fraction`` is 0..1 for the progress bar."""

    step: str
    detail: str = ""
    fraction: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"step": self.step, "detail": self.detail, "fraction": round(self.fraction, 3)}


@dataclass(slots=True)
class Transcription:
    """A finished transcription."""

    text: str
    meta: dict[str, Any]
    elapsed: float
    source: str
    workdir: Path | None = field(default=None, repr=False)


ProgressCallback = Callable[[Progress], None]


class PipelineError(RuntimeError):
    """Anything the user should see as a readable message."""


class TranscriptionService:
    """Owns the loaded ASR model and runs jobs against it.

    The model is loaded once and kept: it is about 230 MB and takes a second to
    initialise, which is fine for a long-running local server and unacceptable
    per job. The load itself is lazy so starting the server is instant.
    """

    def __init__(self, *, model_dir: Path | None = None, threads: int = 4,
                 keep_workdirs: bool = False) -> None:
        self._model_dir = model_dir
        self._threads = threads
        self._keep = keep_workdirs
        self._transcriber: Transcriber | None = None
        self._load_lock = asyncio.Lock()

    @property
    def model_loaded(self) -> bool:
        return self._transcriber is not None

    async def ensure_model(self) -> None:
        """Load the model if it is not loaded. Safe to call concurrently."""
        if self._transcriber is not None:
            return
        async with self._load_lock:
            if self._transcriber is None:
                self._transcriber = await asyncio.to_thread(
                    Transcriber, self._model_dir, threads=self._threads
                )

    async def transcribe_link(
        self,
        link: str,
        *,
        on_progress: ProgressCallback | None = None,
        refresh_session: bool = False,
        headed: bool = False,
        workdir: Path | None = None,
    ) -> Transcription:
        """Resolve a Douyin link and transcribe its audio."""
        started = time.time()
        report = on_progress or (lambda _p: None)

        report(Progress(STEPS[0], "获取访客凭证…", 0.02))
        session = await get_session(refresh=refresh_session, headless=not headed)

        async def fetch(current: Session) -> dict[str, Any]:
            return await fetch_aweme(link, current)

        try:
            detail = await fetch(session)
        except SignatureRefused:
            if refresh_session:
                raise
            # The cached jar went stale. Re-mint once, then give up loudly rather
            # than looping: a second refusal means the signature changed, not the
            # cookie.
            report(Progress(STEPS[0], "访客凭证失效，正在重新获取…", 0.05))
            session = await get_session(refresh=True, headless=not headed)
            detail = await fetch(session)

        meta = describe(detail)
        report(
            Progress(
                STEPS[0],
                f"{meta.get('author') or '未知作者'} · {meta.get('desc', '')[:30]}",
                0.15,
            )
        )

        own_workdir = workdir is None
        directory = workdir or Path(tempfile.mkdtemp(prefix="douyin2text-"))
        directory.mkdir(parents=True, exist_ok=True)

        try:
            report(Progress(STEPS[1], "正在下载无水印视频…", 0.2))
            video_path = directory / "video.mp4"
            size = await download_video(
                pick_play_url(detail), str(video_path), user_agent=session.user_agent
            )
            report(Progress(STEPS[1], f"{size / 1e6:.1f} MB", 0.4))

            text = await self._transcribe_file(
                video_path, directory, report, base=0.4, span=0.55
            )
            report(Progress(STEPS[4], "完成", 1.0))
            return Transcription(
                text=text,
                meta=meta,
                elapsed=time.time() - started,
                source=link,
                workdir=directory,
            )
        finally:
            if own_workdir and not self._keep:
                shutil.rmtree(directory, ignore_errors=True)

    async def transcribe_upload(
        self,
        path: Path,
        *,
        display_name: str,
        on_progress: ProgressCallback | None = None,
        workdir: Path | None = None,
    ) -> Transcription:
        """Transcribe a video or audio file the user already has."""
        started = time.time()
        report = on_progress or (lambda _p: None)
        report(Progress(STEPS[2], f"读取 {display_name}", 0.1))

        directory = workdir or Path(tempfile.mkdtemp(prefix="douyin2text-"))
        directory.mkdir(parents=True, exist_ok=True)
        try:
            text = await self._transcribe_file(path, directory, report, base=0.1, span=0.85)
            report(Progress(STEPS[4], "完成", 1.0))
            return Transcription(
                text=text,
                meta={"desc": display_name, "author": None, "aweme_id": None},
                elapsed=time.time() - started,
                source=display_name,
                workdir=directory,
            )
        finally:
            if workdir is None and not self._keep:
                shutil.rmtree(directory, ignore_errors=True)

    async def _transcribe_file(
        self,
        video_path: Path,
        directory: Path,
        report: ProgressCallback,
        *,
        base: float,
        span: float,
    ) -> str:
        report(Progress(STEPS[2], "用 ffmpeg 提取 16kHz 音频…", base + span * 0.05))
        chunks = await asyncio.to_thread(split_wav, video_path, directory)
        report(
            Progress(
                STEPS[2],
                f"音频切分为 {len(chunks)} 段" if len(chunks) > 1 else "音频就绪",
                base + span * 0.15,
            )
        )

        report(Progress(STEPS[3], "加载 SenseVoice 模型…", base + span * 0.2))
        await self.ensure_model()

        report(Progress(STEPS[3], "正在转写…", base + span * 0.25))

        def work() -> str:
            assert self._transcriber is not None
            return self._transcriber.transcribe_chunks(chunks)

        text = await asyncio.to_thread(work)
        report(Progress(STEPS[3], "转写完成", base + span))
        if not text.strip():
            raise PipelineError("转写结果为空——这条音频里可能没有人声（纯音乐或 MV）。")
        return text


__all__ = [
    "PipelineError",
    "Progress",
    "STEPS",
    "Transcription",
    "TranscriptionService",
]
