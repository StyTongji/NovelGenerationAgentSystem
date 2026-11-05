"""MemoryExtractorAgent: proposes candidate canonical deltas (untrusted).

Consumes ONLY the MemoryExtractionContext (never repositories). Uses
generate_structured; there is NO domain repair loop — validation failures
return the report for the user to re-run (§82, conservative by design).
"""

from __future__ import annotations

from app.context.models import MemoryExtractionContext
from app.llm.client import AgentLLMClient
from app.llm.models import GenerationMetadata, LLMMessage, StructuredRequest
from app.memory.models import MemoryUpdateBatch, MemoryUpdateBatchV3

PROMPT_NAME = "memory_extractor"
PROMPT_VERSION = "memory_extractor_v3.2"


class MemoryExtractorAgent:
    def __init__(self, llm: AgentLLMClient, prompt_registry, *, contract_version: int = 3) -> None:
        self.llm = llm
        self.prompts = prompt_registry
        self.contract_version = contract_version

    async def extract(self, context: MemoryExtractionContext, *, review_feedback=None) -> tuple[MemoryUpdateBatch, GenerationMetadata]:
        system = self.prompts.get(PROMPT_NAME).content
        user_prompt = self._build_prompt(context)
        if review_feedback:
            import json
            user_prompt += ("\n\n## PRIOR REVIEW (advisory, not evidence)\n"
                "Reconsider the previous candidate and review findings against the exact draft evidence and canonical state. "
                "Correct supported mistakes and omissions; do not invent facts or assume every suggestion is right. "
                "Produce a complete replacement batch.\n" + json.dumps(review_feedback, ensure_ascii=False))
        request = StructuredRequest(
            system=system,
            messages=[LLMMessage(role="user", content=user_prompt)],
            response_model=MemoryUpdateBatchV3 if self.contract_version == 3 else MemoryUpdateBatch,
            max_repair_attempts=2,
            metadata={"agent": "memory_extractor", "prompt_version": PROMPT_VERSION},
            temperature=self.llm.temperature,
            model=self.llm.model,
            include_json_schema_hint=True,
            compact_schema_hint=self.contract_version == 3,
        )
        import hashlib
        material = system + "\n" + user_prompt + "\n" + request.json_instruction()
        request.metadata["input_hash"] = hashlib.sha256(material.encode()).hexdigest()
        request.metadata["input_utf8_bytes"] = str(len(material.encode()))
        result = await self.llm.run_request(request)
        result.metadata.extra.update({k: request.metadata[k] for k in ("input_hash", "input_utf8_bytes")})
        batch = MemoryUpdateBatch.model_validate(result.data.model_dump())
        return batch, result.metadata

    # ------------------------------------------------------------- prompt

    def _build_prompt(self, context: MemoryExtractionContext) -> str:
        import json

        canonical = {
            "characters": [c.model_dump(exclude_none=True) for c in context.character_states],
            "character_knowledge": [k.model_dump() for k in context.character_knowledge],
            "items": [i.model_dump(exclude_none=True) for i in context.items],
            "abilities": context.abilities,
            "power_realms": context.power_realms,
            "locations": [loc.model_dump(exclude_none=True) for loc in context.locations],
            "plot_threads": [t.model_dump(exclude_none=True) for t in context.plot_threads],
            "foreshadowing": [f.model_dump(exclude_none=True) for f in context.foreshadowing],
            "open_plot_debts": [d.model_dump(exclude_none=True) for d in context.open_plot_debts],
            "temporary_entities": ([e.model_dump(exclude_none=True) for e in context.draft.temporary_entities]
                                   if context.draft is not None else context.temporary_entities),
        }
        # No semantic summarization: every exact slice remains present once.
        # Offsets are server-owned and need not occupy the model's context.
        evidence_lines = [f"draft_hash={context.draft_hash}"]
        current_scene = None
        for entry in context.evidence_catalog.entries if context.evidence_catalog else []:
            if entry.scene_id != current_scene:
                current_scene = entry.scene_id
                evidence_lines.append(f"SCENE {current_scene}")
            evidence_lines.append(f"[{entry.evidence_id}] {entry.exact_text}")
        sections = [
            f"## TASK\nExtract candidate canonical deltas for chapter "
            f"{context.chapter_number} (draft_hash={context.draft_hash[:12]}).",
            "## CANONICAL STATE (reference for entity matching and state comparison — "
            "NOT evidence of what happened in this chapter)",
            "```json",
            json.dumps(canonical, ensure_ascii=False, separators=(",", ":")),
            "```",
            "## FACT CATALOG (reuse fact_id; this is not chapter evidence)",
            "```json",
            json.dumps({"facts": [f.model_dump(exclude_none=True) for f in context.fact_catalog or context.existing_facts]}, ensure_ascii=False, separators=(",", ":")),
            "```",
            "## EVIDENCE CATALOG (the only evidence source; select evidence_id)",
            "```text",
            "\n".join(evidence_lines),
            "```",
        ]
        if self.contract_version == 2 and context.chapter_plan is not None:
            sections.append("## CHAPTER PLAN (intent only — NEVER evidence)")
            sections.append("```json")
            sections.append(
                context.chapter_plan.model_dump_json(exclude_none=True)
                if hasattr(context.chapter_plan, "model_dump_json")
                else json.dumps(context.chapter_plan, ensure_ascii=False)
            )
            sections.append("```")
        # Catalog entries are ordered exact scene slices: don't send the
        # entire draft a second time, or duplicate the existing FactCatalog.
        sections.append(
            "## OUTPUT\nOne MemoryUpdateBatch JSON object matching the schema. "
            "Every operation: correct update_type, payload, source_scene_id, and "
            "one or more evidence_refs using catalog evidence_id. Do not write "
            "exact_quote; the server resolves it. Existing facts use fact_ref.fact_id. "
            "New story facts declare temporary_fact_ref (for example new_fact_1), "
            "and knowledge updates reference that same temporary ref."
        )
        return "\n\n".join(sections)
