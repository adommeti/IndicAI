from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.stack", override=True)

import os  # noqa: E402

from pydantic import BaseModel, Field  # noqa: E402
from pydantic_settings import BaseSettings, SettingsConfigDict  # noqa: E402

#: Where the Anthropic key is read from, in order of preference.
#:
#: `ANTHROPIC_API_KEY` is the name the Anthropic SDK reads by default and the one every
#: other project uses, so it stays supported and is what CI, a developer's `.env` and
#: the Key Vault wiring in `program/P10` set.
#:
#: It is listed SECOND because it is unusable in one environment that matters: a Claude
#: Code session. There, `ANTHROPIC_API_KEY` is the variable the agent harness itself
#: uses for model auth, the provider is host-managed
#: (`CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST=1`), and a value set on the environment is
#: stripped before the session sees it -- verified by observing `SARVAM_API_KEY` arrive
#: intact while `ANTHROPIC_API_KEY` was absent entirely rather than empty. Every live
#: Anthropic eval was therefore blocked from inside the environment the build runs in,
#: and reported UNMEASURED across uc1/P7, uc1/P6 and uc3/P7.
#:
#: `INDICAI_ANTHROPIC_API_KEY` is project-scoped on purpose. A name like
#: `ANTHROPIC_CUSTOM_KEY` would work today but sits in a namespace Anthropic owns and
#: already populates here with `ANTHROPIC_BASE_URL` and `ANTHROPIC_MODEL`; a prefix
#: nobody else claims cannot be taken away by a future convention.
ANTHROPIC_KEY_NAMES: tuple[str, ...] = ("INDICAI_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")


def anthropic_api_key() -> str | None:
    """The Anthropic key, or None if no supported variable carries one.

    None rather than "" so a caller can tell "not configured" from "configured empty",
    and so `if anthropic_api_key()` reads the way every existing call site already
    gates on the raw variable.
    """
    for name in ANTHROPIC_KEY_NAMES:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def anthropic_key_source() -> str | None:
    """Which variable supplied the key. For diagnostics -- never the value itself."""
    for name in ANTHROPIC_KEY_NAMES:
        if (os.environ.get(name) or "").strip():
            return name
    return None


class RetrievalSettings(BaseModel):
    parallel_translate: bool = False
    translate_deadline_s: float = Field(default=0.250, gt=0, le=0.250)


class BudgetSettings(BaseModel):
    """Vendor spend controls, all amounts in INR (see adapters/budget.py for why INR)."""

    enabled: bool = True
    #: Denominator for the 50/80/100% alerts. Not a cap: crossing it alerts, never refuses.
    monthly_inr: float = Field(default=50_000.0, ge=0)
    #: Hard caps. A call whose projected cost would cross one is refused before the vendor.
    session_inr: float = Field(default=250.0, ge=0)
    day_inr: float = Field(default=5_000.0, ge=0)
    #: Headroom a call with unknowable units (a stream billed on duration) must find free.
    unknown_reserve_inr: float = Field(default=5.0, ge=0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_nested_delimiter="__", extra="ignore")
    tei_url: str = "http://localhost:8080"
    sparse_url: str = "http://localhost:8081"
    qdrant_url: str = "http://localhost:6333"
    #: Set means the spend ledger is shared across workers; unset keeps it in-process.
    redis_url: str = ""
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)


settings = Settings()
