import { useEffect, useState, useCallback } from "react";
import { api } from "./api";
import VoiceNote from "./Voicenote";

const SCENARIOS = [
  ["happy", "Happy path"],
  ["out_of_stock", "Out of stock, substitute exists"],
  ["out_of_stock_no_sub", "Out of stock, no substitute"],
  ["payment_failure", "Payment failure"],
  ["delivery_delay", "Delivery delay"],
  ["budget_exceeded", "Budget exceeded"],
  ["constraint_conflict", "Constraint conflict"],
  ["stale_inventory", "Stale pantry"],
  ["crash_resume", "Crash and resume"],
];

const tone = { COMPLETE: "text-pudina", NEEDS_HUMAN: "text-mirchi", CRASHED: "text-mirchi", CANCELLED: "text-steel", ERROR: "text-mirchi" };

export default function App() {
  const [hh, setHh] = useState(null);
  const [run, setRun] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const refreshHousehold = useCallback(() => api.household().then(setHh).catch((e) => setErr(e.message)), []);
  useEffect(() => {
    refreshHousehold();
    api.latest().then(setRun).catch((e) => setErr(e.message));
  }, [refreshHousehold]);

  const live = run && ["RUNNING", "CRASHED"].includes(run.status);
  useEffect(() => {
    if (!run) return;
    let cancelled = false;
    let timer;
    const poll = async () => {
      try {
        const latest = await api.latest();
        if (cancelled) return;
        setRun(latest);
        if (latest?.status !== "RUNNING") refreshHousehold();
        setErr("");
      } catch (e) {
        if (!cancelled) setErr(`Run status refresh failed: ${e.message}`);
      }
      if (!cancelled) timer = setTimeout(poll, 1500);
    };
    timer = setTimeout(poll, 1500);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [run?.run_id, refreshHousehold]);

  const start = async (kind) => {
    setBusy(true); setErr("");
    try {
      await api.simulate(kind);
      const { run_id } = await api.run();
      await new Promise((r) => setTimeout(r, 400));
      setRun(await api.latest());
      refreshHousehold();
    } catch (e) { setErr(e.message); } finally { setBusy(false); }
  };

  const act = (fn) => async (...a) => { try { await fn(...a); setRun(await api.latest()); } catch (e) { setErr(e.message); } };

  return (
    <div className="min-h-screen">
      <header className="flex items-baseline gap-4 border-b border-line bg-paper px-6 py-4">
        <h1 className="text-3xl font-bold tracking-tight">Rasoi</h1>
        <p className="text-steel">Your household cooking agent</p>
        {err && <p className="ml-auto text-sm text-mirchi" role="alert">{err}</p>}
      </header>

      <main className="mx-auto grid max-w-[1400px] gap-6 p-6 lg:grid-cols-[280px_minmax(0,1fr)_minmax(0,1.1fr)]">
        <Household hh={hh} refresh={refreshHousehold} />

        <section className="space-y-5">
          <div className="rounded-xl bg-ink p-6 text-paper">
            <h2 className="text-2xl font-bold">Plan tomorrow's meal</h2>
            <p className="mt-1 text-sm text-paper/70">Rasoi picks, buys, pays and verifies on its own. Scenarios below only change the world around it.</p>
            <button disabled={busy || run?.status === "RUNNING"} onClick={() => start("happy")}
              className="mt-5 w-full rounded-lg bg-haldi px-5 py-4 font-display text-xl font-bold text-ink transition hover:brightness-95 disabled:opacity-50">
              Run today's plan
            </button>
            <div className="mt-4 flex flex-wrap gap-2">
              {SCENARIOS.slice(1).map(([k, label]) => (
                <button key={k} disabled={busy || run?.status === "RUNNING"} onClick={() => start(k)}
                  className="rounded-full border border-paper/30 px-3 py-1.5 text-sm hover:bg-paper/10 disabled:opacity-40">{label}</button>
              ))}
            </div>
          </div>

          {run ? (
            <>
              <Rail run={run} />
              {run.status === "CRASHED" && (
                <Card>
                  <p className="font-semibold text-mirchi">The server process crashed mid-order.</p>
                  <p className="mt-1 text-sm text-steel">Last verified: {run.last_verified_state || "nothing"}. Restart to resume from the checkpoint.</p>
                  <button onClick={act(() => api.resume(run.run_id))} className="mt-3 rounded-lg bg-ink px-4 py-2 text-paper">Restart Rasoi</button>
                </Card>
              )}
              {run.human_request && run.status === "NEEDS_HUMAN" && (
                <Card border="border-mirchi">
                  <p className="font-semibold">{run.state.replaceAll("_", " ").toLowerCase()}</p>
                  <p className="mt-1">{run.human_request.message}</p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    {run.human_request.options.map((o) => (
                      <button key={o.action + (o.rule_id || "")} onClick={act(() => api.resolve(run.run_id, o.action, { rule_id: o.rule_id, amount: o.amount }))}
                        className="rounded-lg border border-ink px-3 py-2 text-sm hover:bg-ink hover:text-paper">{o.label}</button>
                    ))}
                  </div>
                </Card>
              )}
              <Plan run={run} />
            </>
          ) : (
            <Card><p className="text-steel">No run yet. Start with the happy path, then try a failure scenario.</p></Card>
          )}
        </section>

        <Log run={run} live={live} />
      </main>
    </div>
  );
}

const Card = ({ children, border = "border-line" }) => (
  <div className={`rounded-xl border ${border} bg-paper p-5`}>{children}</div>
);

function Household({ hh, refresh }) {
  const h = hh?.household;
  if (!h) return <aside className="text-steel">Loading household…</aside>;
  return (
    <aside className="space-y-5 text-sm">
      <Card>
        <h2 className="text-lg font-bold">{h.name}</h2>
        <p className="mt-1 text-steel">{h.member_count} members · ₹{h.budget}/day · {h.nutrition_goal?.replace("_", " ")}</p>
        <p className="mt-3 font-semibold">Rules</p>
        <ul className="mt-1 space-y-1">{h.constraint_labels.map((l) => <li key={l}>{l}</li>)}</ul>
      </Card>
      <Card>
        <p className="font-semibold">Pantry</p>
        <ul className="mt-2 space-y-1">
          {hh.inventory.items.map((i) => (
            <li key={i.ingredient} className="flex justify-between">
              <span>{i.ingredient}</span>
              <span className={i.expires_in_days <= 1 ? "font-semibold text-mirchi" : "text-steel"}>{i.quantity}{i.unit === "piece" ? "" : i.unit}</span>
            </li>
          ))}
        </ul>
        {hh.inventory.stale && <p className="mt-2 text-mirchi">Pantry is stale ({hh.inventory.hours_since_update}h)</p>}
      </Card>
      <Card>
        <p className="font-semibold">Voice note</p>
        <div className="mt-3"><VoiceNote onResult={() => refresh()} /></div>
      </Card>
    </aside>
  );
}

function Rail({ run }) {
  const blocked = run.status === "NEEDS_HUMAN" || run.status === "CRASHED" || run.status === "ERROR";
  return (
    <Card>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-lg font-bold">Current run</h2>
        <span className={`text-sm font-semibold ${tone[run.status] || "text-haldi"}`}>{run.status.replace("_", " ").toLowerCase()}</span>
      </div>
      <ol>
        {run.pipeline.map((s, i) => {
          const done = i < run.step_index || run.status === "COMPLETE";
          const cur = i === run.step_index && run.status !== "COMPLETE";
          const bad = cur && blocked;
          return (
            <li key={s.id} className="flex items-stretch gap-3">
              <div className="flex flex-col items-center">
                <span className={`mt-1 grid h-5 w-5 place-items-center rounded-full border-2 text-[10px] font-bold
                  ${done ? "border-pudina bg-pudina text-paper" : bad ? "border-mirchi bg-mirchi text-paper" : cur ? "border-haldi bg-haldi live-dot" : "border-line"}`}>
                  {done ? "✓" : bad ? "!" : ""}
                </span>
                {i < run.pipeline.length - 1 && <span className={`w-0.5 flex-1 ${done ? "bg-pudina" : "bg-line"}`} />}
              </div>
              <div className="pb-3">
                <p className={`${cur ? "font-semibold" : done ? "" : "text-steel"}`}>{s.label}</p>
                {cur && <p className="text-xs text-steel">{run.state.replaceAll("_", " ").toLowerCase()}</p>}
              </div>
            </li>
          );
        })}
      </ol>
      {run.last_verified_state && <p className="mt-1 text-xs text-steel">Last verified: {run.last_verified_state}</p>}
    </Card>
  );
}

function Plan({ run }) {
  if (!run.meal) return null;
  const m = run.meal, sl = run.shopping_list, o = run.order;
  return (
    <Card>
      <h2 className="text-lg font-bold">{m.name}</h2>
      <ul className="mt-2 space-y-0.5 text-sm">{m.reasons.map((r) => <li key={r}>✓ {r}</li>)}</ul>
      {sl && sl.items.length > 0 && (
        <div className="mt-3 text-sm">
          <p className="font-semibold">Shopping list · ₹{sl.estimated_total}</p>
          {sl.items.map((i) => (
            <p key={i.name} className="text-steel">{i.quantity} {i.unit === "piece" ? "" : i.unit} {i.name} · ₹{i.line_total}{i.substitute_for ? ` (instead of ${i.substitute_for})` : ""}</p>
          ))}
        </div>
      )}
      {o && <p className="mt-3 text-sm">Order #{o.id}: payment <b>{o.payment_status}</b>, delivery <b>{o.delivery_status || "not started"}</b></p>}
    </Card>
  );
}

function Log({ run, live }) {
  const ev = run?.events || [];
  return (
    <section>
      <div className="mb-3 flex items-center gap-2">
        <h2 className="text-lg font-bold">Decision log</h2>
        {live && <span className="h-2 w-2 rounded-full bg-haldi live-dot" aria-label="live" />}
      </div>
      {ev.length === 0 && <p className="text-steel">Rasoi's decisions will appear here.</p>}
      <ol className="space-y-3">
        {[...ev].reverse().map((e) => (
          <li key={e.id} className={`rounded-xl border bg-paper p-4 ${["process_crash", "error"].includes(e.action) || e.action === "ask_human" ? "border-mirchi" : "border-line"}`}>
            <div className="flex justify-between text-xs text-steel">
              <span>{new Date(e.ts).toLocaleTimeString()}</span><span>{e.state?.replaceAll("_", " ").toLowerCase()}</span>
            </div>
            <p className="mt-1 font-semibold">{e.action.replaceAll("_", " ")}</p>
            <p className="text-sm">{e.reason}</p>
            {e.next_step && <p className="mt-1 text-sm text-steel">Next: {e.next_step}</p>}
            {e.result && (
              <details className="mt-1"><summary className="cursor-pointer text-xs text-steel">Result</summary>
                <pre className="mt-1 max-h-44 overflow-auto rounded bg-tile p-2 text-xs">{JSON.stringify(e.result, null, 2)}</pre>
              </details>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}
