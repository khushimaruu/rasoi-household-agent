const j = async (r) => {
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
};
const post = (url, body) =>
  fetch("/api" + url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) }).then(j);

export const api = {
  household: (id = "demo-household") => fetch(`/api/household/${id}`).then(j),
  messages: () => fetch("/api/messages").then(j),
  latest: () => fetch("/api/agent/runs/latest").then(j),
  simulate: (kind) => post(`/simulate/${kind}`),
  run: () => post("/agent/run"),
  resume: (id) => post(`/agent/resume/${id}`),
  resolve: (id, action, params) => post(`/agent/${id}/resolve`, { action, params }),
  voice: (text, file, language = "hi-IN") => {
    const f = new FormData();
    f.append("household_id", "demo-household");
    f.append("language", language);
    if (text) f.append("text_fallback", text);
    if (file) f.append("audio", file);
    return fetch("/api/voice", { method: "POST", body: f }).then(j);
  },
};
