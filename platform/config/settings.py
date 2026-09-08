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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_nested_delimiter="__", extra="ignore")
    tei_url: str = "http://localhost:8080"
    sparse_url: str = "http://localhost:8081"
    qdrant_url: str = "http://localhost:6333"
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)


settings = Settings()
