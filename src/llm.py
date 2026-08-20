"""Thin OpenAI wrapper shared by every system and by the judge.

Responsibilities, all of them things the spec asks for exactly once rather
than once per system:

* on-disk response cache keyed by (model, messages, temperature, max_tokens),
  so development reruns cost nothing and results stay stable;
* retry with exponential backoff on rate limits and transient errors, and a
  loud failure on everything else (a swallowed exception that returns an empty
  answer would quietly corrupt the accuracy numbers);
* token, latency and cost accounting for every call.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import tiktoken
from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

import config

RETRYABLE = (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: float = 0.0
    cached: bool = False
    meta: dict = field(default_factory=dict)


def count_tokens(text: str, model: str) -> int:
    """Token count used for the pre-flight context check."""
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("o200k_base")
    return len(encoding.encode(text))


class LLMClient:
    def __init__(self, use_cache: bool = config.USE_CACHE, cache_dir: Path = config.CACHE_DIR):
        load_dotenv(config.ROOT / ".env")
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Copy .env.example to .env and put your key in it."
            )
        self.client = OpenAI(api_key=api_key, timeout=config.REQUEST_TIMEOUT)
        self.use_cache = use_cache
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.calls = 0
        self.cache_hits = 0

    # --- cache ------------------------------------------------------------
    def _cache_key(self, model: str, messages: list, temperature: float, max_tokens: int) -> str:
        payload = json.dumps(
            {"model": model, "messages": messages, "temperature": temperature,
             "max_tokens": max_tokens},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _cache_read(self, key: str) -> dict | None:
        path = self.cache_dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None      # a corrupt cache entry is a cache miss, not a crash

    def _cache_write(self, key: str, value: dict) -> None:
        (self.cache_dir / f"{key}.json").write_text(
            json.dumps(value, ensure_ascii=False), encoding="utf-8"
        )

    # --- call -------------------------------------------------------------
    def complete(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float = config.TEMPERATURE,
    ) -> LLMResponse:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        key = self._cache_key(model, messages, temperature, max_tokens)

        if self.use_cache:
            hit = self._cache_read(key)
            if hit is not None:
                self.cache_hits += 1
                return LLMResponse(
                    text=hit["text"],
                    model=model,
                    prompt_tokens=hit["prompt_tokens"],
                    completion_tokens=hit["completion_tokens"],
                    # Latency and cost are those of the ORIGINAL call. Cached
                    # calls are flagged so aggregates can exclude them.
                    latency_s=hit.get("latency_s", 0.0),
                    cost_usd=hit.get("cost_usd", 0.0),
                    cached=True,
                )

        last_error: Exception | None = None
        for attempt in range(config.MAX_RETRIES):
            try:
                started = time.perf_counter()
                completion = self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                latency = time.perf_counter() - started
                break
            except RETRYABLE as err:
                last_error = err
                delay = config.RETRY_BASE_DELAY * (2 ** attempt)
                print(f"  [retry {attempt + 1}/{config.MAX_RETRIES}] {type(err).__name__}; "
                      f"sleeping {delay:.0f}s")
                time.sleep(delay)
            # Anything else propagates: fail loudly rather than score an empty answer.
        else:
            raise RuntimeError(
                f"{model} failed after {config.MAX_RETRIES} retries"
            ) from last_error

        self.calls += 1
        text = (completion.choices[0].message.content or "").strip()
        usage = completion.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        cost = config.price_of(model, prompt_tokens, completion_tokens)

        record = {
            "text": text,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_s": latency,
            "cost_usd": cost,
            "finish_reason": completion.choices[0].finish_reason,
        }
        if self.use_cache:
            self._cache_write(key, record)

        return LLMResponse(
            text=text,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_s=latency,
            cost_usd=cost,
            cached=False,
            meta={"finish_reason": record["finish_reason"]},
        )
