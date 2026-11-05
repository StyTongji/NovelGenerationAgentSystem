"""Explicit small streaming/usage probe; shares the real trial budget ledger."""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.llm.budget import TrialBudget
from app.llm.client import AgentLLMClient
from app.llm.openai_compatible import OpenAICompatibleProvider
from app.services.trial_artifacts import write_new
from scripts.scnet_probe import Check


async def main(args):
    report = json.loads(args.source.read_text())
    if args.output.exists():
        raise ValueError("Output profile must be new")
    for model in ("Auto", "DeepSeek-V4.1-Flash-1017"):
        provider = OpenAICompatibleProvider(base_url=report["base_url"],
            budget=TrialBudget(args.run_dir / "usage_ledger.json", 1, chapter_tokens=450_000),
            trace_path=args.run_dir / "llm_calls.jsonl", max_retries=0)
        provider.model_capabilities[model] = {**report["models"][model], "stream_ok": True}
        provider.request_cutoffs["Auto"] = datetime.fromisoformat(args.auto_until)
        client = AgentLLMClient(provider, agent_name="stream_capability_probe", model=model,
                                max_tokens=256, deadline_seconds=45)
        try:
            result = await client.generate_structured(system='Return JSON {"ok": true}.', user="Check.",
                response_model=Check, max_repair_attempts=0)
            report["models"][model]["stream_ok"] = result.data.ok and result.metadata.finish_reason == "stop"
            report["models"][model]["stream_metadata"] = result.metadata.model_dump(mode="json")
        except Exception as exc:
            report["models"][model]["stream_ok"] = False
            report["models"][model]["stream_error"] = type(exc).__name__
        finally:
            await provider.aclose()
        print(json.dumps({"model": model, "stream_ok": report["models"][model]["stream_ok"]}), flush=True)
    write_new(args.output, report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--auto-until", required=True)
    asyncio.run(main(parser.parse_args()))
