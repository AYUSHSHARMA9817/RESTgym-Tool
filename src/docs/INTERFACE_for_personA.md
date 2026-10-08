# Person A ↔ Person B interface (one page)

Person B's engine is done and runs today on a built-in fallback. When you drop
your `restleague/spec/` package in, the engine uses it automatically — **no
changes to B's code**. Just match these three signatures and the `models.py`
types.

## What B calls (put these in `restleague/spec/`)

```python
# restleague/spec/parser.py
def parse_spec(path: str) -> list[Operation]: ...

# restleague/spec/deps.py
def order_operations(ops: list[Operation]) -> list[Operation]: ...   # creators first, DELETE last

# restleague/spec/values.py
def generate_request(op: Operation, pool, mode: Mode) -> Request: ...
```

The loader tries `from restleague.spec import parser, values` and
`from restleague.spec.deps import order_operations`; if that import fails it uses
`engine/fallback.py`. So partial is fine — ship `parser.py` first and the rest
keeps using the fallback.

## The one thing that matters most: reuse real ids

Inside `generate_request`, for every **path** and **query** param, and every
id-like body field, call the pool first and use its value when non-None:

```python
v = pool.lookup(param_name, schema, op)      # returns a harvested real id, or None
if v is None:
    v = your_value_generator(schema, param_name, mode)
```

`pool.lookup` returns ids/objects B harvested from earlier 2XX responses (bodies,
`Location` headers, accepted request bodies). This is what lets the tool reach
nested routes like `POST /owners/{ownerId}/pets` — without it, path ids are
random guesses and those endpoints stay at 404. `pool` also offers
`pool.lookup_object(resource)` (a whole harvested object) and `pool.any_value()`.

## `mode` (from `models.Mode`)
- `VALID` – aim for 2XX (examples/defaults/enums, then formats, then name heuristics).
- `BOUNDARY` – min-1 / max+1 / empty / huge.
- `INVALID` – wrong types, missing required, garbage.

B drives `VALID` in the nominal phase and `BOUNDARY`/`INVALID` plus its own
mutations in the fuzz phase, so your value quality in `VALID` mode is what moves
coverage most.

## Types you must produce (see `models.py`)
`Operation(method, path, parameters=[Parameter...], request_body=RequestBody?,
responses={...})`, with `path` including the base path, schemas fully
`$ref`-resolved. `Request(method, path, path_params, query, headers, cookies,
form, body, content_type, op_key)`.

## How to check your parser against B's engine
```bash
python -m pytest tests/test_engine.py -q     # B's suite still passes
python tests/run_e2e.py 60                    # end-to-end vs the buggy mock
```
If `parse_spec` works on every spec in `RESTgym/apis/*/specifications/` without
crashing and `generate_request` calls `pool.lookup`, you're done — coverage and
fault numbers just go up relative to the fallback.
