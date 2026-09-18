"""The compose/Prometheus/Grafana wiring that makes the product runnable (program/P12).

No Docker daemon in a cloud session, so `docker compose config` and the image builds are
proven in CI. What is provable here is that the files say what they are supposed to say --
which is where a wiring mistake actually lives: a worker consuming the wrong queue, a
scrape target pointing at a port nothing serves, an alert whose metric no code emits.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
APPS = ("uc1", "uc2", "uc3")
PACKAGES = {"uc1": "helpdesk_agent", "uc2": "training_localizer", "uc3": "comms_surveillance"}


def compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def alerts() -> dict:
    return yaml.safe_load((ROOT / "infra" / "alerts.yaml").read_text())


def prometheus() -> dict:
    return yaml.safe_load((ROOT / "infra" / "prometheus.yaml").read_text())


def test_every_app_has_an_api_and_a_worker() -> None:
    services = compose()["services"]
    for app in APPS:
        assert f"{app}-api" in services, f"{app} has no API service"
        assert f"{app}-worker" in services, f"{app} has 16 tasks and nothing to run them"


def worker_module(app: str) -> str:
    """The module each worker's `-A` names, read off the compose command."""
    command = compose()["services"][f"{app}-worker"]["command"]
    return command[command.index("-A") + 1]


def beat_module(app: str) -> str:
    command = compose()["services"][f"{app}-beat"]["command"]
    return command[command.index("-A") + 1]


def registry(module: str) -> dict[str, list[str]]:
    """Import `module` in a CLEAN interpreter and report what a worker would see.

    A subprocess, deliberately. The in-process version of this check passed while the
    uc1 defect was live: pytest had already imported `helpdesk_agent.retention` through
    another test module, which registers the task on the shared app object no matter what
    `include` says. Only a fresh interpreter that imports exactly what `-A` names
    reproduces what the worker actually loads.
    """
    code = (
        "import json,importlib;"
        f"m=importlib.import_module({module!r});"
        "a=m.celery_app;a.loader.import_default_modules();"
        "print(json.dumps({'tasks':sorted(a.tasks),"
        "'beat':[e['task'] for e in (a.conf.beat_schedule or {}).values()],"
        "'queue':a.conf.task_default_queue}))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=True
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_beat_exists_exactly_where_something_is_scheduled() -> None:
    """uc2 gets no beat: it registers no beat_schedule, and a beat with an empty schedule
    is a container that looks like it is doing something and is not."""
    services = compose()["services"]
    assert "uc1-beat" in services and "uc3-beat" in services
    assert "uc2-beat" not in services
    assert not registry("training_localizer.pipeline")["beat"], (
        "uc2 now schedules something and needs a beat"
    )


@pytest.mark.parametrize("app", ["uc1", "uc3"])
def test_every_scheduled_task_is_registered_on_the_worker_that_runs_it(app: str) -> None:
    """The defect this exists for: uc1's Celery app had no `include` for its retention
    module, so `-A helpdesk_agent.ticketing` registered only `uc1.file_ticket`. uc1-beat
    published `uc1.retention_sweep` to queue `uc1` every night and uc1-worker discarded it
    as unregistered -- retention, the headline thing that giving the apps a worker was
    supposed to make run at all, never ran. Nothing errored; the task simply vanished.
    """
    scheduled = set(registry(beat_module(app))["beat"])
    assert scheduled, f"{app}-beat schedules nothing"
    runnable = set(registry(worker_module(app))["tasks"])
    assert scheduled <= runnable, (
        f"{app}-beat schedules {scheduled - runnable}, which {app}-worker cannot run"
    )


@pytest.mark.parametrize("app", ["uc1", "uc2", "uc3"])
def test_each_app_publishes_to_the_queue_its_worker_consumes(app: str) -> None:
    assert registry(worker_module(app))["queue"] == app


def test_prometheus_scrapes_the_apps_and_the_workers() -> None:
    """Workers matter as much as APIs: a spend refusal inside a task and the nightly uc3
    chain verification are recorded in a worker and nowhere else."""
    targets = {
        target
        for job in prometheus()["scrape_configs"]
        for entry in job.get("static_configs", [])
        for target in entry["targets"]
    }
    for app in APPS:
        assert f"{app}-api:8000" in targets
        assert f"{app}-worker:9100" in targets
    assert not any("beat" in t for t in targets), (
        "beat emits beat_init, not celeryd_init, so it serves no metrics endpoint and a "
        "permanently-down target trains people to ignore alerts"
    )


def test_prometheus_loads_the_alert_rules_and_compose_mounts_them() -> None:
    assert "/etc/prometheus/alerts.yaml" in prometheus()["rule_files"]
    volumes = compose()["services"]["prometheus"]["volumes"]
    assert any("infra/alerts.yaml" in v for v in volumes), "rules are configured but not mounted"


def test_every_alert_fires_on_a_metric_something_actually_emits() -> None:
    """An alert on a metric no code writes never fires, and looks like health forever."""
    from indic_platform.obs import metrics as platform_metrics

    # The live objects, not a regex over the source: multi-line definitions are the norm
    # in that module and a text scan silently missed every one of them.
    names = {value._name for value in vars(platform_metrics).values() if hasattr(value, "_name")}
    assert "uc3_audit_chain_breaks" in names, "metric names were not collected"
    rules = [rule for group in alerts()["groups"] for rule in group["rules"]]
    assert rules
    for rule in rules:
        expr = rule["expr"]
        if "up{" in expr:  # Prometheus' own target-health metric
            continue
        assert any(name in expr for name in names), f"{rule['alert']} alerts on nothing: {expr}"


def test_the_audit_chain_break_has_an_alert_because_e8_asks_for_one() -> None:
    """E8: "a nightly job re-walks the chains and alerts on any break." It reached
    Langfuse only, which is a tracing backend nobody is paged by."""
    rules = {r["alert"]: r for group in alerts()["groups"] for r in group["rules"]}
    assert "AuditChainBroken" in rules
    assert rules["AuditChainBroken"]["labels"]["severity"] == "critical"
    # And the failure that rule cannot see: a gauge stuck at 0 by a dead beat.
    assert "AuditChainVerificationStale" in rules
    source = (ROOT / "apps" / "comms_surveillance" / "audit.py").read_text()
    assert "AUDIT_CHAIN_BREAKS.set" in source and "AUDIT_CHAIN_LAST_VERIFIED.set" in source


def test_each_app_has_a_dashboard() -> None:
    for app in APPS:
        path = ROOT / "infra" / "grafana" / "dashboards" / f"{app}.json"
        assert path.is_file(), f"{app} has no dashboard"
        dashboard = json.loads(path.read_text())
        assert dashboard["panels"], f"{app} dashboard has no panels"


def test_the_image_builds_every_app_from_one_definition() -> None:
    dockerfile = (ROOT / "infra" / "docker" / "Dockerfile.app").read_text()
    assert "USER indic" in dockerfile, "the app must not run as root"
    assert "uv sync --frozen" in dockerfile, "the lock is what pins what ships"
    services = compose()["services"]
    for app, package in PACKAGES.items():
        assert services[f"{app}-api"]["build"]["args"]["APP"] == package


def test_the_build_context_excludes_local_secrets() -> None:
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    for secret in (".env", ".env.stack"):
        assert secret in ignored, f"{secret} would be copied into an image layer"
