"""Shared bounded client for Jev, for Cloudflare Clef, or for a local model.

Four arrangements for a typed question: a hosted Jev provider behind an API key,
the hosted Cloudflare Clef provider behind a Cloudflare token, a local decision
server that needs no key at all, or an opt in chain between the two. The plain
local route replaces the hosted ones rather than joining the chain. Only the
local first chains let a failed local attempt reach a hosted provider.

Clef is a provider, not a Jev key: it is selected by its own name, it is never
appended to a mode that does not name it, and it falls back to nothing. `clef`
and `clef-flash` are two checkpoints of that one provider, chosen with
`JEV_CLEF_MODEL`, so `clef-flash` is never a provider name of its own.

**The four canonical modes.** Every arrangement above is named by one of four
canonical mode names, each of which says which side leads and whether the other
side is a fallback:

| Mode | Leads | Fallback |
|---|---|---|
| `api_with_local_fallback` | the hosted API | the local slot |
| `api_only` | the hosted API | none |
| `local_only` | the local slot | none |
| `local_with_api_fallback` | the local slot | the hosted API |

`api_only` and `local_only` are single provider routes: a failure is reported,
never rerouted. The other two are two provider chains and use the cooldown,
trigger and breaker machinery below unchanged. This repository also keeps its
own finer grained mode names, which name a concrete provider order directly:
`typesafe`, `openrouter`, `clef`, `typesafe_then_openrouter`,
`openrouter_then_typesafe`, and the four `laya_then_*` local first chains. Those
are not renamed by this contract and not deprecated; each still resolves to the
exact order it always named. `MODE_ALIASES` and `CANONICAL_MODES` hold the whole
mapping, and `resolve_mode` turns any accepted name into the one canonical mode
name every code path below uses, so no alias string reaches a chain, a
diagnostic, a log line, or a URL.

**The local slot is a slot, not a model.** `laya` selects it, and the engine or
checkpoint it speaks is chosen with `JEV_LOCAL_MODEL`. Any local model that
answers the same `/v1/systemone` contract fits, selected by configuration alone
and with no new provider name and no code change. See `local_model`.

Three of DOGA's selector names are accepted as aliases as well:
`laya_local` for the plain local mode, `laya_with_jev_fallback` for the
local first chain that names both hosted Jev providers, and `clef_api` for Clef
alone. `resolve_mode` turns every vocabulary into the one canonical mode name
every code path below uses.

A local failure may reach a hosted provider only until it has failed three times
in a row. The count is per process, so a restart resets it, and any local answer
that passes validation resets it too. Past the limit the local error is re-raised
and no hosted request is made, which bounds repeated remote egress however the
mode names its hosted providers. The count tracks local failures only, so it
bounds egress identically across all four `laya_then_*` modes. It cannot detect a
valid but incorrect local answer.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"
TYPESAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_MODEL = "jev-1.13.0"
TYPESAFE_PROVIDER = "typesafe"
OPENROUTER_PROVIDER = "openrouter"
LAYA_PROVIDER = "laya"
CLEF_PROVIDER = "clef"
# Clef runs at Cloudflare Workers AI. The run endpoint is scoped to an account,
# so the account ID is part of the URL rather than an optional setting, and the
# token authorises the call. They are separate environment variables because
# they are separate kinds of value: the account is configuration, the token is a
# credential. Neither is read from any config file.
CLEF_API_BASE = "https://api.cloudflare.com/client/v4/accounts"
CLEF_RUN_PATH = "/ai/run/@cf/cloudflare/{model}"
CLEF_MODELS = ("clef", "clef-flash")
CLEF_DEFAULT_MODEL = "clef"
CLEF_TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
CLEF_ACCOUNT_ENV = "CLOUDFLARE_ACCOUNT_ID"
CLEF_MODEL_ENV = "JEV_CLEF_MODEL"
# Clef answers typed questions as fast as the hosted Jev route does, so it keeps
# the hosted default timeout rather than the long local CPU budget.
CLEF_MAX_QUESTIONS = 64
CLEF_MAX_ID_LENGTH = 100
# Clef accepts letters, digits, '_', '.' and '-' in a question id and nothing
# else. This repository builds ids such as `candidate:aaa` and `hook:name`, whose
# colon Clef rejects, so ids are mapped before the request and restored after it.
_CLEF_ID_ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
# A Cloudflare account ID is a 32 character hex string. The endpoint interpolates
# it into a URL, so anything that could rewrite the path is refused before a
# socket is opened.
_CLEF_ACCOUNT_ALLOWED = frozenset("0123456789abcdef")
_CLEF_ACCOUNT_LENGTH = 32
HOSTED_PROVIDER_MODES = frozenset({
    TYPESAFE_PROVIDER, OPENROUTER_PROVIDER, CLEF_PROVIDER,
    "typesafe_then_openrouter", "openrouter_then_typesafe",
})
# Local first chains. The local slot answers from the local server, and a failed
# local attempt falls through to the named hosted providers in the order given.
# These are the only modes where one review can reach both a local and a hosted
# route.
LAYA_CHAIN_MODES = frozenset({
    "laya_then_typesafe",
    "laya_then_openrouter",
    "laya_then_typesafe_openrouter",
    "laya_then_openrouter_typesafe",
})
# Hosted first chains that fall back to the local slot. These are the only modes
# where a hosted failure is a licence to call the local server, which is the
# mirror image of `LAYA_CHAIN_MODES`: there the local server leads and a hosted
# provider answers, here a hosted provider leads and the local server answers.
HOSTED_LOCAL_FALLBACK_MODES = frozenset({
    "typesafe_then_laya",
    "openrouter_then_laya",
    "clef_then_laya",
})
# Every mode that is a chain rather than a single provider. A chain gets the
# routing block, the pre flight key check, and the multi route request path.
FALLBACK_MODES = LAYA_CHAIN_MODES | HOSTED_LOCAL_FALLBACK_MODES
PROVIDER_MODES = HOSTED_PROVIDER_MODES | FALLBACK_MODES | {LAYA_PROVIDER}
# The four canonical modes of the shared provider contract. Each names which
# side leads and whether the other side is a fallback, and says nothing about
# which hosted provider or which local engine fills the slots. They are selectable
# on their own: the resolution order below turns each one into the concrete
# provider order this repository routes with.
API_WITH_LOCAL_FALLBACK = "api_with_local_fallback"
API_ONLY = "api_only"
LOCAL_ONLY = "local_only"
LOCAL_WITH_API_FALLBACK = "local_with_api_fallback"
CANONICAL_MODES = frozenset({
    API_WITH_LOCAL_FALLBACK, API_ONLY, LOCAL_ONLY, LOCAL_WITH_API_FALLBACK,
})
# The two single provider canonical modes. `api_only` and `local_only` name no
# fallback at all: a failure on either is reported, never rerouted, so a single
# provider route is exactly one hop.
CANONICAL_SINGLE_MODES = frozenset({API_ONLY, LOCAL_ONLY})
# A provider bound to the local machine carries no credential requirement, so an
# empty key means "send no Authorization header", not "disabled".
KEYLESS_PROVIDERS = frozenset({LAYA_PROVIDER})
# Which concrete order each canonical mode resolves to when it names no provider
# of its own. `api_only` resolves by credential instead (see
# `_hosted_provider_by_credential`), so it has no fixed entry: the hosted side is
# configuration, not a preference this module may impose.
#
# The two fallback canonical modes are chains. `api_with_local_fallback` is the
# hosted provider leading with the local slot behind it, which is the mirror of
# `local_with_api_fallback`; the paired `<hosted>_then_laya` mode is named in
# `FALLBACK_ORDER` and is selected by credential as well, so that a Clef hosted
# side falls back to the local slot rather than to an unrelated Jev provider.
CANONICAL_MODE_DEFAULTS: dict[str, str] = {
    LOCAL_ONLY: LAYA_PROVIDER,
    LOCAL_WITH_API_FALLBACK: "laya_then_openrouter_typesafe",
}
# DOGA names four arrangements: `jev_api`, `clef_api`, `laya_local`, and
# `laya_with_jev_fallback`. `jev_api` is the hosted Jev arrangement this
# repository already has under its own hosted mode names, so it resolves through
# the credential-resolving alias below rather than a fixed order. The other
# three are accepted here and resolved to a canonical mode before anything
# routes:
#
#   clef_api               -> clef
#   jev_api                -> api_only, hosted side by credential
#   laya_local             -> laya
#   laya_with_jev_fallback -> laya_then_openrouter_typesafe
#
# The chain is spelled out rather than implied. DOGA's own Jev route tries
# OpenRouter first and direct TypeSafe second, and `openrouter` is this
# repository's default hosted provider, so the local first chain that names both
# hosted providers in that order is the faithful mapping. Nothing here is
# inferred from the alias name at request time; the table is the whole mapping.
#
# `clef_api` resolves to Clef alone, deliberately. Clef is a hosted route of its
# own, so a Clef failure is a failure of this route and never a licence to call a
# second classifier: the alias names no fallback at all.
MODE_ALIASES: dict[str, str] = {
    "clef_api": CLEF_PROVIDER,
    "jev_api": API_ONLY,
    "laya_local": LAYA_PROVIDER,
    "laya_with_jev_fallback": "laya_then_openrouter_typesafe",
    # The shared contract's own names for the two arrangements this repository
    # already spells out with a provider order. They are accepted alongside the
    # canonical four and behave exactly like the mode each one denotes.
    "clef_with_local_fallback": API_WITH_LOCAL_FALLBACK,
    "laya_then_hosted": LOCAL_WITH_API_FALLBACK,
}
# The two canonical modes whose hosted side is configuration rather than a fixed
# order. `api_only` names no fallback, so only the hosted provider is resolved;
# `api_with_local_fallback` resolves the same provider and pairs it with the local
# slot. The two local side modes are fixed orders and never read a credential here.
_CREDENTIAL_RESOLVED_MODES = frozenset({API_ONLY, API_WITH_LOCAL_FALLBACK})
# Every value `JEV_PROVIDER_MODE` accepts: the canonical modes plus the modes
# this repository already named directly, plus the aliases.
PROVIDER_MODES_ALL = PROVIDER_MODES | CANONICAL_MODES
ACCEPTED_MODES = frozenset(PROVIDER_MODES_ALL | set(MODE_ALIASES))
# Consecutive local failure limit. The first three consecutive local failures in a
# process may fall through to a hosted provider; the fourth and every one after it
# re-raises the local error instead. Hardcoded, per process, and never persisted.
LOCAL_FALLBACK_FAILURE_LIMIT = 3
_local_failure_lock = threading.Lock()
_local_failure_count = 0
# The environment variable that carries each hosted provider's credential.
# Clef needs a second variable for its account ID, which is configuration rather
# than a secret and is checked separately by `clef_credentials`.
PROVIDER_KEY_ENV = {TYPESAFE_PROVIDER: "TYPESAFE_API_KEY", OPENROUTER_PROVIDER: "OPENROUTER_API_KEY",
                    CLEF_PROVIDER: CLEF_TOKEN_ENV}
FALLBACK_ORDER: dict[str, tuple[str, ...]] = {
    "typesafe_then_openrouter": (TYPESAFE_PROVIDER, OPENROUTER_PROVIDER),
    "openrouter_then_typesafe": (OPENROUTER_PROVIDER, TYPESAFE_PROVIDER),
    "laya_then_typesafe": (LAYA_PROVIDER, TYPESAFE_PROVIDER),
    "laya_then_openrouter": (LAYA_PROVIDER, OPENROUTER_PROVIDER),
    "laya_then_typesafe_openrouter": (LAYA_PROVIDER, TYPESAFE_PROVIDER, OPENROUTER_PROVIDER),
    "laya_then_openrouter_typesafe": (LAYA_PROVIDER, OPENROUTER_PROVIDER, TYPESAFE_PROVIDER),
    # Hosted first chains with the local slot behind the hosted provider. A hosted
    # failure falls through to the local server here, which is the mirror of the
    # `laya_then_*` orders above and is what `api_with_local_fallback` routes with.
    "typesafe_then_laya": (TYPESAFE_PROVIDER, LAYA_PROVIDER),
    "openrouter_then_laya": (OPENROUTER_PROVIDER, LAYA_PROVIDER),
    "clef_then_laya": (CLEF_PROVIDER, LAYA_PROVIDER),
}
LAYA_BASE_URL_DEFAULT = "http://127.0.0.1:8123"
LAYA_ENDPOINT_PATH_DEFAULT = "/v1/systemone"
LAYA_MODEL_DEFAULT = "english"
# The local slot's engine or checkpoint, and the setting that chooses it.
#
# `laya` selects the local slot. It does not bind the slot to one model: the
# value here is the engine or checkpoint name the request asks for, so any local
# model that answers the same `/v1/systemone` contract is selected by
# configuration alone, with no new provider name and no code change. Laya's own
# three engine names (`english`, `multilingual`, `typed-decisions`) are what a
# `laya-serve` serves, and other local engines are selected by their own names.
#
# **The default request is unchanged.** The default value is still `english`,
# which is the checkpoint `laya-serve` serves by default and the only one this
# repository has ever called. `laya` is the default *slot*, and `english` is the
# default *engine in that slot*: the slot is Laya's, so the checkpoint Laya
# serves by default is what it answers. A default configuration therefore sends
# exactly the request it always sent, which is the requirement that outranks a
# literal reading of "defaults to laya".
LOCAL_MODEL_DEFAULT = LAYA_MODEL_DEFAULT
LOCAL_MODEL_ENV = "JEV_LOCAL_MODEL"
# `JEV_LAYA_MODEL` is the pre-existing name for the same setting and keeps
# working. When both are set, `JEV_LOCAL_MODEL` wins, because it is the generic
# name and the repository's rule is that the newest spelling of a setting is the
# explicit one. Recorded in the docs, in that order.
LAYA_MODEL_ENV = "JEV_LAYA_MODEL"
# Characters a local model name may not contain. This is not an allowlist of
# model names: an engine nobody has heard of must work without a code change. It
# is a refusal of values that would corrupt the request, because the name is
# interpolated into a JSON string and may reach the server as a path segment.
# Anything that could terminate a JSON string, change its type, split the body,
# rewrite a URL path, smuggle a header, or hide its own trailing characters is
# refused here rather than sent.
_LOCAL_MODEL_FORBIDDEN = frozenset('"\\/\x00\r\n\t') | frozenset({chr(code) for code in range(0x20)}) | frozenset({
    "<", ">", "&", "%", "?", "#", "`", "$", "*", "|", "^", "~",
})
# CPU inference on the base checkpoint measured about 1.6 seconds per question
# row and roughly 2 seconds for a whole six question review on 2026-09-26, so the
# local route gets a longer budget than the hosted default of 30 seconds.
LAYA_TIMEOUT_S = 120.0
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_RETRYABLE = frozenset({429, 500, 502, 503, 504})


class JevClientError(RuntimeError):
    """Base error for unavailable or invalid Jev responses."""


class JevSchemaError(JevClientError):
    """The provider returned a response outside the typed contract."""


def resolve_mode(name: str, environ: Mapping[str, str] | None = None) -> str:
    """The canonical mode an accepted name selects, or an error naming the accepted set.

    Three vocabularies name the same arrangements: the four canonical modes of
    the shared provider contract, this repository's own mode names, and the
    aliases in ``MODE_ALIASES``. Resolution happens once, here, so every code
    path below sees a canonical mode and no routing decision depends on which
    vocabulary the caller used.

    A canonical mode resolves to the concrete mode this repository routes with,
    because that is the layer that knows its own provider orders. Its hosted side
    is chosen by configuration rather than fixed here, so ``api_only`` and
    ``api_with_local_fallback`` reach whichever hosted provider is actually
    configured instead of a hard coded one.
    """
    if name in CANONICAL_MODES:
        return _canonical_mode_order(name, environ)
    if name in PROVIDER_MODES:
        return name
    alias = MODE_ALIASES.get(name)
    if alias is None:
        raise JevClientError(
            f"JEV_PROVIDER_MODE must be one of: {', '.join(sorted(ACCEPTED_MODES))}"
        )
    if alias in CANONICAL_MODES:
        return _canonical_mode_order(alias, environ)
    return alias


def _canonical_mode_order(mode: str, environ: Mapping[str, str] | None = None) -> str:
    """The concrete mode name a canonical mode routes with in this repository.

    Two of the four take a fixed route: `local_only` is the plain local slot and
    `local_with_api_fallback` is the local first chain that names both hosted Jev
    providers, which is the strongest local first route this repository has and is
    what `laya_with_jev_fallback` already selected.

    The two hosted side modes resolve their hosted provider by credential instead
    of by preference, because the contract says the hosted side is chosen by
    configuration and this module must not impose one: `api_only` becomes that
    provider alone, and `api_with_local_fallback` becomes that provider leading
    with the local slot behind it.
    """
    hosted = (
        _hosted_provider_by_credential(environ)
        if mode in _CREDENTIAL_RESOLVED_MODES else ""
    )
    # `api_only` is the one canonical mode that resolves to a hosted provider
    # and nothing else. It has no local hop and no chain, which is the whole
    # difference between it and `api_with_local_fallback`.
    if mode == API_ONLY:
        return hosted
    if mode in CANONICAL_MODE_DEFAULTS:
        return CANONICAL_MODE_DEFAULTS[mode]
    return f"{hosted}_then_{LAYA_PROVIDER}"


def _hosted_provider_by_credential(environ: Mapping[str, str] | None = None) -> str:
    """The hosted provider a credential resolving mode selects.

    Clef needs two variables rather than one, so it is only selected when both
    are present: a token without an account id cannot route anywhere, and
    selecting it anyway would report a confusing provider error later. With no
    hosted credential at all the repository's default hosted provider is named,
    so the missing key is reported by the existing selection check rather than by
    an invented mode, and an `api_with_local_fallback` chain still has a hosted
    hop to try before the local slot answers.
    """
    env = os.environ if environ is None else environ
    if (env.get(CLEF_ACCOUNT_ENV) or "").strip() and (env.get(CLEF_TOKEN_ENV) or "").strip():
        return CLEF_PROVIDER
    for provider in (OPENROUTER_PROVIDER, TYPESAFE_PROVIDER):
        if (env.get(PROVIDER_KEY_ENV[provider]) or "").strip():
            return provider
    return OPENROUTER_PROVIDER


def provider_mode(value: str | None = None, environ: Mapping[str, str] | None = None) -> str:
    """Return the selected route, defaulting to the legacy OpenRouter path.

    `laya` selects the local slot instead of a hosted provider: a replacement for
    the hosted pair, not a third member of it. The `laya_then_*` modes are the
    local first chains, where a failed local attempt falls through to the named
    hosted provider or providers, and the `<hosted>_then_laya` modes are their
    mirror, where a hosted failure falls through to the local slot. The four
    canonical modes and the DOGA aliases are accepted and resolve to the modes
    above.
    """
    env = os.environ if environ is None else environ
    selected = (value or env.get("JEV_PROVIDER_MODE", "openrouter")).strip().lower()
    return resolve_mode(selected, env)


def validate_fallback_order(names: Any) -> tuple[str, ...]:
    """Return an ordered provider chain, rejecting any shape a mode does not name.

    A chain is one or more hosted providers, optionally with the local slot at
    either end but never in the middle. Laya may lead a chain, where a failed
    local attempt falls through to the hosted providers it names, and Laya may
    trail a chain, where a failed hosted attempt falls through to the local
    server. It may do neither in the middle, because a local server replaces the
    hosted route rather than being sandwiched between two hosted hops. A chain
    alone is not a mode, so ``(LAYA_PROVIDER,)`` is rejected here; the plain
    local mode is selected by its own name instead.
    """
    order = tuple(names)
    if not order:
        raise JevClientError("invalid Jev fallback order: the chain is empty")
    for index, name in enumerate(order):
        if name not in PROVIDER_MODES:
            raise JevClientError(
                f"invalid Jev fallback order: {name!r} is not a Jev provider; "
                f"choose one of {', '.join(sorted(PROVIDER_MODES))}"
            )
        if name != LAYA_PROVIDER:
            continue
        if 0 < index < len(order) - 1:
            raise JevClientError(
                "invalid Jev fallback order: Laya is a local provider, not a hosted Jev "
                "provider, so it cannot sit between two hosted hops"
            )
        if len(order) == 1:
            raise JevClientError(
                "invalid Jev fallback order: Laya alone is a local provider, not a hosted "
                "Jev provider chain, and the plain local mode is selected by name"
            )
    if len(order) != len(set(order)):
        raise JevClientError(f"invalid Jev fallback order: {order!r} names a provider twice")
    return order


def provider_order(mode: str, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The providers a mode uses, in the order it tries them.

    A pinned hosted mode is one provider and a chained hosted mode is two. A
    `laya_then_*` mode starts at the local server and names one or both hosted
    providers after it, and a `<hosted>_then_laya` mode is the mirror: the hosted
    provider first, the local server behind it. The plain local mode is exactly
    one: itself. Only a mode that names the local slot reaches it, so selecting a
    hosted mode never contacts a local server by accident and no mode appends
    Laya silently. A canonical mode or an alias is resolved first, so it reports
    the order of the mode it aliases.
    """
    mode = resolve_mode(mode, environ)
    if mode == LAYA_PROVIDER:
        return (LAYA_PROVIDER,)
    return validate_fallback_order(FALLBACK_ORDER.get(mode, (mode,)))


def _hosted_key_names(order: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(PROVIDER_KEY_ENV[name] for name in order if name not in KEYLESS_PROVIDERS)


def provider_keys(mode: str, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Environment variable names for a mode's hosted providers, in chain order."""
    return _hosted_key_names(provider_order(mode, environ))


def uses_local_hop(mode: str, environ: Mapping[str, str] | None = None) -> bool:
    """True when a mode's local budget is needed because it names the local slot.

    The longer budget goes to the local hop whenever the chain contains one, not
    only when it leads it, so a hosted first chain that falls through to the local
    server waits as long as that server needs.
    """
    return LAYA_PROVIDER in provider_order(mode, environ)


def _note_local_failure() -> bool:
    """Count one consecutive local failure; True when the hosted fallback is now suppressed.

    The limit is hardcoded at three. The first three consecutive local failures may
    fall through to a hosted provider; every one after that re-raises the local
    error instead, because a local server that has failed four times in a row is
    not a transient fault and should not keep turning reviews into remote traffic.
    """
    global _local_failure_count
    with _local_failure_lock:
        _local_failure_count += 1
        return _local_failure_count > LOCAL_FALLBACK_FAILURE_LIMIT


def _reset_local_failures() -> None:
    """Restart the consecutive count after any local answer that passed validation."""
    global _local_failure_count
    with _local_failure_lock:
        _local_failure_count = 0


def local_failure_count() -> int:
    """This process's consecutive local failure count, for tests and telemetry."""
    with _local_failure_lock:
        return _local_failure_count


def validate_laya_endpoint(url: str) -> str:
    """A keyless local route is plain HTTP on loopback, or HTTPS anywhere else.

    The local provider sends no credential, so a plain HTTP request to another
    host would put the reviewed state on the wire unauthenticated and
    unencrypted. Loopback is the only place that is safe, and the only place
    `laya-serve` is meant to listen.
    """
    if urlsplit(url).scheme == "https":
        return url
    host = (urlsplit(url).hostname or "").lower()
    if urlsplit(url).scheme == "http" and host in _LOOPBACK_HOSTS:
        return url
    raise JevClientError(
        f"invalid local Laya endpoint {url!r}: plain HTTP is accepted on loopback only; "
        "use HTTPS for any other host"
    )


def local_model(value: Any = None, environ: Mapping[str, str] | None = None) -> str:
    """The engine or checkpoint the local slot asks for, by default `laya`.

    The provider name in configuration stays `laya`; what it selects is the slot,
    not one model. This value is what the request asks the local server for, so a
    different local model that answers the same `/v1/systemone` contract is
    selected here alone, with no new provider name and no code change.

    `JEV_LOCAL_MODEL` is the setting; `JEV_LAYA_MODEL` is the name this setting
    had before the slot became generic, and it still works. When both are set,
    `JEV_LOCAL_MODEL` wins, because it is the name that is not tied to one model.
    An unset value is the default, and an empty or whitespace value is refused
    rather than silently treated as the default: an operator who blanked the
    setting meant something, and the default is what they get by leaving it out.

    **There is no allowlist of model names.** An engine nobody has heard of must
    work without a code change, so the check is only that the value is safe to
    carry: not empty, and free of characters that would corrupt the JSON body it
    is interpolated into or rewrite a URL path segment it might become.
    """
    env = os.environ if environ is None else environ
    if value is None:
        # Presence is what distinguishes "left unset" from "blanked on purpose",
        # because an environment variable set to an empty string is still present
        # in the mapping. An unset setting takes the default; a blanked one is a
        # mistake worth reporting rather than silently covering over.
        if LOCAL_MODEL_ENV in env:
            value = env[LOCAL_MODEL_ENV]
        elif LAYA_MODEL_ENV in env:
            value = env[LAYA_MODEL_ENV]
        else:
            return LOCAL_MODEL_DEFAULT
    if not isinstance(value, str) or not value.strip():
        raise JevClientError(
            f"{LOCAL_MODEL_ENV} must name a local decision model, such as "
            f"{LOCAL_MODEL_DEFAULT!r}; leave it unset to take the default"
        )
    name = value.strip()
    unsafe = sorted({character for character in name if character in _LOCAL_MODEL_FORBIDDEN})
    if unsafe:
        raise JevClientError(
            f"{LOCAL_MODEL_ENV} must be a model name usable as a JSON string and as a "
            f"URL path segment; refuse the character(s) {', '.join(repr(c) for c in unsafe)}"
        )
    return name


def laya_key(environ: Mapping[str, str] | None = None) -> str:
    """The optional bearer a local server was started with. Usually empty.

    ``laya-serve`` needs no credential by default, so an empty value means
    "configured without one" rather than "disabled": the header is then omitted
    entirely instead of being sent as an empty bearer.
    """
    env = os.environ if environ is None else environ
    return (env.get("LAYA_API_KEY") or "").strip()


def laya_route(environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    """The local server URL and the engine or checkpoint it is asked for.

    Both are settings rather than secrets. The URL is the slot's own setting, so
    pointing the slot at a different engine's server is a configuration change and
    not a code change; `local_model` chooses the engine that server is asked for.
    """
    env = os.environ if environ is None else environ
    base = (env.get("JEV_LAYA_BASE_URL") or LAYA_BASE_URL_DEFAULT).strip().rstrip("/")
    path = (env.get("JEV_LAYA_ENDPOINT_PATH") or LAYA_ENDPOINT_PATH_DEFAULT).strip()
    return validate_laya_endpoint(f"{base}/{path.lstrip('/')}"), local_model(environ=env)


def clef_credentials(api_key: str | None = None, environ: Mapping[str, str] | None = None
                     ) -> tuple[str, str]:
    """The Cloudflare bearer and the account that scopes the run endpoint.

    Both are checked here, before any socket work, so a misconfigured Clef route
    fails on the first request instead of degrading into another classifier. The
    error names the missing variable, never its value, and both missing names are
    reported together so a first run needs one fix rather than two.

    The account ID is configuration, not a credential: it is not a secret, it
    appears in the request URL, and it is never written anywhere. It is still
    validated, because it is interpolated into a URL and a value carrying `../`
    or a slash would rewrite which endpoint the request reaches.
    """
    env = os.environ if environ is None else environ
    token = (api_key or env.get(CLEF_TOKEN_ENV) or "").strip()
    account = (env.get(CLEF_ACCOUNT_ENV) or "").strip()
    missing = [
        name for name, value in ((CLEF_ACCOUNT_ENV, account), (CLEF_TOKEN_ENV, token))
        if not value
    ]
    if missing:
        raise JevClientError(
            f"JEV_PROVIDER_MODE={CLEF_PROVIDER} needs {', '.join(missing)}; "
            f"set the token in the Hermes secret scope and the account id in the environment"
        )
    if len(account) != _CLEF_ACCOUNT_LENGTH or not set(account) <= _CLEF_ACCOUNT_ALLOWED:
        raise JevClientError(
            f"invalid {CLEF_ACCOUNT_ENV}: expected {_CLEF_ACCOUNT_LENGTH} lowercase hex "
            f"characters, got a value that is not a Cloudflare account id"
        )
    return token, account


def clef_route(environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    """The Clef run endpoint and checkpoint, both settings rather than secrets.

    ``clef`` is the 27B base checkpoint and ``clef-flash`` the smaller one
    Cloudflare documents for latency-bound paths. Both answer the same typed
    `noul`, `choice` and `score` questions, so the checkpoint is a setting on
    one provider rather than a second provider. An unknown value is refused here
    instead of calling a checkpoint that does not exist.
    """
    env = os.environ if environ is None else environ
    account = (env.get(CLEF_ACCOUNT_ENV) or "").strip()
    model = (env.get(CLEF_MODEL_ENV) or CLEF_DEFAULT_MODEL).strip().lower()
    if model not in CLEF_MODELS:
        raise JevClientError(f"{CLEF_MODEL_ENV} must be one of: {', '.join(CLEF_MODELS)}")
    if not account:
        raise JevClientError(f"JEV_PROVIDER_MODE={CLEF_PROVIDER} needs {CLEF_ACCOUNT_ENV}")
    return f"{CLEF_API_BASE}/{account}{CLEF_RUN_PATH.format(model=model)}", model


def _clef_safe_id(name: Any) -> str:
    """A deterministic Clef-legal id for a caller id Clef would refuse.

    Clef accepts letters, digits, `_`, `.` and `-` only, up to 100 characters.
    A colon is the common case here, because this repository builds ids such as
    `candidate:aaa` and `hook:name`, so the colon becomes an underscore and the
    mapping stays readable in a wire capture. Any other disallowed character is
    replaced the same way. An id too long to send is truncated and gains a digest
    of the original, so truncation can never make two caller ids collide.
    """
    text = name if isinstance(name, str) else str(name)
    safe = "".join(character if character in _CLEF_ID_ALLOWED else "_" for character in text)
    if len(safe) <= CLEF_MAX_ID_LENGTH:
        return safe
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"{safe[:CLEF_MAX_ID_LENGTH - len(digest) - 1]}_{digest}"


def clef_question_ids(questions: dict[str, Any]) -> tuple[dict[str, Any], Callable[[dict[str, Any]], dict[str, Any]]]:
    """The questions as Clef must receive them, and the answer id reverse map.

    Returns a rewritten question map plus a ``restore`` callable that puts the
    caller's own question ids back onto an answer map. Ids Clef already accepts
    are sent unchanged, so a caller that builds only Clef-legal names never sees
    a renamed question; the ones it would refuse, such as ``candidate:aaa``, are
    mapped to a safe id and mapped back afterwards, so nothing downstream has to
    know the substitution happened.
    """
    if len(questions) > CLEF_MAX_QUESTIONS:
        raise JevSchemaError(
            f"a Clef request takes at most {CLEF_MAX_QUESTIONS} questions, got {len(questions)}"
        )
    safe: dict[str, Any] = {}
    forward: dict[str, str] = {}
    # An id Clef already accepts keeps the name the caller gave it, even when some
    # other id would map onto that same string. Only a genuinely rewritten id is
    # salted, so a caller whose own ids are all Clef-legal never sees a rename.
    literal = {
        name for name in questions
        if isinstance(name, str) and name and len(name) <= CLEF_MAX_ID_LENGTH
        and set(name) <= _CLEF_ID_ALLOWED
    }
    taken: set[str] = set()
    for name, question in questions.items():
        candidate = name if name in literal else _clef_safe_id(name)
        if candidate in taken:
            # Two caller ids landed on one Clef id. Disambiguate the later one with
            # the digest its clean mapping would have carried.
            candidate = _clef_safe_id(name)
            if candidate in taken:
                candidate = _clef_safe_id(f"{name}#{len(taken)}")
        taken.add(candidate)
        safe[candidate] = question
        forward[candidate] = name
    restore: Callable[[dict[str, Any]], dict[str, Any]] = (
        lambda answers: {forward.get(name, name): answer for name, answer in answers.items()}
    )
    return safe, restore


def validate_clef_answers(answers: Any, questions: dict[str, Any]) -> dict[str, Any]:
    """Clef's typed answers under this repository's existing contract.

    Clef names its three question types `noul`, `choice` and `score` exactly as
    the System One API this client already speaks, so there is no second answer
    contract to keep in step: `validate_answers` is called unchanged, and the
    score legend index scale is enforced by the same `validate_laya_answers`
    helper the local route uses. Only the envelope unwrapping is new.
    """
    return validate_laya_answers(validate_answers(answers, questions), questions)


def _clef_result(data: Any) -> dict[str, Any]:
    """Unwrap a Clef response envelope into the bare model output shape.

    Cloudflare serves the model output directly from the run endpoint, while its
    general REST surface wraps results in a ``success``/``result`` envelope. Both
    are accepted, top level ``answers`` first. A ``success: false`` envelope
    carries Cloudflare's own error codes, which say more than a generic parse
    failure, so they are surfaced as they are rather than flattened into one.
    """
    if not isinstance(data, dict):
        raise JevSchemaError("Cloudflare Workers AI returned a non-object response")
    if data.get("success") is False:
        codes = [
            str(error.get("code")) for error in (data.get("errors") or [])
            if isinstance(error, dict) and error.get("code") is not None
        ]
        detail = ", ".join(codes) if codes else "unknown error"
        raise JevClientError(f"Cloudflare Workers AI request failed (code {detail})")
    if isinstance(data.get("answers"), dict):
        return data
    inner = data.get("result")
    if isinstance(inner, dict) and isinstance(inner.get("answers"), dict):
        # Keep the envelope's own fields alongside the model output, so a caller
        # reading `usage` or `success` from a wrapped answer still sees them.
        return {**data, **inner}
    raise JevSchemaError("Cloudflare Workers AI returned a response with no answers map")


def _request_clef(state: Any, questions: dict[str, Any], *, transport: Callable[..., Any] | None = None,
                  timeout: float = 30.0, api_key: str | None = None) -> dict[str, Any]:
    """One Clef review through the shared request path, with its own envelope.

    The credentials, the checkpoint, and the question ids are all resolved before
    any socket work, and the POST itself goes through `_request_once`, so Clef
    reuses this module's retry loop, header construction and error text rather
    than a second copy of them. What is specific to Clef is the per account URL,
    the checkpoint in the body's `model` field, the id mapping, and the envelope
    reader. A failure raises: Clef is a hosted route with no fallback, so it is
    never a licence to call a second classifier.
    """
    token, _account = clef_credentials(api_key)
    endpoint, model = clef_route()
    safe_questions, restore = clef_question_ids(questions)
    # A score question needs an ordered list of level descriptions wherever the
    # answer is read on a legend index scale, so Clef is held to the same rule.
    validate_laya_questions(safe_questions)
    if transport is not None:
        raw = transport({"model": model, "state": state, "questions": safe_questions},
                        api_key=token, timeout=timeout, endpoint=endpoint)
        if not isinstance(raw, dict):
            raise JevSchemaError("transport returned a non-object")
        result = _clef_result(raw)
        result["answers"] = validate_clef_answers(result["answers"], safe_questions)
    else:
        result = _request_once(state, safe_questions, token, endpoint, model, timeout,
                               unwrap=_clef_result)
    # The caller's own question ids go back on, so nothing above this line can
    # tell that `candidate:aaa` was sent as `candidate_aaa`.
    return {**result, "answers": restore(result["answers"])}


def validate_laya_questions(questions: dict[str, Any]) -> dict[str, Any]:
    """Reject a question the local route can only answer on the wrong scale.

    A local score question derives its legend from ``criteria`` as an ordered
    list of level descriptions, index 0 first, and answers with the expected
    level on that index scale. A dict has no order to index and the server
    refuses the request, so the check happens here rather than as a 500.
    """
    for name, question in questions.items():
        if not isinstance(question, dict) or question.get("type") != "score":
            continue
        criteria = question.get("criteria")
        if not isinstance(criteria, list) or not criteria:
            raise JevSchemaError(
                f"question {name!r}: a local score question takes 'criteria' as a list of "
                "level descriptions, index 0 first; a dict has no level order to index"
            )
    return questions


def validate_laya_answers(answers: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
    """A local score stays on the legend index scale the question's levels define.

    The hosted route returns the expected level on the same index scale, so a
    value outside ``0..len(criteria)-1`` means the answer is on a different
    scale than the thresholds in ``approval_policy`` were written against, and
    the caller must not compare it to them.
    """
    for name, question in questions.items():
        if not isinstance(question, dict) or question.get("type") != "score":
            continue
        answer = answers.get(name)
        if not isinstance(answer, dict):
            continue
        criteria = question.get("criteria")
        if not isinstance(criteria, list) or not criteria:
            raise JevSchemaError(
                f"question {name!r}: a local score question takes 'criteria' as a list of "
                "level descriptions, index 0 first"
            )
        value = answer.get("score")
        if not _finite(value):
            raise JevSchemaError(f"answer {name!r} has invalid score")
        top = len(criteria) - 1
        if not 0 <= float(value) <= top:
            raise JevSchemaError(
                f"answer {name!r} scored {float(value)} outside the {len(criteria)} level local scale 0..{top}"
            )
        legend = answer.get("legend")
        if legend is not None and legend != {str(index): text for index, text in enumerate(criteria)}:
            raise JevSchemaError(
                f"answer {name!r} legend does not match the level descriptions it was asked about"
            )
    return answers


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def validate_answers(answers: Any, questions: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(answers, dict):
        raise JevSchemaError("Jev response answers must be an object")
    missing = [name for name in questions if name not in answers]
    if missing:
        raise JevSchemaError(f"Jev response omitted answers: {', '.join(missing)}")
    for name, answer in answers.items():
        if not isinstance(answer, dict):
            raise JevSchemaError(f"answer {name!r} must be an object")
        kind = questions.get(name, {}).get("type") if isinstance(questions.get(name), dict) else None
        if kind == "noul":
            value = answer.get("noul")
            if not _finite(value) or not 0 <= float(value) <= 1:
                raise JevSchemaError(f"answer {name!r} has invalid noul probability")
        elif kind == "score":
            value = answer.get("score")
            if not _finite(value):
                raise JevSchemaError(f"answer {name!r} has invalid score")
        elif kind == "choice":
            value = answer.get("choice")
            if not isinstance(value, str) or value not in questions[name].get("criteria", {}):
                raise JevSchemaError(f"answer {name!r} has an unknown choice")
        confidence = answer.get("confidence")
        if confidence is not None and (not _finite(confidence) or not 0 <= float(confidence) <= 1):
            raise JevSchemaError(f"answer {name!r} has invalid confidence")
    return answers


def request_decisions(
    state: Any,
    questions: dict[str, Any],
    api_key: str | None = None,
    *,
    model: str = MODEL,
    timeout: float = 30.0,
    transport: Callable[..., Any] | None = None,
    provider: str | None = None,
    fallback_api_key: str | None = None,
) -> dict[str, Any]:
    if not isinstance(questions, dict) or not questions:
        raise JevSchemaError("questions must be a non-empty object")
    mode = provider_mode(provider)
    order = provider_order(mode)
    local_first = order[0] in KEYLESS_PROVIDERS
    local = mode in KEYLESS_PROVIDERS
    if local and fallback_api_key:
        raise JevClientError(
            "JEV_PROVIDER_MODE=laya answers from a local server in place of the hosted "
            "providers, so there is no hosted fallback to authenticate"
        )
    if mode in FALLBACK_MODES:
        _require_hosted_keys(mode, order, api_key, fallback_api_key)
    if mode == CLEF_PROVIDER:
        # Clef alone is a hosted route with no fallback, so a failed review is
        # raised rather than degrading into another classifier. Its credentials
        # and its checkpoint are checked before the transport is touched.
        try:
            return _request_clef(state, questions, transport=transport, timeout=timeout,
                                 api_key=api_key)
        except JevClientError as exc:
            # Only the exception class is logged, never the reviewed state and
            # never the token. A missing variable is named by the exception
            # message itself, which is configuration rather than case content.
            logger.warning("hosted %s failed (%s); no fallback provider for this route",
                           mode, type(exc).__name__)
            raise
    if local_first:
        validate_laya_questions(questions)
    if transport is not None and mode not in FALLBACK_MODES:
        route_model = laya_route()[1] if local else model
        local_key = laya_key() if local else (api_key or "")
        if not local and (not api_key or not isinstance(api_key, str)):
            raise JevClientError("an API key is required for Jev reviews")
        payload = {"model": route_model, "state": state, "questions": questions}
        response = transport(payload, api_key=local_key, timeout=timeout)
        if not isinstance(response, dict):
            raise JevSchemaError("transport returned a non-object")
        answers = validate_answers(response.get("answers"), questions)
        if local:
            validate_laya_answers(answers, questions)
            # A local answer that passes validation is a healthy local call, so the
            # consecutive failure count restarts here.
            _reset_local_failures()
        return {**response, "answers": answers}
    routes: list[tuple[str, str, str, str]] = []
    hosted_index = 0
    for name in order:
        if name in KEYLESS_PROVIDERS:
            key = laya_key()
        else:
            key = api_key if hosted_index == 0 else fallback_api_key
            hosted_index += 1
        if name == LAYA_PROVIDER:
            endpoint, route_model = laya_route()
        elif name == CLEF_PROVIDER:
            endpoint, route_model = clef_route()
        elif name == TYPESAFE_PROVIDER:
            endpoint, route_model = TYPESAFE_ENDPOINT, TYPESAFE_MODEL
        else:
            endpoint, route_model = ENDPOINT, model
        routes.append((name, endpoint, route_model, key or ""))
    errors: list[str] = []
    attempts: list[dict[str, str]] = []
    for index, (name, endpoint, route_model, route_key) in enumerate(routes):
        if not route_key and name not in KEYLESS_PROVIDERS:
            message = f"{endpoint}: missing API key"
            errors.append(message)
            attempts.append({"provider": name, "error": message})
            continue
        try:
            if transport is not None:
                result = _transport_once(transport, state, questions, route_key, endpoint, route_model, timeout,
                                         provider=name)
            else:
                result = _request_once(state, questions, route_key, endpoint, route_model, timeout)
        except JevClientError as exc:
            # Only a local first chain turns a local failure into egress to a
            # hosted provider, so only that chain counts the failure or can
            # suppress one. The plain local mode and a hosted first chain have
            # neither a hosted hop to reach nor anything to suppress, so their
            # local failure leaves the count alone.
            if mode in LAYA_CHAIN_MODES and name in KEYLESS_PROVIDERS:
                if _note_local_failure():
                    # Only the exception class is logged, never the reviewed state.
                    logger.warning(
                        "local %s failed %d times in a row; hosted fallback suppressed, "
                        "re-raising the local error",
                        type(exc).__name__, LOCAL_FALLBACK_FAILURE_LIMIT,
                    )
                    raise
                logger.warning(
                    "local %s failed (%s); falling through to the hosted fallback",
                    name, type(exc).__name__,
                )
            errors.append(str(exc))
            attempts.append({"provider": name, "error": str(exc)})
            if len(routes) == 1:
                raise
            continue
        if name in KEYLESS_PROVIDERS:
            answers = result.get("answers")
            if isinstance(answers, dict):
                validate_laya_answers(answers, questions)
            # A local answer that passes validation is a healthy local call, so the
            # consecutive failure count restarts here.
            _reset_local_failures()
        return _with_routing(result, mode, routes, index, attempts)
    raise JevClientError("; ".join(errors))


def _require_hosted_keys(mode: str, order: tuple[str, ...], api_key: Any, fallback_api_key: Any) -> None:
    """Fail a ``laya_then_*`` selection when a named hosted provider has no key.

    The local attempt is allowed to fail, but only when the hosted hop it falls
    through to can actually run. A missing key is a selection error, so it is
    raised here, naming the environment variable, rather than surfacing later as a
    failed request after the local review was already sent.
    """
    keys = (api_key, fallback_api_key)
    missing = [
        name for index, name in enumerate(_hosted_key_names(order))
        if not keys[index]
    ]
    if missing:
        raise JevClientError(
            f"JEV_PROVIDER_MODE={mode} needs {', '.join(missing)} for its hosted fallback"
        )


def _transport_once(transport: Callable[..., Any], state: Any, questions: dict[str, Any],
                    api_key: str, endpoint: str, model: str, timeout: float,
                    provider: str = "") -> dict[str, Any]:
    """One chain hop through an injected transport, so a test can watch the route.

    Clef's id mapping and envelope reader are applied here rather than in a
    second chain implementation, so a Clef hop inside `clef_then_laya` behaves
    exactly as Clef does alone. The caller's own question ids go back on before
    the result leaves this function, so nothing above it can tell a rewritten id
    was sent.
    """
    if provider == CLEF_PROVIDER:
        safe_questions, restore = clef_question_ids(questions)
        validate_laya_questions(safe_questions)
        payload = {"model": model, "state": state, "questions": safe_questions}
        response = transport(payload, api_key=api_key, timeout=timeout, endpoint=endpoint)
        if not isinstance(response, dict):
            raise JevSchemaError("transport returned a non-object")
        result = _clef_result(response)
        result["answers"] = restore(validate_clef_answers(result["answers"], safe_questions))
        return result
    payload = {"model": model, "state": state, "questions": questions}
    response = transport(payload, api_key=api_key, timeout=timeout, endpoint=endpoint)
    if not isinstance(response, dict):
        raise JevSchemaError("transport returned a non-object")
    answers = validate_answers(response.get("answers"), questions)
    return {**response, "answers": answers}


def _with_routing(result: dict[str, Any], mode: str, routes: list[tuple[str, str, str, str]],
                  answered_index: int, attempts: list[dict[str, str]]) -> dict[str, Any]:
    """Add the routing diagnostics a chain has to report.

    Only a chain adds this block, so the single provider modes keep returning
    exactly the provider's own response. `provider` is the hop that answered,
    `fallback_used` says whether the successful hop was a fallback, and
    `attempts` records the earlier hops that failed.
    """
    if mode not in FALLBACK_MODES:
        return result
    return {
        **result,
        "provider_routing": {
            "provider": routes[answered_index][0],
            "provider_order": [name for name, *_ in routes],
            "fallback_used": answered_index > 0,
            "attempts": list(attempts),
        },
    }


def _is_clef_endpoint(endpoint: str) -> bool:
    """True when an endpoint is the Cloudflare Clef run route.

    Detected from the endpoint rather than passed down, because a chain hop
    reaches this function with only the endpoint it resolved, and Clef's id
    mapping and envelope reader must still apply when Clef is one hop of a chain
    rather than the whole route.
    """
    return endpoint.startswith(f"{CLEF_API_BASE}/")


def _typed_answers(result: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
    """Read a top level answers map under this repository's typed contract."""
    result["answers"] = validate_answers(result.get("answers"), questions)
    return result


def _clef_chain_answers(questions: dict[str, Any], safe_questions: dict[str, Any],
                        restore: Callable[[dict[str, Any]], dict[str, Any]]
                        ) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """A validator that unwraps a Clef envelope and maps question ids back.

    Clef's id mapping has to run before validation, because the ids it receives
    are the safe ones, and the reverse mapping has to run after, so the caller
    gets its own ids back even when Clef was one hop of a chain.
    """
    def validate(result: dict[str, Any]) -> dict[str, Any]:
        unwrapped = _clef_result(result)
        unwrapped["answers"] = restore(validate_clef_answers(unwrapped["answers"], safe_questions))
        return unwrapped

    return validate


def _request_once(state: Any, questions: dict[str, Any], api_key: str,
                  endpoint: str, model: str, timeout: float,
                  unwrap: Callable[[Any], dict[str, Any]] | None = None) -> dict[str, Any]:
    """One POST with the three attempt retry loop every hosted route shares.

    ``unwrap`` is the one place a route differs: OpenRouter and TypeSafe return
    the answers map at the top level, while Cloudflare's Clef route may also
    wrap it in a REST envelope. Passing the route's own envelope reader here
    keeps the retry, header, and error handling identical across routes instead
    of forking a second copy of the loop.

    A Clef endpoint is recognised from its shape, so a Clef hop inside a chain
    gets the same envelope reading and the same question id mapping it gets when
    Clef is the whole route, without the caller having to say so.
    """
    clef = unwrap is not None or _is_clef_endpoint(endpoint)
    if clef:
        safe_questions, restore_ids = clef_question_ids(questions)
        validate_laya_questions(safe_questions)
        payload = {"model": model, "state": state, "questions": safe_questions}
        validate = _clef_chain_answers(questions, safe_questions, restore_ids)
    else:
        payload = {"model": model, "state": state, "questions": questions}
        validate = lambda result: _typed_answers(result, questions)
    body = json.dumps(payload).encode("utf-8")
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0: break
        headers = {"Content-Type": "application/json"}
        # A keyless provider bound to the local machine carries no credential, so
        # the header is omitted entirely rather than sent as an empty bearer.
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if endpoint == ENDPOINT:
            headers.update({"HTTP-Referer": "https://hermes-agent.nousresearch.com", "X-Title": "Hermes Jev Decision Adapter"})
        request = Request(endpoint, data=body, method="POST", headers=headers)
        try:
            with urlopen(request, timeout=remaining) as response:
                raw = json.loads(response.read().decode("utf-8"))
            result = unwrap(raw) if unwrap is not None else raw
            return validate(result)
        except HTTPError as exc:
            last = exc
            if exc.code not in _RETRYABLE or attempt == 2:
                raise JevClientError(f"{endpoint} HTTP {exc.code}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError, JevSchemaError) as exc:
            last = exc
            if isinstance(exc, JevSchemaError):
                # A typed answer outside the contract is not a transient fault, so
                # it is never retried. It keeps its own type so a caller can still
                # tell a schema failure from a transport failure.
                raise
            if attempt == 2:
                raise JevClientError(str(exc)) from exc
        time.sleep(min(2**attempt, max(0.0, deadline - time.monotonic())))
    raise JevClientError(f"{endpoint} request failed: {last}")
