"""Is this about AI, and which parts of AI? Keyword rules loaded from config/topics.yaml."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class Taxonomy:
    """Regex-based relevance filter and topic tagger.

    ``strong`` terms (``AI``, ``LLM``, ``OpenAI``...) are enough on their own to call an item
    AI-related; ``weak`` terms (``inference``, ``agents``...) need company: two distinct hits.
    """

    strong: list[re.Pattern[str]] = field(default_factory=list)
    weak: list[re.Pattern[str]] = field(default_factory=list)
    topics: dict[str, list[re.Pattern[str]]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Taxonomy:
        def compile_all(patterns: list[str] | None) -> list[re.Pattern[str]]:
            return [re.compile(p, re.IGNORECASE) for p in patterns or []]

        terms = data.get("ai_terms") or {}
        topics = {name: compile_all(pats) for name, pats in (data.get("topics") or {}).items()}
        return cls(
            strong=compile_all(terms.get("strong")),
            weak=compile_all(terms.get("weak")),
            topics=topics,
        )

    @classmethod
    def load(cls, path: Path) -> Taxonomy:
        return cls.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})

    def counts(self, text: str) -> tuple[int, int]:
        """Number of distinct (strong, weak) patterns that match."""
        strong = sum(1 for p in self.strong if p.search(text))
        weak = sum(1 for p in self.weak if p.search(text))
        return strong, weak

    def is_ai(self, title: str, summary: str = "", mode: str = "loose") -> bool:
        """Decide whether an item is about AI.

        ``off`` keeps everything. Otherwise the title decides when it can: one strong term, or two
        weak ones. ``loose`` then also reads the summary, but a passing mention does not count:
        it needs two strong terms, or a strong plus a weak one, or three weak ones.
        """
        if mode == "off":
            return True
        strong, weak = self.counts(title)
        if strong >= 1 or weak >= 2:
            return True
        if mode == "strict":
            return False
        strong, weak = self.counts(f"{title}\n{summary}")
        return strong >= 2 or (strong >= 1 and weak >= 1) or weak >= 3

    def topics_for(self, text: str, limit: int = 4) -> list[str]:
        scored: list[tuple[int, str]] = []
        for name, patterns in self.topics.items():
            hits = sum(1 for p in patterns if p.search(text))
            if hits:
                scored.append((hits, name))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [name for _, name in scored[:limit]]
