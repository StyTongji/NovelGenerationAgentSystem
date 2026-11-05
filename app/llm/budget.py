"""Crash-conservative, process-safe usage ledger for opt-in real trials.

Reserve before sending. Missing usage, network failure, cancellation and
process death retain the reservation, never charge zero or replay for free.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from uuid import uuid4

from app.llm.errors import ProviderRequestError


class BudgetExceededError(ProviderRequestError):
    pass


class TrialBudget:
    def __init__(self, path: Path, chapter: int, *, max_calls=30,
                 chapter_tokens=150_000, run_tokens=2_000_000):
        self.path = path
        self.chapter = str(chapter)
        self.max_calls = max_calls
        self.chapter_tokens = chapter_tokens
        self.run_tokens = run_tokens

    def _summary(self, rows):
        chapter = [r for r in rows.values() if r["chapter"] == self.chapter]
        def totals(values):
            return {"calls": len(values), "charged_tokens": sum(r["charged_tokens"] for r in values),
                    "known_tokens": sum(r["charged_tokens"] for r in values if r["usage_known"]),
                    "unknown_calls": sum(not r["usage_known"] for r in values),
                    "unknown_reserved_tokens": sum(r["charged_tokens"] for r in values if not r["usage_known"])}
        return {"chapter_number": int(self.chapter), "chapter": totals(chapter), "run": totals(list(rows.values())),
                "limits": {"max_calls": self.max_calls, "chapter_tokens": self.chapter_tokens, "run_tokens": self.run_tokens}}

    def summary(self):
        # Ledger publication is atomic; readers see either complete version.
        rows = json.loads(self.path.read_text())["reservations"] if self.path.exists() else {}
        return self._summary(rows)

    def _change(self, change):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            ledger = json.loads(self.path.read_text()) if self.path.exists() else {"reservations": {}}
            result = change(ledger["reservations"])
            temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}")
            with temporary.open("x") as output:
                json.dump(ledger, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.path)
            return result

    def reserve(self, messages, max_tokens: int | None) -> str:
        if max_tokens is None:
            raise BudgetExceededError("A bounded max_tokens is required for real trials")
        # UTF-8 bytes deliberately overestimate tokens, including per-message framing.
        estimate = sum(len(m.content.encode()) + 32 for m in messages) + max_tokens
        def change(rows):
            chapter = [r for r in rows.values() if r["chapter"] == self.chapter]
            if (len(chapter) >= self.max_calls
                or sum(r["charged_tokens"] for r in chapter) + estimate > self.chapter_tokens
                or sum(r["charged_tokens"] for r in rows.values()) + estimate > self.run_tokens):
                summary = self._summary(rows)
                reasons = []
                if len(chapter) >= self.max_calls:
                    reasons.append(f"本章请求次数 {len(chapter)}/{self.max_calls}")
                if summary["chapter"]["charged_tokens"] + estimate > self.chapter_tokens:
                    reasons.append(f"本章已计入 {summary['chapter']['charged_tokens']:,} + 下次预留 {estimate:,} > 上限 {self.chapter_tokens:,} tokens")
                if summary["run"]["charged_tokens"] + estimate > self.run_tokens:
                    reasons.append(f"任务已计入 {summary['run']['charged_tokens']:,} + 下次预留 {estimate:,} > 上限 {self.run_tokens:,} tokens")
                raise BudgetExceededError("Trial call/token budget exhausted; no request sent · " + "；".join(reasons)
                    + "。可在“调整预算”中提高上限，再恢复；已完成阶段和历史用量会保留。")
            reservation = uuid4().hex
            rows[reservation] = {"chapter": self.chapter, "charged_tokens": estimate,
                                 "reserved_tokens": estimate, "usage_known": False}
            return reservation
        return self._change(change)

    def settle(self, reservation: str, tokens: int | None):
        if tokens is None:
            return
        def change(rows):
            rows[reservation].update(charged_tokens=tokens, usage_known=True)
        self._change(change)
