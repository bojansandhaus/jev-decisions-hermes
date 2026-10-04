# Jev Decisions Plugin for Hermes and other AI Agents: Integration guide

Jev Decisions has one provider specific edge and one provider independent boundary.

The edge asks Jev a bounded question. The boundary decides what your agent may do next.

## Provider call contract

The default Hermes adapter sends this shape to OpenRouter's Decisions API:

```json
{
  "model": "typesafe/jev-1.13",
  "state": {"...": "bounded state"},
  "questions": {
    "question_id": {
      "type": "noul",
      "instructions": "A precise question",
      "criteria": {
        "true": "What true means",
        "false": "What false means"
      }
    }
  }
}
```

Keep `state` small and redacted. Put definitions in `criteria`, not in a free form prompt. Keep the question count bounded. Treat the returned answer as a recommendation.

Four canonical modes answer that call, and you pick one: `api_with_local_fallback`, `api_only`, `local_only`, `local_with_api_fallback`. Each says which side leads and whether the other side is a fallback, and none of them names a particular provider. The hosted side is whatever credential you have configured: Jev over a TypeSafe or OpenRouter key, or Clef at Cloudflare Workers AI. The local side is a `POST /v1/systemone` server, and `laya` is the name of that slot rather than a binding to one model: `model` in the payload names whichever engine `JEV_LOCAL_MODEL` selects.

With plain `laya` or `local_only`, `state` never leaves your machine. In `local_with_api_fallback` (or a `laya_then_*` mode), a local attempt that fails sends the case state to the named hosted API: that fallback is the point of the mode, and it stops after three consecutive local failures in one process, after which the local error is raised instead of a hosted request. In `api_with_local_fallback` (or a `<hosted>_then_laya` mode) the order is reversed: the review goes to the hosted API and the local server answers only when the hosted route fails.

`api_only` and `local_only` never reroute a failure. The DOGA names `laya_local`, `laya_with_jev_fallback`, `clef_api` and `jev_api` are accepted as aliases, as are `clef_with_local_fallback` and `laya_then_hosted`. The result carries a `provider_routing` block naming the provider that answered and whether a fallback happened. See [provider settings and the local route](reference.md#provider-settings-and-the-local-route) and [the four canonical modes](reference.md#the-four-canonical-modes).

## The Clef route

The `clef` provider sends the same `state` and `questions` shape to Cloudflare Workers AI, so nothing about your adapter changes. What is different is the endpoint, the credentials, and one stricter rule on question ids:

```json
{
  "model": "clef",
  "state": {"...": "bounded state"},
  "questions": {
    "candidate:aaa": {
      "type": "noul",
      "instructions": "Is the selected candidate answerable from the supplied state?",
      "criteria": {"true": "It is", "false": "It is not"}
    }
  }
}
```

- **Endpoint.** `POST https://api.cloudflare.com/client/v4/accounts/<CLOUDFLARE_ACCOUNT_ID>/ai/run/@cf/cloudflare/clef`, with the same path under `clef-flash` for that checkpoint.
- **Credentials.** `CLOUDFLARE_ACCOUNT_ID` scopes the endpoint and `CLOUDFLARE_API_TOKEN` authorises the call. The account id is configuration rather than a secret, but it appears in the URL; the token needs **Account > Workers AI > Read** and is sent as a bearer token in the `Authorization` header. Both are checked before the request is sent, and a missing one fails naming the variable.
- **Question ids.** Clef accepts letters, digits, `_`, `.` and `-` only, up to 100 characters, and at most 64 questions per request. The `candidate:aaa` form above has a colon, which Clef refuses, so the plugin rewrites it on the way out and rewrites the answers back on the way in. Your adapter sees its own ids.
- **Answers.** The same `noul`, `choice` and `score` contract as the other routes, including a `score` read on the legend index scale its ordered `criteria` define.
- **No fallback, unless you ask for one.** Clef alone never falls back to another provider: a Clef failure is an unavailable review, so apply your host's conservative fallback rather than assuming an answer exists. The single exception is the opt in `clef_then_laya` chain, reachable as `clef_with_local_fallback`, where you have explicitly chosen the local slot as Clef's backstop. It is not a second classifier answering in Clef's place; it is the other side of the same contract, and you asked for it by name.

Egress is the point of the route, exactly as it is for any hosted provider: `state` leaves your machine and reaches Cloudflare. Redact it first, on the same terms as the other routes. A failure logs only the provider name and the exception class, never the state and never the credential.

## Pointing the local slot at a different engine

`laya` is a **slot**, not a model. Two settings decide what answers inside it, and neither is a code change:

| Setting | Default | What it decides |
|---|---|---|
| `JEV_LAYA_BASE_URL` | `http://127.0.0.1:8123` | Which server answers. Its own setting, unchanged by this release. |
| `JEV_LOCAL_MODEL` | `english` | Which engine that server is asked for, sent as the `model` field. |

`JEV_LAYA_MODEL` is the name `JEV_LOCAL_MODEL` had before the slot became generic. It still works, and `JEV_LOCAL_MODEL` wins when both are set. The default is unchanged, so a default configuration still asks for `english`.

To swap the engine, change one variable:

```bash
JEV_PROVIDER_MODE=local_only
JEV_LAYA_BASE_URL=http://127.0.0.1:8123
JEV_LOCAL_MODEL=your-local-engine
```

The slot takes **Laya or other pre-deterministic routing models**. `laya` and its `multilingual` and `typed-decisions` engine names are the ones documented here, and any other engine your server answers to fits it the same way. There is deliberately **no allowlist**: an engine nobody has heard of works the same way, because the whole point is that adding a model is a configuration change rather than a code change.

### A worked example: `chaitin/Decis`

[chaitin/Decis](https://github.com/chaitin/Decis) is a self hosted decision server that publishes this same Jev compatible `/v1/systemone` endpoint and serves more than one engine, with one Docker image per engine. Repointing at a different engine there is a `base_url` change and nothing else, which is the practical demonstration that the slot is interchangeable:

```bash
# Run the server with the engine you want, then:
JEV_PROVIDER_MODE=local_only
JEV_LAYA_BASE_URL=http://127.0.0.1:8080
JEV_LOCAL_MODEL=<the engine name that Decis is serving>
```

Three rules hold for any engine in the slot:

- **The contract is the `/v1/systemone` shape, not the model name.** Your adapter sends the payload above unchanged; only `model` differs, and no `Authorization` header is sent unless your server was started with its own bearer check.
- **The score scale is the local one.** A `score` answer is a legend index into the ordered `criteria`, validated on the same scale as the hosted routes.
- **A value that would corrupt the request is refused before any socket opens.** An empty or whitespace-only `JEV_LOCAL_MODEL`, or one carrying a quote, a backslash, a control character, a `/`, a `?`, a `#` or a `%`, raises naming the setting and the reason without echoing the value back.

## Adapter contract for another agent

A framework adapter only needs to do five things:

1. Build bounded state from its own context.
2. Ask a typed Jev question, or use its own provider for semantic judgment.
3. Call `gateway.decide(state)` before an external action.
4. Execute the action only under the host framework's authority rules.
5. Read back the exact target, derive proof fields from that observation, then call `gateway.verify(result_state)`.

A minimal adapter can use the deterministic boundary without making a network request:

```python
from gateway import decide, verify

state = {
    "action": "update_document",
    "external": True,
    "reversible": True,
}

policy = decide(state)
if policy["decision"] == "human":
    raise RuntimeError("Confirmation required")

# The host agent performs its own approved operation here.
result = {"changed": True, "read_back": True, "evidence": True}
assert verify(result)["verified"] is True
```

The repository does not pretend to know the permission model of every agent framework. Keep the adapter thin and let the host own credentials, tool execution, retries, and user confirmation.

## Shell and service integration

The JSON gateway is useful when an agent runs commands or communicates through a service boundary:

```bash
printf '%s\n' '{"action":"delete_backup","external":true}' \
  | jev-gateway decide

printf '%s\n' '{"changed":true,"read_back":true,"evidence":true}' \
  | jev-gateway verify
```

The current JSON output can be wrapped by a shell wrapper, an MCP tool, an HTTP service, or a workflow engine. Pin the project version in deployments and validate the fields you depend on.

## Redaction pattern

Before sending state to Jev, retain the facts needed for the judgment and remove the payload that does not affect it:

```python
safe_state = {
    "action": "send_email",
    "recipient_class": "external",
    "has_attachment": True,
    "creates_commitment": True,
    "body_length": 842,
}
```

Do not send the email body, access tokens, private correspondence, full memory records, or unrelated personal data unless the judgment genuinely requires them. If the content itself must be reviewed, pass the smallest excerpt that answers the typed question and apply the host's privacy controls first.

## Failure handling

A provider timeout, malformed answer, or missing key is not permission to act. Treat provider failure as an unavailable judgment and apply the host's conservative fallback:

- ask a human for consequential actions,
- wait for evidence when verification is incomplete,
- keep read only work separate from state changing work,
- record the failure without storing sensitive payloads.

## Hermes mapping

Hermes maps the integration contract onto its tool and hook system:

- `jev_decide` sends custom typed questions.
- `jev_workflow` selects a bounded workflow definition.
- `jev_gateway` applies local policy and verification.
- `jev_ingest` opens a classified case from an event.
- `jev_ledger` records reviews and manually supplied commitments and decisions.
- `jev_loop` links decisions, observations, and outcomes.
- The three hooks observe tool calls and model output in shadow mode.

The same boundary can sit behind another agent without importing Hermes internals. Only the plugin registration layer is Hermes specific.

## Hook privacy and cost control

Observer hooks are opt in. The three hooks are registered for discovery but perform reviews only when `JEV_ENABLE_HOOKS` is set to `1`, `true`, `yes`, or `on`. The default is disabled, so installation does not create provider calls or hook records.

When enabled, hooks remain advisory and do not block, authorize, execute, or read targets back. Tool hook journal records store hashes, lengths, workflow metadata, and verification state instead of submitted arguments or result text. Manual `jev_loop` journal entries intentionally persist the text supplied to that tool, so use that tool only for information suitable for local durable storage.

The host integration remains responsible for explicit `changed`, `read_back`, and `evidence` boolean proof fields. Missing or non-boolean fields never count as verification.
