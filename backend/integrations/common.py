import os, uuid
from db.database import one, q, J

def use_mocks(*required_env, rail=None):
    flag = os.getenv(f"MOCK_{rail}", "").lower() if rail else ""
    if flag in ("true", "false"):          # per-rail setting wins
        return flag == "true"
    if os.getenv("USE_MOCKS", "true").lower() == "true":
        return True
    return not all(os.getenv(k) for k in required_env)


def replay(key):
    return one("SELECT * FROM mock_external WHERE idempotency_key=%s", (key,))


def store(provider, prefix, key, status, payload):
    ext = prefix + uuid.uuid4().hex[:10].upper()
    q("INSERT INTO mock_external (external_id,provider,idempotency_key,status,payload) VALUES (%s,%s,%s,%s,%s)",
      (ext, provider, key, status, J({**payload, "id": ext, "mock": True})))
    return ext


def fetch(external_id):
    return one("SELECT * FROM mock_external WHERE external_id=%s", (external_id,))
