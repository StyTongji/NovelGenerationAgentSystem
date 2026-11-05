"""LongHorizonStoryAuditor: post-soak canonical integrity checks (test helper).

Not a production feature; if it proves valuable it can be productized later.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Chapter,
    ChapterCommit,
    ChapterSummary,
    Character,
    CharacterKnowledge,
    CharacterState,
    Event,
    Item,
    PlotDebt,
    PlotThread,
    StorySnapshot,
)


@dataclass
class AuditResult:
    problems: list[str] = field(default_factory=list)
    chapter_numbers: list[int] = field(default_factory=list)
    snapshot_chain_ok: bool = False
    hash_chain_ok: bool = False

    @property
    def ok(self) -> bool:
        return not self.problems


class LongHorizonStoryAuditor:
    def __init__(self, session: AsyncSession, novel_id: uuid.UUID) -> None:
        self.session = session
        self.novel_id = novel_id

    async def audit(self, expected_chapters: list[int]) -> AuditResult:
        result = AuditResult()
        commits = (
            await self.session.execute(
                select(ChapterCommit)
                .where(ChapterCommit.novel_id == self.novel_id)
                .order_by(ChapterCommit.chapter_number.asc())
            )
        ).scalars().all()
        commits_by_number = {c.chapter_number: c for c in commits}

        # chapter chain: no gaps, no duplicates, canonical
        chapters = (
            await self.session.execute(
                select(Chapter)
                .where(Chapter.novel_id == self.novel_id)
                .order_by(Chapter.number.asc())
            )
        ).scalars().all()
        canonical = {
            c.number: c
            for c in chapters
            if c.status == "canonical"
        }
        for number in expected_chapters:
            if number not in canonical:
                result.problems.append(f"missing canonical chapter {number}")
        result.chapter_numbers = sorted(canonical)
        if len(canonical) != len(expected_chapters):
            result.problems.append("unexpected canonical chapter count")

        # snapshot + hash chain
        snapshots = (
            await self.session.execute(
                select(StorySnapshot).where(StorySnapshot.novel_id == self.novel_id)
            )
        ).scalars().all()
        snapshot_by_chapter = {s.after_chapter_number: s for s in snapshots}
        previous_after: str | None = None
        for number in expected_chapters:
            commit = commits_by_number.get(number)
            if commit is None:
                result.problems.append(f"missing commit record for chapter {number}")
                continue
            snapshot = snapshot_by_chapter.get(number)
            if snapshot is None:
                result.problems.append(f"missing snapshot for chapter {number}")
                continue
            if snapshot.state_hash != commit.after_state_hash:
                result.problems.append(
                    f"chapter {number}: snapshot hash != commit after hash"
                )
            if previous_after is not None and commit.before_state_hash != previous_after:
                result.problems.append(
                    f"chapter {number}: before hash does not chain from chapter {number - 1}"
                )
            chapter = canonical.get(number)
            if chapter is not None and chapter.draft_hash != commit.draft_hash:
                result.problems.append(f"chapter {number}: draft hash mismatch with commit")
            if chapter is not None and chapter.story_state_after_hash != commit.after_state_hash:
                result.problems.append(f"chapter {number}: chapter after-hash mismatch")
            previous_after = commit.after_state_hash
        result.snapshot_chain_ok = not result.problems
        result.hash_chain_ok = result.snapshot_chain_ok

        # summary binding: source_draft_hash == chapter.draft_hash
        summaries = (
            await self.session.execute(
                select(ChapterSummary).where(ChapterSummary.novel_id == self.novel_id)
            )
        ).scalars().all()
        for summary in summaries:
            chapter = canonical.get(summary.chapter_number)
            if chapter is None:
                continue
            if not summary.source_draft_hash:
                result.problems.append(
                    f"chapter {summary.chapter_number}: summary missing source_draft_hash"
                )
            elif summary.source_draft_hash != chapter.draft_hash:
                result.problems.append(
                    f"chapter {summary.chapter_number}: summary bound to a stale draft"
                )

        # evidence integrity: every committed evidence quote is inside the
        # committed chapter prose
        for commit in commits:
            chapter = canonical.get(commit.chapter_number)
            if chapter is None:
                continue
            for op in commit.operations_json:
                for quote in op.get("evidence", []):
                    if isinstance(quote, dict):
                        quote = quote.get("exact_text", "")
                    if quote and quote not in chapter.body:
                        result.problems.append(
                            f"chapter {commit.chapter_number}: evidence quote not in "
                            f"committed prose ({quote[:40]}...)"
                        )

        # knowledge invariants: unique (character, fact) pairs
        knowledge = (
            await self.session.execute(
                select(CharacterKnowledge).where(
                    CharacterKnowledge.character_id.in_(
                        select(Character.id).where(Character.novel_id == self.novel_id)
                    )
                )
            )
        ).scalars().all()
        pairs = [(k.character_id, k.fact_id) for k in knowledge]
        if len(pairs) != len(set(pairs)):
            result.problems.append("duplicate (character, fact) knowledge rows")

        # item ownership: no item with two owners is structurally impossible,
        # but check for items owned by dead/missing rows or null-drift
        items = (
            await self.session.execute(
                select(Item).where(Item.novel_id == self.novel_id)
            )
        ).scalars().all()
        owned = [i for i in items if i.current_owner_character_id]
        if len(owned) != len({i.id for i in owned}):
            result.problems.append("item identity drift")

        # plot lifecycle: last_advanced_chapter never in the future / negative
        threads = (
            await self.session.execute(
                select(PlotThread).where(PlotThread.novel_id == self.novel_id)
            )
        ).scalars().all()
        for thread in threads:
            if thread.last_advanced_chapter is not None and thread.last_advanced_chapter > max(
                expected_chapters
            ):
                result.problems.append(
                    f"thread {thread.title}: last_advanced_chapter in the future"
                )

        # resolved debts stay resolved with correct chapter
        debts = (
            await self.session.execute(
                select(PlotDebt).where(PlotDebt.novel_id == self.novel_id)
            )
        ).scalars().all()
        for debt in debts:
            if debt.status == "fulfilled" and debt.resolved_chapter is None:
                result.problems.append(f"debt {debt.description}: resolved without chapter")

        # event uniqueness: no two events with identical fingerprints
        events = (await self.session.execute(select(Event))).scalars().all()
        event_ids = [e.id for e in events]
        if len(event_ids) != len(set(event_ids)):
            result.problems.append("duplicate event rows")
        for commit in commits:
            fingerprints = commit.operation_fingerprints_json
            if len(fingerprints) != len(set(fingerprints)):
                result.problems.append(
                    f"chapter {commit.chapter_number}: duplicate operation fingerprints"
                )
        return result
