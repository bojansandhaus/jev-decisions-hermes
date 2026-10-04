THIRD PARTY NOTICES

hermes-jev-approvals

The optional approval provider in integrations/approval-provider is adapted from:
https://github.com/anpicasso/hermes-jev-approvals

Original author: anpicasso.

The upstream license is reproduced below:

MIT License

Copyright (c) 2026 anpicasso

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

The upstream project is distributed under the MIT License. The adapted provider retains its
MIT licensing terms. The surrounding Jev Decisions plugin is separately Copyright (c) 2026
Bojan Sandhaus and distributed under the MIT License in LICENSE.

The release notes quote the upstream copyright statement with attribution.
The notice above preserves the adapted implementation's license and provenance.

---

hermes-jev

The supervision layer in `supervision.py` is adapted from:

https://github.com/keeltrace/hermes-jev

Reviewed revision: 4feea5ef45aeb301622f18175ed4cf2e068b99bd
Upstream release at that revision: hermes-jev v0.2.1.1
Upstream `plugin.yaml` author field at that revision: KeelTrace community
Upstream license at that revision: MIT, "Copyright (c) 2026 Hermes-Jev contributors"

Adapted behaviors: asynchronous turn admission, a local adaptive relevance router
with semantic hysteresis and bounded batching, confidence-gated challenge delivery
that expires with the challenged state, and action and failure fingerprints with a
provider independent local REPLAN control.

Upstream wording quoted verbatim in `supervision.py` and `docs/releases/0.3.0.md`:

> "Hermes remains the reasoning and execution engine. Jev supervises accountable decisions in parallel..."
> "The recommended path is the nervous system, not synchronous evaluate-every-tool gating."
> "Late opinions remain receipts, not commands."

Note for readers: upstream has since renamed and reorganized the project. The
`hermes-jev` repository now publishes as `nerve` v0.2.2, and its `LICENSE` reads
"Copyright (c) 2026 Nerve contributors". The quotation wording above is attributed
to the revision that was actually reviewed, so it may not appear in the current
upstream tree.

The upstream license, reproduced from that repository:

MIT License

Copyright (c) 2026 Hermes-Jev contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

The adapted supervision layer retains those MIT terms. The surrounding Jev
Decisions plugin is separately Copyright (c) 2026 Bojan Sandhaus and distributed
under the MIT License in LICENSE.

---

osENV.io

The lesson lifecycle in `lessons.py` is adapted from the **design** of:

https://github.com/psygns/osENV.io

Reviewed revision: 0ef07457d461a9d9a30e695799f1ab1e329c2a45
Upstream release at that revision: 0.3.0
Original author: psygns

NO CODE WAS COPIED FROM THIS PROJECT.

That repository contains no license file. Absent a license, its source is
all rights reserved, so nothing from it may be redistributed here. Only the
published design was studied, and `lessons.py` is an independent Python
implementation of it. No upstream source, identifier, comment, or text was
copied, and no MIT notice is claimed for it.

Adapted design: a lesson is a written rule plus a precise description of the
mistake as it is about to happen; severity escalates from advice to a hard stop
once the same mistake has escaped twice; a lesson judged relevant many times
without ever catching anything is noise and retires; proven lessons travel
between installs as a pack.

Upstream wording quoted verbatim in `lessons.py` and `docs/releases/0.4.0.md`,
for attribution:

> "the same mistake as an existing lesson? Then that lesson is sharpened, and escalates to a kick after 2 escapes, instead of a copy piling up."
> -- psygns, osENV.io README.md

> "Lessons that Jev keeps calling relevant but that never catch anything are noise: they retire."
> -- psygns, osENV.io learn.go

Deliberate differences in this implementation: lessons are ranked with a
deterministic score over canonical words rather than vector similarity, and a repeat
merges on a wording match with a manual correction path, rather than on a model
judgement. The canonical word table is a small hand written list, not a language
model, because a local hard stop has to be auditable and cannot call a provider.

---

Cloudflare Clef

The optional hosted provider route in `jev_client.py` (`JEV_PROVIDER_MODE=clef`,
or the DOGA alias `clef_api`) calls the Cloudflare Clef decision model over the
Cloudflare Workers AI REST API:

https://developers.cloudflare.com/workers-ai/models/clef/
https://developers.cloudflare.com/workers-ai/models/clef-flash/

NO CLEF CODE OR MODEL WEIGHTS ARE VENDORED, COPIED, OR ADAPTED IN THIS
REPOSITORY.

Clef is not a dependency of this package and is not installed by it. The route
speaks the request and response contract that Cloudflare publishes, and nothing
else: `POST /client/v4/accounts/{account}/ai/run/@cf/cloudflare/{model}` with a
`{"model", "state", "questions"}` body, and the typed `noul`, `choice` and `score`
answers that contract defines.

Clef's weights are distributed by Cloudflare under the Apache License 2.0, and
the API is Cloudflare's own service subject to its terms. The surrounding Jev
Decisions plugin is separately Copyright (c) 2026 Bojan Sandhaus and distributed
under the MIT License in LICENSE. Because no Clef source or weights are
redistributed here, no Apache 2.0 notice is reproduced in this file; the
license text ships with the model at the links above and in Cloudflare's
Workers AI terms.

Using this route sends the bounded review `state` and the questions to
Cloudflare. It needs two environment values: `CLOUDFLARE_API_TOKEN`, a
Cloudflare API token with **Account > Workers AI > Read**, which is a credential
and is held in the Hermes secret scope, and `CLOUDFLARE_ACCOUNT_ID`, the 32
character account id, which is configuration rather than a secret but does appear
in the request URL. Requests on this route are billed to the Cloudflare account
under Workers AI pricing. Nothing on this route is exercised against a live
Cloudflare account by this repository's test suite; see
`docs/releases/0.7.0.md`.

---

Laya

The optional local provider route in `jev_client.py` (`JEV_PROVIDER_MODE=laya`) is a
plain HTTP client for a `laya-serve` process that the operator installs and starts
separately:

https://github.com/NandhaKishorM/laya

Original author: Convai Innovations.

NO LAYA CODE IS VENDORED, COPIED, OR ADAPTED IN THIS REPOSITORY.

Laya is not a dependency of this package and is not installed by it. The route
speaks the wire contract that Laya publishes, and nothing else. Verified from the
installed distribution metadata of `laya` 0.3.20 on 2026-09-26:

- `METADATA`: `Name: laya`, `Version: 0.3.20`, `Author: Convai Innovations`,
  `License: Apache-2.0`, `License-File: LICENSE`.
- `laya/serve.py` describes itself as an "HTTP server exposing Laya over TypeSafe
  Jev's ``/v1/systemone`` wire protocol", with "a ``POST /v1/systemone`` route, an
  optional bearer check, and a health" endpoint.

Laya is distributed under the Apache License 2.0. Its full text ships upstream and in
the installed `laya-0.3.20.dist-info/LICENSE`. Because no Laya source is
redistributed here, no Laya notice is reproduced in this file. The surrounding Jev
Decisions plugin is separately Copyright (c) 2026 Bojan Sandhaus and distributed
under the MIT License in LICENSE.
