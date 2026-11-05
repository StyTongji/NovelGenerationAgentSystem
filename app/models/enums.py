"""Shared enums for canonical story state.

All enums are plain ``str`` enums stored as VARCHAR so SQLite and PostgreSQL
behave identically, and so LLM-facing JSON never leaks numeric codes.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """Backport-safe string enum base (Python 3.12 has StrEnum; keep alias)."""


class NovelStatus(StrEnum):
    drafting = "drafting"
    ongoing = "ongoing"
    completed = "completed"
    archived = "archived"


class AliveStatus(StrEnum):
    alive = "alive"
    dead = "dead"
    missing = "missing"
    sealed = "sealed"
    transformed = "transformed"
    unknown = "unknown"


class KnowledgeStatus(StrEnum):
    known = "known"
    suspected = "suspected"
    believed_false = "believed_false"
    forgotten = "forgotten"


class TruthStatus(StrEnum):
    true = "true"
    false = "false"
    unverified = "unverified"
    contested = "contested"


class SecrecyLevel(StrEnum):
    public = "public"
    limited = "limited"
    secret = "secret"
    top_secret = "top_secret"


class ItemStatus(StrEnum):
    intact = "intact"
    damaged = "damaged"
    destroyed = "destroyed"
    lost = "lost"
    sealed = "sealed"
    dormant = "dormant"


class EventTargetType(StrEnum):
    character = "character"
    item = "item"
    relationship = "relationship"
    knowledge = "knowledge"
    faction = "faction"
    location = "location"
    plot_thread = "plot_thread"
    foreshadowing = "foreshadowing"
    plot_debt = "plot_debt"
    other = "other"


class ThreadStatus(StrEnum):
    planned = "planned"
    active = "active"
    progressing = "progressing"
    dormant = "dormant"
    resolved = "resolved"
    abandoned = "abandoned"


class ForeshadowStatus(StrEnum):
    planned = "planned"
    planted = "planted"
    reinforced = "reinforced"
    paid_off = "paid_off"
    cancelled = "cancelled"


class ForeshadowVisibility(StrEnum):
    public = "public"
    hidden = "hidden"


class DebtStatus(StrEnum):
    open = "open"
    fulfilled = "fulfilled"
    broken = "broken"
    cancelled = "cancelled"


class ConstraintType(StrEnum):
    hard = "hard"
    soft = "soft"


class ConstraintScope(StrEnum):
    novel = "novel"
    arc = "arc"
    character = "character"
    plot_thread = "plot_thread"


class ConstraintSource(StrEnum):
    user = "user"
    system = "system"


class ArcStatus(StrEnum):
    planned = "planned"
    active = "active"
    completed = "completed"
    abandoned = "abandoned"


class ChapterStatus(StrEnum):
    draft = "draft"
    validated = "validated"
    preview = "preview"
    final = "final"
    canonical = "canonical"
    superseded = "superseded"


class FactionStatus(StrEnum):
    active = "active"
    declining = "declining"
    dissolved = "dissolved"
    hidden = "hidden"


# severity is defined here so models/API/agents share one source of truth
class Severity(StrEnum):
    info = "info"
    warning = "warning"
    error = "error"
    critical = "critical"
