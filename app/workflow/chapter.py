"""Executable chapter graph; canonical writes remain outside preview.

LangGraph owns stage routing, including the continuity repair branch. The
existing TrialAttempt owns durable, input-bound checkpoints, including scene
planner and individual writer stages nested inside ``draft``. We deliberately
do not install a second checkpoint store or serialize the live SQL session.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypedDict

from langgraph.graph import END, START, StateGraph

from app.commit.models import ChapterPreviewPackage
from app.continuity.models import ContinuityRepairResult, ContinuityReport
from app.draft.models import ChapterDraft
from app.planning.models import PlanGenerationResult
from app.services.memory_extraction import MemoryExtractionResult

if TYPE_CHECKING:
    from app.services.chapter_generation import ChapterGenerationService


class ChapterWorkflowState(TypedDict, total=False):
    novel_id: uuid.UUID
    user_direction: str | None
    chapter_number: int | None
    planning: PlanGenerationResult
    draft: ChapterDraft
    continuity_report: ContinuityReport
    memory_result: MemoryExtractionResult
    package: ChapterPreviewPackage


class ChapterWorkflow:
    """One invocation uses one service/session and optional attempt observer."""

    def __init__(self, service: ChapterGenerationService, *, checkpoints=None,
                 auto_repair: bool = True) -> None:
        self.service = service
        self.checkpoints = checkpoints
        self.auto_repair = auto_repair

    async def _stage(self, name, model, produce):
        if self.checkpoints is not None:
            return await self.checkpoints.stage(name, model, produce)
        return await produce()

    def build(self, *, from_draft: bool = False):
        graph = StateGraph(ChapterWorkflowState)
        for name in ("director", "draft", "continuity", "repair", "memory", "package"):
            graph.add_node(name, getattr(self, name))
        graph.add_edge(START, "continuity" if from_draft else "director")
        graph.add_edge("director", "draft")
        graph.add_edge("draft", "continuity")
        graph.add_conditional_edges("continuity", self.after_continuity,
                                    {"memory": "memory", "repair": "repair"})
        # ContinuityService.repair_preview already runs bounded repair + recheck.
        # A failed repair raises before this edge and cannot reach extraction.
        graph.add_edge("repair", "memory")
        graph.add_edge("memory", "package")
        graph.add_edge("package", END)
        return graph.compile()

    async def run(self, state: ChapterWorkflowState, *, from_draft: bool = False):
        result = await self.build(from_draft=from_draft).ainvoke(state)
        return result["package"]

    async def director(self, state: ChapterWorkflowState):
        result = await self._stage("director", PlanGenerationResult,
            lambda: self.service.planning_service.generate_preview(
                state["novel_id"], state.get("user_direction"),
                chapter_number=state.get("chapter_number")))
        return {"planning": result}

    async def draft(self, state: ChapterWorkflowState):
        result = await self._stage("draft", ChapterDraft,
            lambda: self.service.draft_service.generate_preview(
                state["novel_id"], state["planning"].plan, checkpoints=self.checkpoints))
        return {"draft": result}

    async def continuity(self, state: ChapterWorkflowState):
        draft = state["draft"]
        report = await self._stage("continuity", ContinuityReport,
            lambda: self.service.continuity_service.check_draft(
                state["novel_id"], draft, state["planning"].plan,
                scene_plans=list(draft.scene_plans)))
        return {"continuity_report": report}

    def after_continuity(self, state: ChapterWorkflowState):
        return "memory" if state["continuity_report"].valid else "repair"

    async def repair(self, state: ChapterWorkflowState):
        from app.services.chapter_generation import ContinuityRepairExhaustedError

        if not self.auto_repair:
            raise ContinuityRepairExhaustedError(
                "Revised draft has blocking continuity issues; saved for author review. "
                "No automatic rewrite or memory extraction was performed.")
        draft = state["draft"]
        result = await self._stage("repair", ContinuityRepairResult,
            lambda: self.service.continuity_service.repair_preview(
                state["novel_id"], draft, state["planning"].plan,
                scene_plans=list(draft.scene_plans)))
        if result.exhausted or not result.report.valid:
            if not result.repair_history:
                raise ContinuityRepairExhaustedError(
                    "连续性检查未通过，问题无法定位到可修复的场景，未执行自动修复；"
                    "草稿已保存，请查看报告并编辑相关场景。问题类型："
                    + "；".join(i.category.value for i in result.report.blocking_issues()))
            raise ContinuityRepairExhaustedError(
                "Continuity repair exhausted; no valid memory preview: "
                + "; ".join(i.category.value for i in result.report.blocking_issues()))
        # ContinuityRepairResult uses object to avoid a schema import cycle;
        # durable checkpoint JSON therefore needs its nested draft rehydrated.
        repaired = ChapterDraft.model_validate(result.draft)
        return {"draft": repaired, "continuity_report": result.report}

    async def memory(self, state: ChapterWorkflowState):
        draft = state["draft"]
        result = await self._stage("memory", MemoryExtractionResult,
            lambda: self.service.memory_service.extract_preview(
                state["novel_id"], draft, state["planning"].plan,
                state["continuity_report"], scene_plans=list(draft.scene_plans)))
        return {"memory_result": result}

    async def package(self, state: ChapterWorkflowState):
        async def produce():
            planning = state["planning"]
            draft = state["draft"]
            report = state["continuity_report"]
            memory = state["memory_result"]
            return ChapterPreviewPackage(
                novel_id=str(state["novel_id"]), chapter_number=draft.chapter_number,
                user_direction=state.get("user_direction"), plan_result=planning,
                draft=draft, continuity_report=report, memory_result=memory,
                story_state_hash=planning.context_hash,
                preview_package_hash=ChapterPreviewPackage.compute_hash(
                    plan=planning.plan, draft=draft, continuity_report=report,
                    memory_result=memory, story_state_hash=planning.context_hash),
                generated_at=datetime.now(UTC))
        return {"package": await self._stage("package", ChapterPreviewPackage, produce)}


def workflow_metadata() -> dict:
    """UI/teaching inventory: graph nodes are not the count of LLM roles."""
    return {
        "framework": "LangGraph",
        "checkpoint_store": "TrialAttempt (hash-bound stage and scene files)",
        "orchestrator": "deterministic Python graph; does not call an LLM itself",
        "nodes": [
            {"id": "director", "label": "章节导演", "agent_ids": ["director"]},
            {"id": "draft", "label": "分场景规划与写作", "agent_ids": ["scene_planner", "writer"]},
            {"id": "continuity", "label": "连续性检查", "agent_ids": ["continuity_checker"]},
            {"id": "repair", "label": "定向修复与复查", "agent_ids": ["writer_repair", "continuity_checker"]},
            {"id": "memory", "label": "证据记忆与验证", "agent_ids": ["memory_extractor"]},
            {"id": "package", "label": "候选预览", "agent_ids": []},
            {"id": "literary_review", "label": "文学评审（可选）", "agent_ids": ["literary_reviewer"], "optional": True},
            {"id": "approval", "label": "作者审阅", "agent_ids": [], "human": True},
            {"id": "finalize", "label": "事务提交", "agent_ids": [], "deterministic": True},
        ],
        "edges": [
            {"source": "director", "target": "draft"},
            {"source": "draft", "target": "continuity"},
            {"source": "continuity", "target": "repair", "condition": "blocking issues"},
            {"source": "continuity", "target": "memory", "condition": "valid"},
            {"source": "repair", "target": "memory", "condition": "repair valid; otherwise stop"},
            {"source": "memory", "target": "package"},
            {"source": "package", "target": "literary_review", "condition": "enabled"},
            {"source": "package", "target": "approval", "condition": "review disabled"},
            {"source": "literary_review", "target": "approval"},
            {"source": "approval", "target": "finalize", "condition": "explicit approval and valid preview"},
        ],
        "agents": [
            {"id": "director", "name": "章节导演", "goal": "依据已提交世界状态与作者方向规划本章目标和因果推进", "output": "ChapterPlan"},
            {"id": "scene_planner", "name": "场景策划", "goal": "将章节计划分解为有视角、地点、目标和字数的场景", "output": "ScenePlanBatch"},
            {"id": "writer", "name": "场景作家", "goal": "依据 POV 过滤后的知识逐场景创作，并承接上一场景结尾", "output": "SceneDraft"},
            {"id": "continuity_checker", "name": "连续性审查", "goal": "结合确定性规则与语义检查发现设定、角色知识和状态冲突", "output": "ContinuityReport"},
            {"id": "writer_repair", "name": "修订编辑", "goal": "依据安全投影后的问题定向修复相关场景，最多两轮", "output": "repaired SceneDraft"},
            {"id": "memory_extractor", "name": "记忆档案员", "goal": "引用服务器证据 ID 提议事件、状态和知识变化，经验证后交作者确认", "output": "MemoryUpdateBatch"},
            {"id": "literary_reviewer", "name": "文学评审", "goal": "独立评价可读性、人物、对话、叙事衔接和推进感，给出带原文证据的建议", "output": "LiteraryQualityReport", "optional": True},
        ],
    }
