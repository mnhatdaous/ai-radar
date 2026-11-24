"""Configuration: settings and the source list (config/sources.yaml), plus topic rules."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ai_radar.models import KINDS
from ai_radar.relevance import Taxonomy


class ConfigError(ValueError):
    """The configuration is unusable. The message says what to fix."""


class HostPolicy(BaseModel):
    """Politeness limits for one host."""

    min_interval: float = Field(0.4, ge=0)  # seconds between request starts
    max_concurrency: int = Field(2, ge=1)


class DigestSettings(BaseModel):
    top_n: int = 12
    top_mix: dict[str, int] = Field(
        default_factory=lambda: {"editorial": 6, "community": 3, "builders": 3}
    )
    max_per_section: int = 15
    max_per_source_in_top: int = 2
    summary_chars: int = 220
    group_stories: bool = True  # show coverage of one event once, with the other outlets listed
    story_overlap: float = Field(
        0.5, gt=0.0, le=1.0
    )  # how alike two headlines must be; lower groups more


class Settings(BaseModel):
    user_agent: str = "ai-radar/0.1 (+personal AI news aggregator)"
    timeout: float = 30.0
    retries: int = 2
    backoff_base: float = 1.5
    global_concurrency: int = 16
    source_timeout: float = 240.0
    max_age_days: int = 3  # ignore anything published longer ago than this
    respect_robots: bool = True  # applies to HTML pages; feeds and public APIs are not gated
    summary_chars: int = 400  # stored excerpts stay short on purpose: we link out
    hosts: dict[str, HostPolicy] = Field(default_factory=dict)
    digest: DigestSettings = Field(default_factory=DigestSettings)


class SourceConfig(BaseModel):
    """Common fields. Type-specific options (url, include, queries...) live alongside as extras."""

    model_config = ConfigDict(extra="allow")

    id: str
    name: str | None = None
    type: str
    enabled: bool = True
    kind: str = "news"
    weight: float = Field(0.5, ge=0.0, le=1.5)
    ai_filter: Literal["off", "loose", "strict"] = "off"
    max_items: int = Field(25, ge=1)
    max_age_days: int | None = None

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in KINDS:
            raise ValueError(f"unknown kind {value!r}; expected one of {', '.join(KINDS)}")
        return value

    @field_validator("ai_filter", mode="before")
    @classmethod
    def _yaml_booleans(cls, value: Any) -> Any:
        # PyYAML turns a bare `off` / `on` into booleans.
        if value is False or value is None:
            return "off"
        if value is True:
            return "loose"
        return value

    @property
    def display_name(self) -> str:
        return self.name or self.id

    def opt(self, key: str, default: Any = None) -> Any:
        return (self.model_extra or {}).get(key, default)


@dataclass
class AppConfig:
    settings: Settings
    sources: list[SourceConfig]
    taxonomy: Taxonomy


@dataclass(frozen=True)
class Paths:
    """Where everything lives, relative to the repo root."""

    root: Path

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def db(self) -> Path:
        return self.data_dir / "radar.db"

    @property
    def items_dir(self) -> Path:
        return self.data_dir / "items"

    @property
    def digests_dir(self) -> Path:
        return self.root / "digests"

    @property
    def feeds_dir(self) -> Path:
        return self.root / "feeds"


def resolve_root(value: str | os.PathLike[str] | None = None) -> Path:
    return Path(value or os.environ.get("AI_RADAR_ROOT") or Path.cwd()).expanduser().resolve()


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"missing config file: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc
    return data or {}


def load_config(config_dir: Path) -> AppConfig:
    from ai_radar.sources import REGISTRY  # late import: sources depend on this module

    raw = _read_yaml(config_dir / "sources.yaml")
    try:
        settings = Settings(**(raw.get("settings") or {}))
    except ValidationError as exc:
        raise ConfigError(f"settings: {exc}") from exc

    defaults = raw.get("defaults") or {}
    sources: list[SourceConfig] = []
    seen: set[str] = set()
    for entry in raw.get("sources") or []:
        try:
            source = SourceConfig(**{**defaults, **entry})
        except ValidationError as exc:
            raise ConfigError(f"source {entry.get('id', '?')!r}: {exc}") from exc
        if source.id in seen:
            raise ConfigError(f"duplicate source id {source.id!r}")
        seen.add(source.id)
        cls = REGISTRY.get(source.type)
        if cls is None:
            known = ", ".join(sorted(REGISTRY))
            raise ConfigError(
                f"source {source.id!r}: unknown type {source.type!r} (known: {known})"
            )
        cls.validate(source)
        sources.append(source)

    try:
        taxonomy = Taxonomy.from_dict(_read_yaml(config_dir / "topics.yaml"))
    except re.error as exc:
        raise ConfigError(f"topics.yaml has an invalid regex: {exc}") from exc
    return AppConfig(settings=settings, sources=sources, taxonomy=taxonomy)
