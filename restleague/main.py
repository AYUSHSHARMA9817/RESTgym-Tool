"""Entry point. Reads RESTgym env vars, wires the spec parser and the engine.

By default this runs the competition pipeline (engine.strategy.run), which is
the one validated against RESTgym. Two opt-in modes exist for experiments (see
docs/architecture/track_a_stateful_fuzzing.md); neither changes the default:

    TRACK_A=1   stateful sequence fuzzing only
    HYBRID=1    the default engine and Track A together, sharing state
"""
import logging
import os

from restleague.spec.parser import parse_spec
from restleague.spec.deps import order_operations
from restleague.engine.strategy import run
from restleague import hybrid, track_a


def main() -> None:
    host = os.environ.get("HOST", "localhost")
    port = os.environ.get("PORT", "8080")
    budget_min = float(os.environ.get("TIME_BUDGET", "60"))
    spec_path = os.environ.get("SPEC", f"/specifications/{os.environ.get('API', 'api')}-openapi.json")

    ops = order_operations(parse_spec(spec_path))
    base_url = f"http://{host}:{port}"

    if hybrid.enabled() or track_a.enabled():
        logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                            format="%(asctime)s %(levelname)s %(message)s")
        runner = hybrid.run_hybrid if hybrid.enabled() else track_a.run_track_a
        result = runner(ops, budget_seconds=budget_min * 60, base_url=base_url)
        print(result.describe())
        return

    run(ops, budget_seconds=budget_min * 60, base_url=base_url)


if __name__ == "__main__":
    main()
