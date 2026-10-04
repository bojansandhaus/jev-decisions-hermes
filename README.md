<p align="center">
  <strong>README</strong> ·
  <a href="docs/reference.md"><strong>Technical reference</strong></a> ·
  <a href="docs/integrations.md">Integration guide</a> ·
  <a href="CONTRIBUTING.md">Contributing</a> ·
  <a href="LICENSE">MIT license</a>
</p>

<div align="center">
  <h1>Jev Decisions Plugin for Hermes (and other AI Agents)</h1>
  <p><strong>Help Hermes check its plans and its work.</strong></p>
  <p>Review a risky change, catch an unsupported claim, or check what still needs doing.</p>
  <p>
    <a href="https://github.com/bojansandhaus/jev-decisions/actions/workflows/ci.yml"><img src="https://github.com/bojansandhaus/jev-decisions/actions/workflows/ci.yml/badge.svg" alt="Jev Decisions CI status"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-yellow.svg" alt="MIT License"></a>
    <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.10%2B-3776AB.svg" alt="Python 3.10 or newer"></a>
  </p>
  <p>
    <a href="#install-in-hermes">Install</a> ·
    <a href="#try-it-in-a-conversation">Try it</a> ·
    <a href="#what-is-jev">About the model</a> ·
    <a href="#frequently-asked-questions">FAQ</a> ·
    <a href="docs/reference.md">Technical reference</a>
  </p>
</div>

**Jev Decisions Plugin for Hermes (and other AI Agents)** gives Hermes extra tools for reviewing a plan, checking an answer against its sources, and assessing whether a task is really finished. You can ask for a review in a normal conversation. Your usual model continues to do the work.

Suppose Hermes updates a document and tells you it is done. Did it open the saved document and check the change, or just receive a successful response from the editing tool? This plugin helps make that distinction explicit. Hermes still needs to gather the evidence.

It is useful when you trust Hermes to work independently but want closer checks around file changes, messages, research, and other tasks where a confident mistake would matter.

> [!IMPORTANT]
> Reviews are advice. Installing this plugin does not automatically stop dangerous commands or replace your approval settings. Automatic reviews are off by default; begin by asking for specific checks.

## What you can ask it to check

<table>
<tr><th>When you are using Hermes to...</th><th>Ask Jev to review...</th></tr>
<tr><td>Change a file or restart a service</td><td>Whether the plan includes permission, a way to recover, and a check afterward.</td></tr>
<tr><td>Research a question or write a report</td><td>Whether the supplied sources support the claims and whether anything important is missing.</td></tr>
<tr><td>Draft a message</td><td>Whether it is ready to send, contains sensitive information, or makes an unintended commitment.</td></tr>
<tr><td>Save something to memory</td><td>Whether the proposed memory is useful later, sensitive, or in conflict with an existing fact.</td></tr>
<tr><td>Compare purchases or choose a next step</td><td>Whether the choice fits your requirements and what information is still missing.</td></tr>
<tr><td>Follow up on a decision</td><td>What happened afterward, using a local record of observations and outcomes.</td></tr>
</table>

The plugin includes 25 prepared reviews. You do not need to learn their formats to try them: ask Hermes to use the appropriate Jev review and explain the result. See the [full review catalog](docs/reference.md#prepared-review-catalog) when you want a particular check.

## Put it to work on a real task

A useful review changes what happens next. Ask Hermes to bring back the issue, the evidence, and a concrete correction, rather than merely announce that another model agreed.

### Before a change: find the missing step

> Before changing these files, use Jev's `plan_review` on your proposed steps. Include what I authorized, how you will preserve the originals, and how you will check the saved result. Show me any gaps before you proceed.

Hermes prepares the plan and submits the relevant details. Jev assesses it. If recovery or verification is missing, Hermes can revise the plan before any file is touched. Your original permission limits still apply.

### Before a report: separate facts from assumptions

> Use `evidence_review` on this draft and the sources you actually read. Then tell me which important claims still need support. Do not invent a citation to fill a gap.

Supply the source excerpts as well as the draft. A bibliography alone does not let a reviewer compare a claim with what the source says. This is particularly useful when a polished answer mixes observed facts with plausible guesses.

### After a change: check the result, not the promise

> Read the exact record you changed. Compare it with my request, then use `action_verify` to review the result. Tell me what matched, what did not, and what you could not check.

The order matters: perform the authorized action, collect evidence, then review. Asking a model to judge a success message without reading the target adds another opinion, not proof.

### Later: find out whether the decision helped

> Use `jev_loop` to record this decision and what would count as success. When I bring back the result, attach it to the same decision. Leave the outcome unknown until we have evidence.

That gives a recurring choice a history. You can inspect whether a maintenance recommendation fixed the symptom or whether a purchasing decision met the original requirements. Keep sensitive details out of the record unless you intend to store them locally.

## Choose how much checking you want

<table>
<tr><th>Start here</th><th>What you get</th><th>What to expect</th></tr>
<tr><td><strong>One requested review</strong></td><td>A focused check discussed in your conversation.</td><td>Ask Hermes to use the named review. Model reviews require a hosted provider key or a local Laya server.</td></tr>
<tr><td><strong>Local checks only</strong></td><td>Fixed rules for approval and supplied verification evidence.</td><td>No model request. Hermes must supply accurate facts; these rules do not interpret the whole task.</td></tr>
<tr><td><strong>Automatic observation</strong></td><td>Review records around tool activity and answers.</td><td>Opt in explicitly. Additional requests, records, and delay are possible; commands are not blocked.</td></tr>
</table>

For most people, one explicit review before an important change is the best starting point. Leave routine, harmless questions alone. Add more checks only when they answer a question you actually care about.

## What is Jev?

[Jev 1.13](https://openrouter.ai/typesafe/jev-1.13) is a **System One decision model**, also written a typed decision model, made by **TypeSafe**. It reads the information supplied to it and answers focused questions: how likely something is to be true, which option fits, or how something scores against a set of criteria. It returns those answers with numbers that express uncertainty. It does not write chat replies or explanations.

System One is the name of the category rather than of a single product. [TypeSafe coined it on 15 September 2026](https://systemonemodels.org/guides/what-is-a-system-one-model/) alongside Jev: a model that returns typed values, Choice, Score or Noul, each with a probability, instead of prose. Members this plugin can reach or point at:

- **Jev**, TypeSafe, hosted, closed weights. The default route.
- **Clef** and **Clef Flash**, Cloudflare Workers AI, hosted. The `clef` route.
- **Laya**, Convai Innovations, open weights, local. The `laya` slot.
- **Kev**, open weights, from 0.8B to 27B on Qwen3.5 and Qwen3.8 bases, serving the same `/v1/systemone` request shape as TypeSafe's API. Selectable as `JEV_LOCAL_MODEL=kev`.
- **Tev1**, Together AI, Qwen3.5-based, open weights. Also selectable in the local slot.

Membership of the category and the shared wire contract are documented claims from those projects, not measurements made by this repository. Nothing here has been called live.

This plugin connects those reviews to Hermes through a hosted provider, or through a local Laya server you run yourself. Hermes can interpret the returned answers for you; it should not present its explanation as reasoning supplied by Jev. You keep the main model you already use.

The plugin also includes simple local rules for checking whether an action needs approval and whether Hermes has supplied evidence of a completed change. Those checks work without a model request.

Read more from the sources:

- **[Jev 1.13 on OpenRouter](https://openrouter.ai/typesafe/jev-1.13):** model details, current pricing, and provider information.
- **[Cloudflare Clef](https://developers.cloudflare.com/workers-ai/models/clef/):** the hosted decision model, its request shape, and its checkpoints, including [clef-flash](https://developers.cloudflare.com/workers-ai/models/clef-flash/).
- **[TypeSafe introduces Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev):** the creator's explanation of the model and why it was built.
- **[TypeSafe's guide to System One models](https://docs.typesafe.ai/concepts/system-one.md):** how these focused decisions differ from a chat model's replies, including what confidence can and cannot tell you.
- **[What is a System One model?](https://systemonemodels.org/guides/what-is-a-system-one-model/):** the category definition, coined by TypeSafe alongside Jev on 15 September 2026.
- **[The System One model index](https://systemonemodels.org/):** the members of the category, hosted and local, with prices, limits and sources.

Other members catalogued in the same index: **CLM** and **GLiNER2.5-Decide** (open weights), plus hosted **d1** (Liquid AI), **Mercury Decide** (Inception, free on OpenRouter), **Solar Decide** (Upstage), **pplx-decider** (Perplexity), **Span-01** (Respan), **Decider 1** (meraGPT), and the **OpenAI Decisions API**.

This is an independent, community maintained plugin by Bojan Sandhaus. Jev is TypeSafe's model; OpenRouter provides the API used here. This repository is not an official release from either company.

## Decision history and local reports

You can keep a decision, attach later observations, label the outcome, and reopen it when new evidence changes the picture. Ask Hermes to use `jev_loop` and keep the decision ID so follow-up entries stay connected. Unknown outcomes remain unknown; the journal does not quietly count them as failures.

For a direct state comparison, ask `jev_loop` to use `verify_observation` with the expected value and an actual readback. An available device is not necessarily in the state you requested. The comparison checks the values supplied to it; Hermes must still read the correct target.

Supporting Python commands provide case lists, commitment candidates, and review summaries. They help inspect the records behind a conversation. They are not a separate graphical dashboard, and private infrastructure collectors are not included in the public package. See [tool actions](docs/reference.md#tool-actions-and-records) and [reports](docs/reference.md#reports-and-supporting-commands) for the exact interfaces.

## Install in Hermes

**Platform note:** this release uses POSIX journal locking. Linux is tested in CI. On Windows, use a Linux environment such as WSL; native Windows support is not verified and the locking module is unavailable there.

You need Git, Python 3.10 or newer, and a Hermes installation with plugin support. In a terminal, run:

```bash
git clone https://github.com/bojansandhaus/jev-decisions.git \
  ~/.hermes/plugins/jev-decisions

hermes plugins enable jev-decisions
hermes tools enable jev
hermes plugins doctor jev-decisions
```

These commands target the default Hermes profile and enable the tools for the CLI. If you use a named profile or a messaging platform, use that profile's plugin directory and tool settings. If the plugin is already installed, follow [the update instructions](#update-or-disable) instead of cloning over it.

The doctor should report successful discovery and registration of eight tools and three hooks. Hooks are the optional automatic checks; registering them does not switch them on.

**For Jev model reviews, choose a provider with `JEV_PROVIDER_MODE`.** Four canonical names cover every arrangement, and each says which side leads and whether the other side is a fallback:

| Mode | Leads | Fallback |
|---|---|---|
| `api_with_local_fallback` | the hosted API | the local slot |
| `api_only` | the hosted API | none |
| `local_only` | the local slot | none |
| `local_with_api_fallback` | the local slot | the hosted API |

`api_only` and `local_only` never reroute a failure. The concrete names this repository has always accepted still work and still select exactly the order they always named: `typesafe`, `openrouter`, `clef`, `typesafe_then_openrouter`, `openrouter_then_typesafe`, `laya`, the four `laya_then_*` local first chains, and, new in 0.8.0, the hosted first chains `typesafe_then_laya`, `openrouter_then_laya` and `clef_then_laya`. The DOGA names `laya_local`, `laya_with_jev_fallback`, `clef_api` and `jev_api` are accepted aliases, as are `clef_with_local_fallback` and `laya_then_hosted`. The default remains `openrouter`. It runs Jev over a TypeSafe or OpenRouter key, runs Clef at Cloudflare Workers AI, or runs Laya locally with no key. The plugin reads `TYPESAFE_API_KEY` for direct TypeSafe access, `OPENROUTER_API_KEY` for OpenRouter access, and `CLOUDFLARE_API_TOKEN` for Clef from the active Hermes secret scope. Do not paste a key into chat or save it in this repository. The direct route uses `https://api.typesafe.ai/v1/systemone` and model `jev-1.13.0`; the OpenRouter route uses `https://openrouter.ai/api/alpha/decisions` and model `typesafe/jev-1.13`. A `laya_then_*` mode needs the key of every hosted provider it names and fails at selection, naming the missing variable, when one is absent.

**Cloudflare Clef is a third hosted route, with its own credentials and no fallback.** Set `JEV_PROVIDER_MODE=clef` and the review runs at `https://api.cloudflare.com/client/v4/accounts/<CLOUDFLARE_ACCOUNT_ID>/ai/run/@cf/cloudflare/clef`. It needs two variables: `CLOUDFLARE_ACCOUNT_ID`, the 32 character account id, which is configuration and appears in the request URL, and `CLOUDFLARE_API_TOKEN`, the credential, which needs the Workers AI read permission and is sent as a bearer. Both are checked before any request, and the error names whichever one is missing. Choose the checkpoint with `JEV_CLEF_MODEL`: `clef` (default) or `clef-flash`, which is a checkpoint of the same provider rather than a second one. Clef asks the same `noul`, `choice` and `score` questions as the other routes and its answers go through the same validation, so a Clef score is read on the same legend index scale. Clef **alone** has no fallback: if it fails, the review fails and is never quietly answered by another classifier. (The one exception is the opt in `clef_then_laya` chain, where you have explicitly asked for the local slot to back Clef up; a mode that does not name Clef never reaches it.) See [the Clef route](docs/reference.md#the-cloudflare-clef-route).

**The local option is a slot, and Laya is just one model that fits it.** `JEV_PROVIDER_MODE=laya` answers from a local decision server on your own machine and needs no key at all. On its own it is exactly one provider, no hosted mode ever selects it, and it has no fallback. Point it at your server with `JEV_LAYA_BASE_URL` (default `http://127.0.0.1:8123`) and choose which local model answers with **`JEV_LOCAL_MODEL`**, which defaults to `english` so the default path is unchanged.

**You are not locked into Laya.** Any local model that speaks the same `/v1/systemone` contract fits the slot, selected by configuration alone: no new provider name, no code change. Known fits are `laya` (and its `multilingual` and `typed-decisions` engines), `kev`, `tev1` ([Together AI](https://github.com/togethercomputer/tev1)), `jeff-qwen3.5-0.8b` and `jeff-gemma4-e2b`. The claim is sourced: [chaitin/Decis](https://github.com/chaitin/Decis) is a self hosted server serving several of these engines behind this same endpoint, one image per engine, where repointing `base_url` is the whole migration. There is no allowlist of model names, so an engine you run yourself works the same way; only an empty name or one carrying a character that would corrupt the request is refused. The pre-existing `JEV_LAYA_MODEL` setting still works and is used when `JEV_LOCAL_MODEL` is unset.

`LAYA_API_KEY` is sent only if your server was started with its own bearer check; otherwise the `Authorization` header is omitted entirely. See [the measured limits of the base checkpoint](docs/reference.md#measured-limits-of-the-local-checkpoint) before you rely on it.

**Both directions of fallback exist.** The `laya_then_*` modes are the opt in chain where a failed local attempt reaches a hosted API; the `<hosted>_then_laya` modes added in 0.8.0 are the mirror, where a failed hosted attempt reaches the local server instead. Choose which side you would rather depend on: `local_only` keeps everything home and reports a failure, `laya_then_*` keeps everything home until the local server fails, `api_with_local_fallback` goes to the hosted API and falls back locally, and `api_only` goes to the hosted API and reports a failure.

**The `laya_then_*` modes are the opt in chain, and they are the one place a failed local attempt reaches a hosted API.** Laya answers first from the local server; if that hop fails, the named hosted provider or providers answer in the order the mode gives. So `laya_then_typesafe` locally reviews first and sends the case state to TypeSafe only when the local server does not answer. That egress is the point of the mode, and it is why `laya` and `laya_then_*` are separate choices: the plain local route never leaves the machine, while these modes do fall back to a hosted API. Each answer reports which provider answered and whether a fallback happened, under `provider_routing` in the result. Because a chain can only fall through to a provider that can authenticate, a missing `TYPESAFE_API_KEY` or `OPENROUTER_API_KEY` stops the mode at selection rather than after the local review has already been sent.

**Repeated local failures stop the fallback.** After three consecutive local failures in one process, the fourth re-raises the local error instead of sending the case state to a hosted API, and any local answer that passes validation resets the count. The count is per process, so a restart starts it at zero, and it tracks local failures only, so the same limit bounds egress whichever hosted provider your `laya_then_*` mode names. It bounds repeated remote egress after local errors; it cannot detect a local answer that is valid and wrong. See [the breaker](docs/reference.md#the-consecutive-failure-breaker).

Start a fresh Hermes process after installation. For the desktop app or a gateway, restart the backend that runs your sessions. Opening another conversation in an unchanged backend may not load new plugin code.

## Try it in a conversation

Start with a harmless example. Paste this into Hermes:

> Use Jev's `plan_review` to review this plan: edit a test document, save it, and tell me the job is done. Identify anything missing from the plan. Only review it; do not edit any files.

Hermes should call `jev_workflow` and explain the returned assessment. Jev may flag missing recovery or verification steps, but its answer is a judgment, not a guaranteed diagnosis.

For a check that needs **no OpenRouter key**, try:

> Use `jev_gateway` to check whether deleting a backup needs human approval. Set the action to `delete_backup`, external to true, reversible to false, destructive to true, and credential to false. Do not delete anything.

The local rule should return `human`, meaning approval is required. Nothing is deleted by this check.

For everyday work, make the review part of your request:

> Draft the email, then use Jev's `communication_review` to check the recipient, sensitive details, and commitments before showing it to me. Do not send it.

> Before calling the document update complete, read the saved document and use Jev's `action_verify` to review whether the result matches what I asked for.

Hermes chooses and calls the tools. If it answers without using them, ask it to make the Jev tool call explicitly. A missing key or failed request means the review did not happen.

## Optional smart approvals

Version 0.2.1 adds selectable TypeSafe and OpenRouter routing to model reviews. Use a single provider or make either provider the primary route with the other as fallback. The companion approval only provider remains OpenRouter only in this release. It refuses ordinary chat, never executes commands, and escalates on missing or malformed evidence. Hermes's existing approval policy remains authoritative.

Installation does not select the provider, change `approvals.mode`, or enable native smart approvals. Configure it separately only after reviewing the [approval guide](docs/approvals.md). LCM, context handling, ordinary model routing, and existing tools are unchanged.

## Optional automatic reviews

Once explicit checks are useful, you can enable reviews before tool calls, after tool results, and after model responses. Start a CLI session with:

```bash
JEV_ENABLE_HOOKS=1 hermes
```

For a desktop backend or service, set `JEV_ENABLE_HOOKS=1` in that process's environment and restart it. The provider key, or a reachable local Laya server, must also be available to that process for model reviews. On the `clef` route the account id must be in the environment of that same process, because it is configuration rather than a secret.

Automatic reviews create local records. They do **not** insert a warning into every conversation, rewrite answers, or stop commands. Their records are useful for inspecting agent behavior; ask for an explicit review when you want a result discussed in the chat.

Enabling them can send excerpts of requests, answers, and tool activity to OpenRouter, add delay, and incur charges. Start with non-sensitive tasks. To switch them off, unset the variable or set it to `0`, then restart Hermes.

## Local supervision

Version 0.3.0 adds a supervision layer that runs locally and answers from local state. It makes no provider call of its own. Ask Hermes to use `jev_supervision` to inspect it, or enable the hooks above to run it automatically.

It does four things:

- Labels each turn `OFF`, `WATCH`, or `ON` with a local classifier. This costs nothing and never delays Hermes.
- Routes structured events through a local relevance score. Routine and repeated equivalent state is suppressed before any provider call is made.
- Holds a high confidence disagreement as a challenge only while the state it disagrees with is still current. A stale challenge is recorded as telemetry and never delivered as advice.
- Fingerprints each failed tool action, and after a configurable number of identical failures raises a local `REPLAN` control. The first equivalent failure can be assessed remotely; later identical ones are not, so a failing loop stops generating provider requests.

Supervision is advisory by default. In `shadow` mode it records and reports a control but changes nothing. Only `correct_next` and `precommit` enforce it, and enforcement can do exactly one thing: block the precise repeated action that created the control, until a materially different action follows or one retry is permitted.

Enforcement never authorizes anything. Approval and verification remain in the deterministic gateway, and your existing approval settings stay authoritative.

```bash
# Observe only. This is the default and it changes no execution.
export JEV_SUPERVISION_MODE=shadow

# Enforce the local loop breaker for identical repeated failures.
export JEV_SUPERVISION_MODE=correct_next
```

Supervision state is turn scoped and needs no provider key. It stays inert until `JEV_ENABLE_HOOKS=1`, matching the automatic review posture above. The `jev_supervision` tool works without that variable because calling it is an explicit request.

## Learned corrections

Version 0.4.0 adds a store of written rules that carry a track record. A lesson is a rule plus a precise description of the mistake as it is about to happen, and the second half is what makes it catch anything.

Tell Hermes about a mistake once and it is recorded. If the same mistake is recorded again, the existing lesson is sharpened instead of duplicated, and that repeat counts as an escape. After two escapes the lesson stops being advice and becomes a `kick`. A lesson that keeps being judged relevant without ever catching anything is noise, and it retires on its own.

An owner rule is different. Add one with `source=owner` and it is a hard stop from the moment you make it, and it can never retire.

The local check compares the action with the lesson over canonical words, so different ways of naming the same action meet: `remove`, `delete`, `wipe`, and `clear` collapse onto one word, as do `tmp` and `temporary`, `dir` and `folder`, and `-rf` and `recursive`. That means `clear out the temporary decision folder with a recursive delete` now matches a lesson written as `rm -rf /tmp/jevs-decision-store`. What it still cannot catch is an unbounded rephrasing that shares no word with the lesson, which is why the semantic judgement stays with Jev and an explicit review exists. In the default `shadow` mode a kick lesson stops nothing at all. The match is written to the ledger as `lesson_would_kick`, so you can see what enforcement would have done before you turn it on.

Proven lessons travel between installs as a pack: export the ones that actually caught something, import them elsewhere, and each imported lesson starts with no track record of its own.

The lifecycle is adapted from the design of psygns's osENV.io. See `THIRD_PARTY_NOTICES.md` for the reviewed revision, the verbatim quotations, and what differs.

## Privacy and limits

**Reviews see what Hermes supplies.** Jev cannot check a document it has not been shown or confirm a delivery without evidence from the sending system. A high confidence score can still accompany a wrong answer.

**Model reviews leave your machine, unless you run them locally.** A hosted review sends the relevant text to OpenRouter and its model provider, or to Cloudflare Workers AI on the `clef` route. The local rule checks do not make those requests. `JEV_PROVIDER_MODE=laya` instead sends the text to a `laya-serve` process on your own machine, which needs no key and makes no outbound request. A `laya_then_*` mode sends the text to that local server first, and sends the case state to the named hosted API when the local attempt fails. Redaction reduces some exposure but cannot guarantee that private information has been removed, and a local server removes the egress rather than the risk of storing what you send it.

**Records stay on disk until you manage them.** Automatic tool records omit raw arguments and results in favor of hashes, lengths, and review information. Manual journal entries save the text and evidence supplied to them. Protect the records and decide how long to retain them; disabling the plugin does not delete them.

**A failed review grants no permission.** If a provider request fails, Hermes must still follow its existing approval rules. Automatic reviews do not lock execution while a provider is unavailable.

The [security guide](SECURITY.md) and [integration guide](docs/integrations.md) explain these boundaries. For this early release, test unfamiliar workflows with harmless examples before using them around consequential work. CI checks the code and installation; it does not certify every model judgment or integration.

## Read the result without overreading it

A completed review should help you decide what to inspect or change. Ask Hermes for a short account:

- **What was reviewed?** The actual plan, draft, or observed result supplied to Jev.
- **What needs attention?** A missing permission, unsupported claim, incomplete result, or unresolved uncertainty.
- **What happens next?** A specific check or revision, within the authority you already granted.

Jev returns answers and probabilities, not an explanation of its reasoning. Hermes can relate those answers to the supplied material, but that explanation is Hermes's interpretation. Neither model should turn a probability into a statement that it has directly observed the world.

A review that finds no issue is still bounded by its input. If the source was stale, the target was wrong, or crucial details were omitted, agreement tells you little. Fix the input rather than repeating the same question until you get a reassuring answer.

## Frequently asked questions

### What should I try first if I only have a few minutes?

Run the harmless local backup check in [Try it in a conversation](#try-it-in-a-conversation). It confirms that Hermes can call the plugin without a provider key. Then review a short synthetic plan with `plan_review` to test the hosted connection, or your local Laya server if you pinned `JEV_PROVIDER_MODE=laya` or a `laya_then_*` mode. Neither example needs permission to change your files.

### Should I ask for a review on every task?

Usually not. Start with tasks where an error would cost time, money, privacy, or trust. Check an outgoing commitment or a consequential file change before adding reviews to routine questions. Automatic observation is optional and can be noisy; more records do not necessarily mean better decisions.

### What if Jev and my main model disagree?

Ask Hermes to identify the disputed question and show the relevant evidence. Recheck the source or target when possible. If the dispute concerns your intent or permission, ask you. Neither model wins simply because it is more confident.

### How can I report a problem without exposing my data?

Reduce it to a synthetic example that still reproduces the problem. Include the plugin revision, Hermes version, tool or workflow name, expected result, and the sanitized error. Never attach your API key, complete conversation archive, or raw private journals. Follow [CONTRIBUTING.md](CONTRIBUTING.md); use the guidance in [SECURITY.md](SECURITY.md) for security concerns.

### Do I need to replace the model I use with Hermes?

No. Your usual model continues the conversation and performs the task. The plugin calls Jev separately when you request a model review or enable automatic reviews.

### Is there a subscription or extra charge?

The plugin is MIT licensed. Jev requests through OpenRouter may incur usage charges under your account, and `clef` route requests are billed to your Cloudflare account under Workers AI pricing. See [current model pricing](https://openrouter.ai/typesafe/jev-1.13). A local Laya route costs no provider request at all, and local rule checks and local records need no paid provider request either. A `laya_then_*` mode costs nothing while the local server answers, and charges the hosted provider only when it falls through. See [the local option](#can-i-run-it-locally-with-laya-instead-of-a-hosted-provider).

### Why use this instead of asking Hermes to double-check itself?

You can already ask Hermes to review its own work. This plugin adds prepared review questions, a separate decision model, fixed local checks, and a record you can revisit. That makes the checks more explicit and repeatable. It does not prove that two models will catch every error or that Jev will outperform your main model on every task.

### Will it prevent unsafe actions automatically?

No. Keep Hermes's existing approval settings. The plugin recommends when to ask and what to verify. Making a recommendation block an action requires additional integration; installing the plugin alone does not enforce that rule.

### Can I use it without sending anything to OpenRouter?

Yes, in two different ways. For the local approval and verification rules and local records, leave automatic reviews off and avoid the model review tools. For the model reviews themselves, run Laya locally instead of a hosted provider, which sends nothing off your machine and needs no key; see [Can I run it locally with Laya instead of a hosted provider?](#can-i-run-it-locally-with-laya-instead-of-a-hosted-provider). Reviews that read and judge the meaning of a plan, message, or answer require either a hosted provider, which is Jev over TypeSafe or OpenRouter or Clef at Cloudflare, or that local server; there is no fourth option. The `laya_then_*` modes are local first, but they do send the case state to the named hosted API whenever the local server fails, so choose plain `laya` if no egress at all is the requirement.

### Can I run it locally with Laya instead of a hosted provider?

Yes. Three arrangements exist and you pick one: a hosted provider, which is Jev over a TypeSafe or OpenRouter key or Clef at Cloudflare, Laya locally with no key, or a local first chain that falls through to a hosted provider. Laya is a typed decision model you run yourself, and the `laya-serve` server it ships publishes the same `POST /v1/systemone` contract, so the plugin scores through a process on your own machine with no key and no outbound request:

```bash
python3 -m pip install laya
laya-serve --help          # LAYA_HOST, LAYA_PORT, LAYA_DEVICE, LAYA_THREADS, LAYA_MODEL, LAYA_API_KEY
```

Then set `JEV_PROVIDER_MODE=laya` in the environment of the Hermes process that runs your sessions. `JEV_LAYA_BASE_URL` defaults to `http://127.0.0.1:8123` and `JEV_LAYA_ENDPOINT_PATH` to `/v1/systemone`. Set `JEV_LOCAL_MODEL` to whichever local System One decision model your server is serving; it defaults to `english`, and `JEV_LAYA_MODEL` still works and is used when `JEV_LOCAL_MODEL` is unset. You are not locked into Laya: any local model speaking the same `/v1/systemone` contract fits, including `kev`, `tev1` and the `jeff` family, selected by that one setting. Plain `laya` is exactly one provider: it replaces the hosted pair rather than joining it, no hosted mode ever selects it, and no mode appends it silently. The DOGA name `laya_local` selects the same mode, as does `local_only`. The `laya_then_*` modes are the explicit opt in that puts the local slot first, keeps the local score on the same legend index scale, and names the hosted provider or providers that answer when the local server does not; `laya_with_jev_fallback` is the DOGA name for `laya_then_openrouter_typesafe`, which names both hosted providers, OpenRouter first. In those modes the fallback stops after three consecutive local failures in a process, so a broken local server cannot quietly turn every review into outbound traffic. The mirror image, `api_with_local_fallback` (or `openrouter_then_laya` and friends), goes to the hosted API first and uses the local server only when it fails.

Three measured limits, from live runs against `laya-serve` on CPU with the base English checkpoint, are worth knowing before you point real work at it. It is slow: about 1.6 seconds per question row, and model load takes 25 to 35 seconds. Its answers are weakly separated, so the approval policy's confidence floor tends to escalate instead of approving. And no quality claim is made for this base checkpoint. The numbers and the score scale are in [the reference](docs/reference.md#measured-limits-of-the-local-checkpoint). Calibrate on your own labelled examples first.

### Why did the agent say it was verified when the target was not checked?

The local verification tool relies on proof fields supplied by the caller. It requires explicit evidence that a change happened, the target was read back, and the result was supported. It cannot detect a caller inventing those facts. Ask Hermes to show what it actually read before accepting the completion claim.

### Does it learn from my decisions or change its own rules?

It records decisions and outcomes for later review. It does not train the model, rewrite its rules, or turn recommendations into automatic actions. You remain responsible for deciding which checks to trust.

### The plugin is installed, but Hermes cannot find its tools. What should I check?

Run `hermes plugins doctor jev-decisions`. Check that both the plugin and the `jev` toolset are enabled in the profile and platform you are using, then restart the relevant Hermes process. If model reviews fail, check the route that process selected: with a hosted mode, the matching `TYPESAFE_API_KEY`, `OPENROUTER_API_KEY`, or `CLOUDFLARE_API_KEY` credential must be available to it, and a key set in a different terminal will not necessarily be available to the desktop backend. With `JEV_PROVIDER_MODE=laya`, check that your local decision server is listening on `JEV_LAYA_BASE_URL` and that `JEV_LOCAL_MODEL` (or, when that is unset, `JEV_LAYA_MODEL`) names the engine it actually serves. With a `laya_then_*` mode, check all three: the local server must answer for the review to stay home, and every hosted provider the mode names needs its key available, or the review fails at selection with the variable named in the error.

### Can I use it with another agent?

Yes, if you connect its Python functions or command line tool to that agent. The portable part is the local policy, verification, and record keeping code. Native tool registration is for Hermes. This repository does not include a ready-made adapter for every agent framework. See [standalone installation and examples](docs/reference.md#install-the-standalone-python-and-cli-gateway).

## Update or disable

Inside the installed plugin directory, check for local changes before updating:

```bash
git status
git pull
hermes plugins doctor jev-decisions
```

Back up custom changes and resolve conflicts rather than overwriting them. Restart the Hermes process after an update. If you install this in a larger system, pin a tested commit and check upgrades deliberately: the provider's Decisions API is currently an alpha API.

To disable the plugin:

```bash
hermes plugins disable jev-decisions
```

Restart Hermes. Keep or remove its local journals separately, according to your privacy needs.

## For developers

The [technical reference](docs/reference.md) contains all six tools, all 25 review definitions, custom questions, Python and command line examples, and test commands. The [integration guide](docs/integrations.md) covers approval handling, evidence, provider requests, and local storage. [CONTRIBUTING.md](CONTRIBUTING.md) explains contribution checks. [CHANGELOG.md](CHANGELOG.md) indexes what changed in each version, with the detailed notes under [docs/releases/](docs/releases/).

The public package includes the code needed to load the plugin without private collectors or deployment files. CI tests Python 3.10, 3.11, and 3.12 and installs the package outside the checkout. Treat that as a tested starting point, then test the tasks you plan to use.

### Repository topics

GitHub allows at most 20 topics per repository. This repository carries 18, and the documentation and the tags are meant to agree:

`action-verification`, `agent-tools`, `ai-agent-safety`, `ai-agents`, `approval-workflows`, `automation`, `clef`, `cloudflare`, `decision-making`, `hermes`, `kev`, `laya`, `mcp`, `openrouter`, `python`, `system-one`, `tev1`, `tool-use`

**Three older, more generic tags were removed to make room, and should not be restored:** `human-in-the-loop`, `agent-architecture`, and `llm`. This repository already held 14 topics before the taxonomy was added, so adding `system-one`, `clef`, `laya`, `kev` and `tev1` would have crossed GitHub's 20 topic cap. The removed three were the most generic of the originals. Re-adding any of them fails, and a future maintainer should swap one out rather than add. Note that `decision-model` from the shared family taxonomy was **not** added, to stay under the cap; `decision-making` and `system-one` carry the meaning.

## License

MIT. Copyright (c) 2026 Bojan Sandhaus. See [LICENSE](LICENSE).