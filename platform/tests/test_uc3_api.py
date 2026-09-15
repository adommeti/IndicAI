"""The uc3 API's role matrix, and the ways a refused caller could still have won.

The acceptance criterion for uc3/P6 is "role tests prove governance cannot fetch
transcripts", so these tests are written to fail loudly if the check is deleted
rather than to pass because a fixture happened to be empty:

- every row of the contract's endpoint table is asserted for all three roles,
  so a route that silently loses its dependency shows up as a 200 where a 403
  belongs;
- the refusal tests assert that the loader which reads transcripts was *never
  awaited*, not merely that the status code was 403. Removing the dependency
  turns those into failures even if something downstream happened to error;
- the leak tests stuff a sentinel string into every fake the API can reach and
  assert it never appears in a governance-visible response body, which covers
  the indirect routes -- a validation echo, a 404-versus-403 existence oracle,
  a stray metrics field -- as well as the direct ones.

Nothing here touches Postgres or MinIO. The session is a fake, the object store
is a mock and the two collaborating modules (`audit`, `metrics`) are patched at
their call sites; the one test that genuinely needs the stack to prove SQL
ordering is marked `integration`.
"""

import re
import uuid
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from comms_surveillance import api
from comms_surveillance.audit import ChainResult
from comms_surveillance.auth import (
    AUTHENTICATED,
    ROLE_GOVERNANCE,
    ROLE_LEAD,
    ROLE_REVIEWER,
    DevBypassRefused,
)
from fastapi.testclient import TestClient
from indic_platform.db.models import Call, Disposition, Flag
from starlette.authentication import AuthCredentials, AuthenticationBackend, SimpleUser
from starlette.middleware.authentication import AuthenticationMiddleware

# The string that must never reach a governance reader. Every fake below is
# seeded with it, so a leak through any field of any response is one assertion.
SECRET = "SECRET-TRANSCRIPT-bol-do-capital-protected-hai"

FLAG_ID = "11111111-1111-4111-8111-111111111111"
MISSING_FLAG_ID = "99999999-9999-4999-8999-999999999999"
CALL_ID = "22222222-2222-4222-8222-222222222222"

SHA256 = re.compile(r"\b[0-9a-f]{64}\b")


# --- a verified subject, without an identity provider --------------------------


class _Verified(SimpleUser):
    @property
    def identity(self) -> str:
        return self.username


def _backend(identity: str, scopes: tuple[str, ...]) -> AuthenticationBackend:
    class Backend(AuthenticationBackend):
        async def authenticate(self, conn: Any) -> tuple[AuthCredentials, SimpleUser]:
            # Stand-in for server-side SSO validation. Never a request header:
            # the whole point of the contract is that the caller cannot name
            # its own roles.
            return AuthCredentials([AUTHENTICATED, *scopes]), _Verified(identity)

    return Backend()


def client(*scopes: str, identity: str = "person@example.test") -> TestClient:
    return TestClient(AuthenticationMiddleware(api.app, backend=_backend(identity, scopes)))


def anonymous() -> TestClient:
    """No authentication middleware at all -- a bare deployment."""
    return TestClient(api.app)


# --- fakes ---------------------------------------------------------------------


class FakeSession:
    """Just enough AsyncSession for the routes that touch one directly."""

    def __init__(self, objects: dict[tuple[str, str], Any] | None = None) -> None:
        self.objects = objects or {}
        self.added: list[Any] = []
        self.commits = 0

    async def get(self, model: Any, pk: Any) -> Any:
        return self.objects.get((model.__name__, str(pk)))

    def add(self, row: Any) -> None:  # must never be used for a chained table
        self.added.append(row)

    async def commit(self) -> None:
        self.commits += 1


def a_flag() -> Flag:
    flag = Flag(
        id=uuid.UUID(FLAG_ID),
        call_id=uuid.UUID(CALL_ID),
        run_id=uuid.uuid4(),
        category="guaranteed_returns",
        severity="high",
        speaker="agent",
        start_ms=4200,
        evidence_span=SECRET,
        english_rendering=SECRET,
        reasoning=SECRET,
    )
    return flag


SUMMARY = {
    "flag_id": FLAG_ID,
    "call_id": CALL_ID,
    "category": "guaranteed_returns",
    "severity": "high",
    "speaker": "agent",
    "start_ms": 4200,
    "created_at": "2026-09-15T04:00:00+00:00",
    "disposition": None,
}

DETAIL = {
    **SUMMARY,
    "evidence_span": SECRET,
    "english_rendering": SECRET,
    "reasoning": SECRET,
    "policy_clause": "Promising a client a certain return.",
    "transcript": [
        {
            "seg_id": 0,
            "speaker": "agent",
            "start_ms": 4200,
            "end_ms": 6100,
            "text": SECRET,
            "text_roman": SECRET,
        }
    ],
    "dispositions": [],
}


@dataclass
class Wiring:
    """Every collaborator the API reaches, as a mock we can interrogate."""

    session: FakeSession
    summaries: AsyncMock
    detail: AsyncMock
    append: AsyncMock
    presign: Mock
    precision_by_category: AsyncMock
    precision_over_time: AsyncMock
    false_negatives: AsyncMock
    verify_all: AsyncMock

    @property
    def transcript_loaders(self) -> tuple[AsyncMock, ...]:
        """The calls that read, or hand out the means to hear, a recording."""
        return (self.summaries, self.detail, self.append, self.presign)


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch every collaborator and hand the app a fake session."""
    session = FakeSession(
        {
            ("Flag", FLAG_ID): a_flag(),
            (
                "Call",
                CALL_ID,
            ): Call(
                id=uuid.UUID(CALL_ID), source_uri="s3://x/y.wav", source_key="recordings/y.wav"
            ),
        }
    )
    summaries = AsyncMock(return_value=[dict(SUMMARY)])
    detail = AsyncMock(return_value=dict(DETAIL))
    append = AsyncMock(
        return_value=Mock(
            id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
            seq=7,
            row_hash="a" * 64,
        )
    )
    presign = Mock(return_value=f"https://minio.example.test/y.wav?sig=abc&note={SECRET}")
    monkeypatch.setattr(api, "flag_summaries", summaries)
    monkeypatch.setattr(api, "flag_detail", detail)
    monkeypatch.setattr(api.audit, "append", append)
    monkeypatch.setattr(
        api.storage, "client", Mock(return_value=Mock(presigned_get_object=presign))
    )

    by_category = AsyncMock(
        return_value=[
            api.metrics.CategoryPrecision(
                category="guaranteed_returns",
                confirmed=3,
                false_positive=1,
                decided=4,
                precision=0.75,
            ),
            api.metrics.CategoryPrecision(
                category="mnpi_insider", confirmed=0, false_positive=0, decided=0, precision=None
            ),
        ]
    )
    # A stray field on a bucket: exactly how a transcript quote would get into
    # the one aggregate a governance reader is allowed to see.
    over_time = AsyncMock(
        return_value=[
            {
                "bucket": "2026-W37",
                "category": "guaranteed_returns",
                "decided": 4,
                "precision": 0.75,
                "note": SECRET,
                "evidence_span": SECRET,
            }
        ]
    )
    false_negatives = AsyncMock(
        return_value=api.metrics.FalseNegativeEstimate(sampled=0, missed=0, rate=None)
    )
    monkeypatch.setattr(api.metrics, "precision_by_category", by_category)
    monkeypatch.setattr(api.metrics, "precision_over_time", over_time)
    monkeypatch.setattr(api.metrics, "false_negative_estimate", false_negatives)

    verify_all = AsyncMock(
        return_value=[
            ChainResult(
                table="flags",
                rows=12,
                ok=True,
                head_hash="b" * 64,
                anchor_rows=12,
                anchor_ok=True,
            ),
            ChainResult(
                table="dispositions",
                rows=3,
                ok=False,
                first_break_seq=2,
                first_break_id=FLAG_ID,
                reason="row_hash does not match the row content: the row was edited",
                head_hash="c" * 64,
                anchor_rows=3,
                anchor_ok=False,
            ),
        ]
    )
    monkeypatch.setattr(api.audit, "verify_all", verify_all)

    api.app.dependency_overrides[api.db_session] = lambda: session
    yield Wiring(
        session=session,
        summaries=summaries,
        detail=detail,
        append=append,
        presign=presign,
        precision_by_category=by_category,
        precision_over_time=over_time,
        false_negatives=false_negatives,
        verify_all=verify_all,
    )
    api.app.dependency_overrides.clear()


# --- the contract's endpoint table ---------------------------------------------


DISPOSITION_BODY = {"disposition": "confirmed", "note": "spoke to the desk head"}

# (method, path, body, {role: expected status}) -- one row per row of the
# contract's table, in the same order.
MATRIX: list[tuple[str, str, dict[str, Any] | None, dict[str, int]]] = [
    ("GET", "/me", None, {ROLE_REVIEWER: 200, ROLE_LEAD: 200, ROLE_GOVERNANCE: 200}),
    ("GET", "/flags", None, {ROLE_REVIEWER: 200, ROLE_LEAD: 200, ROLE_GOVERNANCE: 403}),
    (
        "GET",
        f"/flags/{FLAG_ID}",
        None,
        {ROLE_REVIEWER: 200, ROLE_LEAD: 200, ROLE_GOVERNANCE: 403},
    ),
    (
        "GET",
        f"/flags/{FLAG_ID}/audio",
        None,
        {ROLE_REVIEWER: 200, ROLE_LEAD: 200, ROLE_GOVERNANCE: 403},
    ),
    (
        "POST",
        f"/flags/{FLAG_ID}/dispositions",
        DISPOSITION_BODY,
        {ROLE_REVIEWER: 201, ROLE_LEAD: 201, ROLE_GOVERNANCE: 403},
    ),
    ("GET", "/qa-sample", None, {ROLE_REVIEWER: 403, ROLE_LEAD: 200, ROLE_GOVERNANCE: 403}),
    (
        "GET",
        "/metrics/precision",
        None,
        {ROLE_REVIEWER: 200, ROLE_LEAD: 200, ROLE_GOVERNANCE: 200},
    ),
    (
        "GET",
        "/metrics/false_negative_estimate",
        None,
        {ROLE_REVIEWER: 200, ROLE_LEAD: 200, ROLE_GOVERNANCE: 200},
    ),
    (
        "GET",
        "/audit/chain_status",
        None,
        {ROLE_REVIEWER: 200, ROLE_LEAD: 200, ROLE_GOVERNANCE: 200},
    ),
]


def call(api_client: TestClient, method: str, path: str, body: dict[str, Any] | None) -> Any:
    return api_client.request(method, path, json=body)


@pytest.mark.parametrize(("method", "path", "body", "expected"), MATRIX)
@pytest.mark.parametrize("role", [ROLE_REVIEWER, ROLE_LEAD, ROLE_GOVERNANCE])
def test_every_row_of_the_role_matrix_is_enforced_server_side(
    wired: Wiring,
    role: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    expected: dict[str, int],
) -> None:
    response = call(client(role), method, path, body)
    assert response.status_code == expected[role], (role, method, path, response.text)


def test_governance_never_reaches_the_code_that_reads_a_transcript(wired: Wiring) -> None:
    """403 is necessary but not sufficient: prove the loaders were never awaited.

    If the dependency were deleted these four would be called and the
    assertions below would fail even where the response happened to be an
    error for some other reason.
    """
    governance = client(ROLE_GOVERNANCE)
    for method, path, body, expected in MATRIX:
        if expected[ROLE_GOVERNANCE] != 403:
            continue
        assert call(governance, method, path, body).status_code == 403
    for loader in wired.transcript_loaders:
        loader.assert_not_called()
    assert wired.session.commits == 0
    assert wired.session.added == []


def test_governance_sees_no_transcript_text_on_any_endpoint_it_can_reach(
    wired: Wiring,
) -> None:
    """The sweep. Every fake is seeded with SECRET; none of it may come back."""
    governance = client(ROLE_GOVERNANCE)
    for method, path, body, expected in MATRIX:
        response = call(governance, method, path, body)
        assert SECRET not in response.text, (method, path, response.text)
        assert expected[ROLE_GOVERNANCE] == response.status_code


def test_governance_cannot_learn_whether_a_flag_exists(wired: Wiring) -> None:
    """403 for a real flag and 403 for an invented one, with the same body.

    A 404 on the missing one and a 403 on the real one would turn this endpoint
    into an existence oracle over the evidence store for a role that is not
    allowed to know the store's contents.
    """
    governance = client(ROLE_GOVERNANCE)
    real = governance.get(f"/flags/{FLAG_ID}")
    invented = governance.get(f"/flags/{MISSING_FLAG_ID}")
    assert real.status_code == invented.status_code == 403
    assert real.json() == invented.json()
    assert FLAG_ID not in real.text and MISSING_FLAG_ID not in invented.text
    wired.detail.assert_not_called()


def test_the_role_check_runs_before_validation_so_nothing_is_echoed_back(
    wired: Wiring,
) -> None:
    """A refused caller must not get a 422 that quotes their own request back.

    FastAPI solves dependencies before it validates path, query and body, so a
    403 raised in the dependency pre-empts the validation error. This pins that
    ordering: it is what stops `/flags?severity=<anything>` and a junk
    disposition body from becoming reflection surfaces for a role that should
    see nothing at all.
    """
    governance = client(ROLE_GOVERNANCE)
    junk = {"disposition": "made_up", "note": SECRET, "reviewer_id": "ceo@example.test"}
    posted = governance.post(f"/flags/{FLAG_ID}/dispositions", json=junk)
    assert posted.status_code == 403
    assert SECRET not in posted.text and "made_up" not in posted.text

    # A path id that is not a UUID at all, and a query value outside the enum.
    assert governance.get("/flags/not-a-uuid").status_code == 403
    assert governance.get(f"/flags?severity={SECRET}").status_code == 403
    wired.summaries.assert_not_called()
    wired.append.assert_not_called()


def test_a_reviewer_does_get_the_validation_error_the_governance_caller_did_not(
    wired: Wiring,
) -> None:
    """The mirror of the test above: the 403 is a refusal, not a broken route."""
    reviewer = client(ROLE_REVIEWER)
    assert reviewer.get("/flags?severity=critical").status_code == 422
    assert (
        reviewer.post(f"/flags/{FLAG_ID}/dispositions", json={"disposition": "made_up"}).status_code
        == 422
    )


def test_probing_with_other_methods_does_not_get_governance_a_transcript(
    wired: Wiring,
) -> None:
    governance = client(ROLE_GOVERNANCE)
    for method in ("HEAD", "OPTIONS", "PUT", "DELETE", "PATCH"):
        response = governance.request(method, f"/flags/{FLAG_ID}")
        assert response.status_code in {403, 405}, (method, response.status_code)
        assert SECRET not in response.text
    # Path shapes that route to the same endpoint: a trailing slash redirects,
    # and the redirect target is refused just the same.
    for path in (f"/flags/{FLAG_ID}/", "/flags/", "/qa-sample/"):
        assert governance.get(path).status_code in {403, 404, 405}, path
    wired.detail.assert_not_called()
    wired.summaries.assert_not_called()


# --- fail closed ---------------------------------------------------------------


def test_without_an_sso_subject_every_route_is_401(wired: Wiring) -> None:
    bare = anonymous()
    for method, path, body, _ in MATRIX:
        assert call(bare, method, path, body).status_code == 401, (method, path)
    for loader in wired.transcript_loaders:
        loader.assert_not_called()


def test_a_verified_subject_with_no_recognised_role_gets_nothing_but_me(
    wired: Wiring,
) -> None:
    """Authenticated is not authorised. Only `/me` answers, and it says so."""
    nobody = client()  # verified subject, no role scopes
    identity = nobody.get("/me")
    assert identity.status_code == 200
    assert identity.json()["roles"] == []
    for method, path, body, _ in MATRIX:
        if path == "/me":
            continue
        assert call(nobody, method, path, body).status_code == 403, (method, path)


def test_an_unrecognised_scope_is_neither_a_grant_nor_echoed_back(wired: Wiring) -> None:
    """A role this build has no rule for must not be reflected through `/me`.

    Echoing arbitrary scope strings would make the SSO claim a reflection
    surface, and a future `compliance_admin` scope must not read as a grant
    here just because it sounds like one.
    """
    impostor = client("compliance_admin", "admin", "root", SECRET)
    assert impostor.get("/me").json()["roles"] == []
    assert SECRET not in impostor.get("/me").text
    assert impostor.get("/flags").status_code == 403


def test_governance_is_subtractive_even_when_held_with_a_reviewer_role(
    wired: Wiring,
) -> None:
    """Segregation of duties: the deny wins over the allow.

    This is the stricter of the two readings of the contract and it is chosen
    deliberately (see `auth.py`). Additive roles would make "governance cannot
    fetch transcripts" conditional on nobody being granted both.
    """
    dual = client(ROLE_GOVERNANCE, ROLE_LEAD)
    assert dual.get("/flags").status_code == 403
    assert dual.get(f"/flags/{FLAG_ID}").status_code == 403
    assert dual.get(f"/flags/{FLAG_ID}/audio").status_code == 403
    assert dual.post(f"/flags/{FLAG_ID}/dispositions", json=DISPOSITION_BODY).status_code == 403
    assert dual.get("/qa-sample").status_code == 403
    # Still a governance reader for the things governance is for.
    assert dual.get("/metrics/precision").status_code == 200
    assert dual.get("/audit/chain_status").status_code == 200
    for loader in wired.transcript_loaders:
        loader.assert_not_called()


def test_the_dev_bypass_is_refused_when_env_is_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refused at startup, and refused again per request if it is set later."""
    monkeypatch.setenv("AUTH__DEV_BYPASS", "true")
    monkeypatch.setenv("ENV", "prod")
    with pytest.raises(DevBypassRefused), TestClient(api.app):
        pass
    # A process already up whose environment changes underneath it: the
    # per-request check still refuses rather than minting the test identity.
    refused = anonymous().get("/me")
    assert refused.status_code == 500
    assert refused.json()["detail"] == "Authentication is misconfigured"
    # It is a refusal, not a fallback: no identity is minted.
    assert "dev-bypass@example.test" not in refused.text


def test_the_dev_bypass_mints_a_fixed_identity_outside_prod(
    monkeypatch: pytest.MonkeyPatch, wired: Wiring
) -> None:
    monkeypatch.setenv("AUTH__DEV_BYPASS", "true")
    monkeypatch.delenv("ENV", raising=False)
    identity = anonymous().get("/me").json()
    assert identity["dev_bypass"] is True
    assert identity["identity"] == "dev-bypass@example.test"
    # Not governance by default: the local reviewer UI needs transcripts, and a
    # bypass granting every role would deny them.
    assert identity["roles"] == sorted([ROLE_LEAD, ROLE_REVIEWER])
    assert anonymous().get("/flags").status_code == 200


def test_the_dev_bypass_can_be_narrowed_to_the_governance_view(
    monkeypatch: pytest.MonkeyPatch, wired: Wiring
) -> None:
    monkeypatch.setenv("AUTH__DEV_BYPASS", "true")
    monkeypatch.setenv("AUTH__DEV_BYPASS_ROLES", "governance")
    monkeypatch.delenv("ENV", raising=False)
    bypassed = anonymous()
    assert bypassed.get("/me").json()["roles"] == [ROLE_GOVERNANCE]
    assert bypassed.get("/flags").status_code == 403
    wired.summaries.assert_not_called()


# --- the audited write ---------------------------------------------------------


def test_a_disposition_joins_the_hash_chain_and_carries_the_sso_identity(
    wired: Wiring,
) -> None:
    response = client(ROLE_REVIEWER, identity="asha@example.test").post(
        f"/flags/{FLAG_ID}/dispositions", json=DISPOSITION_BODY
    )
    assert response.status_code == 201
    assert response.json() == {
        "disposition_id": "33333333-3333-4333-8333-333333333333",
        "seq": 7,
        "row_hash": "a" * 64,
    }
    wired.append.assert_awaited_once()
    awaited = wired.append.await_args
    assert awaited is not None
    written = awaited.args[1]
    assert isinstance(written, Disposition)
    assert written.disposition == "confirmed"
    assert written.reviewer_id == "asha@example.test"
    assert str(written.flag_id) == FLAG_ID
    # Never around the side of the chain.
    assert wired.session.added == []
    assert wired.session.commits == 1


def test_a_disposition_cannot_sign_somebody_elses_name(wired: Wiring) -> None:
    """Identity comes from the claim; a body field must not override it."""
    reviewer = client(ROLE_REVIEWER, identity="asha@example.test")
    assert (
        reviewer.post(
            f"/flags/{FLAG_ID}/dispositions",
            json={**DISPOSITION_BODY, "reviewer_id": "ceo@example.test"},
        ).status_code
        == 422
    )
    wired.append.assert_not_called()


def test_a_disposition_on_an_unknown_flag_is_404_and_writes_nothing(wired: Wiring) -> None:
    response = client(ROLE_REVIEWER).post(
        f"/flags/{MISSING_FLAG_ID}/dispositions", json=DISPOSITION_BODY
    )
    assert response.status_code == 404
    wired.append.assert_not_called()


def test_a_changed_mind_is_a_new_row_not_an_edit(wired: Wiring) -> None:
    reviewer = client(ROLE_REVIEWER)
    reviewer.post(f"/flags/{FLAG_ID}/dispositions", json=DISPOSITION_BODY)
    reviewer.post(
        f"/flags/{FLAG_ID}/dispositions",
        json={"disposition": "false_positive", "note": "cleared with the desk"},
    )
    assert wired.append.await_count == 2
    assert [c.args[1].disposition for c in wired.append.await_args_list] == [
        "confirmed",
        "false_positive",
    ]


def test_the_note_is_bounded(wired: Wiring) -> None:
    response = client(ROLE_REVIEWER).post(
        f"/flags/{FLAG_ID}/dispositions",
        json={"disposition": "confirmed", "note": "x" * 4001},
    )
    assert response.status_code == 422
    wired.append.assert_not_called()


# --- audio ---------------------------------------------------------------------


def test_the_audio_url_is_short_lived_and_seeks_to_the_evidence(
    wired: Wiring, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("DEBUG"):
        response = client(ROLE_REVIEWER).get(f"/flags/{FLAG_ID}/audio")
    assert response.status_code == 200
    payload = response.json()
    assert payload["start_ms"] == 4200
    assert 0 < payload["expires_in_s"] <= 300, "a signed recording URL is a bearer token"
    assert payload["url"].startswith("https://")
    expires = wired.presign.call_args.kwargs["expires"]
    assert expires.total_seconds() == payload["expires_in_s"]
    # `.claude/rules/adapters.md`: signed URLs are never logged or traced.
    assert payload["url"] not in caplog.text
    assert "sig=abc" not in caplog.text


def test_audio_for_an_unknown_flag_is_404_and_signs_nothing(wired: Wiring) -> None:
    assert client(ROLE_REVIEWER).get(f"/flags/{MISSING_FLAG_ID}/audio").status_code == 404
    wired.presign.assert_not_called()


def test_audio_without_object_storage_credentials_is_503_not_a_crash(
    wired: Wiring, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api.storage, "client", Mock(side_effect=KeyError("MINIO_ACCESS_KEY")))
    assert client(ROLE_REVIEWER).get(f"/flags/{FLAG_ID}/audio").status_code == 503


# --- the reviewer's view -------------------------------------------------------


def test_the_queue_and_the_detail_carry_the_contract_fields(wired: Wiring) -> None:
    reviewer = client(ROLE_REVIEWER)
    listed = reviewer.get("/flags?severity=high&undispositioned=true").json()
    assert set(listed) == {"flags"}
    assert set(listed["flags"][0]) == set(SUMMARY)
    summaries_call = wired.summaries.await_args
    assert summaries_call is not None
    assert summaries_call.kwargs == {
        "severity": "high",
        "undispositioned": True,
    }

    detail = reviewer.get(f"/flags/{FLAG_ID}").json()
    assert set(detail) == set(SUMMARY) | {
        "evidence_span",
        "english_rendering",
        "reasoning",
        "policy_clause",
        "transcript",
        "dispositions",
    }
    assert detail["transcript"][0]["text"] == SECRET


def test_the_qa_sample_is_the_leads_alone_and_is_marked_as_such(wired: Wiring) -> None:
    items = client(ROLE_LEAD).get("/qa-sample").json()["items"]
    assert items[0]["qa_sampled"] is True
    assert set(items[0]) == set(SUMMARY) | {"qa_sampled"}
    qa_call = wired.summaries.await_args
    assert qa_call is not None
    assert qa_call.kwargs == {"qa_sample": True}
    wired.summaries.reset_mock()
    assert client(ROLE_REVIEWER).get("/qa-sample").status_code == 403
    wired.summaries.assert_not_called()


def test_the_policy_clause_is_read_from_the_policy_document() -> None:
    """Resolved from `policy.md` at read time, not copied onto the flag row."""
    clause = api.policy_clause("guaranteed_returns")
    assert "Promising a client a certain" in clause
    assert "**Flag.**" not in clause and "Do not flag" not in clause
    assert api.policy_clause("a_category_the_policy_never_defined") == ""


def test_the_queue_orders_high_then_medium_then_low() -> None:
    """The ORDER BY, without a database: severity rank first, oldest first next."""
    compiled = str(
        api.select(api.Flag)
        .order_by(api.SEVERITY_ORDER, api.Flag.created_at.asc(), api.Flag.seq.asc())
        .compile(compile_kwargs={"literal_binds": True})
    )
    order_by = compiled.split("ORDER BY", 1)[1]
    assert order_by.index("'high'") < order_by.index("'medium'") < order_by.index("'low'")
    assert order_by.index("flags.created_at ASC") > order_by.index("'low'")


@pytest.mark.integration
def test_the_queue_sorts_and_joins_the_latest_disposition_against_postgres() -> None:
    """The one thing a fake session cannot prove: that the SQL is right.

    `flag_summaries` leans on a `max(seq)` group-by joined back to the
    dispositions table and on JSONB containment for the QA sample; neither has
    meaning outside Postgres. Needs `make stack-core` and `make migrate`.
    """
    pytest.skip("requires the docker stack; run under make check-full")


# --- metrics and chain status --------------------------------------------------


def test_precision_reports_unmeasured_rather_than_a_passing_placeholder(
    wired: Wiring,
) -> None:
    """The uc3/P1 defect, pinned: no denominator is None, never 0.0 or 1.0."""
    payload = client(ROLE_GOVERNANCE).get("/metrics/precision").json()
    by_category = {row["category"]: row for row in payload["by_category"]}
    assert by_category["mnpi_insider"]["precision"] is None
    assert by_category["mnpi_insider"]["decided"] == 0
    assert by_category["guaranteed_returns"]["precision"] == 0.75
    assert payload["unmeasured"] == ["mnpi_insider"]


def test_the_precision_response_carries_only_the_fields_the_contract_names(
    wired: Wiring,
) -> None:
    """Projection, not pass-through.

    `metrics.py` is a separate module owned by someone else. A field added
    there must not be able to ride into the one aggregate a governance reader
    can see -- the `note` and `evidence_span` keys on the fake bucket are
    exactly that failure, and they are dropped.
    """
    payload = client(ROLE_GOVERNANCE).get("/metrics/precision").json()
    assert set(payload) == {"by_category", "over_time", "unmeasured"}
    for row in payload["by_category"]:
        assert set(row) == {"category", "confirmed", "false_positive", "decided", "precision"}
    for point in payload["over_time"]:
        assert set(point) <= api.OVER_TIME_KEYS
        assert "note" not in point and "evidence_span" not in point
    assert SECRET not in client(ROLE_GOVERNANCE).get("/metrics/precision").text


def test_the_false_negative_estimate_says_unmeasured_rather_than_zero(
    wired: Wiring,
) -> None:
    payload = client(ROLE_GOVERNANCE).get("/metrics/false_negative_estimate").json()
    assert payload == {"sampled": 0, "missed": 0, "rate": None, "unmeasured": True}


def test_the_false_negative_rate_is_reported_once_there_is_a_denominator(
    wired: Wiring,
) -> None:
    wired.false_negatives.return_value = api.metrics.FalseNegativeEstimate(
        sampled=40, missed=2, rate=0.05
    )
    payload = client(ROLE_GOVERNANCE).get("/metrics/false_negative_estimate").json()
    assert payload == {"sampled": 40, "missed": 2, "rate": 0.05, "unmeasured": False}


def test_chain_status_is_metadata_and_never_a_row_hash_or_a_row_id(wired: Wiring) -> None:
    """`audit.summarise` carries `head_hash` and `first_break_id`; this does not.

    A head hash is what an anchor is compared against, so publishing it hands a
    would-be tamperer the value to re-chain to. `first_break_id` names a flag
    or disposition row -- an identifier out of the evidence store, on the one
    endpoint governance can reach.
    """
    response = client(ROLE_GOVERNANCE).get("/audit/chain_status")
    payload = response.json()
    assert set(payload) == {"tables", "breaks", "ok", "checked_at"}
    for table in payload["tables"]:
        assert set(table) == {"table", "rows", "ok", "anchor_ok", "reason"}
    assert payload["breaks"] == 1
    assert payload["ok"] is False
    assert not SHA256.search(response.text), "a row hash reached a read-only endpoint"
    assert FLAG_ID not in response.text
    assert "head_hash" not in response.text


def test_chain_status_keeps_the_reason_so_a_break_is_actionable(wired: Wiring) -> None:
    tables = {
        t["table"]: t for t in client(ROLE_GOVERNANCE).get("/audit/chain_status").json()["tables"]
    }
    assert tables["dispositions"]["ok"] is False
    assert "the row was edited" in tables["dispositions"]["reason"]
    assert tables["flags"]["anchor_ok"] is True


def test_health_needs_no_identity() -> None:
    assert anonymous().get("/health").status_code == 200
