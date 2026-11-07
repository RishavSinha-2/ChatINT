import re
from typing import Optional
from chatint.client_manager import ClientManager


def generate_chat_title(
    prompt: str,
    provider_id: Optional[str] = None,
    model_id: Optional[str] = None,
) -> str:
    """
    Generates a concise, human-readable title from the initial user prompt.
    Uses the active chat model if available and falls back to offline heuristic extraction.
    """
    if not prompt or not prompt.strip():
        return "New Chat"

    if provider_id and model_id:
        try:
            client, err = ClientManager.get_client(provider_id, timeout=10.0)
            if client and not err:
                resp = client.chat.completions.create(
                    model=model_id,
                    messages=[
                        {
                            "role": "system",
                            "content": "Generate a concise 2 to 5 word title for this conversation based on the user's message. Output ONLY the title text, with no quotes or trailing punctuation.",
                        },
                        {"role": "user", "content": prompt.strip()[:500]},
                    ],
                    max_tokens=20,
                    stream=False,
                )
                if resp and resp.choices and resp.choices[0].message:
                    raw_title = (resp.choices[0].message.content or "").strip()
                    # Strip any <think>...</think> blocks if a reasoning model included them
                    raw_title = re.sub(r"<think>[\s\S]*?</think>", "", raw_title).strip()
                    raw_title = raw_title.split("\n")[0].strip().strip('"\'`.?!:;,-')
                    if raw_title and len(raw_title) >= 2:
                        if len(raw_title) > 42:
                            raw_title = raw_title[:39].rsplit(" ", 1)[0]
                        return raw_title
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
