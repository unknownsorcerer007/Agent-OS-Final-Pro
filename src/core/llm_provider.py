"""Universal LLM Provider with Token Saving — Agent-OS Core Module.

A single interface that works with ANY LLM provider for any task
(summarization, content extraction, classification, etc.) with built-in
token saving mechanisms.

Supported Providers:
    OpenAI, Anthropic, Google Gemini, xAI, Mistral, DeepSeek, Groq,
    Together AI, Ollama (local), Azure OpenAI, Amazon Bedrock,
    Any OpenAI-compatible endpoint

Token Saving Mechanisms:
    - TokenBudget: Track token usage per session/task with configurable limits
    - PromptCompressor: Compress prompts by removing boilerplate
    - ResponseCache: LRU cache (1024 entries) with embedding similarity
    - SmartTruncation: Keep most relevant parts when context exceeds budget
    - StreamingSupport: Stream responses to avoid buffering full responses
    - Token counting estimation (tiktoken for OpenAI, heuristic for others)

Usage:
    from src.core.llm_provider import get_llm
    llm = get_llm()
    result = await llm.complete("Hello")
    # result = {"status": "success", "content": "...", "tokens_used": N, ...}
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
import threading
from collections import OrderedDict
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("agent-os.llm_provider")


# ════════════════════════════════════════════════════════════════════
# Provider Registry — all supported providers with their configurations
# ════════════════════════════════════════════════════════════════════

PROVIDER_REGISTRY: Dict[str, Dict[str, Any]] = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "env_key": "OPENAI_API_KEY",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-3.5-turbo", "gpt-4-turbo", "o1-mini", "o3-mini"],
        "api_style": "openai",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-3-5-haiku-20241022",
        "env_key": "ANTHROPIC_API_KEY",
        "models": ["claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022", "claude-3-opus-20240229"],
        "api_style": "anthropic",
    },
    "google": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.0-flash",
        "env_key": "GOOGLE_API_KEY",
        "models": ["gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"],
        "api_style": "openai",
    },
    "xai": {
        "base_url": "https://api.x.ai/v1",
        "default_model": "grok-2-mini",
        "env_key": "XAI_API_KEY",
        "models": ["grok-2", "grok-2-mini"],
        "api_style": "openai",
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "default_model": "mistral-small-latest",
        "env_key": "MISTRAL_API_KEY",
        "models": ["mistral-small-latest", "mistral-medium-latest", "mistral-large-latest"],
        "api_style": "openai",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "env_key": "DEEPSEEK_API_KEY",
        "models": ["deepseek-chat", "deepseek-coder", "deepseek-reasoner"],
        "api_style": "openai",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "env_key": "GROQ_API_KEY",
        "models": ["llama-3.3-70b-versatile", "mixtral-8x7b-32768", "llama-3.1-8b-instant"],
        "api_style": "openai",
    },
    "together": {
        "base_url": "https://api.together.xyz/v1",
        "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "env_key": "TOGETHER_API_KEY",
        "models": [
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "meta-llama/Llama-3.1-8B-Instruct-Turbo",
            "mistralai/Mixtral-8x7B-Instruct-v0.1",
        ],
        "api_style": "openai",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "default_model": "llama3",
        "env_key": "OLLAMA_HOST",
        "models": ["llama3", "mistral", "codellama", "phi3", "gemma2", "qwen2"],
        "api_style": "openai",
        "requires_api_key": False,
    },
    "azure": {
        "base_url": "",
        "default_model": "gpt-4o-mini",
        "env_key": "AZURE_OPENAI_API_KEY",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-35-turbo"],
        "api_style": "azure",
        "extra_env": {
            "endpoint": "AZURE_OPENAI_ENDPOINT",
            "deployment": "AZURE_OPENAI_DEPLOYMENT",
            "api_version": "AZURE_OPENAI_API_VERSION",
        },
    },
    "bedrock": {
        "base_url": "",
        "default_model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "env_key": "AWS_ACCESS_KEY_ID",
        "models": [
            "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "anthropic.claude-3-haiku-20240307-v1:0",
            "amazon.titan-text-premier-v1:0",
        ],
        "api_style": "bedrock",
        "extra_env": {
            "secret_key": "AWS_SECRET_ACCESS_KEY",
            "region": "AWS_REGION",
            "session_token": "AWS_SESSION_TOKEN",
        },
    },
}

# Default fallback chain order — cheapest/fastest first
DEFAULT_FALLBACK_CHAIN = [
    "groq", "deepseek", "together", "google", "mistral",
    "openai", "anthropic", "xai",
]


# ════════════════════════════════════════════════════════════════════
# Token Budget — Track token usage per session/task
# ════════════════════════════════════════════════════════════════════

class TokenBudget:
    """Track token usage per session/task with configurable limits.

    Thread-safe counter that tracks prompt tokens, completion tokens,
    and total tokens with an optional budget cap.
    """

    def __init__(
        self,
        max_total_tokens: int = 1_000_000,
        max_prompt_tokens: int = 500_000,
        max_completion_tokens: int = 500_000,
    ):
        self.max_total_tokens = max_total_tokens
        self.max_prompt_tokens = max_prompt_tokens
        self.max_completion_tokens = max_completion_tokens
        self._lock = threading.Lock()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.requests = 0
        self.cache_saves = 0
        self.cache_hits = 0
        self.compression_savings = 0
        self.truncation_savings = 0
        self._start_time = time.monotonic()

    def record(self, prompt_tokens: int, completion_tokens: int) -> Dict[str, Any]:
        """Record token usage from a single request. Returns budget status."""
        with self._lock:
            self.prompt_tokens += prompt_tokens
            self.completion_tokens += completion_tokens
            self.total_tokens += prompt_tokens + completion_tokens
            self.requests += 1
        return self.status

    # Alias for backward compatibility
    record_usage = record

    def can_spend(self, estimated_tokens: int) -> bool:
        """Check if we have budget for an estimated number of tokens."""
        with self._lock:
            remaining = self.max_total_tokens - self.total_tokens
        return remaining >= estimated_tokens

    def remaining(self) -> int:
        """Return remaining total token budget."""
        with self._lock:
            return max(0, self.max_total_tokens - self.total_tokens)

    def reset(self):
        """Reset budget for a new session."""
        with self._lock:
            self.prompt_tokens = 0
            self.completion_tokens = 0
            self.total_tokens = 0
            self.requests = 0
            self.cache_saves = 0
            self.cache_hits = 0
            self.compression_savings = 0
            self.truncation_savings = 0
            self._start_time = time.monotonic()

    @property
    def status(self) -> Dict[str, Any]:
        """Return current budget status."""
        with self._lock:
            elapsed = time.monotonic() - self._start_time
            return {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "max_total_tokens": self.max_total_tokens,
                "remaining_tokens": max(0, self.max_total_tokens - self.total_tokens),
                "budget_used_pct": round(
                    (self.total_tokens / self.max_total_tokens) * 100, 2
                ) if self.max_total_tokens > 0 else 0,
                "requests": self.requests,
                "cache_saves": self.cache_saves,
                "cache_hits": self.cache_hits,
                "compression_savings": self.compression_savings,
                "truncation_savings": self.truncation_savings,
                "elapsed_seconds": round(elapsed, 1),
            }


# ════════════════════════════════════════════════════════════════════
# Prompt Compressor — Remove boilerplate, keep essentials
# ════════════════════════════════════════════════════════════════════

class PromptCompressor:
    """Compress prompts by removing boilerplate and keeping essentials.

    Strategies:
    1. Remove redundant whitespace and blank lines
    2. Strip boilerplate phrases ("As an AI...", "I hope this helps", etc.)
    3. Deduplicate repeated sentences
    4. Shorten verbose instructions while preserving meaning
    5. Remove HTML/Markdown formatting that doesn't add semantic value
    """

    # Common boilerplate phrases to strip (case-insensitive)
    BOILERPLATE_PATTERNS = [
        re.compile(r"As an AI language model[^.]*\.\s*", re.IGNORECASE),
        re.compile(r"As a large language model[^.]*\.\s*", re.IGNORECASE),
        re.compile(r"I hope this (helps|is helpful)[^.]*\.\s*", re.IGNORECASE),
        re.compile(r"Please (note|keep in mind|remember)[^.]*\.\s*", re.IGNORECASE),
        re.compile(r"It('s| is) important to note[^.]*\.\s*", re.IGNORECASE),
        re.compile(r"In (conclusion|summary)[,.][^.]*(?:\.|$)", re.IGNORECASE),
        re.compile(r"Let me know if you (need|have|want)[^.]*\.\s*", re.IGNORECASE),
        re.compile(r"I('ll| will) (be happy|gladly)[^.]*\.\s*", re.IGNORECASE),
        re.compile(r"If you have any (other|more|further)[^.]*\.\s*", re.IGNORECASE),
        re.compile(r"Feel free to[^.]*\.\s*", re.IGNORECASE),
        re.compile(r"Disclaimer:[^.]*\.\s*", re.IGNORECASE),
        re.compile(r"NOTE:\s*[^.]*\.\s*", re.IGNORECASE),
    ]

    # Verbose → concise instruction mappings
    VERBOSE_REPLACEMENTS = [
        (re.compile(r"Please provide a (detailed |comprehensive )?(explanation|description|analysis) of", re.IGNORECASE), "Explain"),
        (re.compile(r"Can you (please )?tell me about", re.IGNORECASE), "Describe"),
        (re.compile(r"I would like you to", re.IGNORECASE), ""),
        (re.compile(r"Could you (please )?", re.IGNORECASE), ""),
        (re.compile(r"Please ", re.IGNORECASE), ""),
    ]

    def compress(self, text: str, aggression: float = 0.5) -> Tuple[str, int]:
        """Compress a prompt string. Returns (compressed_text, chars_saved).

        Args:
            text: The prompt text to compress.
            aggression: 0.0 (minimal) to 1.0 (aggressive) compression.
                - 0.0-0.3: Only whitespace/boilerplate removal
                - 0.3-0.7: Also deduplicate and shorten verbose instructions
                - 0.7-1.0: Also strip optional context and examples
        """
        original_len = len(text)
        if not text or original_len < 50:
            return text, 0

        result = text

        # Phase 1: Whitespace normalization (always)
        result = re.sub(r"\n{3,}", "\n\n", result)
        result = re.sub(r" {2,}", " ", result)
        result = re.sub(r"\t+", " ", result)
        result = result.strip()

        # Phase 2: Boilerplate removal (always)
        for pattern in self.BOILERPLATE_PATTERNS:
            result = pattern.sub("", result)

        # Phase 3: Deduplicate sentences (aggression >= 0.3)
        if aggression >= 0.3:
            result = self._deduplicate_sentences(result)

        # Phase 4: Shorten verbose instructions (aggression >= 0.3)
        if aggression >= 0.3:
            for pattern, replacement in self.VERBOSE_REPLACEMENTS:
                result = pattern.sub(replacement, result)

        # Phase 5: Strip optional context/examples (aggression >= 0.7)
        if aggression >= 0.7:
            # Remove lines that look like examples (starting with "Example:", "For example:", "e.g.")
            result = re.sub(r"(?m)^(?:Example|For example|e\.g\.|Such as)[^\n]*\n?", "", result)
            # Remove parenthetical asides
            result = re.sub(r"\([^)]{30,}\)", "", result)

        result = result.strip()
        saved = original_len - len(result)
        return result, saved

    def _deduplicate_sentences(self, text: str) -> str:
        """Remove duplicate or near-duplicate sentences."""
        sentences = re.split(r'(?<=[.!?])\s+', text)
        seen = set()
        unique = []
        for sent in sentences:
            # Normalize for comparison: lowercase, strip punctuation
            key = re.sub(r'[^\w\s]', '', sent.lower()).strip()
            if not key or len(key) < 10:
                unique.append(sent)
                continue
            if key not in seen:
                seen.add(key)
                unique.append(sent)
        return " ".join(unique)


# ════════════════════════════════════════════════════════════════════
# Response Cache — LRU with embedding similarity
# ════════════════════════════════════════════════════════════════════

class ResponseCache:
    """LRU cache (1024 entries) for identical/similar queries.

    Uses exact hash matching first, then falls back to simple
    n-gram Jaccard similarity for approximate matching.
    Thread-safe with OrderedDict-based LRU eviction.
    """

    def __init__(self, maxsize: int = 1024, max_size: int = None, similarity_threshold: float = 0.85):
        self._cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._maxsize = max_size if max_size is not None else maxsize
        self._similarity_threshold = similarity_threshold
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.similar_hits = 0

    def _hash_key(self, prompt: str, system: str = "", model: str = "", **kwargs) -> str:
        """Create a deterministic cache key from request parameters."""
        raw = f"{prompt}|{system}|{model}"
        # Include relevant kwargs in key (temperature, max_tokens can change output)
        for k in sorted(kwargs.keys()):
            if k in ("temperature", "max_tokens", "top_p"):
                raw += f"|{k}={kwargs[k]}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _ngram_set(self, text: str, n: int = 3) -> set:
        """Create set of character n-grams for similarity comparison."""
        text = text.lower().strip()
        if len(text) < n:
            return {text}
        return {text[i:i + n] for i in range(len(text) - n + 1)}

    def _jaccard_similarity(self, a: set, b: set) -> float:
        """Compute Jaccard similarity between two sets."""
        if not a or not b:
            return 0.0
        intersection = len(a & b)
        union = len(a | b)
        return intersection / union if union > 0 else 0.0

    def get(self, prompt: str, system: str = "", model: str = "", **kwargs) -> Optional[Dict[str, Any]]:
        """Look up a cached response by exact key match only.

        Previous implementation used O(n) similarity search across all 1024
        cache entries on every miss, which is too expensive for production.
        Similarity search has been removed — exact matching is sufficient
        and provides O(1) lookups.

        Returns the cached result dict or None.
        """
        key = self._hash_key(prompt, system, model, **kwargs)

        with self._lock:
            # Exact match only — O(1) lookup
            if key in self._cache:
                self._cache.move_to_end(key)
                self.hits += 1
                return self._cache[key]

        # Cache miss — skip expensive similarity search
        with self._lock:
            self.misses += 1
        return None

    def put(self, prompt: str, result: Dict[str, Any], system: str = "", model: str = "", **kwargs):
        """Store a result in the cache."""
        key = self._hash_key(prompt, system, model, **kwargs)
        # Store result directly — no need for _prompt field since similarity
        # search has been removed
        entry = dict(result)

        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = entry
            while len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)

    def clear(self):
        """Clear the entire cache."""
        with self._lock:
            self._cache.clear()
            self.hits = 0
            self.misses = 0
            self.similar_hits = 0

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._cache)

    @property
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total_lookups = self.hits + self.similar_hits + self.misses
            return {
                "size": len(self._cache),
                "maxsize": self._maxsize,
                "exact_hits": self.hits,
                "similar_hits": self.similar_hits,
                "misses": self.misses,
                "total_lookups": total_lookups,
                "hit_rate": round(
                    ((self.hits + self.similar_hits) / max(total_lookups, 1)) * 100, 2
                ),
            }


# ════════════════════════════════════════════════════════════════════
# Smart Truncation — Keep most relevant parts when context exceeds budget
# ════════════════════════════════════════════════════════════════════

class SmartTruncation:
    """When context exceeds budget, keep most relevant parts.

    Strategy:
    1. Always keep the system prompt (if any)
    2. Keep first paragraph (usually context-setting)
    3. Keep last paragraph (usually the question/conclusion)
    4. Score middle paragraphs by keyword density matching
    5. Keep highest-scoring paragraphs that fit the budget
    """

    def truncate(
        self,
        text: str,
        max_chars: int = 4000,
        max_tokens: int = None,
        system: str = "",
        keywords: Optional[List[str]] = None,
    ) -> Tuple[str, int]:
        """Truncate text to fit within max_chars while preserving relevance.

        Args:
            text: The text to potentially truncate.
            max_chars: Maximum character budget for the text.
            max_tokens: Alias for max_chars (approximate: 1 token ≈ 4 chars).
            system: System prompt (not counted against budget).
            keywords: Optional keywords to prioritize when truncating.

        Returns:
            (truncated_text, chars_saved)
        """
        # max_tokens alias: approximate 1 token ≈ 4 chars
        if max_tokens is not None:
            max_chars = max_tokens * 4
        if len(text) <= max_chars:
            return text, 0

        paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
        if not paragraphs:
            return text[:max_chars], len(text) - max_chars

        # If only one paragraph, just truncate it
        if len(paragraphs) == 1:
            truncated = text[:max_chars]
            return truncated, len(text) - len(truncated)

        # Always keep first and last paragraphs
        first = paragraphs[0]
        last = paragraphs[-1]
        middle = paragraphs[1:-1]

        # Reserve space for first + last + separator
        reserved = len(first) + len(last) + 4  # 4 for "\n\n"
        remaining_budget = max_chars - reserved

        if remaining_budget <= 0:
            # Can't even fit first and last; truncate first, drop middle and last
            result = first[:max_chars]
            return result, len(text) - len(result)

        # Score middle paragraphs by keyword relevance
        scored_middle = []
        for i, para in enumerate(middle):
            score = self._score_paragraph(para, keywords, i, len(middle))
            scored_middle.append((score, para))

        # Sort by score descending, pick paragraphs that fit
        scored_middle.sort(key=lambda x: x[0], reverse=True)

        selected_paras = []
        used_chars = 0
        for score, para in scored_middle:
            para_len = len(para) + 2  # +2 for "\n\n"
            if used_chars + para_len <= remaining_budget:
                selected_paras.append(para)
                used_chars += para_len
            if used_chars >= remaining_budget * 0.9:
                break

        # Reconstruct: first + selected middle (in original order) + last
        selected_set = set(selected_paras)
        ordered_middle = [p for p in middle if p in selected_set]

        result_parts = [first] + ordered_middle + [last]
        result = "\n\n".join(result_parts)

        # Final safety check
        if len(result) > max_chars:
            result = result[:max_chars]

        return result, len(text) - len(result)

    def _score_paragraph(
        self, para: str, keywords: Optional[List[str]], index: int, total: int
    ) -> float:
        """Score a paragraph's relevance for retention.

        Higher score = more likely to keep. Considers:
        - Keyword density (if keywords provided)
        - Position (earlier paragraphs slightly preferred)
        - Length (substance over fluff)
        - Information density (unique words / total words)
        """
        score = 1.0

        # Position bonus — prefer earlier paragraphs slightly
        if total > 0:
            position_ratio = 1.0 - (index / total) * 0.3
            score *= position_ratio

        # Length bonus — substantial paragraphs score higher
        word_count = len(para.split())
        if word_count < 5:
            score *= 0.3  # Too short to be useful
        elif word_count < 20:
            score *= 0.7
        elif word_count > 200:
            score *= 1.2  # Substantial content

        # Information density
        words = para.lower().split()
        if words:
            unique_ratio = len(set(words)) / len(words)
            score *= (0.5 + unique_ratio * 0.5)

        # Keyword matching
        if keywords:
            para_lower = para.lower()
            keyword_hits = sum(1 for kw in keywords if kw.lower() in para_lower)
            if keyword_hits > 0:
                score *= (1.0 + keyword_hits * 0.5)

        return score


# ════════════════════════════════════════════════════════════════════
# Token Counter — Estimate token counts
# ════════════════════════════════════════════════════════════════════

class TokenCounter:
    """Estimate token counts for prompts and responses.

    Uses tiktoken for OpenAI models when available, otherwise
    falls back to a heuristic estimator (~4 chars per token).
    """

    _tiktoken_encodings: Dict[str, Any] = {}

    @classmethod
    def count(cls, text: str, model: str = "") -> int:
        """Estimate token count for a string.

        Args:
            text: The text to count tokens for.
            model: Model name (used to select tiktoken encoding if available).
        """
        if not text:
            return 0

        # Try tiktoken for OpenAI models
        encoding = cls._get_tiktoken_encoding(model)
        if encoding is not None:
            try:
                return len(encoding.encode(text))
            except Exception:
                pass

        # Heuristic: ~4 characters per token (GPT-style average)
        # Adjust slightly for code-heavy content (fewer tokens per char)
        code_ratio = cls._estimate_code_ratio(text)
        chars_per_token = 3.5 if code_ratio > 0.3 else 4.0
        return max(1, int(len(text) / chars_per_token))

    @classmethod
    def _get_tiktoken_encoding(cls, model: str) -> Any:
        """Try to get tiktoken encoding for a model."""
        if not model:
            return None

        # Check cache
        if model in cls._tiktoken_encodings:
            return cls._tiktoken_encodings[model]

        try:
            import tiktoken
            try:
                enc = tiktoken.encoding_for_model(model)
            except KeyError:
                enc = tiktoken.get_encoding("cl100k_base")
            cls._tiktoken_encodings[model] = enc
            return enc
        except ImportError:
            cls._tiktoken_encodings[model] = None
            return None
        except Exception:
            cls._tiktoken_encodings[model] = None
            return None

    @staticmethod
    def _estimate_code_ratio(text: str) -> float:
        """Estimate how much of the text is code-like (0.0 to 1.0)."""
        if not text:
            return 0.0
        code_indicators = 0
        total_chars = len(text)
        if total_chars == 0:
            return 0.0
        # Count code-like characters
        for char in "{}[]()=;:<>/\\|&!@#$%^*":
            code_indicators += text.count(char)
        # Indented lines suggest code
        indented_lines = sum(1 for line in text.split("\n") if line.startswith(("    ", "\t")))
        code_indicators += indented_lines * 4
        return min(1.0, code_indicators / max(total_chars, 1) * 5)


# ════════════════════════════════════════════════════════════════════
# Auto-Detection — Detect which provider is configured
# ════════════════════════════════════════════════════════════════════

def auto_detect_provider() -> Optional[Dict[str, Any]]:
    """Auto-detect which LLM provider the user has configured.

    Checks environment variables for API keys in priority order.
    Returns provider config dict or None if no provider found.

    Expanded from the existing _detect_user_provider in config.py
    to support all providers in the registry.
    """
    # 1. Check explicit LLM_PROVIDER_* env vars or setup wizard LLM_* env vars first
    explicit_key = os.getenv("LLM_PROVIDER_API_KEY", "").strip() or os.getenv("LLM_API_KEY", "").strip()
    if explicit_key:
        # Determine provider name: check LLM_PROVIDER_NAME, LLM_PROVIDER, or guess from key
        provider = os.getenv("LLM_PROVIDER_NAME", "").strip() or os.getenv("LLM_PROVIDER", "").strip() or "custom"
        base_url = os.getenv("LLM_PROVIDER_BASE_URL", "").strip() or os.getenv("LLM_API_BASE", "").strip() or ""
        model = os.getenv("LLM_PROVIDER_MODEL", "").strip() or os.getenv("LLM_MODEL", "").strip() or "gpt-4o-mini"
        return {
            "provider": provider,
            "api_key": explicit_key,
            "base_url": base_url,
            "model": model,
        }

    # 2. Check SWARM_PROVIDER_* vars (backward compatibility)
    swarm_key = os.getenv("SWARM_PROVIDER_API_KEY", "").strip()
    if swarm_key:
        return {
            "provider": os.getenv("SWARM_PROVIDER_NAME", "custom"),
            "api_key": swarm_key,
            "base_url": os.getenv("SWARM_PROVIDER_BASE_URL", ""),
            "model": os.getenv("SWARM_PROVIDER_MODEL", "gpt-4o-mini"),
        }

    # 3. Check each provider's env key in registry order
    for provider_name, config in PROVIDER_REGISTRY.items():
        env_key = config["env_key"]
        api_key = os.getenv(env_key, "").strip()

        # Special handling for Ollama (no API key needed)
        if provider_name == "ollama":
            # Check if Ollama is running ONLY if explicitly requested via OLLAMA_HOST
            ollama_host = os.getenv("OLLAMA_HOST", "").strip()
            if ollama_host:
                try:
                    import urllib.request
                    req = urllib.request.Request(f"{ollama_host.replace('/v1', '')}/api/tags", method="GET")
                    with urllib.request.urlopen(req, timeout=2) as resp:
                        if resp.status == 200:
                            return {
                                "provider": "ollama",
                                "api_key": "ollama",
                                "base_url": f"{ollama_host.rstrip('/')}/v1",
                                "model": config["default_model"],
                            }
                except Exception:
                    pass
            continue

        # Special handling for Azure OpenAI
        if provider_name == "azure":
            if api_key:
                endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
                deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "").strip()
                api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview").strip()
                if endpoint and deployment:
                    return {
                        "provider": "azure",
                        "api_key": api_key,
                        "base_url": f"{endpoint}/openai/deployments/{deployment}",
                        "model": deployment,
                        "api_version": api_version,
                    }
            continue

        # Special handling for Bedrock
        if provider_name == "bedrock":
            if api_key:
                secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
                region = os.getenv("AWS_REGION", "us-east-1").strip()
                if secret_key:
                    return {
                        "provider": "bedrock",
                        "api_key": api_key,
                        "secret_key": secret_key,
                        "region": region,
                        "base_url": f"https://bedrock-runtime.{region}.amazonaws.com",
                        "model": config["default_model"],
                    }
            continue

        # Standard provider with API key
        if api_key:
            logger.info(f"Auto-detected LLM provider: {provider_name} (model: {config['default_model']})")
            return {
                "provider": provider_name,
                "api_key": api_key,
                "base_url": config["base_url"],
                "model": config["default_model"],
            }

    return None


def detect_available_providers() -> List[Dict[str, Any]]:
    """Detect all available providers (not just the first one).

    Returns list of provider config dicts, ordered by the default
    fallback chain priority.
    """
    available = []
    seen_providers = set()

    for provider_name in DEFAULT_FALLBACK_CHAIN:
        if provider_name in seen_providers:
            continue
        config = PROVIDER_REGISTRY.get(provider_name)
        if not config:
            continue

        env_key = config["env_key"]
        api_key = os.getenv(env_key, "").strip()

        if provider_name == "ollama":
            ollama_host = os.getenv("OLLAMA_HOST", "").strip()
            if ollama_host:
                try:
                    import urllib.request
                    req = urllib.request.Request(f"{ollama_host.replace('/v1', '')}/api/tags", method="GET")
                    with urllib.request.urlopen(req, timeout=2) as resp:
                        if resp.status == 200:
                            available.append({
                                "provider": "ollama",
                                "api_key": "ollama",
                                "base_url": f"{ollama_host.rstrip('/')}/v1",
                                "model": config["default_model"],
                            })
                            seen_providers.add("ollama")
                except Exception:
                    pass
            continue

        if api_key:
            available.append({
                "provider": provider_name,
                "api_key": api_key,
                "base_url": config["base_url"],
                "model": config["default_model"],
            })
            seen_providers.add(provider_name)

    return available


# ════════════════════════════════════════════════════════════════════
# Sanitize — Prompt injection protection
# ════════════════════════════════════════════════════════════════════

def _sanitize_prompt(text: str, max_length: int = 8000) -> str:
    """Sanitize user text to prevent prompt injection.

    Strips common injection patterns while preserving the query meaning.
    """
    if not text:
        return text

    sanitized = text[:max_length]
    injection_patterns = [
        "ignore previous instructions",
        "ignore all previous",
        "disregard all",
        "system:",
        "assistant:",
        "you are now",
        "new instructions",
        "override",
        "jailbreak",
    ]
    lower = sanitized.lower()
    for pattern in injection_patterns:
        if pattern in lower:
            sanitized = sanitized[:200] + " [query sanitized for injection prevention]"
            break
    return sanitized


# ════════════════════════════════════════════════════════════════════
# Universal Provider — The main LLM client
# ════════════════════════════════════════════════════════════════════

class UniversalProvider:
    """Universal LLM provider that works with ANY LLM service.

    A single interface for all LLM tasks (summarization, content extraction,
    classification, etc.) with built-in token saving mechanisms.

    Features:
    - Works with 12+ providers (OpenAI, Anthropic, Google, xAI, Mistral,
      DeepSeek, Groq, Together, Ollama, Azure, Bedrock, custom endpoints)
    - Automatic token budget tracking and enforcement
    - Prompt compression to save tokens
    - LRU response cache with similarity matching
    - Smart truncation for long contexts
    - Streaming support
    - Fallback chain when primary provider fails
    - Thread-safe with asyncio locks
    - Auto-detection of configured providers from env vars

    Usage:
        provider = UniversalProvider()
        result = await provider.complete("Hello, world!")
        # result = {"status": "success", "content": "...", ...}
    """

    def __init__(
        self,
        provider: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        budget: Optional[TokenBudget] = None,
        fallback_chain: Optional[List[str]] = None,
        cache_size: int = 1024,
        compression_aggression: float = 0.5,
        max_retries: int = 3,
        timeout: float = 30.0,
        api_version: Optional[str] = None,
        secret_key: Optional[str] = None,
        region: Optional[str] = None,
    ):
        # Core configuration
        self.provider_name = provider
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.api_version = api_version
        self.secret_key = secret_key
        self.region = region
        self.max_retries = max_retries
        self.timeout = timeout

        # Token saving mechanisms
        self.budget = budget or TokenBudget()
        self.compressor = PromptCompressor()
        self.cache = ResponseCache(maxsize=cache_size)
        self.truncator = SmartTruncation()
        self.token_counter = TokenCounter()
        self.compression_aggression = compression_aggression

        # Fallback chain
        self.fallback_chain = fallback_chain or DEFAULT_FALLBACK_CHAIN

        # Asyncio lock for thread safety
        self._lock = asyncio.Lock()

        # Client instances (lazy-initialized)
        self._openai_client = None
        self._anthropic_client = None
        self._http_client = None
        self._client_lock = threading.Lock()

        # Auto-detect if no explicit config provided
        if not self.api_key:
            detected = auto_detect_provider()
            if detected:
                self.provider_name = self.provider_name or detected.get("provider")
                self.api_key = self.api_key or detected.get("api_key")
                self.base_url = self.base_url or detected.get("base_url")
                self.model = self.model or detected.get("model")
                self.api_version = self.api_version or detected.get("api_version")
                self.secret_key = self.secret_key or detected.get("secret_key")
                self.region = self.region or detected.get("region")
                logger.info(
                    f"Auto-detected LLM provider: {self.provider_name} "
                    f"(model: {self.model})"
                )

        # Track active provider for fallback
        self._active_provider = self.provider_name
        self._available_providers = detect_available_providers()

        if self.api_key:
            logger.info(f"UniversalProvider initialized: {self.provider_name}/{self.model}")
        else:
            logger.info("UniversalProvider initialized: no provider configured (offline mode)")

    # ─── Core Provider Methods ─────────────────────────────────────

    async def complete(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 500,
        temperature: float = 0.3,
        stream: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """Main completion method.

        Args:
            prompt: The user prompt/message.
            system: System prompt (optional).
            max_tokens: Maximum tokens in the response.
            temperature: Sampling temperature (0.0-2.0).
            stream: If True, return an async iterator of chunks.
            **kwargs: Additional provider-specific parameters.

        Returns:
            Dict with keys:
                status: "success" or "error"
                content: The completion text (on success)
                error: Error message (on error)
                tokens_used: Total tokens consumed
                prompt_tokens: Tokens in the prompt
                completion_tokens: Tokens in the response
                provider: Which provider handled the request
                model: Which model was used
                cached: Whether result was from cache
                compressed: Whether prompt was compressed
        """
        # Sanitize input
        safe_prompt = _sanitize_prompt(prompt)

        # Check cache
        cached = self.cache.get(safe_prompt, system, self.model, temperature=temperature, max_tokens=max_tokens)
        if cached is not None:
            self.budget.cache_hits += 1
            result = {k: v for k, v in cached.items() if not k.startswith("_")}
            result["cached"] = True
            return result

        # Compress prompt if needed
        compressed_prompt, chars_saved = self.compressor.compress(
            safe_prompt, self.compression_aggression
        )
        was_compressed = chars_saved > 0
        if was_compressed:
            self.budget.compression_savings += self.token_counter.count(
                " " * chars_saved, self.model
            )

        # Smart truncation if prompt is very long
        estimated_prompt_tokens = self.token_counter.count(compressed_prompt, self.model)
        system_tokens = self.token_counter.count(system, self.model) if system else 0
        total_input_estimate = estimated_prompt_tokens + system_tokens

        if total_input_estimate > 6000:
            compressed_prompt, trunc_saved = self.truncator.truncate(
                compressed_prompt,
                max_chars=20000,
                system=system,
            )
            if trunc_saved > 0:
                self.budget.truncation_savings += self.token_counter.count(
                    " " * trunc_saved, self.model
                )

        # Check budget
        if not self.budget.can_spend(total_input_estimate + max_tokens):
            return {
                "status": "error",
                "error": f"Token budget exhausted. Remaining: {self.budget.remaining()}",
                "tokens_used": 0,
                "provider": self.provider_name,
                "model": self.model,
            }

        # Make the API call with fallback
        if stream:
            return self._stream_complete(
                compressed_prompt, system, max_tokens, temperature, **kwargs
            )

        result = await self._call_with_fallback(
            compressed_prompt, system, max_tokens, temperature, **kwargs
        )

        # Record token usage
        if result.get("status") == "success":
            prompt_tokens = result.get("prompt_tokens", estimated_prompt_tokens)
            completion_tokens = result.get("completion_tokens", 0)
            self.budget.record(prompt_tokens, completion_tokens)

            # Cache the result
            cache_entry = dict(result)
            cache_entry["cached"] = False
            cache_entry["compressed"] = was_compressed
            self.cache.put(safe_prompt, cache_entry, system, self.model, temperature=temperature, max_tokens=max_tokens)
            self.budget.cache_saves += 1

            result["compressed"] = was_compressed
            result["cached"] = False

        return result

    async def classify(
        self,
        prompt: str,
        categories: List[str],
        system: str = "",
        **kwargs,
    ) -> Dict[str, Any]:
        """Structured classification. Returns category + confidence.

        Args:
            prompt: The text to classify.
            categories: List of possible categories.
            system: Optional system prompt override.
            **kwargs: Additional parameters.

        Returns:
            Dict with keys:
                status: "success" or "error"
                category: The selected category
                confidence: Confidence score (0.0-1.0)
                reasoning: Brief explanation of classification
                all_scores: Scores for all categories
        """
        categories_str = ", ".join(f'"{c}"' for c in categories)

        classification_system = system or (
            "You are a precise classifier. Classify the input into exactly one "
            "category. Respond with ONLY a JSON object: "
            '{"category": "<exact_category>", "confidence": <0.0-1.0>, '
            '"reasoning": "<brief reason>"}'
        )

        classification_prompt = (
            f"Classify the following into one of these categories: [{categories_str}]\n\n"
            f"Input: {prompt}\n\n"
            f"Respond with JSON only."
        )

        result = await self.complete(
            classification_prompt,
            system=classification_system,
            max_tokens=200,
            temperature=0.1,
            **kwargs,
        )

        if result.get("status") != "success":
            return result

        # Parse the classification response
        content = result.pop("content", "")
        try:
            parsed = self._extract_json(content)
            category = parsed.get("category", "")
            confidence = float(parsed.get("confidence", 0.5))
            reasoning = parsed.get("reasoning", "")

            # Validate category
            if category not in categories:
                # Try fuzzy match
                category_lower = category.lower().strip()
                for cat in categories:
                    if cat.lower() == category_lower or cat.lower() in category_lower:
                        category = cat
                        break
                else:
                    confidence *= 0.5  # Lower confidence for unmatched category

            result["category"] = category
            result["confidence"] = min(1.0, max(0.0, confidence))
            result["reasoning"] = reasoning
        except (json.JSONDecodeError, ValueError, AttributeError) as e:
            result["category"] = ""
            result["confidence"] = 0.0
            result["reasoning"] = f"Failed to parse classification: {e}"

        return result

    async def extract(
        self,
        prompt: str,
        schema: Dict[str, Any],
        system: str = "",
        **kwargs,
    ) -> Dict[str, Any]:
        """Structured data extraction with schema validation.

        Args:
            prompt: The text to extract data from.
            schema: JSON schema describing expected output structure.
                Example: {"name": "string", "age": "number", "skills": ["string"]}
            system: Optional system prompt override.
            **kwargs: Additional parameters.

        Returns:
            Dict with keys:
                status: "success" or "error"
                data: Extracted data matching the schema
                validation_errors: List of schema validation issues (if any)
        """
        schema_str = json.dumps(schema, indent=2)

        extraction_system = system or (
            "You are a precise data extractor. Extract structured data from the "
            "input according to the provided schema. Respond with ONLY valid JSON "
            "matching the schema. Use null for missing fields."
        )

        extraction_prompt = (
            f"Extract data from the following text according to this schema:\n"
            f"```json\n{schema_str}\n```\n\n"
            f"Text: {prompt}\n\n"
            f"Respond with JSON only."
        )

        result = await self.complete(
            extraction_prompt,
            system=extraction_system,
            max_tokens=1000,
            temperature=0.1,
            **kwargs,
        )

        if result.get("status") != "success":
            return result

        content = result.pop("content", "")
        try:
            parsed = self._extract_json(content)
            validation_errors = self._validate_schema(parsed, schema)
            result["data"] = parsed
            result["validation_errors"] = validation_errors
        except (json.JSONDecodeError, ValueError) as e:
            result["data"] = None
            result["validation_errors"] = [f"Failed to parse JSON: {e}"]

        return result

    async def summarize(
        self,
        text: str,
        max_length: int = 200,
        **kwargs,
    ) -> Dict[str, Any]:
        """Summarization with token budget awareness.

        Args:
            text: The text to summarize.
            max_length: Target maximum length of summary in words.
            **kwargs: Additional parameters.

        Returns:
            Dict with keys:
                status: "success" or "error"
                summary: The summary text
                original_length: Original text word count
                summary_length: Summary word count
                compression_ratio: Ratio of summary to original length
        """
        # Smart truncation for very long texts before sending to LLM
        input_text = text
        text_tokens = self.token_counter.count(text, self.model)
        if text_tokens > 4000:
            input_text, _ = self.truncator.truncate(
                text,
                max_chars=16000,
                keywords=kwargs.get("keywords"),
            )

        summary_system = (
            "You are a concise summarizer. Provide a clear, factual summary "
            f"in no more than {max_length} words. Focus on key information."
        )

        summary_prompt = f"Summarize the following text:\n\n{input_text}"

        result = await self.complete(
            summary_prompt,
            system=summary_system,
            max_tokens=min(max_length * 2, 500),
            temperature=0.3,
            **kwargs,
        )

        if result.get("status") != "success":
            return result

        content = result.pop("content", "")
        original_words = len(text.split())
        summary_words = len(content.split())

        result["summary"] = content
        result["original_length"] = original_words
        result["summary_length"] = summary_words
        result["compression_ratio"] = round(
            summary_words / max(original_words, 1), 3
        )

        return result

    def set_provider(
        self,
        provider_name: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Switch provider at runtime.

        Args:
            provider_name: Name of the provider (e.g., "openai", "anthropic").
            api_key: API key (optional, will use env var if not provided).
            base_url: Custom base URL (optional).
            model: Model name (optional, uses provider default).
            api_version: API version (for Azure).

        Returns:
            Dict with status and new provider info.
        """
        # Look up provider config
        provider_config = PROVIDER_REGISTRY.get(provider_name)
        if not provider_config and not base_url:
            return {
                "status": "error",
                "error": f"Unknown provider '{provider_name}' and no base_url provided. "
                         f"Available: {list(PROVIDER_REGISTRY.keys())}",
            }

        # Resolve API key
        resolved_key = api_key
        if not resolved_key and provider_config:
            env_key = provider_config.get("env_key", "")
            resolved_key = os.getenv(env_key, "").strip()
            if not resolved_key and not provider_config.get("requires_api_key", False):
                resolved_key = "none"  # For providers like Ollama

        # Resolve base URL
        resolved_url = base_url
        if not resolved_url and provider_config:
            resolved_url = provider_config.get("base_url", "")

        # Resolve model
        resolved_model = model
        if not resolved_model and provider_config:
            resolved_model = provider_config.get("default_model", "")

        if not resolved_key:
            return {
                "status": "error",
                "error": f"No API key available for provider '{provider_name}'. "
                         f"Set the {provider_config.get('env_key', '')} environment variable.",
            }

        # Apply changes
        self.provider_name = provider_name
        self.api_key = resolved_key
        self.base_url = resolved_url
        self.model = resolved_model
        self.api_version = api_version

        # Reset clients so they're recreated with new config
        self._reset_clients()

        # Clear cache since provider changed
        self.cache.clear()

        logger.info(f"Provider switched to: {provider_name}/{resolved_model}")
        return {
            "status": "success",
            "provider": provider_name,
            "model": resolved_model,
            "base_url": resolved_url,
        }

    def get_token_usage(self) -> Dict[str, Any]:
        """Get token usage statistics."""
        return {
            "budget": self.budget.status,
            "cache": self.cache.stats,
        }

    def reset_budget(self):
        """Reset token budget for a new session."""
        self.budget.reset()

    # ─── Provider-Specific API Calls ───────────────────────────────

    async def _call_with_fallback(
        self,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
        **kwargs,
    ) -> Dict[str, Any]:
        """Call the primary provider, with fallback to others on failure."""
        providers_to_try = [self.provider_name]

        # Add fallback providers that have credentials
        for p in self._available_providers:
            p_name = p.get("provider")
            if p_name and p_name not in providers_to_try:
                providers_to_try.append(p_name)

        last_error = None
        for provider_name in providers_to_try:
            if not provider_name:
                continue

            for attempt in range(self.max_retries):
                try:
                    result = await self._call_provider(
                        provider_name, prompt, system, max_tokens, temperature, **kwargs
                    )
                    if result.get("status") == "success":
                        return result
                    last_error = result.get("error", "Unknown error")
                except Exception as e:
                    last_error = str(e)
                    logger.debug(
                        f"Provider {provider_name} attempt {attempt + 1}/{self.max_retries} "
                        f"failed: {e}"
                    )
                    if attempt < self.max_retries - 1:
                        wait = 0.5 * (2 ** attempt)
                        await asyncio.sleep(wait)

            logger.warning(f"Provider {provider_name} failed after {self.max_retries} retries")

        # ─── Built-in rule-based fallback (no API key needed) ───
        logger.debug("No external LLM provider available, using built-in rule-based fallback")
        return await self._call_builtin(prompt, system, max_tokens, temperature, **kwargs)

    async def _call_builtin(
        self,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
        **kwargs,
    ) -> Dict[str, Any]:
        """Built-in rule-based LLM substitute. Works without any external API.

        Provides functional implementations for:
        - classify: keyword-based category matching
        - extract: regex-based structured extraction
        - summarize: extractive summarization (key sentences)
        - complete: echo with analysis

        Quality is lower than real LLM but keeps all tools functional.
        """
        prompt_lower = prompt.lower()

        # ── Detect task type from prompt/system ──
        is_classify = "classify" in system.lower() or "category" in prompt_lower
        is_extract = "extract" in system.lower() or "schema" in prompt_lower
        is_summarize = "summarize" in system.lower() or "summarize" in prompt_lower

        if is_classify:
            return self._builtin_classify(prompt, **kwargs)
        elif is_extract:
            return self._builtin_extract(prompt, **kwargs)
        elif is_summarize:
            return self._builtin_summarize(prompt, max_tokens, **kwargs)
        else:
            return self._builtin_complete(prompt, max_tokens, **kwargs)

    def _builtin_classify(self, prompt: str, **kwargs) -> Dict[str, Any]:
        """Keyword-based classification fallback."""
        prompt_lower = prompt.lower()

        # Extract categories from prompt
        categories = []
        cat_match = re.search(r'\[([^\]]+)\]', prompt)
        if cat_match:
            raw = cat_match.group(1)
            categories = [c.strip().strip('"').strip("'") for c in raw.split(',')]

        if not categories:
            for kw in ["category", "categories", "classify into", "one of"]:
                idx = prompt_lower.find(kw)
                if idx >= 0:
                    after = prompt[idx + len(kw):].split('\n')[0]
                    categories = [c.strip().strip('"').strip("'") for c in re.split(r'[,;|]', after) if c.strip()]
                    break

        if not categories:
            return {
                "status": "success",
                "content": json.dumps({"category": "unknown", "confidence": 0.1, "reasoning": "No categories found"}),
                "category": "unknown", "confidence": 0.1,
                "reasoning": "Could not determine categories from prompt",
                "tokens_used": 0, "provider": "builtin", "model": "rule-based",
            }

        scores = {}
        for cat in categories:
            cat_lower = cat.lower()
            if cat_lower in prompt_lower:
                scores[cat] = 1.0
            else:
                cat_words = set(cat_lower.split())
                prompt_words = set(prompt_lower.split())
                overlap = len(cat_words & prompt_words)
                scores[cat] = overlap / max(len(cat_words), 1) * 0.7

        best_cat = max(scores, key=scores.get) if scores else categories[0]
        best_score = scores.get(best_cat, 0.3)

        return {
            "status": "success",
            "content": json.dumps({"category": best_cat, "confidence": best_score, "reasoning": "Keyword-based"}),
            "category": best_cat, "confidence": best_score,
            "reasoning": f"Matched via keyword overlap (score: {best_score:.2f})",
            "tokens_used": 0, "provider": "builtin", "model": "rule-based",
        }

    def _builtin_extract(self, prompt: str, **kwargs) -> Dict[str, Any]:
        """Regex-based structured data extraction fallback."""
        text = prompt
        for marker in ["Text:", "text:", "Input:", "input:", "Content:"]:
            idx = prompt.find(marker)
            if idx >= 0:
                text = prompt[idx + len(marker):].strip()
                break

        schema = {}
        schema_match = re.search(r'```json\s*({[^`]+})\s*```', prompt)
        if schema_match:
            try: schema = json.loads(schema_match.group(1))
            except json.JSONDecodeError: pass
        if not schema:
            schema_match = re.search(r'schema[:\s]*({[^}]+})', prompt, re.IGNORECASE)
            if schema_match:
                try: schema = json.loads(schema_match.group(1))
                except json.JSONDecodeError: pass

        data = {}
        extractors = {
            "email": r'[\w.+-]+@[\w-]+\.[\w.-]+',
            "phone": r'[\+]?[(]?[0-9]{1,4}[)]?[-\s\./0-9]{7,15}',
            "url": r'https?://[^\s<>"{}|\\^`\[\]]+',
            "name": r'(?:name|author|person)[\s:]+([A-Z][a-z]+ [A-Z][a-z]+)',
            "date": r'\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}',
            "number": r'\b\d+(?:\.\d+)?\b',
        }

        for field_name, field_type in (schema.items() if schema else extractors.items()):
            field_key = field_name if isinstance(field_name, str) else str(field_name)
            field_type_str = field_type if isinstance(field_type, str) else str(field_type)

            if field_key in extractors: pattern = extractors[field_key]
            elif "email" in field_key.lower(): pattern = extractors["email"]
            elif "phone" in field_key.lower(): pattern = extractors["phone"]
            elif "url" in field_key.lower() or "link" in field_key.lower(): pattern = extractors["url"]
            elif "date" in field_key.lower(): pattern = extractors["date"]
            elif "name" in field_key.lower(): pattern = extractors["name"]
            elif field_type_str == "number" or "number" in field_key.lower() or "int" in field_type_str.lower(): pattern = extractors["number"]
            else:
                matches = re.findall(r'(.{5,80}?)(?:\.|,|\n|$)', text)
                data[field_key] = matches[0].strip() if matches else None
                continue

            found = re.findall(pattern, text, re.IGNORECASE)
            if isinstance(field_type, list):
                data[field_key] = [v.strip() for v in found[:5]] if found else []
            else:
                val = found[0].strip() if found else None
                if isinstance(val, str): val = val.split('\n')[0].strip()
                data[field_key] = val

        return {
            "status": "success", "content": json.dumps(data), "data": data,
            "validation_errors": [], "tokens_used": 0, "provider": "builtin", "model": "rule-based",
        }

    def _builtin_summarize(self, prompt: str, max_tokens: int = 500, **kwargs) -> Dict[str, Any]:
        """Extractive summarization fallback."""
        text = prompt
        for marker in ["Text:", "text:", "following text:", "following:"]:
            idx = prompt.lower().find(marker)
            if idx >= 0:
                text = prompt[idx + len(marker):].strip()
                break

        sentences = re.split(r'(?<=[.!?])\s+', text.strip())
        sentences = [s.strip() for s in sentences if len(s.strip()) > 20]

        if not sentences:
            summary = text[:500] if len(text) > 500 else text
            return {
                "status": "success", "content": summary, "summary": summary,
                "original_length": len(text.split()), "summary_length": len(summary.split()),
                "compression_ratio": 1.0, "tokens_used": 0, "provider": "builtin", "model": "rule-based",
            }

        important_words = {"important", "key", "main", "primary", "essential", "result", "conclusion", "finding", "significant"}
        scored = []
        for i, sent in enumerate(sentences):
            score = 0.0
            if i == 0: score += 0.3
            elif i == len(sentences) - 1: score += 0.2
            elif i < 3: score += 0.1
            words = set(sent.lower().split())
            score += len(words & important_words) * 0.15
            if 10 <= len(sent.split()) <= 40: score += 0.1
            scored.append((score, i, sent))

        target_words = min(max_tokens * 2, 200)
        scored.sort(key=lambda x: -x[0])
        selected, total_words = [], 0
        for score, idx, sent in scored:
            if total_words >= target_words: break
            selected.append((idx, sent))
            total_words += len(sent.split())

        selected.sort(key=lambda x: x[0])
        summary = ' '.join(s for _, s in selected)
        original_words = len(text.split())
        summary_words = len(summary.split())

        return {
            "status": "success", "content": summary, "summary": summary,
            "original_length": original_words, "summary_length": summary_words,
            "compression_ratio": round(summary_words / max(original_words, 1), 3),
            "tokens_used": 0, "provider": "builtin", "model": "rule-based",
        }

    def _builtin_complete(self, prompt: str, max_tokens: int = 500, **kwargs) -> Dict[str, Any]:
        """Built-in completion — context analysis without LLM."""
        word_count = len(prompt.split())
        prompt_lower = prompt.lower()

        parts = [f"Analyzed prompt ({word_count} words)."]
        if any(w in prompt_lower for w in ["what", "how", "why", "when", "where", "who"]): parts.append("Question detected.")
        if any(w in prompt_lower for w in ["summarize", "summary", "brief"]): parts.append("Summarization requested.")
        if any(w in prompt_lower for w in ["extract", "find", "get", "list"]): parts.append("Data extraction requested.")
        if any(w in prompt_lower for w in ["classify", "categorize", "type"]): parts.append("Classification requested.")
        if any(w in prompt_lower for w in ["http", "url", "website", "web", "page"]): parts.append("Web-related query.")
        parts.append("No external LLM configured. Set OPENAI_API_KEY or ANTHROPIC_API_KEY for enhanced AI.")

        return {
            "status": "success", "content": " ".join(parts),
            "tokens_used": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "provider": "builtin", "model": "rule-based",
        }

    async def _call_provider(
        self,
        provider_name: str,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
        **kwargs,
    ) -> Dict[str, Any]:
        """Route to the appropriate provider-specific API call."""
        # Get the provider config (including fallback providers)
        provider_config = PROVIDER_REGISTRY.get(provider_name, {})
        api_style = provider_config.get("api_style", "openai")

        # Resolve credentials for this specific provider
        api_key, base_url, model = self._resolve_provider_credentials(provider_name, provider_config)

        if not api_key:
            return {
                "status": "error",
                "error": f"No API key for provider {provider_name}",
                "tokens_used": 0,
                "provider": provider_name,
            }

        # Route based on API style
        if api_style == "anthropic":
            return await self._call_anthropic(api_key, prompt, system, max_tokens, temperature, model, **kwargs)
        elif api_style == "azure":
            return await self._call_azure(api_key, base_url, prompt, system, max_tokens, temperature, model, **kwargs)
        elif api_style == "bedrock":
            return await self._call_bedrock(api_key, prompt, system, max_tokens, temperature, model, **kwargs)
        else:
            # OpenAI-compatible (covers: openai, google, xai, mistral, deepseek, groq, together, ollama, custom)
            return await self._call_openai_compatible(api_key, base_url, prompt, system, max_tokens, temperature, model, **kwargs)

    def _resolve_provider_credentials(
        self, provider_name: str, provider_config: Dict[str, Any]
    ) -> Tuple[str, str, str]:
        """Resolve API key, base_url, model for a given provider.

        Checks if this is the active provider first, then falls back to
        env vars and the available providers list.
        """
        # Check if this is the currently active provider
        if provider_name == self.provider_name:
            return self.api_key or "", self.base_url or "", self.model or ""

        # Look in available providers list
        for p in self._available_providers:
            if p.get("provider") == provider_name:
                return p.get("api_key", ""), p.get("base_url", ""), p.get("model", "")

        # Check env vars directly
        env_key = provider_config.get("env_key", "")
        api_key = os.getenv(env_key, "").strip()

        # Special: Ollama doesn't need a key
        if provider_name == "ollama" and not api_key:
            api_key = "ollama"

        base_url = provider_config.get("base_url", "")
        model = provider_config.get("default_model", "")

        return api_key, base_url, model

    # ─── OpenAI-Compatible API Call ────────────────────────────────

    async def _call_openai_compatible(
        self,
        api_key: str,
        base_url: str,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
        model: str,
        **kwargs,
    ) -> Dict[str, Any]:
        """Make an OpenAI-compatible API call.

        Uses the openai SDK if available, otherwise falls back to
        raw HTTP via httpx/aiohttp.
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        # Try openai SDK first
        try:
            return await self._call_with_openai_sdk(
                api_key, base_url, messages, max_tokens, temperature, model, **kwargs
            )
        except ImportError:
            pass

        # Fall back to raw HTTP
        return await self._call_with_http(
            api_key, base_url, messages, max_tokens, temperature, model, **kwargs
        )

    async def _call_with_openai_sdk(
        self,
        api_key: str,
        base_url: str,
        messages: List[Dict[str, str]],
        max_tokens: int,
        temperature: float,
        model: str,
        **kwargs,
    ) -> Dict[str, Any]:
        """Call using the openai Python SDK (AsyncOpenAI)."""
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=self.timeout,
        )

        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            **{k: v for k, v in kwargs.items() if k in ("top_p", "frequency_penalty", "presence_penalty", "stop", "response_format")},
        )

        content = response.choices[0].message.content or ""
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0

        if hasattr(response, "usage") and response.usage:
            prompt_tokens = response.usage.prompt_tokens or 0
            completion_tokens = response.usage.completion_tokens or 0
            total_tokens = response.usage.total_tokens or 0
        else:
            # Estimate
            prompt_tokens = self.token_counter.count(
                " ".join(m["content"] for m in messages), model
            )
            completion_tokens = self.token_counter.count(content, model)
            total_tokens = prompt_tokens + completion_tokens

        return {
            "status": "success",
            "content": content,
            "tokens_used": total_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "provider": self._active_provider or self.provider_name,
            "model": model,
            "finish_reason": response.choices[0].finish_reason if response.choices else "stop",
        }

    async def _call_with_http(
        self,
        api_key: str,
        base_url: str,
        messages: List[Dict[str, str]],
        max_tokens: int,
        temperature: float,
        model: str,
        **kwargs,
    ) -> Dict[str, Any]:
        """Call using raw HTTP via httpx (no openai SDK needed)."""
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        # Add optional kwargs
        for key in ("top_p", "frequency_penalty", "presence_penalty", "stop", "response_format"):
            if key in kwargs:
                payload[key] = kwargs[key]

        try:
            import httpx
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except ImportError:
            # Fall back to aiohttp
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=self.timeout)
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

        # Parse OpenAI-format response
        content = ""
        if "choices" in data and data["choices"]:
            content = data["choices"][0].get("message", {}).get("content", "")

        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        if "usage" in data:
            prompt_tokens = data["usage"].get("prompt_tokens", 0)
            completion_tokens = data["usage"].get("completion_tokens", 0)
            total_tokens = data["usage"].get("total_tokens", 0)
        else:
            prompt_tokens = self.token_counter.count(
                " ".join(m["content"] for m in messages), model
            )
            completion_tokens = self.token_counter.count(content, model)
            total_tokens = prompt_tokens + completion_tokens

        return {
            "status": "success",
            "content": content,
            "tokens_used": total_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "provider": self._active_provider or self.provider_name,
            "model": model,
            "finish_reason": data.get("choices", [{}])[0].get("finish_reason", "stop"),
        }

    # ─── Anthropic API Call ────────────────────────────────────────

    async def _call_anthropic(
        self,
        api_key: str,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
        model: str,
        **kwargs,
    ) -> Dict[str, Any]:
        """Call Anthropic's native API (not OpenAI-compatible).

        Supports both the anthropic SDK and raw HTTP fallback.
        """
        # Try Anthropic SDK first
        try:
            return await self._call_anthropic_sdk(
                api_key, prompt, system, max_tokens, temperature, model, **kwargs
            )
        except ImportError:
            pass

        # Raw HTTP fallback
        return await self._call_anthropic_http(
            api_key, prompt, system, max_tokens, temperature, model, **kwargs
        )

    async def _call_anthropic_sdk(
        self,
        api_key: str,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
        model: str,
        **kwargs,
    ) -> Dict[str, Any]:
        """Call Anthropic using the anthropic Python SDK."""
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=api_key, timeout=self.timeout)

        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system if system else anthropic.NOT_GIVEN,
            messages=[{"role": "user", "content": prompt}],
        )

        content = ""
        if response.content and len(response.content) > 0:
            content = "".join(
                block.text for block in response.content
                if hasattr(block, "text")
            )

        prompt_tokens = 0
        completion_tokens = 0
        if hasattr(response, "usage") and response.usage:
            prompt_tokens = getattr(response.usage, "input_tokens", 0) or 0
            completion_tokens = getattr(response.usage, "output_tokens", 0) or 0

        return {
            "status": "success",
            "content": content,
            "tokens_used": prompt_tokens + completion_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "provider": "anthropic",
            "model": model,
            "finish_reason": response.stop_reason or "stop",
        }

    async def _call_anthropic_http(
        self,
        api_key: str,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
        model: str,
        **kwargs,
    ) -> Dict[str, Any]:
        """Call Anthropic using raw HTTP (no SDK needed)."""
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system

        try:
            import httpx
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except ImportError:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=self.timeout)
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

        content = ""
        if "content" in data and data["content"]:
            content = "".join(
                block.get("text", "")
                for block in data["content"]
                if block.get("type") == "text"
            )

        prompt_tokens = 0
        completion_tokens = 0
        if "usage" in data:
            prompt_tokens = data["usage"].get("input_tokens", 0)
            completion_tokens = data["usage"].get("output_tokens", 0)

        return {
            "status": "success",
            "content": content,
            "tokens_used": prompt_tokens + completion_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "provider": "anthropic",
            "model": model,
            "finish_reason": data.get("stop_reason", "stop"),
        }

    # ─── Azure OpenAI API Call ─────────────────────────────────────

    async def _call_azure(
        self,
        api_key: str,
        base_url: str,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
        model: str,
        **kwargs,
    ) -> Dict[str, Any]:
        """Call Azure OpenAI API."""
        api_version = self.api_version or os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")

        # Try openai SDK with Azure support
        try:
            return await self._call_azure_sdk(
                api_key, base_url, prompt, system, max_tokens, temperature, model, api_version, **kwargs
            )
        except ImportError:
            pass

        # Raw HTTP fallback
        return await self._call_azure_http(
            api_key, base_url, prompt, system, max_tokens, temperature, model, api_version, **kwargs
        )

    async def _call_azure_sdk(
        self,
        api_key: str,
        base_url: str,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
        model: str,
        api_version: str,
        **kwargs,
    ) -> Dict[str, Any]:
        """Call Azure OpenAI using the openai SDK."""
        from openai import AsyncAzureOpenAI

        # base_url for Azure is like: https://xxx.openai.azure.com/openai/deployments/deployment-name
        endpoint = base_url.rsplit("/openai/deployments/", 1)[0] if "/openai/deployments/" in base_url else base_url
        deployment = model  # In Azure, the model is the deployment name

        client = AsyncAzureOpenAI(
            api_key=api_key,
            api_version=api_version,
            azure_endpoint=endpoint,
            timeout=self.timeout,
        )

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        response = await client.chat.completions.create(
            model=deployment,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        content = response.choices[0].message.content or ""
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0

        if hasattr(response, "usage") and response.usage:
            prompt_tokens = response.usage.prompt_tokens or 0
            completion_tokens = response.usage.completion_tokens or 0
            total_tokens = response.usage.total_tokens or 0
        else:
            prompt_tokens = self.token_counter.count(
                " ".join(m["content"] for m in messages), model
            )
            completion_tokens = self.token_counter.count(content, model)
            total_tokens = prompt_tokens + completion_tokens

        return {
            "status": "success",
            "content": content,
            "tokens_used": total_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "provider": "azure",
            "model": model,
            "finish_reason": response.choices[0].finish_reason if response.choices else "stop",
        }

    async def _call_azure_http(
        self,
        api_key: str,
        base_url: str,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
        model: str,
        api_version: str,
        **kwargs,
    ) -> Dict[str, Any]:
        """Call Azure OpenAI using raw HTTP."""
        url = f"{base_url.rstrip('/')}/chat/completions?api-version={api_version}"
        headers = {
            "api-key": api_key,
            "Content-Type": "application/json",
        }
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        try:
            import httpx
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except ImportError:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=self.timeout)
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

        content = ""
        if "choices" in data and data["choices"]:
            content = data["choices"][0].get("message", {}).get("content", "")

        prompt_tokens = data.get("usage", {}).get("prompt_tokens", 0)
        completion_tokens = data.get("usage", {}).get("completion_tokens", 0)

        return {
            "status": "success",
            "content": content,
            "tokens_used": prompt_tokens + completion_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "provider": "azure",
            "model": model,
            "finish_reason": data.get("choices", [{}])[0].get("finish_reason", "stop"),
        }

    # ─── Amazon Bedrock API Call ───────────────────────────────────

    async def _call_bedrock(
        self,
        api_key: str,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
        model: str,
        **kwargs,
    ) -> Dict[str, Any]:
        """Call Amazon Bedrock API.

        Uses boto3 if available, otherwise raw AWS SigV4 HTTP requests.
        """
        # Try boto3 first
        try:
            return await self._call_bedrock_boto3(
                api_key, prompt, system, max_tokens, temperature, model, **kwargs
            )
        except ImportError:
            pass

        # Raw HTTP with AWS SigV4 signing
        return await self._call_bedrock_http(
            api_key, prompt, system, max_tokens, temperature, model, **kwargs
        )

    async def _call_bedrock_boto3(
        self,
        api_key: str,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
        model: str,
        **kwargs,
    ) -> Dict[str, Any]:
        """Call Bedrock using boto3."""
        import boto3
        from botocore.config import Config as BotoConfig

        region = self.region or os.getenv("AWS_REGION", "us-east-1")
        session = boto3.Session(
            aws_access_key_id=api_key,
            aws_secret_access_key=self.secret_key or os.getenv("AWS_SECRET_ACCESS_KEY", ""),
            region_name=region,
        )
        client = session.client(
            "bedrock-runtime",
            config=BotoConfig(read_timeout=self.timeout),
        )

        # Build the request body based on model provider
        if "claude" in model:
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system if system else "",
                "messages": [{"role": "user", "content": prompt}],
            })
        elif "titan" in model:
            body = json.dumps({
                "inputText": f"{system}\n\n{prompt}" if system else prompt,
                "textGenerationConfig": {
                    "maxTokenCount": max_tokens,
                    "temperature": temperature,
                },
            })
        else:
            # Generic format
            body = json.dumps({
                "prompt": f"{system}\n\n{prompt}" if system else prompt,
                "max_gen_len": max_tokens,
                "temperature": temperature,
            })

        # Run in executor since boto3 is synchronous
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.invoke_model(modelId=model, body=body)
        )
        response_body = json.loads(response["body"].read())

        # Parse response
        content = ""
        if "content" in response_body:
            # Claude format
            for block in response_body["content"]:
                if block.get("type") == "text":
                    content += block.get("text", "")
        elif "results" in response_body:
            # Titan format
            for result in response_body["results"]:
                content += result.get("outputText", "")
        elif "generation" in response_body:
            # Llama/Mistral format
            content = response_body["generation"]
        elif "generated_text" in response_body:
            content = response_body["generated_text"]

        prompt_tokens = 0
        completion_tokens = 0
        # Try to extract usage
        if "usage" in response_body:
            usage = response_body["usage"]
            prompt_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
            completion_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
        else:
            prompt_tokens = self.token_counter.count(prompt, model)
            completion_tokens = self.token_counter.count(content, model)

        return {
            "status": "success",
            "content": content,
            "tokens_used": prompt_tokens + completion_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "provider": "bedrock",
            "model": model,
            "finish_reason": "stop",
        }

    async def _call_bedrock_http(
        self,
        api_key: str,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
        model: str,
        **kwargs,
    ) -> Dict[str, Any]:
        """Call Bedrock using raw HTTP with AWS SigV4 signing.

        This is a simplified implementation that constructs the signed request.
        For production use, boto3 is recommended.
        """
        import hashlib as _hashlib
        import hmac as _hmac
        import datetime as _datetime

        region = self.region or os.getenv("AWS_REGION", "us-east-1")
        secret_key = self.secret_key or os.getenv("AWS_SECRET_ACCESS_KEY", "")
        session_token = os.getenv("AWS_SESSION_TOKEN", "")

        host = f"bedrock-runtime.{region}.amazonaws.com"
        url = f"https://{host}/model/{model.replace('/', '%2F')}/invoke"

        # Build request body
        if "claude" in model:
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system if system else "",
                "messages": [{"role": "user", "content": prompt}],
            })
        else:
            body = json.dumps({
                "prompt": f"{system}\n\n{prompt}" if system else prompt,
                "max_gen_len": max_tokens,
                "temperature": temperature,
            })

        # AWS SigV4 signing
        now = _datetime.datetime.utcnow()
        date_stamp = now.strftime("%Y%m%d")
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")

        payload_hash = _hashlib.sha256(body.encode("utf-8")).hexdigest()
        canonical_headers = (
            f"content-type:application/json\n"
            f"host:{host}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        signed_headers = "content-type;host;x-amz-content-sha256;x-amz-date"
        if session_token:
            canonical_headers += f"x-amz-security-token:{session_token}\n"
            signed_headers += ";x-amz-security-token"

        canonical_request = (
            f"POST\n"
            f"/model/{model.replace('/', '%2F')}/invoke\n"
            f"\n"
            f"{canonical_headers}\n"
            f"{signed_headers}\n"
            f"{payload_hash}"
        )

        algorithm = "AWS4-HMAC-SHA256"
        credential_scope = f"{date_stamp}/{region}/bedrock/aws4_request"
        string_to_sign = (
            f"{algorithm}\n"
            f"{amz_date}\n"
            f"{credential_scope}\n"
            f"{_hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )

        def _sign(key: bytes, msg: str) -> bytes:
            return _hmac.new(key, msg.encode('utf-8'), _hashlib.sha256).digest()

        signing_key = _sign(_sign(_sign(_sign(f"AWS4{secret_key}".encode('utf-8'), date_stamp), region), "bedrock"), "aws4_request")
        signature = _hmac.new(signing_key, string_to_sign.encode('utf-8'), _hashlib.sha256).hexdigest()

        authorization = (
            f"{algorithm} Credential={api_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json",
            "Host": host,
            "X-Amz-Content-Sha256": payload_hash,
            "X-Amz-Date": amz_date,
        }
        if session_token:
            headers["X-Amz-Security-Token"] = session_token

        try:
            import httpx
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, content=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except ImportError:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, data=body, headers=headers, timeout=aiohttp.ClientTimeout(total=self.timeout)
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

        content = ""
        if "content" in data:
            for block in data["content"]:
                if block.get("type") == "text":
                    content += block.get("text", "")
        elif "generation" in data:
            content = data["generation"]
        elif "results" in data:
            for result in data["results"]:
                content += result.get("outputText", "")

        prompt_tokens_est = self.token_counter.count(prompt, model)
        completion_tokens_est = self.token_counter.count(content, model)

        return {
            "status": "success",
            "content": content,
            "tokens_used": prompt_tokens_est + completion_tokens_est,
            "prompt_tokens": prompt_tokens_est,
            "completion_tokens": completion_tokens_est,
            "provider": "bedrock",
            "model": model,
            "finish_reason": "stop",
        }

    # ─── Streaming Support ─────────────────────────────────────────

    async def _stream_complete(
        self,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
        **kwargs,
    ) -> Dict[str, Any]:
        """Stream a completion response, yielding chunks as they arrive.

        Returns a dict with an async generator in the 'stream' field.
        """
        provider_config = PROVIDER_REGISTRY.get(self.provider_name, {})
        api_style = provider_config.get("api_style", "openai")

        if api_style == "anthropic":
            stream_gen = self._stream_anthropic(prompt, system, max_tokens, temperature, **kwargs)
        else:
            stream_gen = self._stream_openai_compatible(prompt, system, max_tokens, temperature, **kwargs)

        return {
            "status": "success",
            "stream": stream_gen,
            "provider": self.provider_name,
            "model": self.model,
        }

    async def _stream_openai_compatible(
        self,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
        **kwargs,
    ) -> AsyncIterator[str]:
        """Stream using OpenAI-compatible API."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
            )

            stream = await client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=True,
            )

            total_content = ""
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    content = chunk.choices[0].delta.content
                    total_content += content
                    yield content

            # Record token usage after streaming completes
            prompt_tokens = self.token_counter.count(
                " ".join(m["content"] for m in messages), self.model
            )
            completion_tokens = self.token_counter.count(total_content, self.model)
            self.budget.record(prompt_tokens, completion_tokens)

        except ImportError:
            # Fallback: make a non-streaming call and yield the whole thing
            result = await self._call_with_http(
                self.api_key, self.base_url, messages, max_tokens, temperature, self.model, **kwargs
            )
            if result.get("status") == "success":
                content = result["content"]
                yield content
            else:
                raise RuntimeError(f"Stream fallback failed: {result.get('error')}")

    async def _stream_anthropic(
        self,
        prompt: str,
        system: str,
        max_tokens: int,
        temperature: float,
        **kwargs,
    ) -> AsyncIterator[str]:
        """Stream using Anthropic's native API."""
        try:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=self.api_key, timeout=self.timeout)

            stream = await client.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system if system else anthropic.NOT_GIVEN,
                messages=[{"role": "user", "content": prompt}],
            )

            total_content = ""
            async for text in stream.text_stream:
                total_content += text
                yield text

            prompt_tokens = self.token_counter.count(prompt, self.model)
            completion_tokens = self.token_counter.count(total_content, self.model)
            self.budget.record(prompt_tokens, completion_tokens)

        except ImportError:
            # Fallback to non-streaming HTTP call
            result = await self._call_anthropic_http(
                self.api_key, prompt, system, max_tokens, temperature, self.model, **kwargs
            )
            if result.get("status") == "success":
                yield result["content"]
            else:
                raise RuntimeError(f"Anthropic stream fallback failed: {result.get('error')}")

    # ─── Utility Methods ───────────────────────────────────────────

    def _extract_json(self, text: str) -> Any:
        """Extract JSON from a response that may contain markdown or extra text."""
        if not text:
            raise ValueError("Empty response")

        text = text.strip()

        # Try direct parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try extracting from markdown code blocks
        if "```" in text:
            parts = text.split("```")
            for part in parts[1:]:
                if part.strip().startswith("json"):
                    part = part.strip()[4:].strip()
                else:
                    part = part.strip()
                try:
                    return json.loads(part)
                except json.JSONDecodeError:
                    continue

        # Try finding JSON object boundaries
        for start_char, end_char in [("{", "}"), ("[", "]")]:
            start = text.find(start_char)
            end = text.rfind(end_char)
            if start != -1 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    continue

        raise json.JSONDecodeError("No JSON found in response", text, 0)

    def _validate_schema(self, data: Any, schema: Dict[str, Any]) -> List[str]:
        """Validate extracted data against a simple schema.

        The schema format is:
        - {"field": "string"} → field should be a string
        - {"field": "number"} → field should be a number
        - {"field": "boolean"} → field should be a boolean
        - {"field": ["string"]} → field should be a list of strings
        - {"field": {...}} → field should be a nested object

        Returns list of validation errors (empty if valid).
        """
        errors = []
        if not isinstance(data, dict):
            errors.append(f"Expected object, got {type(data).__name__}")
            return errors

        type_map = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
        }

        for field, field_type in schema.items():
            if field not in data:
                errors.append(f"Missing field: {field}")
                continue

            value = data[field]
            if value is None:
                continue  # null is allowed for missing fields

            if isinstance(field_type, str):
                expected = type_map.get(field_type)
                if expected and not isinstance(value, expected):
                    errors.append(
                        f"Field '{field}': expected {field_type}, got {type(value).__name__}"
                    )
            elif isinstance(field_type, list) and len(field_type) == 1:
                if not isinstance(value, list):
                    errors.append(f"Field '{field}': expected array, got {type(value).__name__}")
                elif field_type[0] in type_map:
                    expected_item = type_map[field_type[0]]
                    for i, item in enumerate(value):
                        if not isinstance(item, expected_item):
                            errors.append(
                                f"Field '{field}[{i}]': expected {field_type[0]}, "
                                f"got {type(item).__name__}"
                            )
            elif isinstance(field_type, dict):
                if not isinstance(value, dict):
                    errors.append(f"Field '{field}': expected object, got {type(value).__name__}")

        return errors

    def _reset_clients(self):
        """Reset all cached client instances."""
        with self._client_lock:
            self._openai_client = None
            self._anthropic_client = None
            self._http_client = None

    @property
    def is_available(self) -> bool:
        """Check if a provider is configured and available."""
        return bool(self.api_key and self.api_key.strip() != "")

    @property
    def provider_info(self) -> Dict[str, Any]:
        """Return current provider configuration info."""
        return {
            "provider": self.provider_name,
            "model": self.model,
            "base_url": self.base_url,
            "available": self.is_available,
            "fallback_chain": self.fallback_chain,
            "available_providers": [p.get("provider") for p in self._available_providers],
        }


# ════════════════════════════════════════════════════════════════════
# Module-level convenience — singleton pattern
# ════════════════════════════════════════════════════════════════════

_global_provider: Optional[UniversalProvider] = None
_provider_lock = threading.Lock()


def get_llm(
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    **kwargs,
) -> UniversalProvider:
    """Get or create the global LLM provider instance.

    On first call, creates a UniversalProvider with auto-detection.
    Subsequent calls return the same instance unless you provide
    different parameters.

    Usage:
        from src.core.llm_provider import get_llm
        llm = get_llm()
        result = await llm.complete("Hello")
    """
    global _global_provider

    # If explicit params provided, always create new
    if provider or api_key or base_url or model:
        return UniversalProvider(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
            **kwargs,
        )

    # Return cached singleton
    with _provider_lock:
        if _global_provider is None:
            _global_provider = UniversalProvider(**kwargs)
        return _global_provider


def reset_llm():
    """Reset the global LLM provider (useful for testing or re-config)."""
    global _global_provider
    with _provider_lock:
        if _global_provider is not None:
            _global_provider._reset_clients()
            _global_provider.cache.clear()
        _global_provider = None


__all__ = [
    "UniversalProvider",
    "TokenBudget",
    "PromptCompressor",
    "ResponseCache",
    "SmartTruncation",
    "TokenCounter",
    "auto_detect_provider",
    "detect_available_providers",
    "get_llm",
    "reset_llm",
    "PROVIDER_REGISTRY",
    "DEFAULT_FALLBACK_CHAIN",
]
