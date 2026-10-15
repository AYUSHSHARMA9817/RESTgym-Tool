"""Schema conformance checking for the offline evaluation.

The implementation moved into the tool package (restleague/oracles/schema.py)
when the Track A conformance oracle started needing it at runtime; this module
re-exports it so `from eval.conformance import violations` keeps working and
there is only one validator to maintain.
"""
from restleague.oracles.schema import violations  # noqa: F401
