"""Small, fixed-evidence semantic review. Diagnostic only, never an approval."""
import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pydantic import BaseModel, Field
from app.commit.models import ChapterPreviewPackage
from app.llm.budget import TrialBudget
from app.llm.client import AgentLLMClient
from app.llm.models import StructuredRequest, LLMMessage
from app.llm.openai_compatible import OpenAICompatibleProvider
from app.services.trial_artifacts import safe_name, write_new
from scripts.real_llm_trial import _check_package


class Finding(BaseModel):
    operation_id: str
    verdict: Literal["correct", "wrong", "over-extracted", "needs_review"]
    reason: str


class Diagnostic(BaseModel):
    findings: list[Finding]
    missing: list[str] = Field(default_factory=list)


async def probe(args):
    run = ROOT / "real_trial_runs" / safe_name(args.run)
    source = run / "chapter_01/attempts" / safe_name(args.source_attempt) / "preview_package.json"
    package = ChapterPreviewPackage.model_validate_json(source.read_text())
    _check_package(package)
    selected = [op for op in package.memory_result.batch.operations if op.operation_id in args.operations]
    if len(selected) != len(set(args.operations)):
        raise ValueError("Unknown operation in fixed review selection")
    # These are a declared reference, not novel evidence. Verify names against
    # the immutable before-state fixture that matches this source state.
    import sqlite3
    with sqlite3.connect(f"file:{run / 'trial.db'}?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT name, description FROM characters WHERE novel_id = ?",
                                  (package.novel_id.replace("-", ""),)).fetchall()
    payload = {"canonical_character_descriptions": rows,
        "operations": [{"operation_id": op.operation_id, "update_type": op.update_type.value,
            "source_scene_id": op.source_scene_id, "payload": op.payload.model_dump(mode="json", exclude_none=True),
            "evidence": [e.model_dump(exclude_none=True) for e in op.evidence]} for op in selected]}
    prompt = (ROOT / "prompts/memory_semantic_review.md").read_text()
    user = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    profile = json.loads(args.capabilities.read_text())
    output = run / "semantic_reviews" / safe_name(args.attempt)
    output.mkdir(parents=True, exist_ok=False)
    write_new(output / "input.json", {"source_preview_hash": package.preview_package_hash,
        "model": args.model, "input_hash": hashlib.sha256((prompt + user).encode()).hexdigest(),
        "payload": payload, "human_approval": False})
    provider = OpenAICompatibleProvider(base_url=profile["base_url"],
        budget=TrialBudget(run / "usage_ledger.json", 1, chapter_tokens=args.chapter_token_budget),
        trace_path=run / "llm_calls.jsonl")
    provider.model_capabilities = profile["models"]
    provider.request_cutoffs["Auto"] = datetime.fromisoformat(args.auto_until)
    client = AgentLLMClient(provider, agent_name="semantic_probe", model=args.model,
                            temperature=.1, max_tokens=4096, deadline_seconds=240)
    try:
        result = await client.run_request(StructuredRequest(model=args.model, temperature=.1,
            system=prompt, messages=[LLMMessage(role="user", content=user)], response_model=Diagnostic,
            compact_schema_hint=True, max_repair_attempts=1,
            metadata={"agent": "semantic_probe", "prompt_version": "memory_semantic_review_v1"}))
        if {f.operation_id for f in result.data.findings} != set(args.operations) or len(result.data.findings) != len(selected):
            raise ValueError("Reviewer did not cover exactly the selected operations")
        write_new(output / "result.json", {"diagnostic": result.data.model_dump(),
            "metadata": result.metadata.model_dump(mode="json"), "human_approval": False})
        print(json.dumps(result.data.model_dump(), ensure_ascii=False, indent=2))
    except Exception as exc:
        write_new(output / "failure.json", {"error_type": type(exc).__name__})
        raise
    finally:
        await provider.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--source-attempt", required=True)
    parser.add_argument("--operations", nargs="+", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--model", default="Auto")
    parser.add_argument("--capabilities", type=Path, required=True)
    parser.add_argument("--chapter-token-budget", type=int, default=150_000)
    parser.add_argument("--auto-until", required=True)
    asyncio.run(probe(parser.parse_args()))
