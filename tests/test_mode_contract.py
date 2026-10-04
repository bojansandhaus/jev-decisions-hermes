"""The four canonical modes, the alias table, and the interchangeable local slot.

Everything here is proved with an injected transport or a captured socket. No
credential on this machine is authorized for a hosted provider and no local
decision server is running, so no test in this file makes, or claims to make, a
live call to any provider.
"""
from __future__ import annotations

import json
import logging
import os
from unittest import mock

import pytest

import jev_client

QUESTIONS = {
    "verdict": {
        "type": "choice",
        "instructions": "Classify the fixture.",
        "criteria": {"APPROVE": "safe", "DENY": "harmful"},
    },
    "certainty": {
        "type": "noul",
        "instructions": "Is this a fixture?",
        "criteria": {"true": "It is", "false": "It is not"},
    },
    "blast_radius": {
        "type": "score",
        "instructions": "How hard is recovery?",
        "criteria": ["trivial", "annoying", "severe"],
    },
}
ANSWERS = {
    "verdict": {"choice": "APPROVE", "confidence": 0.9},
    "certainty": {"noul": 1.0, "confidence": 0.95},
    "blast_radius": {"score": 1, "confidence": 0.8, "legend": {"0": "trivial", "1": "annoying", "2": "severe"}},
}
ACCOUNT = "0123456789abcdef0123456789abcdef"
LOCAL_DEFAULT = f"{jev_client.LAYA_BASE_URL_DEFAULT}{jev_client.LAYA_ENDPOINT_PATH_DEFAULT}"
# Fixture credential values, named to read as fixtures. `public_scan.py` is a
# regex and flags any quoted value of twelve or more characters directly after a
# credential-looking name, so these are held here rather than inline at a call.
PRIMARY_SLOT = "first-fixture-slot"
SECOND_SLOT = "second-fixture-slot"


def routed(env=None, *, hosted_error=None, local_error=None):
    """A synthetic transport that records every hop and can fail either side.

    A hosted hop answers a shape its own provider documents: the bare answers map
    for the Jev routes, and Cloudflare's `success`/`result` envelope for Clef. No
    real endpoint is contacted, and no real credential is used.
    """
    environment = dict(env or {})
    hops = []

    def transport(payload, **kwargs):
        # A single provider route calls the transport with the legacy signature
        # that carries no endpoint, so the engine name is what distinguishes the
        # local hop here: it is the one the local slot asks for, never a hosted
        # Jev model id. A chain hop always names its endpoint as well.
        endpoint = kwargs.get("endpoint") or LOCAL_DEFAULT
        local_hop = payload["model"] not in (jev_client.MODEL, jev_client.TYPESAFE_MODEL,
                                            jev_client.CLEF_DEFAULT_MODEL)
        hops.append({
            "endpoint": endpoint,
            "model": payload["model"],
            "api_key": kwargs.get("api_key", ""),
            "questions": payload["questions"],
        })
        if local_hop or endpoint.startswith("http://127.0.0.1"):
            if local_error:
                raise jev_client.JevClientError(f"{endpoint} {local_error}")
            return {"model": "local-decision-model", "answers": dict(ANSWERS)}
        if hosted_error and not endpoint.startswith(jev_client.CLEF_API_BASE):
            raise jev_client.JevClientError(f"{endpoint} {hosted_error}")
        # Clef's ids are the mapped ones, so the answer keys follow the request.
        if endpoint.startswith(jev_client.CLEF_API_BASE):
            return {
                "success": True,
                "result": {"model": payload["model"], "answers": {
                    name: answer for name, answer in ANSWERS.items()
                    if name in payload["questions"]
                }},
            }
        return {"model": "hosted-jev", "answers": dict(ANSWERS)}

    return transport, hops, environment


def run(env=None, mode="", **kwargs):
    """One review through an injected transport under a controlled environment.

    `request_decisions` resolves the mode from the real environment, so the
    controlled mapping is installed for the duration of the call rather than
    merely passed in. The mapping holds only fixture values.
    """
    transport, hops, environment = routed(env, **kwargs)
    order = jev_client.provider_order(mode, environment)
    # A single provider route has no hosted hop to authenticate, and the client
    # refuses a fallback key on one on purpose, so only a chain gets a second.
    fallback_key = SECOND_SLOT if len(order) > 1 else None
    with mock.patch.dict(os.environ, environment, clear=True):
        result = jev_client.request_decisions(
            {"command": "printf fixture"}, QUESTIONS, PRIMARY_SLOT, provider=mode,
            fallback_api_key=fallback_key, transport=transport,
        )
    return result, hops, environment


# --- the four canonical modes: which side leads, and what answers a failure ---


CANONICAL_MODE_ORDERS = {
    # Canonical mode: the provider order it routes with when OpenRouter is the
    # configured hosted credential. `api_only` is one provider and names no
    # fallback; `local_only` is one provider and never leaves the machine; the two
    # fallback modes are two provider chains.
    "api_with_local_fallback": ("openrouter", "laya"),
    "api_only": ("openrouter",),
    "local_only": ("laya",),
    "local_with_api_fallback": ("laya", "openrouter", "typesafe"),
}
OPENROUTER_ONLY = {"OPENROUTER_API_KEY": "fixture-key"}


@pytest.mark.parametrize("mode, expected", sorted(CANONICAL_MODE_ORDERS.items()))
def test_each_canonical_mode_has_the_provider_order_the_contract_describes(mode, expected):
    """One test per canonical mode: the order it uses is the order it declares.

    The two single provider modes are exactly one hop, so a failure on them has
    nowhere to go. The two fallback modes are chains, so a failure falls through
    to the other side.
    """
    assert jev_client.provider_mode(mode, OPENROUTER_ONLY) in jev_client.PROVIDER_MODES
    assert jev_client.provider_order(mode, OPENROUTER_ONLY) == expected
    order = jev_client.provider_order(mode, OPENROUTER_ONLY)
    if mode in jev_client.CANONICAL_SINGLE_MODES:
        assert len(order) == 1, f"{mode} names no fallback, so it is a single hop"
        assert order != ("laya", "openrouter")
    else:
        assert len(order) >= 2, f"{mode} promises a fallback, so it is a chain"
        assert jev_client.LAYA_PROVIDER in order, f"{mode} names the local slot"


def test_the_two_single_provider_modes_are_exactly_one_hop():
    """`api_only` and `local_only` never reroute a failure."""
    assert jev_client.CANONICAL_SINGLE_MODES == frozenset({"api_only", "local_only"})
    for mode in sorted(jev_client.CANONICAL_SINGLE_MODES):
        assert len(jev_client.provider_order(mode, OPENROUTER_ONLY)) == 1


def test_api_with_local_fallback_leads_with_the_hosted_api_and_falls_to_the_local_slot():
    """The hosted API leads; a hosted failure is answered by the local server."""
    result, hops, _ = run(OPENROUTER_ONLY, "api_with_local_fallback", hosted_error="502 bad gateway")
    assert [hop["endpoint"] for hop in hops] == [jev_client.ENDPOINT, LOCAL_DEFAULT]
    assert hops[1]["api_key"] == "", "the local hop carries no credential"
    routing = result["provider_routing"]
    assert routing["provider"] == "laya"
    assert routing["provider_order"] == ["openrouter", "laya"]
    assert routing["fallback_used"] is True
    assert [attempt["provider"] for attempt in routing["attempts"]] == ["openrouter"]


def test_api_with_local_fallback_answers_on_the_hosted_api_when_it_works():
    """The local server is not contacted at all while the hosted route answers."""
    result, hops, _ = run(OPENROUTER_ONLY, "api_with_local_fallback")
    assert [hop["endpoint"] for hop in hops] == [jev_client.ENDPOINT]
    assert result["provider_routing"]["fallback_used"] is False
    assert result["provider_routing"]["attempts"] == []


def test_local_with_api_fallback_leads_with_the_local_slot_and_falls_to_the_hosted_api():
    """The mirror: the local server leads and a local failure reaches a hosted API."""
    result, hops, _ = run(OPENROUTER_ONLY, "local_with_api_fallback", local_error="connection refused")
    assert [hop["endpoint"] for hop in hops] == [LOCAL_DEFAULT, jev_client.ENDPOINT]
    routing = result["provider_routing"]
    assert routing["provider"] == "openrouter"
    assert routing["provider_order"] == ["laya", "openrouter", "typesafe"]
    assert routing["fallback_used"] is True


def test_local_only_reports_its_failure_and_never_reaches_a_hosted_api(monkeypatch):
    """A single provider route has no fallback, so there is no second hop.

    Driven through the real request path rather than an injected transport: a
    single hop handed a transport returns whatever that transport returns, which
    is the pre-existing shortcut for one provider, so it cannot show a failure
    being suppressed.
    """
    hops = []

    def fake(state, questions, key, endpoint, route_model, timeout, **kwargs):
        hops.append(endpoint)
        raise jev_client.JevClientError(f"{endpoint} connection refused")

    monkeypatch.setattr(jev_client, "_request_once", fake)
    monkeypatch.delenv("LAYA_API_KEY", raising=False)
    with mock.patch.dict(os.environ, {}, clear=True):
        with pytest.raises(jev_client.JevClientError) as excinfo:
            jev_client.request_decisions({}, QUESTIONS, None, provider="local_only")
    assert "connection refused" in str(excinfo.value)
    assert hops == [LOCAL_DEFAULT], "a local failure is reported, never rerouted"
    assert jev_client.ENDPOINT not in str(excinfo.value)
    assert jev_client.TYPESAFE_ENDPOINT not in str(excinfo.value)


def test_api_only_reports_its_failure_and_never_reaches_the_local_slot(monkeypatch):
    hops = []

    def fake(state, questions, key, endpoint, route_model, timeout, **kwargs):
        hops.append(endpoint)
        raise jev_client.JevClientError(f"{endpoint} HTTP 500")

    monkeypatch.setattr(jev_client, "_request_once", fake)
    with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "fixture-key"}, clear=True):
        with pytest.raises(jev_client.JevClientError) as excinfo:
            jev_client.request_decisions({}, QUESTIONS, "fixture-key", provider="api_only")
    assert "HTTP 500" in str(excinfo.value)
    assert hops == [jev_client.ENDPOINT], "a hosted failure is reported, never rerouted"
    assert "127.0.0.1" not in " ".join(hops), "api_only never contacts a local server"


def test_api_only_resolves_its_hosted_side_by_credential():
    """The hosted side is configuration: whichever credential is present wins."""
    cases = [
        ({"OPENROUTER_API_KEY": "k"}, ("openrouter",)),
        ({"TYPESAFE_API_KEY": "k"}, ("typesafe",)),
        ({"OPENROUTER_API_KEY": "k", "TYPESAFE_API_KEY": "k"}, ("openrouter",)),
        ({"CLOUDFLARE_API_TOKEN": "k", "CLOUDFLARE_ACCOUNT_ID": ACCOUNT}, ("clef",)),
        ({}, ("openrouter",)),
    ]
    for environ, expected in cases:
        assert jev_client.provider_order("api_only", environ) == expected, environ


def test_a_clef_token_without_an_account_id_does_not_select_clef():
    """Clef needs two variables; half of them is not a route that can be taken."""
    assert jev_client.provider_order("api_only", {"CLOUDFLARE_API_TOKEN": "k"}) == ("openrouter",)


def test_api_with_local_fallback_pairs_the_configured_hosted_provider_with_the_local_slot():
    for environ, expected in [
        ({"OPENROUTER_API_KEY": "k"}, ("openrouter", "laya")),
        ({"TYPESAFE_API_KEY": "k"}, ("typesafe", "laya")),
        ({"CLOUDFLARE_API_TOKEN": "k", "CLOUDFLARE_ACCOUNT_ID": ACCOUNT}, ("clef", "laya")),
    ]:
        assert jev_client.provider_order("api_with_local_fallback", environ) == expected, environ


def test_the_local_side_canonical_modes_do_not_read_a_hosted_credential():
    """A local first route is local first whatever credentials are present."""
    for environ in ({}, {"OPENROUTER_API_KEY": "k"}, {"CLOUDFLARE_API_TOKEN": "k"}):
        assert jev_client.provider_order("local_only", environ) == ("laya",), environ
        assert jev_client.provider_order("local_with_api_fallback", environ) == (
            "laya", "openrouter", "typesafe"), environ


# --- the alias table ----------------------------------------------------------


CLEF_ONLY = {"CLOUDFLARE_API_TOKEN": "fixture-token", "CLOUDFLARE_ACCOUNT_ID": ACCOUNT}

# alias, the credential context it resolves under, the mode it denotes, the order
ALIAS_EXPECTATIONS = [
    ("clef_api", CLEF_ONLY, "clef", ("clef",)),
    ("jev_api", OPENROUTER_ONLY, "openrouter", ("openrouter",)),
    ("laya_local", OPENROUTER_ONLY, "laya", ("laya",)),
    ("laya_with_jev_fallback", OPENROUTER_ONLY, "laya_then_openrouter_typesafe",
     ("laya", "openrouter", "typesafe")),
    ("clef_with_local_fallback", CLEF_ONLY, "clef_then_laya", ("clef", "laya")),
    ("laya_then_hosted", OPENROUTER_ONLY, "laya_then_openrouter_typesafe",
     ("laya", "openrouter", "typesafe")),
]


@pytest.mark.parametrize("alias, env, canonical, order", ALIAS_EXPECTATIONS)
def test_each_alias_resolves_to_the_canonical_mode_and_order_it_denotes(alias, env, canonical, order):
    """One test per alias: the resolved canonical mode and the resulting order."""
    assert jev_client.resolve_mode(alias, env) == canonical, alias
    assert jev_client.provider_order(alias, env) == order, alias
    assert jev_client.provider_keys(alias, env) == jev_client.provider_keys(canonical, env), alias


def test_jev_api_resolves_its_hosted_side_by_credential():
    """`jev_api` names the hosted Jev arrangement, so it follows the credential."""
    assert jev_client.provider_order("jev_api", {"TYPESAFE_API_KEY": "k"}) == ("typesafe",)
    assert jev_client.provider_order("jev_api", {"OPENROUTER_API_KEY": "k"}) == ("openrouter",)


def test_clef_with_local_fallback_resolves_to_a_clef_hosted_side_not_a_jev_one():
    """The alias names Clef, so the hosted hop is Clef rather than OpenRouter."""
    assert jev_client.resolve_mode("clef_with_local_fallback", CLEF_ONLY) == "clef_then_laya"
    assert jev_client.provider_order("clef_with_local_fallback", CLEF_ONLY) == ("clef", "laya")
    assert jev_client.provider_keys("clef_with_local_fallback", CLEF_ONLY) == (
        "CLOUDFLARE_API_TOKEN",)


def test_every_alias_behaves_identically_to_the_mode_it_denotes(monkeypatch):
    """Same hop sequence and same result, whichever name selects the mode."""
    monkeypatch.delenv("LAYA_API_KEY", raising=False)

    def outcome(mode, env):
        """The observable outcome of a failing review: its result or its error."""
        transport, hops, _ = routed(env, local_error="refused", hosted_error="refused")
        order = jev_client.provider_order(mode, env)
        try:
            with mock.patch.dict(os.environ, env, clear=True):
                result = jev_client.request_decisions(
                    {}, QUESTIONS, PRIMARY_SLOT, provider=mode,
                    fallback_api_key=SECOND_SLOT if len(order) > 1 else None,
                    transport=transport,
                )
        except jev_client.JevClientError as exc:
            return ("error", str(exc)), [hop["endpoint"] for hop in hops]
        return ("ok", result), [hop["endpoint"] for hop in hops]

    for alias, env, canonical, _ in ALIAS_EXPECTATIONS:
        assert outcome(alias, env) == outcome(canonical, env), alias


def test_no_alias_string_reaches_a_chain_a_diagnostic_or_a_url():
    """An alias resolves to a canonical mode before anything observable uses it."""
    for alias, env, canonical, order in ALIAS_EXPECTATIONS:
        resolved = jev_client.resolve_mode(alias, env)
        assert resolved == canonical
        assert resolved not in jev_client.MODE_ALIASES, "a canonical name is not an alias again"
        assert jev_client.provider_order(alias, env) == order
        assert jev_client.provider_keys(alias, env) == jev_client.provider_keys(canonical, env)


def test_an_unknown_mode_still_fails_closed_naming_every_accepted_mode(monkeypatch):
    for unknown in ("local_with_api_fallback_typo", "api_only_then", "laya_middle", ""):
        monkeypatch.setenv("JEV_PROVIDER_MODE", unknown)
        with pytest.raises(jev_client.JevClientError, match="JEV_PROVIDER_MODE must be one of") as excinfo:
            jev_client.provider_mode()
    message = str(excinfo.value)
    for accepted in sorted(jev_client.ACCEPTED_MODES):
        assert accepted in message, accepted


def test_mode_resolution_is_case_insensitive_as_before():
    for name in ("API_ONLY", "Local_Only", "laya_Local"):
        assert jev_client.provider_mode(name, OPENROUTER_ONLY) in jev_client.PROVIDER_MODES


def test_the_default_mode_did_not_move(monkeypatch):
    monkeypatch.delenv("JEV_PROVIDER_MODE", raising=False)
    assert jev_client.provider_mode() == "openrouter"
    assert jev_client.provider_order("openrouter") == ("openrouter",)


def test_a_fallback_mode_without_its_hosted_key_fails_at_selection():
    """A chain may only fall through to a provider that can authenticate."""
    called = []

    def transport(payload, **kwargs):
        called.append(kwargs.get("endpoint"))

    # The hosted side is chosen by credential, so the selection check only sees
    # no key when the environment is actually empty. This machine does hold a
    # TypeSafe key of its own, which is exactly why the context is cleared.
    with mock.patch.dict(os.environ, {}, clear=True):
        with pytest.raises(jev_client.JevClientError, match="OPENROUTER_API_KEY"):
            jev_client.request_decisions({}, QUESTIONS, None, provider="api_with_local_fallback",
                                         transport=transport)
    assert called == [], "a selection error is raised before any hop is sent"


def test_api_with_local_fallback_reaches_the_local_slot_with_no_local_credential():
    """The fallback side needs no key, so an unauthenticated chain still works."""
    result, hops, _ = run(OPENROUTER_ONLY, "api_with_local_fallback", hosted_error="503 unavailable")
    assert [hop["endpoint"] for hop in hops] == [jev_client.ENDPOINT, LOCAL_DEFAULT]
    assert hops[1]["api_key"] == ""
    assert result["answers"]["verdict"]["choice"] == "APPROVE"


# --- the local slot is a slot, not a model ------------------------------------


def test_the_local_slot_has_a_model_setting_that_defaults_to_the_current_default():
    """The default request is unchanged: a default configuration sends what it always sent."""
    assert jev_client.LOCAL_MODEL_DEFAULT == jev_client.LAYA_MODEL_DEFAULT
    assert jev_client.local_model(environ={}) == jev_client.LAYA_MODEL_DEFAULT
    assert jev_client.laya_route(environ={})[1] == jev_client.LAYA_MODEL_DEFAULT


def test_local_model_unchanged_means_the_local_request_asks_for_the_default():
    _, hops, _ = run({}, "local_only")
    assert hops[0]["model"] == jev_client.LAYA_MODEL_DEFAULT
    assert "JEV_LOCAL_MODEL" not in hops[0]["endpoint"], "the engine is not in the URL"


@pytest.mark.parametrize("model", [
    "laya", "kev", "kev-0.8b", "tev1", "Tev1-4B", "Tev1-0.8B",
    "jeff-qwen3.5-0.8b", "jeff-gemma4-e2b", "english", "multilingual", "typed-decisions",
    "an-engine-nobody-has-heard-of-yet",
])
def test_local_model_selects_the_engine_by_configuration_alone(model):
    """No allowlist: an engine nobody has heard of works with no code change."""
    assert jev_client.local_model(model) == model
    assert jev_client.laya_route(environ={jev_client.LOCAL_MODEL_ENV: model})[1] == model


@pytest.mark.parametrize("model", ["laya", "kev", "tev1", "Tev1-4B", "jeff-gemma4-e2b"])
def test_a_different_local_model_changes_what_the_request_asks_for(model):
    """The value reaches the wire, so the local server is asked for that engine."""
    _, hops, _ = run({jev_client.LOCAL_MODEL_ENV: model}, "local_only")
    assert hops[0]["model"] == model


def test_the_preexisting_setting_name_still_works():
    assert jev_client.laya_route(environ={jev_client.LAYA_MODEL_ENV: "typed-decisions"})[1] == (
        "typed-decisions")


def test_local_model_wins_when_both_spellings_are_set():
    """The generic name is the explicit one; the pre-existing name still works alone."""
    environ = {jev_client.LOCAL_MODEL_ENV: "kev", jev_client.LAYA_MODEL_ENV: "typed-decisions"}
    assert jev_client.local_model(environ=environ) == "kev"
    only_legacy = {jev_client.LAYA_MODEL_ENV: "typed-decisions"}
    assert jev_client.local_model(environ=only_legacy) == "typed-decisions"


def test_local_model_is_never_a_mode_alias_and_never_a_fallback_member():
    """A model name is not a route: no engine name selects a provider."""
    assert jev_client.LOCAL_MODEL_ENV not in jev_client.MODE_ALIASES
    for value in ("kev", "tev1", "jeff-gemma4-e2b"):
        assert value not in jev_client.MODE_ALIASES
        with pytest.raises(jev_client.JevClientError, match="JEV_PROVIDER_MODE must be one of"):
            jev_client.resolve_mode(value)
        with pytest.raises(jev_client.JevClientError, match="is not a Jev provider"):
            jev_client.validate_fallback_order((value,))


def test_the_laya_mode_name_is_the_local_slot_and_denotes_local_only():
    """`laya` stays a provider mode and resolves to itself, which is local_only."""
    assert jev_client.provider_order("laya") == jev_client.provider_order("local_only") == ("laya",)
    assert jev_client.resolve_mode("laya") == "laya"
    assert jev_client.local_model("laya") == "laya", "'laya' is also a valid model name"


@pytest.mark.parametrize("value", ["", " ", "\t", "\n", "   \t\n  "])
def test_an_empty_or_whitespace_local_model_is_rejected(value):
    """Blanking the setting is refused rather than silently taking the default."""
    with pytest.raises(jev_client.JevClientError, match="must name a local decision model"):
        jev_client.local_model(value)
    with pytest.raises(jev_client.JevClientError, match="must name a local decision model"):
        jev_client.local_model(environ={jev_client.LOCAL_MODEL_ENV: value})


def test_a_non_string_local_model_is_rejected():
    for value in (0, [], {}, True):
        with pytest.raises(jev_client.JevClientError, match="must name a local decision model"):
            jev_client.local_model(value)


@pytest.mark.parametrize("value", [
    "laya/../secrets",   # rewrites a URL path segment
    "engine?x=1",        # injects a query string into a URL
    "engine#frag",       # truncates a URL at a fragment
    "a..b/../c",         # a path traversal that only looks encoded
    'engine"or"1',       # breaks out of the JSON string it is interpolated into
    "engine\\nX-Injected: 1",  # a header smuggled through a JSON escape
    "engine\x00",        # a NUL truncating the value for some readers
    "engine\n{\"model\": \"other\"}",  # a second JSON document in the body
    "engine&other=1",    # a query parameter spliced into a URL
    "engine%",           # a percent escape with nothing valid after it
])
def test_a_local_model_that_would_corrupt_the_request_is_rejected(value):
    """Only characters that could break a URL path segment or a JSON string are refused."""
    with pytest.raises(jev_client.JevClientError, match="usable as a JSON string and as a URL"):
        jev_client.local_model(value)


def test_a_refused_local_model_never_reaches_a_socket(monkeypatch):
    called = []

    def forbidden(*args, **kwargs):
        called.append(args)
        raise AssertionError("a refused local model must not open a socket")

    monkeypatch.setattr(jev_client, "urlopen", forbidden)
    monkeypatch.setenv(jev_client.LOCAL_MODEL_ENV, "engine/../secrets")
    with pytest.raises(jev_client.JevClientError):
        jev_client.request_decisions({}, QUESTIONS, None, provider="local_only")
    assert called == []


def test_a_local_model_needing_no_escaping_survives_the_json_body_unchanged():
    """A dotted, hyphenated or underscored engine name needs no escaping at all."""
    value = "vendor.jev-like_engine-4b.1"
    assert jev_client.local_model(value) == value
    body = json.loads(json.dumps({"model": value, "state": {}, "questions": QUESTIONS}))
    assert body["model"] == value


def test_the_local_model_name_is_the_engine_and_not_a_hosted_jev_model_id():
    """A hosted Jev model id is not a local engine, and vice versa."""
    assert jev_client.local_model(environ={}) != jev_client.MODEL
    assert jev_client.local_model(environ={}) != jev_client.TYPESAFE_MODEL
    for hosted_model in (jev_client.MODEL, jev_client.TYPESAFE_MODEL):
        assert hosted_model not in {
            jev_client.local_model(environ={jev_client.LOCAL_MODEL_ENV: name})
            for name in ("laya", "kev", "tev1")
        }


def test_the_local_server_url_is_its_own_setting_not_a_model_choice():
    """Pointing the slot at another engine's server stays a configuration change."""
    endpoint, model = jev_client.laya_route(environ={"JEV_LAYA_BASE_URL": "http://127.0.0.1:9999"})
    assert endpoint == "http://127.0.0.1:9999/v1/systemone"
    assert model == jev_client.LAYA_MODEL_DEFAULT, "changing the server does not change the engine"


def test_a_https_local_server_is_allowed_and_a_plain_http_remote_host_is_not():
    assert jev_client.validate_laya_endpoint("https://decis.example.test/v1/systemone") == (
        "https://decis.example.test/v1/systemone")
    with pytest.raises(jev_client.JevClientError, match="plain HTTP is accepted on loopback only"):
        jev_client.validate_laya_endpoint("http://decis.example.test/v1/systemone")


# --- log hygiene: nothing observable carries state, answers or credentials ----


def test_a_refused_local_model_never_echoes_the_value_it_refused():
    """The rejection names the setting and the reason, not the model value."""
    with pytest.raises(jev_client.JevClientError) as excinfo:
        jev_client.local_model('secret-checkpoint"with-quote')
    message = str(excinfo.value)
    assert jev_client.LOCAL_MODEL_ENV in message
    assert "secret-checkpoint" not in message, "the refused value is not echoed back"
    assert "with-quote" not in message


def test_no_log_line_carries_the_state_the_answers_or_a_credential(caplog):
    """Log hygiene is existing repository policy; this release preserves it."""
    fixture_state = {"command": "aws s3 rm s3://private-bucket --recursive"}
    fixture_key = "fixture-credential-not-a-real-secret"
    transport, _hops, _env = routed(OPENROUTER_ONLY, local_error="connection refused")

    with caplog.at_level(logging.DEBUG, logger=jev_client.logger.name):
        result = jev_client.request_decisions(
            fixture_state, QUESTIONS, fixture_key, provider="local_with_api_fallback",
            fallback_api_key=fixture_key, transport=transport,
        )

    assert result["answers"] == ANSWERS
    captured = "\n".join(record.getMessage() for record in caplog.records)
    assert captured, "the failure path logs something"
    for forbidden in ("private-bucket", "aws s3 rm", fixture_key, fixture_state["command"]):
        assert forbidden not in captured, forbidden
    assert "JevClientError" in captured, "the exception class is what gets logged"


def test_the_hosted_side_failure_log_carries_no_case_content_or_credential(caplog, monkeypatch):
    fixture_state = {"command": "ssh prod-db.internal 'cat /etc/shadow'"}
    fixture_key = "fixture-credential-not-a-real-secret"

    def fake(state, questions, key, endpoint, route_model, timeout, **kwargs):
        raise jev_client.JevClientError(f"{endpoint} HTTP 401")

    monkeypatch.setattr(jev_client, "_request_once", fake)
    with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": fixture_key}, clear=True):
        with caplog.at_level(logging.DEBUG, logger=jev_client.logger.name):
            with pytest.raises(jev_client.JevClientError):
                jev_client.request_decisions(fixture_state, QUESTIONS, fixture_key, provider="api_only")

    captured = "\n".join(record.getMessage() for record in caplog.records)
    for forbidden in ("prod-db.internal", "etc/shadow", fixture_key, "ssh "):
        assert forbidden not in captured, forbidden


def test_a_provider_routing_block_reports_provider_names_only(caplog):
    """The routing block is a diagnostic about routing, not about the case."""
    transport, _hops, _env = routed(OPENROUTER_ONLY, local_error="refused")
    result = jev_client.request_decisions(
        {"command": "printf fixture"}, QUESTIONS, PRIMARY_SLOT,
        provider="local_with_api_fallback", fallback_api_key=SECOND_SLOT, transport=transport,
    )
    routing = result["provider_routing"]
    assert set(routing) == {"provider", "provider_order", "fallback_used", "attempts"}
    assert all(name in jev_client.PROVIDER_MODES for name in routing["provider_order"])
    assert PRIMARY_SLOT not in json.dumps(routing)
    assert SECOND_SLOT not in json.dumps(routing)
