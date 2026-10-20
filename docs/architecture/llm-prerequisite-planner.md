# LLM prerequisite planner (optional, HTTP-only)

An **optional** component that uses an LLM to help the fuzzer reach deeper
application states, without ever letting the LLM decide whether something is a
bug. It is **off by default**: the tool runs with or without AI, and the two
modes are the two arms of a controlled experiment.

## The one-sentence design

> **LLM for reachability, deterministic oracles for detection.**

The planner only proposes concrete *values* for request fields a schema-only
generator cannot satisfy (an enumeration named in a property description, a
required status string, a format stated in prose). Those values change which
requests are *sent*; they never touch the oracles that judge responses. A wrong
hint, at worst, fails to reach a state — **it can never manufacture a false
finding.** This is what preserves the no-false-positive guarantee even with a
nondeterministic model in the loop, and it is the paper's core claim: prior
LLM-for-REST work (RESTGPT, DeepREST, AutoRestTest) does not make this split
cleanly.

## Where it plugs in

```
scheduler → synthesizer → [planner hints] → executor → oracles
                             ▲ optional, VALID requests only
```

`AsyncSequenceExecutor.build_request` generates a request from the frozen spec
component as always, then — only when a planner is set and the step is `VALID` —
overlays the planner's `{field: value}` hints (`executor._apply_hints`). The
frozen competition engine (`restleague/engine`, `restleague/spec`) is never
touched; everything lives in `restleague/sequence/planner.py`.

## Runs with and without AI

| Mode | How | Behaviour |
|---|---|---|
| **Without AI** (default) | `LLM_PLANNER` unset | `NullPlanner`; the `anthropic` package is never imported; request generation is the exact spec-only baseline. |
| **With AI** | `LLM_PLANNER=1` | `CachingPlanner` over an Anthropic backend; hints applied to VALID requests. |

```bash
# baseline (no AI, no dependency)
TRACK_A=1 python -m eval.campaign --apis crapi --budget 300 --rounds 1

# treatment (AI): needs `pip install anthropic` + credentials
LLM_PLANNER=1 TRACK_A=1 python -m eval.campaign --apis crapi --budget 300 --rounds 1
```

If `LLM_PLANNER=1` but the SDK or credentials are missing, the planner logs a
warning and returns no hints — the run degrades to spec-only rather than
failing. Enabling AI can never crash a run.

## Determinism (what makes the "with AI" numbers reproducible)

Every solved hint set is cached to a JSON file keyed by a stable operation
signature (method, path, and a hash of the request schema — `op_signature`). The
model is consulted **once per novel operation**; afterwards, and for a committed
cache, the entire run replays **offline with no model call**. A reviewer with no
API key reproduces the exact "with AI" results from the committed cache. The
model also takes no `temperature` (sampling is rejected on current models), so
determinism comes from the cache, not from a sampling knob.

Model: a bulk extractor, default `claude-sonnet-5-5` (override with
`LLM_PLANNER_MODEL`); cache path `specs/<api>.planner-cache.json` (override with
`LLM_PLANNER_CACHE`).

## The experiment

The planner affects **only reachability**, never detection, so the ablation
isolates its effect cleanly:

| | Ops reached | Deep findings unlocked |
|---|---|---|
| spec-only (`LLM_PLANNER` unset) | baseline | baseline |
| + LLM planner (`LLM_PLANNER=1`) | ? | ? |

Same seeds, same budget, same oracles. If the planner lifts coverage past the
spec-only ceiling (crAPI: ~25–27 / 35) and unlocks bugs behind value-gated
endpoints, that table is the contribution.

## Scope / honest limits

- **HTTP-only.** The planner reasons from the spec + what it is given; it does
  **not** read out-of-band channels (email/SMS sidecars), so flows like crAPI's
  email→VIN vehicle registration stay out of reach. That is deliberate (keeps
  the mode fully in-protocol and deterministic) and is explicit future work.
- Operation-level prose (summaries/descriptions outside the schema) is not
  currently passed to the model — only the request schema, parameters and
  operation id — so the signal is schema- and property-level. Widening the
  context is a straightforward extension.
