"""Studio HTTP control plane and replayable event stream (local single-user)."""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import select

from app.commit.models import CommitError
from app.config import get_settings
from app.db import get_engine, get_sessionmaker
from app.models import Base, Novel
from app.schemas.studio import ApprovalInput, BudgetLimits, MemoryEditInput, RecheckInput, RevisionInput, RunInput, StartRunInput, RuntimeCredentialInput, ReviewAssistanceInput
from app.services.studio import StudioManager
from app.services.studio_seed import StudioNovelInput, create_studio_novel

router = APIRouter(prefix="/studio")
page_router = APIRouter()
STATIC = Path(__file__).resolve().parents[2] / "static"


async def get_studio(request: Request) -> StudioManager:
    if not hasattr(request.app.state, "studio_init_lock"):
        request.app.state.studio_init_lock = asyncio.Lock()
    async with request.app.state.studio_init_lock:
        if not hasattr(request.app.state, "studio"):
            async with get_engine().begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            settings = get_settings()
            request.app.state.studio = StudioManager(Path(settings.studio_directory), get_sessionmaker(), settings)
    return request.app.state.studio


@page_router.get("/", include_in_schema=False)
@page_router.get("/studio", include_in_schema=False)
async def studio_page():
    return FileResponse(STATIC / "studio.html", media_type="text/html")


@page_router.get("/static/{filename}", include_in_schema=False)
async def studio_asset(filename: str):
    if filename not in {"studio.css", "studio.js", "book_launch.js", "book_launch.css"}:
        raise HTTPException(404)
    return FileResponse(STATIC / filename)


def _novel(novel):
    return {"id": str(novel.id), "title": novel.title, "genre": novel.genre,
            "synopsis": novel.premise, "current_chapter_number": novel.current_chapter_number}


@router.get("/bootstrap")
async def bootstrap(manager: StudioManager = Depends(get_studio)):
    from app.workflow.chapter import workflow_metadata
    async with manager.session_factory() as session:
        novels = (await session.scalars(select(Novel).order_by(Novel.created_at.desc()))).all()
    workflow = workflow_metadata()
    return {"novels": [_novel(n) for n in novels],
            "runs": sorted(manager.runs.values(), key=lambda r: r.created_at, reverse=True),
            "providers": manager.providers(), "roles": [{**a, "label": a["name"], "description": a["goal"]} for a in workflow["agents"]],
            "graph": workflow, "framework": "LangGraph"}


@router.post("/novels", status_code=201)
async def create_novel(payload: StudioNovelInput, manager: StudioManager = Depends(get_studio)):
    async with manager.session_factory() as session:
        novel = await create_studio_novel(session, payload)
        await session.commit()
        return _novel(novel)


@router.get("/novels/{novel_id}/state")
async def state(novel_id: uuid.UUID, manager: StudioManager = Depends(get_studio)):
    from app.api.routes.story import get_state
    from app.models.item import Item
    from app.models import CharacterKnowledge, StoryFact
    async with manager.session_factory() as session:
        result = (await get_state(novel_id, "author", session)).model_dump(mode="json")
        result["items"] = [{"id": str(item.id), "name": item.name,
                             "owner_id": str(item.current_owner_character_id) if item.current_owner_character_id else None}
                            for item in await session.scalars(select(Item).where(Item.novel_id == novel_id))]
        facts = (await session.scalars(select(StoryFact).where(StoryFact.novel_id == novel_id))).all()
        result["facts"] = [{"id": str(f.id), "statement": f.statement, "secrecy": f.secrecy.value} for f in facts]
        result["knowledge"] = [{"character_id": str(k.character_id), "fact_id": str(k.fact_id), "status": k.status.value}
            for k in await session.scalars(select(CharacterKnowledge).where(CharacterKnowledge.fact_id.in_([f.id for f in facts])))]
        return result


async def _action(awaitable):
    try:
        return await awaitable
    except CommitError as exc:
        raise HTTPException(422, {"code": exc.code.value, "message": exc.message}) from exc
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except (KeyError, StopIteration) as exc:
        raise HTTPException(404, "任务或章节不存在。") from exc


@router.post("/runs", status_code=201)
async def start_run(payload: StartRunInput, manager: StudioManager = Depends(get_studio)):
    return await _action(manager.create(RunInput.model_validate(payload.model_dump()), api_key=payload.api_key,
                                        replace_existing=payload.replace_existing))


@router.post("/runs/{run_id}/credentials")
async def provide_credentials(run_id: str, payload: RuntimeCredentialInput, manager: StudioManager = Depends(get_studio)):
    try:
        return manager.provide_credentials(run_id, payload.api_key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "任务不存在。") from exc


@router.get("/runs/{run_id}")
async def get_run(run_id: str, manager: StudioManager = Depends(get_studio)):
    try:
        return manager.get(run_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(404, "任务不存在。") from exc


@router.get("/runs/{run_id}/chapters/{number}")
async def chapter(run_id: str, number: int, manager: StudioManager = Depends(get_studio)):
    try:
        return manager.chapter_detail(run_id, number)
    except (KeyError, ValueError, StopIteration) as exc:
        raise HTTPException(404, "章节不存在。") from exc


@router.post("/runs/{run_id}/cancel")
async def cancel(run_id: str, manager: StudioManager = Depends(get_studio)):
    return await _action(manager.cancel(run_id))


@router.get("/runs/{run_id}/budget")
async def budget(run_id: str, manager: StudioManager = Depends(get_studio)):
    await get_run(run_id, manager)
    return manager.budget_summary(run_id)


@router.post("/runs/{run_id}/budget")
async def update_budget(run_id: str, payload: BudgetLimits, manager: StudioManager = Depends(get_studio)):
    return await _action(manager.update_budget(run_id, payload))


@router.post("/runs/{run_id}/resume")
async def resume(run_id: str, manager: StudioManager = Depends(get_studio)):
    return await _action(manager.resume(run_id))


@router.post("/runs/{run_id}/finish")
async def finish(run_id: str, manager: StudioManager = Depends(get_studio)):
    return await _action(manager.finish(run_id))


@router.post("/runs/{run_id}/chapters/{number}/assist-review")
async def assist_review(run_id: str, number: int, payload: ReviewAssistanceInput = ReviewAssistanceInput(), manager: StudioManager = Depends(get_studio)):
    return await _action(manager.assist_review(run_id, number, model=payload.model, max_output_tokens=payload.max_output_tokens))


@router.post("/runs/{run_id}/chapters/{number}/approve")
async def approve(run_id: str, number: int, payload: ApprovalInput, manager: StudioManager = Depends(get_studio)):
    return await _action(manager.approve(run_id, number, payload))


@router.post("/runs/{run_id}/chapters/{number}/repair-from-review")
async def repair_from_review(run_id: str, number: int, manager: StudioManager = Depends(get_studio)):
    return await _action(manager.repair_from_review(run_id, number))


@router.post("/runs/{run_id}/chapters/{number}/revise")
async def revise(run_id: str, number: int, payload: RevisionInput, manager: StudioManager = Depends(get_studio)):
    return await _action(manager.new_candidate(run_id, number, "revise", payload.model_dump()))


@router.post("/runs/{run_id}/chapters/{number}/memory")
async def memory_edit(run_id: str, number: int, payload: MemoryEditInput, manager: StudioManager = Depends(get_studio)):
    return await _action(manager.new_candidate(run_id, number, "memory", payload.model_dump(mode="json")))


@router.post("/runs/{run_id}/chapters/{number}/memory-retry")
async def memory_retry(run_id: str, number: int, manager: StudioManager = Depends(get_studio)):
    return await _action(manager.new_candidate(run_id, number, "memory_retry", {}))


@router.post("/runs/{run_id}/chapters/{number}/recheck")
async def recheck(run_id: str, number: int, payload: RecheckInput, manager: StudioManager = Depends(get_studio)):
    return await _action(manager.new_candidate(run_id, number, "recheck", {}, runtime=payload))


@router.get("/runs/{run_id}/export")
async def export(run_id: str, manager: StudioManager = Depends(get_studio)):
    run = await get_run(run_id, manager)
    lines = [run.title, "离线脚本演示（非真实模型生成）" if run.mode == "demo" else "模型生成 · 作者审阅工作稿"]
    for ch in run.chapters:
        detail = manager.chapter_detail(run_id, ch.number)
        if detail["package"]:
            lines.extend([f"\n第 {ch.number} 章 [{ch.status}]", detail["package"]["draft"]["full_text"]])
        else:
            partial = detail["partial"]
            draft = (partial.get("repair") or {}).get("draft") or partial.get("draft") or {}
            text = draft.get("full_text") or "\n\n".join(
                value.get("text", "") for key, value in sorted(partial.items(),
                    key=lambda item: int(item[0].split("_")[1]) if item[0].startswith("scene_") and item[0][6:].isdigit() else -1)
                if key.startswith("scene_") and key[6:].isdigit() and isinstance(value, dict))
            if text:
                lines.extend([f"\n第 {ch.number} 章 [{ch.status} · 未完成草稿]", text])
    return Response("\n\n".join(lines), media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="novel-{run.id[:8]}.txt"'})


@router.get("/runs/{run_id}/events")
async def events(run_id: str, request: Request, after: int = 0, manager: StudioManager = Depends(get_studio)):
    run = await get_run(run_id, manager)
    try:
        cursor = max(after, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        cursor = after

    async def stream():
        nonlocal cursor
        observed_task = manager.tasks.get(run_id)
        normal_exit = False
        manager.subscribers[run_id] = manager.subscribers.get(run_id, 0) + 1
        try:
            while True:
                for event in manager.events(run_id, cursor):
                    cursor = event["sequence"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if await request.is_disconnected():
                    break
                if run.status != "running":
                    normal_exit = True
                    yield f'data: {json.dumps({"type": "idle", "status": run.status, "message": "事件已同步"}, ensure_ascii=False)}\n\n'
                    break
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)
        finally:
            manager.subscribers[run_id] -= 1
            # Disconnecting the last live viewer cancels and awaits its API task.
            # Checkpoints allow explicit resume; no orphan calls after browser exit.
            if (not normal_exit and not manager.subscribers[run_id] and run.status == "running"
                    and manager.tasks.get(run_id) is observed_task):
                import anyio
                with anyio.CancelScope(shield=True):
                    await manager.cancel(run_id, expected_task=observed_task)

    return StreamingResponse(stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
