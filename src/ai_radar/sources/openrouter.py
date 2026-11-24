"""OpenRouter's public model list: a reliable signal that a new model became available."""

from __future__ import annotations

from typing import Any

from ai_radar.models import RawItem
from ai_radar.sources.base import Source, register
from ai_radar.text import clean_html, clean_title, parse_datetime, truncate

API = "https://openrouter.ai/api/v1/models"


def _per_million(value: Any) -> float | None:
    try:
        return float(value) * 1_000_000
    except (TypeError, ValueError):
        return None


@register
class OpenRouterSource(Source):
    type = "openrouter"

    async def fetch(self) -> list[RawItem]:
        data = await self.http.get_json(API)
        cutoff = self.ctx.cutoff(self.cfg)
        items: list[RawItem] = []
        for model in data.get("data", []):
            created = parse_datetime(model.get("created"))
            model_id = model.get("id")
            if not created or created < cutoff or not model_id:
                continue
            pricing = model.get("pricing") or {}
            price_in, price_out = (
                _per_million(pricing.get("prompt")),
                _per_million(pricing.get("completion")),
            )
            facts = []
            if model.get("context_length"):
                facts.append(f"{int(model['context_length']):,} token context")
            if price_in is not None and price_out is not None:
                facts.append(
                    "free"
                    if price_in == price_out == 0
                    else f"${price_in:.2f}/${price_out:.2f} per M tokens"
                )
            modality = (model.get("architecture") or {}).get("modality")
            description = truncate(clean_html(model.get("description")), 200)
            summary = " · ".join(p for p in (description, ", ".join(facts)) if p)
            items.append(
                RawItem(
                    url=f"https://openrouter.ai/{model_id}",
                    title=clean_title(model.get("name") or model_id),
                    summary=truncate(summary, self.ctx.settings.summary_chars),
                    author=model_id.split("/")[0],
                    published_at=created,
                    metrics={"context_length": float(model.get("context_length") or 0)},
                    tags=[modality] if modality else [],
                )
            )
        items.sort(key=lambda r: r.published_at or created, reverse=True)
        return items
