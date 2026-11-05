"""Context budget: bounded, priority-aware context assembly.

Budget is enforced at the ContextBuilder level. Trim priority follows the
Phase 3 spec (highest first — never trimmed):

1. Hard constraints
2. POV character current state
3. Character knowledge
4. Scene / user direction
5. Current location
6. Directly referenced entities
7. Required plot threads
8. Recent events
9. Relevant historical events
10. Soft constraints
11. Background lore

The first version approximates tokens with characters; a tokenizer can
replace ``estimate_size`` later without changing call sites.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.context.models import ContextFact, EventView


class ContextBudget(BaseModel):
    max_events: int = 12
    max_plot_threads: int = 8
    max_items: int = 8
    max_recent_summaries: int = 5
    max_knowledge_facts: int = 30
    max_planning_secrets: int = 10
    previous_chapter_tail_chars: int = 1200
    max_event_chars: int = 6000

    def estimate_size(self, text: str) -> int:
        return len(text)


DEFAULT_BUDGET = ContextBudget()


def trim_events(
    events: list[EventView],
    scores: dict[str, float],
    budget: ContextBudget,
) -> list[EventView]:
    """Deterministic event trimming: score desc, then id asc; cap both by
    count and total content size. Never reorders beyond the sort key."""
    ranked = sorted(events, key=lambda e: (-scores.get(e.id, 0.0), e.id))
    kept: list[EventView] = []
    total = 0
    for event in ranked:
        if len(kept) >= budget.max_events:
            break
        size = budget.estimate_size(event.summary)
        if total + size > budget.max_event_chars and kept:
            break
        kept.append(event)
        total += size
    return kept


def trim_facts(
    facts: list[ContextFact],
    protected_prefixes: tuple[str, ...] = (),
    cap: int = 30,
) -> list[ContextFact]:
    """Keep protected items (e.g. hard-constraint facts) first, cap the rest."""
    protected = [f for f in facts if f.reason.startswith(protected_prefixes)]
    rest = [f for f in facts if not f.reason.startswith(protected_prefixes)]
    return (protected + rest)[:cap]


def tail(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]
