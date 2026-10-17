# SpecSeed: Sound Black-Box Detection of Authorization Bugs in REST APIs via Multi-Identity Differential Oracles

**Term project paper — 2026.**

---

## Abstract

Automated REST API testing has matured at finding server crashes (5XX) and
schema-conformance violations, but remains weak at the defect classes that
dominate real exploitation: broken object-level and function-level
authorization (BOLA/BFLA), ranked #1 and #5 in the OWASP API Security Top 10.
These bugs are structurally invisible to single-identity fuzzers, whose only
verdict on a successful response is "it did not crash and it matched the
schema." We present **SpecSeed**, a black-box REST API tester that drives every
synthesized request sequence with *three identities simultaneously* — the
resource owner, a second legitimate user, and an unauthenticated client — and
compares what each is permitted to observe. SpecSeed layers this
*multi-identity differential oracle* on a soundness pipeline in which every
candidate finding is independently re-checked, replay-verified, and classified
by severity, so that a reproduced-but-benign signal never masquerades as a
vulnerability. On the deliberately-vulnerable benchmarks OWASP crAPI and VAmPI,
SpecSeed finds the planted authorization bugs; on a real, correct production API
(Genome Nexus) it reports **zero false violations**. A third, optional
contribution employs a large language model strictly to *reach* deep
application states — never to decide whether a response is a bug — which
preserves soundness by construction even with a non-deterministic model in the
loop, and makes "with-AI" results reproducible offline via a committed cache.

---

## 1. Introduction

A modern web application is a set of REST endpoints behind authentication.
The highest-impact security defects in such systems are not memory bugs or even
injection, but *authorization* failures: a request that is syntactically valid,
authenticated, and schema-conformant, yet returns data the caller should never
have seen (BOLA), or invokes a function the caller should never have been able
to call (BFLA). The OWASP API Security Top 10 places these two classes at #1 and
#5. They are also the most lucrative bug-bounty categories, precisely because
they are easy to introduce and hard to detect automatically.

The difficulty is epistemic. A single-client fuzzer that issues `GET
/orders/42` and receives `200 OK` with a well-formed order object has no basis
to call it a bug: the response did not crash, and it conformed to the schema.
The bug — "this token belongs to user B, and order 42 belongs to user A" — is
only visible by *comparison across identities*. This observation motivates our
central design choice.

Two forces shape the rest of the design. First, a security tool that produces
false positives is worse than no tool: each false alarm costs a developer time
and erodes trust, so **soundness** (reported findings are real) is as important
as recall. Second, the pragmatic alternative that teams increasingly reach for —
pointing a general LLM at the API — fails on exactly the axes that matter:
LLMs hallucinate findings, they exfiltrate API data to a third-party model
provider, and they are non-deterministic.

SpecSeed is built to beat these constraints. Its contributions are:

1. **A multi-identity differential oracle** for black-box BOLA/BFLA detection
   (§3.3): every synthesized sequence is probed with an owner, an alternate
   user, and an unauthenticated client, and the spec's declared `security`
   defines which endpoints should be protected.
2. **A soundness pipeline** (§3.4): each candidate finding is re-checked for
   reproducibility, replay-verified on a fresh instance, and split into
   *violations* vs *warnings*, under a working discipline where every false
   positive discovered in testing becomes a permanent fix to the tool, the
   spec, or the per-API scope.
3. **LLM-for-reachability** (§3.5): an optional planner uses a language model
   only to propose values that reach deeper states; it is architecturally
   barred from the detection path, so it cannot create a false finding, and its
   outputs are cached for deterministic, offline-reproducible runs.

We evaluate on three real systems (§5): crAPI, VAmPI, and Genome Nexus. The
headline results are the planted bugs found on the vulnerable targets and —
equally important for the soundness claim — zero false violations on the correct
one.

---

## 2. Background and Related Work

**Stateful REST fuzzing.** RESTler [Atlidakis et al., 2019] pioneered stateful
grammar-based fuzzing of REST APIs, inferring producer/consumer dependencies
from an OpenAPI spec to chain requests; its oracle is crash/5XX detection.
EvoMaster [Arcuri, 2019] uses evolutionary search and, in white-box mode, code
coverage; in black-box mode it targets status-code and schema signals. Morest
[Liu et al., 2022] models the API as a RESTful-service property graph to drive
sequence generation. Schemathesis [Hatfield-Dodds, 2021] applies
property-based testing derived from the OpenAPI schema. These systems advanced
coverage and crash-finding but do not reason about *authorization*: they drive
the API as a single client.

**LLMs for REST testing.** RESTGPT [Kim et al., 2024] uses an LLM to extract
machine-readable constraints from natural-language spec descriptions; DeepREST
[Corradini et al., 2024] uses reinforcement learning to learn implicit operation
dependencies; AutoRestTest combines multi-agent LLMs with RL for input and
dependency generation. In these systems the model participates in *generation*,
and thus its errors can propagate into what is reported. SpecSeed differs by
confining the model to reachability and keeping a deterministic, sound oracle
layer as the sole arbiter of findings.

**Authorization testing.** Cross-user access testing exists in interactive
scanners and some commercial DAST tools, typically requiring manual
configuration of two sessions. SpecSeed's contribution is to make the
three-identity differential a first-class, automated oracle over
*synthesized stateful sequences*, combined with a soundness pipeline and a
no-false-positive discipline, and to evaluate it on standard vulnerable
benchmarks plus a real correct API.

---

## 3. Design

### 3.1 Dual-track architecture

SpecSeed is additive by construction. A pre-existing competition engine
(coverage and crash finding, schema conformance) is treated as frozen and
byte-for-byte stable. All new work lives in a separate pipeline ("Track A")
that reads the same OpenAPI spec and talks to the same running instance but
shares no mutable state with the engine. Consequently a fault in Track A cannot
corrupt the engine's results, and new bug classes can be added as isolated,
independently-failing modules. (See the pitch artifact, Fig. 1.)

### 3.2 The testing loop

Track A runs a closed feedback loop per iteration:

1. **Scheduler** — coupled multi-armed bandits select a target operation, an
   optional structural mutation, and an optional attack payload, weighting
   choices by past reward.
2. **Synthesizer** — builds the minimal prefix sequence that makes the target
   reachable, consulting a **Relational Knowledge Graph (RKG)** so that
   prerequisites already satisfied by live state are not rebuilt.
3. **Mutator** — optionally reshapes the plan with one of five operators:
   omit-prerequisite, invert-order, illegal-transition (state-directed via the
   inferred state machine), cross-entity (swap in another tenant's id — a BOLA
   probe), and double-action (fire the final step twice concurrently — a race
   probe).
4. **Executor** — issues each step, binding *related* ids between steps from the
   RKG, and attacking only the final step of an otherwise-valid prefix so deep
   endpoints are reached before being fuzzed.
5. **Oracles** — run per-step (so attribution is correct: a later step cannot be
   blamed for an earlier one) and emit candidate findings.
6. **Reward** — new coverage, new state transitions, new crashes, and new
   violations feed back to the scheduler.

Two learned structures support the loop. The **RKG** tracks parent/child
relationships between entities, so a nested call receives ids that are actually
related (the pet that belongs to *this* owner) rather than a type-correct but
wrong instance that would 404. The **Dynamic State-Machine Inferrer (DSMI)**
tracks per-entity lifecycle by recording the capabilities a transition *removes*
(e.g. a resource that no longer accepts `PUT` after a close), which directs the
illegal-transition mutation.

### 3.3 Multi-identity differential oracle

Every oracle executes with three HTTP clients: a **primary** (owner), an
**alternate** legitimate user, and an **unauthenticated** client. When the
primary creates or reads a resource, the oracle directs the other two at the
*same* resource and compares the responses. The spec's declared `security`
object identifies which endpoints are supposed to require authorization.

- **BOLA** (`cross_user_access`): the alternate user receives the same protected
  data the primary saw.
- **Broken authentication** (`missing_auth`): a declared-protected endpoint
  answers the unauthenticated client.
- **BFLA** (`broken_function_auth`): a non-privileged identity succeeds on an
  administrative function (identified by a name heuristic, deliberately
  conservative to avoid false positives — see §3.4).
- **Hidden mass-assignment** (`mass_assignment`): a field written that should be
  server-controlled is observed via a read-back.

A single-client fuzzer observes three clean `200`s; the differential across
identities is what turns them into a finding. (Pitch artifact, Fig. 3.)

SpecSeed implements 17 oracles in total, grouped as authorization (5), contract
and CRUD (`schema_conformance`, `crud_roundtrip`, `zombie_resource`), lifecycle
and concurrency (`safety`, `idempotency`, `repeated_delete`, `race_condition`,
`dangling_reference`), and injection and response (`second_order_injection`,
`reflected_payload`, `timing_injection`, `cors_misconfig`). Each is an isolated
class exposing `check(sequence, context)` with flags declaring whether it sends
extra probes and whether it mutates state.

### 3.4 The soundness pipeline

Soundness is an explicit multi-stage filter, not an afterthought:

1. **Reproduction re-check** — every candidate is re-run; a signal that does not
   fire twice is discarded as flaky. This generically removes non-deterministic
   false positives without per-oracle patches.
2. **Verification by replay** — the finding is replayed on a freshly restarted
   container (or, for heavy multi-container stacks, live), rebinding ids; only
   findings that reproduce from a clean state survive.
3. **Severity split** — survivors are classified as *violations* (contract
   breaks worth acting on) or *warnings* (reproduced but low-signal: benign
   concurrency, reflection in a non-HTML body, strict-schema mismatches). Only
   violations reach the headline count.

Two further guards operate alongside: **normalization tolerance** (a server may
default, compute, or reformat a field it was not told to preserve — so the
round-trip comparison ignores volatile and read-only fields), and **per-API
scoping** (a public forum's cross-user read is intended behavior, so that
namespace is exempted from the authz oracles — a semantic judgment recorded
explicitly rather than guessed).

The governing discipline is that **every false positive observed during testing
becomes a permanent fix**: a tightened oracle heuristic, a corrected spec, or a
documented per-API scope. Several such fixes are recorded in the project history
(e.g. gating the race oracle to POSTs that actually create a resource, after a
real API's batch-query POSTs produced benign race warnings).

### 3.5 LLM for reachability, oracles for detection

Coverage is capped by endpoints whose valid inputs cannot be derived from the
schema alone — a status constrained in prose to the string `delivered`, a value
described only in a field's `description`. The optional **planner** addresses
this with a single architectural rule:

> The planner proposes concrete *values* to reach a deeper state. It never
> decides whether a response is a bug.

The planner's output is applied by the executor to valid requests only; the
oracles judge the result exactly as before. A wrong hint, at worst, fails to
reach a state — it cannot produce a false finding. Soundness is therefore
preserved by construction, even though the model is non-deterministic. The
planner is **off by default** (the tool runs with no AI and imports no model
SDK); when enabled, each solved hint is cached keyed by a stable operation
signature, so the model is consulted once per novel operation and a committed
cache replays the entire run offline. This makes the "with-AI" numbers
reproducible by a reviewer with no API key. (Pitch artifact, Fig. 5.)

---

## 4. Implementation

SpecSeed is implemented in Python with an `asyncio` HTTP client. Track A
comprises the knowledge graph, state inferrer, sequence synthesizer/mutator/
executor, scheduler, the 17 oracles, a findings store with `curl` replay, and
the optional planner — all additive to a frozen competition engine and spec
component. A campaign runner executes unattended, per-API-parallel, resumable
rounds, writing findings incrementally and regenerating a report each round.
Memory is bounded for long runs: the knowledge graph evicts oldest leaf entities
under a configurable cap, and the value pool and audit log are bounded.
Twenty self-contained test suites cover the graph, synthesis, every oracle, the
soundness mechanisms, findings replay, and the planner.

---

## 5. Evaluation

We ask three research questions:

- **RQ1 (recall).** Does SpecSeed find real authorization bugs on vulnerable
  APIs?
- **RQ2 (soundness).** How many false violations does it report on a real,
  correct API?
- **RQ3 (robustness).** Does it run unattended and stably at scale within a
  bounded memory budget?

All runs are black-box: OpenAPI spec plus running instance, HTTP only, no source
or coverage instrumentation.

### 5.1 RQ1 — OWASP crAPI (44 operations)

SpecSeed reports five authorization/authentication **violations**, each
reproduced from a clean state with a recorded `curl`:

| Finding | Endpoint | Oracle | Class |
|---|---|---|---|
| Any user reads any vehicle's GPS location and owner PII | `GET /vehicle/{id}/location` | `cross_user_access` | BOLA |
| Normal user reaches the admin user directory | `GET /management/users/all` | `broken_function_auth` | BFLA |
| Any order's details and buyer PII readable by id | `GET /shop/orders/{id}` | `cross_user_access` | BOLA |
| The same order needs no token at all | `GET /shop/orders/{id}` | `missing_auth` | broken auth |
| Declared-protected endpoint served unauthenticated | `GET /shop/return_qr_code` | `missing_auth` | low |

On OWASP VAmPI, SpecSeed reports six OWASP-Top-10 findings (BOLA ×2, BFLA,
mass-assignment, race ×2), consistent across runs.

### 5.2 RQ2 — Genome Nexus (23 operations, real and correct)

Genome Nexus is a production bioinformatics annotation API with no planted
bugs. A sound tool should report nothing. SpecSeed issued 8,049 requests over
20/23 operations and reported **0 violations** and **0 server crashes**. The
only signals were low-severity warnings attributable to our hand-written spec
being stricter than the server's actual contract (e.g. an `id` field marked
required that the server legitimately omits for unresolvable inputs), correctly
bucketed below the headline. This is the key result for the soundness claim: on
correct code, the tool invents nothing.

### 5.3 RQ3 — one-hour unattended run (crAPI)

A single sustained run issued **162,363 requests** (152,247 sequences, ~45
req/s) over 60 minutes, covering 26/35 fuzzable operations, with **peak tool
memory of 159 MB** and **zero tool errors or leaks**. Fuzzer memory grew gently
(33→159 MB) and the knowledge graph self-limited under its cap. The same five
violations found in short runs reproduced, confirming determinism.

### 5.4 The ablation (planned)

Because the planner affects only reachability, the planner experiment is a clean
ablation: **spec-only** (`LLM_PLANNER` unset) vs **+planner** (`LLM_PLANNER=1`),
holding seeds, budget, and oracles fixed, and measuring operations reached and
deep findings unlocked. The committed hint cache makes the treatment arm
reproducible offline.

---

## 6. Limitations

- **Spec fidelity.** `schema_conformance` on a hand-written spec tests our
  transcription as much as the server; on an API without a shipped OpenAPI
  document this oracle is better read as a lead than a verdict. We mitigate by
  classifying its signals as warnings.
- **Semantic intent.** Whether a cross-user read is a bug can depend on intent
  the spec does not express (a public forum). We handle this with explicit
  per-API scoping rather than inference.
- **Reachability of out-of-band flows.** The HTTP-only planner cannot satisfy
  prerequisites that require reading an out-of-band channel (email/SMS). This is
  deliberate (it keeps the mode deterministic) and left as future work.
- **Scope.** SpecSeed is an authorization-and-semantics pre-check, not a full
  pentest; it does not target deep business-logic, client-side, or
  infrastructure defects.

---

## 7. Future Work

- A config-driven target loader and `specseed scan` CLI, and a CI mode with a
  baseline/diff that fails a build only on *new* violations — a release gate.
- Sidecar-aware reachability (reading declared out-of-band channels) to unlock
  flows such as verified-resource registration.
- Widening the planner's context (operation-level descriptions) and a full
  precision/recall comparison against EvoMaster and Schemathesis on a shared
  benchmark with manual ground truth.

---

## 8. Conclusion

SpecSeed shows that the most impactful and hardest-to-automate REST API
defects — authorization failures — can be found black-box by driving every
synthesized sequence with multiple identities and comparing their views, and
that this can be done *soundly*: on a real, correct API it reports zero false
violations, while finding the planted bugs on standard vulnerable benchmarks.
By confining a language model to reachability and keeping a deterministic oracle
layer as the sole arbiter of findings, it extends coverage without surrendering
the soundness that makes a security tool worth running.

---

## References

- V. Atlidakis, P. Godefroid, M. Polishchuk. *RESTler: Stateful REST API Fuzzing.* ICSE 2019.
- A. Arcuri. *RESTful API Automated Test Case Generation with EvoMaster.* ACM TOSEM, 2019.
- Y. Liu et al. *Morest: Model-based RESTful API Testing with Execution Feedback.* ICSE 2022.
- Z. Hatfield-Dodds et al. *Schemathesis: property-based testing for OpenAPI.* 2021.
- M. Kim et al. *Enhancing REST API Testing with NLP techniques (RESTGPT).* 2024.
- D. Corradini et al. *DeepREST: Automated Test Case Generation for REST APIs via Deep Reinforcement Learning.* ASE 2024.
- OWASP. *API Security Top 10 (2023).*
- OWASP crAPI; OWASP VAmPI; Genome Nexus (cBioPortal) — evaluation targets.
