#!/usr/bin/env python3
"""Client for the running local Studio; keys are never command-line arguments.

python scripts/book_launch.py create --mode demo
python scripts/book_launch.py show <id>
python scripts/book_launch.py select <id> --topic topic_1 --research-hash <hash>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--mode", choices=["demo", "live"], default="demo")
    create.add_argument("--brief", default="玄幻，能力有代价，配角有独立目标，围绕人物选择持续推进")
    create.add_argument("--sample", type=Path)
    create.add_argument("--provider", default="scnet")
    create.add_argument("--model", default="DeepSeek-V4.1-Flash-1017")
    create.add_argument("--base-url")
    create.add_argument("--api-key-env", help="custom_input 的客户端环境变量名；值不落盘")
    create.add_argument("--token-budget", type=int, default=200000)
    create.add_argument("--max-calls", type=int, default=12)
    create.add_argument("--max-output-tokens", type=int, default=8192)
    create.add_argument("--capabilities", type=Path, help="已确认的模型能力 JSON")
    for command in ("show", "select", "approve", "resume", "cancel", "chapters", "budget", "fork-research"):
        sub = commands.add_parser(command)
        sub.add_argument("id")
        if command == "select":
            sub.add_argument("--topic", required=True)
            sub.add_argument("--research-hash", required=True)
        if command == "approve":
            sub.add_argument("--hash", required=True)
            sub.add_argument("--reviewed-by", required=True)
            sub.add_argument("--approve", action="store_true", required=True)
        if command in {"resume", "chapters"}:
            sub.add_argument("--api-key-env")
        if command == "chapters":
            sub.add_argument("--count", type=int, default=3)
            sub.add_argument("--target-chars", type=int, default=5000)
            sub.add_argument("--chapter-tokens", type=int, default=150000)
            sub.add_argument("--run-tokens", type=int, default=2000000)
            sub.add_argument("--max-calls", type=int, default=30)
        if command == "budget":
            sub.add_argument("--token-budget", type=int, required=True)
            sub.add_argument("--max-calls", type=int, required=True)
        if command == "fork-research":
            sub.add_argument("--provider", required=True)
            sub.add_argument("--model", required=True)
            sub.add_argument("--base-url")
            sub.add_argument("--api-key-env")
            sub.add_argument("--max-output-tokens", type=int, default=8192)
            sub.add_argument("--max-calls", type=int, default=12)
            sub.add_argument("--token-budget", type=int, default=200000)
            sub.add_argument("--capabilities", type=Path, help="已确认的模型能力 JSON")
    collect = commands.add_parser("collect")
    collect.add_argument("--output", type=Path, required=True)
    collect.add_argument("--rank-type", type=int, choices=[1, 2], default=2)
    collect.add_argument("--categories", type=int, nargs="+", default=[258, 257])
    commands.add_parser("list")
    args = parser.parse_args()
    base = args.server.rstrip("/") + "/api/v1/book-launch"

    def call(method, path="", body=None):
        r = httpx.request(method, base + path, json=body, timeout=60, trust_env=False)
        if r.is_error:
            raise ValueError(f"HTTP {r.status_code}: {r.text}")
        return r.json()

    def key(body):
        name = getattr(args, "api_key_env", None)
        if name:
            body["api_key"] = os.environ[name]
        return body

    if args.command == "create":
        if args.sample:
            sample = call("POST", "/sources/import", {"text": args.sample.read_text(encoding="utf-8"),
                          "format": "csv" if args.sample.suffix.lower() == ".csv" else "json"})
        elif args.mode == "demo":
            sample = call("GET", "/demo-sample")
        else:
            parser.error("live 模式必须 --sample；可先 collect 或导入 CSV")
        value = call("POST", body=key({"mode": args.mode, "brief": args.brief, "sample": sample,
            "provider": args.provider, "model": args.model, "custom_base_url": args.base_url,
            "token_budget": args.token_budget, "max_calls": args.max_calls, "max_output_tokens": args.max_output_tokens,
            "runtime_capabilities": json.loads(args.capabilities.read_text()) if args.capabilities else None}))
    elif args.command == "collect":
        if args.output.exists():
            parser.error("output 已存在；请选择新的文件名以保留历史样本")
        value = call("POST", "/sources/fanqie", {"categories": args.categories, "rank_type": args.rank_type})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        value = {"output": str(args.output.resolve()), "books": len(value["books"])}
    elif args.command == "list":
        value = call("GET")
    elif args.command == "show":
        value = call("GET", f"/{args.id}")
    elif args.command == "select":
        value = call("POST", f"/{args.id}/select", {"topic_id": args.topic, "research_hash": args.research_hash})
    elif args.command == "approve":
        value = call("POST", f"/{args.id}/approve", {"blueprint_hash": args.hash, "reviewed_by": args.reviewed_by, "approved": args.approve})
    elif args.command == "chapters":
        cfg = call("GET", f"/{args.id}")["session"]
        value = call("POST", f"/{args.id}/chapters", key({"novel_id": cfg["novel_id"],
            **{k: cfg["config"][k] for k in ("mode", "provider", "model", "custom_base_url", "max_output_tokens", "runtime_capabilities")},
            "chapter_count": args.count, "target_chars": args.target_chars, "max_calls": args.max_calls,
            "chapter_tokens": args.chapter_tokens, "run_tokens": args.run_tokens}))
    elif args.command == "budget":
        value = call("POST", f"/{args.id}/budget", {"max_calls": args.max_calls, "token_budget": args.token_budget})
    elif args.command == "fork-research":
        value = call("POST", f"/{args.id}/fork-research", key({"provider": args.provider, "model": args.model,
            "custom_base_url": args.base_url, "max_output_tokens": args.max_output_tokens,
            "max_calls": args.max_calls, "token_budget": args.token_budget,
            "runtime_capabilities": json.loads(args.capabilities.read_text()) if args.capabilities else None}))
    else:
        value = call("POST", f"/{args.id}/{args.command}", key({}))
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, httpx.HTTPError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
