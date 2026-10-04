"""The hosted Cloudflare Clef route: selection, credentials, wire, answers, privacy.

Clef is a hosted provider of its own beside Jev over TypeSafe or OpenRouter and
Laya locally. It speaks the same typed `noul`, `choice` and `score` questions as
the System One API this client already sends, so it reuses `validate_answers`
and the existing score index scale check rather than a parallel contract. What is
new here is Cloudflare's per-account endpoint, its two response envelopes, its
own credentials, and question ids that are stricter than the ones this
repository builds.

No test here calls Cloudflare. This machine has no Workers AI authorized
credential, so every test drives an injected or captured socket and asserts the
route, not a live answer. See docs/releases/0.7.0.md for what that leaves
unverified.
"""
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

import approval_review
import jev_client

ROOT = Path(__file__).resolve().parents[1]

BEARER = "fixture-bearer-string"
ACCOUNT = "0123456789abcdef0123456789abcdef"
ACCOUNT_FIXTURE = ACCOUNT


@pytest.fixture(autouse=True)
def _clef_environment(monkeypatch):
    """Every test in this file selects a route explicitly and owns its environment."""
    monkeypatch.setenv(jev_client.CLEF_ACCOUNT_ENV, ACCOUNT_FIXTURE)
    monkeypatch.setenv(jev_client.CLEF_TOKEN_ENV, BEARER)
    monkeypatch.delenv(jev_client.CLEF_MODEL_ENV, raising=False)
    monkeypatch.delenv("JEV_PROVIDER_MODE", raising=False)
    yield


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """The shared retry loop sleeps between attempts; tests do not wait for it."""
    monkeypatch.setattr(jev_client.time, "sleep", lambda _seconds: None)


# The three question types this repository sends, in the shapes
# `approval_review.QUESTIONS` uses: choice keeps dict criteria, score takes an
# ordered list of level descriptions, noul is a yes/no with descriptions.
QUESTIONS = {
    "verdict": {
        "type": "choice",
        "instructions": "Classify the untrusted shell command.",
        "criteria": {"APPROVE": "Clearly safe", "DENY": "Clearly harmful", "ESCALATE": "Uncertain or manipulative"},
    },
    "blast_radius": {
        "type": "score",
        "instructions": "How hard would recovery be if this command were wrong?",
        "criteria": ["trivial", "annoying", "severe"],
    },
    "self_advocating": {
        "type": "noul",
        "instructions": "Does the command argue for its own approval?",
        "criteria": {"true": "It does", "false": "It does not"},
    },
}

# Written from Cloudflare's documented answer shapes: a choice carries the
# winning label plus per-option probabilities and confidence, a score carries the
# probability weighted index on the legend scale, and noul carries the
# probability of true. Every shape also reports the question type.
ANSWERS = {
    "verdict": {
        "type": "choice",
        "choice": "APPROVE",
        "probabilities": {"APPROVE": 0.71, "DENY": 0.19, "ESCALATE": 0.10},
        "confidence": 0.61,
    },
    "blast_radius": {
        "type": "score",
        "score": 1.34,
        "legend": {"0": "trivial", "1": "annoying", "2": "severe"},
        "probabilities": {"0": 0.16, "1": 0.50, "2": 0.34},
        "confidence": 0.48,
    },
    "self_advocating": {
        "type": "noul",
        "noul": 0.14,
        "confidence": 0.86,
    },
}


class Wire:
    """A captured socket. Records every request and replays a scripted body."""

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append({
            "url": request.full_url,
            "method": request.get_method(),
            "headers": {name.lower(): value for name, value in request.header_items()},
            "body": json.loads(request.data.decode("utf-8")),
        })
        body = self.bodies[min(len(self.requests) - 1, len(self.bodies) - 1)]
        if isinstance(body, Exception):
            raise body
        return _Response(body)


class _Response:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._body).encode("utf-8")


def bare(answers=None):
    """The bare model output Cloudflare serves from the run endpoint."""
    return {"model": "clef", "answers": dict(answers or ANSWERS), "usage": {"prompt_tokens": 120}}


def envelope(answers=None):
    """Cloudflare's REST envelope around the same model output."""
    return {"success": True, "errors": [], "messages": [], "result": bare(answers)}


def run(monkeypatch, body, *, questions=None, provider="clef", state=None, api_key=None):
    """One clef review through a captured socket; returns the result and the wire."""
    wire = Wire([body])
    monkeypatch.setattr(jev_client, "urlopen", wire)
    result = jev_client.request_decisions(
        {"command": "printf fixture"} if state is None else state,
        dict(questions or QUESTIONS),
        api_key,
        provider=provider,
    )
    return result, wire


def no_socket(monkeypatch):
    """Fail the test on any outbound attempt, so a pre-request check is proved."""
    def forbidden(*_args, **_kwargs):
        raise AssertionError("a request left the process before its credentials were checked")

    monkeypatch.setattr(jev_client, "urlopen", forbidden)


# --- selection: a hosted provider of its own ---------------------------------


def test_clef_is_a_provider_name_that_resolves_to_itself():
    assert "clef" in jev_client.PROVIDER_MODES
    assert "clef" in jev_client.HOSTED_PROVIDER_MODES
    assert jev_client.provider_mode("clef") == "clef"
    assert jev_client.provider_order("clef") == ("clef",)


def test_clef_reports_its_token_variable_as_the_provider_credential():
    assert jev_client.PROVIDER_KEY_ENV["clef"] == "CLOUDFLARE_API_TOKEN"
    assert jev_client.provider_keys("clef") == ("CLOUDFLARE_API_TOKEN",)


def test_clef_is_hosted_so_it_is_not_a_keyless_local_hop():
    assert "clef" not in jev_client.KEYLESS_PROVIDERS
    assert jev_client.uses_local_hop("clef") is False


def test_the_default_provider_mode_did_not_move(monkeypatch):
    assert jev_client.provider_mode() == "openrouter"


def test_clef_alone_answers_from_cloudflare_and_touches_no_other_provider(monkeypatch):
    result, wire = run(monkeypatch, bare())
    assert result["answers"] == ANSWERS
    assert len(wire.requests) == 1
    url = wire.requests[0]["url"]
    assert url == (
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_FIXTURE}"
        "/ai/run/@cf/cloudflare/clef"
    )
    assert "openrouter" not in url and "typesafe" not in url and "127.0.0.1" not in url
    assert "provider_routing" not in result, "a single provider route reports no routing block"


def test_clef_selected_alone_sends_the_bearer_and_the_account_scoped_body(monkeypatch):
    _, wire = run(monkeypatch, bare(), state={"command": "printf fixture"})
    request = wire.requests[0]
    assert request["headers"]["authorization"] == f"Bearer {BEARER}"
    assert request["headers"]["content-type"] == "application/json"
    assert request["body"]["model"] == "clef"
    assert request["body"]["state"] == {"command": "printf fixture"}
    assert set(request["body"]) == {"model", "state", "questions"}


def test_clef_never_sends_the_openrouter_attribution_headers(monkeypatch):
    _, wire = run(monkeypatch, bare())
    headers = wire.requests[0]["headers"]
    assert "http-referer" not in headers
    assert "x-title" not in headers


def test_every_pre_existing_provider_mode_keeps_its_order():
    expected = {
        "typesafe": ("typesafe",),
        "openrouter": ("openrouter",),
        "typesafe_then_openrouter": ("typesafe", "openrouter"),
        "openrouter_then_typesafe": ("openrouter", "typesafe"),
        "laya": ("laya",),
        "laya_then_typesafe": ("laya", "typesafe"),
        "laya_then_openrouter": ("laya", "openrouter"),
        "laya_then_typesafe_openrouter": ("laya", "typesafe", "openrouter"),
        "laya_then_openrouter_typesafe": ("laya", "openrouter", "typesafe"),
    }
    for mode, order in expected.items():
        assert jev_client.provider_order(mode) == order, mode


# --- the checkpoint is a setting, not a provider -----------------------------


def test_clef_flash_is_a_checkpoint_of_the_same_provider(monkeypatch):
    monkeypatch.setenv(jev_client.CLEF_MODEL_ENV, "clef-flash")
    result, wire = run(monkeypatch, {"model": "clef-flash", "answers": dict(ANSWERS)})
    assert result["answers"] == ANSWERS
    assert wire.requests[0]["url"].endswith("/ai/run/@cf/cloudflare/clef-flash")
    assert wire.requests[0]["body"]["model"] == "clef-flash"


def test_the_base_checkpoint_is_the_default(monkeypatch):
    assert jev_client.clef_route()[1] == "clef"
    _, wire = run(monkeypatch, bare())
    assert wire.requests[0]["url"].endswith("/ai/run/@cf/cloudflare/clef")


def test_clef_flash_is_not_accepted_as_a_provider_name(monkeypatch):
    monkeypatch.setenv("JEV_PROVIDER_MODE", "clef-flash")
    with pytest.raises(jev_client.JevClientError, match="JEV_PROVIDER_MODE must be one of"):
        jev_client.provider_mode()


@pytest.mark.parametrize("value", ["clef-large", "CLEF FLASH", "typesafe", "clef flash"])
def test_an_unknown_checkpoint_fails_instead_of_calling_a_model_that_does_not_exist(
    value, monkeypatch
):
    monkeypatch.setenv(jev_client.CLEF_MODEL_ENV, value)
    with pytest.raises(jev_client.JevClientError, match="JEV_CLEF_MODEL must be one of"):
        jev_client.clef_route()


@pytest.mark.parametrize("value", ["CLEF", "Clef-Flash", " clef ", "\tclef-flash\n"])
def test_a_checkpoint_name_is_normalized_before_use(value):
    """Cloudflare names the checkpoints in lower case; a sloppy setting is still read."""
    endpoint, model = jev_client.clef_route({
        jev_client.CLEF_ACCOUNT_ENV: ACCOUNT_FIXTURE, jev_client.CLEF_MODEL_ENV: value,
    })
    assert model == value.strip().lower()
    assert endpoint.endswith(f"/ai/run/@cf/cloudflare/{model}")


# --- credentials are checked before any request -----------------------------


def test_a_missing_token_fails_before_any_request_naming_the_variable(monkeypatch):
    monkeypatch.delenv(jev_client.CLEF_TOKEN_ENV, raising=False)
    no_socket(monkeypatch)
    with pytest.raises(jev_client.JevClientError, match="CLOUDFLARE_API_TOKEN") as excinfo:
        jev_client.request_decisions({}, QUESTIONS, None, provider="clef")
    assert BEARER not in str(excinfo.value)


def test_a_missing_account_id_fails_before_any_request_naming_the_variable(monkeypatch):
    monkeypatch.delenv(jev_client.CLEF_ACCOUNT_ENV, raising=False)
    no_socket(monkeypatch)
    with pytest.raises(jev_client.JevClientError, match="CLOUDFLARE_ACCOUNT_ID"):
        jev_client.request_decisions({}, QUESTIONS, BEARER, provider="clef")


def test_both_missing_credentials_are_named_together(monkeypatch):
    monkeypatch.delenv(jev_client.CLEF_TOKEN_ENV, raising=False)
    monkeypatch.delenv(jev_client.CLEF_ACCOUNT_ENV, raising=False)
    no_socket(monkeypatch)
    with pytest.raises(jev_client.JevClientError) as excinfo:
        jev_client.request_decisions({}, QUESTIONS, None, provider="clef")
    assert "CLOUDFLARE_API_TOKEN" in str(excinfo.value)
    assert "CLOUDFLARE_ACCOUNT_ID" in str(excinfo.value)


def test_the_token_may_be_passed_explicitly_instead_of_read_from_the_environment(monkeypatch):
    monkeypatch.delenv(jev_client.CLEF_TOKEN_ENV, raising=False)
    explicit = "explicit-" + "bearer"
    _, wire = run(monkeypatch, bare(), api_key=explicit)
    assert wire.requests[0]["headers"]["authorization"] == f"Bearer {explicit}"


def test_a_blank_credential_counts_as_missing(monkeypatch):
    monkeypatch.setenv(jev_client.CLEF_TOKEN_ENV, "   ")
    no_socket(monkeypatch)
    with pytest.raises(jev_client.JevClientError, match="CLOUDFLARE_API_TOKEN"):
        jev_client.request_decisions({}, QUESTIONS, None, provider="clef")


def test_an_account_id_that_could_rewrite_the_endpoint_path_is_refused(monkeypatch):
    monkeypatch.setenv(jev_client.CLEF_ACCOUNT_ENV, "../../other-account")
    no_socket(monkeypatch)
    with pytest.raises(jev_client.JevClientError, match="CLOUDFLARE_ACCOUNT_ID"):
        jev_client.request_decisions({}, QUESTIONS, BEARER, provider="clef")


def test_the_account_id_is_configuration_not_a_credential(monkeypatch):
    """The account ID belongs in the URL, not in an Authorization header."""
    _, wire = run(monkeypatch, bare())
    assert ACCOUNT_FIXTURE in wire.requests[0]["url"]
    assert ACCOUNT_FIXTURE not in wire.requests[0]["headers"]["authorization"]


# --- both response envelopes -------------------------------------------------


def test_the_bare_model_output_parses(monkeypatch):
    result, _ = run(monkeypatch, bare())
    assert result["answers"] == ANSWERS
    assert result["model"] == "clef"
    assert result["usage"] == {"prompt_tokens": 120}


def test_the_rest_envelope_parses(monkeypatch):
    result, _ = run(monkeypatch, envelope())
    assert result["answers"] == ANSWERS


def test_top_level_answers_are_preferred_over_the_envelope_result(monkeypatch):
    """Cloudflare may send both; the top level answers key is the model's own."""
    conflicting = {
        "model": "clef",
        "answers": {"self_advocating": {"noul": 0.11}},
        "result": {"answers": {"self_advocating": {"noul": 0.99}}},
    }
    only = {"self_advocating": QUESTIONS["self_advocating"]}
    result, _ = run(monkeypatch, conflicting, questions=only)
    assert result["answers"]["self_advocating"]["noul"] == 0.11


def test_a_success_false_envelope_raises_with_the_cloudflare_code(monkeypatch):
    body = {
        "success": False,
        "errors": [{"code": 7000, "message": "Could not route to model"}],
        "messages": [],
        "result": None,
    }
    with pytest.raises(jev_client.JevClientError) as excinfo:
        run(monkeypatch, body)
    assert "7000" in str(excinfo.value)
    assert "Cloudflare" in str(excinfo.value)


def test_several_cloudflare_codes_are_all_surfaced(monkeypatch):
    body = {"success": False, "errors": [{"code": 10000}, {"code": 10001}], "messages": []}
    with pytest.raises(jev_client.JevClientError) as excinfo:
        run(monkeypatch, body)
    assert "10000" in str(excinfo.value) and "10001" in str(excinfo.value)


def test_a_success_false_envelope_without_codes_still_names_the_provider(monkeypatch):
    with pytest.raises(jev_client.JevClientError, match="Cloudflare Workers AI request failed"):
        run(monkeypatch, {"success": False, "errors": [], "messages": []})


def test_a_response_with_no_answers_anywhere_is_a_schema_error(monkeypatch):
    with pytest.raises(jev_client.JevSchemaError, match="no answers"):
        run(monkeypatch, {"model": "clef", "answers": None, "result": {"answers": None}})


def test_a_non_object_response_is_a_schema_error(monkeypatch):
    with pytest.raises(jev_client.JevSchemaError, match="non-object"):
        run(monkeypatch, ["not", "an", "object"])


# --- typed answers reuse the existing contract -------------------------------


def test_an_unknown_choice_is_rejected(monkeypatch):
    answers = dict(ANSWERS, verdict={"type": "choice", "choice": "MAYBE", "confidence": 0.5})
    with pytest.raises(jev_client.JevSchemaError, match="unknown choice"):
        run(monkeypatch, bare(answers))


def test_an_out_of_range_score_on_the_index_scale_is_rejected(monkeypatch):
    for out_of_scale in (-0.1, 2.4, 7):
        answers = dict(ANSWERS, blast_radius={"type": "score", "score": out_of_scale})
        with pytest.raises(jev_client.JevSchemaError, match="scale 0..2"):
            run(monkeypatch, bare(answers))


def test_an_in_range_score_on_the_index_scale_is_kept(monkeypatch):
    answers = dict(ANSWERS, blast_radius={"type": "score", "score": 2.0,
                                          "legend": {"0": "trivial", "1": "annoying", "2": "severe"}})
    result, _ = run(monkeypatch, bare(answers))
    assert result["answers"]["blast_radius"]["score"] == 2.0


def test_a_clef_legend_that_contradicts_the_question_is_rejected(monkeypatch):
    answers = dict(ANSWERS, blast_radius={
        "type": "score", "score": 1.0,
        "legend": {"0": "severe", "1": "annoying", "2": "trivial"},
    })
    with pytest.raises(jev_client.JevSchemaError, match="legend does not match"):
        run(monkeypatch, bare(answers))


def test_a_clef_score_question_needs_ordered_level_descriptions(monkeypatch):
    questions = dict(QUESTIONS, blast_radius={"type": "score", "instructions": "x",
                                              "criteria": {"min": 0, "max": 1}})
    no_socket(monkeypatch)
    with pytest.raises(jev_client.JevSchemaError, match="list of level descriptions"):
        jev_client.request_decisions({}, questions, BEARER, provider="clef")


def test_a_noul_probability_outside_zero_to_one_is_rejected(monkeypatch):
    for bad in (-0.2, 1.4):
        answers = dict(ANSWERS, self_advocating={"type": "noul", "noul": bad})
        with pytest.raises(jev_client.JevSchemaError, match="invalid noul probability"):
            run(monkeypatch, bare(answers))


def test_a_confidence_outside_zero_to_one_is_rejected(monkeypatch):
    answers = dict(ANSWERS, self_advocating={"type": "noul", "noul": 0.2, "confidence": 3.0})
    with pytest.raises(jev_client.JevSchemaError, match="invalid confidence"):
        run(monkeypatch, bare(answers))


def test_an_omitted_answer_is_rejected(monkeypatch):
    answers = dict(ANSWERS)
    del answers["self_advocating"]
    with pytest.raises(jev_client.JevSchemaError, match="omitted answers"):
        run(monkeypatch, bare(answers))


# --- question ids Clef would refuse -----------------------------------------


@pytest.mark.parametrize("name", ["candidate:aaa", "hook:pre_tool_call", "risk:a:b", "state:1"])
def test_a_colon_id_is_mapped_and_restored(name):
    questions = {name: QUESTIONS["self_advocating"]}
    answers = {name: {"type": "noul", "noul": 0.2}}
    safe, restore = jev_client.clef_question_ids(questions)
    (safe_name,) = safe
    assert ":" not in safe_name, "Clef accepts letters, digits, '_', '.' and '-' only"
    assert safe_name != name, "an id Clef would refuse is renamed rather than sent"
    assert restore(answers) == answers, "the caller sees its own id again"


def test_a_repo_built_id_round_trips_through_a_real_request(monkeypatch):
    questions = {"candidate:aaa": QUESTIONS["self_advocating"], "hook:pre_tool_call": QUESTIONS["verdict"]}
    safe_questions, _ = jev_client.clef_question_ids(questions)
    answers = {
        "candidate_aaa": {"type": "noul", "noul": 0.31},
        "hook_pre_tool_call": {"type": "choice", "choice": "DENY"},
    }
    # Clef answers under the ids it was sent, which are the mapped ones.
    assert set(safe_questions) == set(answers)
    body = {"model": "clef", "answers": answers}
    result, wire = run(monkeypatch, body, questions=questions)
    assert set(wire.requests[0]["body"]["questions"]) == set(safe_questions)
    assert all(":" not in sent for sent in wire.requests[0]["body"]["questions"])
    assert set(result["answers"]) == {"candidate:aaa", "hook:pre_tool_call"}
    assert result["answers"]["hook:pre_tool_call"]["choice"] == "DENY"


def test_an_id_clef_already_accepts_is_sent_unchanged():
    questions = {"verdict": QUESTIONS["verdict"], "hook-2.v_1": QUESTIONS["self_advocating"]}
    safe, _ = jev_client.clef_question_ids(questions)
    assert safe == questions


def test_a_renamed_id_cannot_collide_with_another_question():
    questions = {"a b": QUESTIONS["self_advocating"], "a_b": QUESTIONS["verdict"]}
    safe, _ = jev_client.clef_question_ids(questions)
    assert len(set(safe)) == 2
    assert "a_b" in safe, "the already safe id keeps the name the caller gave it"


def test_two_different_unsafe_ids_get_two_different_safe_ids():
    questions = {"a:b": QUESTIONS["self_advocating"], "a:c": QUESTIONS["verdict"]}
    safe, _ = jev_client.clef_question_ids(questions)
    assert len(set(safe)) == 2


def test_a_safe_id_is_deterministic_for_the_same_caller_id():
    first, _ = jev_client.clef_question_ids({"candidate:aaa": QUESTIONS["self_advocating"]})
    second, _ = jev_client.clef_question_ids({"candidate:aaa": QUESTIONS["self_advocating"]})
    assert first == second


def test_a_mapped_id_is_bounded_at_one_hundred_characters():
    long_id = "candidate:" + "x" * 400
    safe, _ = jev_client.clef_question_ids({long_id: QUESTIONS["self_advocating"]})
    (safe_id,) = safe
    assert len(safe_id) <= 100


def test_an_id_that_is_not_text_still_gets_a_safe_name():
    """A non-string key is mapped to a Clef-legal name and mapped back to itself."""
    safe, restore = jev_client.clef_question_ids({7: QUESTIONS["self_advocating"]})
    (safe_id,) = safe
    assert safe_id.isascii() and ":" not in safe_id
    assert restore({safe_id: {"noul": 0.1}}) == {7: {"noul": 0.1}}


def test_more_than_sixty_four_questions_is_refused_before_a_request(monkeypatch):
    many = {f"q{index}": QUESTIONS["self_advocating"] for index in range(65)}
    no_socket(monkeypatch)
    with pytest.raises(jev_client.JevSchemaError, match="at most 64 questions"):
        jev_client.request_decisions({}, many, BEARER, provider="clef")


def test_exactly_sixty_four_questions_are_accepted(monkeypatch):
    many = {f"q{index}": QUESTIONS["self_advocating"] for index in range(64)}
    answers = {f"q{index}": {"type": "noul", "noul": 0.1} for index in range(64)}
    result, wire = run(monkeypatch, bare(answers), questions=many)
    assert len(result["answers"]) == 64
    assert len(wire.requests[0]["body"]["questions"]) == 64


def test_the_question_count_limit_is_clef_specific(monkeypatch):
    """A route that does not name clef keeps accepting the questions it always did."""
    many = {f"q{index}": QUESTIONS["self_advocating"] for index in range(70)}
    answers = {f"q{index}": {"type": "noul", "noul": 0.1} for index in range(70)}
    wire = Wire([bare(answers)])
    monkeypatch.setattr(jev_client, "urlopen", wire)
    result = jev_client.request_decisions({}, many, "type-key", provider="typesafe")
    assert len(result["answers"]) == 70


def test_the_question_body_is_unchanged_apart_from_the_ids(monkeypatch):
    questions = {"candidate:aaa": QUESTIONS["verdict"]}
    safe_questions, _ = jev_client.clef_question_ids(questions)
    body = {"model": "clef", "answers": {name: {"choice": "APPROVE"} for name in safe_questions}}
    _, wire = run(monkeypatch, body, questions=questions)
    sent = wire.requests[0]["body"]["questions"]
    (sent_question,) = sent.values()
    assert sent_question == questions["candidate:aaa"]


# --- log hygiene -------------------------------------------------------------


def test_a_clef_failure_logs_the_exception_class_and_neither_state_nor_credential(
    caplog, monkeypatch
):
    untrusted_command = "aws s3 rm s3://private-bucket --recursive"
    wire = Wire([URLError("name resolution failed")])
    monkeypatch.setattr(jev_client, "urlopen", wire)
    monkeypatch.setenv(jev_client.CLEF_TOKEN_ENV, "bearer-that-must-not-be-logged")

    with caplog.at_level(logging.WARNING):
        with pytest.raises(jev_client.JevClientError):
            jev_client.request_decisions({"command": untrusted_command}, QUESTIONS, BEARER,
                                         provider="clef")

    assert "JevClientError" in caplog.text
    assert untrusted_command not in caplog.text
    assert "private-bucket" not in caplog.text
    assert "bearer-that-must-not-be-logged" not in caplog.text
    assert BEARER not in caplog.text
    assert "name resolution failed" not in caplog.text, "the exception message is not logged either"


def test_a_clef_log_line_names_the_provider_and_the_absent_fallback(monkeypatch):
    wire = Wire([URLError("down")])
    monkeypatch.setattr(jev_client, "urlopen", wire)
    messages = []
    handler = logging.Handler()
    handler.emit = messages.append  # type: ignore[assignment]
    logger = logging.getLogger("jev_client")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.WARNING)
    try:
        with pytest.raises(jev_client.JevClientError):
            jev_client.request_decisions({}, QUESTIONS, BEARER, provider="clef")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    text = "".join(str(record.getMessage()) for record in messages)
    assert "clef" in text
    assert "no fallback" in text
    assert BEARER not in text


# --- the existing retry behaviour is reused ----------------------------------


def test_a_retryable_cloudflare_status_is_retried_through_the_shared_loop(monkeypatch):
    wire = Wire([
        HTTPError("https://api.cloudflare.com", 503, "unavailable", {}, None),
        bare(),
    ])
    monkeypatch.setattr(jev_client, "urlopen", wire)
    result = jev_client.request_decisions({}, QUESTIONS, BEARER, provider="clef")
    assert result["answers"] == ANSWERS
    assert len(wire.requests) == 2, "the existing three attempt loop handled the retry"


def test_a_retryable_status_that_never_recovers_raises_after_three_attempts(monkeypatch):
    wire = Wire([HTTPError("https://api.cloudflare.com", 500, "boom", {}, None)])
    monkeypatch.setattr(jev_client, "urlopen", wire)
    with pytest.raises(jev_client.JevClientError, match="HTTP 500"):
        jev_client.request_decisions({}, QUESTIONS, BEARER, provider="clef")
    assert len(wire.requests) == 3


def test_a_non_retryable_status_fails_on_the_first_attempt(monkeypatch):
    wire = Wire([HTTPError("https://api.cloudflare.com", 401, "unauthorized", {}, None)])
    monkeypatch.setattr(jev_client, "urlopen", wire)
    with pytest.raises(jev_client.JevClientError, match="HTTP 401"):
        jev_client.request_decisions({}, QUESTIONS, BEARER, provider="clef")
    assert len(wire.requests) == 1


def test_a_success_false_envelope_is_not_retried(monkeypatch):
    body = {"success": False, "errors": [{"code": 7000}]}
    wire = Wire([body])
    monkeypatch.setattr(jev_client, "urlopen", wire)
    with pytest.raises(jev_client.JevClientError, match="7000"):
        jev_client.request_decisions({}, QUESTIONS, BEARER, provider="clef")
    assert len(wire.requests) == 1, "a Cloudflare error envelope is not a transient fault"


# --- mode aliases ------------------------------------------------------------


def test_the_clef_api_alias_selects_clef_alone_with_no_fallback(monkeypatch):
    monkeypatch.setenv("JEV_PROVIDER_MODE", "clef_api")
    assert jev_client.provider_mode() == "clef"
    assert jev_client.provider_order("clef_api") == ("clef",)
    assert jev_client.provider_keys("clef_api") == ("CLOUDFLARE_API_TOKEN",)


def test_the_clef_api_alias_answers_from_cloudflare(monkeypatch):
    monkeypatch.setenv("JEV_PROVIDER_MODE", "clef_api")
    _, wire = run(monkeypatch, bare(), provider=None)
    assert "api.cloudflare.com" in wire.requests[0]["url"]


def test_the_clef_api_alias_does_not_degrade_to_another_classifier(monkeypatch):
    monkeypatch.setenv("JEV_PROVIDER_MODE", "clef_api")
    monkeypatch.delenv(jev_client.CLEF_TOKEN_ENV, raising=False)
    no_socket(monkeypatch)
    with pytest.raises(jev_client.JevClientError, match="CLOUDFLARE_API_TOKEN"):
        jev_client.request_decisions({}, QUESTIONS, None)


def test_clef_may_appear_in_an_explicit_provider_order(monkeypatch):
    assert jev_client.validate_fallback_order(("laya", "clef")) == ("laya", "clef")
    assert jev_client.validate_fallback_order(("clef", "typesafe")) == ("clef", "typesafe")


@pytest.mark.parametrize("mode", ["clef_then_typesafe", "clef_then_openrouter", "laya_then_clef"])
def test_no_chain_mode_routes_to_clef_without_naming_it_in_a_mode(mode, monkeypatch):
    monkeypatch.setenv("JEV_PROVIDER_MODE", mode)
    with pytest.raises(jev_client.JevClientError, match="JEV_PROVIDER_MODE must be one of"):
        jev_client.provider_mode()


def test_clef_credentials_reports_the_missing_variable_before_any_request(monkeypatch):
    with pytest.raises(jev_client.JevClientError, match="CLOUDFLARE_API_TOKEN"):
        jev_client.clef_credentials(None, environ={})


def test_a_chain_naming_clef_needs_its_token_at_selection(monkeypatch):
    monkeypatch.delenv(jev_client.CLEF_TOKEN_ENV, raising=False)
    no_socket(monkeypatch)
    with pytest.raises(jev_client.JevClientError, match="CLOUDFLARE_API_TOKEN"):
        jev_client._require_hosted_keys("laya_then_clef", ("laya", "clef"), "", "")


def test_the_alias_table_still_maps_the_two_doga_names():
    assert jev_client.MODE_ALIASES["laya_local"] == "laya"
    assert jev_client.MODE_ALIASES["laya_with_jev_fallback"] == "laya_then_openrouter_typesafe"
    assert jev_client.MODE_ALIASES["clef_api"] == "clef"


def test_an_unaccepted_mode_still_names_the_whole_accepted_set(monkeypatch):
    monkeypatch.setenv("JEV_PROVIDER_MODE", "clef_local")
    with pytest.raises(jev_client.JevClientError) as excinfo:
        jev_client.provider_mode()
    message = str(excinfo.value)
    assert "clef_api" in message and "clef" in message
    assert "laya_local" in message and "laya_with_jev_fallback" in message


# --- the environment contract ------------------------------------------------


def test_only_the_named_variables_carry_the_clef_configuration(monkeypatch):
    assert jev_client.CLEF_TOKEN_ENV == "CLOUDFLARE_API_TOKEN"
    assert jev_client.CLEF_ACCOUNT_ENV == "CLOUDFLARE_ACCOUNT_ID"
    assert jev_client.CLEF_MODEL_ENV == "JEV_CLEF_MODEL"
    assert jev_client.CLEF_MODELS == ("clef", "clef-flash")
    assert jev_client.clef_route({"CLOUDFLARE_ACCOUNT_ID": ACCOUNT_FIXTURE})[0].startswith(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_FIXTURE}"
    )


def test_the_account_id_is_trimmed_like_the_other_settings(monkeypatch):
    endpoint, _ = jev_client.clef_route({"CLOUDFLARE_ACCOUNT_ID": f"  {ACCOUNT_FIXTURE}  "})
    assert endpoint == (
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_FIXTURE}/ai/run/@cf/cloudflare/clef"
    )


def test_clef_credentials_returns_the_token_and_account_without_printing_them(monkeypatch):
    token, account = jev_client.clef_credentials(None)
    assert token == BEARER and account == ACCOUNT_FIXTURE
    assert jev_client.clef_credentials("explicit")[0] == "explicit"


def test_a_short_mapped_id_stays_readable_and_carries_no_digest():
    """A colon becomes an underscore and nothing else, so a capture stays legible."""
    safe, _ = jev_client.clef_question_ids({"candidate:aaa": QUESTIONS["self_advocating"]})
    assert list(safe) == ["candidate_aaa"]


def test_a_truncated_id_carries_a_digest_of_the_original():
    """Only an id too long to send is salted, so truncation cannot collide."""
    long_id = "candidate:" + "x" * 400
    safe, _ = jev_client.clef_question_ids({long_id: QUESTIONS["self_advocating"]})
    (safe_id,) = safe
    assert len(safe_id) <= 100
    assert hashlib.sha256(long_id.encode()).hexdigest()[:8] in safe_id


def test_two_long_ids_that_share_a_prefix_get_different_safe_ids():
    prefix = "candidate:" + "x" * 400
    safe, _ = jev_client.clef_question_ids({f"{prefix}1": QUESTIONS["self_advocating"],
                                            f"{prefix}2": QUESTIONS["verdict"]})
    assert len(set(safe)) == 2


# --- the plugin and the approval workflow reach Clef -------------------------


def plugin_module(tmp_path, monkeypatch):
    """Load the plugin's __init__.py the way a plugin host does."""
    spec = importlib.util.spec_from_file_location("jev_clef_audit", ROOT / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "get_hermes_home", lambda: str(tmp_path))
    return module


def test_the_plugin_reads_the_clef_token_from_the_secret_scope(tmp_path, monkeypatch):
    """The Hermes credential lookup finds CLOUDFLARE_API_TOKEN for a clef mode."""
    module = plugin_module(tmp_path, monkeypatch)
    monkeypatch.setenv("JEV_PROVIDER_MODE", "clef")
    asked = []
    monkeypatch.setattr(module, "get_secret", lambda name: asked.append(name) or "value-from-scope")
    assert module._secret() == "value-from-scope"
    assert asked == ["CLOUDFLARE_API_TOKEN"]
    assert module._fallback_secret() is None, "clef is a hosted route with no hosted fallback"


def test_the_plugin_reports_the_clef_token_absence_by_name(tmp_path, monkeypatch):
    module = plugin_module(tmp_path, monkeypatch)
    monkeypatch.setenv("JEV_PROVIDER_MODE", "clef_api")
    monkeypatch.setattr(module, "get_secret", lambda _name: None)
    with pytest.raises(RuntimeError, match="CLOUDFLARE_API_TOKEN"):
        module._secret()


def test_the_plugin_routes_clef_through_the_shared_client(tmp_path, monkeypatch):
    """`mode != openrouter` already covers clef, so no new plugin branch is needed."""
    module = plugin_module(tmp_path, monkeypatch)
    monkeypatch.setenv("JEV_PROVIDER_MODE", "clef")
    monkeypatch.setattr(module, "get_secret", lambda _name: "value-from-scope")
    seen = {}

    def fake_request(state, questions, api_key, **kwargs):
        seen["questions"] = questions
        seen["api_key"] = api_key
        seen["provider"] = kwargs.get("provider")
        seen["timeout"] = kwargs.get("timeout")
        return {"answers": {"ok": {"noul": 0.4}}}

    monkeypatch.setattr(module, "request_decisions", fake_request)
    result = module._request({"state": {}, "questions": {"ok": QUESTIONS["self_advocating"]}}, "caller-supplied")
    assert result == {"answers": {"ok": {"noul": 0.4}}}
    assert seen["provider"] == "clef"
    assert seen["timeout"] == 30.0, "clef is hosted, so it keeps the hosted budget"
    assert seen["api_key"] == "caller-supplied", "the plugin forwards the credential it resolved"


def test_the_approval_review_answers_from_clef_with_no_routing_block(monkeypatch):
    """The advisory workflow works on the clef route and reports no fallback."""
    monkeypatch.setenv("JEV_PROVIDER_MODE", "clef")
    answers = {
        "verdict": {"type": "choice", "choice": "DENY", "confidence": 0.7},
        "policy_allows": {"type": "noul", "noul": 0.0},
        "blast_radius": {"type": "score", "score": 1.2,
                         "legend": {"0": "trivial", "1": "annoying", "2": "severe"}},
        "self_advocating": {"type": "noul", "noul": 0.0},
        "reads_secrets": {"type": "noul", "noul": 0.0},
        "sends_outbound": {"type": "noul", "noul": 0.0},
    }
    wire = Wire([bare(answers)])
    monkeypatch.setattr(jev_client, "urlopen", wire)
    result = approval_review.review_command("printf fixture", api_key=BEARER)
    assert result["success"] is True
    assert result["verdict"] == "DENY"
    assert result["model"] == "clef"
    assert "provider_routing" not in result, "clef alone reports no routing block"
    assert "api.cloudflare.com" in wire.requests[0]["url"]