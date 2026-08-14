"""MIMO ASR Bridge — speech-to-text via MiMo OpenAI-compatible endpoint.

Usage:
    from analysis.mimo_asr import analyze_audio
    text = analyze_audio("lecture_segment.mp3", language="zh")

Config:
    MIMO_API_KEY in .env file at project root (shared with vision bridge)
Constraints (per MiMo docs 2026-07):
    - formats: wav / mp3 only; base64 payload <= 10 MB
    - billing: ¥0.5 per audio hour (prepaid balance)
"""

import base64
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

def _load_asr_config() -> dict:
    """Read ASR provider config from config/multimodal.json.

    Swappable model: change provider/model/base_url there; the API key
    env var name lives in the config but the key itself only ever comes
    from the environment (.env) — never from the json file.
    """
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "multimodal.json"
    try:
        import json as _json
        cfg = _json.loads(cfg_path.read_text(encoding="utf-8")).get("asr", {})
    except Exception:
        cfg = {}
    return {
        "model": cfg.get("model", "mimo-v2.5-asr"),
        "base_url": cfg.get("base_url", "https://api.xiaomimimo.com/v1/chat/completions"),
        "api_key_env": cfg.get("api_key_env", "MIMO_API_KEY"),
    }


_ASR_CFG = _load_asr_config()
MIMO_API_KEY = os.getenv(_ASR_CFG["api_key_env"])
MIMO_ASR_URL = _ASR_CFG["base_url"]
MIMO_ASR_MODEL = _ASR_CFG["model"]

MAX_BASE64_BYTES = 10 * 1024 * 1024
ALLOWED_EXTS = {".wav", ".mp3"}
RETRY_STATUSES = {429, 500, 502, 503}


class MimoAsrError(Exception):
    """Raised when MIMO ASR fails after retries."""


def analyze_audio(audio_path: str, language: str = "auto", timeout: int = 600) -> str:
    """Transcribe one audio file via MiMo ASR; returns plain text."""
    if not MIMO_API_KEY:
        raise MimoAsrError("MIMO_API_KEY not set in .env")
    path = Path(audio_path)
    if path.suffix.lower() not in ALLOWED_EXTS:
        raise MimoAsrError(f"unsupported audio format '{path.suffix}' (wav/mp3 only)")
    raw = path.read_bytes()
    b64_size = (len(raw) + 2) // 3 * 4
    if b64_size > MAX_BASE64_BYTES:
        raise MimoAsrError(
            f"audio too large: base64 ~{b64_size // 1024 // 1024}MB > 10MB limit; split into shorter segments"
        )
    payload = {
        "model": MIMO_ASR_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": f"data:audio/{path.suffix.lstrip('.')};base64," + base64.b64encode(raw).decode()},
                    }
                ],
            }
        ],
        "asr_options": {"language": language},
    }
    last_exc: requests.HTTPError | None = None
    for attempt in range(3):
        try:
            resp = requests.post(
                MIMO_ASR_URL,
                headers={"api-key": MIMO_API_KEY, "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            break
        except requests.HTTPError as exc:
            last_exc = exc
            if exc.response.status_code not in RETRY_STATUSES or attempt == 2:
                raise MimoAsrError(f"MIMO ASR error {exc.response.status_code}: {exc.response.text[:300]}") from exc
            time.sleep(5 * (attempt + 1))
    else:
        raise MimoAsrError(f"MIMO ASR request failed: {last_exc}") from last_exc

    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise MimoAsrError(f"unexpected MIMO ASR response: {str(data)[:300]}") from exc


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python mimo_asr.py <audio.wav|mp3> [language]")
        sys.exit(1)
    lang = sys.argv[2] if len(sys.argv) > 2 else "auto"
    print(analyze_audio(sys.argv[1], lang))
