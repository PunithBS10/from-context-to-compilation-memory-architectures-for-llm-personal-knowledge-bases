"""Central configuration. Nothing model-specific belongs in the system classes.

Every value here is written into the results file so any run can be reproduced.
"""
from pathlib import Path

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
LOCOMO_PATH = DATA_DIR / "locomo" / "locomo10.json"
RESULTS_DIR = ROOT / "results"
CACHE_DIR = ROOT / ".cache"

# --- Models -----------------------------------------------------------------
# The SAME answering model must be used for systems L, A, B, C, D, or the
# comparison is invalid. The judge is fixed and separate.
# Decision (Aug 2026): gpt-4o-mini is the answering model for ALL systems.
# Rationale: the thesis claim is about the DIFFERENCE between systems, not peak
# accuracy, so a cheaper model is valid — and it makes a full benchmark of all
# five systems affordable (~$7 per full L run instead of ~$89), avoiding
# sampled runs and asterisks in the results table.
ANSWER_MODEL = "gpt-4o-mini"

# Judge is deliberately a DIFFERENT (and stronger) model than the answerer, to
# avoid a model preferring its own outputs.
JUDGE_MODEL = "gpt-4.1-mini"

# Context window of ANSWER_MODEL, in tokens. Used to detect overflow BEFORE
# calling the API, so we never silently truncate a conversation.
# gpt-4o-mini = 128k. Largest LoCoMo transcript is ~27k tokens, so no overflow
# is expected; the check stays in place to prove that rather than assume it.
MODEL_CONTEXT_LIMIT = 128_000

# --- System A (RAG) ---------------------------------------------------------
# Conventional settings, fixed deliberately and NOT hand-tuned. The strongest
# attack on this thesis is "you beat a weak RAG", so any change here needs a
# documented reason and a re-run.
CHUNK_TURNS = 4        # a lone turn is often meaningless ("Wow, that's cool!")
CHUNK_OVERLAP = 1      # stops a question and its answer splitting across chunks
RAG_K = 5              # chunks retrieved per question; 10 is run as a sweep
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_BATCH = 128  # texts per embeddings request

TEMPERATURE = 0.0
MAX_ANSWER_TOKENS = 256
MAX_JUDGE_TOKENS = 200

# Tokens reserved for the completion + prompt-format slack when checking overflow.
CONTEXT_SAFETY_MARGIN = 2_000

# --- Prices, USD per 1M tokens ----------------------------------------------
# Verify against current OpenAI pricing before a full run; cost figures in the
# results files are only as good as this table.
PRICES = {
    "gpt-4.1":      {"input": 2.00, "output": 8.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "gpt-4o":       {"input": 2.50, "output": 10.00},
    "gpt-4o-mini":  {"input": 0.15, "output": 0.60},
    # Embeddings bill input only; output stays 0 so the same helper works.
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
    "text-embedding-3-large": {"input": 0.13, "output": 0.0},
}

# --- API behaviour ----------------------------------------------------------
MAX_RETRIES = 5
RETRY_BASE_DELAY = 2.0
REQUEST_TIMEOUT = 60.0
USE_CACHE = True


def price_of(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """USD cost of one call. Unknown model -> 0.0, flagged by `is_priced`."""
    p = PRICES.get(model)
    if not p:
        return 0.0
    return (prompt_tokens * p["input"] + completion_tokens * p["output"]) / 1_000_000


def is_priced(model: str) -> bool:
    return model in PRICES


def as_dict() -> dict:
    """Config snapshot recorded in every results file."""
    return {
        "answer_model": ANSWER_MODEL,
        "judge_model": JUDGE_MODEL,
        "model_context_limit": MODEL_CONTEXT_LIMIT,
        "temperature": TEMPERATURE,
        "max_answer_tokens": MAX_ANSWER_TOKENS,
        "max_judge_tokens": MAX_JUDGE_TOKENS,
        "chunk_turns": CHUNK_TURNS,
        "chunk_overlap": CHUNK_OVERLAP,
        "rag_k": RAG_K,
        "embedding_model": EMBEDDING_MODEL,
        "context_safety_margin": CONTEXT_SAFETY_MARGIN,
        "prices_usd_per_1m": PRICES,
    }
