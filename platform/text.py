"""Text predicates shared by the pipeline and the evaluation that scores it."""

import re


def mentions(term: str, text: str) -> bool:
    """Whole-word, case-insensitive presence of an English term.

    This lives here, rather than in either caller, because the UC2 pipeline must
    *enforce* exactly the notion of "the term is present" that the UC2 eval
    *scores*. Two copies of that rule would drift apart silently and the
    terminology gate would start measuring something other than what the
    enforcer guarantees.

    Case-insensitive because a term that opens a sentence is capitalised in the
    source and still counts as kept in English; whole-word so `policymaker` does
    not satisfy an obligation about `policy`.
    """
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE) is not None
