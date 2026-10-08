"""Entry point. Reads RESTgym env vars, wires the spec parser and the engine."""
import os

from restleague.spec.parser import parse_spec
from restleague.spec.deps import order_operations
from restleague.engine.strategy import run


def main() -> None:
    host = os.environ.get("HOST", "localhost")
    port = os.environ.get("PORT", "8080")
    budget_min = float(os.environ.get("TIME_BUDGET", "60"))
    spec_path = os.environ.get("SPEC", f"/specifications/{os.environ.get('API', 'api')}-openapi.json")

    ops = order_operations(parse_spec(spec_path))
    run(ops, budget_seconds=budget_min * 60, base_url=f"http://{host}:{port}")


if __name__ == "__main__":
    main()
