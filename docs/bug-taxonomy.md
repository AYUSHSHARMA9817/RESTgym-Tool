# Black-box REST API bug taxonomy — what's findable, and where we stand

The universe of bugs a black-box REST tester can find, each tagged with where we
stand: **done** (we detect it, tested), **partial** (we detect some of it, with a
known gap), **gap** (findable in principle, we don't yet), or **out of scope**
(not findable black-box, or not worth it here).

"Black-box" means: we have the OpenAPI spec and a running instance, we send HTTP
and read responses, and we may have credentials. No source, no coverage map, no
database access. Anything needing those is **out of scope** and marked so.

The columns that matter for planning are **status** and **what's missing** — read
down the gaps to see the step-by-step work.

---

## A. Crashes and faults (server breaks)

| # | Bug class | How it's found black-box | Status | What's missing |
|---|---|---|---|---|
| A1 | 500 on malformed input (type confusion, boundary, malformed JSON) | send bad values, watch for 5XX | **done** | — `engine/mutations.py` + `FaultTracker` |
| A2 | 500 behind a valid parent chain (deep endpoint) | reach it with real related ids, then attack | **done** | — Track A reaches it, attack payloads fire on the final step |
| A3 | Crash disguised as 4xx (stack trace in a 400/200 body) | scan non-5XX bodies for stack-trace signatures | **done** | — `schema_conformance` oracle |
| A4 | Unhandled content-type / malformed multipart | wrong `Content-Type`, truncated bodies | **done** | — `wrong_content_type`, `malformed_body` |
| A5 | Crash only after state builds up (Nth call, full collection) | long run, growing resource pool | **partial** | longer campaigns; we see some via the pool but don't target depth deliberately |

## B. Injection (input reaches an interpreter)

| # | Bug class | How it's found black-box | Status | What's missing |
|---|---|---|---|---|
| B1 | SQL / NoSQL injection (error-based) | injection payloads, watch for 5XX or DB-error strings | **done** | — `SQLI`/`NOSQLI` families |
| B2 | Command / template / XXE / header injection, path traversal | payload families on string slots | **done** | — all families present in `mutations.py` |
| B3 | **Second-order injection** (stored via one endpoint, fires on another) | store a payload, then read it back through a *different* endpoint and watch *that* one break | **done** | — `second_order_injection` oracle + taint registry; violation when the error echoes the stored payload, warning on correlation alone |
| B4 | Blind injection (time-based, no error leaked) | measure response-time deltas for a sleep payload | **done** | — `timing_injection` oracle: sleep payload + slow response |
| B5 | Reflected XSS / unescaped reflection | send a marker, check it comes back unescaped | **done** | — `reflected_payload` oracle: verbatim echo in HTML = violation, else warning |

## C. Authorization (who may touch what) — OWASP API1/API3/API5

| # | Bug class | How it's found black-box | Status | What's missing |
|---|---|---|---|---|
| C1 | BOLA within one tenant (parent A's URL, parent B's child) | cross-entity path binding, flag a 2XX | **partial** | `cross_entity_access` crosses *resource* ids; it does not yet cross *user* ids |
| C2 | **BOLA across users** (user A's token on user B's object) | log in as two users, use A's token on B's resource, expect 403/404 | **done** | — `cross_user_access` oracle; found & confirmed 2 real BOLA bugs in VAmPI |
| C3 | **BFLA** (regular user reaching an admin-only operation) | call privileged operations with a low-privilege token | **done** | — `broken_function_auth` oracle; admin-op naming heuristic + anon/other-user replay; caught VAmPI `/_debug` |
| C4 | Missing auth entirely (sensitive op answers with no token) | send the request with the auth header stripped, expect 401/403 | **done** | — `missing_auth` oracle, keyed on the spec's `security` requirement |
| C5 | Token flaws (none-alg JWT, no expiry check, weak signature) | tamper the token, replay | **gap** | JWT-specific; narrower, lower priority |

## D. Data integrity and state (the entity misbehaves)

| # | Bug class | How it's found black-box | Status | What's missing |
|---|---|---|---|---|
| D1 | Written field not persisted (CRUD round-trip) | write, read back, compare writable fields | **done** | — `crud_roundtrip`, with normalisation tolerance |
| D2 | Zombie resource (readable after a successful DELETE) | DELETE, then GET, with retry for eventual consistency | **done** | — `zombie_resource` |
| D3 | Mass assignment, **echoed** (undeclared field stored and returned) | send `isAdmin`/`role`, read it back | **done** | — `mass_assignment` |
| D4 | Mass assignment, **hidden** (stored but never echoed) | send the field, then look for it through *any* endpoint that exposes it (list/debug), not just the item GET | **done** | — `mass_assignment` read-back generalised to listing/debug routes; caught VAmPI's `admin:true` via `/_debug` |
| D5 | Illegal state transition (pay an already-paid order) | learn the lifecycle, attempt an operation never seen from that state | **partial** | `illegal_transition` + DSMI exist; we flag a 5XX but not a *silently accepted* illegal transition |
| D6 | Dangling reference (child usable after its parent is deleted) | delete a parent, then use an orphaned child id | **done** | — `dangling_reference` oracle; probes orphans via their standalone route |
| D7 | Race / double-action (double-spend, duplicate create) | fire the state-changing call twice concurrently, compare outcomes | **done** | — `race_condition` oracle; DELETE both-2XX = violation, concurrent create = warning; verified by concurrent re-fire |

## E. Contract and safety semantics (RFC 9110 / OpenAPI)

| # | Bug class | How it's found black-box | Status | What's missing |
|---|---|---|---|---|
| E1 | Response violates its declared schema | validate 2XX bodies against the spec | **done** | — `schema_conformance` |
| E2 | Non-idempotent PUT (repeat changes state) | PUT twice, compare stable views | **done** | — `idempotency` |
| E3 | Unsafe GET/HEAD (a read changes state) | read twice, compare | **done** | — `safety` |
| E4 | Repeated DELETE crashes | DELETE twice, expect 404/204 not 5XX | **done** | — `repeated_delete` |
| E5 | Wrong status code (404 where 400 is due, 200 on an error) | compare status to outcome | **gap** | partially implied by E1; no dedicated status-semantics oracle |
| E6 | Missing/mis-set security headers, verbose error leakage | inspect response headers and error bodies | **partial** | stack-trace leakage (A3) and CORS wildcard+credentials (`cors_misconfig`) done; other headers (cache, CSP) not checked |

## F. Resource exhaustion and limits (OWASP API4)

| # | Bug class | How it's found black-box | Status | What's missing |
|---|---|---|---|---|
| F1 | No pagination limit (ask for everything, huge response) | request an unbounded list / huge `limit` | **gap** | easy: send a large `limit`/`size`, flag a very large or very slow response |
| F2 | Unbounded array / deep nesting accepted (ReDoS, memory) | send huge arrays / deeply nested bodies | **partial** | `array_size`, deep-nesting payloads sent; the slow-endpoint back-off notices the symptom but nothing *reports* it as a finding |
| F3 | No rate limiting on a sensitive op (brute-force login) | hammer an endpoint, check nothing stops you | **out of scope** here | load-shaped; risky and noisy, low value for this tool |

## G. Business logic (needs domain meaning)

| # | Bug class | Status |
|---|---|---|
| G1 | Negative price / quantity accepted, discount abuse, workflow skips | **out of scope** (mostly) — needs semantic understanding of the domain; some overlaps with D5 (illegal transitions) which we can reach structurally |

---

## What is NOT findable black-box (so, never our job)

- Code-coverage-guided path finding (no instrumentation — this is the AHFUZZ/greybox world).
- Memory-safety bugs with no observable symptom (needs ASan/UBSan on an instrumented build).
- Internal logic errors that never surface in a response.
- Anything requiring source, DB, or logs to *confirm* (we can suspect, not prove).

---

## The step-by-step plan these gaps imply

Ordered by value per unit of work, and by what unblocks the most downstream.

**Tier 1 — unlock a whole bug family with one capability**
1. **Multi-identity auth** (C2, C3). Two logged-in users, optionally one admin. Unblocks BOLA-across-users and BFLA — the dominant bug class in VAmPI, crAPI, and most real APIs. Everything else in C depends on it.
2. **Un-authenticated replay** (C4). Replay a successful authed request with the header stripped; flag a 2XX. Tiny, and a real OWASP-API-top-10 class.

**Tier 2 — finish families we've half-built**
3. **Second-order injection** (B3). We already store payloads and walk sequences; correlate a later 5XX to an earlier stored payload. Uniquely a stateful-tool win.
4. **Dangling-reference oracle** (D6). `cascade_delete` already hands us the orphaned ids — go use them and flag a 2XX.
5. **Race oracle** (D7). `double_action` already fires the pair; add the oracle that flags "both succeeded."
6. **Behavioural mass-assignment** (D4). When a written privileged field isn't echoed, probe whether it took effect.

**Tier 3 — cheap breadth — MOSTLY DONE**
7. ~~Blind/timing injection (B4)~~ and ~~reflected-XSS (B5)~~ — `timing_injection`, `reflected_payload`.
9. ~~Security-header (E6)~~ — `cors_misconfig` (wildcard origin + credentials).
   Still open, lower value: **pagination/limit (F1)** and **resource-limit
   reporting (F2)** -- symptoms we provoke but don't yet report; and
   **status-semantics (E5)** -- too intent-dependent to flag soundly without
   more spec signal.

**Tier 1 is the clear next step** — it converts the largest findable-but-unreached
bug family into a reached one, and it's the prerequisite for crAPI being worth running.
