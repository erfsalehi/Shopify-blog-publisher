"""Prompt template loading.

Stage prompts live as plain text files under ./prompts so they can be edited
without touching code. `load_prompt` caches reads; `brand_voice` returns the
guide injected into the draft stage (empty string if the file is missing).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from blog_pipeline.config import get_settings

_PROMPT_DIR = Path("prompts")


@lru_cache
def load_prompt(name: str) -> str:
    """Read prompts/<name>.md (or .txt). Returns '' if not found."""
    for ext in (".md", ".txt"):
        path = _PROMPT_DIR / f"{name}{ext}"
        if path.exists():
            return path.read_text(encoding="utf-8")
    return ""


def brand_voice() -> str:
    path = Path(get_settings().brand_voice_path)
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""
