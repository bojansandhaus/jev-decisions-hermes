# Optional smart approvals

Jev Decisions 0.2.0 includes an approval only companion provider and an advisory `approval_review` workflow. Both are opt in. The provider sees only commands already sent to Hermes's approval gate. It is not a sandbox, command detector, or executor.

## Provider selection

Model reviews and smart approval review can use a hosted Jev provider, the hosted Cloudflare Clef provider, a local Laya server, or a local first chain that falls through to a hosted provider. Set `JEV_PROVIDER_MODE` to one of `typesafe`, `openrouter`, `typesafe_then_openrouter`, `openrouter_then_typesafe`, `clef`, `laya`, `laya_then_typesafe`, `laya_then_openrouter`, `laya_then_typesafe_openrouter`, or `laya_then_openrouter_typesafe`. The DOGA names `laya_local`, `laya_with_jev_fallback` and `clef_api` are accepted aliases for `laya`, `laya_then_openrouter_typesafe` and `clef`. The default is `openrouter` for backward compatibility.

`clef` is a hosted route of its own with no fallback. It needs `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`, both checked before the command state is sent, and a missing one fails naming the variable. Its `clef-flash` checkpoint is selected with `JEV_CLEF_MODEL`. Because a Clef failure is not a licence to call a second classifier, the advisory review is an unavailable review and escalates exactly as any other provider failure does.

The four hosted modes run Jev over a hosted API key. Store `TYPESAFE_API_KEY` and `OPENROUTER_API_KEY` through the Hermes secret manager or the profile environment. Never put a key in `config.yaml`, a command argument, a journal, or this repository. The direct TypeSafe endpoint is `https://api.typesafe.ai/v1/systemone` with model `jev-1.13.0`. The OpenRouter endpoint is `https://openrouter.ai/api/alpha/decisions` with model `typesafe/jev-1.13`. A fallback is attempted only after the selected primary route fails.

`laya` is the other arrangement, not a third route in the chain. It runs a local decision server on your own machine with no key at all. It is exactly one provider, it never has a hosted fallback, and it is never appended to a mode that does not name it. `LAYA_API_KEY` is forwarded only when your server was started with its own bearer check; otherwise no `Authorization` header is sent at all. Point it at your server with `JEV_LAYA_BASE_URL` (default `http://127.0.0.1:8123`) and `JEV_LAYA_ENDPOINT_PATH` (default `/v1/systemone`). Plain HTTP is accepted on loopback only.

**The local slot is not bound to Laya.** `JEV_LOCAL_MODEL` chooses which local model answers, defaulting to `english` so the default request is unchanged, and `JEV_LAYA_MODEL` still works and is used when `JEV_LOCAL_MODEL` is unset. Any local model speaking the same `/v1/systemone` contract fits, so a different engine is a configuration change rather than a code change; the approval policy, the question shapes and the score scale are identical whichever engine is behind the slot. See [pointing the local slot at a different engine](integrations.md#pointing-the-local-slot-at-a-different-engine).

The `laya_then_*` modes are the explicit opt in that puts Laya first and names the hosted provider or providers that answer when the local attempt fails. Privacy changes with that choice and must be read before selecting it: in a `laya_then_*` mode a failed local attempt **sends the command state to the named hosted API**, which is the point of the mode. The plain `laya` route never leaves the machine. Each `laya_then_*` mode needs the key of every hosted provider it names and fails at selection, naming the missing variable, so the fallback hop is always able to authenticate. The result reports which provider answered and whether a fallback happened in its `provider_routing` block.

The fallback is bounded by a consecutive failure breaker. The first three local failures in a row in one process may fall through to the named hosted provider; the fourth, and every one after it, re-raises the local error and makes no hosted request at all. Any local answer that passes validation resets the count to zero, and the count is process state, so a restart starts it clean. Because the count tracks local failures only, it bounds egress identically in all four `laya_then_*` modes whichever hosted provider a mode names. It bounds repeated remote egress after local errors; it cannot detect a valid yet incorrect local judgment, which is returned as a healthy local answer. A blocked review is still an unavailable review, so the gate escalates rather than approving.

A provider outage, missing key, timeout, malformed response, invalid number, unknown enum, or incomplete answer is an unavailable review and must escalate. That includes every Cloudflare error code the `clef` route surfaces. It never silently approves. The measured limits of the local checkpoint are in [the reference](reference.md#provider-settings-and-the-local-route); do not read a local score as a hosted score without checking the scale there.

## Advisory workflow

Ask Hermes to run `jev_workflow` with `workflow: approval_review` and bounded state containing the command, its description, and trusted operator policy separately. The result includes the Jev answers, a verdict, and the deterministic rule applied. The workflow is shadow only. It cannot authorize or execute a command, and caller supplied policy is not authenticated operator authorization.

## Native provider

The companion manifest is in `integrations/approval-provider`. It registers an approval only model provider through Hermes provider discovery. It must not be selected for the main model, ordinary auxiliary tasks, or chat. Activation is a separate setup action and installation does not change `approvals.mode`, `auxiliary.approval`, the main model, or `context.engine`.

When you explicitly activate it, use provider `jev-decisions-approval` with an OpenRouter base URL such as `https://openrouter.ai/api/alpha` and model `~typesafe/jev-latest`. This release rejects direct TypeSafe endpoints and unknown hosts.

Keep the existing Hermes approval settings in force. Before selecting the provider, run the registration and routing tests with a mocked transport, then exercise a harmless flagged command in a fresh Hermes process. Test disabled and manual modes as well as an unavailable key. Do not enable it unattended until representative labeled outcomes show acceptable false approvals and unnecessary escalations.

## Privacy and attribution

Commands are bounded and redacted before provider egress where the integration can do so. Local records contain metadata and hashes rather than raw command text. Review payloads still leave the machine for OpenRouter, or for Cloudflare on the `clef` route, so use synthetic cases when testing. With a `laya_then_*` mode the payload goes to the local server first and to the named hosted API only when that local attempt fails; the plain `laya` mode sends it nowhere but loopback.

The companion provider is adapted from [anpicasso/hermes-jev-approvals](https://github.com/anpicasso/hermes-jev-approvals), authored by anpicasso. See `THIRD_PARTY_NOTICES.md` for the applicable MIT notice. The surrounding Jev Decisions plugin remains Copyright (c) 2026 Bojan Sandhaus under MIT.