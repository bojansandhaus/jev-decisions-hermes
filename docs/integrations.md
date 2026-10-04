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

Four hosted and local ways to answer that call, and you pick one: Jev over a TypeSafe or OpenRouter key, Clef at Cloudflare Workers AI, Laya locally with no key, or an opt in `laya_then_*` chain that answers locally first and falls through to the named hosted provider. A local `laya-serve` process publishes the same `POST /v1/systemone` shape, so the payload above is unchanged except that `model` names a Laya checkpoint instead of a Jev model and no `Authorization` header is sent. With plain `laya`, `state` never leaves your machine. In a `laya_then_*` mode, a local attempt that fails sends the case state to the named hosted API: that fallback is the point of the mode, and it stops after three consecutive local failures in one process, after which the local error is raised instead of a hosted request. The DOGA names `laya_local`, `laya_with_jev_fallback` and `clef_api` are accepted as aliases for `laya`, `laya_then_openrouter_typesafe` and `clef`. The result carries a `provider_routing` block naming the provider that answered and whether a fallback happened. See [provider settings and the local route](reference.md#provider-settings-and-the-local-route).

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
- **No fallback.** Clef never falls back to another provider. A Clef failure is an unavailable review; apply your host's conservative fallback rather than assuming an answer exists.

Egress is the point of the route, exactly as it is for any hosted provider: `state` leaves your machine and reaches Cloudflare. Redact it first, on the same terms as the other routes. A failure logs only the provider name and the exception class, never the state and never the credential.

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
