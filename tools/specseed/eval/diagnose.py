"""Explain the operations that stayed unreached in the live runs (Discussion section).
Sends a few hand-picked requests and records the exchanges in DATA/diagnosis.json.

  1. gestao-hospital POST /v1/hospitais/ with a SpecSeed VALID body
  2. pet-clinic GET /owners/{ownerId}/pets/{petId} with a pet that belongs to the owner,
     and with a pet that belongs to a different owner

Usage: python3 -m eval.diagnose ../paper/data
"""
import json
import os
import sys

from eval import apis
from eval.http import send
from restleague.models import Mode, Request
from restleague.spec import values
from restleague.spec.deps import dependencies
from restleague.spec.parser import parse_spec

SPECS = os.path.join(os.path.dirname(__file__), "..", "specs")


def record(req, resp):
    return {"method": req.method, "path": req.path, "path_params": req.path_params, "body": req.body,
            "status": resp.status, "response": resp.json if resp.json is not None else resp.text[:600]}


def main(out_dir):
    out = {}
    values.rng.seed(11)

    base = apis.start("gestao-hospital")
    ops = {o.key: o for o in parse_spec(os.path.join(SPECS, "gestao-hospital-openapi.json"))}
    op = ops["POST /v1/hospitais/"]
    out["gestao_dependencies"] = {k: {kk: sorted(vv) for kk, vv in v.items()}
                                  for k, v in dependencies(list(ops.values())).items()}
    out["gestao_post_hospital_schema"] = op.request_body.schema if op.request_body else None
    out["gestao_post_hospital"] = []
    for _ in range(3):
        req = values.generate_request(op, None, Mode.VALID)
        out["gestao_post_hospital"].append(record(req, send(base, req)))
    # the same body with and without a client-chosen "id"
    body = dict(out["gestao_post_hospital"][0]["body"] or {})
    body.pop("id", None)
    for label, b in (("without_id", body), ("with_id", {**body, "id": "10"})):
        req = Request(method="POST", path=op.path, body=b, content_type="application/json")
        out[f"gestao_post_hospital_{label}"] = record(req, send(base, req))
    # PUT on a hospital that was just created: body without id, with the same id, with another id
    new_id = out["gestao_post_hospital_without_id"]["response"]["id"]
    put = ops["PUT /v1/hospitais/{hospital_id}"]
    for label, extra in (("no_id", {}), ("same_id", {"id": new_id}), ("other_id", {"id": "3"})):
        req = Request(method="PUT", path=put.path, path_params={"hospital_id": new_id},
                      body={**body, **extra}, content_type="application/json")
        out[f"gestao_put_new_hospital_{label}"] = record(req, send(base, req))
    req = Request(method="PUT", path=put.path, path_params={"hospital_id": "1"},
                  body={**body, "id": "3"}, content_type="application/json")
    out["gestao_put_seeded_hospital_other_id"] = record(req, send(base, req))
    apis.stop("gestao-hospital")

    base = apis.start("pet-clinic")
    owners = send(base, Request(method="GET", path="/petclinic/api/owners")).json
    with_pets = [o for o in owners if o.get("pets")]
    a, b = with_pets[0], with_pets[1]
    pet_of_a = a["pets"][0]["id"]
    path = "/petclinic/api/owners/{ownerId}/pets/{petId}"
    same = Request(method="GET", path=path, path_params={"ownerId": a["id"], "petId": pet_of_a})
    other = Request(method="GET", path=path, path_params={"ownerId": b["id"], "petId": pet_of_a})
    out["petclinic_pet_of_owner"] = record(same, send(base, same))
    out["petclinic_pet_of_other_owner"] = record(other, send(base, other))
    apis.stop("pet-clinic")

    with open(os.path.join(out_dir, "diagnosis.json"), "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out, indent=1)[:4000])


if __name__ == "__main__":
    main(sys.argv[1])
