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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm_codec  # noqa: E402  (同目录, 协议编码层)


def _load_asr_config() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"ASR config not found: {CONFIG_PATH}")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return cfg.get("asr", {})


def analyze_audio(audio_path: str, language: str = "auto", timeout: int = 600) -> str:
    """Transcribe an audio file via the configured ASR provider. Returns text.

    主通道 + fallback 链依次尝试; 降级打印一行, 不静默。
    """
    cfg = _load_asr_config()
    blocks = [{"type": "audio", "path": audio_path}]
    chain = [cfg] + [c for c in (cfg.get("fallback") or []) if isinstance(c, dict)]
    errors: list[str] = []
    for i, c in enumerate(chain):
        params = dict(c.get("params") or {})
        # MIMO 走 chat 格式时语言在私有 asr_options 里 (OpenAI chat 无对应字段)
        if isinstance(params.get("asr_options"), dict):
            params["asr_options"] = {**params["asr_options"], "language": language}
        try:
            text = llm_codec.call({**c, "params": params}, blocks, language=language,
                                  timeout=c.get("timeout", timeout))
            if i:
                print(f"[asr] 主通道失败, 已降级到 {c.get('provider')}", file=sys.stderr)
            return text
        except Exception as exc:  # noqa: BLE001 - 任一通道失败都继续试下一个
            errors.append(f"{c.get('provider')}: {exc}")
            if i + 1 < len(chain):
                print(f"[asr] {c.get('provider')} 失败 ({exc}), 试 fallback", file=sys.stderr)
    raise RuntimeError("所有 ASR 通道均失败: " + " | ".join(errors))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: asr.py <audio> [lang]")
        sys.exit(1)
    audio = sys.argv[1]
    lang = sys.argv[2] if len(sys.argv) > 2 else "auto"
    print(analyze_audio(audio, language=lang))
