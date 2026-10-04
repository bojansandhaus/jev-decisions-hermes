# Changelog

All notable changes to Jev Decisions are recorded here and in more detail under [`docs/releases/`](docs/releases/), one file per version. The release notes for a version are the canonical record; this file is the index.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses semantic versioning.

## [0.8.0] - 2026-10-04

Implements the shared four mode provider contract and turns the local `laya` provider into a generic local decision model slot that any local System One decision model can occupy. Full notes: [docs/releases/0.8.0.md](docs/releases/0.8.0.md).

### Added

- **Four canonical modes**: `api_with_local_fallback`, `api_only`, `local_only`, `local_with_api_fallback`. Each names which side leads and whether the other side is a fallback. `api_only` and `local_only` are single provider routes whose failures are reported, never rerouted.
- **`JEV_LOCAL_MODEL`**: selects which engine answers inside the `laya` slot, which takes **Laya or other pre-deterministic routing models**; the engine is a local System One decision model. Defaults to `english`, so a default configuration sends the request it always sent. There is no allowlist of model names: an engine nobody has heard of works by configuration alone. Only an empty or whitespace value, or one carrying a character that would corrupt a JSON string or a URL path segment, is refused.
- **Hosted first chains**: `typesafe_then_laya`, `openrouter_then_laya`, `clef_then_laya`. These are what `api_with_local_fallback` routes with, and they are the mirror of the existing `laya_then_*` local first chains.
- **Three aliases**: `jev_api` (newly accepted, resolving to `api_only` with the hosted side chosen by credential), `clef_with_local_fallback`, and `laya_then_hosted`.

### Changed

- A local server may now trail a chain as well as lead one. It may still not sit between two hosted hops, and a chain of `laya` alone is still refused.
- `uses_local_hop` reports true whenever a chain contains the local slot, so a hosted first chain gets the longer local budget.
- Clef's question id mapping and response envelope reader are applied wherever a Clef endpoint appears, including inside a chain, by recognising the endpoint rather than forking a second code path.
- Every statement that Clef or the local slot has "no fallback" is qualified, because `clef_then_laya` makes the unqualified claim false.
- The public documentation now describes the local routes as **Laya or other pre-deterministic routing models**, and the vendor-specific engine names it previously listed are no longer published. The setting itself is unchanged: `JEV_LOCAL_MODEL` is still a free-form engine name and still accepts any local engine.

### Compatibility

- No setting was removed and no setting silently changed meaning. `JEV_LAYA_MODEL` still works and is used when `JEV_LOCAL_MODEL` is unset; `JEV_LOCAL_MODEL` wins when both are set.
- Every mode name and alias that existed in 0.7.0 keeps its exact provider order, its required keys, its routing block and its error text.
- The default is still `openrouter`, and the default local request still asks for `english`.
- Two existing assertions were updated because the change intentionally alters the behaviour they encoded; they were merged into a single test rather than weakened, and the release notes name both.

### Not verified

- **No live call was made to any provider.** No local model other than the default `english` checkpoint has ever been called by this repository. Every test drives an injected transport or a captured socket.

## [0.7.0] - 2026-10-04

Adds Cloudflare Clef as a third hosted decision provider, beside Jev over TypeSafe and OpenRouter and beside the local route. Purely additive: one provider name, one checkpoint setting, two Cloudflare environment variables, and the `clef_api` alias. Notes: [docs/releases/0.7.0.md](docs/releases/0.7.0.md).

## [0.6.1] - 2026-09-26

Ports the DOGA fallback breaker, log hygiene guarantee, and selector aliases. Notes: [docs/releases/0.6.1.md](docs/releases/0.6.1.md).

## [0.6.0] - 2026-09-26

Adds the local first chains (`laya_then_*`) with the consecutive failure breaker and the `provider_routing` block. Notes: [docs/releases/0.6.0.md](docs/releases/0.6.0.md).

## [0.5.0] - 2026-09-26

Adds the local Laya route with measured limits of the base English checkpoint. Notes: [docs/releases/0.5.0.md](docs/releases/0.5.0.md).

## [0.4.1] - 2026-09-25

Notes: [docs/releases/0.4.1.md](docs/releases/0.4.1.md).

## [0.4.0] - 2026-09-25

Notes: [docs/releases/0.4.0.md](docs/releases/0.4.0.md).

## [0.3.0] - 2026-09-22

Notes: [docs/releases/0.3.0.md](docs/releases/0.3.0.md).

## [0.2.1] - 2026-09-20

Notes: [docs/releases/0.2.1.md](docs/releases/0.2.1.md).

## [0.2.0] - 2026-09-20

Notes: [docs/releases/0.2.0.md](docs/releases/0.2.0.md).

## [0.1.0]

Notes: [docs/releases/0.1.0.md](docs/releases/0.1.0.md).