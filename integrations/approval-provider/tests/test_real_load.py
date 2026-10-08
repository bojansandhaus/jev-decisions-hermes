#!/usr/bin/env python3
"""Is the provider reachable the way core reaches it? TWO registries must both have it.

`plugins doctor` proves neither: it calls `register(ctx)` itself and only inspects the
plugin manager. Two silent failure modes this catches:

  1. `providers` registry missing -> get_provider_profile() returns None, no client built.
  2. `hermes_cli.auth.PROVIDER_REGISTRY` missing -> resolve_provider_client logs
     "unknown provider" and returns (None, None); auxiliary.approval falls back with no
     visible error.

(2) is the one that actually broke. PROVIDER_REGISTRY is built at hermes_cli.auth IMPORT
time by walking list_providers(), so a provider that registers later — e.g. from
register(ctx) under `kind: standalone` — never lands in it. `kind: model-provider` is
required precisely because providers/ discovery imports the module during that walk.

Needs the host on sys.path; no API key required.
"""
import os
import sys

for _candidate in (os.environ.get("HERMES_AGENT_DIR"),
                   os.path.expanduser("~/.hermes/hermes-agent")):
    if _candidate and os.path.isdir(_candidate):
        sys.path.insert(0, _candidate)
        break

try:
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
except ImportError as exc:
    # This module is collected by pytest as part of the whole suite, so a
    # SystemExit here aborts collection of every provider test rather than
    # skipping this one. Ask pytest to skip the module instead.
    import pytest

    pytest.skip(
        f"Hermes core not importable ({exc}). Set HERMES_AGENT_DIR.",
        allow_module_level=True,
    )

# The plugin id and provider name are independent. The provider is OpenRouter only.
PLUGIN_ID = "jev-approvals"
PROVIDER = "jev-decisions-approval"
ALIASES = ()

# Import order mirrors core's: auth first (building PROVIDER_REGISTRY), then discovery.
from hermes_cli.auth import PROVIDER_REGISTRY  # noqa: E402

discover_plugins(force=True)
mgr = get_plugin_manager()

plugin = mgr._plugins.get(PLUGIN_ID)
assert plugin is not None, f"{PLUGIN_ID} was not discovered at all"
print(f"kind={plugin.manifest.kind}  enabled={plugin.enabled}  error={plugin.error}")
assert plugin.manifest.kind == "model-provider", (
    f"kind is {plugin.manifest.kind!r}; only 'model-provider' is imported by providers/ "
    f"discovery, which is what puts the name into PROVIDER_REGISTRY")

# 1. the providers registry — what actually builds the client
from providers import get_provider_profile  # noqa: E402

profile = get_provider_profile(PROVIDER)
assert profile is not None, f"{PROVIDER} did not self-register at import"
assert profile.name == PROVIDER, f"profile name is {profile.name!r}"
assert tuple(profile.aliases) == ALIASES, (
    f"aliases are {tuple(profile.aliases)!r}, expected {ALIASES!r} — every registered name "
    f"is one more entry in the auxiliary auto-fallback chain, where Jev cannot serve")
print(f"providers registry: {type(profile).__name__} name={profile.name} "
      f"aliases={tuple(profile.aliases)}")

# 2. PROVIDER_REGISTRY — what resolve_provider_client validates the name against
assert PROVIDER in PROVIDER_REGISTRY, (
    f"{PROVIDER!r} is NOT in PROVIDER_REGISTRY — resolve_provider_client rejects it as "
    f"'unknown provider' and auxiliary.approval silently falls back")
for alias in ALIASES:
    assert alias in PROVIDER_REGISTRY, f"alias {alias!r} missing from PROVIDER_REGISTRY"
print(f"PROVIDER_REGISTRY: {PROVIDER} + {list(ALIASES)} present")

# 3. the route core actually takes, for both endpoints
from agent.auxiliary_client import resolve_provider_client  # noqa: E402

for label, base_url, model in (
    ("openrouter", "https://openrouter.ai/api/alpha", "~typesafe/jev-latest"),
):
    client, final_model = resolve_provider_client(
        PROVIDER, model=model,
        explicit_base_url=base_url or None, explicit_api_key="fixture",
    )
    assert client is not None, f"{label}: resolve_provider_client returned no client"
    assert type(client).__name__ == "JevClient", (
        f"{label}: core built {type(client).__name__}, not our client — a base_url may have "
        f"collapsed the route to the generic OpenAI path")
    print(f"{label:<16} -> JevClient  model={final_model!r}  "
          f"route_default={client._default_model()!r}")

# 3b. THE DOCUMENTED CONFIG SHAPE. A key alongside base_url in auxiliary.<task> config
# collapses the provider to "custom" (auxiliary_client.py: `if cfg_base_url and
# cfg_api_key`), which bypasses this plugin entirely — so the OpenRouter config must carry
# base_url and NO api_key/key_env, and the plugin resolves the key from the openrouter
# credential pool itself. This asserts the README stays true.
import agent.auxiliary_client as _aux  # noqa: E402
from agent.auxiliary_client import _resolve_task_provider_model  # noqa: E402

_real_task_cfg = _aux._get_auxiliary_task_config
_OR = "https://openrouter.ai/api/alpha"
try:
    for label, task_cfg, want in (
        ("base_url, no key (documented)",
         {"provider": PROVIDER, "model": "~typesafe/jev-latest", "base_url": _OR}, PROVIDER),
        # An inline api_key is the unambiguous case: `key_env` only resolves to a value
        # when that variable is actually exported, so it collapses the provider only on
        # machines where it is set — a nastier, environment-dependent version of the same
        # bug. Neither belongs in the documented config.
        ("base_url + api_key (must NOT be documented)",
         {"provider": PROVIDER, "model": "~typesafe/jev-latest", "base_url": _OR,
          "api_key": "sk-or-inline"}, "custom"),

    ):
        _aux._get_auxiliary_task_config = lambda _t, _c=task_cfg: _c
        got = _resolve_task_provider_model("approval")[0]
        assert got == want, f"{label}: provider resolved to {got!r}, expected {want!r}"
        print(f"  {label:<44} -> {got}")
finally:
    _aux._get_auxiliary_task_config = _real_task_cfg

# 3c. The model catalog path uses core's exact fetch_models signature.
from hermes_cli.models import provider_model_ids  # noqa: E402

live = profile.fetch_models(api_key="", base_url="")          # core's exact call shape
assert live, "fetch_models returned nothing for the default route"
assert profile.fetch_models(api_key="", base_url="", future_kwarg=1), "must absorb new kwargs"
ids = provider_model_ids(PROVIDER)
assert ids, f"provider_model_ids({PROVIDER!r}) is empty — the picker would show no models"
print(f"picker: provider_model_ids -> {ids}")

# 4. the host -> endpoint mapping, including that an unknown host is not the alpha route
import importlib.util  # noqa: E402
import pathlib  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "tsj_probe", pathlib.Path(__file__).resolve().parent.parent / "__init__.py")
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

assert mod._route_for("https://openrouter.ai/api/alpha")[0] == "/decisions"
assert mod._route_for("https://OPENROUTER.AI/api/alpha")[0] == "/decisions"
for unsupported in (
    "https://example.com/v1",
    "https://evilopenrouter.ai/api/alpha",
    "https://openrouter.ai.evil.test/api/alpha",
):
    try:
        mod._route_for(unsupported)
    except RuntimeError:
        pass
    else:
        raise AssertionError(f"non OpenRouter route was accepted: {unsupported}")
# the OpenRouter model list must carry the filter, or it pulls the whole 447-model catalogue
assert "output_modalities=decisions" in mod._route_for("https://openrouter.ai/api/alpha")[1]
print("route_for: OpenRouter -> /decisions, all other hosts rejected")

# 5. OpenRouter credential resolution prefers the Hermes pool, then the environment.
_real_setting = mod._setting
_real_pool = mod._key_from_runtime_provider
_real_dotenv = mod._key_from_dotenv
try:
    mod._key_from_runtime_provider = lambda _p: ""
    mod._key_from_dotenv = lambda: ""
    os.environ["OPENROUTER_API_KEY"] = "«redacted:sk-…»"
    assert mod._api_key("https://openrouter.ai/api/alpha") == "«redacted:sk-…»"

    # A TypeSafe endpoint must never be accepted, even if its legacy key exists.
    mod._key_from_dotenv = lambda: ""
    os.environ["TYPESAFE_API_KEY"] = "sk-typesafe"
    try:
        mod._api_key("https://api.typesafe.ai/v1")
    except RuntimeError:
        pass
    else:
        raise AssertionError("legacy TypeSafe route accepted")

finally:
    mod._setting = _real_setting
    mod._key_from_runtime_provider = _real_pool
    mod._key_from_dotenv = _real_dotenv
    for _v in ("OPENROUTER_API_KEY", "TYPESAFE_API_KEY"):
        os.environ.pop(_v, None)
print("OpenRouter route: pool first, environment fallback, legacy direct route rejected")

print("\nboth registries have the provider, and both routes build our client")
