# Changelog

All notable changes to Jev Decisions are recorded here and in more detail under [`docs/releases/`](docs/releases/), one file per version. The release notes for a version are the canonical record; this file is the index.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses semantic versioning.

## [0.10.0] - 2026-10-08

Four fail-open defects where missing, ambiguous, or self-supplied evidence was read as a reason to permit. Full notes: [docs/releases/0.10.0.md](docs/releases/0.10.0.md).

### Security

- **A model DENY was upgraded to APPROVE by operator policy.** `apply_policy()` overwrote whatever the model decided whenever `policy_allows >= 0.7` and any operator policy text was present, without restricting the upgrade to a non-DENY verdict. Verified: a `DROP DATABASE prod`-class DENY at 0.99 confidence with a vague policy sentence returned `APPROVE`. Policy may still resolve an ESCALATE or confirm an APPROVE, which is the channel it is meant to be; a refusal the model was unsure about is escalated to a human. Fixed in both copies of the rule, `approval_policy.py` and the shipped `integrations/approval-provider/__init__.py`.
- **The supervised agent could weaken its own owner rule.** `lessons.edit()` had no owner check, while `retire()` and `sweep()` both refuse owner rules, so one tool call took a hard-stop rule from `kick` to `nudge` and the gate stopped firing. `edit` also rewrote `text`, `scope` and `detect`, and zeroed the escape counters. An owner rule is now exempt from `edit` in every field. Non-owner lessons remain editable.
- **`jev_supervision action=configure` let the agent being supervised turn supervision off.** `enabled` and `mode` were both settable through a tool the gated party calls, taking `enforcing()` from True to False and releasing the next blocked action. Both are refused at the model-facing boundary with an explicit error; `enabled` is removed from the tool schema as well. Operator code still configures through `Supervision.configure()` directly.
- **A timeout result consumed the control that was blocking a failing action.** `normalize_error()` recognised only `error`, `success is False` and `ok is False`, so `{"timeout": 30}`, `{"timed_out": true}`, `{"errors": [...]}` and a timeout string all rendered no failure signature and were recorded as success, consuming an active REPLAN control. The failure key set is now shared with `verification._ERROR_KEYS` and asserted equal by a test, and a new `_succeeded()` requires positive evidence of completion, reporting `outcome: "unknown"` and consuming nothing otherwise.

### Added

- `tests/test_fail_closed_regressions.py`, 29 tests pinning one invariant: when the evidence is missing, ambiguous, or supplied by the party being judged, the answer is never the permissive one. It covers both copies of the policy rule, every editable field on an owner rule, the enforcement switch from both the schema and the handler, and each result shape that must not consume a control.

### Changed

- `record_tool_outcome()` now reports three outcomes — `success`, `failure`, and `unknown` — instead of two. A result carrying no failure signature but no positive evidence of completion is `unknown`, which leaves the active control and the failure episode untouched.
- `version` is `0.10.0` in `pyproject.toml` and `plugin.yaml`.

### Known issues

- The decision journal is append-only JSONL with no hash chain or writer identity, so `record_outcome` lets the reviewed agent label its own review, and spamming `action=surfaced` auto-retires a learned kick lesson.
- Turn eviction drops a live session's blocking control, and `no_turn` under an enforcing mode reports `allow: True`.
- `metrics()` reports on a 5,000-row sample while `promotion_ready` reads off it as though it were the whole store.
- `tail_lines()` drops one row at exact block boundaries: `read(8192)` returns 8,191 rows.
- `ingest`'s module-level case maps grow without bound, and an unknown `invocation_id` closes the oldest open case for that tool name.
- `_boolean` fail-opens on any non-boolean value; `{"external": 1}` reads as not external.

## [0.9.0] - 2026-10-05

Bounded store reads, a cached `metrics()`, an observation comparator that reads error signals instead of substrings, and a supervision schema that no longer forbids what its own handler accepts. Full notes: [docs/releases/0.9.0.md](docs/releases/0.9.0.md).

### Fixed

- **`verify_observation` reported ordinary infrastructure states as unavailable.** It tested the lowercased text for `collector_error:`, `error`, and `timeout` as substrings, so a dict result such as `{"state": "errored"}` matched `error` and was reported `unavailable`. This ran on every hooked tool result, so a `status: unavailable` misreport wrote `awaiting_verification` into the closed loop for an observation the host had in fact verified. `errored`, `error_free`, `no_error`, and `timeout_reached` all now compare as the states they are. A collector failure prefix still reports `unavailable`, a dict carrying an `error`, `errors`, `timeout`, or `timed_out` key still reports it, and a bare string result keeps its documented text contract.
- **The `jev_supervision` schema forbade six fields its own handler accepted.** `configure` accepted seven settable fields, but `JEV_SUPERVISION_SCHEMA` declared `additionalProperties: False` without six of them, so through the tool boundary only `mode` could be changed. `enabled`, `admission_enabled`, `relevance_threshold`, `challenge_confidence`, `max_provider_calls_per_turn`, and `repeated_failure_replan_at` are now declared with the types the handler already accepted, so runtime behaviour is unchanged and only the boundary stops rejecting valid input. Both sides now read one `JEV_SUPERVISION_SETTABLE` tuple, and a test asserts the two cannot drift again.
- **`closed_loop.assess()` decoded the whole store on every call.** It called `_read()` with no limit and then ran `json.loads` on every row, to answer a question about one decision. It now walks the store in blocks and parses only lines naming that decision. On a 6.2 MB, 60,120-row store, 149 ms to 19 ms with identical output. Unlike `list_records`, it is not given a bounded tail, because a decision recorded before the window is a decision that exists and a tail read would report `found: False` for it. A decision ID that a writer escaped is still found: an empty prefiltered result falls back to the exact whole-store scan.
- **Two bare sibling imports broke package mode.** `closed_loop.py` and `approval_review.py` each imported a sibling above the `try` block every sibling module uses, so a host loading the directory as a package got `ModuleNotFoundError: No module named 'ledger'` and `No module named 'approval_policy'`. Both are now inside the `try`.

### Changed

- Bounded store reads. `ledger.read(limit)` read the whole file and sliced the result, so `read(10)` cost 96% of `read(5000)`; `closed_loop._read()` had no limit at all. Both now walk backwards from the end of the file. On a 52,500-row store `read(10)` went from 11.31 ms to 0.33 ms.
- Cached `metrics()` on `(mtime_ns, size)`. It is a pure function of an append-only file, and both `digest()` and the gateway snapshot call it: 17.46 ms cold to 0.004 ms warm, values identical.
- `closed_loop.list_records()` now reads only as far back as it returns.
- `plugin.yaml` reported `0.8.0` while packaging read `0.9.0` from `pyproject.toml`, so the published manifest advertised the wrong version. Both now say `0.9.0`.

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
