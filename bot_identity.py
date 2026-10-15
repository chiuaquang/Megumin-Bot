"""
bot_identity.py — bot biết mình là AI/bot nào (id + username lấy từ getMe lúc khởi động),
để phân biệt CHÍNH MÌNH với bot khác (kể cả bot chạy chung code).

Nhận diện bằng ID số của Telegram (không đổi, không trùng) — không dựa vào `bot_username`
trong config, vì config có thể trống hoặc bị dùng chung giữa các bot cùng code.
Không phụ thuộc module nào khác trong dự án.
"""

SELF_ID: int = 0
SELF_USERNAME: str = ""


def set_self(bot_id: int, username: str = "") -> None:
    global SELF_ID, SELF_USERNAME
    SELF_ID = int(bot_id or 0)
    SELF_USERNAME = (username or "").lstrip("@")


def is_self_id(uid) -> bool:
    return bool(SELF_ID) and uid == SELF_ID


def is_self_user(user) -> bool:
    """True nếu `user` (telegram.User) chính là bot này."""
    return user is not None and is_self_id(getattr(user, "id", 0))


def is_other_bot(user) -> bool:
    """True nếu `user` là một bot KHÁC (kể cả bot cùng code), không phải chính bot này."""
    return (user is not None and bool(getattr(user, "is_bot", False))
            and not is_self_user(user))
