from pathlib import Path

import pytest

from ai_radar.config import ConfigError, load_config
from ai_radar.models import KINDS
from ai_radar.sources import REGISTRY
from helpers import ROOT

TOPICS = "ai_terms:\n  strong: ['openai']\ntopics:\n  llm: ['llm']\n"


def write_config(tmp_path: Path, sources: str, topics: str = TOPICS) -> Path:
    (tmp_path / "sources.yaml").write_text(sources, encoding="utf-8")
    (tmp_path / "topics.yaml").write_text(topics, encoding="utf-8")
    return tmp_path


# ---- the configuration that ships with the repo ------------------------------------------


def test_the_shipped_configuration_is_valid() -> None:
    cfg = load_config(ROOT / "config")
    enabled = [s for s in cfg.sources if s.enabled]
    assert len(enabled) >= 60
    assert len({s.id for s in cfg.sources}) == len(cfg.sources)
    assert {s.type for s in cfg.sources} <= set(REGISTRY)
    assert all(s.kind in KINDS and 0 < s.weight <= 1.0 for s in cfg.sources)
    assert sum(cfg.settings.digest.top_mix.values()) == cfg.settings.digest.top_n
    for host, policy in cfg.settings.hosts.items():
        assert policy.min_interval >= 0 and policy.max_concurrency >= 1, host


def test_every_source_type_is_used_by_the_shipped_configuration() -> None:
    used = {s.type for s in load_config(ROOT / "config").sources}
    assert used == set(REGISTRY)


def test_the_shipped_relevance_rules_compile_and_have_content() -> None:
    taxonomy = load_config(ROOT / "config").taxonomy
    assert len(taxonomy.strong) > 20 and len(taxonomy.weak) > 10
    assert {"agents", "llm", "safety", "policy", "business"} <= set(taxonomy.topics)


# ---- errors people will actually make ----------------------------------------------------


@pytest.mark.parametrize(
    ("sources", "message"),
    [
        (
            "sources:\n  - {id: a, type: rss, url: x}\n  - {id: a, type: rss, url: y}\n",
            "duplicate source id",
        ),
        ("sources:\n  - {id: a, type: nope}\n", "unknown type 'nope'"),
        ("sources:\n  - {id: a, type: rss, url: x, kind: gossip}\n", "unknown kind"),
        ("sources:\n  - {id: a, type: rss}\n", "needs: url"),
        ("sources:\n  - {id: a, type: rss, url: x, flavor: myspace}\n", "flavor"),
        ("sources:\n  - {id: a, type: sitemap, url: x, include: '('}\n", "bad regex in include"),
        (
            "sources:\n  - {id: a, type: html_list, url: x, link_regex: '['}\n",
            "bad regex in link_regex",
        ),
        ("sources:\n  - {id: a, type: huggingface, mode: videos}\n", "mode must be"),
        ("sources:\n  - {id: a, type: github_search}\n", "needs `topics` or `queries`"),
        ("sources:\n  - {id: a, type: rss, url: x, weight: 9}\n", "weight"),
        ("sources:\n  - {id: a, type: rss, url: x, ai_filter: sometimes}\n", "ai_filter"),
        ("sources:\n  - {type: rss, url: x}\n", "id"),
        ("settings:\n  retries: many\nsources: []\n", "settings"),
    ],
)
def test_configuration_mistakes_are_reported_clearly(
    tmp_path: Path, sources: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tmp_path, sources))


def test_missing_and_broken_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="missing config file"):
        load_config(tmp_path)
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(write_config(tmp_path, "sources: [unclosed\n"))
    with pytest.raises(ConfigError, match="invalid regex"):
        load_config(write_config(tmp_path, "sources: []\n", topics="ai_terms:\n  strong: ['(']\n"))


def test_yaml_booleans_are_understood_as_filter_modes(tmp_path: Path) -> None:
    sources = (
        "sources:\n"
        "  - {id: a, type: rss, url: x, ai_filter: true}\n"
        "  - {id: b, type: rss, url: x, ai_filter: false}\n"
        "  - {id: c, type: rss, url: x, ai_filter: off}\n"
        "  - {id: d, type: rss, url: x, ai_filter: strict}\n"
        "  - {id: e, type: rss, url: x}\n"
    )
    modes = {s.id: s.ai_filter for s in load_config(write_config(tmp_path, sources)).sources}
    assert modes == {"a": "loose", "b": "off", "c": "off", "d": "strict", "e": "off"}


def test_defaults_apply_to_every_source_unless_overridden(tmp_path: Path) -> None:
    sources = (
        "defaults: {kind: blog, weight: 0.8, max_items: 7}\n"
        "sources:\n  - {id: a, type: rss, url: x}\n  - {id: b, type: rss, url: x, kind: news, max_items: 3}\n"
    )
    a, b = load_config(write_config(tmp_path, sources)).sources
    assert (a.kind, a.weight, a.max_items) == ("blog", 0.8, 7)
    assert (b.kind, b.weight, b.max_items) == ("news", 0.8, 3)


def test_story_grouping_settings_have_sane_defaults_and_bounds(tmp_path: Path) -> None:
    digest = load_config(ROOT / "config").settings.digest
    assert digest.group_stories is True
    assert 0 < digest.story_overlap <= 1
    for bad in ("0", "1.5", "-0.2"):
        text = f"settings:\n  digest: {{story_overlap: {bad}}}\nsources: []\n"
        with pytest.raises(ConfigError, match="story_overlap"):
            load_config(write_config(tmp_path, text))
