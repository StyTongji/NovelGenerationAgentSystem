"""Deterministic catalogs used by Memory Extractor v3.

Catalog entries are projections of the final draft/canonical facts.  They are
not additional truth sources: the draft hash and fact ids bind every entry to
the state that produced it.
"""

from __future__ import annotations

import hashlib
import re

from pydantic import BaseModel, Field

from app.draft.models import ChapterDraft


class EvidenceCatalogEntry(BaseModel):
    evidence_id: str
    scene_id: str
    exact_text: str
    start_char: int
    end_char: int


class EvidenceCatalog(BaseModel):
    draft_hash: str
    entries: list[EvidenceCatalogEntry] = Field(default_factory=list)

    def by_id(self) -> dict[str, EvidenceCatalogEntry]:
        return {entry.evidence_id: entry for entry in self.entries}


class FactCatalogEntry(BaseModel):
    fact_id: str
    statement: str
    secrecy: str
    reader_visible: bool = False


class EvidenceCatalogBuilder:
    """Build stable sentence-level evidence ids from the final draft."""

    _SENTENCE = re.compile(r"[^。！？!?；;\n]+(?:[。！？!?；;][\"”’」』]*|(?=\n)|$)|[。！？!?；;]+")

    @classmethod
    def build(cls, draft: ChapterDraft, draft_hash: str | None = None) -> EvidenceCatalog:
        # Never accept a caller-provided hash as proof of catalog identity.
        draft_hash = hashlib.sha256(draft.full_text.encode("utf-8")).hexdigest()
        entries: list[EvidenceCatalogEntry] = []
        index = 1
        for scene in draft.scenes:
            spans = [(match.start(), match.end()) for match in cls._SENTENCE.finditer(scene.text)]
            if not spans and scene.text.strip():
                start = len(scene.text) - len(scene.text.lstrip())
                end = len(scene.text.rstrip())
                spans = [(start, end)]
            for match_start, _match_end in spans:
                raw = scene.text[match_start:_match_end]
                left = len(raw) - len(raw.lstrip())
                right = len(raw.rstrip())
                start = match_start + left
                end = match_start + right
                if start >= end:
                    continue
                entries.append(EvidenceCatalogEntry(
                    evidence_id=f"ev_{index:04d}",
                    scene_id=scene.scene_id,
                    exact_text=scene.text[start:end],
                    start_char=start,
                    end_char=end,
                ))
                index += 1
        return EvidenceCatalog(draft_hash=draft_hash, entries=entries)


def resolve_evidence_refs(batch, catalog: EvidenceCatalog):
    """Resolve v3 refs to server-owned EvidenceSpan values.

    Legacy exact quotes remain accepted for old fixtures; v3 operations are
    resolved exclusively from catalog ids.
    """
    from app.memory.models import EvidenceSpan

    by_id = catalog.by_id()
    operations = []
    for operation in batch.model_copy(deep=True).operations:
        if operation.evidence_refs:
            resolved = []
            for ref in operation.evidence_refs:
                entry = by_id.get(ref.evidence_id)
                if entry is None:
                    continue
                resolved.append(EvidenceSpan(
                    evidence_id=entry.evidence_id,
                    scene_id=entry.scene_id,
                    exact_quote=entry.exact_text,
                    start_char=entry.start_char,
                    end_char=entry.end_char,
                    reason=ref.reason,
                ))
            operation = operation.model_copy(update={"evidence": resolved})
        operations.append(operation)
    return batch.model_copy(update={"operations": operations})
