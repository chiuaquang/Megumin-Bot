"""
rich_text.py — đọc chữ từ tin nhắn "Rich Message" (Bot API 10.1+).

Bot AI (Mira, Lục Tuyết Kỳ...) thường gửi/stream câu trả lời dưới dạng Rich Message:
chữ nằm trong trường `rich_message`, còn `msg.text` và `msg.caption` thì RỖNG.
Bản python-telegram-bot cũ không biết trường này nên bot "thấy" tin nhưng không có nội dung.
Module này lấy chữ ra từ `rich_message` (hoặc từ msg.api_kwargs nếu thư viện chưa hỗ trợ).
Không phụ thuộc module nào khác trong dự án.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Các khoá chỉ là metadata, không phải nội dung chữ.
_SKIP_KEYS = {
    "type", "url", "id", "file_id", "file_unique_id", "file_name", "mime_type",
    "language", "custom_emoji_id", "anchor", "name", "unix_time", "date_time_format",
    "thumbnail", "user_id", "width", "height", "duration", "size", "file_size",
}


def _raw_rich(msg):
    rm = getattr(msg, "rich_message", None)
    if rm is None:
        rm = (getattr(msg, "api_kwargs", None) or {}).get("rich_message")
    if rm is not None and hasattr(rm, "to_dict"):
        rm = rm.to_dict()
    return rm


def _collect(node, out):
    if isinstance(node, str):
        if node.strip():
            out.append(node)
    elif isinstance(node, dict):
        if "thinking" in str(node.get("type", "")).lower():
            return                      # bỏ khối "đang suy nghĩ" của AI
        for k, v in node.items():
            if k in _SKIP_KEYS or "thinking" in k.lower():
                continue
            _collect(v, out)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _collect(v, out)


def _join(parts) -> str:
    t = re.sub(r"[ \t]+", " ", " ".join(parts)).strip()
    return re.sub(r"\s+([,.!?;:…)\]])", r"\1", t)


def get_rich_text(msg) -> str:
    """Chữ của Rich Message ('' nếu tin không phải Rich Message)."""
    try:
        rm = _raw_rich(msg)
        if not rm:
            return ""
        blocks = rm.get("blocks") if isinstance(rm, dict) else None
        if isinstance(blocks, list) and blocks:
            lines = []
            for b in blocks:
                parts = []
                _collect(b, parts)
                if parts:
                    lines.append(_join(parts))
            return "\n".join(lines).strip()
        parts = []
        _collect(rm, parts)
        return _join(parts)
    except Exception as e:                       # không bao giờ làm hỏng luồng chính
        logger.error("[rich_text] lỗi đọc rich_message: %s", e)
        return ""


def message_text(msg) -> str:
    """text → caption → rich_message."""
    return (getattr(msg, "text", None) or getattr(msg, "caption", None) or get_rich_text(msg) or "")


def log_empty(msg) -> None:
    """Ghi log các trường của tin rỗng để biết đó là loại tin gì."""
    try:
        u = getattr(msg, "from_user", None)
        d = msg.to_dict()
        skip = {"message_id", "date", "chat", "from", "message_thread_id", "is_topic_message"}
        logger.info("[rich_text] tin rỗng từ @%s (bot=%s): %s",
                    getattr(u, "username", None), getattr(u, "is_bot", None),
                    [k for k in d if k not in skip])
    except Exception:
        pass
