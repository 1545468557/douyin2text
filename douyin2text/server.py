# SPDX-License-Identifier: MIT
"""A local web UI: paste a link (or drop a file) and read the transcript.

Runs on loopback only. Nothing is uploaded anywhere - the browser talks to this
process, and this process talks to Douyin and to SenseVoice, both of which stay
on the machine.

Jobs are asynchronous because a transcription takes seconds, not milliseconds:
``POST`` returns an id immediately and the page polls for progress.
"""

from __future__ import annotations

import asyncio
import re
import socket
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import __version__
from .asr import AsrError
from .audio import AudioError
from .douyin import DouyinError
from .history import HistoryEntry, HistoryStore
from .pipeline import PipelineError, Progress, TranscriptionService
from .session import SessionError, cache_dir

#: How many in-flight and recently finished jobs to keep in memory. This is the
#: progress board, not the history: the history lives on disk and outlives the
#: process.
HISTORY_LIMIT: int = 50

#: Upload ceiling. A Douyin video is a few MB; anything past this is a mistake.
MAX_UPLOAD_BYTES: int = 500 * 1024 * 1024

JobState = Literal["queued", "running", "done", "failed"]


@dataclass(slots=True)
class Job:
    id: str
    kind: Literal["link", "file"]
    source: str
    state: JobState = "queued"
    step: str = "排队中"
    detail: str = ""
    fraction: float = 0.0
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    elapsed: float = 0.0
    created_at: float = field(default_factory=time.time)
    #: Id of the row this job wrote to the history file, once it finishes.
    history_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "state": self.state,
            "step": self.step,
            "detail": self.detail,
            "fraction": round(self.fraction, 3),
            "text": self.text,
            "meta": self.meta,
            "error": self.error,
            "elapsed": round(self.elapsed, 2),
            "created_at": self.created_at,
            "history_id": self.history_id,
        }


class TranscribeRequest(BaseModel):
    link: str = Field(min_length=1, max_length=4000)
    refresh_session: bool = False


def create_app(
    service: TranscriptionService | None = None,
    on_shutdown: "Callable[[], None] | None" = None,
    history: HistoryStore | None = None,
) -> FastAPI:
    service = service or TranscriptionService()
    # `is None`, not `or`: HistoryStore defines __len__, so an *empty* store is
    # falsy, and `history or HistoryStore(...)` would silently throw away the
    # caller's store and open the default one instead. The test suite caught this
    # by injecting a fresh store and watching every case share a single file.
    if history is None:
        history = HistoryStore(cache_dir() / "history.json")
    app = FastAPI(title="douyin2text", version=__version__, docs_url=None, redoc_url=None)
    jobs: dict[str, Job] = {}
    order: list[str] = []

    def remember(job: Job) -> None:
        jobs[job.id] = job
        order.append(job.id)
        while len(order) > HISTORY_LIMIT:
            jobs.pop(order.pop(0), None)

    def progress_for(job: Job):
        def report(update: Progress) -> None:
            job.step = update.step
            job.detail = update.detail
            job.fraction = update.fraction

        return report

    async def run_link_job(job: Job, request: TranscribeRequest) -> None:
        job.state = "running"
        try:
            result = await service.transcribe_link(
                request.link,
                on_progress=progress_for(job),
                refresh_session=request.refresh_session,
            )
        except Exception as exc:  # noqa: BLE001 - the message is the product here
            fail(job, exc)
            return
        finish(job, result)

    async def run_file_job(job: Job, path: Path) -> None:
        job.state = "running"
        try:
            result = await service.transcribe_upload(
                path, display_name=job.source, on_progress=progress_for(job)
            )
        except Exception as exc:  # noqa: BLE001
            fail(job, exc)
            return
        finally:
            path.unlink(missing_ok=True)
        finish(job, result)

    def fail(job: Job, exc: Exception) -> None:
        job.state = "failed"
        if isinstance(exc, (DouyinError, SessionError, AudioError, AsrError, PipelineError)):
            job.error = str(exc)
        else:
            job.error = f"{type(exc).__name__}: {exc}"

    def finish(job: Job, result: Any) -> None:
        job.state = "done"
        job.text = result.text
        job.meta = result.meta
        job.elapsed = result.elapsed
        job.fraction = 1.0
        job.step = "完成"
        # Saving is what makes the result survive the window closing. A failure
        # here is not a reason to report the transcription as failed - the user
        # has the text either way.
        try:
            entry = history.add(HistoryEntry.from_job(job))
            job.history_id = entry.id
        except OSError:
            job.history_id = ""

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return INDEX_HTML.replace("__VERSION__", __version__)

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "model_loaded": service.model_loaded,
        }

    @app.post("/api/transcribe")
    async def transcribe(request: TranscribeRequest) -> dict[str, Any]:
        link = request.link.strip()
        if not link:
            raise HTTPException(status_code=400, detail="请粘贴抖音链接或分享文本")
        job = Job(id=uuid.uuid4().hex[:12], kind="link", source=link[:200])
        remember(job)
        asyncio.create_task(run_link_job(job, request))
        return {"job_id": job.id}

    @app.post("/api/upload")
    async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
        name = Path(file.filename or "upload").name
        suffix = Path(name).suffix or ".mp4"
        target = cache_dir() / "uploads"
        target.mkdir(parents=True, exist_ok=True)
        destination = target / f"{uuid.uuid4().hex[:12]}{suffix}"

        written = 0
        with open(destination, "wb") as handle:
            while chunk := await file.read(1 << 20):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    handle.close()
                    destination.unlink(missing_ok=True)
                    raise HTTPException(status_code=413, detail="文件超过 500 MB 上限")
                handle.write(chunk)

        job = Job(id=uuid.uuid4().hex[:12], kind="file", source=name)
        remember(job)
        asyncio.create_task(run_file_job(job, destination))
        return {"job_id": job.id}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在或已被清理")
        return job.as_dict()

    @app.get("/api/jobs/{job_id}/download", response_class=PlainTextResponse)
    async def download(job_id: str) -> PlainTextResponse:
        job = jobs.get(job_id)
        if job is None or job.state != "done":
            raise HTTPException(status_code=404, detail="任务还没有结果")
        return PlainTextResponse(
            job.text + "\n",
            headers={"Content-Disposition": _disposition(_filename(job))},
        )

    # -- history ----------------------------------------------------------
    # The history is a file on disk and outlives the process, so these read from
    # the store rather than from the in-memory job board.

    @app.get("/api/history")
    async def list_history() -> dict[str, Any]:
        return {"entries": history.summaries(), "total": len(history)}

    @app.get("/api/history/{entry_id}")
    async def get_history(entry_id: str) -> dict[str, Any]:
        entry = history.get(entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="这条记录不存在")
        return entry.as_dict()

    @app.delete("/api/history/{entry_id}")
    async def delete_history(entry_id: str) -> dict[str, Any]:
        if not history.delete(entry_id):
            raise HTTPException(status_code=404, detail="这条记录不存在")
        return {"ok": True, "deleted": entry_id, "total": len(history)}

    @app.delete("/api/history")
    async def clear_history() -> dict[str, Any]:
        return {"ok": True, "cleared": history.clear()}

    @app.get("/api/history/{entry_id}/download", response_class=PlainTextResponse)
    async def download_history(entry_id: str) -> PlainTextResponse:
        entry = history.get(entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="这条记录不存在")
        return PlainTextResponse(
            entry.text + "\n",
            headers={"Content-Disposition": _disposition(_history_filename(entry))},
        )

    @app.post("/api/shutdown")
    async def shutdown() -> dict[str, bool]:
        """Quit the app. Used by the page's 退出程序 button.

        The callback runs on the loop rather than inline so the HTTP response
        reaches the browser before the server stops answering.
        """
        if on_shutdown is not None:
            asyncio.get_running_loop().call_later(0.4, on_shutdown)
        return {"ok": True}

    return app


def _disposition(filename: str) -> str:
    """An RFC 5987 ``Content-Disposition`` that survives non-ASCII names.

    An HTTP header is latin-1, so a Chinese title cannot go in a plain
    ``filename=``. The ASCII fallback keeps older clients working; ``filename*``
    carries the real name.
    """
    from urllib.parse import quote

    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "transcript.txt"
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )


def _stem(text: str, fallback: str) -> str:
    """A readable, filesystem-safe filename stem from a Douyin caption.

    Douyin captions carry ``#话题`` tags and can run to hundreds of characters.
    Tags are dropped because they repeat across an author's videos, which makes
    them useless for telling files apart.
    """
    stem = re.sub(r"#\S+", " ", text or "")
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" ._")[:40].strip()
    return stem or fallback


def _filename(job: Job) -> str:
    return f"{_stem(job.meta.get('desc') or job.source, 'transcript')}.txt"


def _history_filename(entry: HistoryEntry) -> str:
    return f"{_stem(entry.title or entry.source, 'transcript')}.txt"


def free_port(preferred: int = 8765) -> int:
    """``preferred`` if it is free, otherwise whatever the OS hands us."""
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return int(sock.getsockname()[1])
    raise RuntimeError("找不到可用端口")


def main(argv: list[str] | None = None) -> int:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="douyin2text-ui", description="本地图形界面")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--model-dir", help="SenseVoice 模型目录")
    parser.add_argument("--threads", type=int, default=4, help="转写线程数")
    parser.add_argument("--version", action="version", version=f"douyin2text-ui {__version__}")
    args = parser.parse_args(argv)

    port = free_port(args.port) if args.host == "127.0.0.1" else args.port
    url = f"http://{args.host}:{port}/"
    service = TranscriptionService(
        model_dir=Path(args.model_dir) if args.model_dir else None, threads=args.threads
    )

    print(f"douyin2text {__version__}")
    print(f"界面地址：{url}")
    print("关闭这个窗口即可退出。")
    if not args.no_browser:
        # Give uvicorn a moment to bind before the browser asks for the page.
        threading_timer = __import__("threading").Timer(1.0, webbrowser.open, args=(url,))
        threading_timer.daemon = True
        threading_timer.start()

    uvicorn.run(create_app(service), host=args.host, port=port, log_level="warning")
    return 0


# ---------------------------------------------------------------------------
# The page. A single file with no build step and no external requests: the app
# has to work offline, and a CDN in the critical path would break that.
# ---------------------------------------------------------------------------

INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>抖音视频转文字</title>
<!-- 内联 favicon：不产生额外请求，离线也能显示 -->
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='14' fill='%23ff2c55'/><text x='32' y='46' font-size='40' text-anchor='middle' fill='%23fff' font-family='PingFang SC,Hiragino Sans GB,sans-serif' font-weight='700'>%E6%96%87</text></svg>">
<style>
  :root {
    --bg: #f5f5f7; --card: #ffffff; --ink: #1d1d1f; --muted: #6e6e73;
    --line: #e3e3e6; --accent: #ff2c55; --accent-ink: #ffffff; --ok: #1a9c5b;
    --err: #d1301a; --radius: 14px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #1a1a1c; --card: #26262a; --ink: #f2f2f4; --muted: #9b9ba1;
      --line: #3a3a40; --accent: #ff3b60;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.6 -apple-system, "PingFang SC", "Helvetica Neue", sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 780px; margin: 0 auto; padding: 40px 20px 80px; }
  header { display: flex; align-items: baseline; gap: 10px; margin-bottom: 6px; }
  h1 { font-size: 26px; margin: 0; letter-spacing: -0.4px; }
  .ver { color: var(--muted); font-size: 12px; }
  .sub { color: var(--muted); margin: 0 0 26px; font-size: 14px; }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: var(--radius); padding: 18px; margin-bottom: 18px;
  }
  textarea {
    width: 100%; min-height: 92px; resize: vertical; padding: 12px 14px;
    border: 1px solid var(--line); border-radius: 10px; background: transparent;
    color: var(--ink); font: inherit; outline: none;
  }
  textarea:focus { border-color: var(--accent); }
  .row { display: flex; gap: 10px; align-items: center; margin-top: 12px; flex-wrap: wrap; }
  button {
    font: inherit; font-weight: 500; border-radius: 10px; padding: 9px 18px;
    border: 1px solid var(--line); background: transparent; color: var(--ink);
    cursor: pointer; transition: opacity .15s;
  }
  button:hover { opacity: .78; }
  button:disabled { opacity: .4; cursor: default; }
  button.primary {
    background: var(--accent); color: var(--accent-ink); border-color: var(--accent);
    padding: 9px 26px;
  }
  .hint { color: var(--muted); font-size: 12.5px; margin-top: 10px; }
  .drop {
    border: 1.5px dashed var(--line); border-radius: 10px; padding: 18px;
    text-align: center; color: var(--muted); font-size: 13.5px; cursor: pointer;
    transition: border-color .15s, color .15s;
  }
  .drop.over { border-color: var(--accent); color: var(--accent); }
  .bar { height: 5px; background: var(--line); border-radius: 3px; overflow: hidden; }
  .bar > i {
    display: block; height: 100%; width: 0; background: var(--accent);
    border-radius: 3px; transition: width .3s ease;
  }
  .steps { display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
  .steps span {
    font-size: 12px; color: var(--muted); padding: 2px 9px;
    border-radius: 20px; border: 1px solid var(--line);
  }
  .steps span.active { color: var(--accent-ink); background: var(--accent); border-color: var(--accent); }
  .steps span.past { color: var(--ok); border-color: var(--ok); }
  .meta { color: var(--muted); font-size: 13px; margin-bottom: 12px; }
  .meta b { color: var(--ink); font-weight: 600; }
  pre.text {
    white-space: pre-wrap; word-break: break-word; margin: 0;
    font: inherit; max-height: 46vh; overflow: auto;
  }
  .err { color: var(--err); white-space: pre-wrap; }
  .hist { list-style: none; padding: 0; margin: 0; }
  .hist li {
    padding: 11px 0; border-bottom: 1px solid var(--line);
    display: flex; gap: 10px; align-items: center; font-size: 13.5px;
  }
  .hist li:last-child { border-bottom: none; }
  .hist .grow {
    flex: 1; min-width: 0; cursor: pointer;
    display: flex; flex-direction: column; gap: 2px;
  }
  .hist .grow:hover .name { color: var(--accent); }
  .hist .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .hist .sub { color: var(--muted); font-size: 11.5px; display: flex; gap: 8px; }
  .hist .when { color: var(--muted); font-size: 12px; white-space: nowrap; }
  .hist .acts { display: flex; gap: 2px; opacity: 0; transition: opacity .12s; }
  .hist li:hover .acts { opacity: 1; }
  .hist .acts button {
    padding: 3px 9px; font-size: 12px; border-radius: 7px; border-color: transparent;
    color: var(--muted);
  }
  .hist .acts button:hover { color: var(--ink); border-color: var(--line); }
  .hist .acts button.del:hover { color: var(--err); border-color: var(--err); }
  .hist-head {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 4px;
  }
  .hist-head .meta { margin: 0; }
  .hist-head button { padding: 3px 10px; font-size: 12px; border-radius: 7px; }
  .tag { font-size: 11px; padding: 1px 7px; border-radius: 6px; border: 1px solid var(--line); color: var(--muted); }
  .hidden { display: none !important; }
</style>
</head>
<body>
<div class="wrap">
  <header><h1>抖音视频转文字</h1><span class="ver">v__VERSION__</span></header>
  <p class="sub">粘贴抖音分享链接，或拖入本地视频。转写全程在本机完成，音频不会上传到任何地方。</p>

  <div class="card">
    <textarea id="link" placeholder="把抖音 App「复制链接」的整段文字粘贴到这里，例如：&#10;7.32 复制打开抖音，看看【xxx的作品】... https://v.douyin.com/xxxx/ ..."></textarea>
    <div class="row">
      <button class="primary" id="go">开始转写</button>
      <button id="clear">清空</button>
      <span class="hint" id="statusHint"></span>
    </div>
    <div class="hint">也支持桌面端链接和 19 位作品 ID。</div>
  </div>

  <div class="card">
    <div class="drop" id="drop">拖入视频/音频文件，或点击选择（用不了抖音解析时，可以先用抖音 App 存到本地）</div>
    <input type="file" id="file" accept="video/*,audio/*" class="hidden">
  </div>

  <div class="card hidden" id="progressCard">
    <div class="steps" id="steps"></div>
    <div class="bar"><i id="barFill"></i></div>
    <div class="hint" id="progressText"></div>
    <div class="row"><button id="cancelHint" class="hidden" disabled></button></div>
  </div>

  <div class="card hidden" id="resultCard">
    <div class="meta" id="meta"></div>
    <pre class="text" id="text"></pre>
    <div class="row">
      <button id="copy">复制全文</button>
      <button id="download">下载 .txt</button>
      <span class="hint" id="copied"></span>
    </div>
  </div>

  <div class="card hidden" id="errorCard">
    <div class="err" id="errorText"></div>
  </div>

  <div class="card hidden" id="histCard">
    <div class="hist-head">
      <div class="meta">历史记录 <span id="histCount"></span></div>
      <button id="clearHist">清空历史</button>
    </div>
    <ul class="hist" id="hist"></ul>
  </div>

  <div class="row" style="justify-content:center;margin-top:26px">
    <button id="quit">退出程序</button>
  </div>
</div>

<script>
const STEPS = ["解析作品", "下载视频", "提取音频", "语音转写", "写出文字稿"];
const $ = (id) => document.getElementById(id);
// `currentJob` is set while looking at a fresh transcription, `currentEntry`
// while looking at one loaded back from the history. The download button has to
// work for both, and they live behind different endpoints.
let currentJob = null, currentEntry = null, timer = null;

function show(id) { $(id).classList.remove("hidden"); }
function hide(id) { $(id).classList.add("hidden"); }

function setSteps(activeStep) {
  const idx = STEPS.indexOf(activeStep);
  $("steps").innerHTML = STEPS.map((s, i) =>
    `<span class="${i === idx ? "active" : (idx > i ? "past" : "")}">${s}</span>`).join("");
}

async function start(url, init) {
  hide("resultCard"); hide("errorCard"); show("progressCard");
  $("go").disabled = true;
  $("barFill").style.width = "0%";
  $("progressText").textContent = "正在提交…";
  setSteps(null);
  try {
    const res = await fetch(url, init);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `请求失败（HTTP ${res.status}）`);
    }
    const { job_id } = await res.json();
    currentJob = job_id;
    currentEntry = null;
    poll();
  } catch (e) {
    finishWithError(e.message);
  }
}

function startLink(link) {
  return start("/api/transcribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ link }),
  });
}

function startUpload(file) {
  const fd = new FormData();
  fd.append("file", file);
  return start("/api/upload", { method: "POST", body: fd });
}

function poll() {
  clearInterval(timer);
  timer = setInterval(async () => {
    if (!currentJob) return;
    let job;
    try {
      const res = await fetch(`/api/jobs/${currentJob}`);
      if (!res.ok) throw new Error("任务丢失");
      job = await res.json();
    } catch (e) { return; }

    setSteps(job.step);
    $("barFill").style.width = Math.round((job.fraction || 0) * 100) + "%";
    $("progressText").textContent = job.detail || job.step || "处理中…";

    if (job.state === "done") { clearInterval(timer); finishWithResult(job); }
    else if (job.state === "failed") { clearInterval(timer); finishWithError(job.error || "未知错误"); }
  }, 500);
}

function finishWithResult(job) {
  $("go").disabled = false;
  hide("progressCard"); show("resultCard");
  const m = job.meta || {};
  const bits = [];
  if (m.author) bits.push(`作者：<b>${esc(m.author)}</b>`);
  if (m.duration_ms) bits.push(`时长 ${(m.duration_ms / 1000).toFixed(1)}s`);
  if (m.digg_count != null) bits.push(`赞 ${m.digg_count} · 评 ${m.comment_count} · 藏 ${m.collect_count}`);
  bits.push(`转写耗时 ${job.elapsed}s`);
  $("meta").innerHTML = bits.join(" &nbsp;|&nbsp; ");
  $("text").textContent = job.text;
  $("copied").textContent = "";
  // The transcript has just been written to disk, so the list on screen is one
  // entry stale until this comes back.
  loadHistory();
}

function finishWithError(message) {
  $("go").disabled = false;
  hide("progressCard"); show("errorCard");
  $("errorText").textContent = message;
}

/* ------------------------------ history ------------------------------ */

async function loadHistory() {
  let data;
  try {
    data = await (await fetch("/api/history")).json();
  } catch (e) { return; }
  const entries = data.entries || [];
  const card = $("histCard");
  if (!entries.length) { hide("histCard"); return; }

  show("histCard");
  $("histCount").textContent = `（${data.total}）`;
  $("hist").innerHTML = entries.map((e) => {
    const sub = [];
    if (e.author) sub.push(esc(e.author));
    if (e.chars) sub.push(`${e.chars} 字`);
    if (e.kind === "file") sub.push("本地文件");
    return `<li>
      <span class="grow" data-id="${e.id}" title="点击查看">
        <span class="name">${esc(e.title).slice(0, 60)}</span>
        <span class="sub">${sub.join(" · ")}</span>
      </span>
      <span class="when">${timeAgo(e.created_at)}</span>
      <span class="acts">
        <button class="dl" data-id="${e.id}">下载</button>
        <button class="del" data-id="${e.id}">删除</button>
      </span>
    </li>`;
  }).join("");

  $("hist").querySelectorAll(".grow").forEach((el) =>
    el.onclick = () => viewEntry(el.dataset.id));
  $("hist").querySelectorAll(".dl").forEach((b) =>
    b.onclick = (ev) => { ev.stopPropagation(); location.href = `/api/history/${b.dataset.id}/download`; });
  $("hist").querySelectorAll(".del").forEach((b) =>
    b.onclick = async (ev) => {
      ev.stopPropagation();
      if (!confirm(`删除这条记录？\n\n${b.closest("li").querySelector(".name").textContent}`)) return;
      await fetch(`/api/history/${b.dataset.id}`, { method: "DELETE" });
      if (currentEntry === b.dataset.id) { currentEntry = null; hide("resultCard"); }
      loadHistory();
    });
}

async function viewEntry(id) {
  let entry;
  try {
    const res = await fetch(`/api/history/${id}`);
    if (!res.ok) throw new Error("记录不存在");
    entry = await res.json();
  } catch (e) {
    $("errorText").textContent = `读取历史记录失败：${e.message}`;
    hide("resultCard"); show("errorCard");
    return;
  }
  hide("errorCard"); hide("progressCard"); show("resultCard");
  const m = entry.meta || {};
  const bits = [];
  if (m.author) bits.push(`作者：<b>${esc(m.author)}</b>`);
  if (m.duration_ms) bits.push(`时长 ${(m.duration_ms / 1000).toFixed(1)}s`);
  if (m.digg_count != null) bits.push(`赞 ${m.digg_count} · 评 ${m.comment_count} · 藏 ${m.collect_count}`);
  bits.push(`${timeAgo(entry.created_at)}转写`);
  bits.push(`${entry.text.length} 字`);
  $("meta").innerHTML = bits.join(" &nbsp;|&nbsp; ");
  $("text").textContent = entry.text;
  $("copied").textContent = "";
  currentJob = null;
  currentEntry = id;
}

function timeAgo(seconds) {
  const delta = Date.now() / 1000 - seconds;
  if (delta < 60) return "刚刚";
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
  if (delta < 86400 * 7) return `${Math.floor(delta / 86400)} 天前`;
  const d = new Date(seconds * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

$("go").onclick = () => {
  const link = $("link").value.trim();
  if (!link) { $("statusHint").textContent = "请先粘贴链接"; return; }
  $("statusHint").textContent = "";
  startLink(link);
};
$("clear").onclick = () => { $("link").value = ""; $("statusHint").textContent = ""; };
$("copy").onclick = async () => {
  try { await navigator.clipboard.writeText($("text").textContent); $("copied").textContent = "已复制"; }
  catch { $("copied").textContent = "复制失败，请手动选择"; }
};
$("download").onclick = () => {
  if (currentEntry) location.href = `/api/history/${currentEntry}/download`;
  else if (currentJob) location.href = `/api/jobs/${currentJob}/download`;
};

const drop = $("drop"), fileInput = $("file");
drop.onclick = () => fileInput.click();
drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = (e) => {
  e.preventDefault(); drop.classList.remove("over");
  if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
};
fileInput.onchange = () => { if (fileInput.files.length) uploadFile(fileInput.files[0]); };

function uploadFile(file) {
  startUpload(file);
}

$("clearHist").onclick = async () => {
  if (!confirm("清空全部历史记录？文字稿会被删除，视频文件不受影响。")) return;
  const res = await fetch("/api/history", { method: "DELETE" });
  const body = await res.json().catch(() => ({}));
  currentEntry = null;
  hide("resultCard");
  await loadHistory();
  $("statusHint").textContent = `已清空 ${body.cleared ?? 0} 条记录`;
};

$("quit").onclick = async () => {
  if (!confirm("确定退出吗？退出后这个窗口就关掉了。")) return;
  $("quit").disabled = true;
  $("quit").textContent = "正在退出…";
  try { await fetch("/api/shutdown", { method: "POST" }); } catch (e) { /* 服务先关了 */ }
  document.body.innerHTML =
    '<div class="wrap"><h1 style="margin-top:60px">已退出</h1>' +
    '<p class="sub">可以直接关掉这个窗口了。</p></div>';
};

loadHistory();

fetch("/api/status").then((r) => r.json()).then((s) => {
  $("statusHint").textContent = s.model_loaded ? "模型已就绪" : "首次转写会加载模型，约需 2 秒";
}).catch(() => {});
</script>
</body>
</html>
"""


__all__ = ["INDEX_HTML", "Job", "create_app", "free_port", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
