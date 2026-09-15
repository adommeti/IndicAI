"""Roman renderings of native-script transcript text.

Why a Roman copy exists at all: the same Hinglish sentence reaches this pipeline
two ways. Saaras returns it in Devanagari; a chat export or an analyst's note has
it in Roman. A lexicon that only indexes one script misses the other half of the
corpus, so every segment is stored both ways and P3 matches against both.

The native text stays authoritative. Evidence spans quoted to a reviewer come
from `text`, never from `text_roman` -- a transliteration is a search aid, not a
record of what was said.

Two backends, because the offline one is not uniformly good:

- `indic-transliteration` (default): deterministic, free, offline, exact for
  Devanagari. **Weak for Tamil**, where the script writes one letter for the
  k/g pair and the library resolves it the wrong way: `வணக்கம்` comes back as
  `vaṇaghghaṁ` rather than `vaṇakkam`, in every scheme it offers. That is a
  property of script-to-script mapping without phonology, not a bug we can
  configure away -- see `KNOWN_WEAK` and the test that pins it.
- Sarvam (`SARVAM_TRANSLITERATE=true`): costs money per character and needs the
  network, but is phonological, so it gets Tamil right. Recommended for any
  corpus with Tamil in it.
"""

import os
import re
from typing import Any

from indic_transliteration import sanscript
from indic_transliteration.sanscript import transliterate as _transliterate

# Language tag -> the source script its text is written in.
SCRIPTS = {
    "hi-IN": sanscript.DEVANAGARI,
    "mr-IN": sanscript.DEVANAGARI,
    "te-IN": sanscript.TELUGU,
    "ta-IN": sanscript.TAMIL,
    "kn-IN": sanscript.KANNADA,
    "ml-IN": sanscript.MALAYALAM,
    "gu-IN": sanscript.GUJARATI,
    "bn-IN": sanscript.BENGALI,
    "pa-IN": sanscript.GURMUKHI,
    "or-IN": sanscript.ORIYA,
}

# ISO 15919: reversible, and the closest of the offline schemes to how people
# actually write these languages in Roman script.
SCHEME = sanscript.ISO

# Languages where the offline backend is known to produce a wrong romanisation.
# Not disabled -- a wrong-but-consistent string is still a usable search key --
# but recorded on the row and reported, so nobody reads `vaṇaghghaṁ` and
# concludes the STT was bad.
KNOWN_WEAK = frozenset({"ta-IN"})


# A maximal run of Indic-script characters (U+0900 Devanagari through U+0D7F
# Malayalam, covering every script in SCRIPTS): the part of a segment that needs
# converting. Everything else -- Latin, digits, punctuation, spaces -- is passed
# through untouched.
#
# Scoped to these blocks rather than "non-ASCII" on purpose: ISO 15919 output is
# Latin *with diacritics* (`maiṁ`, `karūṁgā`), so a non-ASCII test would call
# this module's own output un-transliterated, and would mangle a Latin word that
# merely carries an accent.
NATIVE_RUN = re.compile(r"[\u0900-\u0D7F\u0D80-\u0DFF]+")


def already_roman(text: str) -> bool:
    """Is there nothing here to transliterate?

    True only when the text contains no non-ASCII characters at all. A
    proportion test would be wrong for this corpus: PRD E3's calls are
    code-mixed, so a segment like `Client ko bolo मैं करूंगा` is majority-Latin
    and still has Devanagari in it that a lexicon needs romanised.
    """
    return not NATIVE_RUN.search(text)


def offline(text: str, language: str) -> tuple[str, str]:
    """Transliterate locally, run by run. Returns (roman, source tag).

    Each non-ASCII run is converted in place and the Latin around it is left
    alone, so a code-mixed segment comes back fully Roman rather than being
    skipped because most of it already was.
    """
    script = SCRIPTS.get(language)
    if script is None or already_roman(text):
        # Nothing to convert: English, Hinglish-in-Roman, or a language whose
        # script we have no mapping for. Echo it rather than inventing one.
        return text, "verbatim"
    converted = NATIVE_RUN.sub(lambda m: _transliterate(m.group(), script, SCHEME), text)
    return converted, f"indic-transliteration:{SCHEME}"


async def via_sarvam(text: str, language: str, client: Any | None = None) -> tuple[str, str]:
    """Transliterate through Sarvam, which is phonological and so handles Tamil.

    Costs Rs 2 per 1K characters and goes over the network; used only when
    `SARVAM_TRANSLITERATE=true`.
    """
    if already_roman(text) or language not in SCRIPTS:
        return text, "verbatim"
    from indic_platform.adapters.sarvam_translate import SarvamTranslate

    sut = client if client is not None else SarvamTranslate()
    roman = await sut.transliterate(text, source=language, target="en-IN")
    return roman, "sarvam:mayura:v1"


def use_sarvam() -> bool:
    return os.environ.get("SARVAM_TRANSLITERATE", "").lower() == "true"


async def to_roman(text: str, language: str, client: Any | None = None) -> tuple[str, str]:
    """The configured backend. Returns (roman text, what produced it)."""
    if use_sarvam():
        return await via_sarvam(text, language, client)
    return offline(text, language)
