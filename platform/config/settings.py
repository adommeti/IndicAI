from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.stack", override=True)

import os  # noqa: E402

from pydantic import BaseModel, Field, field_validator  # noqa: E402
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


class AppBudget(BaseModel):
    """Caps for one app. A field left None inherits the shared default beside it."""

    monthly_inr: float | None = Field(default=None, ge=0)
    session_inr: float | None = Field(default=None, ge=0)
    day_inr: float | None = Field(default=None, ge=0)


#: Per-app caps at the 2x factor PRD B8 mandates ("spend caps are set at 2x the estimate
#: for each app so a bug can't run away"). Only VENDOR spend reaches the ledger, so these
#: are derived from each PRD's *variable* subtotal, not its infra line. The arithmetic is
#: spelled out per app so a reviewer can check it against the PRD instead of trusting a
#: number; FX is the indicative 94.5 INR/USD from `platform/config/pricing.yaml`.
#:
#: Day caps are a tenth of the month, matching the shared default's ratio. That is
#: deliberately loose for UC2 and UC3, whose work is bursty -- UC2 produces a module set
#: in one sitting, UC3 sweeps a night's recordings at 02:00 -- and a cap sized at
#: month/30 would refuse legitimate batches.
APP_BUDGETS: dict[str, AppBudget] = {
    # C13 variable subtotal ~$34/mo -> Rs 3,213; 2x -> Rs 6,426. A session is one
    # conversation, and the shared Rs 250 already bounds a runaway chat loop.
    "uc1": AppBudget(monthly_inr=6_500.0, day_inr=650.0, session_inr=250.0),
    # D12 pilot production ~$170 -> Rs 16,065; 2x -> Rs 32,130. A session is one
    # module-language and dubbing alone is Rs 40/min, so a 20-minute module is Rs 800 of
    # dubbing before a single Claude call -- the shared Rs 250 would refuse every dub.
    "uc2": AppBudget(monthly_inr=32_000.0, day_inr=3_200.0, session_inr=2_000.0),
    # E13 variable subtotal at the Sarvam-transliteration upper bound ~$205/mo ->
    # Rs 19,372; 2x -> Rs 38,745. The PRD recommends local transliteration (~$148), so
    # this caps the more expensive of the two paths. A session is one call: an hour of
    # diarized batch STT is Rs 45, and Claude on top of it is a rounding error.
    "uc3": AppBudget(monthly_inr=39_000.0, day_inr=3_900.0, session_inr=500.0),
}


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
    #: Per-app overrides, merged over `APP_BUDGETS`. Override one value without restating
    #: the rest: `BUDGET__APPS__UC3__DAY_INR=2000`.
    apps: dict[str, AppBudget] = Field(default_factory=lambda: dict(APP_BUDGETS))

    @field_validator("apps", mode="after")
    @classmethod
    def _merge_over_defaults(cls, apps: dict[str, AppBudget]) -> dict[str, AppBudget]:
        """Fill in the apps an override did not mention.

        pydantic REPLACES a dict field wholesale rather than merging into its
        `default_factory`, so setting one documented knob -- `BUDGET__APPS__UC3__DAY_INR`
        -- left `apps` as `{"uc3": ...}` and silently put uc1 and uc2 back on the pooled
        budget this class exists to split, including the Rs 250 session cap that would
        refuse every uc2 dub. The failure was invisible: nothing errors, the caps are just
        the wrong ones. Merging per app AND per field means an override names exactly what
        it changes.
        """
        merged = dict(APP_BUDGETS)
        for app, override in apps.items():
            base = merged.get(app)
            merged[app] = (
                override
                if base is None
                else AppBudget(
                    monthly_inr=(
                        base.monthly_inr if override.monthly_inr is None else override.monthly_inr
                    ),
                    session_inr=(
                        base.session_inr if override.session_inr is None else override.session_inr
                    ),
                    day_inr=base.day_inr if override.day_inr is None else override.day_inr,
                )
            )
        return merged

    def for_app(self, app: str) -> tuple[float, float, float]:
        """`(monthly, session, day)` caps for ``app``, falling back to the shared defaults.

        An unknown or empty app name gets the shared defaults, which is what the eval
        runners, the test suite and any process that has not declared itself want: one
        pooled budget rather than a silent zero-cap that would refuse everything.
        """
        override = self.apps.get(app) if app else None
        if override is None:
            return self.monthly_inr, self.session_inr, self.day_inr
        return (
            self.monthly_inr if override.monthly_inr is None else override.monthly_inr,
            self.session_inr if override.session_inr is None else override.session_inr,
            self.day_inr if override.day_inr is None else override.day_inr,
        )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_nested_delimiter="__", extra="ignore")
    tei_url: str = "http://localhost:8080"
    sparse_url: str = "http://localhost:8081"
    qdrant_url: str = "http://localhost:6333"
    #: Set means the spend ledger is shared across workers; unset keeps it in-process.
    redis_url: str = ""
    #: Which app this process is. One app per container, so process-wide identity is
    #: accurate here; it selects the app's spend caps and namespaces its ledger keys so a
    #: UC3 batch night cannot eat UC1's headroom. Empty (tests, eval runners, a REPL)
    #: keeps the shared pooled budget and the un-namespaced keys.
    #: Aliased because a bare `APP` is too generic a name to claim in a container.
    app: str = Field(default="", validation_alias="INDICAI_APP")
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)


settings = Settings()
