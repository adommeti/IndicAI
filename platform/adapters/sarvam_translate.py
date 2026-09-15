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

    async def transliterate(
        self, text: str, *, source: str, target: str = "en-IN", numerals: str = "international"
    ) -> str:
        """Render text from its native script into Roman script.

        Script conversion, not translation: the words stay the same, only the
        writing system changes. Sarvam does this phonologically, which is why it
        is worth paying for on Tamil -- the script writes one letter for the k/g
        pair and an offline script-to-script mapping has no way to choose.

        Billed per character against `mayura:v1`, the per-character text rate.
        Sarvam does not publish a separate transliteration rate; if one appears,
        `platform/config/pricing.yaml` is where it goes.
        """
        clean = self.redact(text)
        response = await self.request(
            self.client.text.transliterate,
            price_model="mayura:v1",
            units={"characters": len(clean)},
            input=clean,
            source_language_code=source,
            target_language_code=target,
            numerals_format=numerals,
        )
        return str(response.transliterated_text)
