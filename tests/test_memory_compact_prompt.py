import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import BaseModel

from app.llm.models import StructuredRequest
from app.llm.budget import BudgetExceededError, TrialBudget
from app.llm.client import AgentLLMClient
from app.llm.openai_compatible import OpenAICompatibleProvider
from app.memory.models import MemoryUpdateBatchV3


def test_compact_schema_keeps_properties_and_constraints():
    request = StructuredRequest(response_model=MemoryUpdateBatchV3, compact_schema_hint=True)
    schema = json.loads(request.json_instruction().split("schema:\n", 1)[1])
    assert "title" in schema["$defs"]["EventCandidate"]["properties"]
    assert schema["$defs"]["MemoryUpdateOperationV3"]["properties"]["evidence_refs"]["minItems"] == 1
    assert schema["$defs"]["StoryFactCandidateV3"]["properties"]["temporary_fact_ref"]["pattern"]
    assert len(request.json_instruction()) < len(StructuredRequest(response_model=MemoryUpdateBatchV3).json_instruction())


async def test_auto_cutoff_blocks_request_without_spending_reservation(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "fake-test-key")
    def forbidden(request):
        raise AssertionError("No HTTP after cutoff")
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        provider = OpenAICompatibleProvider(base_url="https://test", api_key_env="TEST_KEY", http_client=http,
            budget=TrialBudget(tmp_path / "ledger.json", 1))
        provider.request_cutoffs["Auto"] = datetime.now(UTC) - timedelta(seconds=1)
        with pytest.raises(BudgetExceededError, match="window closed"):
            await AgentLLMClient(provider, agent_name="test", model="Auto", max_tokens=100).generate_text(system="", user="test")
    assert not (tmp_path / "ledger.json").exists()


def test_budget_increase_preserves_old_unknown_usage(tmp_path):
    path = tmp_path / "ledger.json"
    budget = TrialBudget(path, 1, chapter_tokens=100)
    reservation = budget.reserve([], 90)
    with pytest.raises(BudgetExceededError):
        budget.reserve([], 50)
    increased = TrialBudget(path, 1, chapter_tokens=200)
    increased.reserve([], 50)
    rows = json.loads(path.read_text())["reservations"]
    assert rows[reservation]["charged_tokens"] == 90
    assert rows[reservation]["usage_known"] is False
