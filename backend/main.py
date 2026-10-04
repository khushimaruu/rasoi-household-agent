from contextlib import asynccontextmanager
import html
import os
from datetime import date, timedelta
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from db.database import q, one, J, jsonable
from db.seed import HID
from agent import rasoi, llm, state_machine as S
from agent.rasoi import Run
from tools import world, household as th, inventory as ti
from integrations import gnani, notifier


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
    if rule_id:
        await run_in_threadpool(
            notifier.notify, None, household_id, "Got it",
            f"Understood: {interp['summary']}. I'll plan tomorrow's meal with that rule.",
        )
    q("INSERT INTO voice_inputs (household_id,raw_response,transcript,interpretation) VALUES (%s,%s,%s,%s)",
      (household_id, J(stt["raw"]), stt["transcript"], J(interp)))
    return {"raw_gnani_response": stt["raw"], "transcript": stt["transcript"], "interpretation": interp, "rule_id": rule_id}


@app.get("/messages")
def messages(household_id: str = HID):
    rows = q("""SELECT id, run_id, channel, direction, recipient, subject, body, status, created_at
                FROM messages WHERE household_id=%s ORDER BY id DESC LIMIT 30""", (household_id,))
    return jsonable(rows)


def _action_page(content, status_code=200):
    return HTMLResponse(
        "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<body style='font-family:system-ui;max-width:420px;margin:15vh auto;padding:0 16px;text-align:center'>"
        f"{content}</body>",
        status_code=status_code,
    )


@app.get("/act", response_class=HTMLResponse)
def act_page(run: str, a: str, exp: int, sig: str, r: str = ""):
    if not notifier.verify_link(run, a, r, exp, sig):
        return _action_page("<h3>This link is invalid or has expired.</h3>", 400)
    return _action_page(
        f"<h2>Confirm: {html.escape(a.replace('_', ' '))}</h2>"
        "<form method='post' action='/act'>"
        f"<input type='hidden' name='run' value='{html.escape(run, quote=True)}'>"
        f"<input type='hidden' name='a' value='{html.escape(a, quote=True)}'>"
        f"<input type='hidden' name='r' value='{html.escape(r, quote=True)}'>"
        f"<input type='hidden' name='exp' value='{exp}'>"
        f"<input type='hidden' name='sig' value='{html.escape(sig, quote=True)}'>"
        "<button style='background:#14201c;color:#fff;border:0;padding:14px 28px;border-radius:10px;font-size:16px'>"
        "Confirm</button></form>"
    )


@app.post("/act", response_class=HTMLResponse)
def act_do(run: str = Form(...), a: str = Form(...), r: str = Form(""),
           exp: int = Form(...), sig: str = Form(...)):
    if not notifier.verify_link(run, a, r, exp, sig):
        return _action_page("<h3>This link is invalid or has expired.</h3>", 400)
    try:
        rasoi.resolve(run, a, {"rule_id": int(r)} if r else {})
    except ValueError as error:
        return _action_page(f"<h3>Already handled.</h3><p>{html.escape(str(error))}</p>", 409)
    return _action_page("<h2>Done.</h2><p>Rasoi is continuing.</p>")
