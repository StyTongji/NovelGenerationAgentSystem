"""Small, budgeted capability probe. Key is read only from the environment."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from pydantic import BaseModel
from app.llm.budget import TrialBudget
from app.llm.client import AgentLLMClient
from app.llm.openai_compatible import OpenAICompatibleProvider
from app.services.trial_artifacts import write_new

MODELS = ("DeepSeek-V4.1-Flash-1017", "Auto", "DeepSeek-V4-Flash")


class Check(BaseModel):
    ok: bool


async def probe(output: Path):
    key = os.environ.get("NOVEL_LLM_API_KEY")
    if not key:
        raise ValueError("NOVEL_LLM_API_KEY is required")
    output.mkdir(parents=True, exist_ok=False)
    base = os.environ.get("NOVEL_LLM_BASE_URL", "https://api.scnet.cn/api/llm/v1").rstrip("/")
    report = {"base_url": base, "models": {}}
    async with httpx.AsyncClient(timeout=20, trust_env=False) as http:
        response = await http.get(base + "/models", headers={"Authorization": f"Bearer {key}"})
        if response.status_code != 200:
            report["models_status"] = response.status_code
            write_new(output / "capabilities.json", report)
            return report
        ids = {m["id"] for m in response.json().get("data", [])}
    for model in MODELS:
        row = {"listed": model in ids}
        report["models"][model] = row
        if model not in ids:
            continue
        provider = OpenAICompatibleProvider(
            base_url=base, model=model, budget=TrialBudget(output / "usage_ledger.json", 0),
            trace_path=output / "calls.jsonl", enable_thinking=False,
        )
        client = AgentLLMClient(provider, agent_name="capability_probe", model=model,
                                max_tokens=256, deadline_seconds=45)
        try:
            result = await client.generate_text(system="Answer briefly.", user="Reply with OK.")
            row["text_ok"] = bool(result.text.strip()) and result.metadata.finish_reason == "stop"
            row["text_metadata"] = result.metadata.model_dump(mode="json")
            row["enable_thinking_false_accepted"] = True
            provider.json_mode = True
            result = await client.generate_structured(system='Return JSON {"ok": true}.', user="Check.",
                                                     response_model=Check, max_repair_attempts=0)
            row["json_ok"] = result.data.ok and result.metadata.finish_reason == "stop"
            row["json_metadata"] = result.metadata.model_dump(mode="json")
        except Exception as exc:
            row["error_type"] = type(exc).__name__  # no provider response/credentials in logs
        finally:
            await provider.aclose()
            print(json.dumps({"model": model, **{k: v for k, v in row.items() if not k.endswith("metadata")}}, ensure_ascii=False))
    write_new(output / "capabilities.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(probe(args.output))
