"""Record one real exchange for the motivating example (Figure 1 of the paper):
POST /petclinic/api/owners/{ownerId}/pets, once with the baseline generator and once
with SpecSeed + pool after one POST /owners. Writes DATA/motivating.json.

Usage: python3 -m eval.motivating ../paper/data
"""
import json
import os
import random
import sys

from eval import apis, baseline
from eval.http import send
from eval.pool import SimplePool
from restleague.models import Mode
from restleague.spec import values
from restleague.spec.parser import parse_spec

OP = "POST /petclinic/api/owners/{ownerId}/pets"


def exchange(req, resp):
    return {"method": req.method, "path": req.path, "path_params": req.path_params, "body": req.body,
            "status": resp.status, "response": resp.json if resp.json is not None else resp.text[:500]}


def main(out_dir):
    base = apis.start("pet-clinic")
    ops = {o.key: o for o in parse_spec(os.path.join(os.path.dirname(__file__), "..", "specs",
                                                     "pet-clinic-openapi.json"))}
    baseline.rng.seed(7)
    values.rng.seed(7)
    out = {"operation": OP, "schema": ops[OP].request_body.schema}

    req = baseline.naive_request(ops[OP], None, Mode.VALID)
    out["baseline"] = exchange(req, send(base, req))

    pool = SimplePool(random.Random(7))
    owner_req = values.generate_request(ops["POST /petclinic/api/owners"], pool, Mode.VALID)
    owner_resp = send(base, owner_req)
    pool.record(ops["POST /petclinic/api/owners"], owner_resp)
    types_resp = send(base, values.generate_request(ops["GET /petclinic/api/pettypes"], pool, Mode.VALID))
    pool.record(ops["GET /petclinic/api/pettypes"], types_resp)
    out["tool_setup"] = [exchange(owner_req, owner_resp)]
    req = values.generate_request(ops[OP], pool, Mode.VALID)
    out["tool"] = exchange(req, send(base, req))
    apis.stop("pet-clinic")

    with open(os.path.join(out_dir, "motivating.json"), "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps({k: out[k] for k in ("baseline", "tool")}, indent=1))


if __name__ == "__main__":
    main(sys.argv[1])
