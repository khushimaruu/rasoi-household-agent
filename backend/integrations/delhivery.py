"""Delhivery adapter. Normalised statuses: DELIVERED | DELAYED | IN_TRANSIT | UNKNOWN.

Real endpoints below follow Delhivery's Express API (staging-express.delhivery.com); confirm against your
developer-portal docs/token. Mock keeps its state in Neon.
"""
import os, json
import httpx
from tools import world
from integrations import common


class MockDelhivery:
    def check_serviceability(self, hid, pincode):
        ok = bool(pincode) and pincode.isdigit() and len(pincode) == 6
        return {"serviceable": ok, "raw": {"pincode": pincode, "delivery_codes": [{"postal_code": {"pin": pincode}}] if ok else [], "mock": True}}

    def create_shipment(self, hid, pincode, key, reference):
        existing = common.replay(key)
        if existing:
            return {"external_id": existing["external_id"], "status": existing["status"], "raw": existing["payload"], "replayed": True}
        ext = common.store("delhivery", "DLV", key, "MANIFESTED", {"reference": reference, "pincode": pincode, "status": "MANIFESTED"})
        return {"external_id": ext, "status": "MANIFESTED", "raw": {"waybill": ext, "status": "MANIFESTED"}}

    def track_shipment(self, hid, external_id):
        row = common.fetch(external_id)
        if not row:
            return {"external_id": external_id, "status": "UNKNOWN", "raw": {}}
        delayed = world.pop_event(hid, "DELIVERY_DELAY")
        status = "DELAYED" if delayed else "DELIVERED"
        from db.database import q, J
        q("UPDATE mock_external SET status=%s, payload=payload || %s WHERE external_id=%s",
          (status, J({"status": status}), external_id))
        return {"external_id": external_id, "status": status, "raw": {"waybill": external_id, "status": status}}


class DelhiveryClient:
    def __init__(self):
        self.base = os.environ["DELHIVERY_BASE_URL"].rstrip("/")
        self.h = {"Authorization": f"Token {os.environ['DELHIVERY_TOKEN']}"}

    def check_serviceability(self, hid, pincode):
        r = httpx.get(f"{self.base}/c/api/pin-codes/json/", params={"filter_codes": pincode}, headers=self.h, timeout=20)
        r.raise_for_status(); raw = r.json()
        return {"serviceable": bool(raw.get("delivery_codes")), "raw": raw}

    def create_shipment(self, hid, pincode, key, reference):
        payload = {"shipments": [{"order": key, "pin": pincode, "payment_mode": "Prepaid", "name": "Household",
                                  "add": "Demo address", "phone": "9999999999", "products_desc": reference}],
                   "pickup_location": {"name": os.getenv("DELHIVERY_PICKUP", "primary")}}
        r = httpx.post(f"{self.base}/api/cmu/create.json", headers=self.h, timeout=30,
                       data={"format": "json", "data": json.dumps(payload)})
        r.raise_for_status(); raw = r.json()
        pk = (raw.get("packages") or [{}])[0]
        return {"external_id": pk.get("waybill"), "status": "MANIFESTED", "raw": raw}

    def track_shipment(self, hid, external_id):
        r = httpx.get(f"{self.base}/api/v1/packages/json/", params={"waybill": external_id}, headers=self.h, timeout=20)
        r.raise_for_status(); raw = r.json()
        try:
            s = raw["ShipmentData"][0]["Shipment"]["Status"]["Status"].upper()
        except (KeyError, IndexError, TypeError):
            s = ""
        status = "DELIVERED" if s == "DELIVERED" else "DELAYED" if "DELAY" in s or "UNDELIVERED" in s else \
            "IN_TRANSIT" if s else "UNKNOWN"
        return {"external_id": external_id, "status": status, "raw": raw}


def get_client():
    if common.use_mocks("DELHIVERY_TOKEN", rail="DELHIVERY"):
        return MockDelhivery()
    return DelhiveryClient()
