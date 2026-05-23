#!/usr/bin/env python3
"""
Agent-OS MCP Passthrough Wrapper
=================================
Drop-in MCP server that works WITHOUT any LLM API key.

How it works:
  - All 199 browser tools → proxied to Agent-OS server (needs it running)
  - LLM tools (llm-complete, llm-classify, etc.) → built-in rule-based (no API)
  - MCP client's own LLM (Claude, GPT-4, etc.) handles all reasoning
  - Agent-OS is just the tool execution layer

Usage:
    # With Agent-OS server running:
    AGENT_OS_URL=http://localhost:8001 AGENT_OS_TOKEN=your-token python3 mcp_passthrough.py

    # Or use the startup script:
    ./run_mcp.sh

Config for Claude Desktop:
    {
      "mcpServers": {
        "agent-os": {
          "command": "python3",
          "args": ["/absolute/path/to/Agent-OS/connectors/mcp_passthrough.py"],
          "env": {
            "AGENT_OS_URL": "http://localhost:8001",
            "AGENT_OS_TOKEN": "your-token"
          }
        }
      }
    }
"""
import os
import json
import sys
import re
import logging
import asyncio
from typing import Any, Dict, List, Optional, Tuple
from collections import OrderedDict

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from connectors._tool_registry import TOOLS, get_command_map, get_mcp_tools

# ─── Configuration ───────────────────────────────────────────

def resolve_agent_token() -> str:
    # 1. Environment Variable
    token = os.environ.get("AGENT_OS_TOKEN")
    if token:
        return token

    # 2. Try .env in repo root or ~/.agent-os/.env
    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = [
        os.path.join(repo_dir, ".env"),
        os.path.expanduser("~/.agent-os/.env")
    ]
    for path in paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            if k in ("AGENT_TOKEN", "AGENT_OS_TOKEN") and v:
                                return v
            except Exception:
                pass

    # 3. Try config.yaml in ~/.agent-os/ or repo root
    config_paths = [
        os.path.expanduser("~/.agent-os/config.yaml"),
        os.path.join(repo_dir, "config.yaml")
    ]
    for path in config_paths:
        if os.path.exists(path):
            try:
                import yaml
                with open(path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f)
                    if cfg and isinstance(cfg, dict):
                        t = cfg.get("server", {}).get("agent_token")
                        if t:
                            return t
            except Exception:
                pass

    # 4. Fallback to generating a temp token
    import secrets
    fallback_token = secrets.token_urlsafe(32)
    print(f"WARNING: AGENT_OS_TOKEN not set or found. Generated temp token: {fallback_token}", file=sys.stderr)
    return fallback_token

AGENT_OS_URL = os.environ.get("AGENT_OS_URL", "http://localhost:8001")
AGENT_OS_TOKEN = resolve_agent_token()

# Compression settings — controls how much tool output gets trimmed
# AGENT_OS_COMPRESS: "aggressive" | "normal" | "off"
COMPRESS_MODE = os.environ.get("AGENT_OS_COMPRESS", "aggressive").lower()
# AGENT_OS_MAX_OUTPUT: max chars returned per tool call (default 8000)
MAX_OUTPUT_CHARS = int(os.environ.get("AGENT_OS_MAX_OUTPUT", "8000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    stream=sys.stderr,  # MCP uses stdout for protocol, log to stderr
)
logger = logging.getLogger("agent-os-mcp-passthrough")

# ─── HTTP Client ─────────────────────────────────────────────

_client: Optional[httpx.AsyncClient] = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=60.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


# ─── Agent-OS Server Communication ───────────────────────────

async def agent_os_command(command: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
    """Send command to Agent-OS server with retry logic. Returns error dict if server unavailable."""
    import asyncio as _asyncio

    payload = {"token": AGENT_OS_TOKEN, "command": command}
    if params:
        payload.update(params)

    client = await _get_client()
    max_retries = 3
    last_error = None

    for attempt in range(max_retries):
        try:
            response = await client.post(f"{AGENT_OS_URL}/command", json=payload, timeout=30.0)
            result = response.json()
            # Don't retry if the server responded (even with an error)
            return result
        except httpx.ConnectError:
            last_error = (
                f"Cannot connect to Agent-OS server at {AGENT_OS_URL}. "
                f"Start it with: python main.py --agent-token '{AGENT_OS_TOKEN}'"
            )
            # Retry on connection errors (server may be restarting)
            if attempt < max_retries - 1:
                await _asyncio.sleep(1.0 * (attempt + 1))
                continue
            return {
                "status": "error",
                "error": last_error,
                "hint": "Browser tools require Agent-OS server running. LLM tools work without it."
            }
        except httpx.TimeoutException:
            last_error = f"Request timed out (attempt {attempt + 1}/{max_retries})"
            if attempt < max_retries - 1:
                await _asyncio.sleep(0.5 * (attempt + 1))
                continue
            return {"status": "error", "error": last_error}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    return {"status": "error", "error": last_error or "Unknown error"}


async def agent_os_status() -> Dict[str, Any]:
    """Check Agent-OS server health."""
    client = await _get_client()
    try:
        response = await client.get(f"{AGENT_OS_URL}/health", timeout=5.0)
        return {"status": "success", "server": response.json(), "connected": True}
    except Exception:
        return {
            "status": "warning",
            "connected": False,
            "message": f"Agent-OS server not reachable at {AGENT_OS_URL}",
            "llm_tools": "Available (built-in, no API key needed)",
            "browser_tools": "Unavailable (start Agent-OS server)"
        }


# ═══════════════════════════════════════════════════════════════
# Built-in LLM — Rule-based, NO API KEY needed
# ═══════════════════════════════════════════════════════════════

class BuiltinLLM:
    """Pure rule-based LLM substitute. Works completely offline.

    Provides functional implementations for:
    - complete: text analysis and response generation
    - classify: keyword + TF-IDF-like category matching
    - extract: regex-based structured data extraction
    - summarize: extractive summarization (key sentence selection)

    Quality is lower than real LLM but keeps all tools 100% functional
    without any API key dependency.
    """

    @staticmethod
    def complete(prompt: str, system: str = "", max_tokens: int = 500,
                 temperature: float = 0.7) -> Dict[str, Any]:
        """Analyze prompt and generate a contextual response."""
        words = prompt.split()
        word_count = len(words)
        prompt_lower = prompt.lower()

        # Detect intent
        intents = []
        intent_patterns = {
            "question": ["what", "how", "why", "when", "where", "who", "which", "?"],
            "summarization": ["summarize", "summary", "brief", "tldr", "overview"],
            "extraction": ["extract", "find", "get", "list", "pull", "scrape"],
            "classification": ["classify", "categorize", "type", "kind", "sort"],
            "comparison": ["compare", "versus", "vs", "difference", "better"],
            "web_query": ["http", "url", "website", "web", "page", "search", "google"],
            "code": ["code", "function", "script", "program", "debug", "error"],
            "creative": ["write", "story", "poem", "creative", "generate"],
        }

        for intent, keywords in intent_patterns.items():
            if any(kw in prompt_lower for kw in keywords):
                intents.append(intent)

        # Extract key entities (capitalized words, URLs, emails)
        entities = {
            "urls": re.findall(r'https?://[^\s]+', prompt),
            "emails": re.findall(r'[\w.+-]+@[\w-]+\.[\w.-]+', prompt),
            "numbers": re.findall(r'\b\d+(?:\.\d+)?\b', prompt),
            "capitalized": re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', prompt),
        }

        # Build response
        parts = []

        if system:
            parts.append(f"[System context: {system[:100]}]")

        if "question" in intents:
            # Extract the question
            questions = [s.strip() for s in re.split(r'[.!]', prompt) if '?' in s]
            if questions:
                parts.append(f"Question identified: {questions[0][:200]}")
                parts.append("This requires domain knowledge or real-time data to answer accurately.")
            else:
                parts.append(f"Query analyzed ({word_count} words). Question-type intent detected.")

        if "web_query" in intents:
            parts.append("This query involves web content. Use browser_navigate to access the page, "
                        "then browser_get_content to extract information.")

        if "code" in intents:
            # Try to identify the programming language
            lang_hints = {
                "python": ["def ", "import ", "print(", "__init__", "self."],
                "javascript": ["function ", "const ", "let ", "=>", "console.log"],
                "java": ["public class", "System.out", "public static void"],
                "rust": ["fn ", "let mut", "println!", "impl "],
                "go": ["func ", "package ", "fmt.Print"],
            }
            detected_lang = "unknown"
            for lang, hints in lang_hints.items():
                if any(h in prompt for h in hints):
                    detected_lang = lang
                    break
            parts.append(f"Code context detected (language: {detected_lang}).")

        if "creative" in intents:
            parts.append("Creative writing request detected. For high-quality creative content, "
                        "configure an LLM provider (OPENAI_API_KEY or ANTHROPIC_API_KEY).")

        if entities["urls"]:
            parts.append(f"URLs found: {', '.join(entities['urls'][:3])}")
            parts.append("Use browser_navigate to access these URLs.")

        if entities["emails"]:
            parts.append(f"Email addresses found: {', '.join(entities['emails'][:3])}")

        if not intents:
            parts.append(f"Input analyzed ({word_count} words, {len(set(words))} unique words).")
            if word_count > 100:
                parts.append("Long text detected. Use llm-summarize for condensation or "
                           "llm-extract for structured data extraction.")

        # Add tool suggestions
        suggestions = []
        if "web_query" in intents or entities["urls"]:
            suggestions.extend(["browser_navigate", "browser_get_content", "browser_smart_navigate"])
        if "extraction" in intents:
            suggestions.extend(["browser_get_links", "browser_get_text", "llm-extract"])
        if "summarization" in intents:
            suggestions.append("llm-summarize")
        if "classification" in intents:
            suggestions.append("llm-classify")

        if suggestions:
            parts.append(f"Suggested tools: {', '.join(suggestions[:5])}")

        # Offline notice
        parts.append("[Built-in rule engine — no external LLM API configured. "
                    "For enhanced AI, set OPENAI_API_KEY or ANTHROPIC_API_KEY]")

        content = "\n".join(parts)

        return {
            "status": "success",
            "content": content,
            "tokens_used": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "provider": "builtin",
            "model": "rule-based",
            "intents_detected": intents,
            "entities": {k: v for k, v in entities.items() if v},
        }

    @staticmethod
    def classify(text: str, categories: List[str]) -> Dict[str, Any]:
        """Keyword + semantic category matching with keyword associations."""
        text_lower = text.lower()
        text_words = set(re.findall(r'\w+', text_lower))

        # Semantic keyword map: category type → related words found in text
        CATEGORY_SEMANTICS = {
            "web_browsing": ["browse", "website", "web", "url", "page", "open", "navigate",
                           "go to", "visit", "search", "google", "github", "amazon", "online",
                           "internet", "link", "site", "http", "www", "click"],
            "coding": ["code", "program", "script", "function", "debug", "compile", "run",
                      "python", "javascript", "java", "rust", "git", "repository", "repo",
                      "api", "developer", "software", "bug", "error", "test"],
            "email": ["email", "mail", "send", "inbox", "message", "attachment", "reply",
                     "forward", "compose", "recipient", "subject"],
            "shopping": ["buy", "purchase", "shop", "order", "cart", "price", "product",
                        "amazon", "ebay", "store", "deal", "discount", "checkout", "payment"],
            "question": ["what", "how", "why", "when", "where", "who", "which",
                        "explain", "tell me", "describe", "?", "define", "meaning"],
            "summarize": ["summarize", "summarization", "summary", "brief", "tldr", "overview",
                        "shorten", "condense", "key points", "main ideas"],
            "extract": ["extract", "find", "get", "list", "pull", "scrape", "parse"],
            "classify": ["classify", "categorize", "type", "kind", "sort", "group"],
            "write": ["write", "compose", "draft", "create", "article", "blog", "essay"],
            "translate": ["translate", "translation", "language"],
            "math": ["calculate", "math", "equation", "solve", "compute", "formula"],
            "image": ["image", "photo", "picture", "screenshot", "visual", "diagram"],
        }

        scores = {}
        for cat in categories:
            cat_lower = cat.lower()
            cat_words = set(re.findall(r'\w+', cat_lower))
            cat_clean = re.sub(r'[_\-/]', ' ', cat_lower)
            cat_clean_words = set(re.findall(r'\w+', cat_clean))
            all_cat_words = cat_words | cat_clean_words

            score = 0.0

            # 1. Exact phrase match
            if cat_lower in text_lower or cat_clean in text_lower:
                score = 1.0
            else:
                # 2. Direct word overlap
                overlap = len(all_cat_words & text_words)
                if overlap > 0:
                    score += (overlap / max(len(all_cat_words), 1)) * 0.5

                # 3. Partial word matching (bidirectional substring + prefix)
                partial = 0
                for cw in all_cat_words:
                    for tw in text_words:
                        if len(cw) > 2 and len(tw) > 2:
                            if cw in tw or tw in cw:  # Bidirectional substring
                                partial += 0.5
                            elif len(cw) > 4 and len(tw) > 4 and (cw[:4] == tw[:4] or cw[-4:] == tw[-4:]):
                                partial += 0.3  # Same prefix or suffix
                score += min(0.4, partial * 0.1)

                # 4. Semantic keyword matching
                for sem_cat, keywords in CATEGORY_SEMANTICS.items():
                    if sem_cat in cat_lower or cat_lower in sem_cat or \
                       any(w in sem_cat for w in all_cat_words if len(w) > 3):
                        keyword_hits = sum(1 for kw in keywords if kw in text_lower)
                        if keyword_hits > 0:
                            score += min(0.5, keyword_hits * 0.12)

            scores[cat] = min(1.0, score)

        if not scores:
            return {
                "status": "success",
                "category": "unknown",
                "confidence": 0.0,
                "reasoning": "No categories provided",
                "all_scores": {},
                "provider": "builtin",
            }

        best_cat = max(scores, key=scores.get)
        best_score = scores.get(best_cat, 0.0)

        # Normalize scores
        max_score = max(scores.values()) if scores else 1
        if max_score > 0:
            normalized = {k: round(v / max_score, 3) for k, v in scores.items()}
        else:
            normalized = {k: 0.0 for k in scores}

        return {
            "status": "success",
            "category": best_cat,
            "confidence": round(best_score, 3),
            "reasoning": f"Keyword overlap analysis (score: {best_score:.3f})",
            "all_scores": normalized,
            "provider": "builtin",
            "model": "rule-based",
        }

    @staticmethod
    def extract(text: str, schema: Dict[str, Any]) -> Dict[str, Any]:
        """Regex-based structured data extraction."""
        data = {}

        extractors = {
            "email": (r'[\w.+-]+@[\w-]+\.[\w.-]+', str),
            "emails": (r'[\w.+-]+@[\w-]+\.[\w.-]+', list),
            "phone": (r'[\+]?[(]?[0-9]{1,4}[)]?[-\s\./0-9]{7,15}', str),
            "phones": (r'[\+]?[(]?[0-9]{1,4}[)]?[-\s\./0-9]{7,15}', list),
            "url": (r'https?://[^\s<>"{}|\\^`\[\]]+', str),
            "urls": (r'https?://[^\s<>"{}|\\^`\[\]]+', list),
            "date": (r'\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}', str),
            "ip": (r'\b(?:\d{1,3}\.){3}\d{1,3}\b', str),
            "number": (r'\b\d+(?:\.\d+)?\b', float),
            "name": (r'(?:name|author|person|by)[\s:]+([A-Z][a-z]+ [A-Z][a-z]+)', str),
            "title": (r'(?:title|heading|subject)[\s:]+(.+?)(?:\n|$)', str),
        }

        # Track which matches have been consumed (for multiple same-type fields)
        consumed_positions: Dict[str, set] = {}  # pattern -> set of char positions already used

        for field_name, field_type in schema.items():
            field_key = str(field_name)
            field_type_str = str(field_type) if isinstance(field_type, str) else ""

            # Determine extractor
            pattern = None
            return_list = False

            # Check if field name matches an extractor
            field_lower = field_key.lower()
            for ext_name, (ext_pattern, ext_type) in extractors.items():
                if ext_name in field_lower or field_lower in ext_name:
                    pattern = ext_pattern
                    return_list = ext_type == list or isinstance(field_type, list)
                    break

            # Check schema type hints
            if not pattern:
                if "email" in field_lower:
                    pattern = extractors["email"][0]
                elif "phone" in field_lower or "tel" in field_lower:
                    pattern = extractors["phone"][0]
                elif "url" in field_lower or "link" in field_lower:
                    pattern = extractors["url"][0]
                elif "date" in field_lower or "time" in field_lower:
                    pattern = extractors["date"][0]
                elif "ip" in field_lower:
                    pattern = extractors["ip"][0]
                elif isinstance(field_type, list) or "list" in field_type_str.lower():
                    pattern = extractors.get(field_key, (r'.+', list))[0]
                    return_list = True
                elif field_type_str in ("number", "float", "int", "integer"):
                    pattern = extractors["number"][0]
                else:
                    # Generic: extract a meaningful sentence
                    matches = re.findall(r'(.{5,100}?)(?:\.|,|\n|$)', text)
                    data[field_key] = matches[0].strip() if matches else None
                    continue

            if pattern:
                if return_list or isinstance(field_type, list):
                    found = re.findall(pattern, text, re.IGNORECASE)
                    data[field_key] = list(dict.fromkeys(m.strip() for m in found))[:10]
                else:
                    # Find all matches with positions
                    all_matches = list(re.finditer(pattern, text, re.IGNORECASE))
                    if not all_matches:
                        data[field_key] = None
                        continue

                    # For number-type fields, skip already-consumed positions
                    if pattern not in consumed_positions:
                        consumed_positions[pattern] = set()

                    val = None
                    for m in all_matches:
                        if m.start() not in consumed_positions[pattern]:
                            val = m.group().strip()
                            consumed_positions[pattern].add(m.start())
                            break

                    if val:
                        val = val.split('\n')[0].strip()
                    data[field_key] = val

        return {
            "status": "success",
            "data": data,
            "validation_errors": [],
            "provider": "builtin",
            "model": "rule-based",
        }

    @staticmethod
    def summarize(text: str, max_length: int = 200) -> Dict[str, Any]:
        """Extractive summarization — picks the most important sentences."""
        # Split into sentences
        sentences = re.split(r'(?<=[.!?])\s+', text.strip())
        sentences = [s.strip() for s in sentences if len(s.strip()) > 15]

        if not sentences:
            summary = text[:500] if len(text) > 500 else text
            return {
                "status": "success",
                "summary": summary,
                "original_length": len(text.split()),
                "summary_length": len(summary.split()),
                "compression_ratio": 1.0,
                "provider": "builtin",
                "model": "rule-based",
            }

        # Score sentences
        important_words = {
            "important", "key", "main", "primary", "essential", "significant",
            "result", "conclusion", "finding", "therefore", "however", "but",
            "discovered", "shows", "indicates", "proves", "demonstrates",
            "first", "second", "finally", "notably", "specifically",
        }

        # Calculate word frequency across all sentences
        all_words = []
        for sent in sentences:
            all_words.extend(w.lower() for w in re.findall(r'\w+', sent) if len(w) > 3)

        word_freq = {}
        for w in all_words:
            word_freq[w] = word_freq.get(w, 0) + 1

        # Score each sentence
        scored = []
        total_sents = len(sentences)
        for i, sent in enumerate(sentences):
            score = 0.0

            # Position score (first/last sentences are usually important)
            if i == 0:
                score += 0.4
            elif i == total_sents - 1:
                score += 0.2
            elif i < 3:
                score += 0.15

            # Important keyword score
            words = set(w.lower() for w in re.findall(r'\w+', sent))
            score += len(words & important_words) * 0.15

            # Word frequency score (sentences with frequent words are important)
            sent_words = [w.lower() for w in re.findall(r'\w+', sent) if len(w) > 3]
            if sent_words:
                avg_freq = sum(word_freq.get(w, 0) for w in sent_words) / len(sent_words)
                score += min(0.3, avg_freq * 0.05)

            # Length penalty (very short or very long sentences)
            word_count = len(sent.split())
            if 8 <= word_count <= 35:
                score += 0.1
            elif word_count < 5:
                score -= 0.2

            # Contains numbers/data (often important)
            if re.search(r'\d+', sent):
                score += 0.1

            scored.append((score, i, sent))

        # Select sentences up to max_length words
        target_words = min(max_length, 300)
        scored.sort(key=lambda x: -x[0])

        selected = []
        total_words = 0
        for score, idx, sent in scored:
            if total_words >= target_words:
                break
            selected.append((idx, sent))
            total_words += len(sent.split())

        # Restore original order
        selected.sort(key=lambda x: x[0])
        summary = ' '.join(s for _, s in selected)

        original_words = len(text.split())
        summary_words = len(summary.split())

        return {
            "status": "success",
            "summary": summary,
            "original_length": original_words,
            "summary_length": summary_words,
            "compression_ratio": round(summary_words / max(original_words, 1), 3),
            "sentences_selected": len(selected),
            "sentences_total": total_sents,
            "provider": "builtin",
            "model": "rule-based",
        }


# ═══════════════════════════════════════════════════════════════
# Smart Compressor — Token Saver
# ═══════════════════════════════════════════════════════════════
# Browser tool results (HTML, page text, links etc.) can be HUGE.
# Without compression, 1 page visit = 10,000-50,000 tokens burned
# in the MCP client's context. This compressor strips the fat
# BEFORE sending back to the LLM.

class SmartCompressor:
    """Compress browser tool results to minimize token burn.

    Strategies:
    1. Strip HTML → keep only readable text
    2. Remove boilerplate (nav, footer, sidebar, scripts, styles)
    3. Deduplicate repeated content
    4. Cap output size per tool type
    5. Keep URLs/links as compact references
    """

    # Max chars to return per tool type (keeps token burn low)
    MAX_OUTPUT = {
        "get_content":    3000,   # Page text content
        "get_dom":        1500,   # DOM/HTML (heavily compressed)
        "get_text":       3000,   # Extracted text
        "get_links":      2000,   # Links list
        "get_images":     1500,   # Image URLs
        "page_summary":   2000,   # Page analysis
        "page_tables":    3000,   # Tables (structured data)
        "page_structured":2000,   # Structured data
        "page_seo":       1500,   # SEO data
        "screenshot":      200,   # Just confirm, no data
        "default":        2000,   # Everything else
    }

    # HTML tags to completely remove (content + tag)
    STRIP_TAGS = {
        'script', 'style', 'noscript', 'iframe', 'svg', 'path',
        'link', 'meta', 'comment', 'head',
    }

    # Boilerplate patterns to remove from text
    BOILERPLATE_PATTERNS = [
        re.compile(r'(?i)(cookie|privacy|consent|gdpr)[^\n]{0,200}', re.IGNORECASE),
        re.compile(r'(?i)(subscribe|newsletter|sign.?up)[^\n]{0,150}', re.IGNORECASE),
        re.compile(r'(?i)(copyright|all rights reserved)[^\n]{0,100}', re.IGNORECASE),
        re.compile(r'(?i)(loading|please wait)[^\n]{0,50}', re.IGNORECASE),
    ]

    @classmethod
    def compress(cls, result: Dict[str, Any], tool_name: str = "",
                 mode: str = "") -> Dict[str, Any]:
        """Compress a tool result dict. Returns modified copy.

        mode: "aggressive" (default) | "normal" | "off"
        """
        if not result or not isinstance(result, dict):
            return result

        # Respect global config
        mode = mode or COMPRESS_MODE
        if mode == "off":
            return result

        result = dict(result)  # Shallow copy

        # Normal mode = lighter compression (2x limits)
        normal_mult = 2 if mode == "normal" else 1

        # Determine max output size
        max_chars = cls.MAX_OUTPUT.get("default", 2000) * normal_mult
        for key, limit in cls.MAX_OUTPUT.items():
            if key in tool_name:
                max_chars = limit * normal_mult
                break

        # ─── Compress HTML content ───
        if "html" in result:
            html = result["html"]
            if isinstance(html, str) and len(html) > 500:
                result["html"] = cls._strip_html(html, max_chars)
                result["_html_compressed"] = True
                result["_html_original_chars"] = len(html)

        # ─── Compress text content ───
        if "text" in result:
            text = result["text"]
            if isinstance(text, str) and len(text) > max_chars:
                result["text"] = cls._compress_text(text, max_chars)
                result["_text_compressed"] = True
                result["_text_original_chars"] = len(text)

        # ─── Compress page content fields ───
        for field in ("content", "summary", "description", "body"):
            if field in result:
                val = result[field]
                if isinstance(val, str) and len(val) > max_chars:
                    result[field] = val[:max_chars] + "\n... [compressed]"

        # ─── Compress links list ───
        if "links" in result and isinstance(result["links"], list):
            links = result["links"]
            if len(links) > 50:
                result["links"] = links[:50]
                result["_links_truncated"] = True
                result["_total_links"] = len(links)

        # ─── Compress images list ───
        if "images" in result and isinstance(result["images"], list):
            images = result["images"]
            if len(images) > 30:
                result["images"] = images[:30]
                result["_images_truncated"] = True
                result["_total_images"] = len(images)

        # ─── Compress tables ───
        if "tables" in result and isinstance(result["tables"], list):
            tables = result["tables"]
            for i, table in enumerate(tables):
                if isinstance(table, dict) and "rows" in table:
                    rows = table["rows"]
                    if isinstance(rows, list) and len(rows) > 20:
                        table["rows"] = rows[:20]
                        table["_truncated"] = True
                        table["_total_rows"] = len(rows)

        # ─── Remove large screenshot data from output ───
        if "screenshot" in result:
            screenshot = result["screenshot"]
            if isinstance(screenshot, str) and len(screenshot) > 1000:
                result["screenshot"] = f"[screenshot: {len(screenshot)} bytes base64 — use directly, not for reading]"
                result["_screenshot_removed"] = True

        # ─── Compress DOM ───
        if "dom" in result:
            dom = result["dom"]
            if isinstance(dom, str) and len(dom) > max_chars:
                result["dom"] = cls._strip_html(dom, max_chars)
                result["_dom_compressed"] = True

        # ─── Final size check ───
        total_output = json.dumps(result, ensure_ascii=False)
        if len(total_output) > max_chars * 3:
            # Still too large — aggressive trim
            result = cls._aggressive_trim(result, max_chars * 3)

        return result

    @classmethod
    def _strip_html(cls, html: str, max_chars: int) -> str:
        """Strip HTML to readable text, removing boilerplate."""
        # Quick approach: remove tags, keep text
        text = html

        # Remove script/style/noscript blocks entirely
        for tag in cls.STRIP_TAGS:
            text = re.sub(
                rf'<{tag}[^>]*>.*?</{tag}>',
                '', text, flags=re.DOTALL | re.IGNORECASE
            )
            # Self-closing
            text = re.sub(rf'<{tag}[^>]*/?>', '', text, flags=re.IGNORECASE)

        # Remove all remaining HTML tags
        text = re.sub(r'<[^>]+>', ' ', text)

        # Decode common HTML entities
        entities = {
            '&nbsp;': ' ', '&amp;': '&', '&lt;': '<', '&gt;': '>',
            '&quot;': '"', '&#39;': "'", '&mdash;': '—', '&ndash;': '–',
            '&hellip;': '…', '&copy;': '©', '&reg;': '®',
        }
        for entity, char in entities.items():
            text = text.replace(entity, char)

        # Remove boilerplate
        for pattern in cls.BOILERPLATE_PATTERNS:
            text = pattern.sub('', text)

        # Clean whitespace
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r' {2,}', ' ', text)
        text = text.strip()

        # Truncate
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... [truncated]"

        return text

    @classmethod
    def _compress_text(cls, text: str, max_chars: int) -> str:
        """Compress plain text — remove duplicates, boilerplate, truncate."""
        # Remove boilerplate
        for pattern in cls.BOILERPLATE_PATTERNS:
            text = pattern.sub('', text)

        # Split into lines and remove duplicate lines
        lines = text.split('\n')
        seen = set()
        unique_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped or len(stripped) < 5:
                continue
            key = stripped.lower()
            if key not in seen:
                seen.add(key)
                unique_lines.append(stripped)

        result = '\n'.join(unique_lines)

        # Truncate
        if len(result) > max_chars:
            # Smart truncation: keep first 60% + last 20%
            first_part = int(max_chars * 0.6)
            last_part = int(max_chars * 0.2)
            result = (
                result[:first_part]
                + "\n\n... [middle content compressed — "
                + f"original {len(result):,} chars] ...\n\n"
                + result[-last_part:]
            )

        return result

    @classmethod
    def _aggressive_trim(cls, result: Dict[str, Any], max_total: int) -> Dict[str, Any]:
        """Aggressively trim a result that's still too large."""
        # Keep only essential fields
        essential_keys = {
            'status', 'error', 'url', 'title', 'text', 'content',
            'summary', 'links', 'images', 'category', 'confidence',
            'data', 'message',
        }

        trimmed = {}
        current_size = 0
        for key, val in result.items():
            if key.startswith('_'):  # Keep metadata
                trimmed[key] = val
                continue
            if key in essential_keys:
                val_str = json.dumps(val, ensure_ascii=False)
                if current_size + len(val_str) > max_total:
                    # Truncate this value
                    if isinstance(val, str):
                        trimmed[key] = val[:max(100, max_total - current_size)] + "..."
                    elif isinstance(val, list):
                        trimmed[key] = val[:5]
                    else:
                        trimmed[key] = val
                    current_size = max_total
                else:
                    trimmed[key] = val
                    current_size += len(val_str)
            # Skip non-essential fields

        return trimmed


# ─── Initialize ──────────────────────────────────────────────

builtin_llm = BuiltinLLM()

# ─── MCP Server Setup ────────────────────────────────────────

server = Server("agent-os-passthrough")

# Build tool list from registry
TOOLS_LIST: List[Tool] = []
_mcp_tools = get_mcp_tools()
for tool_def in _mcp_tools:
    TOOLS_LIST.append(Tool(
        name=tool_def["name"],
        description=tool_def["description"],
        inputSchema=tool_def["inputSchema"],
    ))

command_map = get_command_map()

# Identify which tools are LLM-only (don't need browser)
LLM_TOOL_NAMES = {
    "llm_complete", "llm_classify", "llm_extract", "llm_summarize",
    "llm_provider_set", "llm_token_usage", "llm_cache_clear",
}

# Map MCP tool names to their handler
LLM_HANDLER_MAP = {}  # Built dynamically below


def _find_llm_tool(mcp_name: str) -> Optional[str]:
    """Check if an MCP tool name corresponds to an LLM command."""
    for t in TOOLS:
        if t.mcp_name == mcp_name and t.server_cmd.startswith("llm-"):
            return t.server_cmd
    return None


# ─── MCP Handlers ────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> List[Tool]:
    """List all available tools."""
    return TOOLS_LIST


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    """Execute a tool — LLM tools use built-in, browser tools proxy to Agent-OS."""

    # Check if this is an LLM tool (use built-in handler)
    llm_cmd = _find_llm_tool(name)
    if llm_cmd:
        result = await _handle_llm_tool(llm_cmd, arguments)
    # Check if this is a status/health check
    elif name == "browser_status":
        result = await agent_os_status()
    # All other tools → proxy to Agent-OS server
    elif name in command_map:
        cmd_name, param_keys = command_map[name]
        params = {k: arguments[k] for k in param_keys if k in arguments}
        result = await agent_os_command(cmd_name, params)
    else:
        result = {"status": "error", "error": f"Unknown tool: {name}"}

    # ─── Smart Compress: Minimize token burn ───
    # Browser results can be HUGE (HTML = 20k-50k chars).
    # Compress BEFORE sending back to MCP client's LLM.
    # Set AGENT_OS_COMPRESS=off to disable, AGENT_OS_COMPRESS=normal for lighter.
    result = SmartCompressor.compress(result, name)

    # Format response
    output = json.dumps(result, indent=2, ensure_ascii=False)

    # Hard cap: configurable via AGENT_OS_MAX_OUTPUT (default 8000)
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + f"\n... [capped at {MAX_OUTPUT_CHARS:,} chars — set AGENT_OS_MAX_OUTPUT to change]"

    return [TextContent(type="text", text=output)]


async def _handle_llm_tool(command: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Handle LLM tools using the built-in rule-based engine."""
    try:
        if command == "llm-complete":
            prompt = arguments.get("prompt", "")
            if not prompt:
                return {"status": "error", "error": "Missing 'prompt' parameter"}
            return builtin_llm.complete(
                prompt,
                system=arguments.get("system_prompt", arguments.get("system", "")),
                max_tokens=arguments.get("max_tokens", 500),
                temperature=arguments.get("temperature", 0.7),
            )

        elif command == "llm-classify":
            text = arguments.get("text", "")
            categories = arguments.get("categories", [])
            if not text:
                return {"status": "error", "error": "Missing 'text' parameter"}
            if not categories:
                return {"status": "error", "error": "Missing 'categories' parameter"}
            return builtin_llm.classify(text, categories)

        elif command == "llm-extract":
            text = arguments.get("text", "")
            schema = arguments.get("schema", {})
            if not text:
                return {"status": "error", "error": "Missing 'text' parameter"}
            return builtin_llm.extract(text, schema)

        elif command == "llm-summarize":
            text = arguments.get("text", "")
            if not text:
                return {"status": "error", "error": "Missing 'text' parameter"}
            return builtin_llm.summarize(
                text,
                max_length=arguments.get("max_length", 200),
            )

        elif command == "llm-provider-set":
            return {
                "status": "success",
                "message": "Passthrough mode: LLM provider is built-in (rule-based). "
                          "No external provider needed — MCP client's LLM handles reasoning.",
                "provider": "builtin",
            }

        elif command == "llm-token-usage":
            return {
                "status": "success",
                "data": {
                    "provider": "builtin",
                    "tokens_used": 0,
                    "note": "Built-in engine uses no tokens — pure rule-based processing."
                }
            }

        elif command == "llm-cache-clear":
            return {"status": "success", "message": "Built-in engine has no cache to clear."}

        else:
            return {"status": "error", "error": f"Unknown LLM command: {command}"}

    except Exception as e:
        return {"status": "error", "error": f"LLM handler error: {str(e)}"}


# ─── Entry Point ──────────────────────────────────────────────

async def main():
    logger.info("=" * 60)
    logger.info("Agent-OS MCP Passthrough Wrapper")
    logger.info("=" * 60)
    logger.info(f"Agent-OS URL: {AGENT_OS_URL}")
    logger.info(f"Token: {AGENT_OS_TOKEN[:10]}...")
    logger.info(f"Total tools: {len(TOOLS_LIST)}")

    # Count tool categories
    browser_tools = sum(1 for t in TOOLS if not t.server_cmd.startswith("llm-"))
    llm_tools = sum(1 for t in TOOLS if t.server_cmd.startswith("llm-"))
    logger.info(f"  Browser tools: {browser_tools} (proxy to Agent-OS server)")
    logger.info(f"  LLM tools: {llm_tools} (built-in, no API key needed)")
    logger.info("")
    logger.info("MODE: Passthrough — MCP client's LLM (Claude/GPT) handles reasoning")
    logger.info("      Agent-OS executes browser actions + provides rule-based LLM")
    logger.info("")

    # Check server connection
    status = await agent_os_status()
    if status.get("connected"):
        logger.info("✅ Agent-OS server is reachable")
    else:
        logger.warning("⚠️  Agent-OS server not reachable — browser tools will return errors")
        logger.warning(f"   Start it: python main.py --agent-token '{AGENT_OS_TOKEN}'")
        logger.info("   LLM tools still work without the server.")

    logger.info("=" * 60)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
