"""The 'person behind the curtain'. The simulator only changes the WORLD (prices, stock, failures).
It never tells Rasoi what to decide."""
from db.database import q, one, J
from db import seed


def arm_event(hid, kind, payload=None):
    q("INSERT INTO world_events (household_id,kind,payload) VALUES (%s,%s,%s)", (hid, kind, J(payload or {})))


def pop_event(hid, kind) -> bool:
    """Consume one pending event of this kind (used by the mock integrations)."""
    row = one("""UPDATE world_events SET consumed=true
                 WHERE id=(SELECT id FROM world_events WHERE household_id=%s AND kind=%s AND NOT consumed
                           ORDER BY id LIMIT 1) RETURNING id""", (hid, kind))
    return row is not None


SCENARIOS = {
    "happy": "Happy path",
    "payment_failure": "Payment failure",
    "out_of_stock": "Out of stock (substitute exists)",
    "out_of_stock_no_sub": "Out of stock (no substitute)",
    "delivery_delay": "Delivery delay",
    "budget_exceeded": "Budget exceeded",
    "constraint_conflict": "Constraint conflict",
    "stale_inventory": "Stale inventory",
    "crash_resume": "Crash and resume",
}


def apply_scenario(kind, hid):
    """Reseeds the world, then injects one world-level change."""
    seed.reset_world()
    if kind == "happy":
        return
    if kind == "payment_failure":
        arm_event(hid, "PAYMENT_FAILED")
    elif kind == "delivery_delay":
        arm_event(hid, "DELIVERY_DELAY")
    elif kind == "crash_resume":
        arm_event(hid, "CRASH_AFTER_PAYMENT")
    elif kind == "out_of_stock":
        q("UPDATE catalog SET in_stock=false WHERE ingredient='capsicum'")
    elif kind == "out_of_stock_no_sub":
        q("UPDATE catalog SET in_stock=false WHERE ingredient IN ('capsicum','cabbage','carrot')")
    elif kind == "budget_exceeded":
        q("UPDATE households SET budget_daily=20 WHERE id=%s", (hid,))
    elif kind == "stale_inventory":
        q("UPDATE inventory SET updated_at = now() - interval '5 days' WHERE household_id=%s", (hid,))
    elif kind == "constraint_conflict":
        # a family member adds new requirements; no meal can satisfy all of them
        for t, v in [("exclude_contains", {"value": "dairy"}), ("min_protein", {"value": 28}),
                     ("max_meal_cost", {"value": 100})]:
            q("INSERT INTO household_rules (household_id,rule_type,rule_value,source) VALUES (%s,%s,%s,'family')",
              (hid, t, J(v)))
    else:
        raise ValueError(f"unknown scenario {kind}")
