"""Rasoi: explicit state machine + LLM judgement + deterministic guard-rails + DB checkpoints.

Truth comes from the database and external API responses, never from the LLM.
Every consequential action is preceded by a checkpoint, so a crashed run can resume without double-paying.
"""
import os, time, uuid, threading
from datetime import date, timedelta
from db.database import q, one, J, jsonable, transaction
from agent import state_machine as S, llm
from tools import household as th, inventory as ti, meals as tm, shopping as ts
from tools import payment as tp, delivery as td, learning as tl, world
from integrations import notifier

STEP_DELAY = float(os.getenv("STEP_DELAY", "0.7"))
MAX_REPLANS = 2


class SimulatedCrash(Exception):
    pass


class Run:
    def __init__(self, row):
        self.run_id, self.household_id = row["run_id"], row["household_id"]
        self.state, self.last_verified = row["current_state"], row["last_verified_state"]
        self.status, self.ctx = row["status"], row["context"] or {}

    @classmethod
    def create(cls, hid):
        rid = uuid.uuid4().hex[:8]
        plan = (date.today() + timedelta(days=int(os.getenv("PLAN_DAYS_AHEAD", "1")))).isoformat()
        q("INSERT INTO agent_runs (run_id,household_id,current_state,status,context) VALUES (%s,%s,%s,'RUNNING',%s)",
          (rid, hid, S.START, J({"plan_date": plan})))
        return cls.load(rid)

    @classmethod
    def load(cls, rid):
        row = one("SELECT * FROM agent_runs WHERE run_id=%s", (rid,))
        return cls(row) if row else None

    def save(self, conn=None):
        q("""UPDATE agent_runs SET current_state=%s, last_verified_state=%s, status=%s, context=%s, updated_at=now()
             WHERE run_id=%s""", (self.state, self.last_verified, self.status, J(self.ctx), self.run_id), conn=conn)

    def goto(self, state):
        self.state = state; self.save()

    def verified(self, name):
        self.last_verified = name; self.save()

    def log(self, action, reason, result=None, next_step=None):
        q("INSERT INTO agent_events (run_id,state,action,reason,result,next_step) VALUES (%s,%s,%s,%s,%s,%s)",
          (self.run_id, self.state, action, reason, J(jsonable(result)) if result is not None else None, next_step))

    def need_human(self, state, message, options):
        self.ctx["human_request"] = {"kind": state, "message": message, "options": options}
        self.log("ask_human", message, {"options": [o["label"] for o in options]}, "Waiting for a human decision")
        notifier.notify(self.run_id, self.household_id, "Rasoi needs your decision", message, options)
        return state

    @property
    def plan_date(self):
        return date.fromisoformat(self.ctx["plan_date"])


CANCEL = {"action": "cancel", "label": "Cancel today's plan"}

# ---------------------------------------------------------------- handlers: each returns the next state

def h_start(r):
    r.log("start", "Planning the next meal for the household", {"plan_date": r.ctx["plan_date"]}, "Understand household")
    return S.UNDERSTAND


def h_understand(r):
    hh = th.get_household_context(r.household_id, r.plan_date)
    r.ctx["household"] = hh
    r.log("get_household_context",
          f"{hh['member_count']} members, {hh['diet'] or 'no diet rule'}, ₹{hh['budget']:.0f}/day, goal: {hh['nutrition_goal']}",
          {"constraints": hh["constraint_labels"], "recent_meals": [m["name"] for m in hh["recent_meals"]]},
          "Check pantry")
    return S.INVENTORY


def h_inventory(r):
    inv = ti.get_inventory(r.household_id, r.plan_date)
    r.ctx["inventory"] = inv["items"]
    if inv["stale"]:
        r.log("get_inventory", f"Pantry last updated {inv['hours_since_update']}h ago; too old to plan from safely")
        return r.need_human(S.STALE_INVENTORY,
                            "My pantry data is stale, so I can't trust it. Please confirm the pantry is accurate.",
                            [{"action": "confirm_inventory", "label": "Pantry is accurate"}, CANCEL])
    soon = [f"{i['ingredient']} ({i['expires_in_days']}d)" for i in inv["items"]
            if i["expires_in_days"] is not None and 0 <= i["expires_in_days"] <= 2]
    r.log("get_inventory", f"{len(inv['items'])} pantry items" + (f"; expiring soon: {', '.join(soon)}" if soon else ""),
          {"expiring_soon": soon}, "Select a meal")
    return S.SELECT_MEAL


def h_select(r):
    hh, ctx = r.ctx["household"], r.ctx
    ranked = tm.rank_meals(tm.find_meals(), hh["constraints"], ctx["inventory"], hh["recent_meals"],
                           hh["preferences"], hh["nutrition_goal"], set(ctx.get("rejected_meals", [])),
                           ctx.get("pantry_only", False))
    cands = ranked["candidates"]
    if not cands:
        relax = [{"action": "relax_rule", "rule_id": a["id"], "label": f"Relax: {a['label']}"}
                 for a in hh["active_rules"] if a["relaxable"]]
        lines = "; ".join(hh["constraint_labels"])
        msg = ("I can't cook anything from the pantry right now." if ctx.get("pantry_only") else
               f"I couldn't find a meal that satisfies: {lines}. Which constraint should I relax?")
        r.log("find_meals", "No meal passes the hard constraints", {"excluded": ranked["excluded"]})
        return r.need_human(S.CONSTRAINT_CONFLICT, msg, relax + [CANCEL])
    pick = llm.choose_meal({"goal": hh["nutrition_goal"], "preferences": hh["preferences"],
                            "recent": [m["name"] for m in hh["recent_meals"]]}, cands)
    chosen = next((c for c in cands if pick and c["id"] == pick["meal_id"]), cands[0])
    ctx["meal"] = chosen
    why = (pick or {}).get("reason") or "Highest score among meals that pass every hard constraint"
    r.log("select_meal", f"{chosen['name']}: {why}",
          {"reasons": chosen["reasons"], "llm_used": pick is not None,
           "ranked": [{"name": c["name"], "score": c["score"]} for c in cands[:4]],
           "excluded": ranked["excluded"]},
          "Check missing ingredients")
    return S.MISSING


def h_missing(r):
    ctx = r.ctx; meal = ctx["meal"]
    avail = tm.availability(ctx["inventory"])
    missing = tm.missing_for({"ingredients": meal["ingredients"]}, avail)
    if not missing:
        ctx["shopping_list"] = {"items": [], "estimated_total": 0}; ctx["substitutions"] = []
        r.log("check_missing", f"Everything for {meal['name']} is already in the pantry. No order needed.", None, "Update pantry")
        return S.UPDATE_INVENTORY
    plan = ts.plan_shopping(missing, ctx["household"]["constraints"]["substitutions"], ts.get_catalog(), avail)
    if plan["unresolved"]:
        ctx.setdefault("rejected_meals", []).append(meal["id"])
        n = ctx["replans"] = ctx.get("replans", 0) + 1
        r.log("replan", f"{', '.join(plan['unresolved'])} is out of stock and has no approved substitute; dropping {meal['name']}",
              {"unresolved": plan["unresolved"], "replan": n}, "Select another meal" if n <= MAX_REPLANS else "Ask a human")
        if n <= MAX_REPLANS:
            return S.SELECT_MEAL
        return r.need_human(S.NEEDS_HUMAN, "Too many meals are blocked by unavailable ingredients. Please choose how to proceed.",
                            [{"action": "confirm_inventory", "label": "Re-check pantry"}, CANCEL])
    ctx["shopping_list"], ctx["substitutions"] = plan["shopping_list"], plan["substitutions"]
    items = plan["shopping_list"]["items"]
    subs = plan["substitutions"]
    sub_txt = "; ".join(f"{s['from']} → {s['to']} ({s['source']})" for s in subs)
    r.log("create_shopping_list", f"{len(items)} items, ₹{plan['shopping_list']['estimated_total']:.0f}" + (f". Substitutions: {sub_txt}" if subs else ""),
          {"shopping_list": plan["shopping_list"], "substitutions": subs},
          "Check budget" if items else "Update pantry")
    return S.BUDGET if items else S.UPDATE_INVENTORY


def h_budget(r):
    ctx = r.ctx
    total = ctx["shopping_list"]["estimated_total"]
    budget = ctx.get("overrides", {}).get("budget", ctx["household"]["budget"])
    if total > budget:
        r.log("check_budget", f"₹{total:.0f} exceeds the ₹{budget:.0f} daily budget", {"total": total, "budget": budget})
        return r.need_human(S.BUDGET_EXCEEDED, f"The order is ₹{total:.0f} but today's budget is ₹{budget:.0f}.",
                            [{"action": "raise_budget", "amount": total, "label": f"Approve ₹{total:.0f} today"},
                             {"action": "reject_meal", "label": "Pick a cheaper meal"}, CANCEL])
    r.log("check_budget", f"₹{total:.0f} is within the ₹{budget:.0f} budget", {"total": total, "budget": budget}, "Create order")
    return S.CREATE_ORDER


def h_create_order(r):
    ctx = r.ctx
    if not ctx.get("order_id"):
        ctx["order_id"] = tp.create_order_row(r.run_id, r.household_id, ctx["shopping_list"]["items"],
                                              ctx["shopping_list"]["estimated_total"])
    r.save(); r.verified("ORDER_CREATED")
    r.log("create_order", f"Order #{ctx['order_id']} recorded (not paid yet)", None, "Pay")
    return S.PAYMENT

PAY_WAIT_SECONDS = int(os.getenv("PAY_WAIT_SECONDS", "90"))


def _pay_link(raw):
    d = raw.get("data", raw) if isinstance(raw, dict) else {}
    return next((d[k] for k in ("redirect_url", "payment_url", "checkout_url") if d.get(k)), None)


def h_payment(r):
    """Handles both PAYMENT and PAYMENT_PENDING so a resumed run lands here safely."""
    ctx = r.ctx
    pay = ctx.setdefault("payment", {"attempt": 1})
    total = ctx["shopping_list"]["estimated_total"]
    if not pay.get("external_id"):
        pay["idempotency_key"] = f"{r.run_id}-pay-{pay['attempt']}"
        r.goto(S.PAYMENT_PENDING)  # checkpoint BEFORE the consequential call
        r.log("create_payment", f"Checkpointed. Sending ₹{total:.0f} payment (idempotency key {pay['idempotency_key']})")
        res = tp.create_payment(r.household_id, ctx["order_id"], total, pay["idempotency_key"])
        pay["external_id"] = res["external_id"]
        pay["link"] = _pay_link(res["raw"])
        r.save()
        r.log("create_payment", f"Provider returned id {res['external_id']}. I won't trust this yet; verifying with the provider.",
              res["raw"], "Verify payment")
        if world.pop_event(r.household_id, "CRASH_AFTER_PAYMENT"):
            raise SimulatedCrash()
    else:
        r.log("resume_payment", f"Payment {pay['external_id']} already exists. Verifying it; I will not create another.",
              None, "Verify payment")
    return S.VERIFY_PAYMENT

def h_verify_payment(r):
    ctx = r.ctx; pay = ctx["payment"]
    total = ctx["shopping_list"]["estimated_total"]
    deadline = time.time() + PAY_WAIT_SECONDS
    while True:
        res = tp.check_payment(r.household_id, ctx["order_id"], pay["external_id"])
        if res["status"] in ("PROCESSED", "FAILED") or time.time() >= deadline:
            break
        if not pay.get("link_sent"):
            pay["link_sent"] = True; r.save()
            if pay.get("link"):
                r.log("payment_link", "The provider is waiting for the customer to authorise the payment. Sent the payment link.", {"link": pay["link"]})
                notifier.notify(r.run_id, r.household_id, "Please complete the payment", f"Pay ₹{total:.0f} here: {pay['link']}")
        time.sleep(3)
    if res["status"] == "PROCESSED":
        r.verified("PAYMENT_VERIFIED")
        r.log("verify_payment", f"Provider confirms {pay['external_id']} as PROCESSED", res["raw"], "Create shipment")
        return S.DELIVERY
    if res["status"] == "FAILED":
        r.log("verify_payment", "Payment FAILED. Stopping and not retrying automatically.", res["raw"])
        return r.need_human(S.PAYMENT_FAILED, "The payment failed. I have not retried and no shipment was created.",
                            [{"action": "retry_payment", "label": "Retry payment (I approve)"}, CANCEL])
    n = pay["rechecks"] = pay.get("rechecks", 0) + 1
    r.log("verify_payment", f"Provider status is still {res['status']} after {PAY_WAIT_SECONDS}s; I can't verify the payment finished.", res["raw"])
    opts = ([{"action": "recheck_payment", "label": "Check again"}] if n < 3 else []) + [CANCEL]
    return r.need_human(S.UNRESOLVED, "I can't confirm the payment completed. I won't assume it succeeded or pay again.", opts)

def h_delivery(r):
    ctx = r.ctx
    ship = ctx.setdefault("shipment", {})
    pin = ctx["household"]["pincode"]
    if not ship.get("external_id"):
        svc = td.check_serviceability(r.household_id, pin)
        if not svc["serviceable"]:
            r.log("check_serviceability", f"Pincode {pin} is not serviceable", svc["raw"])
            return r.need_human(S.DELIVERY_FAILED, f"Delivery isn't available to {pin}.", [CANCEL])
        key = f"{r.run_id}-ship"
        r.goto(S.DELIVERY)
        res = td.create_shipment(r.household_id, ctx["order_id"], pin, key)
        ship["external_id"] = res["external_id"]; r.save()
        r.log("create_shipment", f"Shipment {res['external_id']} created", res["raw"], "Verify delivery")
    return S.VERIFY_DELIVERY


def h_verify_delivery(r):
    ctx = r.ctx
    res = td.check_delivery(r.household_id, ctx["order_id"], ctx["shipment"]["external_id"])
    if res["status"] == "DELIVERED":
        r.verified("DELIVERY_VERIFIED")
        r.log("verify_delivery", "Carrier confirms DELIVERED", res["raw"], "Update pantry")
        return S.UPDATE_INVENTORY
    if res["status"] == "DELAYED":
        hh = ctx["household"]
        alt = tm.rank_meals(tm.find_meals(), hh["constraints"], ctx["inventory"], hh["recent_meals"], hh["preferences"],
                            hh["nutrition_goal"], (), True)["candidates"]
        opts = ([{"action": "cook_from_pantry", "label": f"Cook {alt[0]['name']} from the pantry instead"}] if alt else [])
        r.log("verify_delivery", "Carrier reports the delivery is DELAYED", {**res["raw"], "pantry_fallback": alt[0]["name"] if alt else None})
        return r.need_human(S.DELIVERY_FAILED, f"Delivery is delayed, so {ctx['meal']['name']} may not be cookable on time.",
                            opts + [{"action": "recheck_delivery", "label": "Check delivery again"}, CANCEL])
    r.log("verify_delivery", f"Carrier status is {res['status']}; I can't verify delivery", res["raw"])
    return r.need_human(S.UNRESOLVED, "I can't verify the delivery status.",
                        [{"action": "recheck_delivery", "label": "Check again"}, CANCEL])


def h_update_inventory(r):
    ctx = r.ctx
    if ctx.get("inventory_applied"):
        return S.LEARN
    with transaction() as conn:  # inventory change + flag commit atomically: no double-apply on resume
        tl.apply_inventory_changes(r.household_id, ctx["shopping_list"]["items"], ctx["meal"]["ingredients"],
                                   ctx.get("substitutions", []), conn=conn)
        ctx["inventory_applied"] = True
        r.save(conn=conn)
    r.log("update_inventory", "Added delivered items and deducted what the meal uses", None, "Learn")
    return S.LEARN


def h_learn(r):
    ctx = r.ctx
    tl.record_meal(r.household_id, ctx["meal"]["id"], r.plan_date)
    r.log("record_meal", f"Saved {ctx['meal']['name']} to meal history so I avoid repeating it soon", None, "Done")
    shopping_list = ctx.get("shopping_list") or {"items": [], "estimated_total": 0}
    bought = ", ".join(item["name"] for item in shopping_list["items"])
    message = (f"Tomorrow's dinner is {ctx['meal']['name']}. "
               + (f"{bought} arrived (₹{shopping_list['estimated_total']:.0f} paid). "
                  if shopping_list["items"] else "")
               + "Everything else is already in your pantry.")
    r.log("notify_household", message, None, "Done")
    notifier.notify(r.run_id, r.household_id, "Dinner is sorted", message)
    return S.COMPLETE


HANDLERS = {
    S.START: h_start, S.UNDERSTAND: h_understand, S.INVENTORY: h_inventory, S.SELECT_MEAL: h_select,
    S.MISSING: h_missing, S.BUDGET: h_budget, S.CREATE_ORDER: h_create_order, S.PAYMENT: h_payment,
    S.PAYMENT_PENDING: h_payment, S.VERIFY_PAYMENT: h_verify_payment, S.DELIVERY: h_delivery,
    S.VERIFY_DELIVERY: h_verify_delivery, S.UPDATE_INVENTORY: h_update_inventory, S.LEARN: h_learn,
}

# ---------------------------------------------------------------- loop

def advance(run_id):
    r = Run.load(run_id)
    if r.state != S.START:
        r.log("resume", f"Loaded checkpoint at {r.state} (last verified: {r.last_verified or 'nothing'}). Continuing from there.")
    r.status = "RUNNING"; r.save()
    try:
        while r.state not in S.STOP_STATES:
            nxt = HANDLERS[r.state](r)
            r.state = nxt
            r.save()
            time.sleep(STEP_DELAY)
    except SimulatedCrash:
        q("UPDATE agent_runs SET status='CRASHED', updated_at=now() WHERE run_id=%s", (run_id,))
        r.log("process_crash", "Simulated: the server process died right after the payment call.", None, "Waiting for restart")
        return
    except Exception as e:  # real bug or integration error
        r.ctx["error"] = repr(e); r.status = "ERROR"; r.save()
        r.log("error", f"Unexpected error: {e!r}. Stopped safely.")
        return
    r.status = {S.COMPLETE: "COMPLETE", S.CANCELLED: "CANCELLED"}.get(r.state, "NEEDS_HUMAN")
    r.save()


_active, _lock = set(), threading.Lock()


def start_background(run_id):
    with _lock:
        if run_id in _active:
            return False
        _active.add(run_id)

    def go():
        try:
            advance(run_id)
        finally:
            with _lock:
                _active.discard(run_id)
    threading.Thread(target=go, daemon=True).start()
    return True


def start_run(hid):
    r = Run.create(hid)
    start_background(r.run_id)
    return r.run_id


def resume_run(run_id):
    r = Run.load(run_id)
    if not r or r.status not in ("CRASHED", "RUNNING", "ERROR"):
        raise ValueError("Run is not resumable")
    return start_background(run_id)


def recover_orphans():
    """On server boot, pick up runs that were mid-flight when the process died."""
    for row in q("SELECT run_id FROM agent_runs WHERE status IN ('RUNNING','CRASHED')"):
        start_background(row["run_id"])


def resolve(run_id, action, params=None):
    params = params or {}
    r = Run.load(run_id)
    if not r or r.status != "NEEDS_HUMAN":
        raise ValueError("Run is not waiting for a human")
    req = r.ctx.get("human_request") or {}
    if action not in {o["action"] for o in req.get("options", [])}:
        raise ValueError(f"'{action}' is not an offered option")
    ctx = r.ctx
    nxt = None
    if action == "cancel":
        nxt = S.CANCELLED
    elif action == "raise_budget":
        ctx.setdefault("overrides", {})["budget"] = float(params.get("amount") or
                                                          next(o["amount"] for o in req["options"] if o["action"] == action))
        nxt = S.BUDGET
    elif action == "reject_meal":
        ctx.setdefault("rejected_meals", []).append(ctx["meal"]["id"]); nxt = S.SELECT_MEAL
    elif action == "relax_rule":
        q("DELETE FROM household_rules WHERE id=%s AND household_id=%s AND source <> 'setup'",
          (params["rule_id"], r.household_id))
        nxt = S.UNDERSTAND  # re-read constraints
    elif action == "confirm_inventory":
        ti.confirm_inventory(r.household_id); ctx["replans"] = 0; nxt = S.INVENTORY
    elif action == "retry_payment":
        ctx["payment"] = {"attempt": ctx["payment"]["attempt"] + 1}; nxt = S.PAYMENT
    elif action == "recheck_payment":
        nxt = S.VERIFY_PAYMENT
    elif action == "recheck_delivery":
        nxt = S.VERIFY_DELIVERY
    elif action == "cook_from_pantry":
        ctx["pantry_only"] = True; ctx["shopping_list"] = {"items": [], "estimated_total": 0}; ctx["substitutions"] = []
        nxt = S.SELECT_MEAL
    ctx.pop("human_request", None)
    r.state, r.status = nxt, "RUNNING"
    r.save()
    label = next((o["label"] for o in req["options"] if o["action"] == action), action)
    r.log("human_decision", f"Human chose: {label}", params or None, f"Continue at {nxt}")
    start_background(run_id)
