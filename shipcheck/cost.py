"""Token usage and, only when a rate card is given, an estimated cost.

Tokens are the primary measure because they come straight from the runs.
Dollars depend on a rate card the user supplies. The bundled rate card is made
up for the demo, and its label says so wherever a dollar figure appears.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from shipcheck.model import Run, read_json


@dataclass(frozen=True)
class RateCard:
    label: str
    source: str
    as_of: str
    currency: str
    per_tokens: int
    models: dict[str, dict[str, float]]

    @classmethod
    def from_dict(cls, d: dict, fallback_label: str = "UNLABELLED RATES") -> "RateCard":
        return cls(
            label=d.get("label") or fallback_label,
            source=d.get("source", "not stated"),
            as_of=d.get("as_of", "not stated"),
            currency=d.get("currency", "USD"),
            per_tokens=int(d.get("per_tokens", 1_000_000)),
            models={k: {"input": float(v["input"]), "output": float(v["output"])} for k, v in d["models"].items()},
        )

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "source": self.source,
            "as_of": self.as_of,
            "currency": self.currency,
            "per_tokens": self.per_tokens,
            "models": self.models,
        }

    @property
    def caption(self) -> str:
        return f"{self.label}. Source: {self.source} Date: {self.as_of}."


def load_rate_card(path: str | Path | None) -> RateCard | None:
    if not path:
        return None
    return RateCard.from_dict(read_json(path), fallback_label=f"RATES FROM {Path(path).name}")


def usage_totals(runs: Iterable[Run]) -> dict[str, float]:
    runs = list(runs)
    n = len(runs)
    inp = sum(r.usage.input_tokens for r in runs)
    out = sum(r.usage.output_tokens for r in runs)
    per_run = (inp + out) / n if n else 0.0
    return {
        "runs": n,
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": inp + out,
        "input_per_run": inp / n if n else 0.0,
        "output_per_run": out / n if n else 0.0,
        "total_per_run": per_run,
        "total_per_1000_runs": round(per_run * 1000),
    }


def estimate_cost(tokens: dict[str, float], model_label: str, card: RateCard | None) -> dict | None:
    """Estimated cost per run and per 1,000 runs, or None without a usable rate."""
    if card is None or model_label not in card.models:
        return None
    rate = card.models[model_label]
    per_run = (tokens["input_per_run"] * rate["input"] + tokens["output_per_run"] * rate["output"]) / card.per_tokens
    return {
        "currency": card.currency,
        "per_run": per_run,
        "per_1000_runs": round(per_run * 1000, 2),
        "label": card.label,
    }


def relative_change(before: float, after: float) -> float | None:
    if not before:
        return None
    return (after - before) / before


def cost_delta(base_tokens: dict, cand_tokens: dict, base_cost: dict | None, cand_cost: dict | None) -> dict:
    delta = {
        "tokens_per_run_before": base_tokens["total_per_run"],
        "tokens_per_run_after": cand_tokens["total_per_run"],
        "tokens_change": relative_change(base_tokens["total_per_run"], cand_tokens["total_per_run"]),
        "cost_per_1000_before": None,
        "cost_per_1000_after": None,
        "cost_change": None,
    }
    if base_cost and cand_cost:
        delta["cost_per_1000_before"] = base_cost["per_1000_runs"]
        delta["cost_per_1000_after"] = cand_cost["per_1000_runs"]
        delta["cost_change"] = relative_change(base_cost["per_run"], cand_cost["per_run"])
    return delta
