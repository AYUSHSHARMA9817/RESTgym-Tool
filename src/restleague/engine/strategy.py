"""Owner: B. Main testing loop. STUB."""
import time

from restleague.models import Operation


def run(ops: list[Operation], budget_seconds: float, base_url: str) -> None:
    deadline = time.monotonic() + budget_seconds
    while time.monotonic() < deadline:
        time.sleep(1)
