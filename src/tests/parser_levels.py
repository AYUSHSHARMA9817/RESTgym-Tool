"""Incremental exercises for restleague/spec/parser.py.

Each level is a tiny spec + the exact output expected from parse_spec().
Every level adds ONE new idea. The runner stops at the first failing level.

    python3 -m tests.parser_levels          # run until first failure
    python3 -m tests.parser_levels 5        # run only level 5
    python3 -m tests.parser_levels show 5   # print level 5's input + expected output

Output format compared (see summarize() below), one dict per Operation, in file order:
    {"op_id", "method", "path", "params": [(name, location, required, schema)],
     "body": body_schema, "body_type": body_content_type, "body_required": bool,
     "responses": {status: schema}}
"""
import json
import os
import sys
import tempfile

from restleague.spec.parser import parse_spec


def op(method, path, params=(), body=None, body_type=None, body_required=False, responses=None):
    return {"op_id": f"{method} {path}", "method": method, "path": path, "params": list(params),
            "body": body, "body_type": body_type, "body_required": body_required,
            "responses": responses or {}}


LEVELS = []


def level(fn):
    LEVELS.append(fn)
    return fn


# ---------------------------------------------------------------------------
@level
def one_get():
    """L1: one path, one GET, nothing else. Method is UPPER-case; op_id = 'METHOD path'."""
    spec = {"openapi": "3.0.1",
            "paths": {"/health": {"get": {"responses": {}}}}}
    return spec, [op("GET", "/health")]


@level
def many_methods():
    """L2: several paths/methods, kept in file order. Non-method keys
    (summary, description, parameters, servers) under a path are NOT operations."""
    spec = {"openapi": "3.0.1",
            "paths": {
                "/owners": {"summary": "owners", "post": {"responses": {}}, "get": {"responses": {}}},
                "/owners/{id}": {"description": "one owner", "get": {"responses": {}},
                                 "put": {"responses": {}}, "delete": {"responses": {}}},
            }}
    return spec, [op("POST", "/owners"), op("GET", "/owners"),
                  op("GET", "/owners/{id}"), op("PUT", "/owners/{id}"), op("DELETE", "/owners/{id}")]


@level
def inline_params():
    """L3: inline parameters. `required` defaults to False, BUT path params are
    always required. A missing schema becomes {}."""
    spec = {"openapi": "3.0.1",
            "paths": {"/owners/{id}": {"get": {
                "parameters": [
                    {"name": "id", "in": "path", "schema": {"type": "integer"}},
                    {"name": "verbose", "in": "query", "schema": {"type": "boolean"}},
                    {"name": "X-Trace", "in": "header", "required": True},
                ],
                "responses": {}}}}}
    return spec, [op("GET", "/owners/{id}", params=[
        ("id", "path", True, {"type": "integer"}),
        ("verbose", "query", False, {"type": "boolean"}),
        ("X-Trace", "header", True, {}),
    ])]


@level
def path_level_params():
    """L4: parameters declared on the path item apply to every method.
    Operation-level params override path-level ones with the same (name, in).
    Order: path-level first (in their order, replaced in place if overridden), then new op-level ones."""
    spec = {"openapi": "3.0.1",
            "paths": {"/owners/{id}": {
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}},
                               {"name": "lang", "in": "query", "schema": {"type": "string"}}],
                "get": {"responses": {}},
                "delete": {"parameters": [{"name": "lang", "in": "query", "required": True,
                                           "schema": {"type": "string", "enum": ["en"]}},
                                          {"name": "force", "in": "query", "schema": {"type": "boolean"}}],
                           "responses": {}}}}}
    return spec, [
        op("GET", "/owners/{id}", params=[("id", "path", True, {"type": "integer"}),
                                          ("lang", "query", False, {"type": "string"})]),
        op("DELETE", "/owners/{id}", params=[("id", "path", True, {"type": "integer"}),
                                             ("lang", "query", True, {"type": "string", "enum": ["en"]}),
                                             ("force", "query", False, {"type": "boolean"})]),
    ]


@level
def inline_body():
    """L5: requestBody. Prefer application/json; otherwise take the FIRST content type.
    body_required defaults to False. No requestBody -> body None, body_type None."""
    obj = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    spec = {"openapi": "3.0.1",
            "paths": {
                "/a": {"post": {"requestBody": {"required": True, "content": {
                    "application/xml": {"schema": {"type": "string"}},
                    "application/json": {"schema": obj}}}, "responses": {}}},
                "/b": {"put": {"requestBody": {"content": {
                    "application/x-www-form-urlencoded": {"schema": obj},
                    "text/plain": {"schema": {"type": "string"}}}}, "responses": {}}},
            }}
    return spec, [op("POST", "/a", body=obj, body_type="application/json", body_required=True),
                  op("PUT", "/b", body=obj, body_type="application/x-www-form-urlencoded")]


@level
def simple_ref():
    """L6: $ref to #/components/schemas/X is replaced by the target schema."""
    owner = {"type": "object", "properties": {"name": {"type": "string"}}}
    spec = {"openapi": "3.0.1",
            "components": {"schemas": {"Owner": owner}},
            "paths": {"/owners": {"post": {"requestBody": {"content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/Owner"}}}},
                "responses": {}}}}}
    return spec, [op("POST", "/owners", body=owner, body_type="application/json")]


@level
def nested_refs():
    """L7: refs inside refs, inside properties and array items. The SAME ref used twice
    side by side (home + work) is NOT a cycle: both must be expanded."""
    spec = {"openapi": "3.0.1",
            "components": {"schemas": {
                "Address": {"type": "object", "properties": {"city": {"type": "string"}}},
                "Phone": {"type": "string", "pattern": "^[0-9]+$"},
                "Owner": {"type": "object", "properties": {
                    "home": {"$ref": "#/components/schemas/Address"},
                    "work": {"$ref": "#/components/schemas/Address"},
                    "phones": {"type": "array", "items": {"$ref": "#/components/schemas/Phone"}}}}}},
            "paths": {"/owners": {"post": {"requestBody": {"content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/Owner"}}}},
                "responses": {}}}}}
    addr = {"type": "object", "properties": {"city": {"type": "string"}}}
    return spec, [op("POST", "/owners", body_type="application/json", body={
        "type": "object", "properties": {
            "home": addr, "work": addr,
            "phones": {"type": "array", "items": {"type": "string", "pattern": "^[0-9]+$"}}}})]


@level
def non_schema_refs():
    """L8: refs are not only schemas. Parameters and requestBodies can be $refs too
    (kafka-rest-proxy does this). Resolve ANY '#/a/b/c' pointer by walking the spec dict."""
    spec = {"openapi": "3.0.1",
            "components": {
                "schemas": {"Id": {"type": "integer", "minimum": 1}},
                "parameters": {"IdParam": {"name": "id", "in": "path", "required": True,
                                           "schema": {"$ref": "#/components/schemas/Id"}}},
                "requestBodies": {"Rename": {"required": True, "content": {
                    "application/json": {"schema": {"type": "string"}}}}}},
            "paths": {"/topics/{id}": {"patch": {
                "parameters": [{"$ref": "#/components/parameters/IdParam"}],
                "requestBody": {"$ref": "#/components/requestBodies/Rename"},
                "responses": {}}}}}
    return spec, [op("PATCH", "/topics/{id}", params=[("id", "path", True, {"type": "integer", "minimum": 1})],
                     body={"type": "string"}, body_type="application/json", body_required=True)]


@level
def cycles():
    """L9: recursive schemas. When you reach a ref you are ALREADY inside of,
    replace it with {"type": "object"} instead of recursing forever."""
    spec = {"openapi": "3.0.1",
            "components": {"schemas": {
                "Node": {"type": "object", "properties": {
                    "name": {"type": "string"},
                    "child": {"$ref": "#/components/schemas/Node"}}},
                # mutual recursion: Pet -> Visit -> Pet
                "Pet": {"type": "object", "properties": {
                    "visits": {"type": "array", "items": {"$ref": "#/components/schemas/Visit"}}}},
                "Visit": {"type": "object", "properties": {
                    "pet": {"$ref": "#/components/schemas/Pet"}}}}},
            "paths": {
                "/nodes": {"post": {"requestBody": {"content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/Node"}}}}, "responses": {}}},
                "/pets": {"post": {"requestBody": {"content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}}}, "responses": {}}},
            }}
    return spec, [
        op("POST", "/nodes", body_type="application/json", body={"type": "object", "properties": {
            "name": {"type": "string"}, "child": {"type": "object"}}}),
        op("POST", "/pets", body_type="application/json", body={"type": "object", "properties": {
            "visits": {"type": "array", "items": {"type": "object", "properties": {
                "pet": {"type": "object"}}}}}}),
    ]


@level
def all_of():
    """L10: allOf (pet-clinic's Pet = PetFields + {id}). Merge into ONE object schema:
    - keys of the outer schema other than allOf are kept (e.g. title)
    - "type": "object"
    - properties = union of all parts' properties (later part wins on same name)
    - required   = union, in order, no duplicates (omit the key if empty)
    Works anywhere, including nested inside properties/items."""
    spec = {"openapi": "3.0.1",
            "components": {"schemas": {
                "PetFields": {"type": "object", "properties": {"name": {"type": "string"}},
                              "required": ["name"]},
                "Pet": {"title": "Pet", "allOf": [
                    {"$ref": "#/components/schemas/PetFields"},
                    {"type": "object", "properties": {"id": {"type": "integer", "readOnly": True}},
                     "required": ["id", "name"]}]}}},
            "paths": {"/pets": {"get": {"responses": {"200": {"content": {"application/json": {
                "schema": {"type": "array", "items": {"$ref": "#/components/schemas/Pet"}}}}}}}}}}
    pet = {"title": "Pet", "type": "object",
           "properties": {"name": {"type": "string"}, "id": {"type": "integer", "readOnly": True}},
           "required": ["name", "id"]}
    return spec, [op("GET", "/pets", responses={"200": {"type": "array", "items": pet}})]


@level
def one_of_any_of():
    """L11: oneOf / anyOf -> just take the FIRST option (resolved). Outer keys are dropped."""
    spec = {"openapi": "3.0.1",
            "components": {"schemas": {"Cat": {"type": "object", "properties": {"meow": {"type": "boolean"}}},
                                       "Dog": {"type": "object"}}},
            "paths": {"/pets": {"post": {
                "parameters": [{"name": "q", "in": "query",
                                "schema": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}],
                "requestBody": {"content": {"application/json": {"schema": {"oneOf": [
                    {"$ref": "#/components/schemas/Cat"}, {"$ref": "#/components/schemas/Dog"}]}}}},
                "responses": {}}}}}
    return spec, [op("POST", "/pets", params=[("q", "query", False, {"type": "string"})],
                     body={"type": "object", "properties": {"meow": {"type": "boolean"}}},
                     body_type="application/json")]


@level
def responses():
    """L12: responses -> {status_code_string: schema}. Same content-type rule as bodies.
    A response with no content maps to {}. Response objects can be $refs too."""
    spec = {"openapi": "3.0.1",
            "components": {
                "schemas": {"Err": {"type": "object", "properties": {"msg": {"type": "string"}}}},
                "responses": {"NotFound": {"description": "nf", "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/Err"}}}}}},
            "paths": {"/x": {"get": {"responses": {
                "200": {"description": "ok", "content": {"*/*": {"schema": {"type": "string"}}}},
                "204": {"description": "empty"},
                "404": {"$ref": "#/components/responses/NotFound"},
                "default": {"description": "err", "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Err"}}}}}}}}}
    err = {"type": "object", "properties": {"msg": {"type": "string"}}}
    return spec, [op("GET", "/x", responses={"200": {"type": "string"}, "204": {}, "404": err, "default": err})]


@level
def server_base_path():
    """L13: if servers[0].url has a path, prefix it to every path (no double slashes).
    'http://h:8080' or 'http://h/' -> no prefix. Relative urls like '/v1' count too. No servers -> no prefix."""
    spec = {"openapi": "3.0.1",
            "servers": [{"url": "http://localhost:8080/api/v2/"}],
            "paths": {"/users": {"get": {"responses": {}}}}}
    return spec, [op("GET", "/api/v2/users")]


# ---------------------------------------------------------------------------
def summarize(ops):
    return [{"op_id": o.key, "method": o.method, "path": o.path,
             "params": [(p.name, p.location, p.required, p.schema) for p in o.parameters],
             "body": o.request_body.schema if o.request_body else None,
             "body_type": o.request_body.content_types[0] if o.request_body else None,
             "body_required": o.request_body.required if o.request_body else False,
             "responses": o.responses} for o in ops]


def run_level(i):
    fn = LEVELS[i - 1]
    spec, expected = fn()
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(spec, f)
    try:
        got = summarize(parse_spec(f.name))
    except RecursionError:
        got = "RecursionError (infinite $ref loop?)"
    except Exception as e:
        got = f"{type(e).__name__}: {e}"
    finally:
        os.unlink(f.name)
    # tuples become lists after a JSON round trip; compare normalized
    norm = lambda x: json.loads(json.dumps(x, sort_keys=True))
    ok = norm(got) == norm(expected)
    print(f"{'PASS' if ok else 'FAIL'}  L{i:<2} {fn.__name__}")
    if not ok:
        print("\n" + fn.__doc__.strip() + "\n")
        print("EXPECTED:\n" + json.dumps(expected, indent=1))
        print("GOT:\n" + (json.dumps(got, indent=1) if not isinstance(got, str) else got))
    return ok


def show(i):
    fn = LEVELS[i - 1]
    spec, expected = fn()
    print(f"L{i} {fn.__name__}\n{fn.__doc__.strip()}\n")
    print("INPUT SPEC:\n" + json.dumps(spec, indent=1))
    print("\nEXPECTED OUTPUT:\n" + json.dumps(expected, indent=1))


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["show"]:
        show(int(args[1]))
    elif args:
        run_level(int(args[0]))
    else:
        for i in range(1, len(LEVELS) + 1):
            if not run_level(i):
                break
        else:
            print("\nAll levels pass. Now run: python3 -m tests.check_parser")
