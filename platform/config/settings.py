from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.stack", override=True)

from pydantic import BaseModel, Field  # noqa: E402
from pydantic_settings import BaseSettings, SettingsConfigDict  # noqa: E402


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
