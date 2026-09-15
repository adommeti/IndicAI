"""Default outbound text redaction. Audio is not transcribed by this hook."""

import re

_PATTERNS = [
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"(?<!\w)\d{4}[ -]?\d{4}[ -]?\d{4}(?!\w)"), "[NATIONAL_ID]"),
    (
        re.compile(
            r"(?<!\w)(?:\+?\d{1,3}[ .-]?)?"
            # A ten-digit Indian mobile, however the person happened to group it. The
            # bare run and 3-3-4 and 5-5 were covered from the start; 4-3-3 was not, and
            # 4-3-3 is how people actually type a number into a helpdesk widget, so the
            # commonest spelling was the one reaching Sarvam and Anthropic intact
            # (found in uc1/P7 by `platform/tests/test_uc1_redaction.py`, which captured
            # it on a real wire). Adding it is consistency rather than a wider net: the
            # same ten digits written `9876543210` were already masked, so nothing is
            # newly caught that was not caught before under a different spacing.
            r"(?:\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}"
            r"|\d{4}[ .-]?\d{3}[ .-]?\d{3}"
            r"|\d{5}[ .-]?\d{5})(?!\w)"
        ),
        "[PHONE]",
    ),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
