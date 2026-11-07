import re
from typing import Optional
from google.genai import types as genai_types
from chatint.client_manager import ClientManager

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


def generate_chat_title(
    prompt: str,
    provider_id: Optional[str] = None,
    model_id: Optional[str] = None,
) -> str:
    """
    Generates a concise, human-readable title from the initial user prompt.
    Uses the active chat model via its provider's SDK if available and falls back to offline heuristic extraction.
    """
    if not prompt or not prompt.strip():
        return "New Chat"

    if provider_id and model_id:
        try:
            provider = ClientManager.get_provider(provider_id)
            sdk_type = ClientManager.get_sdk_type(provider, provider_id)
            user_snippet = prompt.strip()[:500]

            if sdk_type == "anthropic":
                client, err = ClientManager.get_anthropic_client(provider_id, timeout=10.0)
                if client and not err:
                    resp = client.messages.create(
                        model=model_id,
                        system=_NAMING_SYSTEM_INSTRUCTION,
                        messages=[{"role": "user", "content": user_snippet}],
                        max_tokens=25,
                    )
                    if resp and getattr(resp, "content", None):
                        text_blocks = [
                            getattr(b, "text", "")
                            for b in resp.content
                            if getattr(b, "type", "") == "text"
                        ]
                        cleaned = _clean_generated_title(" ".join(text_blocks))
                        if cleaned:
                            return cleaned

            elif sdk_type == "gemini":
                client, err = ClientManager.get_gemini_client(provider_id, timeout=10.0)
                if client and not err:
                    resp = client.models.generate_content(
                        model=model_id,
                        contents=user_snippet,
                        config=genai_types.GenerateContentConfig(
                            system_instruction=_NAMING_SYSTEM_INSTRUCTION,
                            max_output_tokens=25,
                        ),
                    )
                    raw_text = (getattr(resp, "text", "") or "").strip()
                    cleaned = _clean_generated_title(raw_text)
                    if cleaned:
                        return cleaned

            else:
                client, err = ClientManager.get_client(provider_id, timeout=10.0)
                if client and not err:
                    resp = client.chat.completions.create(
                        model=model_id,
                        messages=[
                            {
                                "role": "system",
                                "content": _NAMING_SYSTEM_INSTRUCTION,
                            },
                            {"role": "user", "content": user_snippet},
                        ],
                        max_tokens=20,
                        stream=False,
                    )
                    if resp and resp.choices and resp.choices[0].message:
                        raw_title = (resp.choices[0].message.content or "").strip()
                        cleaned = _clean_generated_title(raw_title)
                        if cleaned:
                            return cleaned
        except Exception:
            pass

    return _offline_heuristic_title(prompt)


def _offline_heuristic_title(prompt: str) -> str:
    """
    Offline fallback that derives a clean, concise, human-readable title from the initial user prompt.
    """
    if not prompt:
        return "New Chat"

    # Take first line if multi-line
    first_line = prompt.strip().split("\n")[0].strip()

    # Strip code blocks, markdown punctuation
    cleaned = re.sub(r"[`#*~_>\[\]\(\)]", " ", first_line)
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # Common conversational fillers and prefixes to strip
    prefixes = [
        r"^(please\s+)?(can\s+you\s+)?(could\s+you\s+)?(write|create|code|implement|generate|build)(\s+me)?(\s+a|\s+an)?\s+",
        r"^(please\s+)?(can\s+you\s+)?(could\s+you\s+)?(explain|tell\s+me\s+about|describe|summarize)(\s+to\s+me)?(\s+about)?\s+",
        r"^(what\s+is\s+the\s+difference\s+between|what\s+is|what\s+are|who\s+is|why\s+is|why\s+does)\s+",
        r"^(how\s+do\s+i|how\s+can\s+i|how\s+to|how\s+does)\s+",
        r"^(help\s+me\s+(with|to)|i\s+need\s+help\s+with|i\s+want\s+to)\s+",
    ]

    for p in prefixes:
        cleaned = re.sub(p, "", cleaned, flags=re.IGNORECASE).strip()

    # Remove trailing punctuation (question marks, periods, colons)
    cleaned = re.sub(r"[?!.,:;]+$", "", cleaned).strip()

    if not cleaned:
        cleaned = first_line[:30]

    # Split into words and limit length
    words = cleaned.split()
    if len(words) > 6:
        cleaned = " ".join(words[:6])

    # Limit to max 38 characters
    if len(cleaned) > 38:
        cleaned = cleaned[:35].rsplit(" ", 1)[0]

    # Title-case nicely
    title = cleaned.title().strip()
    return title or "New Chat"
