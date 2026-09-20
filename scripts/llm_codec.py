"""协议编码层: 把内部内容块翻译成各家 API 的请求格式, 并解析响应。

内部内容块 (IR) 只有 3 种:
    {"type": "text",  "text": str}
    {"type": "image", "path": str}
    {"type": "audio", "path": str}

支持的协议:
    anthropic_messages   Anthropic Messages (image.source.base64; max_tokens)
    openai_chat          OpenAI 兼容 chat/completions (image_url / input_audio)
    transcriptions       multipart/form-data 音频转写 (OpenAI whisper / 智谱 GLM ASR)

分工: 调用方只传 IR 不关心协议; 换模型 = 改 config/multimodal.json 的
protocol/auth/params/limits, 不需要改代码。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# .env 里有 MIMO_API_KEY; 原来由 mimo_vision/mimo_asr 各自 load_dotenv,
# 现在这两条老路径不再被调用, 所以在这里统一加载 (缺 dotenv 包不致命)
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

# 与 season 取数同口径的退避梯度
RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_BACKOFF = (30, 60, 90)

_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}


class CodecError(Exception):
    """协议层失败: 编码、限制校验或响应解析。"""


# 配置允许出现的字段 (下划线开头的注释键不受限)
KNOWN_KEYS = {
    "provider", "protocol", "model", "base_url", "api_key_env", "api_key_file",
    "auth", "params", "limits", "fallback", "token_param", "timeout", "codex_home",
}

# limits 子键白名单: 顶层拼错会报错, 子键拼错同样不能静默不生效
KNOWN_LIMITS = {
    "max_images", "max_image_bytes", "max_audio_bytes", "max_audio_seconds",
    "allowed_audio_exts",
}


def check_config_keys(config: dict, require_protocol: bool = True) -> None:
    """校验配置字段名。拼错 (如 protocal) 会静默走默认值, 是最难查的一类 bug。"""
    unknown = {k for k in config if not k.startswith("_")} - KNOWN_KEYS
    if unknown:
        raise CodecError(
            f"配置含未知字段 {sorted(unknown)} —— 可能拼写错误; 已知字段: {sorted(KNOWN_KEYS)}"
        )
    if require_protocol and not config.get("protocol"):
        raise CodecError(
            f"配置缺 protocol 字段 (provider={config.get('provider')}, "
            f"base_url={config.get('base_url')}) —— 见 config/multimodal.json 示例"
        )
    limits = config.get("limits") or {}
    bad_limits = {k for k in limits if not k.startswith("_")} - KNOWN_LIMITS
    if bad_limits:
        raise CodecError(f"limits 含未知字段 {sorted(bad_limits)}; 已知: {sorted(KNOWN_LIMITS)}")


def resolve_key(config: dict) -> str:
    """取 key: 环境变量优先, 其次 api_key_file (相对路径按项目根解析)。

    项目里 DeepSeek 的 key 不在 .env 而在 config/ds_key.local.json (gitignored),
    所以配置用 api_key_env + api_key_file 两者兜。
    """
    key = os.getenv(config.get("api_key_env", ""), "")
    if key:
        return key
    key_file = config.get("api_key_file")
    if not key_file:
        return ""
    path = Path(key_file)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path = path.resolve()
    # 限定在项目内: 否则配置可指向任意路径, 读到别的 api_key 再发往任意 base_url
    if not path.is_relative_to(PROJECT_ROOT):
        raise CodecError(f"api_key_file 必须位于项目目录内: {key_file}")
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("api_key", "") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def auth_headers(style: str, key: str) -> dict[str, str]:
    """认证头风格: bearer / x-api-key / api-key (三家供应商各一种)。"""
    if style == "bearer":
        return {"Authorization": f"Bearer {key}"}
    if style in ("x-api-key", "api-key"):
        return {style: key}
    raise CodecError(f"未知 auth 风格 {style!r} (可选 bearer / x-api-key / api-key)")


def _b64_size(path: Path) -> int:
    return (path.stat().st_size + 2) // 3 * 4


def _audio_duration(path: Path) -> float | None:
    """ffprobe 读时长; 取不到返回 None (只在配了 max_audio_seconds 时才会被调用)。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def check_limits(blocks: list[dict], limits: dict) -> None:
    """发送前校验硬限制。超限显式报错 —— 不静默丢弃、不留给 API 报 400。"""
    if not limits:
        return
    images = [b for b in blocks if b["type"] == "image"]
    audios = [b for b in blocks if b["type"] == "audio"]

    max_images = limits.get("max_images")
    if max_images and len(images) > max_images:
        raise CodecError(f"图片数 {len(images)} 超过上限 {max_images}")

    max_image_bytes = limits.get("max_image_bytes")
    if max_image_bytes:
        for b in images:
            size = _b64_size(Path(b["path"]))
            if size > max_image_bytes:
                raise CodecError(
                    f"图片 base64 {size // 1024 // 1024}MB 超过上限 "
                    f"{max_image_bytes // 1024 // 1024}MB: {Path(b['path']).name}"
                )

    exts = {e.lower() for e in limits.get("allowed_audio_exts", [])}
    max_audio_bytes = limits.get("max_audio_bytes")
    max_audio_seconds = limits.get("max_audio_seconds")
    for b in audios:
        path = Path(b["path"])
        if exts and path.suffix.lower() not in exts:
            raise CodecError(f"音频格式 {path.suffix} 不在允许列表 {sorted(exts)}: {path.name}")
        if max_audio_bytes and _b64_size(path) > max_audio_bytes:
            raise CodecError(
                f"音频 base64 ~{_b64_size(path) // 1024 // 1024}MB 超过上限 "
                f"{max_audio_bytes // 1024 // 1024}MB: {path.name} (需更短分段)"
            )
        if max_audio_seconds:
            duration = _audio_duration(path)
            if duration is None:
                # 取不到时长就拒发: 否则这条限制会静默不生效 (而它通常正是接入动机)
                raise CodecError(
                    f"无法读取音频时长 (ffprobe 不可用?), 而配置声明了 "
                    f"max_audio_seconds={max_audio_seconds} —— 拒绝发送以免超限: {path.name}"
                )
            if duration > max_audio_seconds:
                raise CodecError(
                    f"音频时长 {duration:.0f}s 超过上限 {max_audio_seconds}s: {path.name} "
                    f"(该模型需更短分段)"
                )


def _image_block(path: str) -> tuple[str, str]:
    p = Path(path)
    return base64.b64encode(p.read_bytes()).decode(), _MIME.get(p.suffix.lower(), "image/jpeg")


def encode(protocol: str, model: str, blocks: list[dict], prompt: str = "",
           system: str = "", max_tokens: int | None = None, params: dict | None = None,
           token_param: str = "max_tokens", language: str = "auto") -> tuple[dict, dict | None]:
    """IR → (payload, files)。files 非 None 时该请求走 multipart。"""
    if protocol == "anthropic_messages":
        content: list[dict] = []
        for b in blocks:
            if b["type"] == "text":
                content.append({"type": "text", "text": b["text"]})
            elif b["type"] == "image":
                data, media_type = _image_block(b["path"])
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": media_type, "data": data}})
            else:
                raise CodecError(
                    "anthropic_messages 协议没有音频内容块 —— 音频请用 openai_chat 或 transcriptions"
                )
        if prompt:
            content.append({"type": "text", "text": prompt})
        # params 先打底、固定字段后覆盖: 防 params 里的同名键静默遮蔽 model/messages
        payload: dict = dict(params or {})
        payload["model"] = model
        payload["messages"] = [{"role": "user", "content": content}]
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if system:
            payload["system"] = system
        return payload, None

    if protocol == "openai_chat":
        content = []
        for b in blocks:
            if b["type"] == "text":
                content.append({"type": "text", "text": b["text"]})
            elif b["type"] == "image":
                data, media_type = _image_block(b["path"])
                content.append({"type": "image_url", "image_url": {
                    "url": f"data:{media_type};base64,{data}"}})
            else:
                p = Path(b["path"])
                ext = p.suffix.lstrip(".").lower()
                data = base64.b64encode(p.read_bytes()).decode()
                content.append({"type": "input_audio", "input_audio": {
                    "data": f"data:audio/{ext};base64,{data}", "format": ext}})
        if prompt:
            content.append({"type": "text", "text": prompt})
        messages = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": content})
        payload: dict = dict(params or {})
        payload["model"] = model
        payload["messages"] = messages
        if max_tokens:
            payload[token_param] = max_tokens
        return payload, None

    if protocol == "transcriptions":
        audios = [b for b in blocks if b["type"] == "audio"]
        if len(audios) != 1:
            raise CodecError(f"transcriptions 协议需要恰好 1 个音频块, 收到 {len(audios)} 个")
        p = Path(audios[0]["path"])
        data: dict = dict(params or {})
        data["model"] = model
        if language and language != "auto":
            data["language"] = language
        files = {"file": (p.name, p.read_bytes(), f"audio/{p.suffix.lstrip('.').lower()}")}
        return data, files

    raise CodecError(
        f"未知协议 {protocol!r} (可选 anthropic_messages / openai_chat / transcriptions)"
    )


def decode(protocol: str, data: dict) -> str:
    """响应 JSON → 纯文本。"""
    if protocol == "transcriptions":
        text = data.get("text")
        if not text:
            raise CodecError(f"转写响应无 text 字段: {str(data)[:300]}")
        return str(text).strip()

    if protocol == "anthropic_messages":
        parts = data.get("content") or []
        for part in parts:
            if part.get("type") == "text":
                return part["text"]
        # 思考型模型 max_tokens 被推理吃光时会只剩 thinking 块
        for part in parts:
            if part.get("type") == "thinking":
                return f"[THINKING ONLY - increase max_tokens]\n{str(part.get('thinking', ''))[:500]}"
        raise CodecError(f"响应里没有文本块: {str(data)[:300]}")

    if protocol == "openai_chat":
        try:
            return str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise CodecError(f"响应结构异常: {str(data)[:300]}") from exc

    raise CodecError(f"未知协议 {protocol!r}")


def call(config: dict, blocks: list[dict], prompt: str = "", system: str = "",
         max_tokens: int | None = None, language: str = "auto", timeout: int = 180) -> str:
    """按一段配置发一次请求: 取 key → 校验限制 → 编码 → 请求 → 解析。

    配置缺 protocol 时显式报错 (而不是猜 base_url) —— 老配置请对照
    config/creators.example.json 同级的 multimodal.json 补齐 protocol/auth。
    """
    check_config_keys(config)
    protocol = config["protocol"]
    key = resolve_key(config)
    if not key:
        raise CodecError(
            f"取不到 key: 环境变量 {config.get('api_key_env')!r} 未设置"
            f"{', 且 ' + repr(config.get('api_key_file')) + ' 不可读/无 api_key' if config.get('api_key_file') else ''}"
            f" (provider={config.get('provider')})"
        )

    check_limits(blocks, config.get("limits") or {})
    payload, files = encode(
        protocol, config.get("model", ""), blocks, prompt=prompt, system=system,
        max_tokens=max_tokens, params=config.get("params"),
        token_param=config.get("token_param", "max_tokens"), language=language,
    )
    headers = auth_headers(config.get("auth", "bearer"), key)
    return request(config["base_url"], protocol, payload, files, headers, timeout=timeout)


def request(url: str, protocol: str, payload: dict, files: dict | None, headers: dict,
            timeout: int = 180) -> str:
    """发请求 → 退避重试 (429/5xx) → 解析。返回文本。

    错误信息只带 HTTP 状态与响应体片段 —— 不带 URL/凭证, 可安全进日志。
    """
    last_exc: Exception | None = None
    for attempt in range(len(RETRY_BACKOFF) + 1):
        try:
            if files is not None:
                resp = requests.post(url, headers=headers, data=payload, files=files, timeout=timeout)
            else:
                resp = requests.post(
                    url, headers={**headers, "Content-Type": "application/json"},
                    json=payload, timeout=timeout,
                )
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= len(RETRY_BACKOFF):
                break
            time.sleep(RETRY_BACKOFF[attempt])
            continue
        if resp.status_code in RETRY_STATUSES and attempt < len(RETRY_BACKOFF):
            time.sleep(RETRY_BACKOFF[attempt])
            continue
        if resp.status_code != 200:
            raise CodecError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return decode(protocol, resp.json())
    raise CodecError(
        f"请求失败 (已重试 {len(RETRY_BACKOFF)} 次): {type(last_exc).__name__ if last_exc else '上游持续不可用'}"
    )
