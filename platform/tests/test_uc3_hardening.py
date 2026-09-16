"""Two ways uc3 leaks a recording, and the refusals that stop them (uc3/P7).

Both fixes are about a channel, not a feature, so the tests that matter are the
ones that prove the channel is shut:

- the presigned audio URL is a bearer token for a call recording, so the client
  that signs it must be a TLS client unless a *named* non-prod environment says
  otherwise. The old test asserted `https://` on a mock that returned `https://`,
  which proved nothing about the transport;
- the nightly break alert is written to the application log, which is read by
  more people than `GET /audit/chain_status` and is shipped off-box. It must not
  carry the two fields that endpoint deliberately withholds.

The shape follows `platform/tests/test_uc1_dev_bypass.py`: the interesting cases
are the refusals, and misspelt and unset environment names are refusals too.
"""

import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from comms_surveillance import audit, storage
from comms_surveillance.auth import NON_PROD_ENVS

ACCESS_KEY = "minio-test"
SECRET_KEY = "minio-test-secret"  # pragma: allowlist secret

# Values a deployment could plausibly carry that are NOT a known non-prod
# environment. `""` stands for a variable nobody set; `"dv"` and `"devv"` are
# the typo that a `!= "prod"` check would have waved through.
NOT_NON_PROD = ["prod", "production", "prd", "", "Prod ", "staging", "dv", "devv"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ambient ENV or MinIO settings leak in from a shell or .env.stack.

    CI has neither file, so a test that silently depended on one would pass on a
    developer's machine and fail (or, worse, pass for the wrong reason) there.
    """
    for key in ("ENV", "MINIO_SECURE", "MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def minio_calls(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Record how `storage.client()` actually constructs the MinIO client.

    Asserting on the constructor argument rather than on a returned URL is the
    point of this file: a mocked `presigned_get_object` returns whatever scheme
    the test told it to, so the previous `startswith("https://")` assertion held
    for a plaintext deployment too.
    """
    recorder = Mock(return_value=Mock(name="Minio"))
    monkeypatch.setattr("minio.Minio", recorder)
    monkeypatch.setenv("MINIO_ACCESS_KEY", ACCESS_KEY)
    monkeypatch.setenv("MINIO_SECRET_KEY", SECRET_KEY)
    return recorder


# --- fix 1: the recording grant is not minted over plaintext -------------------


def test_an_unset_minio_secure_gives_a_tls_client(minio_calls: Mock) -> None:
    """The default flipped: plaintext is now opt-in, not opt-out.

    This is the bug. `MINIO_SECURE` defaulted to false, so every deployment that
    never heard of the variable signed 180-second recording grants over `http://`
    and nothing in the system said so.
    """
    assert storage.secure() is True
    storage.client()
    assert minio_calls.call_args.kwargs["secure"] is True


def test_being_in_dev_is_not_by_itself_consent_to_plaintext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dev environment with no flag still gets TLS. The flag has to be asked for."""
    monkeypatch.setenv("ENV", "dev")
    assert storage.secure() is True


def test_plaintext_with_no_env_at_all_is_refused(
    minio_calls: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ENV` genuinely unset, not set to "": the likeliest real misconfiguration.

    A flag copied from a developer's .env into a deployment that never sets ENV
    is how a plaintext grant reaches production, so unset fails closed.
    """
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.setenv("MINIO_SECURE", "false")
    with pytest.raises(storage.InsecureObjectStoreRefused) as caught:
        storage.client()
    assert "<unset>" in str(caught.value)
    minio_calls.assert_not_called()


@pytest.mark.parametrize("env", NOT_NON_PROD)
def test_plaintext_is_refused_outside_a_known_non_prod_env(
    env: str, monkeypatch: pytest.MonkeyPatch, minio_calls: Mock
) -> None:
    """Unset and misspelt refuse too, exactly as the dev bypass does.

    A guard reading `env != "prod"` would mint http grants for `ENV=production`,
    for a trailing space, for an unset variable and for every typo. The failure
    mode of a typo must be a closed door, because on the other side of this one
    is confidential call audio on the wire.
    """
    monkeypatch.setenv("ENV", env)
    monkeypatch.setenv("MINIO_SECURE", "false")

    with pytest.raises(storage.InsecureObjectStoreRefused) as caught:
        storage.secure()
    message = str(caught.value)
    assert "MINIO_SECURE" in message, "the refusal must name the variable to change"
    assert "ci, dev, local, test" in message, "and must name what IS allowed"
    assert (env.strip() or "<unset>") in message

    # The refusal is at the chokepoint, so the audio route cannot route around
    # it -- and it never reaches the MinIO constructor.
    with pytest.raises(storage.InsecureObjectStoreRefused):
        storage.client()
    minio_calls.assert_not_called()


def test_the_refusal_is_not_mistaken_for_missing_credentials(
    monkeypatch: pytest.MonkeyPatch, minio_calls: Mock
) -> None:
    """Not a KeyError.

    `api.flag_audio` catches `KeyError` and answers 503 "object storage is not
    configured". A refusal to use plaintext arriving as that 503 would be read as
    an outage and answered with a restart, and the transport would stay wrong.
    """
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("MINIO_SECURE", "false")
    with pytest.raises(storage.InsecureObjectStoreRefused) as caught:
        storage.client()
    assert not isinstance(caught.value, KeyError)


@pytest.mark.parametrize("flag", ["flase", "fasle", "no-really", "maybe", "  "])
def test_an_unreadable_flag_resolves_towards_tls(
    flag: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo in the *flag* must not downgrade the transport either.

    `MINIO_SECURE=flase` meaning "false" resolves to TLS, which fails loudly at
    connect time against a plaintext MinIO. The other direction would be a silent
    downgrade nobody ever notices.
    """
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("MINIO_SECURE", flag)
    assert storage.secure() is True


@pytest.mark.parametrize("env", sorted(NON_PROD_ENVS) + ["DEV", " local "])
def test_local_development_over_http_still_works(
    env: str, monkeypatch: pytest.MonkeyPatch, minio_calls: Mock
) -> None:
    """`ENV=dev` plus an explicit flag still talks to http://localhost:9000.

    A hardening change that broke `make stack-core` would be reverted within the
    week, and the revert would take the default with it.
    """
    monkeypatch.setenv("ENV", env)
    monkeypatch.setenv("MINIO_SECURE", "false")
    assert storage.secure() is False
    storage.client()
    assert minio_calls.call_args.kwargs["secure"] is False
    assert minio_calls.call_args.args[0] == "localhost:9000"


def test_the_env_allow_list_is_uc3s_one_list() -> None:
    """Imported from `auth`, not restated.

    Two copies of "which environments are not production" drift, and the copy
    that drifts is the one nobody re-reads. If this ever fails, one of them grew
    an entry the other did not.
    """
    from comms_surveillance import auth

    assert storage.NON_PROD_ENVS is auth.NON_PROD_ENVS


# --- fix 2: the break alert says enough, and no more ---------------------------

HEAD_HASH = "f" * 64
BREAK_ID = "9d2f0b4e-0000-4000-8000-00000000abcd"


class _FakeSession:
    """`run_chain_verify` only needs something to hand `verify_all`, which is mocked."""

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False


def _results() -> list[audit.ChainResult]:
    """One broken chain, one clean one, so the alert has to be selective."""
    return [
        audit.ChainResult(
            table="analysis_runs",
            rows=1204,
            ok=False,
            first_break_seq=417,
            first_break_id=BREAK_ID,
            reason="row_hash does not match the row content: the row was edited",
            head_hash=HEAD_HASH,
            anchor_rows=1204,
            anchor_ok=True,
        ),
        audit.ChainResult(
            table="dispositions", rows=88, ok=True, head_hash="a" * 64, anchor_ok=True
        ),
    ]


@pytest.fixture
def verified(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Run the nightly job against fabricated results, with no database and no sink."""
    emit = Mock(return_value=True)
    monkeypatch.setattr(audit, "emit_chain_metric", emit)
    return emit


async def test_the_break_alert_names_the_tables_but_not_the_evidence(
    caplog: pytest.LogCaptureFixture, verified: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The head hash and the break id must not reach the application log.

    `GET /audit/chain_status` withholds both on purpose -- a head hash is the
    target a tamperer has to re-chain to, and `first_break_id` names a row in the
    evidence store. Logging them is the same disclosure through a wider channel:
    logs are readable by more people than that endpoint and are shipped off-box.

    Asserted on the FORMATTED message, because that is what gets written. An
    assertion on the log call's arguments would pass while the whole summary dict
    was being interpolated into the line, which is precisely the bug.
    """
    monkeypatch.setattr(audit, "verify_all", AsyncMock(return_value=_results()))
    with caplog.at_level(logging.INFO, logger=audit.log.name):
        summary = await audit.run_chain_verify(_FakeSession, update_anchors=False)
    assert summary["ok"] is False

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, "one alert per run: on-call reads it once"
    line = errors[0].getMessage()

    assert HEAD_HASH not in line, "a published head hash is what a tamperer re-chains to"
    assert BREAK_ID not in line, "and this names a row in the evidence store"
    assert "417" not in line, "the seq locates the same row just as well as its id"


async def test_the_break_alert_stays_loud_enough_to_act_on(
    caplog: pytest.LogCaptureFixture, verified: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redacting it must not turn it into "something happened".

    The point of the line is that a person looks tonight, so it still has to say
    which chain broke, how big it is, what kind of break it was, and -- since the
    row id is gone -- where to go to find the row.
    """
    monkeypatch.setattr(audit, "verify_all", AsyncMock(return_value=_results()))
    with caplog.at_level(logging.INFO, logger=audit.log.name):
        await audit.run_chain_verify(_FakeSession, update_anchors=False)
    line = next(r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)

    assert "analysis_runs" in line, "which chain broke"
    assert "1204" in line, "and how many rows it holds"
    assert "the row was edited" in line, "and which of the three failures it was"
    assert "verify_chain" in line, "and where a reviewer goes to find the row itself"
    assert "dispositions" not in line, "the clean chain is not part of the alert"


async def test_a_clean_run_logs_a_row_count_and_not_a_dict(
    caplog: pytest.LogCaptureFixture, verified: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both lines interpolate named values, not the whole summary.

    The two used different styles -- `%s` against the summary dict on one,
    mapping-style `%(rows)s` on the other. Both formatted (logging collapses a
    lone mapping argument into `record.args`), so this was never a crash; it was
    how the error line came to dump every field the summary had, including the
    two that must not be logged.
    """
    clean = [audit.ChainResult(table="dispositions", rows=88, ok=True, head_hash="a" * 64)]
    monkeypatch.setattr(audit, "verify_all", AsyncMock(return_value=clean))
    with caplog.at_level(logging.INFO, logger=audit.log.name):
        await audit.run_chain_verify(_FakeSession, update_anchors=False)

    line = next(r.getMessage() for r in caplog.records if r.levelno == logging.INFO)
    assert "88 rows" in line
    assert "'head_hash'" not in line and "{" not in line, "no summary dict in the log line"


def test_break_detail_is_selective_without_a_run() -> None:
    """The helper itself, so the redaction is testable without the nightly job."""
    summary = audit.summarise(_results())
    detail = audit.break_detail(summary)
    assert "analysis_runs" in detail and "1204" in detail
    assert HEAD_HASH not in detail and BREAK_ID not in detail
    assert audit.break_detail(audit.summarise(_results()[1:])) == "no table reported a break"
