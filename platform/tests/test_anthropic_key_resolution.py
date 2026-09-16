"""Which variable the Anthropic key is read from, and why there is more than one.

`ANTHROPIC_API_KEY` is the name the Anthropic SDK reads by default, and it is unusable
in the environment this project is built in: inside a Claude Code session that variable
belongs to the agent harness, the provider is host-managed, and a value set on the
environment is stripped before the session sees it. Every live Anthropic eval was
blocked by that and reported UNMEASURED across three prompts before anyone noticed the
cause was a name collision rather than a missing key.

So the resolver takes the project-scoped name first and the standard one second. These
tests exist to keep both halves true: the fallback must keep working (CI, a developer's
`.env` and the Key Vault wiring all set the standard name), and the preference order
must not silently invert, which would put the build back where it started.
"""

import pytest
from indic_platform.config.settings import (
    ANTHROPIC_KEY_NAMES,
    anthropic_api_key,
    anthropic_key_source,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient key leaks in from the session, `.env` or `.env.stack`."""
    for name in ANTHROPIC_KEY_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_the_project_scoped_name_is_preferred(monkeypatch: pytest.MonkeyPatch) -> None:
    """The preference order is the whole point; inverting it re-breaks the build.

    With both set, the project-scoped one wins. In a Claude Code session the standard
    name is stripped and never reaches us, but a developer may well have both in a
    local `.env`, and the one they added deliberately for this project should win.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "standard")  # pragma: allowlist secret
    monkeypatch.setenv("INDICAI_ANTHROPIC_API_KEY", "scoped")  # pragma: allowlist secret
    assert anthropic_api_key() == "scoped"
    assert anthropic_key_source() == "INDICAI_ANTHROPIC_API_KEY"


def test_the_standard_name_still_works_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI, a local shell and Key Vault all set this one; the rename must not break them."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "standard")  # pragma: allowlist secret
    assert anthropic_api_key() == "standard"
    assert anthropic_key_source() == "ANTHROPIC_API_KEY"


def test_the_scoped_name_works_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Claude Code session case: the standard name is absent, not empty."""
    monkeypatch.setenv("INDICAI_ANTHROPIC_API_KEY", "scoped")  # pragma: allowlist secret
    assert anthropic_api_key() == "scoped"


def test_no_key_configured_is_none_not_empty_string() -> None:
    """None distinguishes "not configured" from "configured empty".

    Callers gate with `if not anthropic_api_key()`, and the adapter passes the result
    straight to the SDK, where "" and None produce different errors -- the SDK's
    "no key" message is the useful one.
    """
    assert anthropic_api_key() is None
    assert anthropic_key_source() is None


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_blank_value_is_not_a_key(blank: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A variable set to whitespace is a misconfiguration, not a credential.

    Without the strip, an exported-but-empty `INDICAI_ANTHROPIC_API_KEY` would shadow a
    perfectly good `ANTHROPIC_API_KEY` and fail at the vendor with an auth error that
    names neither variable.
    """
    monkeypatch.setenv("INDICAI_ANTHROPIC_API_KEY", blank)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "standard")  # pragma: allowlist secret
    assert anthropic_api_key() == "standard"
    assert anthropic_key_source() == "ANTHROPIC_API_KEY"


def test_the_key_is_stripped_of_surrounding_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """A trailing newline from a copy-paste or a file-sourced secret is not part of the key."""
    monkeypatch.setenv("INDICAI_ANTHROPIC_API_KEY", "  scoped\n")  # pragma: allowlist secret
    assert anthropic_api_key() == "scoped"


def test_the_adapter_uses_the_resolver_rather_than_the_sdk_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevents a silent regression to the SDK's implicit ANTHROPIC_API_KEY lookup.

    `AsyncAnthropic()` with no `api_key` reads `ANTHROPIC_API_KEY` itself. In a Claude
    Code session that is absent, so the client would be built with no credential and
    every live call would fail at the vendor rather than at configuration. Asserted on
    the key the constructed client actually holds.
    """
    from indic_platform.adapters.claude import Claude

    monkeypatch.setenv(
        "INDICAI_ANTHROPIC_API_KEY", "scoped-for-the-client"
    )  # pragma: allowlist secret
    assert Claude().client.api_key == "scoped-for-the-client"


def test_an_injected_client_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every mocked test in the suite passes its own client; resolution must not touch it."""
    from indic_platform.adapters.claude import Claude

    sentinel = object()
    assert Claude(client=sentinel).client is sentinel  # type: ignore[arg-type]
