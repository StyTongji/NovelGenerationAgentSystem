"""Local real-LLM trial page and its streamed preview endpoint."""

from __future__ import annotations

import asyncio
import anyio
import json
import os
import sys
import uuid
from contextlib import aclosing
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import AnyHttpUrl, BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[3]
PAGE_FILE = REPO_ROOT / "app" / "static" / "trial.html"

page_router = APIRouter()
api_router = APIRouter()

CHILD_STOP_TIMEOUT = 3.0


async def _stop_child(process) -> None:
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), CHILD_STOP_TIMEOUT)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


class TrialPreviewRequest(BaseModel):
    base_url: AnyHttpUrl
    api_key: str = Field(min_length=1, max_length=512)
    model: str = Field(min_length=1, max_length=128)


def _event(name: str, payload: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _command_output(args: list[str], env: dict[str, str]) -> AsyncIterator[str]:
    process = await asyncio.create_subprocess_exec(
        sys.executable, *args,
        cwd=REPO_ROOT,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert process.stdout is not None
    try:
        while line := await process.stdout.readline():
            yield line.decode("utf-8", errors="replace").rstrip()
        if await process.wait():
            raise RuntimeError(f"命令退出，状态码 {process.returncode}")
    finally:
        # Starlette uses level cancellation (AnyIO): asyncio.shield alone
        # doesn't protect the await in a cancelled task-group scope.
        with anyio.CancelScope(shield=True):
            cleanup = asyncio.create_task(_stop_child(process))
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError


@page_router.get("/trial", include_in_schema=False)
async def trial_page() -> FileResponse:
    return FileResponse(PAGE_FILE, media_type="text/html")


@api_router.post("/trial/preview")
async def start_trial_preview(payload: TrialPreviewRequest) -> StreamingResponse:
    """Create a fresh trial and stream its subprocess output as SSE.

    The supplied key only exists in this subprocess environment; it is not
    included in its arguments, run state, artifacts, or SSE payload.
    """
    run_id = f"ui_{datetime.now(UTC):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    base_url = str(payload.base_url).rstrip("/")
    env = {
        **os.environ,
        "NOVEL_LLM_API_KEY": payload.api_key,
        "NOVEL_LLM_BASE_URL": base_url,
        "PYTHONUNBUFFERED": "1",
    }

    async def stream() -> AsyncIterator[str]:
        yield _event("status", {"message": "正在创建试验世界", "run_id": run_id})
        try:
            async with aclosing(_command_output(
                ["scripts/real_llm_trial.py", "init", "--run", run_id,
                 "--model", payload.model, "--base-url", base_url], env,
            )) as output:
                async for line in output:
                    yield _event("log", {"line": line})

            yield _event("status", {"message": "正在生成第 1 章 preview", "run_id": run_id})
            async with aclosing(_command_output(
                ["scripts/real_llm_trial.py", "preview", "--run", run_id, "--attempt", "initial"], env,
            )) as output:
                async for line in output:
                    yield _event("log", {"line": line})

            chapter = REPO_ROOT / "real_trial_runs" / run_id / "chapter_01" / "attempts" / "initial" / "chapter.txt"
            if not chapter.exists():
                raise RuntimeError("Preview 完成但未找到 chapter.txt")
            yield _event("chapter", {"run_id": run_id, "text": chapter.read_text(encoding="utf-8")})
            yield _event("done", {"message": "Preview 已完成，可在下方阅读正文。", "run_id": run_id})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield _event("error", {"message": str(exc), "run_id": run_id})

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
