"""Pine Labs adapter. Normalised statuses: PROCESSED | FAILED | PENDING | UNKNOWN.

MockPineLabs keeps its 'server' state in Neon so it survives an agent crash (needed for the crash demo).
PineLabsClient is the real HTTP adapter. Paths/payloads are env-configurable: check them against the
Pine Labs docs for your UAT credentials before relying on it. A real order usually has to be completed by a
customer at hosted checkout, so it will stay PENDING and Rasoi will correctly report UNRESOLVED, not SUCCESS.
"""
import os
import httpx
from tools import world
from integrations import common

NORMALISE = {"PROCESSED": "PROCESSED", "SUCCESS": "PROCESSED", "CAPTURED": "PROCESSED",
             "FAILED": "FAILED", "CANCELLED": "FAILED", "REJECTED": "FAILED",
             "CREATED": "PENDING", "PENDING": "PENDING", "PROCESSING": "PENDING"}


class MockPineLabs:
    def create_payment(self, hid, amount, currency, key, reference):
        existing = common.replay(key)
        if existing:  # idempotent: same key never creates a second payment
            return {"external_id": existing["external_id"], "status": existing["status"],
                    "raw": existing["payload"], "replayed": True}
        failed = world.pop_event(hid, "PAYMENT_FAILED")
        status = "FAILED" if failed else "PROCESSED"
        payload = {"reference": reference, "amount": {"value": int(amount * 100), "currency": currency},
                   "status": status, **({"failure_reason": "CARD_DECLINED"} if failed else {})}
        ext = common.store("pinelabs", "PL", key, status, payload)
        return {"external_id": ext, "status": status, "raw": {**payload, "id": ext}}

    def get_payment(self, hid, external_id):
        row = common.fetch(external_id)
        if not row:
            return {"external_id": external_id, "status": "UNKNOWN", "raw": {}}
        return {"external_id": external_id, "status": row["status"], "raw": row["payload"]}


class PineLabsClient:
    def __init__(self):
        self.base = os.environ["PINELABS_BASE_URL"].rstrip("/")
        self.token_path = os.getenv("PINELABS_TOKEN_PATH", "/api/auth/v1/token")
        self.orders_path = os.getenv("PINELABS_ORDERS_PATH", "/api/pay/v1/orders")
        self._token = None

    def _headers(self):
        if not self._token:
            r = httpx.post(self.base + self.token_path, timeout=20, json={
                "client_id": os.environ["PINELABS_CLIENT_ID"],
                "client_secret": os.environ["PINELABS_CLIENT_SECRET"], "grant_type": "client_credentials"})
            r.raise_for_status()
            self._token = r.json()["access_token"]
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def create_payment(self, hid, amount, currency, key, reference):
        body = {"merchant_order_reference": key, "order_amount": {"value": int(amount * 100), "currency": currency},
                "notes": reference}
        r = httpx.post(self.base + self.orders_path, headers=self._headers(), json=body, timeout=30)
        r.raise_for_status()
        raw = r.json(); data = raw.get("data", raw)
        return {"external_id": data.get("order_id") or data.get("id"),
                "status": NORMALISE.get(str(data.get("status", "")).upper(), "UNKNOWN"), "raw": raw}

    def get_payment(self, hid, external_id):
        r = httpx.get(f"{self.base}{self.orders_path}/{external_id}", headers=self._headers(), timeout=30)
        r.raise_for_status()
        raw = r.json(); data = raw.get("data", raw)
        return {"external_id": external_id,
                "status": NORMALISE.get(str(data.get("status", "")).upper(), "UNKNOWN"), "raw": raw}


def get_client():
    if common.use_mocks("PINELABS_BASE_URL", "PINELABS_CLIENT_ID", "PINELABS_CLIENT_SECRET", rail="PINELABS"):
        return MockPineLabs()
    return PineLabsClient()
