"""
claude_module.py — Nhà cung cấp AI thứ hai: Claude (Anthropic).

Gọi thẳng Messages API bằng httpx (đã có trong requirements.txt) nên KHÔNG cần
cài thêm thư viện. Dùng chung persona, lịch sử hội thoại (AI_HISTORY), bối cảnh
nhóm và bộ tool quản trị nhóm với phần Gemini trong ai_module.py.

Bật bằng `/aiprovider claude`, đặt key bằng `/claudekey KEY` (hoặc biến môi
trường ANTHROPIC_KEY). Nếu Claude lỗi (hết tiền, sai key, mạng...) thì
ai_module.py tự rơi về Gemini như cũ.

Lưu ý: API của Anthropic là dịch vụ TÍNH PHÍ (không có gói API miễn phí).
Dùng claude-haiku-5-5 cho rẻ nhất.
"""

import asyncio
import base64
import time

import httpx

from config import CONFIG
from utils import vlog

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

CLAUDE_MODELS = [
    "claude-haiku-5-5",    # rẻ + nhanh nhất
    "claude-sonnet-5-5",   # cân bằng
    "claude-opus-5-5",     # mạnh nhất, đắt nhất
]
DEFAULT_CLAUDE_MODEL = "claude-haiku-5-5"
MAX_TOKENS = 2048

# Sau khi Claude lỗi, tạm bỏ qua Claude một lúc để các nhánh fallback của
# Gemini không gọi lại Claude nhiều lần liên tiếp.
_COOLDOWN_SECS = 30
_fail_until = 0.0


class ClaudeUnavailable(Exception):
    """Claude không dùng được lượt này → caller nên rơi về Gemini."""


def get_key() -> str:
    return (CONFIG.get("claude_api_key") or "").strip()


def is_active() -> bool:
    """True nếu owner đã chọn provider=claude, có key và không đang cooldown."""
    return (
        CONFIG.get("ai_provider", "gemini") == "claude"
        and bool(get_key())
        and time.time() >= _fail_until
    )


def _model() -> str:
    return (CONFIG.get("claude_model") or DEFAULT_CLAUDE_MODEL).strip()


# ── Lịch sử: AI_HISTORY (role user/model, parts=[str]) → messages Claude ──
def _history_to_messages(history: list) -> list:
    msgs: list = []
    for h in history:
        text = "\n".join(str(p) for p in (h.get("parts") or []) if str(p).strip()).strip()
        if not text:
            continue
        role = "assistant" if h.get("role") == "model" else "user"
        if msgs and msgs[-1]["role"] == role:        # Claude yêu cầu xen kẽ user/assistant
            msgs[-1]["content"] += "\n" + text
        else:
            msgs.append({"role": role, "content": text})
    while msgs and msgs[0]["role"] != "user":        # phải bắt đầu bằng user
        msgs.pop(0)
    return msgs


def _extract_text(data: dict) -> str:
    return "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()


async def _post(payload: dict) -> dict:
    headers = {
        "x-api-key": get_key(),
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(API_URL, headers=headers, json=payload)
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", r.text)
        except Exception:
            msg = r.text
        raise RuntimeError(f"Claude API {r.status_code}: {msg[:300]}")
    return r.json()


def _save_history(chat_id: int, user_label: str, reply: str) -> None:
    from ai_module import AI_HISTORY, AI_MAX_HISTORY, _get_db, _bump_ai_stats
    history = AI_HISTORY.get(chat_id, [])
    history.append({"role": "user", "parts": [user_label]})
    history.append({"role": "model", "parts": [reply]})
    if len(history) > AI_MAX_HISTORY * 2:
        history = history[-(AI_MAX_HISTORY * 2):]
    AI_HISTORY[chat_id] = history
    db = _get_db()
    if db:
        db.save_history(chat_id, history)
    _bump_ai_stats()


def _mark_failed(e: Exception) -> None:
    global _fail_until
    _fail_until = time.time() + _COOLDOWN_SECS
    vlog(f"Claude lỗi → rơi về Gemini: {str(e)[:200]}")


# ── Chat thường (có tuỳ chọn tự search web qua DDG/Bing như bản Gemini) ──
async def chat(chat_id: int, user_text: str, with_search: bool = False) -> str:
    from ai_module import (AI_HISTORY, _gemini_get_sys_prompt, _should_ddg_search,
                           _ddg_fetch_results, _format_search_context)
    try:
        sys_prompt = _gemini_get_sys_prompt()
        if with_search and _should_ddg_search(user_text):
            try:
                results = await asyncio.wait_for(
                    _ddg_fetch_results(user_text, max_per_source=10), timeout=12)
                ctx = _format_search_context(results)
                if ctx:
                    sys_prompt = f"{ctx}\n\n{'─'*40}\n\n{sys_prompt}"
            except Exception:
                pass
        msgs = _history_to_messages(AI_HISTORY.get(chat_id, []))
        msgs.append({"role": "user", "content": user_text})
        data = await _post({
            "model": _model(), "max_tokens": MAX_TOKENS,
            "system": sys_prompt, "messages": msgs,
        })
        reply = _extract_text(data)
        if not reply:
            raise RuntimeError("Claude trả về nội dung rỗng")
        _save_history(chat_id, user_text, reply)
        return reply
    except Exception as e:
        _mark_failed(e)
        raise ClaudeUnavailable(str(e))


# ── Ảnh (vision) ──
async def chat_media(chat_id: int, media_parts: list, prompt_text: str,
                     history_label: str = "[Ảnh]") -> str:
    """media_parts: list[(bytes, mime)]. Claude chỉ nhận ảnh — audio/video thì
    để Gemini xử lý (ClaudeUnavailable)."""
    from ai_module import AI_HISTORY, _gemini_get_sys_prompt
    if any(not (m or "").startswith("image/") for _, m in media_parts):
        raise ClaudeUnavailable("Claude chưa hỗ trợ audio/video")
    try:
        content = [
            {"type": "image", "source": {
                "type": "base64",
                "media_type": m if m in ("image/jpeg", "image/png", "image/gif", "image/webp") else "image/jpeg",
                "data": base64.b64encode(b).decode(),
            }} for b, m in media_parts
        ]
        content.append({"type": "text", "text": prompt_text})
        msgs = _history_to_messages(AI_HISTORY.get(chat_id, []))
        msgs.append({"role": "user", "content": content})
        data = await _post({
            "model": _model(), "max_tokens": MAX_TOKENS,
            "system": _gemini_get_sys_prompt(), "messages": msgs,
        })
        reply = _extract_text(data)
        if not reply:
            raise RuntimeError("Claude trả về nội dung rỗng")
        _save_history(chat_id, f"{history_label} {prompt_text}", reply)
        return reply
    except Exception as e:
        _mark_failed(e)
        raise ClaudeUnavailable(str(e))


# ── Agent mode: tool quản trị nhóm (tái dùng khai báo tool của Gemini) ──
def _build_claude_tools() -> list:
    """Chuyển khai báo FunctionDeclaration của agent_tools sang schema tool của Claude."""
    from ai_module import _get_agent_tools, genai_types
    if genai_types is None:
        raise RuntimeError("Thiếu google-genai để đọc khai báo tool")
    tools = []
    for t in _get_agent_tools().build_gemini_tools(genai_types):
        for d in (t.function_declarations or []):
            props = {}
            sch = d.parameters
            for k, v in ((sch.properties or {}) if sch else {}).items():
                props[k] = {"type": "string", "description": v.description or ""}
            tools.append({
                "name": d.name,
                "description": d.description or d.name,
                "input_schema": {
                    "type": "object",
                    "properties": props,
                    "required": list((sch.required or []) if sch else []),
                },
            })
    return tools


async def chat_with_tools(chat_id: int, user_text: str, bot, action_chat_id: int,
                          extra_context: str = "") -> str:
    from ai_module import (AI_HISTORY, AGENT_MAX_TOOL_STEPS, _AGENT_REPORT_INSTRUCTION,
                           _gemini_get_sys_prompt, _fix_agent_reply_links, _get_agent_tools)
    try:
        agent_tools = _get_agent_tools()
        tools = _build_claude_tools()
        sys_prompt = (_gemini_get_sys_prompt() or "") + (extra_context or "") + _AGENT_REPORT_INSTRUCTION
        msgs = _history_to_messages(AI_HISTORY.get(chat_id, []))
        msgs.append({"role": "user", "content": user_text})
        tool_log: list = []
        reply = ""

        for step in range(AGENT_MAX_TOOL_STEPS + 1):
            await asyncio.sleep(0)  # checkpoint cho /cancel
            payload = {"model": _model(), "max_tokens": MAX_TOKENS,
                       "system": sys_prompt, "messages": msgs}
            if step < AGENT_MAX_TOOL_STEPS:       # bước cuối: không đưa tool → ép trả lời text
                payload["tools"] = tools
            data = await _post(payload)
            blocks = data.get("content", [])
            uses = [b for b in blocks if b.get("type") == "tool_use"]
            if not uses:
                reply = _extract_text(data)
                break

            msgs.append({"role": "assistant", "content": blocks})
            results = []
            for u in uses:
                args = dict(u.get("input") or {})
                for f in ("message_id", "topic_id"):
                    if f in args:
                        try:
                            args[f] = int(str(args[f]).strip() or "0")
                        except (ValueError, TypeError):
                            args[f] = 0
                try:
                    res = await agent_tools.execute_tool(u["name"], args, bot=bot, chat_id=action_chat_id)
                except TypeError as te:                 # model gửi sai tham số
                    res = {"ok": False, "error": f"Sai tham số: {te}"}
                tool_log.append({"tool": u["name"], "args": args, "result": res})
                results.append({"type": "tool_result", "tool_use_id": u["id"],
                                "content": str(res)})
            msgs.append({"role": "user", "content": results})

        if not reply:
            raise RuntimeError("Claude không trả về văn bản sau khi gọi tool")
        reply = _fix_agent_reply_links(reply, tool_log)
        _save_history(chat_id, user_text, reply)
        return reply
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # Nếu tool ĐÃ chạy thật thì KHÔNG rơi về Gemini (tránh làm lặp hành động ban/mute...)
        if locals().get("tool_log"):
            _mark_failed(e)
            return f"❌ *Lỗi Claude sau khi đã thực thi tool:* `{str(e)[:200]}`"
        _mark_failed(e)
        raise ClaudeUnavailable(str(e))


async def validate_key(key: str) -> tuple[bool, str]:
    """Gọi thử 1 request nhỏ để kiểm tra key."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(API_URL, headers={
                "x-api-key": key, "anthropic-version": API_VERSION,
                "content-type": "application/json"},
                json={"model": DEFAULT_CLAUDE_MODEL, "max_tokens": 8,
                      "messages": [{"role": "user", "content": "hi"}]})
        if r.status_code == 200:
            return True, ""
        try:
            return False, r.json().get("error", {}).get("message", r.text)[:200]
        except Exception:
            return False, r.text[:200]
    except Exception as e:
        return False, str(e)[:200]
