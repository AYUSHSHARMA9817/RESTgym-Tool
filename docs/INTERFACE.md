# Spec ↔ Engine interface (one page)

The engine imports the spec component and falls back to a built-in
parser/generator if it is missing, so the two parts stay decoupled. To plug in,
match these signatures and the `models.py` types.

## Functions (in `restleague/spec/`)

```python
# parser.py
def parse_spec(path: str) -> list[Operation]: ...

# deps.py
def order_operations(ops: list[Operation]) -> list[Operation]: ...   # creators first, DELETE last

# values.py
def generate_request(op: Operation, pool, mode: Mode) -> Request: ...
```

The engine imports `from restleague.spec import parser, values` and
`from restleague.spec.deps import order_operations`.

## Reuse real ids (the key coupling)

Inside `generate_request`, for every path and query parameter and every id-like
body field, call the pool first and use its value when it is not None:

```python
v = pool.lookup(param_name, schema, op)      # a harvested real id, or None
if v is None:
    v = generate_your_own_value(schema, param_name, mode)
```

`pool.lookup` returns ids/objects harvested from earlier 2XX responses. This is
what lets nested routes like `POST /owners/{ownerId}/pets` be reached instead of
staying at 404. The pool also offers `pool.lookup_object(resource)` and
`pool.any_value()`.

## Modes (from `models.Mode`)
- `VALID` – aim for 2XX (examples/defaults/enums, then formats, then name heuristics).
- `BOUNDARY` – min-1 / max+1 / empty / huge.
- `INVALID` – wrong types, missing required, garbage.

## Checking it
`python -m pytest tests/test_engine.py -q` and `python tests/run_e2e.py 60`
should both stay green; coverage and fault numbers rise relative to the fallback.
