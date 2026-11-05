"""Context layer: Story Memory Compiler + Information Firewall.

repositories -> retrieval -> ContextBuilder -> agent-safe DTOs -> agents
"""

from app.context.budget import ContextBudget, trim_events, trim_facts, tail
from app.context.builder import ContextBuilder
from app.context.knowledge_filter import KnowledgeFilter
from app.context.models import (
    ArcView,
    ChapterGuidance,
    ChapterSummaryView,
    CharacterStateView,
    CharacterView,
    ConstraintView,
    ContextDebugReport,
    ContextFact,
    ContinuityContext,
    DebtUrgency,
    DirectorContext,
    EventView,
    ForeshadowingDirectorView,
    ItemView,
    KnowledgeRowView,
    LocationView,
    MemoryExtractionContext,
    PlotDebtView,
    PlotThreadDirectorView,
    RecentContext,
    RelationshipView,
    WriterContext,
    WriterSafeForeshadowing,
    WriterSafePlotThread,
)

__all__ = [
    "ContextBuilder",
    "KnowledgeFilter",
    "ContextBudget",
    "trim_events",
    "trim_facts",
    "tail",
    "ChapterGuidance",
    "DirectorContext",
    "WriterContext",
    "ContinuityContext",
    "MemoryExtractionContext",
    "ContextFact",
    "ContextDebugReport",
    "CharacterView",
    "CharacterStateView",
    "LocationView",
    "ItemView",
    "KnowledgeRowView",
    "RelationshipView",
    "WriterSafePlotThread",
    "PlotThreadDirectorView",
    "WriterSafeForeshadowing",
    "ForeshadowingDirectorView",
    "PlotDebtView",
    "DebtUrgency",
    "ConstraintView",
    "EventView",
    "ChapterSummaryView",
    "ArcView",
    "RecentContext",
]
