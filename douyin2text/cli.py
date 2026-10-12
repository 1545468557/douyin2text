# SPDX-License-Identifier: MIT
"""Command line entry point: one Douyin link in, one text file out."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

from . import __version__
from .asr import AsrError
from .audio import AudioError
from .douyin import DouyinError
from .pipeline import STEPS, PipelineError, Progress, TranscriptionService
from .session import SessionError

#: Characters Windows and macOS both object to in a filename.
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_stem(text: str, fallback: str, limit: int = 40) -> str:
    """A readable filename stem from a Douyin caption.

    Douyin captions are written for the feed, not for the filesystem: they carry
    ``#话题`` tags and can run to hundreds of characters. Tags are dropped
    because they are the same on every video from one author, which makes them
    useless for telling files apart.
    """
    cleaned = re.sub(r"#\S+", " ", text or "")
    cleaned = _UNSAFE.sub("_", cleaned).strip(" ._")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" _-，,。.、")
    return (cleaned[:limit].strip(" _-，,。.、") or fallback)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="douyin2text",
        description="把抖音视频里的人声拆成文字稿（本地离线转写）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  douyin2text 'https://v.douyin.com/xxxxxxx/'\n"
            "  douyin2text '7.32 复制打开抖音... https://v.douyin.com/xxxx/ ...' -o 文案.txt\n"
            "  douyin2text https://www.douyin.com/video/7185413072142568737 --keep-video\n"
            "\n"
            "想要图形界面：运行 d2t-gui，或双击项目里的「抖音视频转文字.app」\n"
        ),
    )
    parser.add_argument("link", help="抖音链接、App 分享文本，或 19 位作品 ID")
    parser.add_argument("-o", "--output", help="输出 .txt 路径（默认按标题自动命名）")
    parser.add_argument("--outdir", default=".", help="不指定 -o 时的输出目录（默认当前目录）")
    parser.add_argument("--keep-video", action="store_true", help="保留下载的视频和音频文件")
    parser.add_argument("--workdir", help="临时工作目录（配合 --keep-video 使用）")
    parser.add_argument("--refresh-session", action="store_true", help="强制重新获取访客 cookie")
    parser.add_argument(
        "--headed", action="store_true", help="用有界面的浏览器获取 cookie，便于排查"
    )
    parser.add_argument("--model-dir", help="SenseVoice 模型目录")
    parser.add_argument("--threads", type=int, default=4, help="转写线程数（默认 4）")
    parser.add_argument(
        "--header", action="store_true", help="在文字稿开头写入标题、作者和互动数据"
    )
    parser.add_argument("--json", action="store_true", help="把元数据以 JSON 打印到 stdout")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出结果路径")
    parser.add_argument("--version", action="version", version=f"douyin2text {__version__}")
    return parser


class _Reporter:
    """Prints each pipeline stage once, keeping the numbered CLI look."""

    def __init__(self, quiet: bool) -> None:
        self.quiet = quiet
        self._step: str | None = None
        self._detail: str | None = None
        self._index = 0

    def __call__(self, update: Progress) -> None:
        if self.quiet:
            return
        if update.step != self._step:
            self._step = update.step
            self._detail = None
            self._index = min(self._index + 1, len(STEPS))
            if update.step in STEPS:
                position = STEPS.index(update.step) + 1
                print(f"[{position}/{len(STEPS)}] {update.step}")
            else:
                print(f"      {update.step}")
        if update.detail and update.detail != self._detail:
            self._detail = update.detail
            print(f"      {update.detail}")


async def _run(args: argparse.Namespace) -> int:
    service = TranscriptionService(
        model_dir=Path(args.model_dir) if args.model_dir else None,
        threads=args.threads,
        keep_workdirs=bool(args.keep_video or args.workdir),
    )
    workdir = Path(args.workdir) if args.workdir else None
    if workdir is not None:
        workdir.mkdir(parents=True, exist_ok=True)

    result = await service.transcribe_link(
        args.link,
        on_progress=_Reporter(args.quiet),
        refresh_session=args.refresh_session,
        headed=args.headed,
        workdir=workdir,
    )

    if not args.quiet:
        print(f"[{len(STEPS)}/{len(STEPS)}] 写出文字稿（{result.elapsed:.1f}s）")
    out_path = _write_output(args, result.meta, result.text)

    if args.json:
        import json

        print(
            json.dumps(
                {**result.meta, "transcript_path": str(out_path), "text": result.text},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(out_path)
    return 0


def _write_output(args: argparse.Namespace, meta: dict, text: str) -> Path:
    if args.output:
        out_path = Path(args.output)
    else:
        stem = _safe_stem(meta.get("desc") or "", f"douyin_{meta.get('aweme_id')}")
        outdir = Path(args.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        out_path = outdir / f"{stem}.txt"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    body = text
    if args.header:
        lines = [
            f"标题：{meta.get('desc') or '(无)'}",
            f"作者：{meta.get('author') or '(未知)'}",
            f"作品 ID：{meta.get('aweme_id')}",
        ]
        if meta.get("duration_ms"):
            lines.append(f"时长：{meta['duration_ms'] / 1000:.1f} 秒")
        lines.append(
            f"互动：赞 {meta.get('digg_count')} / 评 {meta.get('comment_count')} "
            f"/ 藏 {meta.get('collect_count')} / 转 {meta.get('share_count')}"
        )
        body = "\n".join(lines) + "\n" + "-" * 40 + "\n" + text

    out_path.write_text(body + "\n", encoding="utf-8")
    return out_path


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except (DouyinError, SessionError, AudioError, AsrError, PipelineError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
