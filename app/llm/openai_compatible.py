"""OpenAI-compatible chat-completions provider (httpx based).

Works with OpenAI, DeepSeek, Qwen (DashScope compatible mode), vLLM,
and any OpenAI-compatible gateway. Configured entirely via settings:
base_url, api_key_env, model, timeout, retries — nothing hardcoded.

Secrets: the API key is read from the environment, stored privately and
scrubbed from every raised error message and log line.
"""

from __future__ import annotations

import asyncio
import logging
import time
import json
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import SecretStr

from app.llm.base import BaseLLMProvider
from app.llm.errors import (
    ProviderConfigurationError,
    ProviderRequestError,
    ProviderTimeoutError,
    scrub_secrets,
)
from app.llm.models import LLMMessage, Usage

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}


class OpenAICompatibleProvider(BaseLLMProvider):
    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key_env: str = "NOVEL_LLM_API_KEY",
        api_key: SecretStr | None = None,
        model: str = "",
        timeout_seconds: float = 120.0,
        connect_timeout_seconds: float = 10.0,
        deadline_seconds: float = 240.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
        http_client: httpx.AsyncClient | None = None,
        budget=None,
        trace_path: Path | None = None,
        json_mode: bool = False,
        enable_thinking: bool | None = None,
    ) -> None:
        if not base_url:
            raise ProviderConfigurationError("base_url must not be empty")
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self._api_key = api_key
        self.default_model = model
        self.timeout_seconds = timeout_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self.deadline_seconds = deadline_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self._client = http_client  # injectable for tests
        self.budget = budget
        self.trace_path = trace_path
        self.json_mode = json_mode
        self.enable_thinking = enable_thinking
        self.model_capabilities = {}
        self.request_cutoffs = {}
        self.event_callback = None
        self._prompt_version = ContextVar(f"provider_prompt_{id(self)}", default=None)
        self._input_metadata = ContextVar(f"provider_input_{id(self)}", default=None)
        self._trace = ContextVar(f"provider_trace_{id(self)}", default=None)

    async def _with_trace(self, method, request):
        token = self._trace.set([])
        prompt_token = self._prompt_version.set(request.metadata.get("prompt_version"))
        input_token = self._input_metadata.set({k: request.metadata.get(k) for k in ("input_hash", "input_utf8_bytes")})
        try:
            result = await method(request)
            attempts = self._trace.get()
            result.metadata.extra["http_attempts"] = attempts
            result.metadata.extra["usage_complete"] = all(a.get("usage_known", False) for a in attempts)
            if attempts:
                result.metadata.extra.update({
                    "requested_model": request.model or self.default_model,
                    "response_model": attempts[-1].get("response_model"),
                    "request_id": attempts[-1].get("request_id"),
                })
            return result
        finally:
            self._trace.reset(token)
            self._prompt_version.reset(prompt_token)
            self._input_metadata.reset(input_token)

    async def generate_text(self, request):
        return await self._with_trace(super().generate_text, request)

    async def generate_structured(self, request):
        return await self._with_trace(super().generate_structured, request)

    def _record(self, record):
        record = {**record, "prompt_version": self._prompt_version.get()}
        record.update(self._input_metadata.get() or {})
        if self._trace.get() is not None:
            self._trace.get().append(record)
        if self.trace_path:
            self.trace_path.parent.mkdir(parents=True, exist_ok=True)
            with self.trace_path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self.event_callback:
            self.event_callback({"message": f"API · {record.get('agent', '')} · {record.get('status', '')}", **record})

    # ------------------------------------------------------------------ setup

    def _get_api_key(self) -> str:
        from app.llm.credentials import read_api_key
        api_key = self._api_key.get_secret_value().strip() if self._api_key is not None else read_api_key(self.api_key_env)
        if not api_key:
            raise ProviderConfigurationError(
                f"Environment variable {self.api_key_env!r} is not set; "
                "cannot authenticate against the LLM provider"
            )
        return api_key

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # trust_env=False: do NOT pick up system proxy env vars
            # (HTTP_PROXY / all_proxy etc). LLM API endpoints are called
            # directly; users who need a proxy can set `proxy` explicitly.
            self._client = httpx.AsyncClient(
                timeout=self.timeout_seconds, trust_env=False
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -------------------------------------------------------------- completion

    async def _request_completion(self, payload, api_key, timeout, progress):
        kwargs = {"json": payload, "headers": {"Authorization": f"Bearer {api_key}"},
                  "timeout": httpx.Timeout(timeout, connect=self.connect_timeout_seconds)}
        url = f"{self.base_url}/chat/completions"
        if not payload.get("stream"):
            return await self._ensure_client().post(url, **kwargs)
        # Consume only final-answer content. Reasoning chunks are deliberately
        # neither retained nor exposed; they merely keep the read socket alive.
        async with self._ensure_client().stream("POST", url, **kwargs) as response:
            if response.status_code != 200:
                await response.aread()
                return response
            body = {"choices": [], "usage": None}
            parts, finish, done, size = [], None, False, 0
            started = last_log = time.perf_counter()
            progress.update(chunks=0, answer_chars=0)
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done = True
                    break
                try:
                    chunk = json.loads(data)
                except ValueError as exc:
                    raise ProviderRequestError("Invalid streaming JSON frame") from exc
                if not isinstance(chunk, dict) or chunk.get("error"):
                    raise ProviderRequestError("Provider returned a streaming error")
                progress["chunks"] += 1
                progress.setdefault("first_chunk_ms", (time.perf_counter() - started) * 1000)
                for key in ("id", "model", "usage"):
                    if chunk.get(key) is not None:
                        body[key] = chunk[key]
                for choice in chunk.get("choices", []):
                    if choice.get("index", 0) != 0:
                        continue
                    content = (choice.get("delta") or {}).get("content")
                    if content:
                        if not isinstance(content, str):
                            raise ProviderRequestError("Unexpected streaming content type")
                        size += len(content.encode())
                        if size > 2_000_000:
                            raise ProviderRequestError("Streaming response exceeded safety limit")
                        parts.append(content)
                        progress["answer_chars"] += len(content)
                    finish = choice.get("finish_reason") or finish
                if time.perf_counter() - last_log >= 15:
                    logger.info("llm stream progress model=%s chunks=%d answer_chars=%d elapsed_s=%.0f",
                                payload["model"], progress["chunks"], progress["answer_chars"], time.perf_counter() - started)
                    last_log = time.perf_counter()
            if not done or finish is None:
                raise ProviderRequestError("Incomplete streaming response; no completion marker")
            body["choices"] = [{"message": {"content": "".join(parts)}, "finish_reason": finish}]
            return httpx.Response(200, json=body)

    async def _complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str,
        temperature: float,
        max_tokens: int | None,
        timeout_seconds: float | None,
        agent: str | None = None,
        mode: str = "text",
    ) -> tuple[str, str | None, Usage]:
        if not model:
            raise ProviderConfigurationError(
                "No model configured: set model on the request or provider default"
            )
        api_key = self._get_api_key()
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        capabilities = self.model_capabilities.get(model, {})
        if capabilities.get("stream_ok"):
            payload.update(stream=True, stream_options={"include_usage": True})
        if (self.json_mode or capabilities.get("json_ok")) and mode == "structured":
            payload["response_format"] = {"type": "json_object"}
        if capabilities.get("thinking_disabled_accepted"):
            # DeepSeek-native switch is distinct from Qwen's enable_thinking.
            # Gate by observed capability; unknown custom endpoints get neither.
            payload["thinking"] = {"type": "disabled"}
        elif self.enable_thinking is not None:
            payload["enable_thinking"] = self.enable_thinking
        elif capabilities.get("enable_thinking_false_accepted"):
            payload["enable_thinking"] = False

        timeout = timeout_seconds or self.timeout_seconds
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            cutoff = self.request_cutoffs.get(model)
            if cutoff is not None and datetime.now(UTC) >= cutoff:
                from app.llm.budget import BudgetExceededError
                raise BudgetExceededError(f"Request window closed for model {model}; no request sent")
            reservation = self.budget.reserve(messages, max_tokens) if self.budget else None
            start = time.perf_counter()
            record = {"at": datetime.now(UTC).isoformat(), "agent": agent,
                      "requested_model": model, "mode": mode, "attempt": attempt + 1,
                      "reservation": reservation, "usage_known": False,
                      "stream": bool(payload.get("stream"))}
            progress = {}
            if payload.get("stream"):
                record["stream_progress"] = progress
            logger.info(
                "llm call started provider=openai_compatible agent=%s model=%s mode=%s attempt=%d timeout_s=%s",
                agent or "unknown", model, mode, attempt + 1, timeout,
            )
            if self.event_callback:
                self.event_callback({**record, "status": "started", "message": f"API 请求 · {agent} · {model}"})
            try:
                response = await self._request_completion(payload, api_key, timeout, progress)
            except ProviderRequestError:
                self._record({**record, "status": "protocol_error", "latency_ms": (time.perf_counter() - start) * 1000})
                raise
            except asyncio.CancelledError:
                self._record({**record, "status": "cancelled", "latency_ms": (time.perf_counter() - start) * 1000})
                raise
            except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                self._record({**record, "status": "timeout"})
                logger.warning(
                    "llm call timed out provider=openai_compatible agent=%s model=%s mode=%s attempt=%d timeout_s=%s",
                    agent or "unknown", model, mode, attempt + 1, timeout,
                )
                raise ProviderTimeoutError(
                    f"LLM request timed out after {timeout}s (model={model})"
                ) from exc
            except httpx.HTTPError as exc:
                self._record({**record, "status": "transport_error"})
                logger.warning(
                    "llm transport error provider=openai_compatible agent=%s model=%s mode=%s attempt=%d",
                    agent or "unknown", model, mode, attempt + 1,
                )
                last_error = ProviderRequestError(
                    scrub_secrets(f"LLM transport error: {exc}", api_key)
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_backoff_seconds * (2**attempt))
                    continue
                raise last_error from exc

            latency_ms = (time.perf_counter() - start) * 1000
            if response.status_code == 200:
                try:
                    body = response.json()
                    if not isinstance(body, dict):
                        raise ValueError("expected object")
                except ValueError as exc:
                    self._record({**record, "status": "invalid_response"})
                    raise ProviderRequestError("Provider returned a non-JSON completion") from exc
                choice = (body.get("choices") or [{}])[0]
                text = (choice.get("message") or {}).get("content") or ""
                finish_reason = choice.get("finish_reason")
                usage_raw = body.get("usage") or {}
                usage_known = all(isinstance(usage_raw.get(k), int) and usage_raw[k] >= 0
                                  for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
                if self.budget:
                    self.budget.settle(reservation, usage_raw["total_tokens"] if usage_known else None)
                response_model = body.get("model")
                self._record({**record, "status": "completed", "latency_ms": latency_ms,
                              "response_model": response_model, "request_id": body.get("id"),
                              "model_disclosed": bool(response_model and response_model != "Auto"),
                              "finish_reason": finish_reason, "usage_known": usage_known,
                              "usage": usage_raw if usage_known else None})
                usage = Usage(
                    prompt_tokens=usage_raw["prompt_tokens"] if usage_known else 0,
                    completion_tokens=usage_raw["completion_tokens"] if usage_known else 0,
                    total_tokens=usage_raw["total_tokens"] if usage_known else 0,
                )
                logger.info(
                    "llm call completed provider=openai_compatible agent=%s model=%s mode=%s attempt=%d latency_ms=%.0f finish_reason=%s prompt_tokens=%d completion_tokens=%d total_tokens=%d",
                    agent or "unknown", model, mode, attempt + 1, latency_ms, finish_reason,
                    usage.prompt_tokens, usage.completion_tokens, usage.total_tokens,
                )
                return text, finish_reason, usage

            self._record({**record, "status": response.status_code, "latency_ms": latency_ms})
            error_text = scrub_secrets(
                f"LLM provider error: HTTP {response.status_code}: {response.text[:500]}",
                api_key,
            )
            last_error = ProviderRequestError(error_text, status_code=response.status_code)
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                logger.warning(
                    "llm call retrying provider=openai_compatible agent=%s model=%s mode=%s attempt=%d status=%d",
                    agent or "unknown", model, mode, attempt + 1, response.status_code,
                )
                await asyncio.sleep(self.retry_backoff_seconds * (2**attempt))
                continue
            logger.warning(
                "llm call failed provider=openai_compatible agent=%s model=%s mode=%s attempt=%d status=%d latency_ms=%.0f",
                agent or "unknown", model, mode, attempt + 1, response.status_code, latency_ms,
            )
            raise last_error

        raise last_error or ProviderRequestError("LLM request failed without an error object")
