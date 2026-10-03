from contextlib import asynccontextmanager
import os
from datetime import date, timedelta
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from db.database import q, one, J, jsonable
from db.seed import HID
from agent import rasoi, llm, state_machine as S
from agent.rasoi import Run
from tools import world, household as th, inventory as ti
from integrations import gnani


@asynccontextmanager
async def lifespan(app):
    try:
        rasoi.recover_orphans()  # crash recovery on boot
    except Exception as e:
        print("recover_orphans skipped:", e)
    yield


app = FastAPI(title="Rasoi", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])


class RunReq(BaseModel):
    household_id: str = HID


class ResolveReq(BaseModel):
    action: str
    params: dict = {}


@app.get("/health")
def health():
    return {"ok": True, "db": one("SELECT 1 AS x")["x"] == 1}


@app.get("/household/{hid}")
def household(hid: str):
    plan = date.today() + timedelta(days=int(os.getenv('PLAN_DAYS_AHEAD', '1')))
    ctx = th.get_household_context(hid, plan)
    inv = ti.get_inventory(hid, plan)
    return {"household": ctx, "inventory": inv, "scenarios": world.SCENARIOS}


@app.post("/simulate/{kind}")
def simulate(kind: str, req: RunReq = RunReq()):
    try:
        world.apply_scenario(kind, req.household_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "scenario": kind}


@app.post("/agent/run")
def run_agent(req: RunReq = RunReq()):
    return {"run_id": rasoi.start_run(req.household_id)}


def _view(run_id):
    r = Run.load(run_id)
    if not r:
        raise HTTPException(404, "run not found")
    events = q("SELECT id, ts, state, action, reason, result, next_step FROM agent_events WHERE run_id=%s ORDER BY id", (run_id,))
    order = one("SELECT * FROM orders WHERE run_id=%s ORDER BY id DESC LIMIT 1", (run_id,))
    c = r.ctx
    return jsonable({
        "run_id": r.run_id, "state": r.state, "last_verified_state": r.last_verified, "status": r.status,
        "step_index": S.step_index(r.state), "pipeline": S.PIPELINE,
        "plan_date": c.get("plan_date"), "meal": c.get("meal"), "shopping_list": c.get("shopping_list"),
        "substitutions": c.get("substitutions"), "human_request": c.get("human_request"),
        "order": order, "events": events,
    })


@app.get("/agent/runs/latest")
def latest(household_id: str = HID):
    row = one("SELECT run_id FROM agent_runs WHERE household_id=%s ORDER BY created_at DESC LIMIT 1", (household_id,))
    return _view(row["run_id"]) if row else None


@app.get("/agent/runs/{run_id}")
def get_run(run_id: str):
    return _view(run_id)


@app.post("/agent/resume/{run_id}")
def resume(run_id: str):
    try:
        return {"resumed": rasoi.resume_run(run_id)}
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/agent/{run_id}/resolve")
def resolve(run_id: str, req: ResolveReq):
    try:
        rasoi.resolve(run_id, req.action, req.params)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/voice")
async def voice(household_id: str = Form(HID), language: str = Form("hi-IN"),
                text_fallback: str = Form(None), audio: UploadFile = File(None)):
    data = await audio.read() if audio else None
    try:
        stt = await gnani.transcribe(data, audio.filename if audio else "", language, text_fallback)
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from e
    known = [r["ingredient"] for r in q("SELECT ingredient FROM catalog")]
    interp = llm.interpret_voice(stt["transcript"], known)
    rule_id = th.add_voice_rule(household_id, interp)
    q("INSERT INTO voice_inputs (household_id,raw_response,transcript,interpretation) VALUES (%s,%s,%s,%s)",
      (household_id, J(stt["raw"]), stt["transcript"], J(interp)))
    return {"raw_gnani_response": stt["raw"], "transcript": stt["transcript"], "interpretation": interp, "rule_id": rule_id}
