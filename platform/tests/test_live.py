import os

import pytest
from indic_platform.adapters.claude import Claude
from indic_platform.adapters.sarvam_translate import SarvamTranslate
from indic_platform.obs.langfuse import LangfuseSink, default_sink
from pydantic import BaseModel


class SmokeAnswer(BaseModel):
    answer: str


@pytest.mark.slow
async def test_sarvam_live_smoke() -> None:
    assert os.getenv("SARVAM_API_KEY"), "SARVAM_API_KEY required"
    answer = await SarvamTranslate().translate("Hello.", source="en-IN", target="hi-IN")
    assert answer.strip()
    sink = default_sink()
    assert isinstance(sink, LangfuseSink)
    sink.flush()


@pytest.mark.slow
async def test_anthropic_live_smoke() -> None:
    assert os.getenv("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY required"
    answer = await Claude().structured(
        system="Return a JSON answer with answer equal to ok.",
        user="Please acknowledge.",
        schema=SmokeAnswer,
        model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5"),
    )
    assert answer.answer.lower() == "ok"
    sink = default_sink()
    assert isinstance(sink, LangfuseSink)
    sink.flush()
