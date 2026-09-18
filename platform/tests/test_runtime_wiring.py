"""The compose/Prometheus/Grafana wiring that makes the product runnable (program/P12).

No Docker daemon in a cloud session, so `docker compose config` and the image builds are
proven in CI. What is provable here is that the files say what they are supposed to say --
which is where a wiring mistake actually lives: a worker consuming the wrong queue, a
scrape target pointing at a port nothing serves, an alert whose metric no code emits.
"""

import json
from pathlib import Path

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


def test_a_beat_exists_exactly_where_something_is_scheduled() -> None:
    """uc2 gets no beat: it registers no beat_schedule, and a beat with an empty schedule
    is a container that looks like it is doing something and is not."""
    services = compose()["services"]
    assert "uc1-beat" in services and "uc3-beat" in services
    assert "uc2-beat" not in services
    uc2 = (ROOT / "apps" / "training_localizer" / "pipeline.py").read_text()
    assert "beat_schedule" not in uc2, "uc2 now schedules something and needs a beat"


def test_each_worker_consumes_only_its_own_queue() -> None:
    """The regression: all three Celery apps published to Celery's default queue on one
    broker, so a UC3 batch night sat in front of UC1's ticket retries in the same FIFO."""
    services = compose()["services"]
    for app in APPS:
        command = services[f"{app}-worker"]["command"]
        assert "-Q" in command, f"{app}-worker consumes every queue"
        assert command[command.index("-Q") + 1] == app
        # Threads, not prefork: a forked child has its own Prometheus registry, so the
        # process serving WORKER_METRICS_PORT would report zeroes forever.
        assert command[command.index("--pool") + 1] == "threads"


def test_each_app_publishes_to_the_queue_its_worker_consumes() -> None:
    """A queue nothing consumes is just a backlog; a worker on the wrong queue is worse."""
    for app, package in PACKAGES.items():
        module = {"uc1": "ticketing", "uc2": "pipeline", "uc3": "ingest"}[app]
        source = (ROOT / "apps" / package / f"{module}.py").read_text()
        assert f'task_default_queue="{app}"' in source, f"{package} does not publish to {app}"


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
