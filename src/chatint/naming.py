from __future__ import annotations

import re
from typing import Optional

from chatint.catalog import is_gemini_3_or_newer
from chatint.client_manager import ClientManager
from chatint.logging_setup import get_logger
from chatint.providers import get_provider_adapter

_logger = get_logger("chatint.naming")

_NAMING_SYSTEM_INSTRUCTION = (
    "Generate a concise 2 to 5 word title for this conversation based on the user's message. "
    "Output ONLY the title text, with no quotes or trailing punctuation."
)


def _clean_generated_title(raw_title: str) -> Optional[str]:
    if not raw_title:
        return None
    raw_title = re.sub(r"<think>[\s\S]*?</think>", "", raw_title).strip()
    raw_title = raw_title.split("\n")[0].strip().strip('"\'`.?!:;,-')
    if raw_title and len(raw_title) >= 2:
        if len(raw_title) > 42:
            raw_title = raw_title[:39].rsplit(" ", 1)[0]
        return raw_title
    return None


def _finalize_title_usage(
    cleaned: str, user_snippet: str, in_tok: int, out_tok: int, cache_tok: int
) -> tuple[str, int, int, int]:
    if in_tok == 0 and user_snippet:
        in_tok = max(1, len(user_snippet) // 4)
    if out_tok == 0 and cleaned:
        out_tok = max(1, len(cleaned) // 4)
    return cleaned, in_tok, out_tok, cache_tok


def generate_chat_title(
    prompt: str,
    provider_id: Optional[str] = None,
    model_id: Optional[str] = None,
) -> tuple[str, int, int, int]:
    """
    Generate a concise, human-readable title from the initial user prompt via the unified ProviderAdapter,
    falling back cleanly to offline heuristic extraction on error.
    """
    if not prompt or not prompt.strip():
        return "New Chat", 0, 0, 0

    if provider_id and model_id:
        try:
            provider = ClientManager.get_provider(provider_id)
            sdk_type = ClientManager.get_sdk_type(provider, provider_id)
            adapter = get_provider_adapter(sdk_type)
            user_snippet = prompt.strip()[:500]

            if sdk_type == "anthropic":
                client, err = ClientManager.get_anthropic_client(provider_id, timeout=10.0)
            elif sdk_type == "gemini":
                client, err = ClientManager.get_gemini_client(provider_id, timeout=10.0)
            else:
                client, err = ClientManager.get_client(provider_id, timeout=10.0)

            if client and not err:
                try:
                    res = adapter.complete_title(client, model_id, _NAMING_SYSTEM_INSTRUCTION, user_snippet)
                    if res:
                        raw_text, in_tok, out_tok, cache_tok = res
                        cleaned = _clean_generated_title(raw_text)
                        if cleaned:
                            return _finalize_title_usage(cleaned, user_snippet, in_tok, out_tok, cache_tok)
                finally:
                    ClientManager.close_client(client)
        except Exception as exc:
            _logger.debug("Chat title generation fell back to heuristic: %s", exc)

    return _offline_heuristic_title(prompt), 0, 0, 0


def _offline_heuristic_title(prompt: str) -> str:
    """Offline fallback that derives a clean, concise, human-readable title from the initial user prompt."""
    if not prompt:
        return "New Chat"

    first_line = prompt.strip().split("\n")[0].strip()
    cleaned = re.sub(r"[`#*~_>\[\]\(\)]", " ", first_line)
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    prefixes = [
        r"^(please\s+)?(can\s+you\s+)?(could\s+you\s+)?(write|create|code|implement|generate|build)(\s+me)?(\s+a|\s+an)?\s+",
        r"^(please\s+)?(can\s+you\s+)?(could\s+you\s+)?(explain|tell\s+me\s+about|describe|summarize)(\s+to\s+me)?(\s+about)?\s+",
        r"^(what\s+is\s+the\s+difference\s+between|what\s+is|what\s+are|who\s+is|why\s+is|why\s+does)\s+",
        r"^(how\s+do\s+i|how\s+can\s+i|how\s+to|how\s+does)\s+",
        r"^(help\s+me\s+(with|to)|i\s+need\s+help\s+with|i\s+want\s+to)\s+",
    ]

    for p in prefixes:
        cleaned = re.sub(p, "", cleaned, flags=re.IGNORECASE).strip()

    cleaned = re.sub(r"[?!.,:;]+$", "", cleaned).strip()
    if not cleaned:
        cleaned = first_line[:30]

    words = cleaned.split()
    if len(words) > 6:
        cleaned = " ".join(words[:6])

    if len(cleaned) > 38:
        cleaned = cleaned[:35].rsplit(" ", 1)[0]

    title = cleaned.title().strip()
    return title or "New Chat"


__all__ = [
    "_clean_generated_title",
    "_finalize_title_usage",
    "_offline_heuristic_title",
    "generate_chat_title",
    "is_gemini_3_or_newer",
]
