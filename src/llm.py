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
class EmbeddingResult:
    vectors: list[list[float]]
    model: str
    prompt_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: float = 0.0
    api_texts: int = 0       # texts that actually hit the API
    cached_texts: int = 0    # texts served from the embedding cache


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: float = 0.0
    cached: bool = False
    # Part of `completion_tokens` a reasoning model spent thinking. Zero for
    # a model that does not reason, or one told not to.
    reasoning_tokens: int = 0
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
    def _cache_key(self, model: str, messages: list, temperature: float, max_tokens: int,
                   reasoning_effort: str | None = None) -> str:
        # The payload for the gpt-4o/4.1 generation is byte-identical to what
        # it always was, so every cached response behind the published runs
        # still hits. A reasoning effort is added to the key only when one is
        # sent, because the same prompt with reasoning on and off are two
        # different experiments.
        payload = {"model": model, "messages": messages, "temperature": temperature,
                   "max_tokens": max_tokens}
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

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
        # What this model is actually sent. The gpt-4o/4.1 generation gets
        # temperature + max_tokens as before; a newer model gets whatever its
        # API accepts (see config.api_params), and the cache key reflects it.
        params = config.api_params(model, max_tokens, temperature)
        key = self._cache_key(model, messages, params.get("temperature"), max_tokens,
                              params.get("reasoning_effort"))

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
                    reasoning_tokens=hit.get("reasoning_tokens", 0),
                    # Carried through so a truncated reply stays visible on a
                    # cached rerun: System B counts those.
                    meta={"finish_reason": hit.get("finish_reason"),
                          "reasoning_tokens": hit.get("reasoning_tokens", 0)},
                )

        last_error: Exception | None = None
        for attempt in range(config.MAX_RETRIES):
            try:
                started = time.perf_counter()
                completion = self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    **params,
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
        # Reasoning tokens are inside completion_tokens (and so inside the
        # cost); they are pulled out separately so a run can say how much of
        # its output budget went on thinking rather than answering.
        details = getattr(usage, "completion_tokens_details", None) if usage else None
        reasoning_tokens = getattr(details, "reasoning_tokens", 0) or 0
        cost = config.price_of(model, prompt_tokens, completion_tokens)

        record = {
            "text": text,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "reasoning_tokens": reasoning_tokens,
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
            reasoning_tokens=reasoning_tokens,
            meta={"finish_reason": record["finish_reason"],
                  "reasoning_tokens": reasoning_tokens},
        )

    # --- embeddings -------------------------------------------------------
    def embed(self, texts: list[str], model: str = config.EMBEDDING_MODEL) -> EmbeddingResult:
        """Embed many texts, batching the API calls.

        Cached per individual text, not per batch, so a changed batch boundary
        still reuses every vector it has already paid for. The cache namespace
        is distinct from the chat cache ("emb:" prefix in the key payload), so
        the two can never collide.
        """
        vectors: list[list[float] | None] = [None] * len(texts)
        pending: list[int] = []

        if self.use_cache:
            for index, text in enumerate(texts):
                hit = self._cache_read(self._embed_key(model, text))
                if hit is not None:
                    vectors[index] = hit["vector"]
                    self.cache_hits += 1
                else:
                    pending.append(index)
        else:
            pending = list(range(len(texts)))

        prompt_tokens = 0
        latency = 0.0
        for start in range(0, len(pending), config.EMBEDDING_BATCH):
            batch = pending[start:start + config.EMBEDDING_BATCH]
            payload = [texts[i] for i in batch]

            last_error: Exception | None = None
            for attempt in range(config.MAX_RETRIES):
                try:
                    began = time.perf_counter()
                    response = self.client.embeddings.create(model=model, input=payload)
                    latency += time.perf_counter() - began
                    break
                except RETRYABLE as err:
                    last_error = err
                    delay = config.RETRY_BASE_DELAY * (2 ** attempt)
                    print(f"  [embed retry {attempt + 1}/{config.MAX_RETRIES}] "
                          f"{type(err).__name__}; sleeping {delay:.0f}s")
                    time.sleep(delay)
            else:
                raise RuntimeError(
                    f"{model} embeddings failed after {config.MAX_RETRIES} retries"
                ) from last_error

            self.calls += 1
            prompt_tokens += response.usage.prompt_tokens if response.usage else 0
            for index, item in zip(batch, response.data):
                vectors[index] = item.embedding
                if self.use_cache:
                    self._cache_write(self._embed_key(model, texts[index]),
                                      {"vector": item.embedding})

        return EmbeddingResult(
            vectors=[v for v in vectors if v is not None],
            model=model,
            prompt_tokens=prompt_tokens,
            latency_s=latency,
            cost_usd=config.price_of(model, prompt_tokens, 0),
            api_texts=len(pending),
            cached_texts=len(texts) - len(pending),
        )

    def _embed_key(self, model: str, text: str) -> str:
        payload = json.dumps({"kind": "emb", "model": model, "text": text}, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
