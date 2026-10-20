"""AsyncSequenceExecutor: run a plan, passing real ids from each step to the next.

Milestone 2 of docs/architecture/track_a_stateful_fuzzing.md §4.2 ("Context
Passing"). The executor is deliberately thin: it owns the loop and the
request/response -> RelationalKnowledgeGraph harvesting, and delegates request
construction to the spec component's ``generate_request`` and HTTP to
``engine/client.py``'s ApiClient. Nothing here is wired into
engine/strategy.py.

The binding rule: before sending step k, ask the RKG for a consistent set of
path parameters (``query_path_bindings``). Those ids come from entities created
by steps 1..k-1, and they are guaranteed to be *related* to each other -- the
pet belongs to the owner -- which is the whole point of the graph.
"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict
import logging
import random
import re
from typing import Any, Callable, Optional

from ..engine.mutations import mutate as payload_mutate
from ..engine.pool import resource_for_param, resource_of
from ..models import Mode, Operation, Request, Response, Sequence, Step

log = logging.getLogger("restleague.sequence")

_PARAM_RE = re.compile(r"\{([^}]+)\}")


def _extract_id(resp: Response, req: Request, op: Operation) -> Any:
    """The id of the entity this response is about, or None.

    Order: an "id"-ish field in the JSON body, then the Location header's last
    segment, then the path parameter naming this operation's own resource
    (which covers GET/PUT/PATCH on an existing entity).
    """
    body = resp.json
    if isinstance(body, dict):
        for k in ("id", "Id", "ID", "_id", "uuid", "key", "code"):
            v = body.get(k)
            if v is not None and not isinstance(v, (dict, list)):
                return v
        res = resource_of(op.path)
        if res:
            for k, v in body.items():
                if k.lower() in (res + "id", res + "_id") and not isinstance(v, (dict, list)):
                    return v

    loc = resp.headers.get("location") or resp.headers.get("Location")
    if loc:
        last = loc.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
        if last:
            return int(last) if last.isdigit() else last

    params = _PARAM_RE.findall(op.path)
    if params:
        own = resource_of(op.path)
        last = params[-1]
        if own and resource_for_param(op.path, last) == own:
            return req.path_params.get(last)
    return None


def _parent_of(req: Request, op: Operation, res_type: Optional[str]) -> Optional[tuple]:
    """The (type, id) of the entity that owns the one this operation acts on.

    For "/owners/{ownerId}/pets" the parent is the owner; for
    "/owners/{ownerId}/pets/{petId}" it is still the owner, since the last
    parameter names the pet itself.
    """
    params = [p for p in _PARAM_RE.findall(op.path) if p in req.path_params]
    for name in reversed(params):
        ptype = resource_for_param(op.path, name)
        if ptype and ptype != res_type:
            return (ptype, req.path_params[name])
    return None


def harvest_to_graph(rkg, op: Operation, req: Request, resp: Response) -> None:
    """Update the RKG from one request/response pair. Only 2XX responses count."""
    if not resp.ok:
        return
    res_type = resource_of(op.path)
    if not res_type:
        return
    method = op.method.upper()

    if method == "DELETE":
        params = _PARAM_RE.findall(op.path)
        if params:
            last = params[-1]
            if last in req.path_params:
                rkg.cascade_delete(res_type, req.path_params[last])
        return

    entity_id = _extract_id(resp, req, op)
    if entity_id is None:
        return
    attrs = resp.json if isinstance(resp.json, dict) else {}
    from ..findings import RequestRecord
    rkg.add_entity(res_type, entity_id, attrs,
                   parent=_parent_of(req, op, res_type), created_by_op=op.key,
                   created_by_request=asdict(RequestRecord.of(req)))


class AsyncSequenceExecutor:
    """Executes a plan of Operations as one connected Sequence."""

    def __init__(self, client, generate_request: Callable[..., Request], pool=None,
                 seed: Optional[int] = None, planner=None):
        self.client = client                      # engine.client.ApiClient
        self.generate_request = generate_request  # spec.values.generate_request
        self.pool = pool                          # optional engine.pool.ResourcePool
        self.rng = random.Random(seed)            # used by the payload operators
        # Optional, off by default. When set (LLM_PLANNER=1) it supplies value
        # hints for fields a schema-only generator cannot satisfy, applied to
        # VALID requests only. None -> exact spec-only behaviour (no AI).
        self.planner = planner

    async def execute(self, plan, rkg, *, mode: Mode = Mode.VALID,
                      target_mode: Optional[Mode] = None, stop_on_prefix_failure: bool = True,
                      dsmi=None, on_step: Optional[Callable] = None,
                      attack: Optional[str] = None) -> Sequence:
        """Run every operation in ``plan`` in order, binding ids as it goes.

        ``plan`` is a list of Operations, or a ``PlanMutation`` from
        sequence/mutator.py -- in which case its execution flags are honoured:
        ``cross_entity`` binds the final step to another parent's child, and
        ``duplicate_last`` fires the final step twice concurrently.

        Setup steps always run in ``mode`` (normally VALID -- they exist only to
        build state). The final step runs in ``target_mode`` if given, which is
        how the sequence mutator fuzzes a target while keeping its prefix valid.

        A ``stop_on_prefix_failure`` of False is what the ``omit_prerequisite``
        mutation needs: the whole point there is to reach the target with a
        broken prefix.

        ``on_step`` is awaited after each step, as ``on_step(step, op, is_last)``.
        Oracles hook in here rather than inspecting the finished sequence: a
        later step can change the state an oracle is about to re-read, which
        would make the oracle blame the wrong step.
        """
        ops, mutation = _unpack(plan)
        seq = Sequence(target_op_key=ops[-1].key if ops else "")
        for i, op in enumerate(ops):
            is_target = (i == len(ops) - 1)
            step_mode = target_mode if (is_target and target_mode is not None) else mode
            cross = bool(is_target and mutation is not None and mutation.cross_entity)
            req = self.build_request(op, rkg, step_mode, cross_entity=cross)
            if is_target and attack and not cross:
                # Attack only the final step: the prefix must stay valid or the
                # chain never reaches the endpoint we went to this trouble to
                # set up. Skipped when cross_entity is in play, so a 5XX is not
                # confounded between the payload and the authorization probe.
                req = self._apply_attack(attack, req, op)

            if is_target and mutation is not None and mutation.duplicate_last:
                # race probe: two identical in-flight copies of the same action.
                # Both are tagged "double_action" so the content oracles skip
                # them and the race oracle recognises the pair; the second step
                # carries the first's status in its bindings so the oracle can
                # judge both outcomes from the one step it is handed.
                req.mutation = "double_action"
                twin = copy.deepcopy(req)
                first, second = await asyncio.gather(self.client.send(req), self.client.send(twin))
                for n, (r, resp) in enumerate(((req, first), (twin, second))):
                    step = Step(request=r, response=resp, bindings=dict(r.path_params))
                    if n == 1:
                        step.bindings["_twin_status"] = first.status
                    seq.append(step)
                    self._harvest(rkg, op, r, resp, dsmi)
                    if on_step is not None:
                        await on_step(step, op, n == 1)
                break

            resp = await self.client.send(req)
            step = Step(request=req, response=resp, bindings=dict(req.path_params))
            seq.append(step)
            self._harvest(rkg, op, req, resp, dsmi)
            if on_step is not None:
                await on_step(step, op, is_target)
            if stop_on_prefix_failure and not is_target and not resp.ok:
                log.debug("prefix step %s failed (%s); abandoning sequence", op.key, resp.status)
                break
        return seq

    def build_request(self, op: Operation, rkg, mode: Mode = Mode.VALID, *,
                      cross_entity: bool = False) -> Request:
        """Generate a request, then overwrite its path params with related ids."""
        req = self.generate_request(op, self.pool, mode)
        req.op_key = op.key
        if self.planner is not None and mode == Mode.VALID:
            self._apply_hints(op, req)
        bindings = None
        try:
            if cross_entity:
                bindings = rkg.query_cross_entity_bindings(op.path)
                if bindings:
                    req.mutation = "cross_entity"
            if bindings is None:
                bindings = rkg.query_path_bindings(op.path, op)
        except Exception as e:
            log.debug("path binding failed for %s: %s", op.key, e)
            bindings = None
        if bindings:
            req.path_params.update(bindings)
        return req

    def _apply_hints(self, op: Operation, req: Request) -> None:
        """Overlay the planner's value hints onto a freshly generated VALID
        request. Purely additive: the hints only change which concrete values go
        out to reach a state; the oracles judge the result either way, so a bad
        hint costs reachability, never soundness. Applied to whichever of body /
        form / query the request carries the field in; unknown fields are added
        to the body (the planner is told to use declared fields only)."""
        try:
            hints = self.planner.hints(op)
        except Exception as e:
            log.debug("planner hints failed for %s: %s", op.key, e)
            return
        if not hints:
            return
        for key, value in hints.items():
            placed = False
            for holder in (req.body, req.form, req.query):
                if isinstance(holder, dict) and key in holder:
                    holder[key] = value
                    placed = True
            if not placed and isinstance(req.body, dict):
                req.body[key] = value

    def _apply_attack(self, name: str, req: Request, op: Operation) -> Request:
        """Run one of engine/mutations.py's payload operators over the request.

        This is the point of building a valid prefix: a deep endpoint such as
        POST /owners/{id}/pets/{id}/visits is reachable only once real related
        ids exist, and reaching it is worth little if we then send a
        well-behaved body. The engine's existing operators (injection,
        boundary values, type confusion, malformed bodies) are reused verbatim
        rather than reimplemented.
        """
        try:
            mutated = payload_mutate(name, req, op, self.pool, self.rng)
        except Exception as e:
            log.debug("payload mutation %s failed on %s: %s", name, op.key, e)
            return req
        if mutated is None:
            return req                      # operator did not apply to this request
        mutated.op_key = op.key
        mutated.path_params.update(req.path_params)   # keep the ids that made it reachable
        return mutated

    def _harvest(self, rkg, op: Operation, req: Request, resp: Response, dsmi=None) -> None:
        """Update the graph and, when present, the state-machine inferrer."""
        try:
            harvest_to_graph(rkg, op, req, resp)
        except Exception as e:                    # harvesting must never kill a run
            log.debug("graph harvest failed for %s: %s", op.key, e)
        if dsmi is None:
            return
        try:
            res_type = resource_of(op.path)
            entity_id = _extract_id(resp, req, op)
            if res_type and entity_id is not None:
                dsmi.observe(op, req, resp, entity=(res_type, entity_id))
        except Exception as e:
            log.debug("state inference failed for %s: %s", op.key, e)


def _unpack(plan) -> tuple[list[Operation], Any]:
    """Accept either a plain list of Operations or a PlanMutation."""
    ops = getattr(plan, "ops", None)
    return (list(ops), plan) if ops is not None else (list(plan), None)
