# Jev Decisions: Technical reference

Installation for other agents, tool names, review definitions, and executable examples. Start with the [product README](../README.md) for Hermes setup and everyday use.

### Install the standalone Python and CLI gateway

Use a separate virtual environment:

```bash
git clone https://github.com/bojansandhaus/jev-decisions.git
cd jev-decisions
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install .
```

On Windows PowerShell, activate the environment with `.venv\Scripts\Activate.ps1` instead. The examples below use a POSIX shell.

Standalone installation provides the local gateway, CLI, and supporting Python modules. Hermes loads its registration layer separately; deployment specific collectors are not included.

This installs the `jev-gateway` command. It evaluates JSON supplied by your runner; it does not launch another agent or connect to your services. There is no need to configure OpenRouter for local `decide` and `verify` calls.

### In the terminal: check policy, then evidence

```bash
printf '%s\n' '{"action":"delete_backup","external":true,"reversible":false,"destructive":true,"credential":false}' | jev-gateway decide
```

Inspect the `decision` field. A destructive operation requires `human` regardless of a model's opinion.

Now describe a changed target that has not been checked:

```bash
printf '%s\n' '{"changed":true,"read_back":false,"evidence":false}' | jev-gateway verify
```

The verification result should contain `"verified": false` and `"next": "read_back"`. Missing or non-boolean change evidence returns `establish_change`; a readback without evidence returns `inspect_evidence`. The gateway requires all three proof fields to be the boolean `true` before returning `done`.

After your agent actually reads the exact target and confirms the requested result, it can submit:

```bash
printf '%s\n' '{"changed":true,"read_back":true,"evidence":true}' | jev-gateway verify
```

The result should contain `"verified": true` and `"next": "done"`. These inputs are deliberately simple fixtures. In a real integration, derive them from observed results. Setting them to `true` without checking anything defeats the purpose.

## Hermes tools and hooks

The plugin exposes eight tools under the `jev` toolset.

<table>
<tr><th>Tool</th><th>Use it for</th><th>Provider required?</th></tr>
<tr><td><code>jev_decide</code></td><td>Custom typed questions against bounded state.</td><td>Yes, a hosted key or local Laya</td></tr>
<tr><td><code>jev_workflow</code></td><td>A prepared review such as <code>plan_review</code>, <code>output_review</code>, or the opt in <code>approval_review</code>.</td><td>Yes, a hosted key or local Laya</td></tr>
<tr><td><code>jev_gateway</code></td><td>Local action policy, verification, domain classification, and ledger snapshot.</td><td>No</td></tr>
<tr><td><code>jev_ingest</code></td><td>Open a rule classified case from supplied event data.</td><td>No</td></tr>
<tr><td><code>jev_ledger</code></td><td>Record reviews, labeled outcomes, commitments, and decisions; inspect metrics.</td><td>No</td></tr>
<tr><td><code>jev_loop</code></td><td>Link decisions, observations, and outcomes; assess the resulting history.</td><td>No</td></tr>
<tr><td><code>jev_supervision</code></td><td>Turn admission, adaptive event routing, challenge freshness, and repeated failure controls.</td><td>No</td></tr>
<tr><td><code>jev_lessons</code></td><td>Learned corrections: severity escalation, catch and escape tallies, noise retirement, and lesson packs.</td><td>No</td></tr>
</table>

The `approval_review` workflow is an opt in advisory check for flagged commands. It returns typed answers and a deterministic applied rule; it never authorizes execution. Configure the optional provider through [the approval guide](approvals.md).

Three optional hooks observe the agent lifecycle when `JEV_ENABLE_HOOKS` is enabled:

- `pre_tool_call` reviews proposed tool use.
- `post_tool_call` reviews the returned result.
- `post_llm_call` reviews the answer against the supplied request and context.

Optional platform event handlers can also record event metadata for Home Assistant, email, Telegram, Discord, Matrix, and DingTalk when the host exposes the required adapter API. They do not send messages or control those services. This is an integration surface, not a guarantee that every platform version has been tested.

Hooks return no approval, replacement answer, or execution instruction. They may record local review data. Model reviews can add provider requests and latency when enabled; see the configuration and privacy details in [the integration guide](integrations.md).

## Tool actions and records

### Local policy: `jev_gateway`

- `decide`: apply local rules to `state`. Destructive actions, credential changes, and irreversible external actions recommend `human`; other external actions recommend `suggest`; internal work recommends `observe`.
- `verify`: require explicit boolean `changed`, `read_back`, and `evidence` fields. Missing or invalid proof cannot produce a verified result.
- `classify`: classify supplied `state` within a required `domain` using local rules.
- `snapshot`: return the timestamp and ledger metrics.

Use JSON booleans, never strings such as `"false"`. The policy falls back to its defaults for omitted or invalid boolean fields: `reversible` defaults to true; `external`, `destructive`, and `credential` default to false. Supply these facts explicitly and validate them in your host. The rule is not a shell command parser or an enforcement gate. The `success` field on a tool response describes the handler result, not proof that your external task succeeded. Inspect `verified` and `next` when checking evidence.

### Review ledger: `jev_ledger`

- `record_review`: create a review with `details`; `status` supplies its workflow label. Supplying an existing `review_id` returns that ID without creating another review.
- `record_outcome`: attach boolean `correct` and optional `details` to a `review_id`.
- `add_commitment` and `add_decision`: record nonempty `text`, optional `owner`, `deadline`, `status`, and `details`.
- `close`: append a closure against a `review_id`. This preserves the earlier entry.
- `list`: return the latest 200 entries.
- `metrics`: summarize recorded reviews, outcomes, labels, and record kinds. These figures describe supplied labels, not an independent benchmark of model quality.
- `verify_chain`: walk the ledger and report whether its hash chain still holds. `intact: false` names the record id where the walk stopped. Every record carries the id and content hash of the one before it, so removing or rewriting a record breaks the chain from that point onward. Records written before the chain linked to nothing and are reported as an unlinked head rather than as a break, so a ledger that predates the feature is not accused of tampering. This is an operator check: it makes truncation and rewriting detectable, and it does not stop anybody who can write the file from writing it.

### Decision journal: `jev_loop`

- `record_decision`: record `question`, `chosen`, optional `options`, `evidence`, `assumptions`, `owner`, and `deadline`. Keep the returned `decision_id`.
- `record_observation`: attach an `observation` and `source` to that ID. Omit `supports` when support is unknown.
- `record_outcome`: attach `status`, optional boolean `success`, `evidence`, and `notes`. Omitted success remains unknown.
- `label_outcome`: attach an explicit boolean `success`, supporting `evidence`, optional `labeler`, and `notes`. The default labeler is `user`; this field is attribution supplied by the caller, not authenticated identity.
- `reopen`: append a reason and optional evidence for revisiting a decision.
- `assess`: summarize the decision's observations and labeled outcomes, then recommend collecting an outcome, continuing observation, reviewing behavior, or reviewing new evidence.
- `list`: return recent journal rows; `limit` is clamped to 1 through 500.
- `verify_observation`: compare a supplied observation with an explicit expected value, without a provider request or journal write.

Assessment uses explicit outcome labels when present, otherwise known boolean outcomes. Unknown outcomes do not enter the success fraction. Multiple labels count separately; the fraction is descriptive, not a statistical guarantee. A reopen record continues to request review of new evidence. The journal does not train a model or change execution rules.

### Observation comparison

Call `jev_loop` with:

```json
{
  "action": "verify_observation",
  "source": "home_assistant",
  "expected": "on",
  "result": {"state": "off"}
}
```

This returns `verified: false`, `status: "mismatch"`, and `next: "read_back_expected"`. A matching `on` observation returns `matched`. Without an expected value, the next step is `compare_expected`. Python callers can use `verification.verify_observation(source, context, result)`.

The comparator trims whitespace and ignores letter case. For an object result it compares the `state` field; otherwise it compares the supplied result as text. `expected_state` and `expected_status` are aliases for `expected`. A failure signal produces `unavailable`: a `collector_error:` prefix on the result, an `error`, `errors`, `timeout` or `timed_out` KEY in an object result, or the words `error` or `timeout` in a bare string result, which has no structure to read keys from. A key that is present but empty, such as `{"error": null}`, is not a failure. This is a small text/state comparator, not a general object validator or service health client. It does not contact a device, check timestamps, or prove that the observation belongs to the requested target. The host must establish those facts.

### Event cases: `jev_ingest`

Supply `source`, `event_type`, and a `payload` object. Ingestion routes the event to a domain, applies local classification, and returns a case ID. Python integrations can call `fabric.open_case`, `fabric.close_case`, and `fabric.queue`; queue filters accept status and domain. Tool result correlation uses invocation IDs when supplied. Supply those IDs for overlapping calls to the same tool.

### Local supervision: `jev_supervision`

This tool answers from local state and makes no provider request. Actions:

<table>
<tr><th>Action</th><th>Effect</th></tr>
<tr><td><code>status</code></td><td>Bounded telemetry: mode, authority, counters, and the current control.</td></tr>
<tr><td><code>begin_turn</code> / <code>end_turn</code></td><td>Create or close a supervised turn. Admission is local.</td></tr>
<tr><td><code>observe_event</code></td><td>Route one structured event and report whether a remote opinion is warranted.</td></tr>
<tr><td><code>consider_challenge</code></td><td>Record a disagreement. Only high confidence disagreement becomes a challenge.</td></tr>
<tr><td><code>take_challenge</code></td><td>Take one still-current challenge. A stale one is retained as telemetry instead.</td></tr>
<tr><td><code>check_control</code></td><td>Ask whether the exact action is under a local control, and whether it may run.</td></tr>
<tr><td><code>record_tool_outcome</code></td><td>Fingerprint a tool outcome; identical failures accumulate into a control.</td></tr>
<tr><td><code>allow_retry</code></td><td>Convert an active blocking control into one permitted retry.</td></tr>
<tr><td><code>configure</code></td><td>Change mode, thresholds, or the repeated failure threshold for this process.</td></tr>
</table>

Modes are `off`, `shadow`, `correct_next`, and `precommit`. `shadow` is the default and changes no execution. `correct_next` and `precommit` enforce the local control lease through the `pre_tool_call` veto shape, `{"action": "block", "message": ...}`, and only for the exact action fingerprint that created the control.

A control lease can constrain a repeated action. It can never authorize one, and it does not replace approval or verification. Those remain in `jev_gateway`.

Supervised turns are turn scoped and evicted after sixteen tracked turns when no `end_turn` arrives. Fingerprints and counters hold hashes, labels, and counts, never raw arguments or tool results.

Wording in this layer is adapted from `keeltrace/hermes-jev`; see `THIRD_PARTY_NOTICES.md` for the reviewed revision and the upstream license.

### Learned corrections: `jev_lessons`

A lesson is a rule plus a detect line: the mistake described as the action that is about to happen. Actions:

<table>
<tr><th>Action</th><th>Effect</th></tr>
<tr><td><code>add</code></td><td>Record a correction. A repeat of an existing lesson merges into it and counts an escape.</td></tr>
<tr><td><code>list</code> / <code>get</code></td><td>Inspect lessons, including retired ones with <code>include_retired</code>.</td></tr>
<tr><td><code>edit</code></td><td>Correct a severity, an escape count, a catch count, or the detect line, keeping the record.</td></tr>
<tr><td><code>retire</code> / <code>sweep</code></td><td>Retire one lesson by hand, or retire every lesson that surfaced 40 or more times without ever catching or escaping.</td></tr>
<tr><td><code>candidates</code></td><td>Pre-rank lessons against an action. A shortlist for a semantic judgement, not a verdict.</td></tr>
<tr><td><code>caught</code> / <code>surfaced</code></td><td>Record that a lesson caught a mistake, or that it was judged relevant.</td></tr>
<tr><td><code>export</code> / <code>import</code></td><td>Move proven lessons between installs as a pack. An imported lesson starts with no track record.</td></tr>
<tr><td><code>stats</code></td><td>Counts by status, severity, and source.</td></tr>
</table>

Severity is `nudge` or `kick`. A new lesson is a nudge; a lesson with `source=owner` starts as a kick and is never retired. A nudge becomes a kick once it has escaped twice.

In an enforcing supervision mode, a kick lesson whose wording closely matches an action blocks that action through the same `pre_tool_call` veto shape the control lease uses, and its catch and surfaced tallies are incremented. In `shadow` mode nothing is blocked, nothing is counted as caught, and the match is recorded to the ledger as `lesson_would_kick`.

The local match runs over canonical words, so the named paraphrase families meet (`remove`/`delete`/`wipe`/`clear`, `tmp`/`temporary`, `dir`/`folder`, `-rf`/`recursive`) and a paraphrased action can be stopped locally. Reach is bounded twice over: by that table, and by a minimum number of words in common, so a short lesson cannot stop a long action on a containment score alone. A rephrasing that shares no canonical word still slips past the local gate, and that residual is deliberate, because the semantic judgement stays with Jev through an explicit review.

Recording `surfaced` from the prefilter would inflate the tally and retire good lessons, so only a real judgement counts: a local kick match, or a review that returned which lessons applied.

Design adapted from psygns's osENV.io, with no code copied. See `THIRD_PARTY_NOTICES.md`.

## Reports and supporting commands

Only `jev-gateway` is installed as a named console command. Other packaged entry points run as Python modules:

```bash
printf '%s\n' '{}' | python3 -m jev_case list
printf '%s\n' 'I will review the draft.' | python3 -m jev_cockpit
python3 -m shadow_report
```

`jev_case` supports `open`, `close`, and `list`, reading a JSON object from stdin. Use its command help for case ID, domain, and status options. Opening and closing cases writes local records. `jev_cockpit` prints a case/ledger snapshot and, when text is supplied, candidate commitments. Detection is a simple English phrase rule; candidates are not confirmed obligations.

The Python `cockpit` module also exposes `promote_commitment`, `stale_cases`, and `digest`. Promotion writes a reviewed commitment. The other functions summarize existing records. These functions are not additional registered Hermes tools or a graphical dashboard.

`shadow_report` summarizes automatic review requests, answer probabilities, reported cost, ledger labels, and journal outcomes. Request success is not task success. Its default input is `~/.hermes/logs/jev-shadow.jsonl`; use its log path option for a named profile or standalone storage. Its help also offers JSON output. Reports depend on the records you retained and labels you supplied. Journals are append-only in normal use, but are not cryptographically tamper evident and can be changed by someone with filesystem access.

The package currently uses POSIX file locking for journal writes. Linux is exercised by CI. Native Windows is not supported by this locking implementation; use a Linux environment such as WSL rather than assuming the PowerShell activation example above establishes platform compatibility.

## Prepared review catalog

The plugin includes 25 named workflows. Supply only the context each question needs.

<details>
<summary><strong>Goals, plans, and answers</strong></summary>

- `goal_judge`: assess task completion, blockers, and result quality.
- `plan_review`: check the outcome, prerequisites, and recovery path.
- `output_review`: check grounding, coverage, actionability, and remaining risk.
- `next_action`: choose a concrete next step or identify a blocking question.
- `decision_circling`: identify repeated analysis that no longer advances a choice.

</details>

<details>
<summary><strong>Actions, authority, and verification</strong></summary>

- `command_review`: assess risk and recommend allowing, asking, or denying.
- `action_verify`: assess whether supplied evidence supports completion.
- `tool_result_verify`: review tool output and the required followup.
- `verification_depth`: choose the amount of checking an action deserves.
- `escalation`: identify uncertainty about intent, permission, risk, or evidence.

</details>

<details>
<summary><strong>Memory and evidence</strong></summary>

- `memory_gate`: review durability, type, and sensitivity.
- `memory_review`: review usefulness, type, and conflicts.
- `memory_maintenance`: recommend keeping, merging, refreshing, quarantining, or discarding.
- `recall_rerank`: review relevance and conflicts in a candidate memory set.
- `claim_status`: classify a claim as observed, inferred, assumed, unverified, or contradicted.
- `evidence_review`: assess source support, citation needs, and uncertainty.

</details>

<details>
<summary><strong>Choices, operations, and communication</strong></summary>

- `agent_referee`: compare supplied candidate outputs.
- `option_select`: compare labeled options against an objective.
- `purchase_review`: assess fit, evidence, and the next purchasing step.
- `anomaly_review`: identify unusual behavior and its severity.
- `daily_anomaly`: review a deviation from a supplied baseline.
- `infrastructure_review`: assess operational risk, backup, and verification.
- `document_quality`: review duplication, metadata, and extracted facts.
- `communication_review`: review readiness to send, commitments, and sensitive content.
- `promotion_review`: review whether evidence supports a narrower, stricter integration.

</details>

These are review definitions, not independent service connectors. For example, `document_quality` evaluates what you provide; it does not retrieve your document archive. Option selection and agent comparison use the labels defined by their questions. Use `jev_decide` when you need a different choice set.

## Custom questions

Use `jev_decide` when the prepared workflows do not fit. Each question needs a type, instructions, and criteria that define the possible answers.

- **Yes/no probability (`noul`)** returns a number from 0 to 1 for a true or false question, rather than a boolean verdict.
- **Choice** selects from named alternatives.
- **Score** evaluates against an ordered list of criteria.

Example tool arguments:

```json
{
  "state": {
    "task": "Update a test document",
    "backup_exists": true,
    "readback_planned": false
  },
  "questions": {
    "ready": {
      "type": "noul",
      "instructions": "Does the plan include a direct check of the edited document?",
      "criteria": {
        "true": "The plan includes reading and comparing the edited document",
        "false": "The plan lacks a direct comparison after editing"
      }
    },
    "next": {
      "type": "choice",
      "instructions": "What should the host do before executing this plan?",
      "criteria": {
        "proceed": "The plan covers recovery and verification",
        "revise": "Add the missing verification step",
        "ask": "Clarify a missing permission"
      }
    }
  }
}
```

Returned probabilities and confidence describe the model's judgment. They do not prove that a statement is true or that an action is authorized. Keep questions specific enough that a later observation can confirm or contradict them.

## Provider settings and the local route

There are three ways to answer a typed question: a hosted provider, which is Jev over a TypeSafe or OpenRouter key or Clef at Cloudflare Workers AI; Laya locally with no key; or an opt in chain that starts at the local server and falls through to a hosted provider. You pick one with `JEV_PROVIDER_MODE`.

| Setting | Default | Meaning |
|---|---|---|
| `JEV_PROVIDER_MODE` | `openrouter` | Any of the four canonical modes (`api_with_local_fallback`, `api_only`, `local_only`, `local_with_api_fallback`), any of the concrete mode names below, or an alias. See [the mode table](#the-four-canonical-modes) and the [alias table](#mode-aliases). |
| `typesafe`, `openrouter`, `clef` | | A single hosted provider, no fallback. These are `api_only` on a pinned provider. |
| `typesafe_then_openrouter`, `openrouter_then_typesafe` | | Two hosted providers. `clef` is never a member: it has no fallback and nothing appends it to a mode that does not name it. |
| `laya_then_typesafe`, `laya_then_openrouter` | | The local slot first, one hosted provider behind it. |
| `laya_then_typesafe_openrouter`, `laya_then_openrouter_typesafe` | | The local slot first, both hosted providers behind it. |
| `typesafe_then_laya`, `openrouter_then_laya`, `clef_then_laya` | | One hosted provider first, the local slot behind it. These are what `api_with_local_fallback` routes with. New in 0.8.0. |
| `TYPESAFE_API_KEY` | unset | Credential for the direct TypeSafe route. Required by the `typesafe` modes and by every `laya_then_*` mode that names TypeSafe. |
| `OPENROUTER_API_KEY` | unset | Credential for the OpenRouter route. Required by the `openrouter` modes and by every `laya_then_*` mode that names OpenRouter. |
| `CLOUDFLARE_API_TOKEN` | unset | Credential for the Cloudflare Clef route: a Cloudflare API token with **Account > Workers AI > Read**. Required by the `clef` mode and by the `clef_api` alias. |
| `CLOUDFLARE_ACCOUNT_ID` | unset | The Cloudflare account the run endpoint is scoped to, 32 lowercase hex characters. Configuration rather than a secret, but it does appear in the request URL. Required by the `clef` mode and by the `clef_api` alias. |
| `JEV_CLEF_MODEL` | `clef` | The Clef checkpoint: `clef` or `clef-flash`. A checkpoint of the one Clef provider, not a second provider. |
| `LAYA_API_KEY` | unset | Optional bearer for a `laya-serve` started with its own `LAYA_API_KEY`. The local hop needs no credential. |
| `JEV_LAYA_BASE_URL` | `http://127.0.0.1:8123` | Local `laya-serve` base URL. Plain HTTP is accepted on `localhost`, `127.0.0.1`, and `::1`; any other host must be HTTPS. |
| `JEV_LAYA_ENDPOINT_PATH` | `/v1/systemone` | The Decisions protocol path `laya-serve` publishes. |
| `JEV_LOCAL_MODEL` | `english` | Which local System One decision model answers. This is the generic name for the slot's engine, and it takes precedence over `JEV_LAYA_MODEL` when both are set. Any local model that speaks the same `/v1/systemone` contract fits; there is no allowlist of names. Rejected only when empty or whitespace, or when it carries a character that would corrupt a JSON string or a URL path segment. See [the local slot](#the-local-slot-is-a-slot-not-a-model). |
| `JEV_LAYA_MODEL` | `english` | The pre-existing name for the same setting, kept for backwards compatibility. Used when `JEV_LOCAL_MODEL` is unset. `english`, `multilingual`, and `typed-decisions` name a `laya-serve` checkpoint directly. |

`laya` on its own is a replacement for the hosted providers, never a member of them. It builds a chain of exactly one provider, no hosted mode ever selects it, and no mode appends it silently. The wire client omits the `Authorization` header entirely when no key is configured, so a server started without `LAYA_API_KEY` accepts the request unchanged; an empty bearer is wrong and is not sent. Laya is an external package you install yourself, authored by Convai Innovations under Apache-2.0 at [NandhaKishorM/laya](https://github.com/NandhaKishorM/laya); no Laya code ships in this repository.

### The four canonical modes

Four names cover every arrangement. Each says which side leads and whether the other side is a fallback, and none of them names a particular hosted provider or a particular local engine.

| Mode | Leads | Fallback | Providers tried, in order |
|---|---|---|---|
| `api_with_local_fallback` | the hosted API | the local slot | the configured hosted provider, then `laya` |
| `api_only` | the hosted API | none | the configured hosted provider |
| `local_only` | the local slot | none | `laya` |
| `local_with_api_fallback` | the local slot | the hosted API | `laya`, then the hosted providers |

`api_only` and `local_only` are single provider routes. There is no fallback, no chain, and no cooldown list beyond the one provider, so a failure is **reported, never rerouted**. The other two are two provider chains and use the existing cooldown, trigger and breaker machinery unchanged.

The hosted side is chosen by configuration rather than by the mode: whichever hosted credential is present selects it, with OpenRouter first, then direct TypeSafe, and Clef when it is the only hosted credential there is. That is why `api_only` resolves to `openrouter`, `typesafe` or `clef` rather than to one fixed provider, and why `api_with_local_fallback` becomes `<that provider>_then_laya`.

The concrete mode names this repository has always accepted are **not** renamed and **not** deprecated. Each still selects the exact order it always named, so a configuration written for 0.7.0 routes identically in 0.8.0:

| Concrete mode | Resolves to | Order |
|---|---|---|
| `openrouter` | `api_only` on OpenRouter | `openrouter` |
| `typesafe` | `api_only` on TypeSafe | `typesafe` |
| `clef` | `api_only` on Clef | `clef` |
| `typesafe_then_openrouter` | two hosted providers | `typesafe`, `openrouter` |
| `openrouter_then_typesafe` | two hosted providers | `openrouter`, `typesafe` |
| `laya` | `local_only` | `laya` |
| `laya_then_typesafe` | `local_with_api_fallback` | `laya`, `typesafe` |
| `laya_then_openrouter` | `local_with_api_fallback` | `laya`, `openrouter` |
| `laya_then_typesafe_openrouter` | `local_with_api_fallback` | `laya`, `typesafe`, `openrouter` |
| `laya_then_openrouter_typesafe` | `local_with_api_fallback` | `laya`, `openrouter`, `typesafe` |
| `typesafe_then_laya` | `api_with_local_fallback` | `typesafe`, `laya` |
| `openrouter_then_laya` | `api_with_local_fallback` | `openrouter`, `laya` |
| `clef_then_laya` | `api_with_local_fallback` on Clef | `clef`, `laya` |

**The `<hosted>_then_laya` modes are new in 0.8.0** and are what `api_with_local_fallback` needs. Before this release a local server could only lead a chain, because a hosted failure was never a licence to call the local slot. It still cannot sit between two hosted hops: `("typesafe", "laya", "openrouter")` is refused, and a chain of `laya` alone is refused because the plain local mode is selected by its own name.

**The privacy consequence is the reverse of `laya_then_*`.** A `laya_then_*` mode keeps the case state home while the local server answers and only sends it out when the local server fails. A `<hosted>_then_laya` mode does the opposite: the review goes to the hosted API first, and the local server is a backstop for when that route is unavailable. Choose which side you would rather depend on.

### Mode aliases

Every name below keeps working, so no deployed configuration breaks. Each resolves to a canonical mode **before** anything routes, so no alias string reaches a chain, a diagnostic, a log line, or a URL.

| Alias | Resolves to | Order | Note |
|---|---|---|---|
| `laya_local` | `local_only` | `laya` | DOGA's name for the plain local mode. |
| `laya_with_jev_fallback` | `local_with_api_fallback` | `laya`, `openrouter`, `typesafe` | DOGA's name. Both hosted providers, OpenRouter first. |
| `clef_api` | `api_only` on Clef | `clef` | Clef alone, deliberately: a Clef failure is not a licence to call a second classifier. |
| `jev_api` | `api_only` | by credential | The hosted side resolves by credential, as it always has. |
| `clef_with_local_fallback` | `api_with_local_fallback` | `clef`, `laya` | Clef as the hosted side with the local slot behind it. |
| `laya_then_hosted` | `local_with_api_fallback` | `laya`, `openrouter`, `typesafe` | Another spelling of the local first chain. |

`resolve_mode` stays case insensitive and still fails closed for an unknown name, with an error that lists every accepted value. `MODE_ALIASES` and `CANONICAL_MODES` are the whole mapping; nothing is inferred from an alias name at request time.

### The local slot is a slot, not a model

`laya` selects the local slot, and the slot takes **Laya or other pre-deterministic routing models**. What answers inside it is chosen by `JEV_LOCAL_MODEL`, so **Laya is interchangeable with any other local System One decision model by configuration alone**: a different local model is one variable, with no new provider name, no new mode, and no code change.

The value is the engine or checkpoint name the request asks the local server for. There is deliberately **no allowlist of model names**, because an engine nobody has heard of must work without a code change. Two things are refused instead:

- an empty or whitespace-only value, which is a mistake worth reporting rather than silently covering over with the default, and
- a value carrying a character that would corrupt the request, because the name is interpolated into a JSON string and may become a URL path segment: a quote, a backslash, a control character, a `/`, a `?`, a `#`, a `%`, or anything else in that class.

A rejection names the setting and the reason. It never echoes the value back, and it happens before any socket is opened.

Local models known to fit the same `/v1/systemone` contract. Only the engine names this repository documents are listed; any other local model your server answers to fits the slot the same way:

| Engine name | Notes |
|---|---|
| `laya` | Convai Innovations; a System One decision model with open weights, run locally. `laya-multilingual` and `laya-typed-decisions` are engine names of the same project. The default server is a `laya-serve`. |
| `english`, `multilingual`, `typed-decisions` | Checkpoint names the `laya` server answers to. `english` is the default this repository sends. |

**The interchangeability claim is sourced, not asserted.** [chaitin/Decis](https://github.com/chaitin/Decis) is a self hosted server that speaks this Jev compatible `/v1/systemone` endpoint and serves more than one engine, one Docker image per engine, where repointing `base_url` is the whole migration. That is what makes Laya a slot rather than a binding.

**The default request is unchanged.** `JEV_LOCAL_MODEL` defaults to `english`, which is the checkpoint `laya-serve` serves by default and the only one this repository has ever called. A default configuration sends byte for byte the request it always sent. `JEV_LAYA_MODEL` still works and is used when `JEV_LOCAL_MODEL` is unset; when both are set, `JEV_LOCAL_MODEL` wins, because it is the name that is not tied to one model.

`local_model` is never a mode alias and never a member of a fallback order. An engine name is not a route: `JEV_PROVIDER_MODE=your-local-engine` is rejected, and `("your-local-engine",)` is rejected as a chain.

### The Cloudflare Clef route

`clef` is a hosted System One decision model served by Cloudflare Workers AI. It sits beside Jev over TypeSafe or OpenRouter, not inside their chain: one provider name, one checkpoint setting, and no fallback.

| | |
|---|---|
| Mode | `JEV_PROVIDER_MODE=clef`, or the DOGA alias `clef_api` |
| Endpoint | `POST https://api.cloudflare.com/client/v4/accounts/<CLOUDFLARE_ACCOUNT_ID>/ai/run/@cf/cloudflare/clef` |
| Body | `{"model": "clef", "state": <state>, "questions": <questions>}` |
| Credential | `CLOUDFLARE_API_TOKEN`, a Cloudflare API token with **Account > Workers AI > Read**, sent as a bearer token in the `Authorization` header |
| Configuration | `CLOUDFLARE_ACCOUNT_ID`, the 32 character account id, and `JEV_CLEF_MODEL`, the checkpoint |
| Checkpoints | `clef` (default) and [`clef-flash`](https://developers.cloudflare.com/workers-ai/models/clef-flash/), both at the `/ai/run/@cf/cloudflare/<model>` path |
| Limits | 64 questions per request, 65536 token context window |
| Source | [Clef](https://developers.cloudflare.com/workers-ai/models/clef/) |

**The checkpoint is a setting, not a provider.** `clef` and `clef-flash` answer the same typed questions, so `JEV_CLEF_MODEL` chooses between them and there is still exactly one provider. `clef-flash` is rejected if you set it as `JEV_PROVIDER_MODE`, because a mode name is a route and a checkpoint is not. An unrecognised `JEV_CLEF_MODEL` fails naming the accepted values rather than calling a checkpoint that does not exist.

**Both credentials are checked before any socket is opened.** A missing `CLOUDFLARE_API_TOKEN` or `CLOUDFLARE_ACCOUNT_ID` is a selection error that names the variable, and both missing names are reported together, so a first run needs one fix rather than two. The account id is validated as 32 lowercase hex characters: it is configuration rather than a secret, but it is interpolated into a URL, so a value carrying `../` or a slash would rewrite which endpoint the request reaches and is refused before anything is sent.

**Clef alone has no fallback, deliberately.** A Clef failure is a failure of this route, not permission to call a second classifier, and nothing appends Clef to a mode that does not name it. There is no `laya_then_clef` mode and no hosted-to-hosted Clef chain. The one opt in exception is `clef_then_laya`, reachable as `clef_with_local_fallback`, where you have asked by name for the local slot to back Clef up; there the fallback is the other side of the same contract rather than a second classifier. In every other case a failed review is an unavailable review, and the host's conservative fallback applies.

**Both response envelopes are accepted.** Cloudflare serves the model output directly from the run endpoint as `{"model", "answers", "usage"}`, and its general REST surface wraps that in `{"success": true, "result": {...}}`. The route prefers a top-level `answers` and falls back to `result.answers`. A `{"success": false}` envelope is refused with Cloudflare's own error codes in the message, because they identify what went wrong where a generic parse failure would not.

**The answer contract is the one this repository already had.** Clef names its three question types `noul`, `choice` and `score`, exactly as the System One API this client already speaks, so answers go through the same `validate_answers` and the same score legend index check as the other routes. A `score` question must send `criteria` as an ordered list of level descriptions, and an answer outside `0..len(criteria)-1`, or a `legend` that contradicts those levels, is rejected. A `noul` probability and a `confidence` outside `0..1` are rejected, and so is a `choice` that is not one of the question's own criteria.

**Question ids are mapped, and mapped back.** Clef accepts letters, digits, `_`, `.` and `-` in a question id, up to 100 characters. This repository builds ids such as `candidate:aaa` and `hook:name`, whose colon Clef refuses, so an id is rewritten before the request (`candidate:aaa` becomes `candidate_aaa`) and the answers are rewritten back afterwards. A caller never sees a renamed question, and an id Clef already accepts is sent untouched. An id too long to send is truncated and gains a short digest of the original, so truncation cannot make two questions collide. A request with more than 64 questions is refused before it is sent; that limit is Clef's and applies only to the route that names it.

**Privacy and log hygiene.** A `clef` review sends the bounded `state` and the questions to Cloudflare, which is egress in the same sense as any other hosted route. The credential is sent only as an `Authorization` header and is never logged, and neither is the reviewed state: a Clef failure logs only the provider name and the exception class, never the exception message, matching the guarantee the other routes make. See `THIRD_PARTY_NOTICES.md` for Clef's licensing and provenance.

### Local first chains: Laya primary with a hosted fallback

The four `laya_then_*` modes are the explicit opt in that the first two arrangements do not cover. The order is the literal reading of the mode name:

| Mode | Providers tried, in order |
|---|---|
| `laya_then_typesafe` | `laya`, then `typesafe` |
| `laya_then_openrouter` | `laya`, then `openrouter` |
| `laya_then_typesafe_openrouter` | `laya`, `typesafe`, then `openrouter` |
| `laya_then_openrouter_typesafe` | `laya`, `openrouter`, then `typesafe` |

Laya always answers first, from the local server. A local attempt that fails falls through to the named hosted provider, and if that fails too, to the next one the mode names. Three rules hold, and each one is a test:

- **Laya leads this chain, and in this chain it may only lead.** A chain that puts `laya` between two hosted hops is rejected, and a chain of `laya` alone is rejected because the plain local mode is selected by its own name. `laya` is the only local provider, and it is never appended to a mode that does not name it. As of 0.8.0 a chain may also *end* in `laya`, which is the `<hosted>_then_laya` mode; that is a different chain, described [above](#the-four-canonical-modes).
- **A named hosted provider needs its key before the local review is sent.** A missing `TYPESAFE_API_KEY` or `OPENROUTER_API_KEY` is a selection error raised before any hop runs, naming the variable, rather than a failure discovered after the case state was already on the local server. That is why the mode can be trusted to fall through: the hop it falls through to can authenticate.
- **The answer says which provider answered.** A `laya_then_*` result carries a `provider_routing` block: `provider` is the hop that answered, `provider_order` is the chain, `fallback_used` is true when the answering hop was a fallback, and `attempts` lists each earlier hop with its error. The hosted modes and the plain local mode return no such block, so their existing response shape is unchanged.

**The privacy consequence, stated plainly.** In a `laya_then_*` mode the case state leaves the machine whenever the local attempt fails: that egress to a hosted API is the point of the mode, not a side effect. The plain `laya` route never leaves the machine, because it has no hosted hop at all. Choose `laya` when no egress is acceptable, and a `laya_then_*` mode when local first with a hosted backstop is worth that exposure. Nothing in a `laya_then_*` mode is sent to a hosted API while the local server answers, and the local hop still sends no credential.

The question shapes and the score scale are the local server's, because the local hop is the one that runs first: the `laya_then_*` modes validate the same ordered `score` criteria and enforce the same legend index scale as `laya`.

### DOGA selector aliases

The DOGA fork names four arrangements `jev_api`, `clef_api`, `laya_local`, and `laya_with_jev_fallback`. **As of 0.8.0 all four are accepted** as aliases of the canonical modes, so a setting written for DOGA works unchanged. `jev_api` was refused before this release; it now resolves to `api_only` with the hosted side chosen by credential, which is what the name always meant. The mapping is a table in `jev_client.MODE_ALIASES`, and it is the whole mapping: nothing is inferred from the alias name at request time. The complete alias table, including the two names the shared contract adds, is [above](#mode-aliases).

| DOGA name | Accepted here? | Resolves to | Providers tried, in order |
|---|---|---|---|
| `jev_api` | Yes, new in 0.8.0 | `api_only` | `openrouter` or `typesafe`, by credential |
| `clef_api` | Yes | `clef` | `clef` |
| `laya_local` | Yes | `laya` | `laya` |
| `laya_with_jev_fallback` | Yes | `laya_then_openrouter_typesafe` | `laya`, `openrouter`, then `typesafe` |

`clef_api` selects Clef **alone, with no fallback at all**. That is the point of the alias: DOGA's Clef route is a hosted route of its own, so a Clef failure is a failure of that route rather than a licence to call a second classifier. The alias needs `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`, and a missing one fails before any request, naming the variable. No mode silently routes to Clef: a mode that does not name Clef never reaches it. The one mode that pairs the two sides is `clef_then_laya`, which is reached only through `clef_with_local_fallback` or by naming it directly, and there the local slot is the fallback rather than a second classifier.

`laya_local` is the plain local mode under DOGA's name: one provider, no hosted hop, and no egress. `laya_with_jev_fallback` is the local first chain that names **both** hosted Jev providers, and the order is OpenRouter first and direct TypeSafe second. That order is not arbitrary: DOGA's own Jev route tries OpenRouter first and falls back to direct TypeSafe, and `openrouter` is this repository's default hosted provider. Naming both providers also means the chain is not weaker than DOGA's fallback, which can reach either one. A `laya_with_jev_fallback` selection therefore needs both `OPENROUTER_API_KEY` and `TYPESAFE_API_KEY`, and a missing one fails at selection naming the variable.

`jev_api` resolves to `api_only`, so the hosted side follows the credential present: `openrouter` when `OPENROUTER_API_KEY` is available, `typesafe` when `TYPESAFE_API_KEY` is, and Clef when it is the only hosted credential there is. To pin the OpenRouter-first order that DOGA's own Jev route uses, set `JEV_PROVIDER_MODE=openrouter_then_typesafe` instead; that is the two-provider chain rather than the single provider `api_only` names.

An alias resolves to its canonical mode before anything routes, so it behaves identically to the mode it names: the same provider order, the same required keys, the same `provider_routing` block, and the same error. Anything outside the accepted set is rejected with an error listing every accepted value; there are thirteen concrete modes, four canonical modes and six aliases, so `JEV_PROVIDER_MODE` accepts twenty-three names.

### The consecutive failure breaker

A local failure may reach a hosted provider only three times in a row. The count lives in `jev_client` as process state, so restarting the process resets it, and nothing is written to disk. Every local failure in a `laya_then_*` mode increments it; the first three are allowed to fall through to the named hosted provider, and the fourth and every one after it re-raises the local error with no hosted request at all. Any local answer that passes validation resets the count to zero, on both the plain local path and the chain path, so a server that recovers is trusted again immediately.

The count tracks **local** failures only, so it bounds remote egress the same way in all four `laya_then_*` modes regardless of which hosted provider a mode names. The limit is hardcoded at three (`jev_client.LOCAL_FALLBACK_FAILURE_LIMIT`), and a suppressed fallback is reported at warning level before the local error is re-raised.

Two honest limits come with it. The plain `laya` mode has no hosted hop, so a local failure there neither increments the count nor is suppressed: there is nothing to suppress. And a breaker bounds repeated remote egress after local **errors**; it cannot detect a valid yet incorrect local judgment. A confidently wrong local answer is returned as a healthy local answer and never reaches the hosted hop, which is why the quality evidence in the release notes matters more than the breaker.

### Question shapes on the local route

`laya-serve` answers the same `answers` mapping a hosted Decisions provider returns, with the same three question types, but the shapes are stricter in two places:

- A `choice` question keeps `criteria` as a dict of option to description, and the answer's `choice` is the winning label string.
- A `score` question must send `criteria` as an ordered list of level descriptions, index 0 first. A dict is refused by the server out loud, so the plugin rejects it before the request. The answer carries a `legend` that maps index to description.
- A `noul` question answers with `noul` as the probability of true.

### The score scale, measured

A local `score` is the expected level on the legend index scale `0..N-1`, not a `0..1` probability. That is the same scale the hosted route uses, which is what the `blast_radius` thresholds in `approval_policy` were written against: measured on 2026-09-26 with the same three level `blast_radius` question (`["trivial", "annoying", "severe"]`) and the same command state, the hosted route returned `score 0.72` with `probabilities {0: 0.42, 1: 0.44, 2: 0.14}` and the local route returned `score 1.2068` with `probabilities {0: 0.1079, 1: 0.5773, 2: 0.3148}`. Both values are the expected index of their own distribution, so `1.6` and `2.0` keep their meaning on the local route for a three level question. They are not portable to a question with a different number of levels, and the local plugin asserts the answer stays inside `0..len(criteria)-1` and that the returned legend matches the levels it asked about.

### Measured limits of the local checkpoint

These come from live runs against `laya-serve` 0.3.20 on CPU, base English checkpoint, on 2026-09-26:

1. **It is slow and it is one forward pass at a time.** A six question review took about 2 seconds once loaded, and CPU inference is roughly 1.6 seconds per question row. Model load takes 25 to 35 seconds. The local route therefore gets a 120 second budget instead of the hosted 30 second default, and one process should serve one request at a time.
2. **Its confidence is low, so the approval policy escalates.** `confidence` is one minus normalized entropy and falls as probability spreads. A live local `verdict` answer returned `confidence 0.1119` against a `0.55` floor in `approval_policy`, and the end to end local approval review returned `ESCALATE` on `self_advocating 0.83 >= 0.6` rather than an approval. That is the policy working as designed on a weakly separated answer, not a bug, but it means a local route is not a drop in quiet replacement for a hosted one.
3. **No quality claim is made for this checkpoint.** On the compaction retention evaluation in the companion plugin, this base checkpoint's zero shot discrimination between keep and drop was near zero, with a 0.0014 gap between the two classes and calibration hitting its 0.40 ceiling. Those numbers were measured there, not here, and they are reported because they bound what a local score is worth. Calibrate on your own labelled examples before trusting a local route for a consequential decision.

## Integrate another AI agent

Call the local gateway before a consequential operation. Branch on its result, apply your own approval rules, execute through your host, and verify using observations from the target.

```python
from gateway import decide, verify

policy = decide({
    "action": "restart_service",
    "external": True,
    "reversible": True,
    "destructive": False,
    "credential": False,
})

# A recommendation is not permission to execute.
assert policy["decision"] == "suggest"

# Your host checks authority and performs the approved action.
# This fixture represents a result that still needs a target readback.
check = verify({
    "changed": True,
    "read_back": False,
    "evidence": False,
})
assert check["verified"] is False
assert check["next"] == "read_back"
```

The Python functions and CLI can sit behind an MCP tool or HTTP service you build. This repository does not ship a universal MCP server or native adapters for every agent framework. The Hermes registration code stays specific to Hermes; your own provider can supply semantic judgment while the local gateway handles explicit policy state.

See [the integration guide](integrations.md) for the interface contract and operational details.

## Local storage

Hermes uses the active profile home. Without Hermes, storage uses `JEV_HOME`, falling back to `~/.jev`.

- `logs/jev-shadow.jsonl`: automatic review metadata and model answers.
- `logs/jev-ledger.jsonl`: reviews, outcomes, commitments, decisions, and case events.
- `logs/jev-closed-loop.jsonl`: linked decision, observation, and outcome records.

Manual entries can contain submitted text and evidence. No automatic retention policy is provided. Protect the directory, review what you store, and keep runtime files out of public repositories.

## Development and verification

From a source checkout:

```bash
python3 -m pip install -e '.[test]'
python3 -m pytest -q
python3 tools/public_scan.py
```

Run tests with isolated runtime storage. Keep live provider tests separate from deterministic CI and use synthetic inputs. A passing test suite should be followed by a clean installation check, not just an import from a working directory containing private files.

The repository separates its public plugin code from deployment specific collectors and private runtime state. [CONTRIBUTING.md](../CONTRIBUTING.md) describes contribution checks; [SECURITY.md](../SECURITY.md) explains the security boundary.
