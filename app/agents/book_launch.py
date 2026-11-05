"""Two author-side roles; no DB access and no direct vendor calls."""
from app.llm.client import AgentLLMClient
from app.llm.prompts import get_default_registry
from app.schemas.book_launch import BookBlueprint, LaunchConfig, MarketResearch, TopicCandidate


PROMPTS = {"market_research": "market_research_v1", "story_architect": "story_architect_v1"}


class MarketResearchAgent:
    def __init__(self, llm: AgentLLMClient):
        self.llm = llm

    async def research(self, config: LaunchConfig) -> MarketResearch:
        result = await self.llm.generate_structured(
            system=get_default_registry().get("market_research").content,
            user=config.model_dump_json(include={"brief", "sample"}), response_model=MarketResearch,
        )
        value = MarketResearch.model_validate(result.data)
        known = {b.id for b in config.sample.books}
        cited = {ref for entry in [*value.findings, *value.topics] for ref in entry.source_ids}
        if cited - known:
            raise ValueError(f"市场分析引用未知来源 ID：{sorted(cited - known)}")
        return value


class StoryArchitectAgent:
    def __init__(self, llm: AgentLLMClient):
        self.llm = llm

    async def design(self, brief: str, topic: TopicCandidate) -> BookBlueprint:
        import json
        result = await self.llm.generate_structured(
            system=get_default_registry().get("story_architect").content,
            user=json.dumps({"brief": brief, "confirmed_topic": topic.model_dump(mode="json")}, ensure_ascii=False),
            response_model=BookBlueprint,
        )
        return BookBlueprint.model_validate(result.data)
