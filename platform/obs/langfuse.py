import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import yaml
from indic_platform.config import settings as _settings  # noqa: F401

log = logging.getLogger(__name__)


class Sink(Protocol):
    def emit(self, record: dict[str, Any]) -> None: ...


class MemorySink:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def emit(self, record: dict[str, Any]) -> None:
        self.records.append(dict(record))


@lru_cache
def prices() -> dict[str, Any]:
    return yaml.safe_load((Path(__file__).parents[1] / "config/pricing.yaml").read_text())


def cost(model: str, units: dict[str, float]) -> tuple[float, float]:
    config = prices()
    rate = config["models"][model]
    amount = sum(float(rate.get(unit, 0)) * count for unit, count in units.items())
    if rate["currency"] == "INR":
        return amount, amount / config["fx_inr_per_usd"]
    return amount * config["fx_inr_per_usd"], amount


class LangfuseSink:
    def __init__(self) -> None:
        from langfuse import Langfuse

        self.client = Langfuse(
            public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
            secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
            base_url=os.getenv("LANGFUSE_HOST", "http://localhost:3002"),
        )

    def emit(self, record: dict[str, Any]) -> None:
        # Metadata only: never send raw inputs, outputs, headers, URIs or error messages.
        with self.client.start_as_current_observation(
            name=f"{record['vendor']}.{record['capability']}",
            as_type="generation",
            model=record["model"],
            metadata=record,
            usage_details=record["units"],
            cost_details={"total": record["cost_usd"]},
        ):
            pass

    def flush(self) -> None:
        self.client.flush()


class TeeSink:
    """Fan one adapter record out to several sinks.

    Exists because "record this call's cost locally" and "emit a Langfuse span" are
    two different needs for the same record, and passing a private sink to an
    `AdapterRuntime` *replaces* the default one rather than adding to it. uc3's
    ingestion did exactly that and so emitted no span for any Saaras call, which
    CLAUDE.md lists as a non-negotiable ("every adapter call emits a Langfuse span").

    A failing sink must not take the others down with it, and must never fail the
    vendor call that produced the record: observability is not in the critical path.
    So each sink is attempted and its exception is logged, not raised.
    """

    def __init__(self, *sinks: Sink) -> None:
        self.sinks = tuple(sinks)

    def emit(self, record: dict[str, Any]) -> None:
        for sink in self.sinks:
            try:
                sink.emit(record)
            except Exception:
                log.exception("sink %s failed to emit; continuing", type(sink).__name__)


@lru_cache
def default_sink() -> Sink:
    return LangfuseSink()
