"""Local, author-facing reading copies. These files never approve a commit."""
from __future__ import annotations

import json
from pathlib import Path

from app.commit.models import ChapterPreviewPackage
from app.services.trial_artifacts import write_new


async def build_review_before_state(session, package):
    """Complete review context, not the intentionally small snapshot hash view."""
    import uuid
    from app.context.builder import ContextBuilder
    from app.memory.dry_run import build_memory_before_state
    context = await ContextBuilder(session).build_extraction_context(
        uuid.UUID(package.novel_id), chapter_number=package.chapter_number,
        chapter_text=package.draft.full_text, chapter_plan=package.plan_result.plan,
        scene_plans=list(package.draft.scene_plans), draft=package.draft,
        draft_hash=package.memory_result.draft_hash)
    before = await build_memory_before_state(session, uuid.UUID(package.novel_id),
                                           package.chapter_number, context)
    before["fact_catalog"] = [fact.model_dump(mode="json") for fact in context.fact_catalog]
    before["character_knowledge"] = [row.model_dump(mode="json") for row in context.character_knowledge]
    before["power_realms"] = context.power_realms
    before["locations"] = [row.model_dump(mode="json") for row in context.locations]
    return before


def declare_draft_items(draft, declarations):
    """Explicit editorial metadata, not extractor-driven whitelist expansion.

    No prose change and no canonical write. Exact scene-local evidence and
    a verbatim item name are required; proposals still face domain validation.
    """
    return _declare_draft_entities(draft, declarations, "item")


def declare_draft_locations(draft, declarations):
    """Explicit editorial location registration with the same exact-evidence gate."""
    return _declare_draft_entities(draft, declarations, "location")


def _declare_draft_entities(draft, declarations, entity_type):
    from app.draft.models import TemporaryEntityInfo
    from app.memory.catalog import EvidenceCatalogBuilder
    from app.schemas.studio import ItemDeclarationInput
    result = draft.model_copy(deep=True)
    entries = EvidenceCatalogBuilder.build(draft).by_id()
    refs = {entity.temporary_ref for entity in draft.temporary_entities}
    for value in declarations:
        declaration = ItemDeclarationInput.model_validate(value)
        if declaration.temporary_ref in refs:
            raise ValueError("临时声明已存在，不允许覆盖实体类型或身份。")
        evidence = [entries.get(ref) for ref in declaration.evidence_ids]
        if any(entry is None or entry.scene_id != declaration.source_scene_id for entry in evidence):
            raise ValueError("补充物品声明必须使用同一来源场景的有效证据 ID。")
        if not any(declaration.name in entry.exact_text for entry in evidence):
            raise ValueError("物品名称必须逐字存在于所选证据中。")
        result.temporary_entities.append(TemporaryEntityInfo(
            temporary_ref=declaration.temporary_ref, entity_type=entity_type, name_hint=declaration.name,
            introduced_in_scene=declaration.source_scene_id, role="编辑根据正文精确证据补充的实体声明"))
        refs.add(declaration.temporary_ref)
    return result


def export_author_review(package: ChapterPreviewPackage, destination: Path) -> Path:
    """Export editable scene copies plus the actual operations and their evidence.

    Existing reading copies may contain the author's edits; leave them alone.
    Immutable preview_package.json remains the source for validation/finalize.
    """
    destination.mkdir(parents=True, exist_ok=True)
    scenes_dir = destination / "scenes"
    scenes_dir.mkdir(exist_ok=True)
    memory = package.memory_result
    batch_path = destination / "memory_batch.json"
    if not batch_path.exists():
        batch = memory.batch.model_dump(mode="json", exclude_none=True)
        if memory.batch.contract_version == 3:
            # Quotes/offsets are derived by the server, not fields for authors to maintain.
            for operation in batch["operations"]:
                operation.pop("evidence", None)
        write_new(batch_path, batch)
    lines = [
        f"# 第 {package.chapter_number} 章：{package.draft.title or '未命名'}",
        "",
        "作者审阅副本，可能包含未公开设定；不要直接作为读者版发布。",
        "修改分场景文件后使用 revise 生成新候选；本报告不会自动批准或提交。",
        "仅修正记忆时编辑 memory_batch.json，再用 memory-edit 本地验证，无需 API。",
        "",
        f"- 正文：{len(package.draft.full_text)} 字符，{len(package.draft.scenes)} 场景",
        f"- Continuity：{'通过' if package.continuity_report.valid else '未通过'}",
        f"- Memory：{'通过' if memory.validation_report.valid else '未通过'}",
        f"- Dry-run：{'可应用' if memory.dry_run and memory.dry_run.applicable else '不可应用'}",
        f"- Preview hash：`{package.preview_package_hash}`",
        "",
        "## 分场景正文",
        "",
    ]
    index = []
    for position, scene in enumerate(package.draft.scenes, 1):
        filename = f"scene_{position:02d}.txt"
        path = scenes_dir / filename
        if not path.exists():
            write_new(path, scene.text, text=True)
        index.append({"scene_id": scene.scene_id, "file": filename})
        lines.append(f"- `{scene.scene_id}`：[{filename}](scenes/{filename})，{len(scene.text)} 字符")
    if not (scenes_dir / "index.json").exists():
        write_new(scenes_dir / "index.json", index)

    lines += ["", "## 连续性问题", ""]
    for issue in package.continuity_report.issues:
        lines.append(f"- [{issue.severity.value}] {issue.scene_id or '全章'}：{issue.author_explanation}")
    if not package.continuity_report.issues:
        lines.append("检查器未报告问题；这不代替人工语义审阅。")
    lines += ["", "## 记忆变化（逐条核对）", "",
              "在原 attempt 的 review.json 中标记 correct / missing / wrong / over-extracted。",
              "额外漏提记录到 missing；五项质量分别按 1–5 分打分。", ""]
    if not memory.batch.operations:
        lines.append("没有抽取到任何操作，不能仅凭格式合法视为验收通过。")
    for op in memory.batch.operations:
        lines += [f"### {op.operation_id} · {op.update_type.value}", "",
                  f"来源场景：`{op.source_scene_id}`", ""]
        payload = op.model_dump(mode="json", exclude_none=True)
        for key in ("evidence", "evidence_refs", "operation_id", "update_type", "source_scene_id"):
            payload.pop(key, None)
        lines += ["```json", json.dumps(payload, ensure_ascii=False, indent=2), "```", "", "证据：", ""]
        for evidence in op.evidence:
            lines.extend("> " + line for line in evidence.exact_quote.splitlines())
        for issue in memory.validation_report.issues:
            if issue.operation_id == op.operation_id:
                lines.append(f"\n- [{issue.severity.value}] {issue.code}：{issue.message}")
        lines.append("")
    lines += ["## 批次级问题", ""]
    lines.extend(f"- {issue.code}：{issue.message}" for issue in memory.validation_report.issues
                 if issue.operation_id is None)
    report = destination / "review.md"
    if not report.exists():
        write_new(report, "\n".join(lines) + "\n", text=True)
    return report
