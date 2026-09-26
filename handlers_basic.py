"""
handlers_basic.py — Các lệnh cơ bản: start, help, id, stats, info,
                     promote/demote (áp dụng cho bất kỳ user/bot nào),
                     ban/kick/mute, warn, note, filter, rules,
                     block, broadcast, proxy, setgroup, deldm/editdm,
                     lock/unlock, cancel, version, encstatus/encrekey.
"""

import asyncio
import re
import json
import io
import os
import time
import uuid
from datetime import datetime, timedelta

from telegram import (
    Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, User,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import (
    CONFIG, DB, save_config, save_db,
    CONFIG_FILE, KEY_FILE, now_vn, fmt_vn,
    CRYPTO_AVAILABLE, _SENSITIVE_FIELDS, _get_fernet,
    _encrypt_config, _decrypt_config,
)
from utils import (
    is_owner, is_bot_admin, get_chat_key, track_user, lookup_user_id_by_username,
    require_owner, safe_reply, maid, maid_reply,
    build_help_overview, build_help_keyboard,
    extract_msg_id_from_link, _action_kb, fmt_duration,
    fmt_bytes, get_process_memory_mb, get_db_size_bytes,
    BOT_VERSION, PROCESS_START,
    extract_message_album,
    set_phrase_media, del_phrase_media, get_phrase_media,
    set_thinking_media, del_thinking_media, get_thinking_media,
    MAID_PHRASES,
    parse_filter_spec, parse_filter_flags,
    merge_variants, is_group_scope,
    has_variant_marker, build_new_variants, normalize_variants, normalize_media_list,
    set_pending_overwrite, pop_pending_overwrite, cleanup_variant_media,
    register_filter_view,
    normalize_note, send_note_response, NOTES_FILTERS_KEY,
    to_markdown_v2, escape_markdown_v2,
)

try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None

# ── Warn limit ────────────────────────────────────────────────────
WARN_LIMIT = 3

# ════════════════════════════════════════════════════════════════
# TARGET RESOLUTION HELPERS
# ════════════════════════════════════════════════════════════════

async def resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         allow_username: bool = True):
    msg = update.message
    # ── Ưu tiên explicit target trong args (@username hoặc user_id số) ──
    # Phải kiểm tra TRƯỚC reply, vì nếu owner vừa reply vào tin A vừa gõ
    # `/ban @userB`, ý định rõ ràng là ban @userB — không phải người gửi A.
    # Logic cũ (kiểm tra reply trước) gây bug "ban nhầm người khi có @username".
    if context.args:
        raw = context.args[0]
        if allow_username and raw.startswith("@"):
            # 1) Tra cache nội bộ TRƯỚC — Telegram Bot API KHÔNG hỗ trợ
            #    get_chat_member() bằng username cho user thường (chỉ nhận
            #    user_id số), nên gọi thẳng API gần như luôn lỗi với member
            #    bình thường dù họ đang chat sống trong nhóm. Cache này được
            #    track_user() ghi lại từ mọi tin nhắn thật → đây là cách duy
            #    nhất (và cách Rose/Combot cũng dùng) để map @username → id.
            cached_id = lookup_user_id_by_username(raw)
            if cached_id is not None:
                try:
                    return (await context.bot.get_chat_member(update.effective_chat.id, cached_id)).user
                except TelegramError:
                    info = DB.get("users", {}).get(str(cached_id), {})
                    return User(id=cached_id, first_name=info.get("name", raw),
                                is_bot=False, username=raw.lstrip("@"))
            # 2) Fallback: thử thẳng Telegram (hiếm khi thành công với member
            #    thường, nhưng vẫn work nếu raw là public channel/group/bot).
            try:
                return (await context.bot.get_chat_member(update.effective_chat.id, raw)).user
            except TelegramError:
                try:
                    c = await context.bot.get_chat(raw)
                    return User(id=c.id, first_name=c.first_name or raw, is_bot=False,
                                last_name=c.last_name, username=c.username)
                except TelegramError:
                    return None
        if raw.lstrip("-").isdigit():
            try:
                return (await context.bot.get_chat_member(update.effective_chat.id, int(raw))).user
            except TelegramError:
                return None
    # ── Fallback: lấy target từ tin nhắn được reply (khi không có explicit target) ──
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user
    return None

get_target = resolve_target

async def _resolve_target_dm(update, context, group_id: int):
    if not context.args:
        return None
    raw = context.args[0]
    if raw.startswith("@"):
        cached_id = lookup_user_id_by_username(raw)
        if cached_id is not None:
            try:
                return (await context.bot.get_chat_member(group_id, cached_id)).user
            except TelegramError:
                info = DB.get("users", {}).get(str(cached_id), {})
                return User(id=cached_id, first_name=info.get("name", raw),
                            is_bot=False, username=raw.lstrip("@"))
        try:
            return (await context.bot.get_chat_member(group_id, raw)).user
        except TelegramError:
            try:
                c = await context.bot.get_chat(raw)
                return User(id=c.id, first_name=c.first_name or raw, is_bot=False,
                            last_name=c.last_name, username=c.username)
            except TelegramError:
                return None
    if raw.lstrip("-").isdigit():
        try:
            return (await context.bot.get_chat_member(group_id, int(raw))).user
        except TelegramError:
            return None
    return None

# ── Duration & Reason parsers ─────────────────────────────────────
def _parse_duration(args: list) -> int:
    for arg in args:
        if arg.isdigit():
            return int(arg)
        if len(arg) > 1 and arg[:-1].isdigit():
            n, u = int(arg[:-1]), arg[-1].lower()
            if u == "m": return n * 60
            if u == "h": return n * 3600
            if u == "d": return n * 86400
    return 0

def _extract_reason(args: list) -> str:
    reason_args = (
        args[1:] if args and (args[0].lstrip("-").isdigit() or args[0].startswith("@"))
        else args
    )
    return " ".join(reason_args) if reason_args else "Không có lý do"

# ── Promote/Demote helpers ────────────────────────────────────────
async def _do_promote_full(bot, chat_id: int, user_id: int) -> None:
    await bot.promote_chat_member(
        chat_id=chat_id, user_id=user_id, is_anonymous=False,
        can_manage_chat=True, can_post_messages=True, can_edit_messages=True,
        can_delete_messages=True, can_manage_video_chats=True,
        can_restrict_members=True, can_promote_members=True,
        can_change_info=True, can_invite_users=True,
        can_pin_messages=True, can_manage_topics=True,
        can_manage_tags=True,
        can_delete_stories=True,
        can_edit_stories=True,
        can_post_stories=True,
    )

async def _do_promote_basic(bot, chat_id: int, user_id: int) -> None:
    await bot.promote_chat_member(
        chat_id=chat_id, user_id=user_id, is_anonymous=False,
        can_manage_chat=True, can_post_messages=True, can_edit_messages=True,
        can_delete_messages=True, can_manage_video_chats=True,
        can_restrict_members=False, can_promote_members=False,
        can_change_info=False, can_invite_users=True,
        can_pin_messages=True, can_manage_topics=False, can_manage_tags=False,
    )

async def _do_demote(bot, chat_id: int, user_id: int) -> None:
    await bot.promote_chat_member(
        chat_id=chat_id, user_id=user_id, is_anonymous=False,
        can_manage_chat=False, can_post_messages=False, can_edit_messages=False,
        can_delete_messages=False, can_manage_video_chats=False,
        can_restrict_members=False, can_promote_members=False,
        can_change_info=False, can_invite_users=False,
        can_pin_messages=False, can_manage_topics=False,
        can_manage_tags=False,
        can_delete_stories=False,
        can_edit_stories=False,
        can_post_stories=False,
    )

# ════════════════════════════════════════════════════════════════
# BASIC COMMANDS
# ════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Deep-link từ nút {rules} của filter: /start rules_<chat_id> — bất kỳ ai
    # bấm nút cũng xem được, không cần là owner.
    if context.args and context.args[0].startswith("rules_"):
        track_user(update.effective_user)
        raw_id = context.args[0][len("rules_"):]
        rules = DB.get("rules", {}).get(raw_id)
        await safe_reply(
            update,
            f"📜 *NỘI QUY NHÓM:*\n\n{rules}" if rules
            else "📜 *Nhóm này chưa có nội quy ạ~*"
        )
        return
    if not await require_owner(update): return
    track_user(update.effective_user)
    await maid_reply(update, "welcome")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    await safe_reply(update, build_help_overview(), reply_markup=build_help_keyboard())

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    user = update.effective_user
    chat = update.effective_chat
    text = f"🆔 *ID của ngài:* `{user.id}`"
    if chat.type != "private":
        text += f"\n🏠 *ID nhóm:* `{chat.id}`"
    await safe_reply(update, text)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from utils import MSG_BUFFER
    if not await require_owner(update): return
    st   = CONFIG["stats"]
    chat = update.effective_chat
    member_count = "?"
    if chat.type != "private":
        try:
            member_count = await context.bot.get_chat_member_count(chat.id)
        except Exception:
            pass
    uptime_str = fmt_duration((now_vn() - PROCESS_START).total_seconds())
    mem_mb     = get_process_memory_mb()
    db_size    = get_db_size_bytes()
    await safe_reply(update,
        f"📊 *THỐNG KÊ MAID BOT* 📊\n\n"
        f"👤 *Người dùng:* {len(DB.get('users', {}))}\n"
        f"💬 *Tin đã xử lý:* {st['total_messages']}\n"
        f"🤖 *Tin AI đã trả lời:* {st.get('ai_messages', 0)}\n"
        f"👥 *Thành viên nhóm:* {member_count}\n"
        f"📦 *Buffer monitor:* {len(MSG_BUFFER)} tin\n"
        f"🧠 *RAM đang dùng:* {mem_mb:.1f} MB\n"
        f"🗄️ *Dung lượng DB:* {fmt_bytes(db_size)}\n"
        f"👁️ *Monitor:* {'🟢 Bật' if CONFIG.get('monitor_mode') else '🔴 Tắt'}\n"
        f"📡 *Proxy:* {'🟢 Bật' if CONFIG.get('proxy_mode') else '🔴 Tắt'}\n"
        f"🔓 *Filter/note cho mọi người:* {'🟢 Bật' if CONFIG.get('fn_open') else '🔴 Tắt'}\n"
        f"🕐 *Giờ VN hiện tại:* {now_vn().strftime('%H:%M %d/%m/%Y')} ICT\n"
        f"🗓️ *Khởi động từ:* {fmt_vn(st.get('started_at', ''))}\n"
        f"⏱️ *Tiến trình đã chạy:* {uptime_str} _(từ lần khởi động/redeploy gần nhất)_\n\n"
        f"✨ *Em luôn canh gác cho ngài!* ✨"
    )

async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    target = await get_target(update, context) or update.effective_user
    # Lưu lại target vào DB["users"] ngay (kể cả khi get_chat_member lỗi ở dưới)
    # để các lệnh admin sau này (ban/promote/...) có thể resolve user này qua
    # @username hoặc ID dù họ không còn nằm trong cache members của Telegram.
    track_user(target)
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, target.id)
        role   = ("👑 Owner / Ngài Master" if is_owner(target.id) else
                  "⭐ Bot Admin" if is_bot_admin(target.id) else member.status)
        warns  = (DB.get("warns", {})
                    .get(get_chat_key(update.effective_chat.id), {})
                    .get(str(target.id), 0))
        await safe_reply(update,
            f"ℹ️ *THÔNG TIN USER* ℹ️\n\n"
            f"🆔 *ID:* `{target.id}`\n"
            f"👤 *Tên:* {target.full_name}\n"
            f"🏷️ *Username:* @{target.username or 'không có'}\n"
            f"🤖 *Bot:* {'✅ Có' if target.is_bot else '❌ Không'}\n"
            f"📌 *Trạng thái:* {member.status}\n"
            f"🎖️ *Vai trò:* {role}\n"
            f"⚠️ *Cảnh cáo:* {warns}/3\n"
            f"👑 *Là Owner:* {'✅ Có' if is_owner(target.id) else '❌ Không'}"
        )
    except TelegramError as e:
        await safe_reply(update, f"❌ *Lỗi:* `{e}`")

# ── Promote/Demote commands ───────────────────────────────────────
async def _promote_cmd(update, context, fn, label: str):
    if not await require_owner(update): return
    chat = update.effective_chat
    if chat.type == "private":
        gid = CONFIG.get("target_group_id", 0)
        if not gid:
            await maid_reply(update, "no_group_set"); return
        target = await _resolve_target_dm(update, context, gid)
        if not target:
            await safe_reply(update, f"Dùng: `/{label} @username` hoặc `/adm @username` ạ~"); return
        try:
            await fn(context.bot, gid, target.id)
            if target.id not in DB["admins"]:
                DB["admins"].append(target.id); save_db(DB)
            await maid_reply(update, label, extra=f"👤 *{target.full_name}* — Nhóm: `{gid}`")
        except TelegramError as e:
            await safe_reply(update, f"❌ *Lỗi:* `{e}`")
        return
    target = await get_target(update, context)
    if not target:
        await safe_reply(update, f"Dùng: `/adm @username` hoặc reply ạ~"); return
    try:
        await fn(context.bot, chat.id, target.id)
        if target.id not in DB["admins"]:
            DB["admins"].append(target.id); save_db(DB)
        await maid_reply(update, label, extra=f"👤 *{target.full_name}*")
    except TelegramError as e:
        await safe_reply(update, f"❌ *Lỗi:* `{e}`")

async def cmd_promote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _promote_cmd(update, context, _do_promote_full, "promote_full")

async def cmd_apromote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _promote_cmd(update, context, _do_promote_basic, "promote_basic")

async def cmd_demote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    chat = update.effective_chat
    if chat.type == "private":
        gid = CONFIG.get("target_group_id", 0)
        if not gid:
            await maid_reply(update, "no_group_set"); return
        target = await _resolve_target_dm(update, context, gid)
        if not target:
            await safe_reply(update, "Dùng: `/deladm @username` ạ~"); return
        try:
            await _do_demote(context.bot, gid, target.id)
            if target.id in DB["admins"]:
                DB["admins"].remove(target.id); save_db(DB)
            await maid_reply(update, "demote_done", extra=f"👤 *{target.full_name}* — Nhóm: `{gid}`")
        except TelegramError as e:
            await safe_reply(update, f"❌ *Lỗi:* `{e}`")
        return
    target = await get_target(update, context)
    if not target:
        await safe_reply(update, "Dùng: `/deladm @username` hoặc reply ạ~"); return
    bot_uname = CONFIG.get("bot_username", "").lstrip("@").lower()
    if target.is_bot and target.username and target.username.lower() == bot_uname:
        await safe_reply(update, "😅 *Em không thể tự hạ chức mình được ạ!*"); return
    try:
        await _do_demote(context.bot, chat.id, target.id)
        if target.id in DB["admins"]:
            DB["admins"].remove(target.id); save_db(DB)
        await maid_reply(update, "demote_done", extra=f"👤 *{target.full_name}*")
    except TelegramError as e:
        await safe_reply(update, f"❌ *Lỗi:* `{e}`")

# ── Member management ─────────────────────────────────────────────
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if update.effective_chat.type == "private":
        await maid_reply(update, "not_group"); return
    target = await get_target(update, context)
    if not target:
        await safe_reply(update, "Dùng: `/ban @username [lý do]` hoặc reply ạ~"); return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        reason = _extract_reason(context.args or [])
        await maid_reply(update, "ban_done", extra=f"👤 *{target.full_name}*\n📋 *Lý do:* {reason}")
    except TelegramError as e:
        await safe_reply(update, f"❌ *Lỗi:* `{e}`")

async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    target = await get_target(update, context)
    if not target:
        await safe_reply(update, "Dùng: `/unban @username` ạ~"); return
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, target.id, only_if_banned=True)
        await safe_reply(update, f"✅ *Đã tha cho {target.full_name} rồi ạ~*")
    except TelegramError as e:
        await safe_reply(update, f"❌ *Lỗi:* `{e}`")

async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if update.effective_chat.type == "private":
        await maid_reply(update, "not_group"); return
    target = await get_target(update, context)
    if not target:
        await maid_reply(update, "no_target"); return
    try:
        cid = update.effective_chat.id
        await context.bot.ban_chat_member(cid, target.id)
        await context.bot.unban_chat_member(cid, target.id)
        await maid_reply(update, "kick_done", extra=f"👤 *{target.full_name}*")
    except TelegramError as e:
        await safe_reply(update, f"❌ *Lỗi:* `{e}`")

async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if update.effective_chat.type == "private":
        await maid_reply(update, "not_group"); return
    target = await get_target(update, context)
    if not target:
        await safe_reply(update, "Dùng: `/mute @username [30m/1h/2d]` ạ~"); return
    duration_sec = _parse_duration(context.args or [])
    until_date   = datetime.now() + timedelta(seconds=duration_sec) if duration_sec > 0 else None
    try:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id, user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False), until_date=until_date)
        dur_str = (f"{duration_sec // 86400} ngày" if duration_sec >= 86400 else
                   f"{duration_sec // 3600} giờ"   if duration_sec >= 3600  else
                   f"{duration_sec // 60 or duration_sec} phút" if duration_sec > 0 else "vĩnh viễn")
        await maid_reply(update, "mute_done", extra=f"👤 *{target.full_name}* — ⏱️ *{dur_str}*")
    except TelegramError as e:
        await safe_reply(update, f"❌ *Lỗi:* `{e}`")

async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    target = await get_target(update, context)
    if not target:
        await maid_reply(update, "no_target"); return
    try:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id, user_id=target.id,
            permissions=ChatPermissions(can_send_messages=True, can_send_polls=True,
                                        can_send_other_messages=True, can_add_web_page_previews=True))
        await maid_reply(update, "unmute_done", extra=f"👤 *{target.full_name}*")
    except TelegramError as e:
        await safe_reply(update, f"❌ *Lỗi:* `{e}`")

async def cmd_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not update.message.reply_to_message:
        await safe_reply(update, "📌 *Reply vào tin nhắn muốn ghim nhé~*"); return
    try:
        await context.bot.pin_chat_message(
            update.effective_chat.id, update.message.reply_to_message.message_id,
            disable_notification=True)
        await maid_reply(update, "pin_done")
    except TelegramError as e:
        await safe_reply(update, f"❌ *Lỗi:* `{e}`")

async def cmd_unpin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    try:
        if update.message.reply_to_message:
            await context.bot.unpin_chat_message(
                update.effective_chat.id, update.message.reply_to_message.message_id)
        else:
            await context.bot.unpin_all_chat_messages(update.effective_chat.id)
        await maid_reply(update, "unpin_done")
    except TelegramError as e:
        await safe_reply(update, f"❌ *Lỗi:* `{e}`")

async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not update.message.reply_to_message:
        await safe_reply(update, "🗑️ *Reply vào tin nhắn muốn xóa nhé~*"); return
    try:
        await context.bot.delete_message(update.effective_chat.id,
                                         update.message.reply_to_message.message_id)
        try: await update.message.delete()
        except Exception: pass
    except TelegramError as e:
        await safe_reply(update, f"❌ *Lỗi xóa:* `{e}`")

async def cmd_purge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not update.message.reply_to_message:
        await safe_reply(update, "🧹 *Reply vào tin đầu tiên muốn xóa nhé~*"); return
    start_id = update.message.reply_to_message.message_id
    end_id   = update.message.message_id
    chat_id  = update.effective_chat.id
    total    = end_id - start_id + 1

    PURGE_CAP = 200
    force = bool(context.args) and context.args[0].strip().lower() == "force"
    if total > PURGE_CAP and not force:
        await safe_reply(update,
            f"⚠️ *Phạm vi xóa quá lớn: {total} tin!*\n"
            f"Giới hạn an toàn là `{PURGE_CAP}` tin/lần.\n\n"
            f"Nếu chắc chắn muốn xóa hết `{total}` tin, gõ:\n"
            f"`/purge force` (reply vào tin đầu tiên như cũ)")
        return

    deleted = fail = 0
    for msg_id in range(start_id, end_id + 1):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            deleted += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    try:
        notif = await context.bot.send_message(
            chat_id, f"✅ *Đã xóa {deleted} tin rồi ạ~*"
            + (f" (Không xóa được {fail} tin)" if fail else ""))
        await asyncio.sleep(3)
        await notif.delete()
    except Exception:
        pass

async def cmd_settitle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    target = await get_target(update, context)
    if not target:
        await safe_reply(update, "Dùng: `/the @username Chức Vụ` ạ~"); return
    title = " ".join(
        a for a in (context.args or [])
        if not a.lstrip("-").isdigit() and not a.startswith("@")
    ) or "Admin"
    title = title[:16]
    chat_id = update.effective_chat.id
    try:
        await context.bot.set_chat_administrator_custom_title(
            chat_id, target.id, custom_title=title)
        await safe_reply(update, f"✅ *Đã đặt chức vụ [{title}] cho {target.full_name} rồi ạ~*")
        return
    except TelegramError as e:
        err = str(e).lower()
        if "not an administrator" not in err and "chat_admin_required" not in err:
            await safe_reply(update, f"❌ *Lỗi:* `{e}`")
            return
    # Không phải admin → thử method mới dành cho thẻ thành viên thường.
    try:
        await context.bot.set_chat_member_tag(chat_id, target.id, tag=title)
        await safe_reply(update, f"✅ *Đã đặt thẻ [{title}] cho {target.full_name} rồi ạ~*")
    except TelegramError as e2:
        await safe_reply(update, f"❌ *Lỗi:* `{e2}`")
    except AttributeError:
        await safe_reply(
            update,
            f"❌ *{target.full_name} không phải admin* nên không đặt được chức vụ kiểu cũ. "
            "Bot cần hỗ trợ `set_chat_member_tag` (tính năng mới của Telegram) để đặt thẻ "
            "cho thành viên thường, nhưng thư viện hiện tại chưa có hàm này ạ~"
        )

# ── Warn system ───────────────────────────────────────────────────
async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    target = await get_target(update, context)
    if not target:
        await maid_reply(update, "no_target"); return
    if is_owner(target.id):
        await safe_reply(update, "😱 *Em không thể cảnh cáo ngài được ạ!*"); return
    chat_key = get_chat_key(update.effective_chat.id)
    DB["warns"].setdefault(chat_key, {})
    uid = str(target.id)
    DB["warns"][chat_key][uid] = DB["warns"][chat_key].get(uid, 0) + 1
    current = DB["warns"][chat_key][uid]
    save_db(DB)
    reason = _extract_reason(context.args or [])
    if current >= WARN_LIMIT:
        try:
            await context.bot.ban_chat_member(update.effective_chat.id, target.id)
            DB["warns"][chat_key][uid] = 0; save_db(DB)
            await safe_reply(update, f"🔨 *{target.full_name} đủ {WARN_LIMIT} cảnh cáo → BAN!*\n📋 *Lý do:* {reason}")
        except TelegramError as e:
            await safe_reply(update, f"❌ *Lỗi ban:* `{e}`")
    else:
        await maid_reply(update, "warn_add",
            extra=f"👤 *{target.full_name}*: `{current}/{WARN_LIMIT}`\n📋 *Lý do:* {reason}")

async def cmd_unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    target = await get_target(update, context)
    if not target:
        await maid_reply(update, "no_target"); return
    chat_key = get_chat_key(update.effective_chat.id)
    uid = str(target.id)
    current = DB.get("warns", {}).get(chat_key, {}).get(uid, 0)
    if current > 0:
        DB["warns"][chat_key][uid] = current - 1; save_db(DB)
        await safe_reply(update, f"✅ *Đã xóa 1 cảnh cáo của {target.full_name}.* Hiện: `{current-1}/{WARN_LIMIT}`")
    else:
        await safe_reply(update, f"ℹ️ *{target.full_name} chưa có cảnh cáo nào ạ~*")

async def cmd_warns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    target   = await get_target(update, context) or update.effective_user
    chat_key = get_chat_key(update.effective_chat.id)
    current  = DB.get("warns", {}).get(chat_key, {}).get(str(target.id), 0)
    await safe_reply(update, f"⚠️ *{target.full_name} có `{current}/{WARN_LIMIT}` cảnh cáo ạ~*")

# ── Notes ─────────────────────────────────────────────────────────
NOTE_USAGE = (
    "Dùng: `/note #tên nội_dung` ạ~\n"
    "💡 Reply vào 1 ảnh/video/gif rồi gõ lệnh để gắn media cho note nhé~ "
    "(có thể vừa gắn media, vừa thêm caption ngay trong câu lệnh)\n"
    "💡 Reply vào 1 ảnh/video *trong album nhiều ảnh* → bot tự gom *cả "
    "album* gắn vào note, gửi lại y hệt khi gọi `/get`\n"
    "• Gõ lại `/note #tên` *không* có `%%%` → *ghi đè* toàn bộ nội dung cũ "
    "(nếu đang có ≥2 câu random thì bot sẽ hỏi xác nhận trước khi xoá)\n"
    "• Thêm `%%%` vào nội dung → *thêm 1 câu random mới*, giữ nguyên các câu "
    "cũ. Mỗi câu random có thể kèm ảnh/video/album riêng (reply lúc thêm câu đó)\n"
    "• Chèn link: `[chữ hiển thị](https://example.com)` ạ~"
)

async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not is_group_scope(update.effective_chat):
        return  # note chỉ dùng trong nhóm/kênh/topic — im lặng bỏ qua ở DM
    msg = update.message
    # Lấy NGUYÊN VĂN từ msg.text (không dùng context.args — Telegram tách
    # args theo MỌI whitespace kể cả xuống dòng, join lại sẽ làm phẳng mất
    # hết \n gốc người dùng đã gõ, hiện ra dồn cục 1 đoạn khi /get).
    raw = re.sub(r"^/note(@\w+)?\s*", "", msg.text or "", count=1, flags=re.IGNORECASE)
    m = re.match(r"(\S+)[ \t]*(.*)$", raw, re.DOTALL)
    if not m:
        await safe_reply(update, NOTE_USAGE); return
    name = m.group(1).lstrip("#").lower()
    body = m.group(2).strip()
    if not body and msg.reply_to_message and msg.reply_to_message.text:
        body = msg.reply_to_message.text

    chat_key  = NOTES_FILTERS_KEY  # note dùng chung 1 kho global cho mọi chat
    is_random = has_variant_marker(body)

    media = []
    if update.message.reply_to_message:
        media = extract_message_album(update.message.reply_to_message)

    new_variants = build_new_variants(body, media)
    if not new_variants:
        await safe_reply(update, "📝 *Cần có nội dung hoặc reply vào tin/media ạ~*"); return

    existing = normalize_note(DB["notes"].get(chat_key, {}).get(name))

    if is_random:
        # %%% → luôn THÊM, không bao giờ hỏi xác nhận
        variants = merge_variants(existing["variants"], new_variants)
        entry = {"variants": variants, "media": normalize_media_list(existing.get("media"))}
        DB["notes"].setdefault(chat_key, {})[name] = entry
        save_db(DB)
        tag = " 📎" if any(v.get("media") for v in variants) else ""
        await maid_reply(update, "note_saved",
                          extra=f"📝 *#{name}* đã thêm câu random mới! (`{len(variants)}` câu){tag}")
        return

    # Không có %%% → ghi đè. Nếu đang có ≥2 câu random thì hỏi xác nhận trước.
    if len(existing["variants"]) >= 2:
        pkey = uuid.uuid4().hex[:16]  # id ngắn — callback_data Telegram giới hạn 64 byte
        set_pending_overwrite(pkey, {
            "kind": "note", "chat_key": chat_key, "name": name,
            "new_variants": new_variants,
        })
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Ghi đè, xoá hết câu cũ", callback_data=f"ovwconfirm:{pkey}"),
            InlineKeyboardButton("❌ Huỷ", callback_data=f"ovwcancel:{pkey}"),
        ]])
        await update.message.reply_text(
            to_markdown_v2(
                f"⚠️ *#{name}* đang có `{len(existing['variants'])}` câu random (%%%).\n"
                f"Gõ lại không kèm `%%%` sẽ *xoá hết* và ghi đè bằng nội dung mới. "
                f"Xác nhận chứ ạ?"
            ),
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb,
        )
        return

    entry = {"variants": new_variants, "media": []}
    cleanup_variant_media(existing["variants"], existing.get("media"), new_variants)
    DB["notes"].setdefault(chat_key, {})[name] = entry
    save_db(DB)
    tag = " 📎" if any(v.get("media") for v in new_variants) else ""
    await maid_reply(update, "note_saved", extra=f"📝 *#{name}* đã lưu! (`{len(new_variants)}` câu){tag}")

async def cmd_get(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not is_group_scope(update.effective_chat):
        return  # note chỉ dùng trong nhóm/kênh/topic — im lặng bỏ qua ở DM
    if not context.args:
        await safe_reply(update, "Dùng: `/get #tên` ạ~"); return
    name     = context.args[0].lstrip("#").lower()
    chat_key = NOTES_FILTERS_KEY
    note     = DB.get("notes", {}).get(chat_key, {}).get(name)
    if note is None:
        await safe_reply(update, f"❓ *Không tìm thấy ghi chú #{name} ạ~*"); return
    await send_note_response(update, note, name)

async def cmd_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not is_group_scope(update.effective_chat):
        return  # note chỉ dùng trong nhóm/kênh/topic — im lặng bỏ qua ở DM
    chat_key  = NOTES_FILTERS_KEY
    note_list = DB.get("notes", {}).get(chat_key, {})
    if not note_list:
        await safe_reply(update, "📭 *Không có ghi chú nào ạ~*"); return
    lines = []
    for n, v in sorted(note_list.items()):
        data = normalize_note(v)
        tag  = " 📎" if data.get("media") or any(x.get("media") for x in data["variants"]) else ""
        cnt  = f" (`{len(data['variants'])}`)" if len(data["variants"]) > 1 else ""
        lines.append(f"• `#{n}`{tag}{cnt}")
    await safe_reply(update, "📋 *DANH SÁCH GHI CHÚ:*\n\n" + "\n".join(lines))

async def cmd_delnote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not is_group_scope(update.effective_chat):
        return  # note chỉ dùng trong nhóm/kênh/topic — im lặng bỏ qua ở DM
    if not context.args:
        await safe_reply(update, "Dùng: `/delnote #tên` ạ~"); return
    name     = context.args[0].lstrip("#").lower()
    chat_key = NOTES_FILTERS_KEY
    if chat_key in DB["notes"] and name in DB["notes"][chat_key]:
        old  = DB["notes"][chat_key].pop(name); save_db(DB)
        data = normalize_note(old)
        cleanup_variant_media(data["variants"], data.get("media"), new_variants=None)
        await safe_reply(update, f"✅ *Đã xóa ghi chú #{name} rồi ạ~*")
    else:
        await safe_reply(update, f"❓ *Không tìm thấy #{name} ạ~*")

# ── Filters (cú pháp kiểu Rose: nhiều trigger, exact/prefix, {admin}/{user},
#    {allow_bot}, {protect}, {replytag}, {nonotif}, {mediaspoiler}, {preview},
#    {rules}, random reply qua %%%) ─────────────────────────────────
FILTER_USAGE = (
    "Dùng: `/filter từ_khóa phản_hồi` ạ~\n"
    "💡 Reply vào 1 ảnh/video/gif rồi gõ lệnh để gắn media cho filter nhé~\n"
    "💡 Reply vào 1 ảnh/video *trong album nhiều ảnh* → bot tự gom *cả "
    "album* gắn vào filter, gửi lại y hệt khi trigger nổ\n\n"
    "Cú pháp nâng cao (giống Rose):\n"
    "• `/filter (hi, hey, \"hi there\") Hello!` — nhiều trigger 1 lần\n"
    "• `/filter \"exact:hi\" ...` / `/filter \"prefix:hi\" ...`\n"
    "• Thêm `{admin}` / `{user}` / `{allow_bot}` / `{protect}` / `{replytag}` / "
    "`{nonotif}` / `{mediaspoiler}` / `{preview}` / `{rules}` vào cuối nội dung\n"
    "• Gõ lại cùng trigger *không* có `%%%` → *ghi đè* toàn bộ nội dung cũ "
    "(nếu đang có ≥2 câu random thì bot sẽ hỏi xác nhận trước khi xoá)\n"
    "• Thêm `%%%` vào nội dung → *thêm 1 câu random mới*, giữ nguyên các câu "
    "cũ. Mỗi câu random có thể kèm ảnh/video/album riêng (reply lúc thêm câu đó)\n"
    "• Chèn link: `[chữ hiển thị](https://example.com)` ạ~\n"
    "• Biến: {first} {last} {fullname} {username} {mention} {id} {chatname}"
)

def _norm_filter_key(content: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", content.lower()).strip("_") or "filter"

def _build_filter_entry(triggers, variants, flags, preview, rules_btn, existing) -> dict:
    access = "admin" if flags["admin"] else "user" if flags["user"] else "all"
    return {
        "triggers":     [{"content": c, "mode": m} for c, m in triggers],
        "variants":     variants,
        "media":        normalize_media_list(existing.get("media")) if existing else [],  # media chung kiểu cũ
        "access":       access,
        "allow_bot":    flags["allow_bot"],
        "protect":      flags["protect"],
        "replytag":     flags["replytag"],
        "nonotif":      flags["nonotif"],
        "mediaspoiler": flags["mediaspoiler"],
        "preview":      preview,
        "rules_btn":    rules_btn,
        "force_reply":  existing.get("force_reply", False) if existing else False,
    }

async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not is_group_scope(update.effective_chat):
        return  # filter chỉ dùng trong nhóm/kênh/topic — im lặng bỏ qua ở DM
    msg = update.message
    raw = re.sub(r"^/filter(@\w+)?\s*", "", msg.text or "", count=1, flags=re.IGNORECASE)
    triggers, rest = parse_filter_spec(raw)
    if not triggers:
        await safe_reply(update, FILTER_USAGE); return

    body, flags, preview, rules_btn = parse_filter_flags(rest)
    chat_key  = NOTES_FILTERS_KEY  # filter dùng chung 1 kho global cho mọi chat
    primary   = f"{triggers[0][0]}__{_norm_filter_key(triggers[0][0])}"
    is_random = has_variant_marker(body)

    media = []
    if msg.reply_to_message:
        media = extract_message_album(msg.reply_to_message)

    new_variants = build_new_variants(body, media)
    existing = DB["filters"].get(chat_key, {}).get(primary)
    existing_variants = normalize_variants(existing.get("variants")) if isinstance(existing, dict) else []

    if not new_variants and not (existing and existing.get("media")):
        await safe_reply(update, "❌ *Cần có nội dung trả lời hoặc media đính kèm ạ~*"); return

    if is_random:
        # %%% → luôn THÊM, không bao giờ hỏi xác nhận
        variants = merge_variants(existing_variants, new_variants)
        entry = _build_filter_entry(triggers, variants, flags, preview, rules_btn, existing)
        DB["filters"].setdefault(chat_key, {})[primary] = entry
        save_db(DB)
        trig_list = ", ".join(f"`{c}`" + ({"exact": " (exact)", "prefix": " (prefix)"}.get(m, "")) for c, m in triggers)
        tag = " 📎" if any(v.get("media") for v in variants) else ""
        await maid_reply(update, "filter_saved",
                          extra=f"🔍 {trig_list} → đã thêm câu random mới (`{len(variants)}` câu){tag}")
        return

    # Không có %%% → ghi đè. Nếu đang có ≥2 câu random thì hỏi xác nhận trước.
    if len(existing_variants) >= 2:
        pkey = uuid.uuid4().hex[:16]  # id ngắn — callback_data Telegram giới hạn 64 byte
        set_pending_overwrite(pkey, {
            "kind": "filter", "chat_key": chat_key, "primary": primary,
            "triggers": triggers, "new_variants": new_variants,
            "flags": flags, "preview": preview, "rules_btn": rules_btn,
        })
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Ghi đè, xoá hết câu cũ", callback_data=f"ovwconfirm:{pkey}"),
            InlineKeyboardButton("❌ Huỷ", callback_data=f"ovwcancel:{pkey}"),
        ]])
        trig_list = ", ".join(f"`{c}`" for c, _ in triggers)
        await msg.reply_text(
            to_markdown_v2(
                f"⚠️ Filter {trig_list} đang có `{len(existing_variants)}` câu random (%%%).\n"
                f"Gõ lại không kèm `%%%` sẽ *xoá hết* và ghi đè bằng nội dung mới. "
                f"Xác nhận chứ ạ?"
            ),
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb,
        )
        return

    entry = _build_filter_entry(triggers, new_variants, flags, preview, rules_btn, existing)
    if isinstance(existing, dict):
        cleanup_variant_media(existing.get("variants"), existing.get("media"), new_variants)
    DB["filters"].setdefault(chat_key, {})[primary] = entry
    save_db(DB)

    trig_list = ", ".join(f"`{c}`" + ({"exact": " (exact)", "prefix": " (prefix)"}.get(m, "")) for c, m in triggers)
    tag = " 📎" if any(v.get("media") for v in new_variants) else ""
    preview_text = new_variants[0]["text"] if new_variants and new_variants[0]["text"] else "(chỉ media)"
    extra_cnt = f" (`{len(new_variants)}` câu)" if len(new_variants) > 1 else ""
    await maid_reply(update, "filter_saved", extra=f"🔍 {trig_list} → {preview_text}{tag}{extra_cnt}")

async def cmd_stopfilter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not is_group_scope(update.effective_chat):
        return  # filter chỉ dùng trong nhóm/kênh/topic — im lặng bỏ qua ở DM
    if not context.args:
        await safe_reply(update, "Dùng: `/stop từ_khóa` ạ~"); return
    keyword  = " ".join(context.args).strip().lower()
    chat_key = NOTES_FILTERS_KEY
    flist    = DB.get("filters", {}).get(chat_key, {})

    found_key = None
    for k, v in flist.items():
        if isinstance(v, dict) and "triggers" in v:
            if any(t["content"] == keyword for t in v["triggers"]):
                found_key = k; break
        elif k == keyword:
            found_key = k; break

    if found_key:
        old = flist.pop(found_key)
        save_db(DB)
        if isinstance(old, dict):
            cleanup_variant_media(old.get("variants"), old.get("media"), new_variants=None)
        await safe_reply(update, f"✅ *Đã xóa filter [{keyword}] rồi ạ~*")
    else:
        await safe_reply(update, f"❓ *Không tìm thấy filter [{keyword}] ạ~*")


def _filter_summary_line(key: str, value) -> str:
    """1 dòng tóm tắt filter: trigger(s) + cờ quyền + media + số câu random
    — KHÔNG hiện nội dung, dùng cho /filters gọn gàng."""
    if isinstance(value, dict) and "triggers" in value:
        trig_str = ", ".join(t["content"] for t in value["triggers"])
        variants = normalize_variants(value.get("variants"))
        n        = len(variants)
        tag      = "📎" if value.get("media") or any(v.get("media") for v in variants) else ""
        access   = value.get("access", "all")
        atag     = "👑" if access == "admin" else "🙋" if access == "user" else ""
        cnt      = f" ({n} câu)" if n > 1 else ""
        flags = []
        if value.get("protect"):      flags.append("🔒")
        if value.get("nonotif"):      flags.append("🔕")
        if value.get("mediaspoiler"): flags.append("👁️‍🗨️")
        if value.get("replytag"):     flags.append("↩️")
        flag_str = "".join(flags)
        return f"`{trig_str}`{tag}{atag}{flag_str}{cnt}"
    variants = normalize_variants(value.get("variants")) if isinstance(value, dict) else []
    n = len(variants)
    cnt = f" ({n} câu)" if n > 1 else ""
    return f"`{key}`{cnt}"


def _filter_detail_text(key: str, value) -> str:
    """Nội dung chi tiết đầy đủ 1 filter — hiện khi bấm nút xem chi tiết."""
    if isinstance(value, dict) and "triggers" in value:
        trig_str = ", ".join(
            t["content"] + ({"exact": " (exact)", "prefix": " (prefix)"}.get(t.get("mode"), ""))
            for t in value["triggers"]
        )
        variants = normalize_variants(value.get("variants"))
        access   = value.get("access", "all")
        access_label = {"admin": "👑 chỉ admin", "user": "🙋 chỉ thành viên thường"}.get(access, "👥 mọi người")
        flags = []
        if value.get("protect"):      flags.append("🔒 protect")
        if value.get("nonotif"):      flags.append("🔕 nonotif")
        if value.get("mediaspoiler"): flags.append("👁️‍🗨️ mediaspoiler")
        if value.get("replytag"):     flags.append("↩️ replytag")
        if value.get("allow_bot"):    flags.append("🤖 allow_bot")
        if value.get("preview"):      flags.append(f"🔗 preview:{value['preview']}")
        if value.get("rules_btn"):    flags.append("📜 rules")
        flag_str = ", ".join(flags) if flags else "—"
        lines = [
            f"🔍 *Chi tiết filter*",
            f"*Trigger:* `{trig_str}`",
            f"*Quyền dùng:* {access_label}",
            f"*Cờ:* {flag_str}",
            "",
        ]
        for i, v in enumerate(variants, 1):
            n_media = len(v.get("media") or [])
            mtag = f" 📎×{n_media}" if n_media else ""
            prefix = f"*Câu {i}/{len(variants)}{mtag}:*" if len(variants) > 1 else f"*Nội dung{mtag}:*"
            lines.append(f"{prefix}\n{v.get('text') or '_(không có text, chỉ media)_'}")
            lines.append("")
        return "\n".join(lines).strip()
    # Format rất cũ (string thuần hoặc dict {"text","media"} không có "triggers")
    if isinstance(value, dict):
        tag = " 📎" if value.get("media") else ""
        return f"🔍 *Chi tiết filter `{key}`*\n\n{value.get('text', '')}{tag}"
    return f"🔍 *Chi tiết filter `{key}`*\n\n{value}"


async def cmd_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not is_group_scope(update.effective_chat):
        return  # filter chỉ dùng trong nhóm/kênh/topic — im lặng bỏ qua ở DM
    chat_key = NOTES_FILTERS_KEY
    flist    = DB.get("filters", {}).get(chat_key, {})
    if not flist:
        await safe_reply(update, "🔍 *Không có filter nào ạ~*"); return

    lines = ["🔍 *DANH SÁCH FILTERS:*", "_Bấm nút bên dưới để xem nội dung đầy đủ từng filter ạ~_", ""]
    buttons = []
    for i, (k, v) in enumerate(flist.items(), 1):
        lines.append(f"{i}. {_filter_summary_line(k, v)}")
        pkey = uuid.uuid4().hex[:16]
        register_filter_view(pkey, k)
        buttons.append(InlineKeyboardButton(str(i), callback_data=f"fview:{pkey}"))
    rows = [buttons[i:i + 8] for i in range(0, len(buttons), 8)]
    await safe_reply(update, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows) if rows else None)


async def cmd_fn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not is_group_scope(update.effective_chat):
        return  # /fn chỉ áp dụng trong nhóm/kênh/topic
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("on", "off"):
        state = "🟢 BẬT" if CONFIG.get("fn_open") else "🔴 TẮT"
        await safe_reply(
            update,
            f"🔓 *Trạng thái hiện tại:* {state}\n\n"
            f"Dùng: `/fn on` hoặc `/fn off` ạ~\n"
            f"• `on` — mọi thành viên trong nhóm đều *trigger* được filter/note "
            f"(gõ trúng từ khóa, `#tên` note)\n"
            f"• `off` _(mặc định)_ — chỉ owner trigger được\n\n"
            f"_Lưu ý: chỉ ảnh hưởng việc TRIGGER — tạo/sửa/xoá filter/note luôn "
            f"chỉ owner làm được, không phụ thuộc cờ này._",
        )
        return
    CONFIG["fn_open"] = (arg == "on")
    save_config(CONFIG)
    if CONFIG["fn_open"]:
        await safe_reply(update, "✅ *Đã BẬT* — mọi thành viên trong nhóm giờ trigger được filter/note ạ~")
    else:
        await safe_reply(update, "✅ *Đã TẮT* — chỉ owner trigger được filter/note ạ~")


# ── Media đính kèm cho câu thoại tĩnh & status AI ──────────────────
async def cmd_setmedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not context.args:
        await safe_reply(update, "Dùng: `/setmedia <key>` (reply vào ảnh/video/gif) ạ~\nGõ `/listmedia` để xem danh sách key ạ~"); return
    key = context.args[0].lower()
    if key not in MAID_PHRASES:
        await safe_reply(update, f"❓ *Không có key `{key}` ạ~* Gõ `/listmedia` để xem danh sách nhé~"); return
    if not update.message.reply_to_message:
        await safe_reply(update, "📎 *Reply vào 1 ảnh/video/gif rồi gõ lệnh lại ạ~*"); return
    ok = set_phrase_media(update.message.reply_to_message, key)
    if ok:
        await safe_reply(update, f"✅ *Đã gắn media cho câu thoại `{key}` rồi ạ~*")
    else:
        await safe_reply(update, "😭 *Tin nhắn reply không có ảnh/video/gif hợp lệ ạ~*")

async def cmd_delmedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not context.args:
        await safe_reply(update, "Dùng: `/delmedia <key>` ạ~"); return
    key = context.args[0].lower()
    if del_phrase_media(key):
        await safe_reply(update, f"🗑️ *Đã gỡ media của `{key}` rồi ạ~*")
    else:
        await safe_reply(update, f"❓ *Key `{key}` chưa có media nào ạ~*")

async def cmd_listmedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    attached = DB.get("phrase_media", {})
    lines = [f"• `{k}`" + (" 📎" if k in attached else "") for k in sorted(MAID_PHRASES.keys())]
    await safe_reply(update, "🎬 *DANH SÁCH KEY CÂU THOẠI:*\n(📎 = đã gắn media)\n\n" + "\n".join(lines))

async def cmd_setthinkingmedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not update.message.reply_to_message:
        await safe_reply(update, "📎 *Reply vào 1 ảnh/video/gif rồi gõ `/setthinkingmedia` ạ~*"); return
    ok = set_thinking_media(update.message.reply_to_message)
    if ok:
        await safe_reply(update, "✅ *Đã đặt media riêng cho status \"đang suy nghĩ\" của AI rồi ạ~*")
    else:
        await safe_reply(update, "😭 *Tin nhắn reply không có ảnh/video/gif hợp lệ ạ~*")

async def cmd_delthinkingmedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if del_thinking_media():
        await safe_reply(update, "🗑️ *Đã gỡ media riêng, quay lại mặc định rồi ạ~*")
    else:
        await safe_reply(update, "❓ *Chưa có media riêng nào được đặt ạ~*")

# ── Rules ──────────────────────────────────────────────────────────
async def cmd_setrules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not context.args:
        await safe_reply(update, "Dùng: `/setrules Nội dung...` ạ~"); return
    chat_key = get_chat_key(update.effective_chat.id)
    DB["rules"][chat_key] = " ".join(context.args); save_db(DB)
    await maid_reply(update, "rules_set")

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    chat_key = get_chat_key(update.effective_chat.id)
    rules    = DB.get("rules", {}).get(chat_key)
    await safe_reply(update, f"📜 *NỘI QUY NHÓM:*\n\n{rules}" if rules
                     else "📜 *Nhóm này chưa có nội quy. Dùng /setrules nhé~*")

# ── Owner tools ───────────────────────────────────────────────────
async def cmd_setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not context.args:
        await safe_reply(update,
            f"🏠 *Dùng:* `/setgroup group_id` ạ~\n*Hiện tại:* `{CONFIG.get('target_group_id', 0)}`\n"
            f"💡 *Lấy ID nhóm bằng /id trong nhóm đó.*"); return
    try:
        gid = int(context.args[0])
        CONFIG["target_group_id"] = gid; save_config(CONFIG)
        await safe_reply(update, f"✅ *Đã đặt nhóm relay:* `{gid}`\nDùng `/proxy` và `/monitor` ạ~")
    except ValueError:
        await safe_reply(update, "❌ *group_id phải là số nguyên (vd: -1001234567890)*")

async def cmd_setbufferlimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """[BIG UPDATE] Tùy chỉnh số tin nhắn tối đa lưu trong msg_buffer mỗi
    nhóm — trước đây hardcode 5000 trong db_postgres.py, giờ chỉnh qua lệnh
    này, áp dụng ngay cho tin nhắn tiếp theo không cần restart bot."""
    if not await require_owner(update): return
    current = CONFIG.get("msg_buffer_limit", 5000)
    if not context.args:
        await safe_reply(update,
            f"📦 *Dùng:* `/setbufferlimit số_lượng` ạ~\n"
            f"*Hiện tại:* `{current}` tin nhắn / nhóm\n\n"
            f"💡 Đây là số tin nhắn tối đa giữ trong `msg_buffer` (PostgreSQL) "
            f"cho mỗi nhóm — tin cũ hơn sẽ tự bị xóa khi vượt giới hạn. "
            f"Ảnh hưởng tới `/tomtat ngay`, `/tomtat moi`, `/tomtat [n]`.\n"
            f"⚠️ Đặt quá lớn (vd >50000) có thể làm DB phình to nhanh."
        ); return
    try:
        new_limit = int(context.args[0])
    except ValueError:
        await safe_reply(update, "❌ *Số lượng phải là số nguyên (vd: 8000)*"); return
    if new_limit < 50:
        await safe_reply(update, "❌ *Tối thiểu 50 tin nhắn để /tomtat còn hoạt động ý nghĩa ạ~*"); return
    if new_limit > 200000:
        await safe_reply(update, "❌ *Tối đa 200000 — quá lớn sẽ khiến DB phình to & query chậm ạ~*"); return
    CONFIG["msg_buffer_limit"] = new_limit
    save_config(CONFIG)
    await safe_reply(update,
        f"✅ *Đã đặt giới hạn buffer:* `{current}` → `{new_limit}` tin nhắn / nhóm\n"
        f"_Áp dụng ngay từ tin nhắn tiếp theo, không cần restart ạ~_"
    )

async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    users = DB.get("users", {})
    if not users:
        await safe_reply(update, "📭 *Chưa có user nào trong database ạ~*"); return
    lines = [f"👥 *Users trong DB ({len(users)}):*"]
    for uid, info in list(users.items())[:30]:
        uname = f"@{info['username']}" if info.get("username") else ""
        lines.append(f"• `{uid}`  {info.get('name', '?')}  {uname}")
    if len(users) > 30:
        lines.append(f"...và {len(users) - 30} user khác")
    await safe_reply(update, "\n".join(lines))

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not context.args:
        await safe_reply(update, "📢 *Dùng:* `/broadcast Nội dung` ạ~"); return
    msg_text = " ".join(context.args)
    safe_broadcast = to_markdown_v2(f"📢 *THÔNG BÁO:*\n{msg_text}")
    ok = fail = 0
    for uid_str in DB.get("users", {}):
        try:
            await context.bot.send_message(int(uid_str), safe_broadcast, parse_mode=ParseMode.MARKDOWN_V2)
            ok += 1; await asyncio.sleep(0.1)
        except Exception:
            fail += 1
    await safe_reply(update, f"✅ *Broadcast xong!*\n✔️ Thành công: `{ok}`\n❌ Thất bại: `{fail}`")

async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not context.args:
        await safe_reply(update, "Dùng: `/block user_id` ạ~"); return
    try:
        uid = int(context.args[0])
    except ValueError:
        await safe_reply(update, "❌ *user_id phải là số nguyên!*"); return
    blocked = DB.setdefault("blocked", [])
    if uid not in blocked:
        blocked.append(uid); save_db(DB)
    await maid_reply(update, "blocked", extra=f"🛡️ *Đã chặn* `{uid}`!")

async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not context.args:
        await safe_reply(update, "Dùng: `/unblock user_id` ạ~"); return
    try:
        uid = int(context.args[0])
    except ValueError:
        await safe_reply(update, "❌ *user_id phải là số nguyên!*"); return
    blocked = DB.get("blocked", [])
    if uid in blocked:
        blocked.remove(uid); save_db(DB)
        await safe_reply(update, f"✅ *Đã bỏ chặn* `{uid}` *rồi ạ~*")
    else:
        await safe_reply(update, "ℹ️ *User này chưa bị chặn ạ~*")

async def cmd_deldm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if update.effective_chat.type != "private":
        await safe_reply(update, "📱 *Lệnh này chỉ dùng trong DM thôi ạ~*"); return
    if update.message.reply_to_message:
        target_msg_id = update.message.reply_to_message.message_id
    elif context.args and context.args[0].isdigit():
        target_msg_id = int(context.args[0])
    else:
        await safe_reply(update, "🗑️ *Reply vào tin muốn xóa hoặc /deldm <msg_id> ạ~*"); return
    try:
        await context.bot.delete_message(update.effective_chat.id, target_msg_id)
        try: await update.message.delete()
        except Exception: pass
    except TelegramError as e:
        await safe_reply(update, f"❌ *Lỗi xóa tin DM:* `{e}`")

async def cmd_editdm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if update.effective_chat.type != "private":
        await safe_reply(update, "📱 *Lệnh này chỉ dùng trong DM thôi ạ~*"); return
    if update.message.reply_to_message:
        target_msg_id = update.message.reply_to_message.message_id
        new_text = " ".join(context.args) if context.args else ""
    else:
        if not context.args or len(context.args) < 2:
            await safe_reply(update, "✏️ *Dùng:* `/editdm <msg_id> nội_dung`\nhoặc reply rồi `/editdm nội_dung` ạ~"); return
        if not context.args[0].isdigit():
            await safe_reply(update, "❌ *msg_id phải là số nguyên ạ~*"); return
        target_msg_id = int(context.args[0])
        new_text = " ".join(context.args[1:])
    if not new_text.strip():
        await safe_reply(update, "📝 *Cần có nội dung mới ạ~*"); return
    try:
        await context.bot.edit_message_text(new_text, chat_id=update.effective_chat.id,
                                             message_id=target_msg_id)
        try: await update.message.delete()
        except Exception: pass
    except TelegramError as e:
        await safe_reply(update, f"❌ *Lỗi sửa tin DM:* `{e}`")

async def cmd_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if update.effective_chat.type != "private":
        await safe_reply(update, "📡 *Bật proxy trong DM với bot nhé~*"); return
    gid = CONFIG.get("target_group_id", 0)
    if not gid:
        await maid_reply(update, "no_group_set"); return
    CONFIG["proxy_mode"] = not CONFIG["proxy_mode"]; save_config(CONFIG)
    if CONFIG["proxy_mode"]:
        await maid_reply(update, "proxy_on",
            extra=f"🏠 *Nhóm đích:* `{gid}`\n\n"
                  f"📖 *Cách dùng:*\n"
                  f"• *Nhắn bình thường* → gửi vào nhóm\n"
                  f"• `reply <msg_id> nội_dung` → reply vào tin\n"
                  f"• `del <msg_id>` → xóa tin\n"
                  f"• `edit <msg_id> nội_dung` → sửa tin\n"
                  f"• `/proxy` → tắt relay")
    else:
        await maid_reply(update, "proxy_off")

# ── Encryption status ─────────────────────────────────────────────
async def cmd_encstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not CRYPTO_AVAILABLE:
        await safe_reply(update, "⚠️ *Chưa cài cryptography!*\n`pip install cryptography`\nSau khi cài, bot tự mã hóa khi lưu config ạ~")
        return
    raw = {}
    if CONFIG_FILE.exists():
        try: raw = json.loads(CONFIG_FILE.read_text("utf-8"))
        except Exception: pass
    enc_f, plain_f = [], []
    for field in _SENSITIVE_FIELDS:
        val = raw.get(field, "")
        def is_enc(v):
            if isinstance(v, str):   return v.startswith("ENC:")
            if isinstance(v, list):  return all(not x or (isinstance(x, str) and x.startswith("ENC:")) for x in v)
            if isinstance(v, dict):  return all(not x or (isinstance(x, str) and x.startswith("ENC:")) for x in v.values())
            return False
        if is_enc(val):  enc_f.append(field)
        elif val:        plain_f.append(field)
        else:            enc_f.append(field)
    enc_list   = ", ".join(f"`{x}`" for x in enc_f)   or "không có"
    plain_list = ", ".join(f"`{x}`" for x in plain_f) or ""
    msg = (f"🔐 *TRẠNG THÁI MÃ HÓA*\n\n🗝️ Key: `{KEY_FILE}`\n\n✅ Đã mã hóa: {enc_list}")
    if plain_f:
        msg += f"\n⚠️ Chưa mã hóa: {plain_list}\nDùng `/encrekey` để mã hóa lại ạ~"
    else:
        msg += "\n✨ *Tất cả field nhạy cảm đã mã hóa!*"
    await safe_reply(update, msg)

async def cmd_encrekey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not CRYPTO_AVAILABLE:
        await safe_reply(update, "⚠️ *Chưa cài cryptography!*\n`pip install cryptography`")
        return
    if os.environ.get("CONFIG_FERNET_KEY", "").strip():
        await safe_reply(update,
            "⚠️ *Đang dùng key cố định từ env var* `CONFIG_FERNET_KEY`!\n"
            "Lệnh này không đổi được key trong trường hợp đó — hãy tự đổi giá trị "
            "env var rồi khởi động lại bot nếu muốn xoay key ạ~")
        return
    old_key = KEY_FILE.read_bytes() if KEY_FILE.exists() else None
    try:
        old_f     = Fernet(old_key) if old_key else None
        plain_cfg = _decrypt_config(CONFIG, old_f)
        new_key   = Fernet.generate_key()
        KEY_FILE.write_bytes(new_key)
        try: KEY_FILE.chmod(0o600)
        except Exception: pass
        new_f         = Fernet(new_key)
        encrypted_cfg = _encrypt_config(plain_cfg, new_f)
        CONFIG_FILE.write_text(
            json.dumps(encrypted_cfg, ensure_ascii=False, indent=2), "utf-8"
        )
        # [BIG UPDATE] Đồng bộ key mới + config mã hóa lại lên Postgres ngay —
        # nếu không, lần restart sau sẽ tự khôi phục NHẦM key cũ từ DB (vì
        # sync_encryption_key_with_db() ưu tiên key đã có trong Postgres) và
        # config trên Postgres vẫn còn mã hóa bằng key cũ → không giải mã được.
        db_synced = False
        try:
            import db_postgres
            if db_postgres.is_ready():
                db_postgres.config_save("_enc_key", {"key": new_key.decode()})
                db_postgres.config_save("settings", encrypted_cfg)
                db_synced = True
        except Exception:
            pass
        sync_note = ("🗄️ Đã đồng bộ key + config mới lên Postgres." if db_synced else
                     "⚠️ Postgres chưa sẵn sàng — key mới CHƯA đồng bộ, restart có thể bị lệch!")
        await safe_reply(update,
            f"✅ *Đã tạo key mới và mã hóa lại config!*\n🗝️ `{KEY_FILE}`\n"
            f"{sync_note}\n"
            f"⚠️ Backup file key này — mất key = mất config!")
    except Exception as e:
        if old_key: KEY_FILE.write_bytes(old_key)
        await safe_reply(update, f"❌ *Lỗi rekey:* `{str(e)}`")



# ════════════════════════════════════════════════════════════════
# LOCK / UNLOCK NHÓM

async def cmd_lock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if update.effective_chat.type == "private":
        await maid_reply(update, "not_group"); return
    try:
        await context.bot.set_chat_permissions(
            update.effective_chat.id,
            ChatPermissions(can_send_messages=False, can_send_polls=False,
                             can_send_other_messages=False, can_add_web_page_previews=False,
                             can_change_info=False, can_invite_users=False, can_pin_messages=False))
        await maid_reply(update, "lock_done")
    except TelegramError as e:
        await safe_reply(update, f"❌ *Lỗi:* `{e}`")

async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if update.effective_chat.type == "private":
        await maid_reply(update, "not_group"); return
    try:
        await context.bot.set_chat_permissions(
            update.effective_chat.id,
            ChatPermissions(can_send_messages=True, can_send_polls=True,
                             can_send_other_messages=True, can_add_web_page_previews=True,
                             can_change_info=False, can_invite_users=True, can_pin_messages=False))
        await maid_reply(update, "unlock_done")
    except TelegramError as e:
        await safe_reply(update, f"❌ *Lỗi:* `{e}`")



# ════════════════════════════════════════════════════════════════
# CANCEL — hủy thao tác reply/edit đang chờ trong DM
# ════════════════════════════════════════════════════════════════

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return

    chat = update.effective_chat

    # 1) Hủy pending reply/edit thủ công trong DM
    had_pending = bool(context.user_data.get("pending_reply_to") or context.user_data.get("pending_edit_msg"))
    context.user_data.pop("pending_reply_to", None)
    context.user_data.pop("pending_edit_msg", None)

    # 2) Hủy task AI đang chạy cho chat này (nếu có)
    cancelled_ai = False
    try:
        from ai_cancel import AI_ACTIVE_TASKS
        task = AI_ACTIVE_TASKS.get(chat.id)
        if task and not task.done():
            task.cancel()
            cancelled_ai = True
    except Exception:
        pass

    if cancelled_ai:
        await safe_reply(update, "🛑 *Đã hủy!* AI đang suy nghĩ đã bị dừng lại ạ~")
    else:
        await maid_reply(update, "cancel_done" if had_pending else "cancel_none")


# ════════════════════════════════════════════════════════════════
# BACKUP / RESTORE
# ════════════════════════════════════════════════════════════════

async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Đóng gói toàn bộ config + DB thành file JSON và gửi về DM owner."""
    if not await require_owner(update): return

    owner_id = CONFIG.get("owner_id", 0)

    # Tạo snapshot plaintext (giải mã toàn bộ) để file backup có thể dùng ngay
    f = _get_fernet()
    plain_cfg = _decrypt_config(CONFIG.copy(), f)

    # Bỏ token Telegram vì mỗi bot chỉ có 1 token (không thể import sang bot khác)
    plain_cfg.pop("token", None)

    # Thống kê không cần thiết khi restore — giữ lại để tham khảo nhưng đánh dấu rõ
    snapshot = {
        "_backup_meta": {
            "version":    "1.0",
            "created_at": now_vn().isoformat(),
            "note":       (
                "File backup này chứa toàn bộ cấu hình bot (trừ TELEGRAM_TOKEN).\n"
                "Dùng lệnh /restore (reply vào file này) để nạp lại.\n"
                "⚠️ BẢO MẬT: file có chứa Gemini API key — không chia sẻ công khai!"
            ),
        },
        "config": plain_cfg,
        "db": {
            k: DB.get(k)
            for k in ("notes", "filters", "warns", "users", "rules",
                       "admins", "blocked", "audit_log",
                       "phrase_media", "thinking_media")
        },
    }

    backup_bytes = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
    ts = now_vn().strftime("%Y%m%d_%H%M%S")
    filename = f"maidbot_backup_{ts}.json"

    # Gửi file về DM owner
    try:
        import io
        await context.bot.send_document(
            chat_id=owner_id,
            document=io.BytesIO(backup_bytes),
            filename=filename,
            caption=to_markdown_v2(
                f"🗄️ *Backup Bot — {now_vn().strftime('%d/%m/%Y %H:%M')} (ICT)*\n\n"
                f"✅ Đã đóng gói toàn bộ config + DB.\n"
                f"⚠️ Không có TELEGRAM_TOKEN (mỗi bot 1 token riêng).\n"
                f"🔑 Gemini API key đã được kèm theo.\n\n"
                f"📥 Để khôi phục: reply vào file này và gõ `/restore`"
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        # Nếu lệnh gọi từ nhóm, thông báo ngắn ở nhóm
        if update.effective_chat.id != owner_id:
            await safe_reply(update, "✅ *Đã gửi file backup về DM của ngài rồi ạ~*")
    except Exception as e:
        await safe_reply(update, f"❌ *Lỗi gửi backup:* `{str(e)}`")


async def cmd_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nạp lại config + DB từ file backup (reply vào file .json)."""
    if not await require_owner(update): return

    msg = update.message

    # Phải reply vào một tin có file
    replied = msg.reply_to_message
    if not replied or not replied.document:
        await safe_reply(update,
            "📥 *Cách dùng:* Reply vào file backup `.json` rồi gõ `/restore`\n"
            "_(Dùng `/backup` để tạo file backup trước)_"
        )
        return

    doc = replied.document
    if not (doc.file_name or "").endswith(".json"):
        await safe_reply(update, "❌ *Chỉ hỗ trợ file `.json`* — hãy reply đúng file backup ạ.")
        return

    # Giới hạn kích thước file (5 MB) để tránh OOM
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await safe_reply(update, "❌ File quá lớn (> 5 MB), không thể restore.")
        return

    status_msg = await safe_reply(update, "⏳ *Đang tải và kiểm tra file backup...*")

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        import io, httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(tg_file.file_path)
            raw_bytes = resp.content

        try:
            snapshot = json.loads(raw_bytes.decode("utf-8"))
        except Exception:
            await safe_reply(update, "❌ *Không đọc được file* — có thể bị hỏng hoặc không phải JSON.")
            return

        # Kiểm tra cấu trúc cơ bản
        if "_backup_meta" not in snapshot or "config" not in snapshot or "db" not in snapshot:
            await safe_reply(update,
                "❌ *File không đúng định dạng backup* — thiếu key `_backup_meta`, `config` hoặc `db`."
            )
            return

        backup_cfg: dict = snapshot["config"]
        backup_db:  dict = snapshot["db"]

        # ── Restore config ──────────────────────────────────────────
        # Không ghi đè token (không có trong file, nhưng phòng trường hợp có)
        backup_cfg.pop("token", None)
        # Chỉ restore các field hợp lệ
        from config import DEFAULT_CONFIG
        restored_cfg_keys = []
        for k, v in backup_cfg.items():
            if k in DEFAULT_CONFIG:
                CONFIG[k] = v
                restored_cfg_keys.append(k)

        # ── Restore DB ──────────────────────────────────────────────
        db_valid_keys = ("notes", "filters", "warns", "users", "rules",
                          "admins", "blocked", "audit_log",
                          "phrase_media", "thinking_media")
        restored_db_keys = []
        for k in db_valid_keys:
            if k in backup_db:
                DB[k] = backup_db[k]
                restored_db_keys.append(k)

        # Lưu xuống disk + Postgres
        save_config(CONFIG)
        from config import save_db
        save_db(DB)

        meta = snapshot.get("_backup_meta", {})
        created_at_raw = meta.get("created_at", "")
        try:
            from datetime import datetime, timezone, timedelta
            dt = datetime.fromisoformat(created_at_raw)
            created_str = dt.strftime("%d/%m/%Y %H:%M (ICT)")
        except Exception:
            created_str = created_at_raw[:16].replace("T", " ") if created_at_raw else "không rõ"

        await safe_reply(update,
            f"✅ *Restore thành công!*\n\n"
            f"📅 *Backup tạo lúc:* {created_str}\n"
            f"⚙️ *Config đã restore:* {len(restored_cfg_keys)} field\n"
            f"🗂️ *DB đã restore:* {', '.join(restored_db_keys) or 'không có'}\n\n"
            f"💾 Đã lưu vào disk + PostgreSQL.\n"
            f"♻️ Nếu cần, hãy restart bot để áp dụng toàn bộ."
        )

    except Exception as e:
        await safe_reply(update, f"❌ *Lỗi restore:* `{str(e)}`")


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    uptime_str = fmt_duration((now_vn() - PROCESS_START).total_seconds())
    db_ready = False
    try:
        import db_postgres
        db_ready = db_postgres.is_ready()
    except Exception:
        pass
    await safe_reply(update,
        f"🌸 *MAID BOT* — phiên bản `{BOT_VERSION}`\n\n"
        f"⏱️ *Đã chạy:* {uptime_str}\n"
        f"🗄️ *PostgreSQL:* {'🟢 Kết nối' if db_ready else '🔴 Chưa kết nối'}\n\n"
        f"📋 *Tính năng chính:*\n"
        f"• AI Gemini tích hợp search (Google + Facebook)\n"
        f"• Giám sát nhóm real-time, tóm tắt AI\n"
        f"• Relay DM ↔ nhóm (proxy mode)\n"
        f"• Promote/Demote bất kỳ user hoặc bot\n"
        f"• Lock/Unlock nhóm, ban/kick/mute/warn\n"
        f"• Mã hóa config\n\n"
        f"💝 *Gõ `/help` để xem đầy đủ sách lệnh ạ~*"
    )
