import re


def generate_chat_title(prompt: str) -> str:
    """
    Derives a clean, concise, human-readable title from the initial user prompt.
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
