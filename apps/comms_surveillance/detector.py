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
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from indic_platform.security.harden import new_canary, unwrap_quoted, wrap_untrusted
from pydantic import BaseModel, Field

from comms_surveillance.lexicon import matcher

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

    theta: int = 55
    qa_sample_rate: float = 0.05
    # A per-deployment secret. Regenerated per process rather than stored: it
    # only has to be unguessable to the people on the call.
    canary: str = field(default_factory=new_canary)


SETTINGS = DetectorSettings()


# --- prompts -------------------------------------------------------------------


def prompt_body(path: Path) -> str:
    """The prompt text without its YAML front matter."""
    return re.sub(r"^---\n.*?\n---\n", "", path.read_text(), flags=re.S)


def prompt_version(path: Path) -> str:
    """`prompt_version` = sha256[:12] of the file content (`.claude/rules/apps.md`)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def policy_version() -> str:
    return hashlib.sha256(POLICY.read_bytes()).hexdigest()[:12]


def policy_summary() -> str:
    """The category names and one-line definitions, for the Haiku system prompt.

    Haiku gets the summary rather than the whole policy: it is scoring, not
    deciding, and the full document is several thousand tokens on every call.
    """
    lines = []
    for block in POLICY.read_text().split("\n## ")[1:]:
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
    if qa_sampled(call_id, rate=settings.qa_sample_rate, day=day):
        reasons.append("qa_sample")
    return Escalation(
        escalate=bool(reasons),
        reasons=tuple(reasons),
        risk_score=triage.risk_score,
        lexicon_severity=severity,
    )


# --- verifier ------------------------------------------------------------------


@dataclass
class Verification:
    """What survived, and a count of everything that did not (E9)."""

    flags: list[AnalysisFlag] = field(default_factory=list)
    dropped_not_substring: int = 0
    dropped_bad_category: int = 0
    dropped_bad_severity: int = 0
    canary_leaked: bool = False
    rejected: list[dict[str, str]] = field(default_factory=list)

    @property
    def checked(self) -> int:
        return (
            len(self.flags)
            + self.dropped_not_substring
            + self.dropped_bad_category
            + self.dropped_bad_severity
        )

    @property
    def failure_rate(self) -> float | None:
        """None, not zero, when nothing was checked -- an empty verification is
        not a clean one."""
        return (self.checked - len(self.flags)) / self.checked if self.checked else None


def verify(
    payload: Flags, transcript: str, *, canary: str, settings: DetectorSettings = SETTINGS
) -> Verification:
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
    if canary and canary in payload.model_dump_json():
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
        if flag.category != "instruction_like_content":
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


def merge_with_lexicon(verified: list[AnalysisFlag], hits: list[matcher.Hit]) -> list[AnalysisFlag]:
    """Stage 0's high-severity hits survive whatever Stage 2 said.

    The deterministic floor is the part an auditor can read and the part no
    prompt can talk out of a finding. If the model returns nothing for a call
    the lexicon flagged at high severity, the lexicon's flag stands -- that is
    the entire point of having a rule stage under the model stage.
    """
    seen = {(f.category, f.evidence_span) for f in verified}
    out = list(verified)
    for hit in hits:
        if hit.severity != "high" or hit.field != "text":
            continue
        key = (hit.category, hit.matched)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            AnalysisFlag(
                category=hit.category,
                severity=hit.severity,
                speaker=hit.speaker,
                start_ms=0,
                evidence_span=hit.matched,
                english_rendering="",
                reasoning=f"Stage 0 lexicon entry {hit.entry_id}: {hit.entry_id} matched verbatim.",
            )
        )
    return out


# --- the vendor stages ----------------------------------------------------------


def transcript_text(segments: list[matcher.Segment]) -> str:
    """The native-script transcript as the model sees it.

    Native script, not the Roman rendering: PRD E5 analyses in the original
    language so evidence stays verbatim and the translation layer never becomes
    a place where risk language is softened. Only flagged spans are translated.
    """
    return "\n".join(s.text for s in segments)


def claude(redactor: Any = None) -> Any:
    """A Claude adapter with redaction off, as PRD E9 requires for this app.

    An off-channel-comms finding can turn on the phone number itself, and a
    reviewer handed `[PHONE]` as the evidence cannot act on it. This is the
    documented override in `apps/comms_surveillance/README.md`; redaction stays
    on for everything that reaches a log or a trace, which is the platform
    default everywhere else.
    """
    from indic_platform.adapters.claude import Claude

    return Claude(
        redactor=redactor or (lambda text: text),
        wrapper=lambda text: wrap_untrusted(text, "transcript"),
    )


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
        .replace("{policy_document}", POLICY.read_text())
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

    def row(self) -> dict[str, Any]:
        """The `analysis_runs` payload (PRD E8), ready for P5 to hash-chain."""
        return {
            "call_id": self.call_id,
            "stage": "deep_analysis" if self.escalation.escalate else "triage",
            "model": self.model if self.escalation.escalate else self.triage_model,
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
    lex = lexicon if lexicon is not None else matcher.load()
    text = transcript_text(segments)
    hits = lex.scan(segments)

    scored = await triage(text, client=client, settings=settings)
    escalation = combine(call_id, hits, scored, settings, day=day)

    verification = Verification()
    flags: list[AnalysisFlag] = []
    if escalation.escalate:
        produced = await deep_analysis(text, client=client, settings=settings)
        verification = verify(produced, text, canary=settings.canary, settings=settings)
        flags = merge_with_lexicon(verification.flags, hits)
    else:
        flags = merge_with_lexicon([], hits)

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
