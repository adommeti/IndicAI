"""Drop and recreate the platform schema, so `make demo` can start the data over.

Why a schema reset and not a volume drop: the repository's guard refuses `docker
compose down -v`, and rightly -- the `tei-data` volume holds a model download that
takes a quarter of an hour, and nothing about "I want an empty queue" should cost that.
Why a schema reset and not a truncate: uc3's three chained tables are append-only, and
the application role holds no DELETE on them by design (migration 0008). Dropping the
schema as the OWNER role -- the one `make migrate` already uses -- is the deliberate,
whole-database act that design leaves open, and it does not weaken the application
role by one grant.

Refuses to run anywhere but a known non-production environment, the same allow-list
the dev bypass and the seeders use. `make demo-reset` runs this and then
`alembic upgrade head`.
"""

import os
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

# The apps' own allow-list, copied rather than imported: a script under scripts/ is not
# part of any app and must not pull one in to read one constant.
NON_PROD_ENVS = frozenset({"dev", "local", "test", "ci"})

# And the database has to be a local one. The ENV check alone would let `ENV=ci` plus a
# DATABASE_URL pointing at a shared database through; `settings` normally forces the
# local URL from .env.stack, but a guard that relies on a side effect of the settings
# loader is not a guard. `postgres` is the compose service name, for a run from inside
# a container.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres"})


def main() -> int:
    # Loads .env and .env.stack the way every host-side tool here does.
    from indic_platform.config import settings as _settings  # noqa: F401

    env = os.environ.get("ENV", "").strip().lower()
    if env not in NON_PROD_ENVS:
        allowed = sorted(NON_PROD_ENVS)
        print(f"refusing to drop the schema with ENV={env!r}; set ENV to one of {allowed}")
        return 2
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set; run `make bootstrap` (it lands in .env.stack)")
        return 2
    # Alembic's sync driver: this is a one-shot administrative statement, not app code.
    sync_url = make_url(url).set(drivername="postgresql+psycopg")
    host = (sync_url.host or "").strip().lower()
    if host not in LOCAL_HOSTS:
        print(
            f"refusing to drop the schema on {host!r}: this script resets a local stack "
            f"and runs only against {sorted(LOCAL_HOSTS)}"
        )
        return 2
    engine = create_engine(sync_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            owner = connection.execute(text("select current_user")).scalar_one()
            connection.execute(text("drop schema public cascade"))
            connection.execute(text("create schema public"))
            connection.execute(text(f'grant all on schema public to "{owner}"'))
            connection.execute(text("grant usage on schema public to public"))
    finally:
        engine.dispose()
    print(f"schema public dropped and recreated on {sync_url.render_as_string()} as {owner}")
    print("now: uv run alembic upgrade head")
    return 0


if __name__ == "__main__":
    sys.exit(main())
