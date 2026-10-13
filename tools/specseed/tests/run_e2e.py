"""End-to-end: serve the buggy API in a thread, run the engine against it."""
import sys, threading, time, socket
sys.path.insert(0, ".")
import uvicorn
from tests.buggy_api import app
from restleague.engine import fallback
from restleague.engine.strategy import run

PORT = 8111
cfg = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="critical")
server = uvicorn.Server(cfg)
threading.Thread(target=server.run, daemon=True).start()

# wait for it to accept connections
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", PORT), timeout=0.5).close()
        break
    except OSError:
        time.sleep(0.1)

budget = int(sys.argv[1]) if len(sys.argv) > 1 else 30
ops = fallback.order_operations(fallback.parse_spec("tests/buggy_openapi.json"))
res = run(ops, budget_seconds=budget, base_url=f"http://127.0.0.1:{PORT}", workers=6, seed=7,
          nominal_fraction=0.25, fault_log="/tmp/claude-0/faults.jsonl")
server.should_exit = True

print("unique 5xx     :", res.unique_5xx)
print("ops covered    :", res.operations_covered, "/", res.operations_total)
print("uncovered      :", res.uncovered)
print("requests       :", res.total_requests, "(", round(res.total_requests / res.elapsed), "req/s)")
print("status hist    :", res.status_histogram)
print("faults:")
for f in res.faults["faults"]:
    print(f"  {f['status']} {f['op']:28s} via {str(f['mutation']):18s} t={f['t']}s :: {f['msg'][:60]}")
