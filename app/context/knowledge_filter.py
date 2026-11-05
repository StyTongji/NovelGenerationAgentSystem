"""Character Knowledge Firewall — the information-isolation core.

This filter sits at the FINAL CONTEXT ASSEMBLY boundary, not inside a
repository. Every text that could reach a POV WriterContext is scrubbed
here, regardless of which canonical table it came from:

- StoryFact statements (secret / top_secret)
- Event summaries + tagged secret_fact_ids in event metadata
- ChapterSummary / Arc / Novel summaries (author-level: excluded wholesale)
- PlotThread.truth (Writer only ever sees public_summary, also scrubbed)
- Foreshadowing.intended_payoff (never writer-visible)
- Item / Location secret references (never serialized into views)

Knowledge-status semantics:
- known          -> certain knowledge, stated as-is
- suspected      -> "POV suspects ... (unconfirmed)"; never asserted as fact
- believed_false -> POV's (wrong) belief is provided, framed as belief
- forgotten      -> excluded by default

Reader visibility (fact.revealed_to_reader_chapter) is independent of
character knowledge and must never relax the POV filter.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.context.models import ContextDebugReport, ContextFact
from app.models import CharacterKnowledge, StoryFact
from app.models.enums import KnowledgeStatus, SecrecyLevel
from app.repositories.character import CharacterRepository
from app.repositories.novel import FactRepository

_MASK = "……（POV未知信息）……"


class KnowledgeFilter:
    """Per-POV information firewall. One instance per build."""

    def __init__(self, session: AsyncSession, novel_id: uuid.UUID) -> None:
        self.session = session
        self.novel_id = novel_id
        self.characters = CharacterRepository(session)
        self.facts_repo = FactRepository(session)
        self._all_facts: list[StoryFact] = []
        self._pov_knowledge: dict[uuid.UUID, CharacterKnowledge] = {}

    async def load(self, pov_character_id: uuid.UUID) -> None:
        self._all_facts = await self.facts_repo.list_for_novel(self.novel_id)
        rows = await self.characters.knowledge_of(pov_character_id)
        self._pov_knowledge = {row.fact_id: row for row in rows}

    # ------------------------------------------------------------ predicates

    @staticmethod
    def _enum_value(field: object) -> str:
        return field.value if hasattr(field, "value") else str(field)

    def _is_secret(self, fact: StoryFact) -> bool:
        return self._enum_value(fact.secrecy) in (
            SecrecyLevel.secret.value,
            SecrecyLevel.top_secret.value,
        )

    def _aware_status(self, fact: StoryFact) -> KnowledgeStatus | None:
        row = self._pov_knowledge.get(fact.id)
        return row.status if row is not None else None

    def pov_is_aware(self, fact: StoryFact) -> bool:
        """known / suspected / believed_false all mean the POV is aware the
        proposition exists (framing differs); forgotten means unaware."""
        return self._aware_status(fact) != KnowledgeStatus.forgotten and (
            self._aware_status(fact) is not None
        )

    # ------------------------------------------------------- text scrubbing

    def scrub_text(self, text: str) -> tuple[str, list[str]]:
        """Mask secret statements the POV does not know in arbitrary text.

        Two detection layers:
        1. explicit tagging: sources may reference secret_fact_ids
           (handled by callers via ``scrub_tagged``)
        2. substring match of the secret statement itself

        Returns (scrubbed_text, masked_statements).
        """
        masked: list[str] = []
        scrubbed = text
        for fact in self._all_facts:
            if not self._is_secret(fact):
                continue
            if self.pov_is_aware(fact):
                continue
            if fact.statement and fact.statement in scrubbed:
                scrubbed = scrubbed.replace(fact.statement, _MASK)
                masked.append(fact.statement)
        return scrubbed, masked

    def unknown_secret_ids(self, secret_fact_ids: list[str]) -> list[str]:
        """Filter tagged secret ids down to ones the POV does NOT know."""
        ids_by_str = {str(f.id): f for f in self._all_facts}
        unknown = []
        for fact_id in secret_fact_ids:
            fact = ids_by_str.get(fact_id)
            if fact is not None and self._is_secret(fact) and not self.pov_is_aware(fact):
                unknown.append(fact_id)
        return unknown

    # -------------------------------------------------------- pov knowledge

    async def pov_knowledge(self, debug: ContextDebugReport) -> list[ContextFact]:
        """Everything the POV legitimately knows, with status framing."""
        out: list[ContextFact] = []
        for fact in self._all_facts:
            # forgotten overrides even public/limited facts: the character
            # no longer holds this knowledge
            if self._aware_status(fact) == KnowledgeStatus.forgotten:
                debug.exclude(
                    "story_fact", str(fact.id), fact.statement,
                    "POV has forgotten this fact (forgotten)",
                )
                continue
            secrecy_value = self._enum_value(fact.secrecy)
            if secrecy_value in (SecrecyLevel.public.value, SecrecyLevel.limited.value):
                out.append(
                    ContextFact(
                        content=fact.statement,
                        source_type="story_fact",
                        source_id=str(fact.id),
                        reason=f"{secrecy_value} fact",
                    )
                )
                debug.include("story_fact", str(fact.id), fact.statement, f"{secrecy_value} fact")
                continue

            status = self._aware_status(fact)
            status_value = self._enum_value(status) if status is not None else None
            if status_value == KnowledgeStatus.known.value:
                content = fact.statement
                reason = "POV knows this (known)"
            elif status_value == KnowledgeStatus.suspected.value:
                content = f"POV角色怀疑以下情况可能属实，但未经确认：{fact.statement}"
                reason = "POV suspects this (suspected) — never assert as fact"
            elif status_value == KnowledgeStatus.believed_false.value:
                content = f"POV角色认为以下说法是假的（这是角色的错误认知，只作为角色认知呈现）：{fact.statement}"
                reason = "POV believes this false (believed_false)"
            else:
                debug.exclude(
                    "story_fact", str(fact.id), fact.statement,
                    "POV does not know this secret" if self._is_secret(fact)
                    else "POV has no knowledge of this fact",
                )
                continue
            out.append(
                ContextFact(
                    content=content,
                    source_type="story_fact",
                    source_id=str(fact.id),
                    reason=reason,
                )
            )
            debug.include("story_fact", str(fact.id), fact.statement, reason)
        return out

    # ------------------------------------------------------- director views

    async def planning_secrets(self, debug: ContextDebugReport, cap: int = 10) -> list[ContextFact]:
        """Author-level secrets for the Director (planning needs truth)."""
        out: list[ContextFact] = []
        for fact in self._all_facts:
            if self._is_secret(fact):
                out.append(
                    ContextFact(
                        content=fact.statement,
                        source_type="story_fact",
                        source_id=str(fact.id),
                        reason="director planning secret",
                    )
                )
                debug.include("story_fact", str(fact.id), fact.statement, "director planning secret")
                if len(out) >= cap:
                    break
        return out
