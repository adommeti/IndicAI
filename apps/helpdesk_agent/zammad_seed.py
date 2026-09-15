"""Seed the local Zammad with the group and ticket field uc1 files tickets into.

Run once after `make stack-ticketing`, with a token minted in Zammad (see
`.env.example` for how):

    ZAMMAD_URL=http://localhost:8082 ZAMMAD_TOKEN=... \
        uv run python -m helpdesk_agent.zammad_seed

Idempotent: it lists what exists before creating anything, prints which of the
two objects it had to create, and exits 0 either way. Running it twice does not
produce a second "IT Support" group or a duplicate `source_session_id` field.

This is an operator CLI, not a request path. It talks to Zammad with httpx
directly rather than through `indic_platform.adapters`, which exist to wrap the
two model vendors. It sends only the fixed literals below -- no employee text,
no utterances -- so `indic_platform.security.redact` has nothing to act on here.
"""

import argparse
import os
import sys
from typing import Any

import httpx

GROUP = "IT Support"
SOURCE_FIELD = "source_session_id"
# Short read timeout is fine for these two calls; Zammad is local. The first
# request after `make stack-ticketing` can still be slow while Rails warms up,
# hence the connect budget.
TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# A plain text Ticket attribute. uc1 writes the helpdesk session UUID (36 chars)
# into it so a ticket can be traced back to the conversation that filed it.
FIELD: dict[str, Any] = {
    "object": "Ticket",
    "name": SOURCE_FIELD,
    "display": "Source Session ID",
    "data_type": "input",
    "data_option": {
        "type": "text",
        "maxlength": 64,
        "null": True,
        "default": "",
        "translate": False,
    },
    "screens": {
        "create_middle": {"ticket.agent": {"shown": True, "required": False}},
        "edit": {"ticket.agent": {"shown": True, "required": False}},
        "view": {"ticket.agent": {"shown": True}},
    },
    "active": True,
}


def client(url: str, token: str) -> httpx.Client:
    return httpx.Client(
        base_url=url.rstrip("/") + "/api/v1",
        headers={"Authorization": f"Token token={token}"},
        timeout=TIMEOUT,
    )


def _listing(http: httpx.Client, path: str) -> list[dict[str, Any]]:
    response = http.get(path)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise SystemExit(f"GET {path} returned {type(payload).__name__}, expected a list")
    return payload


def ensure_group(http: httpx.Client) -> bool:
    """Create the "IT Support" group unless it is already there. True if created."""
    for group in _listing(http, "/groups"):
        if group.get("name") == GROUP:
            print(f'group "{GROUP}" already exists (id {group.get("id")}) -- not creating it')
            return False
    response = http.post("/groups", json={"name": GROUP, "active": True})
    # Zammad validates group names for uniqueness, so a second seeder racing this
    # one loses here rather than creating a duplicate.
    if response.status_code == 422:
        print(f'group "{GROUP}" was created concurrently -- not creating it ({response.text})')
        return False
    response.raise_for_status()
    print(f'group "{GROUP}" created (id {response.json().get("id")})')
    return True


def ensure_field(http: httpx.Client) -> bool:
    """Create the `source_session_id` Ticket attribute unless present. True if created."""
    for attribute in _listing(http, "/object_manager_attributes"):
        if attribute.get("object") == "Ticket" and attribute.get("name") == SOURCE_FIELD:
            print(
                f'ticket field "{SOURCE_FIELD}" already exists '
                f"(id {attribute.get('id')}) -- not creating it"
            )
            return False
    response = http.post("/object_manager_attributes", json=FIELD)
    # Zammad's own create rejects a duplicate attribute with 422 "already exists".
    if response.status_code == 422:
        print(
            f'ticket field "{SOURCE_FIELD}" was created concurrently '
            f"-- not creating it ({response.text})"
        )
        return False
    response.raise_for_status()
    print(f'ticket field "{SOURCE_FIELD}" created (id {response.json().get("id")})')
    return True


def execute_migrations(http: httpx.Client) -> None:
    """Apply pending object-attribute migrations. A no-op when nothing is pending.

    Run unconditionally so that a run interrupted between create and migrate
    converges on the next run instead of leaving the field unusable.
    """
    response = http.post("/object_manager_attributes_execute_migrations")
    response.raise_for_status()
    print("object attribute migrations applied")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Idempotently seed Zammad with the 'IT Support' group and the "
            "source_session_id ticket field. Reads ZAMMAD_URL and ZAMMAD_TOKEN."
        )
    )
    parser.parse_args(argv)

    url = os.environ.get("ZAMMAD_URL", "").strip()
    token = os.environ.get("ZAMMAD_TOKEN", "").strip()
    missing = [name for name, value in (("ZAMMAD_URL", url), ("ZAMMAD_TOKEN", token)) if not value]
    if missing:
        print(f"set {' and '.join(missing)} in the environment; see .env.example", file=sys.stderr)
        return 2

    with client(url, token) as http:
        created = [ensure_group(http), ensure_field(http)]
        execute_migrations(http)
    print("seed complete" if any(created) else "seed complete -- nothing to change")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
