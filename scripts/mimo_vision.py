"""MIMO Vision Bridge — image recognition via MIMO Anthropic-compatible API.

Usage:
    from analysis.mimo_vision import analyze_image
    result = analyze_image("path/to/image.jpg", "描述这张图片")

Config:
    MIMO_API_KEY in .env file at project root
"""

import os, base64, json, requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

def _load_vision_config() -> dict:
    """Read vision provider config from config/multimodal.json.

    Swappable model: change provider/model/base_url there; the API key
    env var name lives in the config but the key itself only ever comes
    from the environment (.env) — never from the json file.
    """
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "multimodal.json"
    try:
        import json as _json
        cfg = _json.loads(cfg_path.read_text(encoding="utf-8")).get("vision", {})
    except Exception:
        cfg = {}
    return {
        "model": cfg.get("model", "mimo-v2.5"),
        "base_url": cfg.get("base_url", "https://api.xiaomimimo.com/anthropic/v1/messages"),
        "api_key_env": cfg.get("api_key_env", "MIMO_API_KEY"),
    }


_VISION_CFG = _load_vision_config()
MIMO_API_KEY = os.getenv(_VISION_CFG["api_key_env"])
MIMO_BASE_URL = _VISION_CFG["base_url"]
MIMO_MODEL = _VISION_CFG["model"]

_MIME_MAP = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}


def encode_image(image_path: str) -> tuple[str, str]:
    ext = Path(image_path).suffix.lower()
    media_type = _MIME_MAP.get(ext, "image/jpeg")
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8"), media_type


def analyze_image(image_path: str, prompt: str = "请详细描述这张图片的内容",
                  max_tokens: int = 2048, system: str = "",
                  enable_thinking: bool = False) -> str:
    """Send an image to MIMO for analysis.

    Args:
        image_path: Path to image file
        prompt: Question about the image
        max_tokens: Max response tokens (thinking disabled by default, so all tokens go to text)
        system: Optional system prompt
        enable_thinking: Set True to enable MIMO thinking mode (consumes extra tokens)

    Returns:
        MIMO's text response
    """
    if not MIMO_API_KEY:
        raise RuntimeError("MIMO_API_KEY not set in .env")

    b64_data, media_type = encode_image(image_path)
    content_parts = [
        {"type": "image", "source": {
            "type": "base64", "media_type": media_type, "data": b64_data
        }},
        {"type": "text", "text": prompt},
    ]

    payload = {
        "model": MIMO_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content_parts}],
    }
    if system:
        payload["system"] = system
    if not enable_thinking:
        payload["thinking"] = {"type": "disabled"}

    resp = requests.post(
        MIMO_BASE_URL,
        headers={"api-key": MIMO_API_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"MIMO API error {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    # MIMO returns: content = [{type: "text", text: "..."}] or [{type: "thinking", ...}, {type: "text", ...}]
    for part in data.get("content", []):
        if part.get("type") == "text":
            return part["text"]
    # Fallback: if only thinking was returned (max_tokens exhausted)
    for part in data.get("content", []):
        if part.get("type") == "thinking":
            return f"[THINKING ONLY - increase max_tokens]\n{part.get('thinking', '')[:500]}"
    return str(data)


def analyze_images(image_paths: list[str], prompt: str = "请详细描述这些图片的内容",
                   max_tokens: int = 4096, system: str = "") -> str:
    """Send multiple images to MIMO for analysis."""
    if not MIMO_API_KEY:
        raise RuntimeError("MIMO_API_KEY not set in .env")

    content_parts = []
    for ip in image_paths:
        b64_data, media_type = encode_image(ip)
        content_parts.append({
            "type": "image", "source": {
                "type": "base64", "media_type": media_type, "data": b64_data
            }
        })
    content_parts.append({"type": "text", "text": prompt})

    payload = {
        "model": MIMO_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content_parts}],
    }
    if system:
        payload["system"] = system
    payload["thinking"] = {"type": "disabled"}

    resp = requests.post(
        MIMO_BASE_URL,
        headers={"api-key": MIMO_API_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"MIMO API error {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    for part in data.get("content", []):
        if part.get("type") == "text":
            return part["text"]
    return str(data)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python mimo_vision.py <image_path> [prompt]")
        sys.exit(1)
    path = sys.argv[1]
    p = sys.argv[2] if len(sys.argv) > 2 else "请详细描述这张图片的内容"
    result = analyze_image(path, p)
    print(result)
