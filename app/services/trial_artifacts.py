"""Immutable trial attempts and hash-bound, resumable stage checkpoints."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def safe_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError("run/attempt names must contain only letters, digits, underscores or hyphens")
    return value


def write_new(path: Path, value, *, text=False) -> None:
    # A checkpoint is atomically published only after complete serialization.
    import os
    import uuid
    content = value if text else json.dumps(value, ensure_ascii=False, indent=2)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)  # publish without replacing an existing artifact
    finally:
        temporary.unlink(missing_ok=True)


class TrialAttempt:
    def __init__(self, path: Path, signature: dict, *, resume=False):
        self.path = path
        self.signature = signature
        self.signature_hash = digest(signature)
        if resume:
            manifest = json.loads((path / "manifest.json").read_text())
            if manifest["signature"] != signature or manifest["signature_hash"] != self.signature_hash:
                raise ValueError("Resume refused: story state, input, model or prompt version changed")
        else:
            path.mkdir(parents=True, exist_ok=False)
            write_new(path / "manifest.json", {"signature": signature, "signature_hash": self.signature_hash})

    async def stage(self, name: str, model, produce):
        path = self.path / f"stage_{safe_name(name)}.json"
        if path.exists():
            saved = json.loads(path.read_text())
            if saved["signature_hash"] != self.signature_hash or saved["hash"] != digest(saved["value"]):
                raise ValueError(f"Checkpoint integrity failure: {name}")
            return model.model_validate(saved["value"])
        result = await produce()
        value = result.model_dump(mode="json")
        write_new(path, {"signature_hash": self.signature_hash, "hash": digest(value), "value": value})
        return result

    def save_package(self, package):
        from app.commit.models import ReviewApproval
        path = self.path / "preview_package.json"
        if path.exists():
            if json.loads(path.read_text()) != package.model_dump(mode="json"):
                raise ValueError("Attempt already contains a different preview")
            return
        artifacts = {
            "memory_preview.json": package.memory_result.model_dump(mode="json"),
            "validation_report.json": package.memory_result.validation_report.model_dump(mode="json"),
            "dry_run.json": package.memory_result.dry_run.model_dump(mode="json") if package.memory_result.dry_run else None,
        }
        for name, value in artifacts.items():
            target = self.path / name
            if target.exists():
                if json.loads(target.read_text()) != value:
                    raise ValueError(f"Partial artifact integrity failure: {name}")
            else:
                write_new(target, value)
        chapter = self.path / "chapter.txt"
        if chapter.exists():
            if chapter.read_text() != package.draft.full_text:
                raise ValueError("Partial chapter integrity failure")
        else:
            write_new(chapter, package.draft.full_text, text=True)
        review = {
            "attempt_id": self.path.name, "reviewed_by": "", "approved": False,
            "draft_hash": hashlib.sha256(package.draft.full_text.encode()).hexdigest(),
            "memory_hash": ReviewApproval.hash_memory(package.memory_result),
            "preview_package_hash": package.preview_package_hash,
            "operation_reviews": {op.operation_id: "missing" for op in package.memory_result.batch.operations},
            "missing": [], "semantic_review_complete": False,
            "quality_scores": {k: 1 for k in ("readability", "character", "dialogue", "continuity", "progression")},
        }
        if not (self.path / "review.json").exists():
            write_new(self.path / "review.json", review)
        # Completion marker is published last; interrupted saves can be resumed.
        from app.services.author_review import export_author_review
        export_author_review(package, self.path / "reading")
        write_new(path, package.model_dump(mode="json"))
