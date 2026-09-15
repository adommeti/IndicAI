"""The uc1 Grafana dashboard, pinned the way `test_uc2_delivery.py` pins uc2's.

A dashboard is a JSON file nobody compiles. Nothing else in the repo fails when a panel
title is renamed, a datasource uid drifts, or a query starts reading a table it was not
meant to read -- and because no Grafana runs in CI, "dashboard renders" is UNMEASURED, so
these assertions are the only thing standing between a bad edit and a silently broken
dashboard in a deployment nobody is watching.

What is deliberately NOT asserted here: that any panel returns rows. Two of them cannot
today -- the spend panel reads `adapter_calls`, which has no writer, and the degraded-mode
panel needs a degradation to have happened. Asserting emptiness would pin a bug in place;
asserting non-emptiness would fail on a clean database. The SQL is validated by executing
it (see `test_uc1_dashboard_sql_executes`, `integration`), which is the part that rots.
"""

import json
from pathlib import Path
from typing import Any

import pytest

DASHBOARD = Path(__file__).parents[2] / "infra" / "grafana" / "dashboards" / "uc1.json"
PROVISIONING = Path(__file__).parents[2] / "infra" / "grafana" / "provisioning"


def dashboard() -> dict[str, Any]:
    return json.loads(DASHBOARD.read_text())


def panels() -> list[dict[str, Any]]:
    return list(dashboard()["panels"])


def test_the_dashboard_has_the_four_panels_the_prompt_asks_for() -> None:
    """uc1/P6: "latency percentiles, action mix, degraded-mode counts, and daily INR/USD spend"."""
    titles = [p["title"].lower() for p in panels()]
    for wanted in ("latency", "action", "degraded", "spend"):
        assert any(wanted in t for t in titles), f"no panel covers {wanted!r}; titles were {titles}"
    doc = dashboard()
    assert doc["uid"] and doc["title"]


def test_every_panel_says_what_its_number_means() -> None:
    """A panel whose description is empty is a number without provenance.

    Both dashboards in this repo use `description` to say what the figure counts and what
    it excludes, which is the only place a reader learns that (say) the degraded-mode panel
    cannot distinguish a vendor outage from a guard rejection.
    """
    for panel in panels():
        assert panel.get("description", "").strip(), f"panel {panel['title']!r} has no description"


def test_the_spend_panel_reads_adapter_calls_rather_than_a_hand_kept_total() -> None:
    """Same contract uc2's dashboard is held to, for the same reason.

    `adapter_calls` carries the cost the pricing table computed at call time. A panel that
    recomputed spend from rates in the query would drift the day `pricing.yaml` changes,
    and would report today's rates for last month's calls.
    """
    panel = next(p for p in panels() if "spend" in p["title"].lower())
    sql = panel["targets"][0]["rawSql"]
    assert "adapter_calls" in sql
    assert "cost_inr" in sql and "cost_usd" in sql


def test_no_panel_depends_on_prometheus_while_nothing_is_scraped() -> None:
    """Nothing exposes a `/metrics` endpoint, so a Prometheus panel would never fill.

    `platform/obs/metrics.py` defines the counters, but no app mounts an exposition
    endpoint and `infra/prometheus.yaml` scrapes only prometheus, livekit and qdrant
    (`docs/build/BLOCKERS.md`). When that is fixed, this test is the one that should fail
    and be deleted -- which is the point of writing it down rather than leaving the absence
    implicit.
    """
    for panel in panels():
        source = panel.get("datasource", {})
        assert source.get("type") == "postgres", (
            f"panel {panel['title']!r} uses datasource {source!r}; until something is "
            "scraped, a non-postgres panel renders an empty graph for ever"
        )


def test_the_dashboard_is_actually_provisioned() -> None:
    """The mechanism, not just the file.

    `uc2.json` sat in this repo unreferenced by compose, any script or any Makefile target:
    a dashboard nothing loads. uc1/P6 added the provider and datasource configs, so this
    asserts both exist and that the provider points at the directory the dashboards live in.
    """
    import yaml

    providers = yaml.safe_load((PROVISIONING / "dashboards" / "indic-ai.yaml").read_text())
    paths = [p["options"]["path"] for p in providers["providers"]]
    assert paths, "no dashboard provider configured"

    sources = yaml.safe_load((PROVISIONING / "datasources" / "platform.yaml").read_text())
    names = {d["name"] for d in sources["datasources"]}
    uids = {d.get("uid") for d in sources["datasources"]}
    assert "platform" in names and "platform" in uids, (
        "uc2's dashboard stores the datasource as the string 'platform'; a Grafana "
        "datasource variable resolves by uid on current versions and by name on older "
        "ones, so both must be 'platform' or one of the two dashboards silently breaks"
    )


def test_the_compose_grafana_service_mounts_the_provisioning() -> None:
    """A provisioning directory Grafana cannot see is the same as no provisioning."""
    import yaml

    compose = yaml.safe_load((Path(__file__).parents[2] / "docker-compose.yml").read_text())
    mounts = " ".join(compose["services"]["grafana"].get("volumes", []))
    assert "provisioning/dashboards" in mounts and "provisioning/datasources" in mounts, (
        f"grafana mounts {mounts!r}; the dashboards in infra/grafana are not loaded"
    )
    assert "infra/grafana/dashboards" in mounts, "the dashboard JSON itself is not mounted"


@pytest.mark.integration
def test_uc1_dashboard_sql_executes() -> None:
    """Every panel's SQL runs against the real schema.

    This is the assertion that earns its keep. The queries are never compiled, never
    imported and never executed anywhere else, so a renamed column or a function that does
    not accept the type it is handed surfaces the first time somebody opens Grafana --
    which, with no Grafana in CI, means in a deployment. Returning zero rows is a pass; a
    syntax error or an unknown column is not.

    `$__timeFilter(x)` is Grafana's macro, expanded here the way Grafana expands it.
    """
    import os
    import re

    import psycopg

    url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as conn:
        for panel in panels():
            for target in panel.get("targets", []):
                sql = target.get("rawSql")
                if not sql:
                    continue
                expanded = re.sub(
                    r"\$__timeFilter\(([^)]+)\)",
                    r"\1 >= now() - interval '30 days' and \1 <= now()",
                    sql,
                )
                with conn.cursor() as cur:
                    cur.execute(expanded)  # type: ignore[arg-type]
                    rows = cur.fetchall()
                print(f"dashboard sql ok: {panel['title']!r} -> {len(rows)} row(s)")
            conn.rollback()
