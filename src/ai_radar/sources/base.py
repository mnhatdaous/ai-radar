"""Source plugin interface, the shared crawl context and the type registry."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, ClassVar

from ai_radar.config import ConfigError, Settings, SourceConfig
from ai_radar.http import HttpClient
from ai_radar.models import RawItem

REGISTRY: dict[str, type[Source]] = {}


def register(cls: type[Source]) -> type[Source]:
    REGISTRY[cls.type] = cls
    return cls


@dataclass
class CrawlContext:
    """Everything a source may need during one run."""

    http: HttpClient
    settings: Settings
    now: datetime
    known_ids: set[str] = field(default_factory=set)  # lets page-fetching sources skip known URLs
    github_token: str | None = None
    max_age_override: int | None = None

    def cutoff(self, cfg: SourceConfig) -> datetime:
        days = self.max_age_override or cfg.max_age_days or self.settings.max_age_days
        return self.now - timedelta(days=days)


class Source(abc.ABC):
    """One kind of place to crawl. Subclass, set ``type``, decorate with ``@register``."""

    type: ClassVar[str]
    required: ClassVar[tuple[str, ...]] = ()  # option keys that must be present in the YAML

    def __init__(self, cfg: SourceConfig, ctx: CrawlContext) -> None:
        self.cfg = cfg
        self.ctx = ctx
        self.http = ctx.http
        self.warnings: list[str] = []

    @classmethod
    def validate(cls, cfg: SourceConfig) -> None:
        missing = [key for key in cls.required if cfg.opt(key) in (None, "", [])]
        if missing:
            raise ConfigError(f"source {cfg.id!r} (type {cls.type}) needs: {', '.join(missing)}")

    def opt(self, key: str, default: Any = None) -> Any:
        return self.cfg.opt(key, default)

    def warn(self, message: str) -> None:
        """Record a non-fatal problem (one feed of many failed, one page was blocked...)."""
        if len(self.warnings) < 8:
            self.warnings.append(message)

    @abc.abstractmethod
    async def fetch(self) -> list[RawItem]:
        """Return candidate items, preferred first. Raise FetchError if the source is unusable."""
