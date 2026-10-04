"""Schema + demo data. Dates are relative to today so the demo always works."""
from datetime import date, timedelta
from pathlib import Path
from db.database import transaction, J

HID = "demo-household"
TABLES = ["messages", "voice_inputs", "mock_external", "world_events", "agent_events", "agent_runs", "meal_history",
          "orders", "meals", "catalog", "inventory", "household_rules", "members", "households"]

# name: (unit, price_per_unit, in_stock)
CATALOG = {
    "paneer": ("g", 0.4, True), "onion": ("piece", 12, True), "tomato": ("piece", 8, True),
    "capsicum": ("piece", 36, True), "atta": ("g", 0.05, True), "rice": ("g", 0.08, True),
    "toor dal": ("g", 0.15, True), "ghee": ("g", 0.6, True), "chickpeas": ("g", 0.12, True),
    "mixed veg": ("g", 0.06, True), "soya chunks": ("g", 0.5, True), "eggs": ("piece", 7, True),
    "cabbage": ("piece", 30, True), "carrot": ("piece", 10, True), "oil": ("g", 0.15, True),
}

def ing(name, qty):
    return {"name": name, "quantity": qty, "unit": CATALOG[name][0]}

# name, ingredients, diet, contains, protein, tags, instructions
MEALS = [
    ("Paneer Bhurji", [("paneer", 250), ("onion", 2), ("tomato", 2), ("capsicum", 2), ("atta", 400)],
     "vegetarian", ["dairy"], 24, ["Indian", "spicy"], "Scramble paneer with onion, tomato, capsicum. Serve with roti."),
    ("Dal Tadka", [("toor dal", 200), ("onion", 1), ("tomato", 2), ("ghee", 20), ("rice", 300)],
     "vegetarian", ["dairy"], 18, ["Indian"], "Pressure-cook dal, temper with ghee, serve with rice."),
    ("Vegetable Pulao", [("rice", 300), ("mixed veg", 300), ("onion", 1)],
     "vegetarian", [], 8, ["Indian"], "Cook rice with vegetables and whole spices."),
    ("Paneer Wrap", [("paneer", 200), ("atta", 300), ("capsicum", 1), ("onion", 1)],
     "vegetarian", ["dairy"], 20, ["Indian"], "Stuff rotis with spiced paneer."),
    ("Chole Rice", [("chickpeas", 250), ("rice", 300), ("onion", 2), ("tomato", 2)],
     "vegetarian", [], 19, ["Indian", "spicy"], "Simmer chickpeas in onion-tomato masala."),
    ("Mixed Veg Curry", [("mixed veg", 400), ("onion", 1), ("tomato", 2), ("atta", 300)],
     "vegetarian", [], 6, ["Indian"], "Dry vegetable curry with roti."),
    ("Soya Chunk Curry", [("soya chunks", 150), ("onion", 2), ("tomato", 2)],
     "vegetarian", [], 30, ["Indian", "spicy"], "Soak soya chunks, cook in masala."),
    ("Egg Curry", [("eggs", 8), ("onion", 2), ("tomato", 2)],
     "non_veg", ["egg"], 26, ["Indian", "spicy"], "Boiled eggs in onion-tomato gravy."),
]


def create_schema(reset=False, conn=None):
    def create(c):
        if reset:
            c.execute("DROP TABLE IF EXISTS " + ", ".join(TABLES) + " CASCADE")
        c.execute((Path(__file__).with_name("schema.sql")).read_text())

    if conn is None:
        with transaction() as c:
            create(c)
    else:
        create(conn)


def seed(conn=None):
    today = date.today()
    if conn is None:
        with transaction() as c:
            _seed(c, today)
    else:
        _seed(conn, today)


def _seed(c, today):
    c.execute("INSERT INTO households (id,name,budget_daily,pincode,nutrition_goal) VALUES (%s,%s,%s,%s,%s)",
              (HID, "The Mehta household", 500, "400001", "high_protein"))
    for n in ["Aarti", "Rohan", "Dadi", "Mira"]:
        c.execute("INSERT INTO members (household_id,name,diet,preferences,restrictions) VALUES (%s,%s,%s,%s,%s)",
                  (HID, n, "vegetarian", J(["Indian", "spicy"]), J(["no_egg"])))
    rules = [
        ("diet", {"value": "vegetarian"}),
        ("religious_restriction", {"value": "no_non_veg"}),
        ("exclude_contains", {"value": "egg"}),
        ("approved_substitution", {"from": "capsicum", "to": ["cabbage", "carrot"]}),
        ("approved_substitution", {"from": "ghee", "to": ["oil"]}),
    ]
    for t, v in rules:
        c.execute("INSERT INTO household_rules (household_id,rule_type,rule_value,source) VALUES (%s,%s,%s,'setup')",
                  (HID, t, J(v)))
    for name, (unit, price, stock) in CATALOG.items():
        c.execute("INSERT INTO catalog VALUES (%s,%s,%s,%s)", (name, unit, price, stock))
    for name, items, diet, contains, protein, tags, how in MEALS:
        ings = [ing(n, qn) for n, qn in items]
        cost = round(sum(CATALOG[n][1] * qn for n, qn in items))
        c.execute("""INSERT INTO meals (name,ingredients,instructions,nutrition,cost_estimate,tags,diet,contains)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                  (name, J(ings), how, J({"protein": protein}), cost, J(tags), diet, J(contains)))
        pantry = [("paneer", 250, "g", today + timedelta(days=7)), ("tomato", 4, "piece", today + timedelta(days=7)),
              ("rice", 2000, "g", today + timedelta(days=60)), ("atta", 1000, "g", today + timedelta(days=90)),
              ("toor dal", 500, "g", today + timedelta(days=2))]
    for i, u, unit, exp in pantry:
        c.execute("INSERT INTO inventory (household_id,ingredient,quantity,unit,expiry_date) VALUES (%s,%s,%s,%s,%s)",
                  (HID, i, u, unit, exp))
    for name, ago in [("Paneer Wrap", 1), ("Chole Rice", 2)]:
        c.execute("""INSERT INTO meal_history (household_id,meal_id,date,status)
                     SELECT %s, id, %s, 'cooked' FROM meals WHERE name=%s""",
                  (HID, today - timedelta(days=ago), name))


def reset_world():
    with transaction() as c:
        create_schema(reset=True, conn=c)
        seed(conn=c)
