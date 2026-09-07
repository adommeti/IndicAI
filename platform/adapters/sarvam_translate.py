from typing import Any

from indic_platform.adapters.sarvam_common import SarvamAdapter


class SarvamTranslate(SarvamAdapter):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__("translate", **kwargs)

    async def translate(
        self, text: str, *, source: str = "auto", target: str, mode: str = "formal"
    ) -> str:
        clean = self.redact(text)
        response = await self.request(
            self.client.text.translate,
            price_model="mayura:v1",
            units={"characters": len(clean)},
            input=clean,
            source_language_code=source,
            target_language_code=target,
            mode=mode,
            **{"model": "mayura:v1"},
        )
        return str(response.translated_text)
