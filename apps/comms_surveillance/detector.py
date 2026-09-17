"""The three-stage hybrid detector (PRD E5, E6, E9).

    Stage 0  lexicon          deterministic, every call, free
    Stage 1  Haiku triage     every call, cheap
    combine  high lexicon hit OR risk_score >= theta OR the daily QA sample
    Stage 2  Sonnet analysis  escalated calls only, no tools, JSON only
    verify   evidence exact, schema valid, canary absent -- or the flag is dropped

Two things this module is careful about, because both are load-bearing for the
whole use case:

**The transcript is data, never instructions.** It is wrapped in `<transcript>`
tags and escaped so it cannot close its own tag, both system prompts say so, and
neither call is given tools -- there is no channel to exfiltrate through even if
a model were talked into trying. An attempt to instruct the system is not
silently ignored: it becomes an `instruction_like_content` flag, *added to*
whatever else the call contains. Suppressing the other findings is precisely
what the attacker wants, so `verify` never lets an injection flag remove one.

**A flag nobody can trace back to a line in the call is not reviewable.** Every
evidence span is checked to be an exact substring of the transcript the model
was shown. Spans that fail are dropped and counted, never repaired: a verifier
that "fixes" a near-miss is a verifier that launders a hallucination.

Claude never reaches a verdict here. Stage 2 produces candidate findings and a
person decides (E1).
"""

import hashlib
import logging
import os
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from indic_platform.adapters import budget
from indic_platform.security.harden import new_canary, unwrap_quoted, wrap_untrusted
from pydantic import BaseModel, Field

from comms_surveillance.lexicon import matcher

log = logging.getLogger(__name__)

APP = Path(__file__).parent
PROMPTS = APP / "prompts"
POLICY = APP / "policy.md"

CATEGORIES = (*matcher.CATEGORIES, "instruction_like_content")
SEVERITIES = ("low", "medium", "high")

TRIAGE_MODEL = "claude-haiku-4-5"
ANALYSIS_MODEL = "claude-sonnet-5"


# --- settings ------------------------------------------------------------------


@dataclass(frozen=True)
class DetectorSettings:
    """Everything that changes what gets escalated, in one place.

    `theta` and `qa_sample_rate` are recorded on every `analysis_runs` row (E7)
    because a flag is only interpretable against the threshold that produced it.
    """

    # PRD E5 and ADR 0005 both say start at 60.
    theta: int = 60
    qa_sample_rate: float = 0.05
    # A per-*deployment* secret (PRD E9), from the environment. It sits in line
    # three of the Sonnet system prompt, so a value that changed per process
    # would invalidate the cached prefix carrying the whole policy document on
    # every worker and every restart -- the caching non-negotiable, and real
    # money. It would also make a stored response impossible to re-verify
    # against the canary that produced it. A generated fallback keeps tests and
    # a first run working without configuration.
    canary: str = field(default_factory=lambda: os.environ.get("UC3_CANARY") or new_canary())


SETTINGS = DetectorSettings()


# --- prompts -------------------------------------------------------------------


@lru_cache(maxsize=8)
def prompt_body(path: Path) -> str:
    """The prompt text without its YAML front matter.

    Cached, like the policy reads below: `analyse` runs once per call and a
    nightly batch is hundreds of calls. Re-reading four files inside an async
    function each time is blocking I/O on the event loop and pointless work.
    """
    return re.sub(r"^---\n.*?\n---\n", "", path.read_text(), flags=re.S)


@lru_cache(maxsize=8)
def prompt_version(path: Path) -> str:
    """`prompt_version` = sha256[:12] of the file content (`.claude/rules/apps.md`)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


@lru_cache(maxsize=1)
def policy_version() -> str:
    return hashlib.sha256(POLICY.read_bytes()).hexdigest()[:12]


@lru_cache(maxsize=1)
def policy_document() -> str:
    return POLICY.read_text()


@lru_cache(maxsize=1)
def policy_summary() -> str:
    """The category names and one-line definitions, for the Haiku system prompt.

    Haiku gets the summary rather than the whole policy: it is scoring, not
    deciding, and the full document is several thousand tokens on every call.
    """
    lines = []
    for block in policy_document().split("\n## ")[1:]:
        name, _, rest = block.partition("\n")
        conduct = rest.split("**Conduct.**")
        if len(conduct) > 1:
            text = conduct[1].split("**")[0].strip().replace("\n", " ")
            lines.append(f"{name.strip()}: {text}")
    return " | ".join(lines)


# --- schemas -------------------------------------------------------------------


class Triage(BaseModel):
    risk_score: int = Field(ge=0, le=100)
    candidate_categories: list[str] = Field(default_factory=list)


class AnalysisFlag(BaseModel):
    category: str
    severity: str
    speaker: str = ""
    start_ms: int = 0
    evidence_span: str
    english_rendering: str = ""
    reasoning: str = ""


class Flags(BaseModel):
    flags: list[AnalysisFlag] = Field(default_factory=list)


# --- combine -------------------------------------------------------------------


@dataclass(frozen=True)
class Escalation:
    """Why a call went to Stage 2 -- or did not. Recorded, so a reviewer can ask."""

    escalate: bool
    reasons: tuple[str, ...]
    risk_score: int
    lexicon_severity: str | None


def qa_sampled(call_id: str, *, rate: float, day: str | None = None) -> bool:
    """Is this call in today's random QA sample?

    PRD E5 adds a random sample of *un-flagged* calls so false negatives are
    measured rather than assumed. Seeded from the UTC date and the call id, so
    a re-run on the same day makes the same choices -- an eval whose sample
    reshuffles between runs is an eval whose numbers cannot be compared.
    """
    if rate <= 0:
        return False
    stamp = day or datetime.now(UTC).strftime("%Y-%m-%d")
    seed = hashlib.sha256(f"{stamp}:{call_id}".encode()).hexdigest()
    return random.Random(seed).random() < rate


def combine(
    call_id: str,
    hits: list[matcher.Hit],
    triage: Triage,
    settings: DetectorSettings = SETTINGS,
    *,
    day: str | None = None,
) -> Escalation:
    """E5's combine rule, with every reason it fired recorded rather than just
    the fact that it did."""
    severity = matcher.highest_severity(hits)
    reasons = []
    if severity == "high":
        reasons.append("lexicon:high")
    if triage.risk_score >= settings.theta:
        reasons.append(f"triage:{triage.risk_score}>=theta:{settings.theta}")
    # Only a call nothing else escalated. E5's sample exists to estimate false
    # negatives, and anyone later filtering on `qa_sample` to build that
    # estimate would get a denominator contaminated with calls that were
    # escalated on their merits.
    if not reasons and qa_sampled(call_id, rate=settings.qa_sample_rate, day=day):
        reasons.append("qa_sample")
    return Escalation(
        escalate=bool(reasons),
        reasons=tuple(reasons),
        risk_score=triage.risk_score,
        lexicon_severity=severity,
    )


# --- verifier ------------------------------------------------------------------


# An `instruction_like_content` span is not quoted from the transcript, so it is
# arbitrary model output on its way to a human. Cap it.
EXEMPT_SPAN_LIMIT = 300


@dataclass
class Verification:
    """What survived, and a count of everything that did not (E9)."""

    flags: list[AnalysisFlag] = field(default_factory=list)
    dropped_not_substring: int = 0
    dropped_bad_category: int = 0
    dropped_bad_severity: int = 0
    # Flags that skipped the substring check by category exemption. Counted so
    # they cannot masquerade as verified ones.
    exempt_not_verified: int = 0
    canary_leaked: bool = False
    rejected: list[dict[str, str]] = field(default_factory=list)

    @property
    def verified(self) -> int:
        """Flags that actually passed the substring check."""
        return len(self.flags) - self.exempt_not_verified

    @property
    def checked(self) -> int:
        """Flags the substring check was applied to. Exempt ones are not among
        them: counting an unverifiable span as a passing check is how a health
        metric stops meaning anything."""
        return (
            self.verified
            + self.dropped_not_substring
            + self.dropped_bad_category
            + self.dropped_bad_severity
        )

    @property
    def failure_rate(self) -> float | None:
        """None, not zero, when nothing was checked -- an empty verification is
        not a clean one."""
        return (self.checked - self.verified) / self.checked if self.checked else None


# A canary is only useful if it survives the ways a model might mangle it on the
# way out. Comparison is on alphanumerics only, casefolded, so a space, a case
# change or intervening punctuation does not hide the leak.
_ALNUM = re.compile(r"[^a-z0-9]+")
# Long enough that a coincidental collision is implausible, short enough that
# splitting the token across two fields still trips it.
CANARY_SLICE = 12


def _flatten(text: str) -> str:
    return _ALNUM.sub("", text.casefold())


def canary_leaked(payload: str, canary: str) -> bool:
    """Did any recognisable piece of the canary come back?

    `canary in payload` is defeated by a single space, by a case change, and by
    splitting the token across two fields -- all three were demonstrated against
    the first version of this check. Both sides are flattened to lowercase
    alphanumerics and any contiguous slice of the canary is enough.
    """
    flat_payload, flat_canary = _flatten(payload), _flatten(canary)
    if not flat_canary:
        return False
    if len(flat_canary) <= CANARY_SLICE:
        return flat_canary in flat_payload
    return any(
        flat_canary[i : i + CANARY_SLICE] in flat_payload
        for i in range(len(flat_canary) - CANARY_SLICE + 1)
    )


def verify(payload: Flags, transcript: str, *, canary: str) -> Verification:
    """Drop every flag that cannot be traced back to the transcript, and count it.

    Four rejections, each for a different reason a flag might be untrustworthy:

    1. **Canary leaked.** The system prompt is in the output, so the model was
       manipulated into reproducing its instructions. The whole response is
       discarded -- not filtered, discarded -- because nothing in a compromised
       response can be trusted, including the flags that look fine.
    2. **Evidence is not an exact substring.** A paraphrase, a translation, or
       an invention. Dropped, never repaired.
    3. **Unknown category.** Outside the policy's vocabulary.
    4. **Unknown severity.** Not one of low/medium/high.

    `instruction_like_content` is the one category exempt from (2): the model is
    reporting that the transcript addressed *it*, and quoting the injection
    verbatim is not required for that report to be useful. It still cannot
    suppress anything -- see `merge_with_lexicon`.
    """
    out = Verification()
    if canary and canary_leaked(payload.model_dump_json(), canary):
        out.canary_leaked = True
        out.rejected.append({"reason": "canary_leaked", "category": "*", "evidence": ""})
        return out

    for flag in payload.flags:
        if flag.category not in CATEGORIES:
            out.dropped_bad_category += 1
            out.rejected.append(
                {"reason": "unknown_category", "category": flag.category, "evidence": ""}
            )
            continue
        if flag.severity not in SEVERITIES:
            out.dropped_bad_severity += 1
            out.rejected.append(
                {"reason": "unknown_severity", "category": flag.category, "evidence": flag.severity}
            )
            continue
        if flag.category == "instruction_like_content":
            # Exempt from the substring check because the model is reporting
            # that the transcript addressed *it*; requiring a verbatim quote
            # would be beside the point. But exempt is not unbounded: this span
            # is model-controlled text that reaches a reviewer, so it is capped,
            # and it is counted separately rather than as a passing check --
            # otherwise flooding injection flags would keep `failure_rate`
            # looking clean while nothing was actually verified.
            flag = flag.model_copy(update={"evidence_span": flag.evidence_span[:EXEMPT_SPAN_LIMIT]})
            out.exempt_not_verified += 1
            out.flags.append(flag)
            continue
        # The model quoted what it was shown, which was escaped.
        quoted = unwrap_quoted(flag.evidence_span)
        if not quoted or quoted not in transcript:
            out.dropped_not_substring += 1
            out.rejected.append(
                {
                    "reason": "evidence_not_substring",
                    "category": flag.category,
                    "evidence": flag.evidence_span[:160],
                }
            )
            continue
        flag = flag.model_copy(update={"evidence_span": quoted})
        out.flags.append(flag)
    return out


SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}


def merge_with_lexicon(
    verified: list[AnalysisFlag],
    hits: list[matcher.Hit],
    segments: list[matcher.Segment] | None = None,
) -> list[AnalysisFlag]:
    """Stage 0's high-severity hits survive whatever Stage 2 said.

    The deterministic floor is the part an auditor can read and the part no
    prompt can talk out of a finding. Two ways a model could defeat it, both of
    which the first version of this function allowed:

    1. **Omission** -- return nothing for a call the lexicon flagged. The hit is
       re-added below.
    2. **Shadowing** -- return the *same* category and span at `severity: low`,
       so a presence-only dedupe finds a match and drops the lexicon's `high`.
       Severity drives the reviewer's queue order, so this quietly buries a
       finding without ever deleting it. The floor is therefore a floor on
       severity as well as on presence: a matching flag is raised to the
       lexicon's severity, never lowered by it.

    Speaker attribution comes from the lexicon hit, not the model, for the same
    reason: the hit knows which segment it matched, and misattributing a genuine
    quote to the wrong participant is a worse error than missing it.
    """
    floor: dict[tuple[str, str], matcher.Hit] = {}
    for hit in hits:
        if hit.severity != "high":
            continue
        # A hit found only in the Roman rendering still has to produce a flag:
        # transliteration evasion is exactly the adversarial class E9 names, and
        # dropping those hits would mean an evaded term escalates the call and
        # then contributes nothing. Quote the native turn it came from.
        span = hit.matched if hit.field == "text" else _native_span(hit, segments)
        if not span:
            continue
        floor[(hit.category, span)] = hit

    out: list[AnalysisFlag] = []
    for flag in verified:
        key = (flag.category, flag.evidence_span)
        hit = floor.pop(key, None)
        if hit is None:
            out.append(flag)
            continue
        raised = flag
        if SEVERITY_RANK.get(flag.severity, 0) < SEVERITY_RANK[hit.severity]:
            raised = raised.model_copy(update={"severity": hit.severity})
        if hit.speaker and raised.speaker != hit.speaker:
            raised = raised.model_copy(update={"speaker": hit.speaker})
        out.append(raised)

    for (category, span), hit in floor.items():
        out.append(
            AnalysisFlag(
                category=category,
                severity=hit.severity,
                speaker=hit.speaker,
                start_ms=_start_ms(hit, segments),
                evidence_span=span,
                english_rendering="",
                reasoning=f"Stage 0 lexicon entry {hit.entry_id} matched verbatim.",
            )
        )
    return out


def _native_span(hit: matcher.Hit, segments: list[matcher.Segment] | None) -> str:
    """The native text of the turn a transliteration-only hit came from."""
    if not segments or not 0 <= hit.segment_index < len(segments):
        return ""
    return segments[hit.segment_index].text


def _start_ms(hit: matcher.Hit, segments: list[matcher.Segment] | None) -> int:
    """Where in the call this is, so the reviewer's audio seek lands on it.

    `matcher.Segment` carries no timing -- it is the matcher's own minimal view
    -- so this is 0 until the caller supplies timed segments. Stated rather than
    silently wrong.
    """
    return 0


# --- the vendor stages ----------------------------------------------------------


def transcript_text(segments: list[matcher.Segment]) -> str:
    """The native-script transcript as the model sees it.

    Native script, not the Roman rendering: PRD E5 analyses in the original
    language so evidence stays verbatim and the translation layer never becomes
    a place where risk language is softened. Only flagged spans are translated.
    """
    return "\n".join(s.text for s in segments)


#: The cached adapter, the event loop its connection pool belongs to, and the redactor
#: it was built with. The loop is held by reference rather than by `id()`: `asyncio.run`
#: frees its loop on return, CPython reuses the address, and a key built from `id()`
#: therefore reports a brand-new loop as the old one -- which is the very bug below,
#: reintroduced by its own fix. Holding the object guarantees a new loop is a new
#: identity. One closed loop object is the entire cost.
_ADAPTER: Any = None
_ADAPTER_LOOP: Any = None
_ADAPTER_REDACTOR: Any = None


def _current_loop() -> Any:
    """The running event loop, or None when called synchronously."""
    import asyncio

    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def claude(redactor: Any = None) -> Any:
    """A Claude adapter with redaction off, as PRD E9 requires for this app.

    An off-channel-comms finding can turn on the phone number itself, and a
    reviewer handed `[PHONE]` as the evidence cannot act on it. This is the
    documented override in `apps/comms_surveillance/README.md`; redaction stays
    on for everything that reaches a log or a trace, which is the platform
    default everywhere else.

    ## Why this is not an `lru_cache`

    It was one, and that is wrong for an async client. `AsyncAnthropic` owns an httpx
    connection pool whose sockets are registered with the event loop that first used
    them, so a cached instance is usable only from that loop. `detect` -- the eval's
    entry point -- calls `asyncio.run` once per transcript, creating a fresh loop each
    time and closing the previous one. The first transcript succeeded and the second
    raised `RuntimeError: Event loop is closed`, which the SDK re-raised as
    `APIConnectionError: Connection error.`: a network error for what was really a
    lifetime bug, on a run that had already spent money.

    No single-call test could catch it, because one call is one loop. It took the first
    live 200-item eval to surface -- which is why it sat here undetected for as long as
    the key needed to run that eval was itself blocked by a name collision.

    Rebuilding on a loop change, rather than keeping one adapter per loop, holds a
    single live client: a 200-item eval would otherwise accumulate 200, each with
    sockets against a loop that no longer exists.
    """
    global _ADAPTER, _ADAPTER_LOOP, _ADAPTER_REDACTOR
    from indic_platform.adapters.claude import Claude

    loop = _current_loop()
    stale = (
        _ADAPTER is None
        or _ADAPTER_LOOP is not loop
        or _ADAPTER_REDACTOR is not redactor
        or (loop is not None and loop.is_closed())
    )
    if stale:
        _ADAPTER = Claude(
            redactor=redactor or (lambda text: text),
            wrapper=lambda text: wrap_untrusted(text, "transcript"),
        )
        _ADAPTER_LOOP = loop
        _ADAPTER_REDACTOR = redactor
    return _ADAPTER


def _clear_adapter() -> None:
    """Drop the cached adapter. Kept as `claude.cache_clear` for callers."""
    global _ADAPTER, _ADAPTER_LOOP, _ADAPTER_REDACTOR
    _ADAPTER = None
    _ADAPTER_LOOP = None
    _ADAPTER_REDACTOR = None


# Preserves the surface the `lru_cache` gave this function, so tests and any
# caller that resets the adapter keep working unchanged.
claude.cache_clear = _clear_adapter  # type: ignore[attr-defined]


async def triage(text: str, *, client: Any = None, settings: DetectorSettings = SETTINGS) -> Triage:
    """Stage 1: cheap enough to run on every call. No tools."""
    sut = client if client is not None else claude()
    system = prompt_body(PROMPTS / "triage.md").replace("{policy_summary}", policy_summary())
    return await sut.structured(system=system, user=text, schema=Triage, model=TRIAGE_MODEL)


async def deep_analysis(
    text: str, *, client: Any = None, settings: DetectorSettings = SETTINGS
) -> Flags:
    """Stage 2: escalated calls only. Full policy, no tools, JSON only."""
    sut = client if client is not None else claude()
    system = (
        prompt_body(PROMPTS / "deep_analysis.md")
        .replace("{canary}", settings.canary)
        .replace("{policy_document}", policy_document())
    )
    return await sut.structured(system=system, user=text, schema=Flags, model=ANALYSIS_MODEL)


class Rendering(BaseModel):
    english: str


async def render_english(flag: AnalysisFlag, *, client: Any = None) -> str:
    """An English rendering of one flagged span (E5: only flagged evidence).

    Uses whatever Stage 2 already returned when it is there; a separate call is
    a fallback, not the path. Failure is not fatal -- a reviewer who reads the
    language does not need it, and an empty rendering is better than a wrong one.
    """
    if flag.english_rendering.strip():
        return flag.english_rendering
    sut = client if client is not None else claude()
    try:
        result = await sut.structured(
            system="Translate the user's text into English. Return only the translation.",
            user=flag.evidence_span,
            schema=Rendering,
            model=TRIAGE_MODEL,
        )
    except Exception:
        return ""
    return result.english


@dataclass
class Analysis:
    """One call's result, with everything E7 says a persisted row must record."""

    call_id: str
    flags: list[AnalysisFlag]
    escalation: Escalation
    verification: Verification
    model: str
    triage_model: str
    prompt_version: str
    triage_prompt_version: str
    policy_version: str
    lexicon_version: str
    theta: int
    qa_sample_rate: float
    input_sha256: str
    stage2_error: str | None = None

    @property
    def effective_model(self) -> str:
        """The model that actually ran. `model` names the Stage 2 model whether
        or not Stage 2 ran, so a direct reader of the dataclass would otherwise
        see a false id on a non-escalated call."""
        return self.model if self.escalation.escalate else self.triage_model

    def row(self) -> dict[str, Any]:
        """The `analysis_runs` payload (PRD E8), ready for P5 to hash-chain."""
        return {
            "call_id": self.call_id,
            "stage": "deep_analysis" if self.escalation.escalate else "triage",
            "model": self.effective_model,
            # E8's `analysis_runs` carries `input_sha256`: it is what binds a row
            # to the exact transcript that produced it, and without it a chain
            # can be intact while describing text nobody can reconstruct.
            "input_sha256": self.input_sha256,
            "stage2_error": self.stage2_error,
            "policy_version": self.policy_version,
            "lexicon_version": self.lexicon_version,
            "prompt_version": (
                self.prompt_version if self.escalation.escalate else self.triage_prompt_version
            ),
            "theta": self.theta,
            "qa_sample_rate": self.qa_sample_rate,
            "escalated": self.escalation.escalate,
            "escalation_reasons": list(self.escalation.reasons),
            "risk_score": self.escalation.risk_score,
            "flags": [f.model_dump() for f in self.flags],
            "verification": {
                "checked": self.verification.checked,
                "exempt_not_verified": self.verification.exempt_not_verified,
                "dropped_not_substring": self.verification.dropped_not_substring,
                "dropped_bad_category": self.verification.dropped_bad_category,
                "dropped_bad_severity": self.verification.dropped_bad_severity,
                "canary_leaked": self.verification.canary_leaked,
            },
        }


async def analyse(
    call_id: str,
    segments: list[matcher.Segment],
    *,
    lexicon: matcher.Lexicon | None = None,
    client: Any = None,
    settings: DetectorSettings = SETTINGS,
    day: str | None = None,
) -> Analysis:
    """Stage 0 -> Stage 1 -> combine -> Stage 2 -> verify, for one call."""
    # A call is uc3's unit of work, so it owns the spend scope: triage, deep analysis
    # and the English rendering of every flag all charge to this call id and are
    # refused together once they cross the per-session cap. Without it these calls
    # charge "no session at all" (`AdapterRuntime.session()` returns None), and a
    # single pathological transcript -- a very long call, or one escalating every
    # segment -- is bounded only by the whole day's budget.
    with budget.session_scope(call_id):
        return await _analyse(
            call_id, segments, lexicon=lexicon, client=client, settings=settings, day=day
        )


async def _analyse(
    call_id: str,
    segments: list[matcher.Segment],
    *,
    lexicon: matcher.Lexicon | None = None,
    client: Any = None,
    settings: DetectorSettings = SETTINGS,
    day: str | None = None,
) -> Analysis:
    lex = lexicon if lexicon is not None else matcher.load()
    text = transcript_text(segments)
    hits = lex.scan(segments)

    scored = await triage(text, client=client, settings=settings)
    escalation = combine(call_id, hits, scored, settings, day=day)

    verification = Verification()
    flags: list[AnalysisFlag] = []
    stage2_error: str | None = None
    if escalation.escalate:
        try:
            produced = await deep_analysis(text, client=client, settings=settings)
        except Exception as error:
            # Losing the whole call because Stage 2 failed would mean a vendor
            # outage, a refusal, or a malformed reply silently clears a call the
            # lexicon had already flagged. Degrade to the deterministic floor and
            # record why, so the gap is visible rather than absent.
            stage2_error = f"{type(error).__name__}: {error}"
            log.warning("uc3 stage 2 failed for %s: %s", call_id, stage2_error)
        else:
            verification = verify(produced, text, canary=settings.canary)
        flags = merge_with_lexicon(verification.flags, hits, segments)
    else:
        # Reachable only for a non-high hit set: any high `text` hit forces
        # escalation. Kept so the floor is applied on exactly one path.
        flags = merge_with_lexicon([], hits, segments)

    # PRD E5: only flagged evidence is translated, and only where Stage 2 did
    # not already supply it. Failures are non-fatal -- a reviewer who reads the
    # language does not need it, and an empty rendering beats a wrong one.
    for index, flag in enumerate(flags):
        if flag.category == "instruction_like_content" or flag.english_rendering.strip():
            continue
        rendered = await render_english(flag, client=client)
        if rendered:
            flags[index] = flag.model_copy(update={"english_rendering": rendered})

    return Analysis(
        call_id=call_id,
        flags=flags,
        escalation=escalation,
        verification=verification,
        model=ANALYSIS_MODEL,
        triage_model=TRIAGE_MODEL,
        prompt_version=prompt_version(PROMPTS / "deep_analysis.md"),
        triage_prompt_version=prompt_version(PROMPTS / "triage.md"),
        policy_version=policy_version(),
        lexicon_version=lex.version,
        theta=settings.theta,
        qa_sample_rate=settings.qa_sample_rate,
        input_sha256=hashlib.sha256(text.encode()).hexdigest(),
        stage2_error=stage2_error,
    )


# --- the eval runner's system under test -----------------------------------------


def detect(transcript: Any) -> list[Any]:
    """`run_uc3`'s `Transcript -> [Flag]` contract, running all three stages.

    Synchronous because the runner is; each call is one transcript, and the
    concurrency question belongs to whoever drives a nightly batch.
    """
    import asyncio

    from indic_platform.eval.runners.run_uc3 import Flag

    from comms_surveillance.stage0 import segments_of

    analysis = asyncio.run(analyse(transcript.id, segments_of(transcript)))
    return [
        Flag(
            category=flag.category,
            evidence_span=flag.evidence_span,
            speaker=flag.speaker,
            severity=flag.severity,
        )
        for flag in analysis.flags
        # The eval scores the six policy categories; an injection flag is a
        # security signal, not a policy finding, and counting it as a false
        # positive against a labelled corpus would punish the control for
        # working.
        if flag.category != "instruction_like_content"
    ]


def estimate_cost(transcripts: int, escalation_rate: float = 0.2) -> str:
    """What a full `make eval-uc3` will spend, before it spends it.

    `.claude/rules/eval.md`: a live run prints an estimate first. Haiku sees
    every transcript; Sonnet sees the escalated share.
    """
    import yaml

    pricing = yaml.safe_load(
        (Path(__file__).parents[2] / "platform/config/pricing.yaml").read_text()
    )
    fx = float(pricing["fx_inr_per_usd"])
    haiku = pricing["models"][TRIAGE_MODEL]
    sonnet = pricing["models"][ANALYSIS_MODEL]
    escalated = round(transcripts * escalation_rate)

    # Rough shapes: triage is a short transcript plus a cached summary; deep
    # analysis carries the whole policy document.
    triage_usd = transcripts * (1200 * haiku["input_tokens"] + 120 * haiku["output_tokens"])
    deep_usd = escalated * (4500 * sonnet["input_tokens"] + 700 * sonnet["output_tokens"])
    usd = triage_usd + deep_usd
    return (
        f"uc3 full detector: {transcripts} triage calls on {TRIAGE_MODEL} + "
        f"~{escalated} deep-analysis calls on {ANALYSIS_MODEL} "
        f"-- estimated ${usd:.2f} / Rs {usd * fx:.0f}"
    )
