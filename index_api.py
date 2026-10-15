"""index_api.py — 3 model Gemma qua API công khai (call_api.py):
    • Gemma 35B-A3B  (flagship)  → Index-Translate-35B-A3B
    • Gemma 9B       (cân bằng)  → Index-Translate-9B
    • Gemma 2B Fast  (siêu nhanh) → Index-Translate-2B

Endpoint OpenAI-compatible: https://index-translate.bilibili.com/v1 (không cần key).
Chỉ dùng thư viện chuẩn (urllib) — tái sử dụng DEFAULT_API_BASE / strip_think từ call_api.py.
Hỗ trợ text + ảnh (image_url, base64). KHÔNG hỗ trợ function-calling / audio / video.
"""

import asyncio
import base64
import json
import urllib.error
import urllib.request

from call_api import DEFAULT_API_BASE, strip_think
from config import CONFIG

# id thật trên API → tên hiển thị
INDEX_MODELS: dict = {
    "Index-Translate-35B-A3B": "Gemma 35B-A3B",
    "Index-Translate-9B":      "Gemma 9B",
    "Index-Translate-2B":      "Gemma 2B Fast",
}

# Gõ tắt cho /aimodel
INDEX_ALIASES: dict = {
    "gemma-35b": "Index-Translate-35B-A3B", "gemma-35b-a3b": "Index-Translate-35B-A3B",
    "gemma35b": "Index-Translate-35B-A3B", "35b": "Index-Translate-35B-A3B",
    "gemma-9b": "Index-Translate-9B", "gemma9b": "Index-Translate-9B", "9b": "Index-Translate-9B",
    "gemma-2b": "Index-Translate-2B", "gemma-2b-fast": "Index-Translate-2B",
    "gemma2b": "Index-Translate-2B", "2b": "Index-Translate-2B", "2b-fast": "Index-Translate-2B",
}

_CHAT_TEMPERATURE = 0.7
_CHAT_MAX_TOKENS = 2048
_TIMEOUT = 120
_MAX_HISTORY_MSGS = 20   # số lượt gần nhất gửi kèm (model nhỏ, tránh tràn context)


def is_index_model(name) -> bool:
    return bool(name) and name in INDEX_MODELS


def index_display_name(name: str) -> str:
    return INDEX_MODELS.get(name, name)


def resolve_index_alias(name: str) -> str:
    """'gemma-9b' / '2b' / 'Index-Translate-9B' → id chuẩn; không khớp thì trả nguyên."""
    return INDEX_ALIASES.get((name or "").strip().lower(), name)


def _history_to_messages(history: list) -> list:
    msgs = []
    for h in (history or [])[-_MAX_HISTORY_MSGS:]:
        role = "assistant" if h.get("role") == "model" else "user"
        text = "\n".join(str(p) for p in h.get("parts", []) if isinstance(p, str)).strip()
        if text:
            msgs.append({"role": role, "content": text})
    return msgs


def _post(model: str, messages: list, max_tokens: int, temperature: float) -> str:
    api_base = (CONFIG.get("index_api_base") or DEFAULT_API_BASE).rstrip("/")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        # Tắt CoT: giống call_api.py, tránh phần "suy nghĩ" thay thế câu trả lời.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        f"{api_base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Index-Translate-Client/1.0"},
    )
    last_err = None
    for attempt in range(2):                      # thử lại 1 lần với lỗi tạm thời
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                res = json.loads(resp.read().decode("utf-8"))
            text = strip_think(res["choices"][0]["message"]["content"] or "")
            if not text:
                raise RuntimeError("API trả về nội dung rỗng")
            return text
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300]
            last_err = RuntimeError(f"HTTP {e.code}: {body}")
            if e.code not in (429, 500, 502, 503, 504):
                break
        except Exception as e:
            last_err = e
    raise last_err


async def index_chat(model: str, system: str, history: list, user_text: str,
                     images: list = None) -> str:
    """Gọi model Index. `images`: list[(bytes, mime)] (tuỳ chọn). Raise nếu lỗi."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend(_history_to_messages(history))
    if images:
        content = [{"type": "text", "text": user_text}]
        for b, mime in images:
            b64 = base64.b64encode(b).decode("ascii")
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{mime or 'image/jpeg'};base64,{b64}"}})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": user_text})
    return await asyncio.to_thread(_post, model, messages, _CHAT_MAX_TOKENS, _CHAT_TEMPERATURE)
