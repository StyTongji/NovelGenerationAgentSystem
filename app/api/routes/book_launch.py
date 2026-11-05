"""Market-to-book controls for the existing Studio (local, single-user)."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.routes.studio import get_studio, _action
from app.schemas.book_launch import LaunchStart, TopicSelection, BlueprintApproval, LaunchBudget, ResearchForkInput, BlueprintRevision
from app.schemas.studio import StartRunInput, RunInput, RuntimeCredentialInput
from app.services.book_launch import BookLaunchManager
from app.services.market_sources import import_sample, collect_fanqie

router = APIRouter(prefix="/book-launch")
page_router = APIRouter()
STATIC = Path(__file__).resolve().parents[2] / "static"


async def get_launch(request: Request, studio=Depends(get_studio)):
    # No await between check and construction: shared by requests in one worker.
    if not hasattr(request.app.state, "book_launch"):
        request.app.state.book_launch = BookLaunchManager(studio)
    return request.app.state.book_launch


@page_router.get("/book-launch", include_in_schema=False)
async def page():
    return FileResponse(STATIC / "book_launch.html")


class ImportInput(BaseModel):
    text: str = Field(max_length=1_000_000)
    format: str = "json"


class CollectInput(BaseModel):
    categories: list[int] = Field(default_factory=lambda: [258, 257], min_length=1, max_length=2)
    rank_type: int = 2
    limit: int = Field(default=10, ge=1, le=10)


@router.post("/sources/import")
async def import_sources(payload: ImportInput):
    try:
        return import_sample(payload.text, payload.format)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/sources/fanqie")
async def collect(payload: CollectInput):
    import httpx
    try:
        return await collect_fanqie(payload.categories, payload.rank_type, payload.limit)
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/demo-sample")
async def demo_sample():
    from app.services.book_launch_demo import demo_sample
    return demo_sample()


@router.get("")
async def sessions(manager=Depends(get_launch)):
    return sorted(manager.sessions.values(), key=lambda s: s.created_at, reverse=True)


@router.post("", status_code=201)
async def create(payload: LaunchStart, manager=Depends(get_launch)):
    return await _action(manager.create(payload))


def find(manager, id):
    try:
        return manager.get(id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(404, "开书规划不存在") from exc


@router.get("/{id}")
async def detail(id: str, manager=Depends(get_launch)):
    item = find(manager, id)
    return {"session": item, "events": manager.events(id), "budget": manager.budget(item).summary()}


@router.post("/{id}/select")
async def select(id: str, payload: TopicSelection, manager=Depends(get_launch)):
    find(manager, id)
    return await _action(manager.select(id, payload))


@router.post("/{id}/fork-research", status_code=201)
async def fork_research(id: str, payload: ResearchForkInput, manager=Depends(get_launch)):
    find(manager, id)
    return await _action(manager.fork_research(id, payload))


@router.post("/{id}/approve")
async def approve(id: str, payload: BlueprintApproval, manager=Depends(get_launch)):
    find(manager, id)
    return await _action(manager.approve(id, payload))


@router.post("/{id}/revise-blueprint")
async def revise_blueprint(id: str, payload: BlueprintRevision, manager=Depends(get_launch)):
    find(manager, id)
    return await _action(manager.revise_blueprint(id, payload))


@router.post("/{id}/resume")
async def resume(id: str, payload: RuntimeCredentialInput | None = None, manager=Depends(get_launch)):
    find(manager, id)
    return await _action(manager.resume(id, payload.api_key if payload else None))


@router.post("/{id}/cancel")
async def cancel(id: str, manager=Depends(get_launch)):
    find(manager, id)
    return await _action(manager.cancel(id))


@router.post("/{id}/chapters")
async def chapters(id: str, payload: StartRunInput, manager=Depends(get_launch)):
    find(manager, id)
    return await _action(manager.start_chapters(id, RunInput.model_validate(payload.model_dump()), payload.api_key))


@router.post("/{id}/budget")
async def budget(id: str, payload: LaunchBudget, manager=Depends(get_launch)):
    find(manager, id)
    return await _action(manager.update_budget(id, payload))
