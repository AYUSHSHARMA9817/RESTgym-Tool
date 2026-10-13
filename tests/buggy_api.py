"""A small FastAPI app with intentionally injected bugs, for engine testing.

Mirrors the shape of the competition APIs: resource creation, path-id lookups,
validation, and a handful of distinct crash sites reachable only by specific
bad inputs. Run: uvicorn tests.buggy_api:app --port 8099
"""
from __future__ import annotations

import traceback

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Buggy Clinic", version="1.0.0")


@app.exception_handler(Exception)
async def leak(_: Request, exc: Exception):
    # mimic a debug-mode API leaking the exception class + message (common in the benchmark)
    tb = traceback.extract_tb(exc.__traceback__)
    where = tb[-1].name if tb else "?"
    return JSONResponse(status_code=500,
                        content={"error": type(exc).__name__, "message": str(exc), "at": where})

_owners: dict[int, dict] = {}
_pets: dict[int, dict] = {}
_seq = {"owner": 0, "pet": 0}


class Owner(BaseModel):
    firstName: str = Field(min_length=1, max_length=30)
    lastName: str = Field(min_length=1, max_length=30)
    age: int = Field(ge=0, le=150)
    email: str | None = None


class Pet(BaseModel):
    name: str = Field(min_length=1, max_length=30)
    type: str = Field(pattern="^(cat|dog|bird)$")
    weight: float = 1.0


@app.get("/owners")
def list_owners(limit: int = Query(10, ge=0), q: str | None = None):
    # BUG 6 (injection point): naive query handling crashes on a single quote,
    # the way a string-concatenated SQL filter would.
    if q is not None and "'" in q:
        raise RuntimeError("unterminated string in query filter: " + q)
    data = list(_owners.values())
    if q:
        data = [o for o in data if q.lower() in o["firstName"].lower()]
    return data[:limit]


@app.post("/owners", status_code=201)
def create_owner(owner: Owner):
    _seq["owner"] += 1
    oid = _seq["owner"]
    rec = {"id": oid, **owner.model_dump()}
    # BUG 1: certain ages hit an unhandled division (distinct 500)
    if owner.age == 42:
        _ = 1 / 0  # ZeroDivisionError
    # BUG 2: a last name of a specific length indexes out of range
    if len(owner.lastName) == 7:
        _ = [][len(owner.lastName)]  # IndexError
    _owners[oid] = rec
    return rec


@app.get("/owners/{owner_id}")
def get_owner(owner_id: int):
    # BUG 3: negative id path triggers a key error on an internal cache (checked first)
    if owner_id < 0:
        raise KeyError("cache miss for negative id")
    if owner_id not in _owners:
        raise HTTPException(404, "owner not found")
    return _owners[owner_id]


@app.put("/owners/{owner_id}")
def update_owner(owner_id: int, owner: Owner):
    if owner_id not in _owners:
        raise HTTPException(404, "owner not found")
    # BUG 4: email containing a '%' breaks an internal format call
    if owner.email and "%" in owner.email:
        _ = "hello %d" % owner.email  # TypeError
    _owners[owner_id] = {"id": owner_id, **owner.model_dump()}
    return _owners[owner_id]


@app.delete("/owners/{owner_id}", status_code=204)
def delete_owner(owner_id: int):
    if owner_id not in _owners:
        raise HTTPException(404, "owner not found")
    del _owners[owner_id]
    return Response(status_code=204)


@app.post("/owners/{owner_id}/pets", status_code=201)
def create_pet(owner_id: int, pet: Pet):
    if owner_id not in _owners:
        raise HTTPException(404, "owner not found")
    _seq["pet"] += 1
    pid = _seq["pet"]
    # BUG 5: huge weight overflows an int cast
    if pet.weight > 1e18:
        _ = int("x" * 2)  # ValueError
    rec = {"id": pid, "ownerId": owner_id, **pet.model_dump()}
    _pets[pid] = rec
    return rec


@app.get("/pets/{pet_id}")
def get_pet(pet_id: int):
    if pet_id not in _pets:
        raise HTTPException(404, "pet not found")
    return _pets[pet_id]
