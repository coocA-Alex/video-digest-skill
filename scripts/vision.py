"""Neutral vision entry point: route image analysis to a configured provider.

Provider is selected by config/multimodal.json -> vision.provider:
- "mimo"        (default)  -> delegate to mimo_vision (Anthropic-compatible messages)
- "codex-cli"   -> OpenAI Codex CLI with ChatGPT OAuth login (no API key needed);
                    runs `codex exec -i <image> "<prompt>" --output-last-message`
- "openai-responses" -> OpenAI Responses API image input (requires OPENAI_API_KEY;
                    config sample in config/multimodal.openai.example.json, adapter pending)

Output contract is a plain str, identical for every provider, so callers
(video_vision.py / xhs_note.py) never need to know which provider is active.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "multimodal.json"
LOCAL_CONFIG_PATH = PROJECT_ROOT / "config" / "multimodal.local.json"


def _load_vision_config() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"vision config not found: {CONFIG_PATH}")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    # 本地私有覆盖 (gitignored): 只合并本机字段 (如 codex_home), 不含任何 key
    if LOCAL_CONFIG_PATH.exists():
        local = json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(local, dict):
            for k, v in local.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
    return cfg.get("vision", {})


def _resolve_codex_home(cfg: dict) -> str | None:
    """Locate the Codex config dir that holds auth.json for `codex exec`.

    Precedence: multimodal.local.json vision.codex_home -> env CODEX_HOME
    -> ~/.codex. Without it the CLI falls back to API-key auth (401).
    """
    for cand in (cfg.get("codex_home"), os.environ.get("CODEX_HOME")):
        if cand and os.path.isdir(str(cand)):
            return str(cand)
    home_dot = str(Path.home() / ".codex")
    return home_dot if os.path.isdir(home_dot) else None


def _delegate_mimo():
    from mimo_vision import analyze_image as _mimo_image
    from mimo_vision import analyze_images as _mimo_images

    return _mimo_image, _mimo_images


def _codex_cli_images(image_paths: list[str], prompt: str, timeout: int = 600) -> str:
    """Analyze images via `codex exec -i` reusing the ChatGPT OAuth login.

    `--output-last-message` requires an output FILE path (not a bare flag);
    the prompt is the trailing positional argument. Runs ephemeral with a
    read-only sandbox so the call leaves no session files behind.
    Codex CLI is not built for batch vision; a single call is one agent
    turn, so batch size should stay small (callers already cap frame counts).
    """
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("codex CLI not found in PATH (provider=codex-cli)")
    base = [codex]
    if codex.lower().endswith(".cmd"):  # npm shim: run codex.js via node directly
        js = Path(codex).parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        if js.exists():
            base = ["node", str(js)]
    out_file = Path(tempfile.mkstemp(suffix=".txt")[1])
    codex_home = _resolve_codex_home(_load_vision_config())
    env = dict(os.environ)
    if codex_home:
        env["CODEX_HOME"] = codex_home
    else:
        raise RuntimeError(
            "codex auth not found: set vision.codex_home in config/multimodal.local.json "
            "or CODEX_HOME env (codex exec needs the dir holding auth.json)"
        )
    try:
        cmd = base + ["exec", "--ephemeral", "--skip-git-repo-check", "--sandbox", "read-only"]
        for ip in image_paths:
            cmd += ["-i", str(ip)]
        cmd += ["--output-last-message", str(out_file), prompt]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8",
                env=env, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"codex exec timed out after {timeout}s")
        if proc.returncode != 0:
            raise RuntimeError(f"codex exec failed ({proc.returncode}): {proc.stderr[-500:]}")
        if not out_file.exists() or not out_file.stat().st_size:
            raise RuntimeError("codex exec finished but produced no output message")
        return out_file.read_text(encoding="utf-8").strip()
    finally:
        try:
            out_file.unlink()
        except OSError:
            pass


def analyze_image(image_path: str, prompt: str = "请详细描述这张图片的内容",
                  max_tokens: int = 2048, system: str = "",
                  enable_thinking: bool = False) -> str:
    """Send one image to the configured vision provider. Returns text."""
    provider = _load_vision_config().get("provider", "mimo")
    if provider == "mimo":
        fn, _ = _delegate_mimo()
        return fn(image_path, prompt, max_tokens=max_tokens, system=system,
                  enable_thinking=enable_thinking)
    if provider == "codex-cli":
        return _codex_cli_images([image_path], prompt)
    raise RuntimeError(
        f"vision provider '{provider}' not implemented; "
        "supported: mimo, codex-cli (openai-responses needs OPENAI_API_KEY, stage B)"
    )


def analyze_images(image_paths: list[str], prompt: str = "请详细描述这些图片的内容",
                   max_tokens: int = 4096, system: str = "") -> str:
    """Send multiple images to the configured vision provider. Returns text."""
    if not image_paths:
        raise ValueError("no image paths given")
    provider = _load_vision_config().get("provider", "mimo")
    if provider == "mimo":
        _, fn = _delegate_mimo()
        return fn(image_paths, prompt, max_tokens=max_tokens, system=system)
    if provider == "codex-cli":
        return _codex_cli_images(image_paths, prompt)
    raise RuntimeError(
        f"vision provider '{provider}' not implemented; "
        "supported: mimo, codex-cli (openai-responses needs OPENAI_API_KEY, stage B)"
    )


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("usage: vision.py <image...> [--prompt <text>]")
        sys.exit(1)
    p = ["--prompt"] if "--prompt" in args else None
    if p:
        i = args.index("--prompt")
        prompt = " ".join(args[i + 1:])
        paths = args[:i]
    else:
        prompt = "请详细描述这张图片的内容"
        paths = args
    print(analyze_images(paths, prompt))
