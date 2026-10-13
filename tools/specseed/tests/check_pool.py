"""Check generate_request uses pool.lookup(name, schema, op) for path/query params and id-like body fields."""
from restleague.models import Mode
from restleague.spec.deps import order_operations
from restleague.spec.parser import parse_spec
from restleague.spec.values import generate_request


class FakePool:
    def __init__(self):
        self.calls = []

    def lookup(self, name, schema, op):
        self.calls.append((op.key, name))
        return 4242 if name.lower().endswith("id") else None

    def lookup_object(self, resource):
        return {"id": 7, "name": "cat"} if resource == "type" else None


pool = FakePool()
ops = {o.key: o for o in order_operations(parse_spec("specs/pet-clinic-openapi.json"))}

req = generate_request(ops["POST /petclinic/api/owners/{ownerId}/pets"], pool, Mode.VALID)
print("request :", req.method, req.path, req.path_params, req.body, req.content_type, req.op_key)
assert req.path_params == {"ownerId": 4242}, req.path_params
assert req.body["type"] == {"id": 7, "name": "cat"}, req.body
assert ("POST /petclinic/api/owners/{ownerId}/pets", "ownerId") in pool.calls

req = generate_request(ops["PUT /petclinic/api/owners/{ownerId}/pets/{petId}"], pool, Mode.VALID)
assert req.path_params == {"ownerId": 4242, "petId": 4242}, req.path_params

bad = [generate_request(ops["POST /petclinic/api/owners"], pool, Mode.INVALID) for _ in range(20)]
print("invalid :", [r.body if r.raw_body is None else r.raw_body for r in bad[:4]])
print("OK - pool.lookup used for path params; lookup_object used for resource references")
