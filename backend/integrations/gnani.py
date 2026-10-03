"""Gnani speech-to-text adapter. The raw provider response is always returned so it can be stored and shown.

Real mode needs GNANI_STT_URL + GNANI_API_KEY (+ header name/scheme if Gnani's docs differ from the defaults).
Mock mode does NOT transcribe audio: it echoes `text_fallback` and is flagged "mock": true so it is never
passed off as a real Gnani response.
"""
import os
import httpx
from integrations import common


def _extract(raw) -> str:
    if isinstance(raw, dict):
        for k in ("transcript", "text", "transcription"):
            if isinstance(raw.get(k), str):
                return raw[k]
        for v in raw.values():
            t = _extract(v)
            if t:
                return t
    return ""


async def transcribe(audio: bytes | None, filename: str, language: str, text_fallback: str | None):
    if common.use_mocks("GNANI_STT_URL", "GNANI_API_KEY") or not audio:
        raw = {"mock": True, "transcript": text_fallback or "", "language": language,
               "note": "Mock STT: no audio was sent to Gnani."}
        return {"raw": raw, "transcript": raw["transcript"]}
    scheme = os.getenv("GNANI_AUTH_SCHEME", "Bearer")
    key = os.environ["GNANI_API_KEY"]
    headers = {os.getenv("GNANI_AUTH_HEADER", "Authorization"): f"{scheme} {key}".strip()}
    async with httpx.AsyncClient(timeout=60) as c:
        try:
            r = await c.post(os.environ["GNANI_STT_URL"], headers=headers,
                             files={"audio_file": (filename or "audio.wav", audio)},
                             data={"language_code": language})
            r.raise_for_status()
            raw = r.json()
        except httpx.HTTPStatusError as exc:
            response_body = exc.response.text[:500]
            raise RuntimeError(f"Gnani returned HTTP {exc.response.status_code}: {response_body}") from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError("Gnani speech recognition timed out.") from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"Could not connect to Gnani: {exc}") from exc
        except ValueError as exc:
            raise RuntimeError("Gnani returned an invalid JSON response.") from exc
    return {"raw": raw, "transcript": _extract(raw)}
