import { useEffect, useRef, useState } from "react";
import { toWav16k } from "./audio";
import { api } from "./api";

// Language codes are sent to your backend as `language`. Confirm the codes Gnani expects in their docs.
const LANGS = [
  ["hi-IN", "Hindi"], ["en-IN", "English (India)"], ["mr-IN", "Marathi"],
  ["gu-IN", "Gujarati"], ["ta-IN", "Tamil"], ["te-IN", "Telugu"],
];
const MAX_SECONDS = 60;
const MIN_SECONDS = 1;
const BARS = 36;

const Svg = ({ children, className = "h-5 w-5" }) => (
  <svg viewBox="0 0 24 24" className={className} fill="none" stroke="currentColor" strokeWidth="2"
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>
);
const MicIcon = () => <Svg><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" /></Svg>;
const TrashIcon = () => <Svg><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" /></Svg>;
const SendIcon = () => <Svg><path d="M12 19V5M5 12l7-7 7 7" /></Svg>;
const Spinner = () => (
  <svg viewBox="0 0 24 24" className="h-5 w-5 animate-spin" fill="none" aria-hidden="true">
    <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity=".25" strokeWidth="3" />
    <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
  </svg>
);

const fmt = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

/**
 * Tap the mic -> live waveform + timer -> tap the arrow to stop and send.
 * Audio is converted to 16 kHz mono WAV in the browser, posted to POST /voice (which forwards to Gnani),
 * and the transcript Gnani returns is shown here.
 *
 * Props: onResult(response)  called after a successful transcription.
 */
export default function VoiceNote({ onResult }) {
  const [phase, setPhase] = useState("idle"); // idle | recording | processing | done | error
  const [lang, setLang] = useState("hi-IN");
  const [secs, setSecs] = useState(0);
  const [levels, setLevels] = useState(() => Array(BARS).fill(0.08));
  const [result, setResult] = useState(null);
  const [transcriptDraft, setTranscriptDraft] = useState("");
  const [error, setError] = useState("");
  const [clipUrl, setClipUrl] = useState(null);
  const res = useRef({}); // recording resources (not state: no re-render needed)

  function teardown() {
    const c = res.current;
    cancelAnimationFrame(c.raf);
    c.stream?.getTracks().forEach((t) => t.stop());
    c.ac?.close().catch(() => {});
  }

  function fail(message) {
    teardown();
    setError(message);
    setPhase("error");
  }

  async function start() {
    setError(""); setResult(null);
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      return fail("This browser can't record audio. Try a recent Chrome, Edge, Firefox or Safari over HTTPS or localhost.");
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
      const rec = new MediaRecorder(stream);
      const chunks = [];
      rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);

      // live level meter
      const ac = new (window.AudioContext || window.webkitAudioContext)();
      const analyser = ac.createAnalyser();
      analyser.fftSize = 512;
      ac.createMediaStreamSource(stream).connect(analyser);
      const buf = new Uint8Array(analyser.fftSize);
      const startedAt = Date.now();
      let lastPush = 0;

      const tick = (t) => {
        analyser.getByteTimeDomainData(buf);
        let peak = 0;
        for (let i = 0; i < buf.length; i++) peak = Math.max(peak, Math.abs(buf[i] - 128) / 128);
        if (t - lastPush > 70) {
          lastPush = t;
          setLevels((l) => [...l.slice(1), Math.max(0.08, Math.min(1, peak * 2))]);
        }
        const s = Math.floor((Date.now() - startedAt) / 1000);
        setSecs(s);
        if (s >= MAX_SECONDS) return finish();
        res.current.raf = requestAnimationFrame(tick);
      };

      res.current = { stream, rec, chunks, ac, startedAt, cancelled: false, raf: requestAnimationFrame(tick) };
      rec.start();
      setSecs(0);
      setLevels(Array(BARS).fill(0.08));
      setPhase("recording");
    } catch (e) {
      fail(e.name === "NotAllowedError"
        ? "Microphone access was blocked. Allow it in your browser's site settings and try again."
        : `Couldn't start recording: ${e.message}`);
    }
  }

  function cancel() {
    const c = res.current;
    c.cancelled = true;
    if (c.rec && c.rec.state !== "inactive") c.rec.stop();
    teardown();
    setPhase("idle");
  }

  function finish() {
    const c = res.current;
    if (!c.rec || c.rec.state === "inactive") return;
    cancelAnimationFrame(c.raf);
    const seconds = (Date.now() - c.startedAt) / 1000;
    c.rec.onstop = async () => {
      teardown();
      if (c.cancelled) return;
      if (seconds < MIN_SECONDS) return fail("That was too short. Hold on a little longer and speak clearly.");
      setPhase("processing");
      try {
        const wav = await toWav16k(new Blob(c.chunks, { type: c.rec.mimeType }));
        setClipUrl((old) => { if (old) URL.revokeObjectURL(old); return URL.createObjectURL(wav); });
        const response = await api.voice("", wav, lang);
        setResult(response);
        setTranscriptDraft(response.transcript || "");
        setPhase("done");
        onResult?.(response);
      } catch (e) {
        fail(`Couldn't transcribe: ${e.message}`);
      }
    };
    c.rec.stop();
  }

  async function reinterpret() {
    const correctedTranscript = transcriptDraft.trim();
    if (!correctedTranscript) {
      setError("Enter the words you meant to say, then re-interpret.");
      setPhase("error");
      return;
    }
    setPhase("processing");
    setError("");
    try {
      const response = await api.voice(correctedTranscript, null, lang);
      setResult((previous) => ({
        ...previous,
        transcript: correctedTranscript,
        interpretation: response.interpretation,
        rule_id: response.rule_id,
      }));
      setPhase("done");
      onResult?.(response);
    } catch (e) {
      setError(`Couldn't re-interpret: ${e.message}`);
      setPhase("error");
    }
  }

  // Esc cancels while recording
  useEffect(() => {
    if (phase !== "recording") return;
    const onKey = (e) => e.key === "Escape" && cancel();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [phase]);

  // cleanup on unmount
  useEffect(() => () => { teardown(); }, []);
  useEffect(() => () => { if (clipUrl) URL.revokeObjectURL(clipUrl); }, [clipUrl]);

  const circleBtn = "grid h-9 w-9 shrink-0 place-items-center rounded-full transition active:scale-95";

  return (
    <div className="space-y-3">
      {phase === "idle" || phase === "error" || phase === "done" ? (
        <div className="flex items-center gap-3">
          <button onClick={start} aria-label="Record a voice note"
            className="grid h-12 w-12 shrink-0 place-items-center rounded-full bg-ink text-paper shadow-sm transition hover:scale-105 active:scale-95">
            <MicIcon />
          </button>
          <div className="min-w-0">
            <p className="font-semibold leading-tight">{phase === "done" ? "Record another" : "Speak to Rasoi"}</p>
            <select value={lang} onChange={(e) => setLang(e.target.value)} aria-label="Language"
              className="mt-0.5 -ml-1 rounded bg-transparent text-xs text-steel hover:text-ink">
              {LANGS.map(([code, name]) => <option key={code} value={code}>{name}</option>)}
            </select>
          </div>
        </div>
      ) : null}

      {phase === "recording" && (
        <div className="flex items-center gap-2 rounded-full border border-line bg-paper py-1.5 pl-1.5 pr-1.5 shadow-sm"
          role="group" aria-label="Recording">
          <button onClick={cancel} aria-label="Discard recording (Esc)" className={`${circleBtn} text-steel hover:bg-tile hover:text-ink`}>
            <TrashIcon />
          </button>
          <span className="h-2.5 w-2.5 shrink-0 rounded-full bg-mirchi live-dot" aria-hidden="true" />
          <span className="w-9 shrink-0 text-sm tabular-nums" aria-live="off">{fmt(secs)}</span>
          <div className="flex h-8 min-w-0 flex-1 items-center gap-[2px]" aria-hidden="true">
            {levels.map((v, i) => (
              <span key={i} className="w-[3px] rounded-full bg-ink"
                style={{ height: `${v * 100}%`, opacity: 0.3 + 0.7 * (i / BARS) }} />
            ))}
          </div>
          <button onClick={finish} aria-label="Stop and send to Gnani" className={`${circleBtn} bg-ink text-paper hover:brightness-110`}>
            <SendIcon />
          </button>
        </div>
      )}

      {phase === "processing" && (
        <div className="flex items-center gap-3 rounded-full border border-line bg-paper px-4 py-2.5 text-sm shadow-sm" role="status">
          <Spinner /> Transcribing with Gnani…
        </div>
      )}

      {phase === "error" && (
        <p role="alert" className="rounded-lg border border-mirchi bg-mirchi/10 p-3 text-sm text-mirchi">{error}</p>
      )}

      {phase === "done" && result && (
        <div className="space-y-3 rounded-xl border border-line bg-paper p-4 text-sm">
          {result.raw_gnani_response?.mock && (
            <p className="rounded-lg bg-haldi/20 p-2 text-xs">
              Mock mode: nothing was transcribed. Set USE_MOCKS=false and add GNANI_STT_URL and GNANI_API_KEY in backend/.env.
            </p>
          )}
          <div>
            <label htmlFor="voice-transcript" className="text-xs text-steel">Transcript (edit if Gnani heard it incorrectly)</label>
            <textarea id="voice-transcript" value={transcriptDraft} onChange={(e) => setTranscriptDraft(e.target.value)}
              rows={2} className="mt-1 w-full resize-y rounded-lg border border-line bg-tile px-3 py-2 text-base leading-snug"
              placeholder="Gnani returned no text. Type what you said here." />
            <button type="button" onClick={reinterpret} disabled={!transcriptDraft.trim()}
              className="mt-2 rounded-lg border border-ink px-3 py-1.5 text-xs font-semibold hover:bg-ink hover:text-paper disabled:opacity-50">
              Re-interpret corrected text
            </button>
          </div>
          {clipUrl && <audio controls src={clipUrl} className="h-9 w-full" />}
          <div>
            <p className="text-xs text-steel">Rasoi understood</p>
            <p className="mt-0.5">{result.interpretation?.summary}</p>
            {result.interpretation?.intent === "exclude_ingredient" ? (
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                <span className="rounded-full bg-pudina/15 px-2 py-0.5 text-xs text-pudina">no {result.interpretation.ingredient}</span>
                {result.interpretation.date && <span className="rounded-full bg-tile px-2 py-0.5 text-xs">{result.interpretation.date}</span>}
              </div>
            ) : (
              <p className="mt-1 text-xs text-steel">I couldn't turn this into a rule, so nothing changed.</p>
            )}
          </div>
          <details>
            <summary className="cursor-pointer text-xs text-steel">Raw Gnani response</summary>
            <pre className="mt-1 max-h-48 overflow-auto rounded bg-tile p-2 text-xs">{JSON.stringify(result.raw_gnani_response, null, 2)}</pre>
          </details>
        </div>
      )}
    </div>
  );
}