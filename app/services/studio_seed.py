"""Initialize a user-owned story without the historical xianxia fixture."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Character, CharacterState, Constraint, Location, Novel, PlotThread, StoryFact
from app.models.enums import ConstraintType, SecrecyLevel, ThreadStatus


class StudioNovelInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    genre: str = Field(default="悬疑", min_length=1, max_length=50)
    synopsis: str = Field(min_length=1, max_length=8000)
    protagonist: str = Field(min_length=1, max_length=100)
    companions: list[str] = Field(default_factory=list, max_length=8)
    world_rules: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("companions", "world_rules")
    @classmethod
    def clean_lines(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @model_validator(mode="after")
    def distinct_characters(self):
        if self.protagonist in self.companions:
            raise ValueError("主角不能同时出现在配角列表中")
        if any(len(name) > 100 for name in self.companions):
            raise ValueError("人物名称最多 100 字")
        return self


async def create_studio_novel(session: AsyncSession, request: StudioNovelInput) -> Novel:
    """Create initial canon; the caller owns commit/rollback of this transaction."""
    novel = Novel(title=request.title, genre=request.genre, premise=request.synopsis,
                  style_profile="清晰、具体、人物行动推动情节", current_chapter_number=0)
    session.add(novel)
    await session.flush()
    # A neutral opening place is explicit initial canon; no hidden default cast,
    # cultivation system or inherited facts from previous demo novels.
    location = Location(novel_id=novel.id, name="故事起点",
                        description="开篇地点；具体环境依据本书题材与梗概展开。")
    session.add(location)
    await session.flush()
    for index, name in enumerate([request.protagonist, *request.companions]):
        character = Character(
            novel_id=novel.id, name=name, is_protagonist=index == 0,
            description="本书主角" if index == 0 else "与主角共同参与故事的配角",
            personality_traits=["重视证据", "愿意承担后果"] if index == 0 else ["有自己的判断"],
            background=request.synopsis if index == 0 else None,
        )
        session.add(character)
        await session.flush()
        session.add(CharacterState(
            character_id=character.id, chapter_number=0,
            current_location_id=location.id, physical_condition="健康",
            mental_condition="谨慎", current_goals=["面对开篇问题并作出自己的选择"],
        ))
    for rule in request.world_rules:
        session.add(Constraint(novel_id=novel.id, type=ConstraintType.hard, statement=rule))
        session.add(StoryFact(novel_id=novel.id, statement=rule, secrecy=SecrecyLevel.public,
                             category="world_rule", introduced_chapter=0,
                             revealed_to_reader_chapter=0))
    session.add(PlotThread(
        novel_id=novel.id, title="主线：" + request.title[:180], status=ThreadStatus.active,
        thread_type="main", public_summary=request.synopsis, introduced_chapter=0,
        target_resolution_chapter=10,
    ))
    await session.flush()
    return novel
