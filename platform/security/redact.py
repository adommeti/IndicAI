"""Default outbound text redaction. Audio is not transcribed by this hook."""

import re

_PATTERNS = [
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"(?<!\w)\d{4}[ -]?\d{4}[ -]?\d{4}(?!\w)"), "[NATIONAL_ID]"),
    (
        re.compile(
            r"(?<!\w)(?:\+?\d{1,3}[ .-]?)?"
            r"(?:\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}|\d{5}[ .-]?\d{5})(?!\w)"
        ),
        "[PHONE]",
    ),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
