"""Book-launch proposals are derived data until an author approves bootstrap."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr, model_validator

from app.schemas.story import BootstrapWorldRequest
from app.schemas.studio import RunInput


class MarketBook(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    platform: str
    title: str = Field(min_length=1, max_length=200)
    author: str = ""
    category: str = ""
    synopsis: str = Field(default="", max_length=4000)
    source_url: str
    observed_at: datetime
    metric_name: str = "unknown"
    metric_value: str | None = None
    rank: int | None = Field(default=None, ge=1)
    rank_type: str = ""


class MarketSample(BaseModel):
    books: list[MarketBook] = Field(min_length=1, max_length=60)
    coverage: str
    limitations: list[str] = Field(default_factory=lambda: [
        "单次小样本不能证明平台总体趋势、读者留存或商业收入。",
        "仅分析公开简介与榜单元数据，不复制作品正文或具体剧情。",
    ])
    synthetic: bool = False

    @model_validator(mode="after")
    def unique_ids(self):
        if len({b.id for b in self.books}) != len(self.books):
            raise ValueError("source IDs must be unique; keep each rank observation distinct")
        return self


class MarketFinding(BaseModel):
    observation: str
    source_ids: list[str] = Field(min_length=1)
    hypothesis: str = Field(description="推断与不确定性，不宣称收入或多日趋势")


class TopicCandidate(BaseModel):
    id: str
    title: str
    logline: str
    protagonist: str
    reader_promise: str
    differentiation: str
    opening_hook: str
    risks: list[str]
    source_ids: list[str] = Field(min_length=1)


class MarketResearch(BaseModel):
    findings: list[MarketFinding] = Field(min_length=1, max_length=8)
    topics: list[TopicCandidate] = Field(min_length=3, max_length=5)
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_topics(self):
        if len({t.id for t in self.topics}) != len(self.topics):
            raise ValueError("duplicate topic ID")
        return self


class VolumePlan(BaseModel):
    title: str
    start_chapter: int = Field(ge=1)
    end_chapter: int = Field(ge=1)
    goal: str
    turning_points: list[str]


class ChapterGuide(BaseModel):
    chapter: int = Field(ge=1, le=10)
    title: str
    goal: str
    conflict: str
    ending_hook: str
    forbidden_reveals: list[str] = Field(default_factory=list)

    def direction(self):
        return (f"暂定章名：{self.title}。目标：{self.goal}；冲突：{self.conflict}；"
                f"结尾方向：{self.ending_hook}。不可提前揭露：{'；'.join(self.forbidden_reveals)}。"
                "这是计划，尚未发生；必须服从已提交状态，必要时调整而非假定目标已达成。")


class BookBlueprint(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    genre: str = Field(default="玄幻", max_length=50)
    public_premise: str = Field(min_length=1, max_length=3000)
    author_outline: str = Field(min_length=1, max_length=12000)
    world: BootstrapWorldRequest
    volumes: list[VolumePlan] = Field(min_length=1, max_length=12)
    opening_chapters: list[ChapterGuide] = Field(min_length=10, max_length=10)

    @model_validator(mode="after")
    def coherent_initial_world(self):
        w = self.world
        def unique(values, label):
            if len(values) != len(set(values)) or any(not v.strip() for v in values):
                raise ValueError(f"duplicate/empty {label}")
            return set(values)
        names = unique([c.name for c in w.characters], "characters")
        places = unique([p.name for p in w.locations], "locations")
        realms = unique([r.name for r in w.power_system.realms], "realms")
        if not realms or not places or sum(c.is_protagonist for c in w.characters) != 1:
            raise ValueError("require realms, locations and exactly one protagonist")
        if len({r.rank for r in w.power_system.realms}) != len(realms):
            raise ValueError("realm ranks must be unique")
        for c in w.characters:
            if c.realm_name not in realms or c.location_name not in places:
                raise ValueError(f"unknown initial realm/location for {c.name}")
            realm = next(r for r in w.power_system.realms if r.name == c.realm_name)
            if c.subrealm and c.subrealm not in realm.sublevels:
                raise ValueError(f"unknown subrealm for {c.name}")
        for loc in w.locations:
            if loc.parent_name and loc.parent_name not in places:
                raise ValueError("unknown parent location")
        unique([f.name for f in w.factions], "factions")
        if any(f.leader_name and f.leader_name not in names for f in w.factions):
            raise ValueError("unknown faction leader")
        unique([i.name for i in w.items], "items")
        for i in w.items:
            if i.owner_name and i.owner_name not in names or i.location_name and i.location_name not in places:
                raise ValueError("unknown item owner/location")
        for f in w.initial_facts:
            if (set(f.known_by) | set(f.suspected_by)) - names:
                raise ValueError("unknown character in initial fact knowledge")
        if [c.chapter for c in self.opening_chapters] != list(range(1, 11)):
            raise ValueError("opening chapters must be exactly 1..10 in order")
        previous = 0
        for v in self.volumes:
            if v.start_chapter != previous + 1 or v.end_chapter < v.start_chapter:
                raise ValueError("volumes must be contiguous, ordered chapter intervals")
            previous = v.end_chapter
        if previous < 10:
            raise ValueError("volume plan must cover opening ten chapters")
        return self


class LaunchBudget(BaseModel):
    max_calls: int = Field(default=12, ge=1, le=100)
    token_budget: int = Field(default=200000, ge=1000, le=2000000)


class LaunchConfig(LaunchBudget):
    brief: str = Field(default="玄幻：有明确能力代价、人物选择与持续剧情推进", min_length=1, max_length=4000)
    mode: Literal["demo", "live"] = "demo"
    provider: str = "scnet"
    model: str = "DeepSeek-V4.1-Flash-1017"
    custom_base_url: str | None = None
    max_output_tokens: int = Field(default=8192, ge=1024, le=32768)
    runtime_capabilities: dict[str, dict[str, bool]] | None = None
    sample: MarketSample

    @model_validator(mode="after")
    def provider_contract(self):
        self.custom_base_url = RunInput.normalize_base_url(self.custom_base_url)
        if self.mode == "live" and self.sample.synthetic:
            raise ValueError("live research requires non-synthetic sources")
        if self.provider == "custom_input" and not self.custom_base_url:
            raise ValueError("custom provider requires base URL")
        return self


class LaunchStart(LaunchConfig):
    api_key: SecretStr | None = Field(default=None, exclude=True)


class ResearchForkInput(BaseModel):
    provider: str
    model: str = Field(min_length=1)
    custom_base_url: str | None = None
    max_output_tokens: int = Field(default=8192, ge=1024, le=32768)
    max_calls: int = Field(default=12, ge=1, le=100)
    token_budget: int = Field(default=200000, ge=1000, le=2000000)
    runtime_capabilities: dict[str, dict[str, bool]] | None = None
    api_key: SecretStr | None = Field(default=None, exclude=True)


class TopicSelection(BaseModel):
    topic_id: str
    research_hash: str


class BlueprintApproval(BaseModel):
    blueprint_hash: str
    reviewed_by: str = Field(min_length=1, max_length=100)
    approved: bool = False


class BlueprintRevision(BaseModel):
    source_hash: str
    blueprint: BookBlueprint


class LaunchSession(BaseModel):
    id: str
    config: LaunchConfig
    status: Literal["researching", "awaiting_topic", "planning", "awaiting_approval", "created", "failed", "cancelled", "interrupted"]
    research: MarketResearch | None = None
    research_hash: str = ""
    source_session_id: str | None = None
    selected_topic: str | None = None
    blueprint: BookBlueprint | None = None
    blueprint_hash: str = ""
    novel_id: UUID
    studio_run_id: str | None = None
    approval: BlueprintApproval | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
