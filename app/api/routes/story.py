"""HTTP routes: health, novels, story state (Phase 0 + 1 surface)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Novel
from app.schemas.story import (
    BootstrapWorldRequest,
    CharacterRead,
    CharacterStateRead,
    ConstraintIn,
    ConstraintRead,
    NovelCreate,
    NovelRead,
    PlotDebtRead,
    PlotThreadRead,
    StoryFactIn,
    StoryStateView,
)
from app.services.story_state import StoryStateError, StoryStateService

router = APIRouter()


async def _load_novel_or_404(novel_id: uuid.UUID, session: AsyncSession) -> Novel:
    novel = await session.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(status_code=404, detail=f"Novel {novel_id} not found")
    return novel


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/novels", response_model=NovelRead, status_code=201)
async def create_novel(payload: NovelCreate, session: AsyncSession = Depends(get_session)):
    service = StoryStateService(session)
    novel = await service.create_novel(
        title=payload.title,
        genre=payload.genre,
        premise=payload.premise,
        style_profile=payload.style_profile,
    )
    await session.commit()
    return novel


@router.get("/novels/{novel_id}", response_model=NovelRead)
async def get_novel(novel_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    return await _load_novel_or_404(novel_id, session)


@router.post("/novels/{novel_id}/bootstrap", response_model=dict, status_code=201)
async def bootstrap_world(
    novel_id: uuid.UUID,
    payload: BootstrapWorldRequest,
    session: AsyncSession = Depends(get_session),
):
    await _load_novel_or_404(novel_id, session)
    service = StoryStateService(session)
    try:
        result = await service.bootstrap_world(novel_id, payload)
    except StoryStateError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await session.commit()
    return {"novel_id": str(novel_id), "bootstrap": "ok"}


@router.get("/novels/{novel_id}/characters", response_model=list[CharacterRead])
async def list_characters(
    novel_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    await _load_novel_or_404(novel_id, session)
    service = StoryStateService(session)
    out: list[CharacterRead] = []
    for character in await service.characters.list_for_novel(novel_id):
        state = await service.characters.current_state(character.id)
        state_read = None
        if state is not None:
            state_read = CharacterStateRead(
                chapter_number=state.chapter_number,
                alive_status=state.alive_status.value,
                realm_name=state.realm.name if state.realm else None,
                subrealm=state.subrealm,
                location_name=state.location.name if state.location else None,
                physical_condition=state.physical_condition,
                mental_condition=state.mental_condition,
                current_goals=state.current_goals,
            )
        out.append(
            CharacterRead(
                id=character.id,
                name=character.name,
                aliases=character.aliases,
                description=character.description,
                personality_traits=character.personality_traits,
                is_protagonist=character.is_protagonist,
                current_state=state_read,
            )
        )
    return out


@router.get("/novels/{novel_id}/state", response_model=StoryStateView)
async def get_state(
    novel_id: uuid.UUID,
    view: str = Query(default="author", pattern="^(author|reader)$"),
    session: AsyncSession = Depends(get_session),
):
    novel = await _load_novel_or_404(novel_id, session)
    service = StoryStateService(session)
    characters = await list_characters(novel_id, session)
    threads = [
        PlotThreadRead.model_validate(t)
        for t in await service.threads.list_threads(novel_id)
    ]
    debts = [
        PlotDebtRead.model_validate(d) for d in await service.debts.list_open(novel_id)
    ]
    constraints = [
        ConstraintRead.model_validate(c)
        for c in await service.constraints.list_active(novel_id)
    ]
    hidden_facts: list[str] = []
    if view == "author":
        facts = await service.facts.list_for_novel(novel_id)
        hidden_facts = [
            f.statement
            for f in facts
            if f.secrecy in ("secret", "top_secret")
        ]

    return StoryStateView(
        novel_id=str(novel.id),
        current_chapter_number=novel.current_chapter_number,
        characters=characters,
        plot_threads=threads,
        plot_debts=debts,
        constraints=constraints,
        hidden_facts=hidden_facts if view == "author" else [],
    )


@router.get("/novels/{novel_id}/plot-threads", response_model=list[PlotThreadRead])
async def list_plot_threads(
    novel_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    await _load_novel_or_404(novel_id, session)
    service = StoryStateService(session)
    return [PlotThreadRead.model_validate(t) for t in await service.threads.list_threads(novel_id)]


@router.post("/novels/{novel_id}/constraints", response_model=ConstraintRead, status_code=201)
async def add_constraint(
    novel_id: uuid.UUID,
    payload: ConstraintIn,
    session: AsyncSession = Depends(get_session),
):
    await _load_novel_or_404(novel_id, session)
    service = StoryStateService(session)
    from app.models import Constraint as ConstraintModel

    constraint = ConstraintModel(
        novel_id=novel_id,
        type=payload.type,
        scope=payload.scope,
        statement=payload.statement,
        start_chapter=payload.start_chapter,
        end_chapter=payload.end_chapter,
    )
    session.add(constraint)
    await session.commit()
    await session.refresh(constraint)
    return ConstraintRead.model_validate(constraint)


@router.post("/novels/{novel_id}/facts", status_code=201)
async def add_fact(
    novel_id: uuid.UUID,
    payload: StoryFactIn,
    session: AsyncSession = Depends(get_session),
):
    await _load_novel_or_404(novel_id, session)
    service = StoryStateService(session)
    fact = await service.add_fact(
        novel_id,
        payload.statement,
        payload.secrecy,
        truth_status=payload.truth_status,
    )
    # grant knowledge to named characters
    characters = await service.characters.list_for_novel(novel_id)
    by_name = {c.name: c for c in characters}
    from app.models.enums import KnowledgeStatus as KS

    for name in payload.known_by:
        if name not in by_name:
            await session.rollback()
            raise HTTPException(status_code=422, detail=f"Unknown character {name!r}")
        await service.grant_knowledge(by_name[name].id, fact.id, status=KS.known)
    for name in payload.suspected_by:
        if name not in by_name:
            await session.rollback()
            raise HTTPException(status_code=422, detail=f"Unknown character {name!r}")
        await service.grant_knowledge(by_name[name].id, fact.id, status=KS.suspected, confidence=0.4)
    await session.commit()
    return {"fact_id": str(fact.id), "statement": fact.statement, "secrecy": fact.secrecy.value}
