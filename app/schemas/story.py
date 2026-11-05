"""Pydantic v2 API / DTO schemas for canonical story state (Phase 1).

Read models keep internal UUIDs as str for JSON friendliness.
Write models validate everything coming from users before it can reach
domain services.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import (
    AliveStatus,
    ConstraintScope,
    ConstraintType,
    SecrecyLevel,
    ThreadStatus,
    TruthStatus,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- Novel ----------


class NovelCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    genre: str | None = None
    premise: str | None = None
    style_profile: str | None = None


class NovelRead(ORMModel):
    id: uuid.UUID
    title: str
    genre: str | None
    premise: str | None
    status: str
    current_chapter_number: int


# ---------- Bootstrap world ----------


class RealmIn(BaseModel):
    name: str
    rank: int
    sublevels: list[str] = Field(default_factory=list)
    requirements: str | None = None
    capabilities: str | None = None


class PowerSystemIn(BaseModel):
    name: str
    description: str | None = None
    rules: list[str] = Field(default_factory=list)
    realms: list[RealmIn] = Field(default_factory=list)


class CharacterIn(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    gender: str | None = None
    description: str | None = None
    personality_traits: list[str] = Field(default_factory=list)
    background: str | None = None
    is_protagonist: bool = False
    realm_name: str | None = None
    subrealm: str | None = None
    location_name: str | None = None
    physical_condition: str | None = None
    current_goals: list[str] = Field(default_factory=list)


class LocationIn(BaseModel):
    name: str
    parent_name: str | None = None
    description: str | None = None


class FactionIn(BaseModel):
    name: str
    type: str | None = None
    description: str | None = None
    goals: list[str] = Field(default_factory=list)
    leader_name: str | None = None


class ItemIn(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    type: str | None = None
    grade: str | None = None
    description: str | None = None
    owner_name: str | None = None
    location_name: str | None = None
    abilities: list[str] = Field(default_factory=list)
    restrictions: list[str] = Field(default_factory=list)


class StoryFactIn(BaseModel):
    statement: str
    truth_status: TruthStatus = TruthStatus.true
    secrecy: SecrecyLevel = SecrecyLevel.public
    category: str | None = None
    known_by: list[str] = Field(
        default_factory=list,
        description="Character names that already know this fact",
    )
    suspected_by: list[str] = Field(default_factory=list)


class PlotThreadIn(BaseModel):
    title: str
    thread_type: str | None = None
    status: ThreadStatus = ThreadStatus.active
    truth: str | None = None
    public_summary: str | None = None
    priority: float = 0.5


class PlotDebtIn(BaseModel):
    debt_type: str | None = None
    description: str
    created_chapter: int = 0
    due_chapter: int | None = None
    due_story_time: str | None = None


class ArcIn(BaseModel):
    title: str
    start_chapter: int = 1
    planned_end_chapter: int | None = None
    goal: str | None = None
    major_beats: list[str] = Field(default_factory=list)


class ConstraintIn(BaseModel):
    type: ConstraintType
    scope: ConstraintScope = ConstraintScope.novel
    statement: str
    start_chapter: int | None = None
    end_chapter: int | None = None


class BootstrapWorldRequest(BaseModel):
    """Initialize the canonical world of a novel.

    Mirrors ``examples/bootstrap_world.json`` plus locations/items/facts.
    """

    power_system: PowerSystemIn
    characters: list[CharacterIn] = Field(default_factory=list)
    locations: list[LocationIn] = Field(default_factory=list)
    factions: list[FactionIn] = Field(default_factory=list)
    items: list[ItemIn] = Field(default_factory=list)
    initial_facts: list[StoryFactIn] = Field(default_factory=list)
    plot_threads: list[PlotThreadIn] = Field(default_factory=list)
    plot_debts: list[PlotDebtIn] = Field(default_factory=list)
    arc: ArcIn | None = None
    constraints: list[ConstraintIn] = Field(default_factory=list)


# ---------- State view ----------


class CharacterStateRead(ORMModel):
    chapter_number: int
    alive_status: str
    realm_name: str | None = None
    subrealm: str | None = None
    location_name: str | None = None
    physical_condition: str | None = None
    mental_condition: str | None = None
    current_goals: list[str] = Field(default_factory=list)


class CharacterRead(ORMModel):
    id: uuid.UUID
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str | None = None
    personality_traits: list[str] = Field(default_factory=list)
    is_protagonist: bool
    current_state: CharacterStateRead | None = None


class PlotThreadRead(ORMModel):
    id: uuid.UUID
    title: str
    status: str
    public_summary: str | None
    priority: float
    last_advanced_chapter: int | None


class ConstraintRead(ORMModel):
    id: uuid.UUID
    type: str
    scope: str
    statement: str
    active: bool


class PlotDebtRead(ORMModel):
    id: uuid.UUID
    description: str
    status: str
    created_chapter: int
    due_chapter: int | None


class StoryStateView(BaseModel):
    """Projection of canonical state for UI / Director consumption.

    ``view=reader`` filters hidden truths; ``view=author`` includes them.
    """

    novel_id: str
    current_chapter_number: int
    characters: list[CharacterRead] = Field(default_factory=list)
    plot_threads: list[PlotThreadRead] = Field(default_factory=list)
    plot_debts: list[PlotDebtRead] = Field(default_factory=list)
    constraints: list[ConstraintRead] = Field(default_factory=list)
    hidden_facts: list[str] = Field(default_factory=list)
