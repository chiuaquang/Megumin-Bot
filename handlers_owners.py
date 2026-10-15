"""
handlers_owners.py — Quản lý owner phụ + cấu hình bối cảnh/lịch sử nhóm cho AI.

  /addowner <id | @username | reply>   Thêm owner phụ        (CHỈ owner chính)
  /delowner <id | @username | reply>   Xoá owner phụ         (CHỈ owner chính)
  /owners                              Xem danh sách owner   (mọi owner)
  /aictx ...                           Bối cảnh/lịch sử AI   (mọi owner)

Owner phụ dùng bot đầy đủ như owner chính (is_owner() trả True) nhưng KHÔNG thể
thêm/xoá owner, và các lệnh này cố ý KHÔNG có trong danh sách tool của AI — để
không ai "nhờ AI" cấp quyền owner được (kể cả qua prompt-injection).
"""

import group_context
from telegram import Update
from telegram.ext import ContextTypes

from config import CONFIG, DB, save_config, normalize_owner_ids
from utils import (
    require_owner, safe_reply, is_owner, is_primary_owner, lookup_user_id_by_username,
)

MAX_EXTRA_OWNERS = 20


def _display(uid: int) -> str:
    info = (DB.get("users") or {}).get(str(uid)) or {}
    name = info.get("name") or ""
    uname = f" @{info['username']}" if info.get("username") else ""
    return f"{name}{uname}".strip() or "chưa thấy chat lần nào"


def _resolve_target(update: Update, args: list):
    """Lấy user id từ: reply > số id > @username đã được bot thấy chat. Trả (uid, is_bot, lỗi)."""
    msg = update.effective_message
    rp = getattr(msg, "reply_to_message", None)
    if rp is not None and rp.from_user is not None and not args:
        return rp.from_user.id, bool(rp.from_user.is_bot), ""
    if not args:
        return None, False, "Cách dùng: `/addowner 123456789` · `/addowner @username` · hoặc reply vào tin của người đó."
    raw = args[0].strip()
    if raw.lstrip("-").isdigit():
        uid = int(raw)
        if uid <= 0:
            return None, False, "ID người dùng phải là số dương (ID âm là của nhóm/kênh)."
        return uid, False, ""
    if raw.startswith("@"):
        uid = lookup_user_id_by_username(raw)
        if uid is None:
            return None, False, ("Chưa thấy @username này chat lần nào nên không tra ra ID. "
                                 "Hãy dùng ID số (xin họ nhắn @userinfobot để lấy).")
        return uid, False, ""
    return None, False, "Không hiểu đối tượng. Dùng ID số, @username hoặc reply."


async def cmd_addowner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_owner(user.id):
        return
    if not is_primary_owner(user.id):
        await safe_reply(update, "🔒 *Chỉ owner chính mới thêm/xoá được owner ạ~*")
        return

    uid, is_bot, err = _resolve_target(update, context.args or [])
    if err:
        await safe_reply(update, f"❌ {err}")
        return
    if is_bot:
        await safe_reply(update, "❌ *Không thể đặt bot làm owner ạ.*")
        return
    if is_primary_owner(uid):
        await safe_reply(update, "ℹ️ *Đó là owner chính rồi ạ~*")
        return

    current = normalize_owner_ids(CONFIG.get("extra_owners"))
    if uid in current:
        await safe_reply(update, f"ℹ️ *`{uid}` đã là owner phụ rồi ạ.*")
        return
    if len(current) >= MAX_EXTRA_OWNERS:
        await safe_reply(update, f"❌ *Đã đạt tối đa {MAX_EXTRA_OWNERS} owner phụ.* Xoá bớt bằng `/delowner`.")
        return

    CONFIG["extra_owners"] = current + [uid]     # gán list MỚI (không sửa list dùng chung)
    save_config(CONFIG)

    # Thử báo cho người mới — chỉ gửi được nếu họ đã /start bot ở tin nhắn riêng.
    notified = "✅ đã nhắn báo cho họ"
    try:
        await context.bot.send_message(
            uid, "🌸 Ngài vừa được thêm làm *owner* của bot này. Gõ /help để xem lệnh ạ~",
            parse_mode="Markdown",
        )
    except Exception:
        notified = "⚠️ chưa nhắn báo được (họ cần /start bot ở tin nhắn riêng trước)"

    await safe_reply(
        update,
        f"✅ *Đã thêm owner phụ:* `{uid}` — {_display(uid)}\n"
        f"{notified}\n"
        f"👑 Tổng owner phụ: {len(current) + 1}/{MAX_EXTRA_OWNERS}\n\n"
        f"_Lưu ý: owner phụ có đủ quyền dùng bot (kể cả lệnh quản trị nhóm qua AI), "
        f"chỉ không thêm/xoá được owner. Người ngoài vẫn bị chặn như cũ._"
    )


async def cmd_delowner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_owner(user.id):
        return
    if not is_primary_owner(user.id):
        await safe_reply(update, "🔒 *Chỉ owner chính mới thêm/xoá được owner ạ~*")
        return

    uid, _is_bot, err = _resolve_target(update, context.args or [])
    if err:
        await safe_reply(update, f"❌ {err.replace('/addowner', '/delowner')}")
        return
    if is_primary_owner(uid):
        await safe_reply(update, "❌ *Không thể xoá owner chính ạ.*")
        return

    current = normalize_owner_ids(CONFIG.get("extra_owners"))
    if uid not in current:
        await safe_reply(update, f"ℹ️ *`{uid}` không nằm trong danh sách owner phụ.*")
        return

    CONFIG["extra_owners"] = [x for x in current if x != uid]
    save_config(CONFIG)
    await safe_reply(update, f"✅ *Đã xoá owner phụ:* `{uid}` — {_display(uid)}")


async def cmd_owners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update):
        return
    primary = CONFIG.get("owner_id", 0)
    extra = normalize_owner_ids(CONFIG.get("extra_owners"))
    lines = [f"👑 *Owner chính:* `{primary}` — {_display(primary)}"]
    if extra:
        lines.append(f"\n🤝 *Owner phụ ({len(extra)}/{MAX_EXTRA_OWNERS}):*")
        lines += [f"• `{u}` — {_display(u)}" for u in extra]
    else:
        lines.append("\n🤝 *Owner phụ:* chưa có — thêm bằng `/addowner <id>`")
    await safe_reply(update, "\n".join(lines))


async def cmd_aictx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/aictx — xem · /aictx <số tin> — số tin bối cảnh (0=tắt) · /aictx chars <n>
    · /aictx log on|off — ghi lịch sử nhóm · /aictx clear — xoá lịch sử nhóm này."""
    if not await require_owner(update):
        return
    chat = update.effective_chat
    args = [a.lower() for a in (context.args or [])]
    target_chat = chat.id if chat.type != "private" else CONFIG.get("target_group_id", 0)

    if not args:
        ram = len(group_context.GROUP_HISTORY.get(target_chat, [])) if target_chat else 0
        await safe_reply(
            update,
            "🧠 *Bối cảnh & lịch sử nhóm cho AI*\n\n"
            f"• Số tin gần nhất đính kèm: `{CONFIG.get('ai_ctx_msgs', 30)}` _(0 = tắt)_\n"
            f"• Trần ký tự bối cảnh: `{CONFIG.get('ai_ctx_chars', 6000)}`\n"
            f"• Ghi lịch sử mọi nhóm: {'🟢 BẬT' if CONFIG.get('ai_log_groups', True) else '🔴 TẮT'}\n"
            f"• Tin đang có trong RAM (nhóm này): `{ram}`\n\n"
            "`/aictx 40` · `/aictx chars 8000` · `/aictx log off` · `/aictx clear`"
        )
        return

    if args[0] == "clear":
        if not target_chat:
            await safe_reply(update, "❌ Dùng trong nhóm, hoặc `/setgroup` trước khi dùng ở DM.")
            return
        n = group_context.clear_chat(target_chat)
        extra = f" ({n} dòng trong PostgreSQL)" if n >= 0 else " _(RAM — không có PostgreSQL)_"
        await safe_reply(update, f"🧹 *Đã xoá lịch sử chat của nhóm `{target_chat}`*{extra}")
        return

    if args[0] == "log" and len(args) > 1 and args[1] in ("on", "off"):
        CONFIG["ai_log_groups"] = args[1] == "on"
        save_config(CONFIG)
        await safe_reply(update, f"✅ *Ghi lịch sử nhóm:* {'🟢 BẬT' if CONFIG['ai_log_groups'] else '🔴 TẮT'}")
        return

    try:
        if args[0] == "chars" and len(args) > 1:
            val = int(args[1])
            if not 1000 <= val <= 20000:
                raise ValueError
            CONFIG["ai_ctx_chars"] = val
            save_config(CONFIG)
            await safe_reply(update, f"✅ *Trần ký tự bối cảnh:* `{val}`")
        else:
            val = int(args[0])
            if not 0 <= val <= 100:
                raise ValueError
            CONFIG["ai_ctx_msgs"] = val
            save_config(CONFIG)
            await safe_reply(update, f"✅ *Số tin bối cảnh:* `{val}`" + (" _(đã tắt)_" if val == 0 else ""))
    except ValueError:
        await safe_reply(update, "❌ Số tin: 0–100 · `chars`: 1000–20000. Ví dụ `/aictx 40`.")
