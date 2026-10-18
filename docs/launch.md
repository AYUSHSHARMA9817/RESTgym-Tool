# Launch posts (drafts)

Honest, no-hype drafts. Security readers punish overclaiming instantly, and
SpecSeed's whole brand is *not crying wolf* — so the copy stays understated and
lets the result (zero false positives on real code) do the work.

Replace `<repo-url>` and `<demo-url>` before posting.

---

## Show HN

**Title:**
`Show HN: SpecSeed – black-box tester that finds API authorization bugs (BOLA/BFLA)`

**Body:**

I built SpecSeed to catch the API bugs normal fuzzers miss: broken object- and
function-level authorization (BOLA/BFLA), which are #1 and #5 in the OWASP API
Security Top 10.

The problem is that a single-client fuzzer can't see these. If `GET /orders/42`
returns `200` with a valid order, nothing looks wrong — unless you ask whether
*this* caller should have seen *that* order. SpecSeed drives every synthesized
request sequence with three identities at once (the owner, a second user, and no
credentials) and compares what each is allowed to read. That comparison is what
turns three clean 200s into a finding.

The thing I care most about is soundness. A security tool that produces false
positives wastes more time than it saves, so every finding is re-run, replayed
on a clean instance, and split into violations vs warnings. On OWASP crAPI it
finds 5 real authz/auth bugs (each with a curl to reproduce); on OWASP VAmPI, 6.
The test I'm proudest of: pointed at a real, correct production API (Genome
Nexus), it reports zero false violations. It stays quiet when there's nothing to
find.

It's black-box (OpenAPI spec + running instance, no source, no coverage), it's
deterministic, and it runs entirely in your own network — no data goes to a
model. There's an optional LLM mode that only helps *reach* deeper states and is
architecturally barred from deciding what's a bug, so it can't introduce false
positives; it's off by default.

Honest limitations: it needs an OpenAPI spec, it's an authorization/semantics
pre-check rather than a full pentest, and I haven't yet done a precision/recall
comparison against EvoMaster/Schemathesis. Feedback very welcome, especially on
the oracle design and the soundness approach.

Repo: `<repo-url>` · 60s demo (finding a real crAPI BOLA): `<demo-url>`

---

## Blog post

**Title:** Why API fuzzers miss authorization bugs — and how to find them black-box

**Outline / talking points:**

1. **The blind spot.** REST fuzzers (RESTler, EvoMaster, Schemathesis) are good
   at crashes and schema violations because those are visible to one client. But
   the bugs that get exploited — BOLA/BFLA — are *relational*: whether a 200 is a
   leak depends on *who asked*. Show the `GET /orders/42` example: identical
   response, different verdict depending on the token.

2. **The idea: differential testing across identities.** Run every sequence with
   owner / alternate-user / unauthenticated clients; compare. Walk through the
   crAPI vehicle-location BOLA: a user who owns no vehicle reads any vehicle's
   GPS and owner email. Include the actual request/response.

3. **The hard part is soundness, not detection.** Anyone can flag "a 200 came
   back"; the work is *not* flagging correct behavior. Describe the pipeline:
   reproduce → verify on a clean instance → severity split, plus normalization
   tolerance and scoping public-by-design endpoints (a public forum's cross-user
   read is not a bug). Then the proof: zero false violations on Genome Nexus, a
   real correct API.

4. **Using an LLM without trusting it.** The rule: LLM for reachability, sound
   oracles for detection. A wrong hint fails to reach a state; it can't
   manufacture a finding. Caching makes runs reproducible offline.

5. **Try it / limitations.** Quickstart against crAPI; honest scope (needs a
   spec, pre-check not pentest, no baseline comparison yet); roadmap (CLI, CI
   gate). Link the repo and demo.

Cross-post to dev.to and /r/netsec after it's up.

---

## Where to post (in order)

1. GitHub repo polished (README + demo GIF embedded) — everything links here.
2. Show HN (pick a weekday morning US time; reply to every comment).
3. Blog post → cross-post dev.to, then r/netsec, r/devsecops, r/webdev.
4. PR to add it to `awesome-api-security` and similar awesome-lists.
5. OWASP API Security community / Slack (on-topic: it tests crAPI, an OWASP project).
6. LinkedIn / Mastodon infosec — share the blog post, not the bare repo link.

Keep every claim checkable, answer criticism straight, and ship updates
regularly. Reputation compounds; a single launch doesn't.
