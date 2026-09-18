# syntax=docker/dockerfile:1.7
# One image definition for all three apps, selected with `--build-arg APP=<package>`:
# helpdesk_agent | training_localizer | comms_surveillance. The build arg is the Python
# package name AND the directory under apps/, which is why one file can serve all three.
#
# Built with `make images` or per app with `make image-uc1`; CI builds all three on every
# PR so a broken image is caught without a Docker daemon in the agent's session.

# ---- ui -----------------------------------------------------------------------------
# Every app owns a Vite bundle and `dist/` is gitignored, so a fresh clone has no UI and
# the image has to build one. Its own stage: the ~400MB of node_modules must not reach
# the runtime image.
FROM node:22-slim AS ui
ARG APP
WORKDIR /ui
# Manifests first so a source-only change does not re-resolve the dependency tree.
COPY apps/${APP}/ui/package.json apps/${APP}/ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/${APP}/ui/ ./
RUN npm run build \
  # All three vite configs build with sourcemap: true, which is right for local
  # development and wrong for the image: the .map files sit next to the bundle and
  # are served unauthenticated, handing any reader the original TypeScript. Dropped
  # here rather than in three vite configs so one rule covers every app and nobody
  # loses sourcemaps on their own machine.
  && find dist -name "*.map" -delete

# ---- python dependencies ------------------------------------------------------------
FROM python:3.12-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv
WORKDIR /srv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
# The workspace manifests alone, so the dependency layer is cached until the lock moves.
# `--all-packages` because the apps share `platform/` and the lock resolves them together;
# per-app pruning would need four lockfiles to stay honest about what shipped.
COPY pyproject.toml uv.lock ./
COPY platform/pyproject.toml platform/pyproject.toml
COPY apps/helpdesk_agent/pyproject.toml apps/helpdesk_agent/pyproject.toml
COPY apps/training_localizer/pyproject.toml apps/training_localizer/pyproject.toml
COPY apps/comms_surveillance/pyproject.toml apps/comms_surveillance/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --all-packages --no-install-workspace --no-dev

# ---- runtime ------------------------------------------------------------------------
FROM python:3.12-slim AS runtime
ARG APP
# The SHORT name (uc1|uc2|uc3), not the package name. These are two different
# namespaces and conflating them is silent: `APP_BUDGETS` is keyed uc1/uc2/uc3, so an
# image that exported INDICAI_APP=helpdesk_agent fell through `for_app()` to the pooled
# defaults and every per-app spend cap was inert in every container -- uc1 running on a
# Rs 5,000 day cap instead of Rs 650, uc2 on the Rs 250 session cap its own comment says
# would refuse every dub. Nothing failed; the caps were simply never the ones intended.
ARG APP_KEY
ARG GIT_SHA=""
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv
WORKDIR /srv

# Non-root, and the account owns nothing it does not need to write.
RUN useradd --system --create-home --uid 10001 indic

ENV PATH=/srv/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    INDICAI_APP=${APP_KEY} \
    INDICAI_GIT_SHA=${GIT_SHA} \
    APP_MODULE=${APP}

COPY --from=deps /srv/.venv /srv/.venv
COPY pyproject.toml uv.lock ./
COPY platform/ platform/
COPY apps/ apps/
# The bundle the ui stage built, where `<app>/api.py` looks for it.
COPY --from=ui /ui/dist apps/${APP}/ui/dist
# Workspace packages only: dependencies are already in the venv copied above.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --all-packages --no-dev && chown -R indic:indic /srv

USER indic
EXPOSE 8000

# `/health` is deliberately cheap -- no database, no broker, no vendor -- so a dependency
# blip cannot restart a healthy container. See `platform/serving.py`.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Shell form so ${APP_MODULE} expands at run time; the worker and beat services override
# this with their own commands in docker-compose.yml.
CMD uvicorn ${APP_MODULE}.api:app --host 0.0.0.0 --port 8000
