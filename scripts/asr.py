"""Neutral ASR entry point: route audio transcription to a configured provider.

Provider is selected by config/multimodal.json -> asr.provider:
- "mimo"  (default) -> delegate to mimo_asr (mimo-v2.5-asr)
- others (openai-transcriptions) need OPENAI_API_KEY and are stage B;
  codex-cli does NOT support audio input (verified 2026-08-29).

Output contract is a plain str, identical for every provider.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "multimodal.json"


def _load_asr_config() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"ASR config not found: {CONFIG_PATH}")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return cfg.get("asr", {})


def analyze_audio(audio_path: str, language: str = "auto", timeout: int = 600) -> str:
    """Transcribe an audio file via the configured ASR provider. Returns text."""
    provider = _load_asr_config().get("provider", "mimo")
    if provider == "mimo":
        import mimo_asr  # noqa: E402

        return mimo_asr.analyze_audio(audio_path, language=language, timeout=timeout)
    raise RuntimeError(
        f"ASR provider '{provider}' not implemented; "
        "supported: mimo (openai-transcriptions needs OPENAI_API_KEY, stage B; "
        "codex-cli has no audio input)"
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: asr.py <audio> [lang]")
        sys.exit(1)
    audio = sys.argv[1]
    lang = sys.argv[2] if len(sys.argv) > 2 else "auto"
    print(analyze_audio(audio, language=lang))
