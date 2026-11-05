"""Model package: import all modules so Base.metadata is complete."""

from app.models.base import Base
from app.models.chapter import Chapter, ChapterSummary, StorySnapshot
from app.models.character import (
    Character,
    CharacterKnowledge,
    CharacterState,
    Relationship,
)
from app.models.enums import (
    AliveStatus,
    ArcStatus,
    ChapterStatus,
    ConstraintScope,
    ConstraintSource,
    ConstraintType,
    DebtStatus,
    EventTargetType,
    ForeshadowStatus,
    ForeshadowVisibility,
    ItemStatus,
    KnowledgeStatus,
    NovelStatus,
    SecrecyLevel,
    Severity,
    ThreadStatus,
    TruthStatus,
)
from app.models.event import Event, EventEffect
from app.models.fact import StoryFact
from app.models.item import Item
from app.models.location import Faction, Location
from app.models.novel import Novel
from app.models.commit import ChapterCommit
from app.models.plot import Arc, Constraint, Foreshadowing, PlotDebt, PlotThread
from app.models.power import Ability, PowerSystem, Realm

__all__ = [
    "Base",
    "Novel",
    "Character",
    "CharacterState",
    "CharacterKnowledge",
    "Relationship",
    "StoryFact",
    "PowerSystem",
    "Realm",
    "Ability",
    "Item",
    "Location",
    "Faction",
    "Event",
    "EventEffect",
    "Arc",
    "PlotThread",
    "Foreshadowing",
    "PlotDebt",
    "Constraint",
    "Chapter",
    "ChapterSummary",
    "StorySnapshot",
    "ChapterCommit",
    # enums
    "NovelStatus",
    "AliveStatus",
    "KnowledgeStatus",
    "TruthStatus",
    "SecrecyLevel",
    "ItemStatus",
    "EventTargetType",
    "ThreadStatus",
    "ForeshadowStatus",
    "ForeshadowVisibility",
    "DebtStatus",
    "ConstraintType",
    "ConstraintScope",
    "ConstraintSource",
    "ArcStatus",
    "ChapterStatus",
    "FactionStatus",
    "Severity",
]
