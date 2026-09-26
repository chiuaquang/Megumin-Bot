"""
bot.py — Entry point chính: handle_message, handle_callback, main().
"""

import asyncio
import re
import os
import time
from datetime import datetime

from aiohttp import web, ClientSession, ClientTimeout

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ChatMemberHandler, filters, ContextTypes,
)

# ── Core ──────────────────────────────────────────────────────────
from config import (
    CONFIG, DB, save_config, save_db,
    DATA_DIR, CRYPTO_AVAILABLE, now_vn, VN_TZ,
    restore_config_from_db, restore_db_from_db, sync_encryption_key_with_db,
)
from utils import (
    is_owner, track_user, safe_reply, maid, maid_reply,
    MSG_BUFFER, MEDIA_GROUP_PENDING,
    extract_msg_id_from_link, _action_kb, match_bot_trigger, _get_alt_triggers,
    vision_register_album_photo, vision_get_album_photos, vlog,
    nf_register_album_msg,
    BOT_VERSION, PROCESS_START, fmt_duration,
    send_styled, send_thinking_status,
    set_phrase_media, del_phrase_media, get_phrase_media,
    set_thinking_media, del_thinking_media, get_thinking_media,
    find_matching_filter, filter_is_allowed, send_filter_response, is_chat_admin,
    is_group_scope, send_note_response, NOTES_FILTERS_KEY, migrate_notes_filters_to_global,
    pop_pending_overwrite, cleanup_variant_media, normalize_note, normalize_variants,
    get_filter_view,
    HELP_SECTIONS, build_help_overview, build_help_keyboard, build_help_detail_keyboard,
    to_markdown_v2, escape_markdown_v2,
)
from ai_module import (
    AI_HISTORY, AI_PERSONA_PRESETS, GEMINI_MODELS_FREE, _get_all_ai_keys,
    _gemini_chat_with_tools, _gemini_chat_with_grounding, _gemini_chat_with_ddg_search,
    get_all_personas, is_builtin_persona,
)
from ai_cancel import AI_ACTIVE_TASKS  # noqa: F401 — re-exported for handlers_basic

# ── Handlers ──────────────────────────────────────────────────────
from handlers_basic import (
    cmd_start, cmd_help, cmd_id, cmd_stats, cmd_info,
    cmd_promote, cmd_apromote, cmd_demote, cmd_settitle,
    cmd_ban, cmd_unban, cmd_kick, cmd_mute, cmd_unmute,
    cmd_pin, cmd_unpin, cmd_del, cmd_purge,
    cmd_warn, cmd_unwarn, cmd_warns,
    cmd_note, cmd_get, cmd_notes, cmd_delnote,
    cmd_filter, cmd_stopfilter, cmd_filters, cmd_fn, _build_filter_entry, _filter_detail_text,
    cmd_setmedia, cmd_delmedia, cmd_listmedia,
    cmd_setthinkingmedia, cmd_delthinkingmedia,
    cmd_setrules, cmd_rules,
    cmd_setgroup, cmd_setbufferlimit, cmd_proxy, cmd_listusers, cmd_broadcast,
    cmd_block, cmd_unblock, cmd_deldm, cmd_editdm,
    cmd_encstatus, cmd_encrekey,
    cmd_lock, cmd_unlock,
    cmd_cancel, cmd_version,
    cmd_backup, cmd_restore,
    _do_promote_full, _do_demote, _resolve_target_dm,
)
from handlers_ai import (
    cmd_ai_toggle, cmd_ai_key, cmd_ai_add_key, cmd_ai_list_keys,
    cmd_ai_del_key, cmd_ai_models, cmd_ai_model, cmd_ai_persona,
    cmd_ai_sys, cmd_ai_clear,
    cmd_ai_persona_new, cmd_ai_persona_del, cmd_ai_persona_edit,
    cmd_ai_memory, cmd_ai_memory_clear,
    cmd_searchdebug, cmd_aithinking, cmd_aigrounding,
    cmd_monitor, cmd_feed, cmd_search_feed, cmd_clearfeed,
    cmd_addkw, cmd_delkw, cmd_listkw, cmd_groupstatus, cmd_tomtat,
    cmd_dbstats, cmd_cleandb,
)
from handlers_monitor import _buf_entry, _push_monitor, _flush_media_group

# ════════════════════════════════════════════════════════════════
# SHARED AI VISION HELPER
# ════════════════════════════════════════════════════════════════

async def _run_vision_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                target_msg, prompt: str, log_tag: str = "vision") -> None:
    """Thu thập ảnh (kể cả album), hiện status 'đang phân tích', gọi Gemini Vision,
    rồi xóa status và trả lời. Dùng chung cho cả group/DM, ảnh trực tiếp hoặc reply."""
    from ai_module import _gemini_chat_vision

    msg = update.effective_message
    chat = update.effective_chat
    status_msg = None
    try:
        # Thu thập tất cả ảnh trong album (nếu target_msg thuộc media_group)
        album_msgs = vision_get_album_photos(target_msg.media_group_id) if target_msg.media_group_id else []
        if not album_msgs:
            album_msgs = [target_msg]

        images = []
        for m in album_msgs[:10]:
            if m.photo:
                fobj = await context.bot.get_file(m.photo[-1].file_id)
                mt   = "image/jpeg"
            elif m.document and m.document.mime_type and m.document.mime_type.startswith("image/"):
                fobj = await context.bot.get_file(m.document.file_id)
                mt   = m.document.mime_type or "image/jpeg"
            else:
                continue
            ib = await fobj.download_as_bytearray()
            images.append((bytes(ib), mt))

        if not images:
            return

        status_msg = await send_thinking_status(
            msg,
            f"*{get_all_personas().get(CONFIG.get('ai_persona',''), {}).get('name', 'AI').split(' (')[0]} đang phân tích ảnh..* 🔍",
        )

        async def _do_vision():
            if len(images) == 1:
                return await _gemini_chat_vision(chat.id, images[0][0], images[0][1], prompt)
            return await _gemini_chat_vision(chat.id, images, "", prompt)

        task = asyncio.ensure_future(_do_vision())
        AI_ACTIVE_TASKS[chat.id] = task
        try:
            ai_reply = await task
            try: await status_msg.delete()
            except Exception: pass
            await safe_reply(update, ai_reply)
        except asyncio.CancelledError:
            try: await status_msg.delete()
            except Exception: pass
        finally:
            AI_ACTIVE_TASKS.pop(chat.id, None)
    except Exception as e:
        if status_msg:
            try: await status_msg.delete()
            except Exception: pass
        vlog(f"{log_tag} vision error chat_id={chat.id} err={e}")
        await safe_reply(update, f"❌ *Lỗi phân tích ảnh:* `{e}`")


async def _run_voice_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               target_msg, prompt: str, log_tag: str = "voice") -> None:
    """Tải voice message, hiện status 'đang nghe', gọi Gemini nghe trực tiếp
    (không qua bước speech-to-text riêng), rồi xóa status và trả lời."""
    from ai_module import _gemini_chat_voice

    msg  = update.effective_message
    chat = update.effective_chat
    status_msg = None
    try:
        voice = target_msg.voice or target_msg.audio
        if not voice:
            return
        fobj = await context.bot.get_file(voice.file_id)
        ab   = await fobj.download_as_bytearray()
        mime = getattr(voice, "mime_type", None) or "audio/ogg"

        status_msg = await send_thinking_status(
            msg,
            f"*{get_all_personas().get(CONFIG.get('ai_persona',''), {}).get('name', 'AI').split(' (')[0]} đang nghe..* 🎧",
        )
        task = asyncio.ensure_future(_gemini_chat_voice(chat.id, bytes(ab), mime, prompt))
        AI_ACTIVE_TASKS[chat.id] = task
        try:
            ai_reply = await task
            try: await status_msg.delete()
            except Exception: pass
            await safe_reply(update, ai_reply)
        except asyncio.CancelledError:
            try: await status_msg.delete()
            except Exception: pass
        finally:
            AI_ACTIVE_TASKS.pop(chat.id, None)
    except Exception as e:
        if status_msg:
            try: await status_msg.delete()
            except Exception: pass
        vlog(f"{log_tag} voice error chat_id={chat.id} err={e}")
        await safe_reply(update, f"❌ *Lỗi nghe ghi âm:* `{e}`")


async def _run_video_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               target_msg, prompt: str, log_tag: str = "video") -> None:
    """Tải video ngắn, hiện status 'đang xem', gọi Gemini xem trực tiếp,
    rồi xóa status và trả lời. Chỉ dùng cho video NGẮN gửi inline."""
    from ai_module import _gemini_chat_video

    msg  = update.effective_message
    chat = update.effective_chat
    status_msg = None
    try:
        vid = target_msg.video or target_msg.video_note or target_msg.animation
        if not vid:
            return
        # Chặn video quá nặng — inline Part.from_bytes chỉ phù hợp file nhỏ,
        # tránh timeout/lỗi tốn quota vô ích với video dài/nặng.
        file_size = getattr(vid, "file_size", 0) or 0
        if file_size > 19 * 1024 * 1024:
            await safe_reply(update, "⚠️ *Video quá nặng (>19MB)*, hiện chỉ xem được video ngắn/nhẹ ạ~")
            return

        fobj = await context.bot.get_file(vid.file_id)
        vb   = await fobj.download_as_bytearray()
        mime = getattr(vid, "mime_type", None) or "video/mp4"

        status_msg = await send_thinking_status(
            msg,
            f"*{get_all_personas().get(CONFIG.get('ai_persona',''), {}).get('name', 'AI').split(' (')[0]} đang xem video..* 🎬",
        )
        task = asyncio.ensure_future(_gemini_chat_video(chat.id, bytes(vb), mime, prompt))
        AI_ACTIVE_TASKS[chat.id] = task
        try:
            ai_reply = await task
            try: await status_msg.delete()
            except Exception: pass
            await safe_reply(update, ai_reply)
        except asyncio.CancelledError:
            try: await status_msg.delete()
            except Exception: pass
        finally:
            AI_ACTIVE_TASKS.pop(chat.id, None)
    except Exception as e:
        if status_msg:
            try: await status_msg.delete()
            except Exception: pass
        vlog(f"{log_tag} video error chat_id={chat.id} err={e}")
        await safe_reply(update, f"❌ *Lỗi xem video:* `{e}`")


async def _run_text_analysis(update: Update, target_text: str, extra_prompt: str) -> None:
    """Hiện status 'đang phân tích nội dung', gọi Gemini phân tích text, rồi xóa status và trả lời."""
    from ai_module import _gemini_text_analyze

    msg  = update.effective_message
    chat = update.effective_chat
    status_msg = None
    try:
        status_msg = await send_thinking_status(
            msg,
            f"*{get_all_personas().get(CONFIG.get('ai_persona',''), {}).get('name', 'AI').split(' (')[0]} đang phân tích nội dung..* 🔍",
        )
        task = asyncio.ensure_future(_gemini_text_analyze(chat.id, target_text, extra_prompt))
        AI_ACTIVE_TASKS[chat.id] = task
        try:
            ai_reply = await task
            try: await status_msg.delete()
            except Exception: pass
            await safe_reply(update, ai_reply)
        except asyncio.CancelledError:
            try: await status_msg.delete()
            except Exception: pass
        finally:
            AI_ACTIVE_TASKS.pop(chat.id, None)
    except Exception as e:
        if status_msg:
            try: await status_msg.delete()
            except Exception: pass
        await safe_reply(update, f"❌ *Lỗi phân tích:* `{e}`")


def _build_group_ai_context(msg, chat_id: int, max_msgs: int = 20, max_chars: int = 3500) -> str:
    """Dựng khối 'bối cảnh nhóm' để gắn kèm mỗi lần AI được gọi TRONG NHÓM,
    giúp Bot đọc hiểu hội thoại xung quanh giống một thành viên bình
    thường — thay vì chỉ thấy đúng câu chữ owner gõ ra sau tên trigger, và
    không bắt buộc owner phải reply thì bot mới "đọc" được.

    Gồm 3 phần, xếp theo thứ tự ưu tiên (phần đầu KHÔNG bao giờ bị cắt nếu
    vượt max_chars — chỉ phần lịch sử mở rộng ở cuối mới bị cắt bớt):
      1. ĐỐI TƯỢNG MẶC ĐỊNH cho "tin nhắn này/nó/đó/đi":
         - Nếu owner có reply → dùng đúng tin đang reply (lấy trực tiếp từ
           Telegram, luôn đầy đủ dù MSG_BUFFER có lưu hay không).
         - Nếu KHÔNG reply → dùng tin NGAY TRƯỚC ĐÓ trong nhóm (từ
           MSG_BUFFER) — vì đây là cách một người bình thường đọc nhóm sẽ
           hiểu "dịch đi"/"tóm tắt đi" nói tới cái gì.
      2. Hướng dẫn rõ: ưu tiên đối tượng mặc định ở trên, KHÔNG lấy nhầm từ
         lịch sử hội thoại CŨ giữa AI và owner (vd 1 video đã phân tích từ
         trước) — đây chính là lỗi đã gặp (Bot lấy nhầm "[Video]" của
         một lượt phân tích cũ thay vì tin chữ mới nhất trong nhóm).
      3. N tin nhắn gần nhất trong nhóm (ngữ cảnh mở rộng, không bắt buộc).

    QUAN TRỌNG: khối này chỉ được gắn vào `system_instruction` của LƯỢT GỌI
    HIỆN TẠI (xem _gemini_chat_with_key_and_tools), KHÔNG lưu vào AI_HISTORY,
    để lịch sử hội thoại đã lưu không bị phình to/lặp lại mỗi lượt chat.
    """
    def _fmt_media_type(e_or_msg, is_buffer_entry: bool) -> str:
        if is_buffer_entry:
            return {
                "photo": "ảnh", "video": "video", "sticker": "sticker",
                "voice": "voice", "audio": "audio", "file": "file",
                "gif": "gif", "vidnote": "video note", "poll": "poll",
            }.get(e_or_msg.get("type", "text"), e_or_msg.get("type", "media"))
        return "media"

    header = ("[BỐI CẢNH NHÓM — phần này hệ thống tự đính kèm mỗi lượt, "
              "KHÔNG phải do owner gõ ra]")

    default_block = None   # khối mô tả đối tượng mặc định (reply HOẶC tin ngay trước đó)
    has_reply_target = False

    # 1a) Có reply cụ thể → ưu tiên tuyệt đối, dùng luôn.
    reply_msg = getattr(msg, "reply_to_message", None)
    if reply_msg is not None:
        r_text = (reply_msg.text or reply_msg.caption or "").strip()
        r_user = reply_msg.from_user
        r_label = "NGÀI/CHỦ NHÂN CỦA BẠN" if (r_user and is_owner(r_user.id)) else (
            (r_user.full_name if r_user else "?"))
        if r_text:
            default_block = (
                "[ĐỐI TƯỢNG MẶC ĐỊNH cho \"tin nhắn này\"/\"tin này\"/\"nó\"/\"cái đó\" — "
                "owner đang REPLY thẳng vào tin này]\n"
                f"{r_label}: {r_text[:1500]}"
            )
            has_reply_target = True
        elif reply_msg.photo or reply_msg.video or reply_msg.sticker or reply_msg.document or reply_msg.voice:
            default_block = (
                "[ĐỐI TƯỢNG MẶC ĐỊNH] — owner đang reply vào 1 tin media "
                "(ảnh/video/voice/file), không có chữ/caption để đọc trực tiếp ở đây. "
                "Nếu owner yêu cầu dịch/đọc nội dung, hãy nói rõ là không nhận được "
                "nội dung chữ của tin media đó, đừng bịa ra nội dung."
            )
            has_reply_target = True

    # Lấy trước lịch sử buffer (dùng chung cho cả bước 1b và bước 3).
    try:
        recent = [e for e in MSG_BUFFER
                  if e.get("chat_id") == chat_id and e.get("msg_id") != msg.message_id]
    except Exception:
        recent = []
    recent = recent[-max_msgs:]

    # 1b) KHÔNG có reply → đối tượng mặc định là tin NGAY TRƯỚC ĐÓ trong nhóm.
    if not has_reply_target and recent:
        last_e = recent[-1]
        last_text = (last_e.get("text") or "").strip()
        last_label = "NGÀI/CHỦ NHÂN CỦA BẠN" if is_owner(last_e.get("user_id", 0)) else last_e.get("name", "?")
        if last_text:
            default_block = (
                "[ĐỐI TƯỢNG MẶC ĐỊNH cho \"tin nhắn này\"/\"tin đó\"/\"nó\"/\"cái đó\" — "
                "owner KHÔNG reply cụ thể, nên đây là tin NGAY TRƯỚC ĐÓ trong nhóm]\n"
                f"{last_label}: {last_text[:1500]}"
            )
        else:
            default_block = (
                f"[ĐỐI TƯỢNG MẶC ĐỊNH] — tin ngay trước đó trong nhóm là "
                f"{_fmt_media_type(last_e, True)}, không có chữ/caption. Nếu owner yêu cầu "
                "dịch/đọc nội dung, hãy nói rõ là không nhận được nội dung chữ, đừng bịa."
            )

    # 2) Hướng dẫn ưu tiên — chặn đúng lỗi đã gặp: AI lấy nhầm 1 lượt phân
    #    tích video/ảnh CŨ trong lịch sử hội thoại riêng (AI_HISTORY) thay vì
    #    tin nhắn mới nhất trong nhóm.
    priority_note = (
        "[QUY TẮC ƯU TIÊN — BẮT BUỘC]\nKhi owner dùng từ mơ hồ không chỉ rõ đối tượng "
        "(vd \"dịch đi\", \"dịch tin nhắn này\", \"tóm tắt đi\", \"nó nói gì vậy\"), LUÔN "
        "hiểu theo khối [ĐỐI TƯỢNG MẶC ĐỊNH] ở trên — dù đó là tin đang reply hay tin ngay "
        "trước đó trong nhóm. TUYỆT ĐỐI KHÔNG lấy nội dung từ các lượt chat CŨ giữa bạn và "
        "owner trước đây (vd 1 video/ảnh bạn từng phân tích ở lượt trước) trừ khi owner nói "
        "RÕ RÀNG là đang tiếp tục hỏi về đúng thứ đó (vd \"video lúc nãy đó\", \"cái ảnh em "
        "vừa xem\")."
    )

    parts = [header]
    if default_block:
        parts.append(default_block)
    parts.append(priority_note)

    # 3) Lịch sử chat mở rộng (không bắt buộc, chỉ để nắm mạch chuyện rộng
    #    hơn) — phần DUY NHẤT có thể bị cắt bớt nếu vượt max_chars.
    lines: list[str] = []
    for e in recent:
        text = (e.get("text") or "").strip()
        if not text:
            continue
        label = "NGÀI/CHỦ NHÂN CỦA BẠN" if is_owner(e.get("user_id", 0)) else e.get("name", "?")
        try:
            ts_str = datetime.fromisoformat(e["ts"]).astimezone(VN_TZ).strftime("%H:%M")
        except Exception:
            ts_str = ""
        lines.append(f"{ts_str} {label}: {text[:300]}")
    if lines:
        parts.append(
            "[LỊCH SỬ CHAT GẦN ĐÂY TRONG NHÓM — chỉ để nắm ngữ cảnh rộng hơn như một "
            "thành viên đang đọc nhóm, KHÔNG dùng để suy ra đối tượng mặc định (xem quy "
            "tắc ưu tiên ở trên), KHÔNG cần nhắc lại/tóm tắt trừ khi được yêu cầu]\n"
            + "\n".join(lines)
        )

    if len(parts) <= 1:  # chỉ có header, không có gì đáng gắn kèm
        return ""

    fixed = "\n\n".join(parts[:3])          # header + default_block + priority_note — giữ nguyên
    rest  = "\n\n".join(parts[3:])          # phần lịch sử mở rộng — có thể bị cắt
    if rest:
        budget = max_chars - len(fixed) - 2
        if budget > 100:
            block = fixed + "\n\n" + rest[:budget]
        else:
            block = fixed
    else:
        block = fixed
    return block



async def _run_ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, query_text: str,
                        extra_context: str = "") -> None:
    """Hiện status 'đang suy nghĩ', gọi Gemini agent mode (tự gọi tool quản
    trị nhóm khi cần + tự search khi cần), rồi xóa status và trả lời.

    Xác định `action_chat_id` — nhóm mà các tool ban/mute/note... sẽ tác
    động lên — theo thứ tự ưu tiên:
      1. Đang chat ngay trong 1 nhóm  → dùng nhóm đó.
      2. Đang chat trong DM           → dùng `target_group_id` đã /setgroup
         (nếu có); nếu chưa setgroup, AI vẫn trả lời nhưng KHÔNG thể tác
         động lên nhóm nào (tool sẽ tự báo lỗi thiếu context khi được gọi).

    Task được đăng ký vào AI_ACTIVE_TASKS[chat_id] để /cancel có thể huỷ
    ngay lập tức mà không cần đợi AI trả lời xong.
    """
    msg  = update.effective_message
    chat = update.effective_chat

    if chat.type != "private":
        action_chat_id = chat.id
    else:
        action_chat_id = CONFIG.get("target_group_id", 0) or None

    status_msg = await send_thinking_status(
        msg,
        f"*{get_all_personas().get(CONFIG.get('ai_persona',''), {}).get('name', 'AI').split(' (')[0]} đang suy nghĩ..* 💭",
    )

    async def _do_ai():
        if action_chat_id:
            return await _gemini_chat_with_tools(
                chat.id, query_text, context.bot, action_chat_id, extra_context=extra_context
            )
        if CONFIG.get("ai_grounding", True):
            return await _gemini_chat_with_grounding(chat.id, query_text)
        return await _gemini_chat_with_ddg_search(chat.id, query_text)

    task = asyncio.ensure_future(_do_ai())
    AI_ACTIVE_TASKS[chat.id] = task
    try:
        ai_reply = await task
        try: await status_msg.delete()
        except Exception: pass
        await safe_reply(update, ai_reply)
    except asyncio.CancelledError:
        try: await status_msg.delete()
        except Exception: pass
        # Không gửi gì thêm — /cancel đã tự thông báo
    finally:
        AI_ACTIVE_TASKS.pop(chat.id, None)

# ════════════════════════════════════════════════════════════════
# TAG COMMAND DISPATCHER (dùng trong nhóm: @bot /lệnh)
# ════════════════════════════════════════════════════════════════

_TAG_HANDLER_MAP = None

def _get_tag_map():
    global _TAG_HANDLER_MAP
    if _TAG_HANDLER_MAP is None:
        _TAG_HANDLER_MAP = {
            "ban": cmd_ban, "unban": cmd_unban, "kick": cmd_kick,
            "mute": cmd_mute, "unmute": cmd_unmute,
            "warn": cmd_warn, "unwarn": cmd_unwarn, "warns": cmd_warns,
            "pin": cmd_pin, "unpin": cmd_unpin,
            "del": cmd_del, "purge": cmd_purge,
            "promote": cmd_promote, "apromote": cmd_apromote, "demote": cmd_demote,
            "settitle": cmd_settitle, "info": cmd_info,
            "id": cmd_id, "help": cmd_help, "stats": cmd_stats,
            "rules": cmd_rules, "notes": cmd_notes,
            "lock": cmd_lock, "unlock": cmd_unlock,
        }
    return _TAG_HANDLER_MAP

async def handle_tag_command(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              cmd: str, args: list) -> bool:
    """Chỉ chạy lệnh cố định (cmd_* + lời thoại maid set sẵn) khi owner gõ
    ĐÚNG cú pháp lệnh chuẩn có dấu '/' ngay sau khi tag bot (vd '@bot /ban
    @user spam'). Nếu gõ trần không có '/' (vd '@bot ban Long vì spam') —
    tức ngôn ngữ tự nhiên — trả về False để rơi xuống AI agent xử lý, AI sẽ
    tự suy luận và gọi đúng tool qua function calling thay vì khớp cứng
    theo tên lệnh."""
    if not cmd.startswith("/"):
        return False
    fn = _get_tag_map().get(cmd[1:].lower())
    if not fn:
        return False
    context.args = args
    await fn(update, context)
    return True

# ════════════════════════════════════════════════════════════════
# CALLBACK QUERY HANDLER
# ════════════════════════════════════════════════════════════════

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if not is_owner(query.from_user.id):
        await query.answer()
        return
    data = query.data or ""

    # ── /filters: xem chi tiết đầy đủ 1 filter (gửi tin riêng, không sửa
    # đè lên bảng tổng quan để vẫn bấm xem được các filter khác) ──
    if data.startswith("fview:"):
        pkey = data.split(":", 1)[1]
        filter_key = get_filter_view(pkey)
        if not filter_key:
            await query.answer("⌛ Nút đã hết hạn, gõ lại /filters nhé~", show_alert=True)
            return
        chat_key = NOTES_FILTERS_KEY
        value = DB.get("filters", {}).get(chat_key, {}).get(filter_key)
        if value is None:
            await query.answer("❓ Filter này không còn tồn tại ạ~", show_alert=True)
            return
        await query.answer()
        try:
            await query.message.reply_text(
                to_markdown_v2(_filter_detail_text(filter_key, value)),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception:
            pass
        return

    # ── /help: điều hướng giữa bảng tổng quan và chi tiết từng nhóm ──
    if data == "help:home":
        await query.answer()
        try:
            await query.message.edit_text(
                build_help_overview(), parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=build_help_keyboard(),
            )
        except Exception:
            pass
        return
    if data.startswith("help:"):
        key = data.split(":", 1)[1]
        sec = HELP_SECTIONS.get(key)
        if not sec:
            await query.answer("❓ Không tìm thấy nhóm lệnh này ạ~", show_alert=True)
            return
        await query.answer()
        try:
            await query.message.edit_text(
                to_markdown_v2(sec["body"]), parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=build_help_detail_keyboard(),
            )
        except Exception:
            pass
        return

    # ── Note/Filter: xác nhận ghi đè khi đang có ≥2 câu random (%%%) ──
    if data.startswith("ovwconfirm:") or data.startswith("ovwcancel:"):
        is_confirm = data.startswith("ovwconfirm:")
        pkey = data.split(":", 1)[1]
        payload = pop_pending_overwrite(pkey)
        if not payload:
            await query.answer("⌛ Yêu cầu đã hết hạn rồi ạ~", show_alert=True)
            try:
                await query.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            return
        if not is_confirm:
            await query.answer("❌ Đã huỷ, giữ nguyên nội dung cũ ạ~", show_alert=False)
            try:
                await query.message.edit_text(to_markdown_v2("❌ *Đã huỷ ghi đè, giữ nguyên nội dung cũ ạ~*"), parse_mode=ParseMode.MARKDOWN_V2)
            except Exception:
                pass
            return

        await query.answer("✅ Đã ghi đè!", show_alert=False)
        if payload["kind"] == "note":
            chat_key, name = payload["chat_key"], payload["name"]
            new_variants = payload["new_variants"]
            old_note = normalize_note(DB["notes"].get(chat_key, {}).get(name))
            cleanup_variant_media(old_note["variants"], old_note.get("media"), new_variants)
            entry = {"variants": new_variants, "media": []}
            DB["notes"].setdefault(chat_key, {})[name] = entry
            save_db(DB)
            tag = " 📎" if any(v.get("media") for v in new_variants) else ""
            try:
                await query.message.edit_text(
                    to_markdown_v2(f"✅ *#{name}* đã ghi đè! (`{len(new_variants)}` câu){tag}"),
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            except Exception:
                pass
        else:  # filter
            chat_key, primary = payload["chat_key"], payload["primary"]
            triggers     = payload["triggers"]
            new_variants = payload["new_variants"]
            existing     = DB["filters"].get(chat_key, {}).get(primary)
            entry = _build_filter_entry(
                triggers, new_variants, payload["flags"],
                payload["preview"], payload["rules_btn"], existing,
            )
            if isinstance(existing, dict):
                cleanup_variant_media(existing.get("variants"), existing.get("media"), new_variants)
            DB["filters"].setdefault(chat_key, {})[primary] = entry
            save_db(DB)
            trig_list = ", ".join(f"`{c}`" for c, _ in triggers)
            tag = " 📎" if any(v.get("media") for v in new_variants) else ""
            try:
                await query.message.edit_text(
                    to_markdown_v2(f"✅ Filter {trig_list} đã ghi đè! (`{len(new_variants)}` câu){tag}"),
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            except Exception:
                pass
        return

    # ── AI: đổi model ──
    if data.startswith("aimdl:"):
        model = data[6:]
        CONFIG["gemini_model"] = model
        save_config(CONFIG)
        await query.answer(f"✅ Đã chuyển sang {model}!", show_alert=False)
        kb_rows = []
        for i in range(0, len(GEMINI_MODELS_FREE), 2):
            row = []
            for m in GEMINI_MODELS_FREE[i:i+2]:
                mark = "✅ " if m == model else ""
                row.append(InlineKeyboardButton(f"{mark}{m}", callback_data=f"aimdl:{m}"))
            kb_rows.append(row)
        try:
            await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(kb_rows))
        except Exception:
            pass
        await query.message.reply_text(f"✅ *Đã đổi model sang:*\n`{model}` ạ~", parse_mode="Markdown")
        return

    # ── AI: đổi persona ──
    if data.startswith("aipersona:"):
        persona_key = data[10:]
        all_personas = get_all_personas()
        if persona_key in all_personas:
            CONFIG["ai_persona"] = persona_key
            if persona_key != "custom":
                CONFIG["ai_system_prompt"] = all_personas[persona_key]["prompt"]
            save_config(CONFIG)
            AI_HISTORY.clear()
            persona = all_personas[persona_key]
            await query.answer(f"✅ Đã chọn {persona['name']}!", show_alert=False)
            kb_rows, row = [], []
            for i, (key, info) in enumerate(all_personas.items()):
                mark = "✅ " if key == persona_key else ""
                btn  = InlineKeyboardButton(f"{mark}{info['emoji']} {info['name']}",
                                            callback_data=f"aipersona:{key}")
                row.append(btn)
                if len(row) == 2:
                    kb_rows.append(row); row = []
            if row: kb_rows.append(row)
            try:
                await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(kb_rows))
            except Exception:
                pass
            await query.message.reply_text(
                f"🎭 *Đã đổi nhân vật sang:*\n"
                f"{persona['emoji']} {persona['name']}\n_(Lịch sử đã xóa)_",
                parse_mode="Markdown")
        return

    # ── Proxy/Monitor action buttons ──
    await query.answer()
    gid = CONFIG.get("target_group_id", 0)
    if not gid:
        await maid_reply(query, "no_group_set"); return
    try:
        prefix, raw_id = data.split(":", 1)
        target_id = int(raw_id)
    except ValueError:
        return

    if prefix == "mr":
        context.user_data["pending_reply_to"] = target_id
        await query.message.reply_text(f"📝 *Nhập nội dung reply vào tin #{target_id}:*",
                                        parse_mode="Markdown")
    elif prefix == "md":
        try:
            await context.bot.delete_message(gid, target_id)
            await query.message.edit_reply_markup(reply_markup=None)
            await maid_reply(query, "delete_done")
        except TelegramError as e:
            await query.message.reply_text(f"❌ *Lỗi xóa:* `{e}`", parse_mode="Markdown")
    elif prefix == "me":
        context.user_data["pending_edit_msg"] = target_id
        await query.message.reply_text(f"✏️ *Nhập nội dung mới cho tin #{target_id}:*",
                                        parse_mode="Markdown")

# ════════════════════════════════════════════════════════════════
# CHAT MEMBER TRACKING (join/leave/promote/demote)
# ════════════════════════════════════════════════════════════════

async def on_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cache username ngay khi có sự kiện member thay đổi (join nhóm, leave,
    bị kick, được promote/demote...) — kể cả khi người đó CHƯA từng gửi 1
    tin nhắn nào. Mở rộng độ phủ của lookup_user_id_by_username() ra ngoài
    phạm vi "chỉ ai đã chat", vì track_user() trong handle_message() chỉ
    chạy khi có tin nhắn thật."""
    cmu = update.chat_member
    if not cmu or not cmu.new_chat_member:
        return
    track_user(cmu.new_chat_member.user)

# ════════════════════════════════════════════════════════════════
# MAIN MESSAGE HANDLER
# ════════════════════════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    chat = update.effective_chat
    user = update.effective_user

    # ══════════════════════════════════════════════════════════
    # CHANNEL POST (bài đăng kênh) — không có user gửi (đăng dưới tên
    # kênh) nên không thể chạy các lệnh/trigger cần quyền owner. Chỉ
    # cho filter/note tự động khớp & trả lời ngay trong kênh đó.
    # ══════════════════════════════════════════════════════════
    if chat.type == "channel":
        if not msg.text:
            return
        text_lower = msg.text.strip().lower()
        chat_key   = NOTES_FILTERS_KEY  # note/filter dùng chung 1 kho global

        if text_lower.startswith("#"):
            note_name = text_lower[1:].split()[0]
            note = DB.get("notes", {}).get(chat_key, {}).get(note_name)
            if note is not None:
                await send_note_response(update, note, note_name)
            return

        flist_chan = DB.get("filters", {}).get(chat_key, {})
        matched = find_matching_filter(flist_chan, text_lower)
        if matched is not None:
            if isinstance(matched, dict) and "triggers" in matched:
                # Channel post không có user thật → chỉ filter "all" mới khớp,
                # filter {admin}/{user}/{allow_bot} cần user nên bỏ qua ở đây.
                if matched.get("access", "all") == "all":
                    await send_filter_response(update, matched)
            else:
                if isinstance(matched, dict):
                    await send_styled(update, matched.get("text", ""), matched.get("media"))
                else:
                    await safe_reply(update, matched)
        return

    if not user:
        return

    track_user(user)
    CONFIG["stats"]["total_messages"] += 1
    if CONFIG["stats"]["total_messages"] % 100 == 0:
        save_config(CONFIG)

    # ══════════════════════════════════════════════════════════
    # TRIGGER "xóa" → thay cho /del
    # Bắt buộc tin nhắn CHỈ chứa đúng y nguyên từ "xóa" (không thêm/bớt
    # ký tự nào, kể cả dấu câu hay khoảng trắng giữa chữ), phải reply
    # vào tin muốn xóa, và chỉ owner mới dùng được.
    # ══════════════════════════════════════════════════════════
    if msg.text and msg.text.strip().lower() == "xóa" and msg.reply_to_message:
        if not is_owner(user.id):
            return
        try:
            await context.bot.delete_message(
                chat.id, msg.reply_to_message.message_id)
            await msg.delete()
        except TelegramError as e:
            await safe_reply(update, f"❌ *Lỗi xóa:* `{e}`")
        return

    # ══════════════════════════════════════════════════════════
    # TRIGGER "nâng tôi lên admin" → promote chính người gửi
    # TRIGGER "hạ chức tôi" → demote chính người gửi
    # Bắt buộc tin nhắn CHỈ chứa đúng y nguyên trigger (không thêm/bớt
    # ký tự nào), chỉ owner mới dùng được.
    # ══════════════════════════════════════════════════════════
    if msg.text and msg.text.strip().lower()  == "nâng tôi lên admin":
        if not is_owner(user.id):
            return
        gid_self = CONFIG.get("target_group_id", 0)
        if not gid_self:
            await maid_reply(update, "no_group_set")
            return
        try:
            await _do_promote_full(context.bot, gid_self, user.id)
            if user.id not in DB["admins"]:
                DB["admins"].append(user.id); save_db(DB)
            await maid_reply(update, "promote_full", extra=f"👤 *{user.full_name}*")
        except TelegramError as e:
            await safe_reply(update, f"❌ *Lỗi promote:* `{e}`")
        return

    if msg.text and msg.text.strip().lower()  == "hạ chức tôi":
        if not is_owner(user.id):
            return
        gid_self = CONFIG.get("target_group_id", 0)
        if not gid_self:
            await maid_reply(update, "no_group_set")
            return
        try:
            await _do_demote(context.bot, gid_self, user.id)
            if user.id in DB["admins"]:
                DB["admins"].remove(user.id); save_db(DB)
            await maid_reply(update, "demote_done", extra=f"👤 *{user.full_name}*")
        except TelegramError as e:
            await safe_reply(update, f"❌ *Lỗi demote:* `{e}`")
        return

    # ══════════════════════════════════════════════════════════
    # GROUP HANDLER
    # ══════════════════════════════════════════════════════════
    if chat.type in ("group", "supergroup"):
        gid = CONFIG.get("target_group_id", 0)

        # Buffer & monitor
        if gid and chat.id == gid:
            entry = _buf_entry(msg)
            kws   = CONFIG.get("monitor_keywords", [])
            if not kws or any(k in entry["text"].lower() for k in kws) \
                       or any(k in entry["name"].lower() for k in kws):
                MSG_BUFFER.append(entry)

                # Lưu vào maid-bot-db (PostgreSQL) NGAY, bất kể /monitor
                # đang bật hay tắt — /monitor giờ chỉ điều khiển việc đẩy
                # tin nhắn real-time ra DM owner để quan sát nhóm.
                try:
                    import db_postgres
                    if db_postgres.is_ready():
                        db_postgres.msg_buffer_push(
                            entry, max_keep=CONFIG.get("msg_buffer_limit", 5000)
                        )
                except Exception:
                    pass

                if CONFIG.get("monitor_mode"):
                    mg_id = msg.media_group_id
                    if mg_id:
                        pending = MEDIA_GROUP_PENDING.setdefault(
                            mg_id, {"msgs": [], "entry": entry, "task": None}
                        )
                        pending["msgs"].append(msg)
                        if pending["task"] and not pending["task"].done():
                            pending["task"].cancel()
                        pending["task"] = asyncio.create_task(
                            _flush_media_group(context.bot, mg_id)
                        )
                    else:
                        await _push_monitor(context.bot, entry, original_msg=msg)

        # Đăng ký ảnh thuộc album (media_group_id) để hỗ trợ phân tích nhiều ảnh
        if msg.media_group_id and (msg.photo or msg.document):
            vision_register_album_photo(msg)
        # Đăng ký mọi media thuộc album cho /note và /filter (TTL dài hơn nhiều
        # — xem nf_register_album_msg trong utils.py)
        if msg.media_group_id and (msg.photo or msg.video or msg.animation or msg.document):
            nf_register_album_msg(msg)

        # AI Vision trong nhóm — phân tích ảnh khi:
        #  1. Tin nhắn có ảnh + caption gọi trigger (Bot/@bot), hoặc reply bot
        #  2. Reply vào ảnh của BẤT KỲ AI (kể cả người khác) kèm trigger trong text
        #  3. "Bot phân tích" + reply vào tin nhắn văn bản → phân tích nội dung text
        if CONFIG.get("ai_mode") and is_owner(user.id):
            bot_uname_g = CONFIG.get("bot_username", "").lstrip("@").lower()

            msg_has_image = bool(
                msg.photo or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/"))
            )
            reply_msg = msg.reply_to_message
            reply_has_image = bool(
                reply_msg and (
                    reply_msg.photo
                    or (reply_msg.document and reply_msg.document.mime_type and reply_msg.document.mime_type.startswith("image/"))
                )
            )
            reply_has_text = bool(reply_msg and reply_msg.text and not reply_has_image)

            caption_grp = (msg.caption or msg.text or "").strip()
            is_triggered, trig_remainder = match_bot_trigger(caption_grp, bot_uname_g)
            # Cũng cho phép @bot/Bot xuất hiện ở bất kỳ đâu trong caption (không chỉ đầu câu)
            if not is_triggered and bot_uname_g and f"@{bot_uname_g}" in caption_grp.lower():
                is_triggered   = True
                trig_remainder = re.sub(rf"@{re.escape(bot_uname_g)}", "", caption_grp, flags=re.IGNORECASE).strip()
            if not is_triggered:
                for _t in _get_alt_triggers():
                    if _t in caption_grp.lower():
                        is_triggered   = True
                        trig_remainder = re.sub(rf"\b{re.escape(_t)}\b", "", caption_grp, flags=re.IGNORECASE).strip()
                        break

            _analyze_keywords = ["phân tích", "phan tich", "analyze", "analysis"]
            wants_analysis = any(k in trig_remainder.lower() for k in _analyze_keywords)

            is_reply_to_bot_img = (
                reply_msg
                and reply_msg.from_user
                and reply_msg.from_user.is_bot
                and reply_msg.from_user.username
                and reply_msg.from_user.username.lower() == bot_uname_g
            )

            target_msg, prompt_grp = None, trig_remainder

            if msg_has_image and (is_triggered or is_reply_to_bot_img):
                target_msg = msg
            elif (not msg_has_image) and msg.text and reply_has_image and is_triggered:
                # Reply vào ảnh của người khác (hoặc của mình) kèm trigger
                target_msg = reply_msg

            # Case 3: "Bot phân tích" + reply vào tin nhắn TEXT (không ảnh)
            if target_msg is None and is_triggered and wants_analysis and reply_has_text:
                await _run_text_analysis(update, reply_msg.text, trig_remainder)
                return

            if target_msg is not None:
                await _run_vision_analysis(update, context, target_msg, prompt_grp, log_tag="group")
                return

            # AI nghe voice / xem video ngắn — cùng cơ chế trigger như ảnh
            # (cần tag bot/reply bot, không tự xử lý mọi voice/video gửi
            # ngẫu nhiên trong nhóm để tránh tốn quota bất ngờ).
            msg_has_voice = bool(msg.voice or msg.audio)
            msg_has_video = bool(msg.video or msg.video_note or msg.animation)
            reply_has_voice = bool(reply_msg and (reply_msg.voice or reply_msg.audio))
            reply_has_video = bool(reply_msg and (reply_msg.video or reply_msg.video_note or reply_msg.animation))

            voice_target, voice_prompt = None, trig_remainder
            if msg_has_voice and (is_triggered or is_reply_to_bot_img):
                voice_target = msg
            elif (not msg_has_voice) and msg.text and reply_has_voice and is_triggered:
                voice_target = reply_msg
            if voice_target is not None:
                await _run_voice_analysis(update, context, voice_target, voice_prompt, log_tag="group")
                return

            video_target, video_prompt = None, trig_remainder
            if msg_has_video and (is_triggered or is_reply_to_bot_img):
                video_target = msg
            elif (not msg_has_video) and msg.text and reply_has_video and is_triggered:
                video_target = reply_msg
            if video_target is not None:
                await _run_video_analysis(update, context, video_target, video_prompt, log_tag="group")
                return

        if not msg.text:
            return

        text       = msg.text.strip()
        text_lower = text.lower()
        chat_key   = NOTES_FILTERS_KEY  # note/filter dùng chung 1 kho global
        bot_uname  = CONFIG.get("bot_username", "").lstrip("@").lower()

        # Tag @bot hoặc "Bot" → lệnh hoặc AI
        is_tagged, remainder = match_bot_trigger(text, bot_uname)
        if is_tagged:
            if remainder:
                parts = remainder.split()
                if await handle_tag_command(update, context, parts[0], parts[1:]):
                    return
                if CONFIG.get("ai_mode") and is_owner(user.id) and remainder.strip():
                    grp_ctx = _build_group_ai_context(msg, chat.id)
                    await _run_ai_chat(update, context, remainder.strip(), extra_context=grp_ctx)
                    return
            return

        # Reply vào bot → AI
        if (CONFIG.get("ai_mode") and is_owner(user.id)
                and msg.reply_to_message
                and msg.reply_to_message.from_user
                and msg.reply_to_message.from_user.is_bot
                and msg.reply_to_message.from_user.username
                and msg.reply_to_message.from_user.username.lower() == bot_uname):
            grp_ctx = _build_group_ai_context(msg, chat.id)
            await _run_ai_chat(update, context, text.strip(), extra_context=grp_ctx)
            return

        # Ghi chú #tên — mặc định chỉ owner trigger được; /fn on mở cho mọi
        # thành viên trong nhóm (KHÔNG ảnh hưởng quyền tạo/sửa/xoá, luôn chỉ
        # owner làm được việc đó).
        if text_lower.startswith("#") and (is_owner(user.id) or CONFIG.get("fn_open")):
            note_name = text_lower[1:].split()[0]
            note = DB.get("notes", {}).get(chat_key, {}).get(note_name)
            if note is not None:
                await send_note_response(update, note, note_name)
                return

        # Quản lý nhanh filter qua chat thường: "<trigger> noformat" để lấy bản
        # thô (copy/sửa), "<trigger> force" để bật/tắt buộc reply cho filter
        # {user}. Chỉ owner / admin được dùng.
        flist_mgmt = DB.get("filters", {}).get(chat_key, {})
        for _suffix, _cmd in ((" noformat", "noformat"), (" force", "force")):
            if text_lower.endswith(_suffix):
                trig_candidate = text_lower[: -len(_suffix)].strip()
                hit = None
                for v in flist_mgmt.values():
                    if isinstance(v, dict) and "triggers" in v and any(
                        t["content"] == trig_candidate for t in v["triggers"]
                    ):
                        hit = v; break
                if hit is not None:
                    if not (is_owner(user.id) or await is_chat_admin(context.bot, chat.id, user.id)):
                        return
                    if _cmd == "noformat":
                        nvariants = normalize_variants(hit.get("variants"))
                        parts = []
                        for v in nvariants:
                            n_media = len(v.get("media") or [])
                            tag = f" [📎x{n_media}]" if n_media else ""
                            parts.append((v.get("text") or "") + tag)
                        raw_text = "\n%%%\n".join(parts) or "_(không có nội dung văn bản, chỉ có media)_"
                        await safe_reply(update, f"```\n{raw_text}\n```")
                    else:
                        if hit.get("access") != "user":
                            await safe_reply(update, "⚠️ *Chỉ filter dạng {user} mới bật được buộc reply ạ~*")
                        else:
                            hit["force_reply"] = not hit.get("force_reply", False)
                            save_db(DB)
                            state = "BẬT 🟢" if hit["force_reply"] else "TẮT 🔴"
                            await safe_reply(update, f"✅ *Đã {state} buộc reply cho filter `{trig_candidate}` ạ~*")
                    return

        # Filter auto-reply (hỗ trợ cả format mới [triggers/flags] & format cũ)
        # — mặc định chỉ owner trigger được; /fn on mở cho mọi thành viên
        # trong nhóm (KHÔNG ảnh hưởng quyền tạo/sửa/xoá filter).
        if not (is_owner(user.id) or CONFIG.get("fn_open")):
            return
        matched = find_matching_filter(flist_mgmt, text_lower)
        if matched is not None:
            if isinstance(matched, dict) and "triggers" in matched:
                if await filter_is_allowed(matched, context.bot, chat.id, user):
                    await send_filter_response(update, matched)
                return
            else:
                if isinstance(matched, dict):
                    await send_styled(update, matched.get("text", ""), matched.get("media"))
                else:
                    await safe_reply(update, matched)
                return
        return

    # ══════════════════════════════════════════════════════════
    # DM HANDLER
    # ══════════════════════════════════════════════════════════
    if chat.type != "private":
        return
    if user.id in DB.get("blocked", []):
        return
    if not is_owner(user.id):
        return

    gid = CONFIG.get("target_group_id", 0)

    # Pending reply từ nút monitor
    pending_reply = context.user_data.pop("pending_reply_to", None)
    if pending_reply:
        if gid and msg.text:
            try:
                sent = await context.bot.send_message(
                    gid, msg.text, reply_to_message_id=pending_reply
                )
                await update.message.reply_text(
                    maid("reply_done") + f"\n📩 Msg ID: `{sent.message_id}`",
                    reply_markup=_action_kb(sent.message_id),
                )
            except TelegramError as e:
                await safe_reply(update, f"❌ *Lỗi reply:* `{e}`")
        return

    # Pending edit từ nút monitor
    pending_edit = context.user_data.pop("pending_edit_msg", None)
    if pending_edit:
        if gid and msg.text:
            try:
                await context.bot.edit_message_text(
                    msg.text, chat_id=gid, message_id=pending_edit
                )
                await maid_reply(update, "edit_done")
            except TelegramError as e:
                await safe_reply(update, f"❌ *Lỗi sửa:* `{e}`")
        return

    # AI chat trong DM
    if CONFIG.get("ai_mode") and msg.text:
        text_dm      = msg.text.strip()
        bot_uname_dm = CONFIG.get("bot_username", "").lstrip("@").lower()
        is_tagged, tagged_remainder = match_bot_trigger(text_dm, bot_uname_dm)
        is_reply_to_bot  = (
            msg.reply_to_message
            and msg.reply_to_message.from_user
            and msg.reply_to_message.from_user.is_bot
        )
        is_plain_dm = not CONFIG.get("proxy_mode")

        # "Bot phân tích" + reply vào tin nhắn TEXT (không ảnh) → phân tích nội dung
        _analyze_keywords = ["phân tích", "phan tich", "analyze", "analysis"]
        reply_target = msg.reply_to_message
        reply_has_text_dm = bool(reply_target and reply_target.text and not reply_target.photo)
        if is_tagged and reply_has_text_dm and any(k in tagged_remainder.lower() for k in _analyze_keywords):
            await _run_text_analysis(update, reply_target.text, tagged_remainder)
            return

        if is_tagged or is_reply_to_bot or is_plain_dm:
            query_text = tagged_remainder if is_tagged else text_dm
            if query_text.strip():
                await _run_ai_chat(update, context, query_text.strip())
                return

    # AI Vision trong DM — gửi ảnh (có hoặc không có caption), hoặc reply ảnh kèm trigger
    msg_has_image_dm = bool(msg.photo or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/")))
    if msg.media_group_id and msg_has_image_dm:
        vision_register_album_photo(msg)

    reply_dm = msg.reply_to_message
    reply_has_image_dm = bool(
        reply_dm and (
            reply_dm.photo
            or (reply_dm.document and reply_dm.document.mime_type and reply_dm.document.mime_type.startswith("image/"))
        )
    )
    if CONFIG.get("ai_mode") and (msg_has_image_dm or (msg.text and reply_has_image_dm)):
        is_plain_dm = not CONFIG.get("proxy_mode")
        is_reply_to_bot = (
            msg.reply_to_message
            and msg.reply_to_message.from_user
            and msg.reply_to_message.from_user.is_bot
        )
        caption_dm   = (msg.caption or msg.text or "").strip()
        bot_uname_dm = CONFIG.get("bot_username", "").lstrip("@").lower()
        is_tagged_img, trig_remainder_dm = match_bot_trigger(caption_dm, bot_uname_dm)
        if not is_tagged_img and bot_uname_dm and f"@{bot_uname_dm}" in caption_dm.lower():
            is_tagged_img       = True
            trig_remainder_dm   = re.sub(rf"@{re.escape(bot_uname_dm)}", "", caption_dm, flags=re.IGNORECASE).strip()
        if not is_tagged_img:
            for _t in _get_alt_triggers():
                if _t in caption_dm.lower():
                    is_tagged_img     = True
                    trig_remainder_dm = re.sub(rf"\b{re.escape(_t)}\b", "", caption_dm, flags=re.IGNORECASE).strip()
                    break

        target_msg_dm, prompt_dm = None, caption_dm
        if msg_has_image_dm and (is_plain_dm or is_reply_to_bot or is_tagged_img):
            target_msg_dm = msg
        elif (not msg_has_image_dm) and msg.text and reply_has_image_dm and is_tagged_img:
            target_msg_dm = reply_dm
            prompt_dm = trig_remainder_dm

        if target_msg_dm is not None:
            await _run_vision_analysis(update, context, target_msg_dm, prompt_dm, log_tag="DM")
            return

    # AI nghe voice / xem video ngắn trong DM — cùng cơ chế như ảnh (DM riêng
    # với owner thì không cần trigger, gửi thẳng là được).
    if CONFIG.get("ai_mode"):
        is_plain_dm_media = not CONFIG.get("proxy_mode")
        is_reply_to_bot_media = bool(
            msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.is_bot
        )
        caption_media_dm = (msg.caption or msg.text or "").strip()
        bot_uname_media  = CONFIG.get("bot_username", "").lstrip("@").lower()
        is_tagged_media, trig_remainder_media = match_bot_trigger(caption_media_dm, bot_uname_media)
        if not is_tagged_media and bot_uname_media and f"@{bot_uname_media}" in caption_media_dm.lower():
            is_tagged_media = True
            trig_remainder_media = re.sub(rf"@{re.escape(bot_uname_media)}", "", caption_media_dm, flags=re.IGNORECASE).strip()

        msg_has_voice_dm = bool(msg.voice or msg.audio)
        msg_has_video_dm = bool(msg.video or msg.video_note or msg.animation)

        if msg_has_voice_dm and (is_plain_dm_media or is_reply_to_bot_media or is_tagged_media):
            await _run_voice_analysis(update, context, msg, trig_remainder_media or caption_media_dm, log_tag="DM")
            return
        if msg_has_video_dm and (is_plain_dm_media or is_reply_to_bot_media or is_tagged_media):
            await _run_video_analysis(update, context, msg, trig_remainder_media or caption_media_dm, log_tag="DM")
            return

    # Proxy tắt → báo ngủ
    if not CONFIG.get("proxy_mode"):
        await maid_reply(update, "bot_sleeping")
        return

    if not gid:
        await maid_reply(update, "no_group_set")
        return

    # ── Text relay ────────────────────────────────────────────
    if msg.text:
        text = msg.text.strip()
        tl   = text.lower()

        # promote / demote
        for prefix_cmd, fn, label in [
            ("promote ", _do_promote_full, "promote_full"),
            ("demote ",  _do_demote,       "demote_done"),
        ]:
            if tl.startswith(prefix_cmd):
                parts = text.split(None, 1)
                if len(parts) == 2:
                    context.args = [parts[1].strip()]
                    target = await _resolve_target_dm(update, context, gid)
                    if target:
                        try:
                            await fn(context.bot, gid, target.id)
                            if prefix_cmd.startswith("promote"):
                                if target.id not in DB["admins"]:
                                    DB["admins"].append(target.id); save_db(DB)
                            else:
                                if target.id in DB["admins"]:
                                    DB["admins"].remove(target.id); save_db(DB)
                            await maid_reply(update, label, extra=f"👤 *{target.full_name}*")
                        except TelegramError as e:
                            await safe_reply(update, f"❌ *Lỗi:* `{e}`")
                    else:
                        await safe_reply(update, "❓ *Không tìm thấy user!*")
                else:
                    await safe_reply(update, f"📝 *Dùng:* `{prefix_cmd.strip()} @username` ạ~")
                return

        # reply <id> nội_dung
        if tl.startswith("reply "):
            parts = text.split(" ", 2)
            if len(parts) >= 3:
                msg_id = extract_msg_id_from_link(parts[1])
                if msg_id:
                    try:
                        sent = await context.bot.send_message(
                            gid, parts[2], reply_to_message_id=msg_id
                        )
                        await update.message.reply_text(
                            maid("reply_done") + f"\n📩 Msg ID: `{sent.message_id}`",
                            reply_markup=_action_kb(sent.message_id),
                        )
                    except TelegramError as e:
                        await safe_reply(update, f"❌ *Lỗi reply:* `{e}`")
                else:
                    await safe_reply(update, "❓ *Không nhận ra msg_id ạ~*")
            else:
                await safe_reply(update, "📝 *Thiếu nội dung.* `reply <id> nội_dung` ạ~")
            return

        # del <id>
        if tl.startswith("del "):
            parts = text.split(" ", 1)
            if len(parts) == 2:
                msg_id = extract_msg_id_from_link(parts[1].strip())
                if msg_id:
                    try:
                        await context.bot.delete_message(gid, msg_id)
                        await maid_reply(update, "delete_done")
                    except TelegramError as e:
                        await safe_reply(update, f"❌ *Lỗi xóa:* `{e}`")
                else:
                    await safe_reply(update, "❓ *Không nhận ra msg_id ạ~*")
            else:
                await safe_reply(update, "🗑️ *Dùng:* `del <msg_id>` ạ~")
            return

        # edit <id> nội_dung
        if tl.startswith("edit "):
            parts = text.split(" ", 2)
            if len(parts) >= 3:
                msg_id = extract_msg_id_from_link(parts[1])
                if msg_id:
                    try:
                        await context.bot.edit_message_text(
                            parts[2], chat_id=gid, message_id=msg_id
                        )
                        await maid_reply(update, "edit_done")
                    except TelegramError as e:
                        await safe_reply(update, f"❌ *Lỗi sửa:* `{e}`")
                else:
                    await safe_reply(update, "❓ *Không nhận ra msg_id ạ~*")
            else:
                await safe_reply(update, "✏️ *Dùng:* `edit <msg_id> nội_dung` ạ~")
            return

        # Text thông thường → relay vào nhóm
        reply_to_id = None
        if msg.reply_to_message and msg.reply_to_message.text:
            m = re.search(r"Msg ID(?:\s*mới)?:\s*(\d+)", msg.reply_to_message.text)
            if m:
                reply_to_id = int(m.group(1))
        try:
            kwargs = {"reply_to_message_id": reply_to_id} if reply_to_id else {}
            sent   = await context.bot.send_message(gid, text, **kwargs)
            await update.message.reply_text(
                maid("relay_sent", msg_id=sent.message_id),
                reply_markup=_action_kb(sent.message_id),
            )
        except TelegramError as e:
            await safe_reply(update, f"❌ *Lỗi gửi:* `{e}`")
        return

    # ── Media relay ───────────────────────────────────────────
    caption = msg.caption or ""
    reply_to_id = None
    if msg.reply_to_message and msg.reply_to_message.text:
        m = re.search(r"Msg ID(?:\s*mới)?:\s*(\d+)", msg.reply_to_message.text)
        if m:
            reply_to_id = int(m.group(1))
    rk = {"reply_to_message_id": reply_to_id} if reply_to_id else {}

    try:
        sent = None
        media_type = "media"
        if msg.sticker:
            sent = await context.bot.send_sticker(gid, msg.sticker.file_id, **rk)
            media_type = "sticker"
        elif msg.photo:
            sent = await context.bot.send_photo(gid, msg.photo[-1].file_id, caption=caption or None, **rk)
            media_type = "ảnh"
        elif msg.video:
            sent = await context.bot.send_video(gid, msg.video.file_id, caption=caption or None, **rk)
            media_type = "video"
        elif msg.video_note:
            sent = await context.bot.send_video_note(gid, msg.video_note.file_id, **rk)
            media_type = "video note"
        elif msg.animation:
            sent = await context.bot.send_animation(gid, msg.animation.file_id, caption=caption or None, **rk)
            media_type = "GIF"
        elif msg.voice:
            sent = await context.bot.send_voice(gid, msg.voice.file_id, caption=caption or None, **rk)
            media_type = "voice"
        elif msg.audio:
            sent = await context.bot.send_audio(gid, msg.audio.file_id, caption=caption or None, **rk)
            media_type = "audio"
        elif msg.document:
            sent = await context.bot.send_document(gid, msg.document.file_id, caption=caption or None, **rk)
            media_type = "file"
        elif msg.location:
            sent = await context.bot.send_location(
                gid, latitude=msg.location.latitude, longitude=msg.location.longitude, **rk
            )
            media_type = "vị trí"
        elif msg.contact:
            sent = await context.bot.send_contact(
                gid, phone_number=msg.contact.phone_number,
                first_name=msg.contact.first_name,
                last_name=msg.contact.last_name or "", **rk,
            )
            media_type = "contact"
        elif msg.poll:
            p    = msg.poll
            sent = await context.bot.send_poll(
                gid, question=p.question,
                options=[o.text for o in p.options],
                is_anonymous=p.is_anonymous, **rk,
            )
            media_type = "poll"
        else:
            await maid_reply(update, "media_unsupported")
            return

        await update.message.reply_text(
            f"✅ *Đã relay {media_type} vào nhóm rồi ạ~*\n📩 Msg ID: `{sent.message_id}`",
            reply_markup=_action_kb(sent.message_id),
        )
    except TelegramError as e:
        await safe_reply(update, f"❌ *Lỗi relay media:* `{e}`")

# ════════════════════════════════════════════════════════════════
# KEEP-ALIVE: web server nhỏ + tự ping mỗi 10 phút
# ════════════════════════════════════════════════════════════════
# Render free Web Service cần 1 cổng HTTP đang lắng nghe, nếu không
# Render coi service "không khoẻ" và cycle/ngắt nó bất kể có UptimeRobot
# ping hay không. Ngoài ra, nếu không ai ping trong 15 phút thì Render
# vẫn cho service "ngủ". Nên ta tự chạy 1 web server nhỏ bằng aiohttp
# (chạy song song với run_polling trên cùng asyncio loop) để:
#   1) Render thấy có port mở → không coi service unhealthy
#   2) Cung cấp endpoint /health để UptimeRobot (hoặc ai khác) ping
#   3) Tự ping chính mình mỗi 10 phút như một lớp bảo hiểm thêm,
#      phòng trường hợp chưa kịp set UptimeRobot.

async def _health(request):
    return web.Response(text="ok")


async def _status(request):
    """[BIG UPDATE] Endpoint JSON chi tiết hơn /health — hữu ích để debug nhanh
    qua trình duyệt hoặc dùng cho dashboard/uptime-monitor nâng cao mà không
    cần đăng nhập Telegram."""
    db_ready = False
    try:
        import db_postgres
        db_ready = db_postgres.is_ready()
    except Exception:
        pass
    data = {
        "ok": True,
        "version": BOT_VERSION,
        "uptime_seconds": int((now_vn() - PROCESS_START).total_seconds()),
        "uptime_human": fmt_duration((now_vn() - PROCESS_START).total_seconds()),
        "ai_mode": bool(CONFIG.get("ai_mode", False)),
        "monitor_mode": bool(CONFIG.get("monitor_mode", False)),
        "proxy_mode": bool(CONFIG.get("proxy_mode", False)),
        "db_ready": db_ready,
        "target_group_set": bool(CONFIG.get("target_group_id", 0)),
    }
    return web.json_response(data)


async def _start_keepalive_server(app_ptb):
    """Chạy 1 aiohttp server nhỏ, bind vào $PORT (Render yêu cầu)."""
    port = int(os.environ.get("PORT", 10000))
    web_app = web.Application()
    web_app.router.add_get("/", _health)
    web_app.router.add_get("/health", _health)
    web_app.router.add_get("/status", _status)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    print(f"  🌐 Keep-alive server: lắng nghe port {port} (/, /health, /status)")
    return runner


async def _self_ping_loop(interval_seconds: int = 600):
    """Tự ping chính mình mỗi `interval_seconds` giây để Render không cho ngủ."""
    self_url = (
        os.environ.get("WEBHOOK_URL")
        or os.environ.get("RENDER_EXTERNAL_URL")
        or ""
    ).rstrip("/")

    if not self_url:
        print("  ⚠️  Self-ping: không có WEBHOOK_URL/RENDER_EXTERNAL_URL → bỏ qua tự ping "
              "(vẫn nên dùng UptimeRobot để ping từ bên ngoài)")
        return

    ping_url = f"{self_url}/health"
    timeout = ClientTimeout(total=15)

    # Chờ 1 chút cho server kịp khởi động hẳn trước khi ping lần đầu
    await asyncio.sleep(10)

    while True:
        try:
            async with ClientSession(timeout=timeout) as session:
                async with session.get(ping_url) as resp:
                    print(f"  🔁 Self-ping: {ping_url} → {resp.status}")
        except Exception as e:
            print(f"  ⚠️  Self-ping lỗi: {e}")
        await asyncio.sleep(interval_seconds)


async def _post_init(app_ptb):
    """Hook của python-telegram-bot, chạy sau khi Application khởi tạo xong
    nhưng trước khi bắt đầu polling — nơi lý tưởng để spawn server + ping loop
    trên cùng event loop mà run_polling đang dùng."""
    await _start_keepalive_server(app_ptb)
    asyncio.create_task(_self_ping_loop(600))  # 10 phút


async def _post_shutdown(app_ptb):
    """[BIG UPDATE] Hook của PTB chạy NGAY TRƯỚC KHI process dừng hẳn — kể cả
    khi dừng do nhận SIGTERM (Render gửi tín hiệu này mỗi lần redeploy/restart
    instance). Lưu lại CONFIG & DB lần cuối để không mất các thay đổi gần nhất
    (vd vài tin nhắn cuối chưa kịp tới mốc lưu định kỳ mỗi 100 tin)."""
    try:
        save_config(CONFIG)
        save_db(DB)
        print("  💾 [shutdown] Đã lưu CONFIG & DB lần cuối trước khi tắt.")
    except Exception as e:
        print(f"  ⚠️  [shutdown] Lỗi lưu state: {e}")


_last_error_notify_ts = 0.0

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """[BIG UPDATE] Bắt TẤT CẢ exception chưa được xử lý ở bất kỳ handler nào —
    trước đây nếu 1 handler crash, PTB chỉ log ra console rồi im lặng bỏ qua,
    ngài sẽ không biết bot đang lỗi cho tới khi tự nhận ra bot không phản hồi.
    Giờ sẽ luôn log đầy đủ traceback + báo ngay cho owner qua DM (gộp/giới hạn
    tần suất báo để tránh spam DM nếu lỗi xảy ra liên tục)."""
    global _last_error_notify_ts
    import traceback
    err = context.error
    tb  = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    print(f"⚠️  [UNHANDLED ERROR] {err!r}\n{tb[-2000:]}", flush=True)

    owner_id = CONFIG.get("owner_id", 0)
    now_ts   = time.time()
    if owner_id and (now_ts - _last_error_notify_ts > 30):
        _last_error_notify_ts = now_ts
        try:
            # str(err) là dữ liệu KHÔNG kiểm soát được (có thể chứa backtick,
            # ngoặc, bất kỳ ký tự nào) — escape thuần riêng phần này thay vì
            # để to_markdown_v2 cố nhận diện nó như code-span, tránh trường
            # hợp 1 backtick lẻ bên trong làm lệch ranh giới token.
            err_escaped = escape_markdown_v2(str(err)[:500])
            await context.bot.send_message(
                owner_id,
                to_markdown_v2("😱 *Ối! Có lỗi không mong muốn xảy ra ạ...*\n\n")
                + f"`{err_escaped}`"
                + to_markdown_v2(
                    "\n\n_Em đã ghi log đầy đủ vào console rồi, ngài kiểm tra giúp em nếu cần ạ~ "
                    "(thông báo lỗi sẽ tạm ẩn 30s nếu lỗi này lặp lại liên tục để khỏi làm phiền ngài)_"
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════

def main():
    # ── Khởi tạo PostgreSQL TRƯỚC TIÊN ────────────────────────────────────────
    # Phải làm bước này đầu tiên để khôi phục lại các setting (target_group_id,
    # monitor_mode, ai_persona, ...) đã lưu trước khi đọc/dùng CONFIG ở dưới.
    # Trên Render free, disk local bị reset mỗi lần restart/redeploy nên
    # config.json không đủ — PostgreSQL mới là nơi lưu sống sót qua restart.
    db_ok = False
    try:
        import db_postgres
        db_ok = db_postgres.init()
        if db_ok:
            # [BIG UPDATE] Đồng bộ key mã hóa TRƯỚC khi khôi phục config — nếu
            # không, restore_config_from_db() có thể dùng SAI key (key local
            # mới-tạo-do-disk-bị-reset) để giải mã dữ liệu đã mã hóa bằng key
            # cũ trong Postgres, dẫn tới hỏng toàn bộ token/API key đã lưu.
            sync_encryption_key_with_db()
            restored = restore_config_from_db()
            if restored:
                print(f"  ⚙️  Config: đã khôi phục {restored} setting từ PostgreSQL")
            # [BIG UPDATE] Khôi phục notes/filters/warns/users/rules/admins/
            # blocked/audit_log — TRƯỚC ĐÂY các dữ liệu này CHỈ sống trên disk
            # local nên mất hết mỗi lần Render free tier restart/redeploy.
            restored_db = restore_db_from_db()
            if restored_db:
                print(f"  🗂️  DB: đã khôi phục {restored_db} mục (notes/filters/warns/...) từ PostgreSQL")
        else:
            print("  ⚠️  PostgreSQL: không kết nối được (kiểm tra DATABASE_URL) → chạy in-memory only, mất hết khi restart")
    except Exception as e:
        print(f"  ⚠️  PostgreSQL init lỗi: {e}")

    # [BIG UPDATE] Gộp note/filter cũ (đang lưu rải theo từng chat_id) vào 1
    # kho `_global` chung duy nhất — owner lưu 1 lần, dùng được ở MỌI nhóm/
    # kênh/topic mà bot có mặt (trừ DM). Idempotent, an toàn gọi mỗi lần start.
    try:
        migrate_notes_filters_to_global()
    except Exception as e:
        print(f"  ⚠️  Lỗi gộp notes/filters về global: {e}")

    # ── Đọc token & owner từ env vars (ưu tiên) hoặc config (đã khôi phục ở trên) ──
    token    = os.environ.get("TELEGRAM_TOKEN") or CONFIG.get("token", "")
    owner_id = int(os.environ.get("OWNER_ID", 0)) or CONFIG.get("owner_id", 0)
    if owner_id:
        CONFIG["owner_id"] = owner_id

    if not token or token == "PASTE_BOT_TOKEN_HERE":
        print("❌ Chưa cấu hình TOKEN!  Set TELEGRAM_TOKEN env var hoặc config.json")
        return
    if CONFIG["owner_id"] == 0:
        print("⚠️  CẢNH BÁO: owner_id = 0 — set OWNER_ID env var!")

    # ── Load lại AI history & monitor buffer từ DB (nếu kết nối được) ─────────
    if db_ok:
        # Nạp lại toàn bộ lịch sử AI từ DB vào RAM
        loaded = db_postgres.load_all_history(max_per_conv=40)
        AI_HISTORY.update(loaded)
        print(f"  💾 PostgreSQL: OK — đã load {len(loaded)} cuộc hội thoại")

        # Nạp lại monitor buffer từ DB
        gid = CONFIG.get("target_group_id", 0)
        if gid:
            from utils import MSG_BUFFER
            db_msgs = db_postgres.msg_buffer_load(gid, limit=200)
            MSG_BUFFER.clear()
            MSG_BUFFER.extend(db_msgs)
            print(f"  📦 Monitor buffer: {len(MSG_BUFFER)} tin nhắn đã khôi phục")

    # Gemini key từ env
    gemini_key = os.environ.get("GEMINI_KEY", "").strip()
    if gemini_key:
        CONFIG["gemini_api_key"] = gemini_key

    app = Application.builder().token(token).post_init(_post_init).post_shutdown(_post_shutdown).build()
    app.add_error_handler(error_handler)  # [BIG UPDATE] bắt mọi exception chưa xử lý

    # Basic
    for cmd, fn in [
        ("start", cmd_start), ("help", cmd_help), ("id", cmd_id),
        ("stats", cmd_stats), ("info", cmd_info),
        ("rules", cmd_rules), ("notes", cmd_notes), ("get", cmd_get),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    # Monitor
    for cmd, fn in [
        ("monitor", cmd_monitor), ("feed", cmd_feed), ("search", cmd_search_feed),
        ("clearfeed", cmd_clearfeed), ("addkw", cmd_addkw), ("delkw", cmd_delkw),
        ("listkw", cmd_listkw), ("groupstatus", cmd_groupstatus), ("tomtat", cmd_tomtat),
        ("cleandb", cmd_cleandb),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    # Group management
    for cmd, fn in [
        ("warn", cmd_warn), ("unwarn", cmd_unwarn), ("warns", cmd_warns),
        ("mute", cmd_mute), ("unmute", cmd_unmute),
        ("kick", cmd_kick), ("ban", cmd_ban), ("unban", cmd_unban),
        ("pin", cmd_pin), ("unpin", cmd_unpin),
        ("xoa", cmd_del), ("del", cmd_del), ("purge", cmd_purge),
        ("note", cmd_note), ("delnote", cmd_delnote),
        ("filter", cmd_filter), ("stop", cmd_stopfilter), ("filters", cmd_filters),
        ("fn", cmd_fn),
        ("setmedia", cmd_setmedia), ("delmedia", cmd_delmedia), ("listmedia", cmd_listmedia),
        ("setthinkingmedia", cmd_setthinkingmedia), ("delthinkingmedia", cmd_delthinkingmedia),
        ("setrules", cmd_setrules),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    # [BIG UPDATE] Lock / cancel / version
    for cmd, fn in [
        ("lock", cmd_lock), ("unlock", cmd_unlock),
        ("cancel", cmd_cancel),
        ("version", cmd_version),
        ("backup", cmd_backup),
        ("restore", cmd_restore),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    # Owner & Admin
    for cmd, fn in [
        ("adm", cmd_promote), ("apromote", cmd_apromote),
        ("deladm", cmd_demote), ("the", cmd_settitle),
        ("setgroup", cmd_setgroup), ("setbufferlimit", cmd_setbufferlimit), ("proxy", cmd_proxy),
        ("block", cmd_block), ("unblock", cmd_unblock),
        ("listusers", cmd_listusers), ("broadcast", cmd_broadcast),
        ("deldm", cmd_deldm), ("editdm", cmd_editdm),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    # Search + Encryption
    app.add_handler(CommandHandler("searchdebug", cmd_searchdebug))
    app.add_handler(CommandHandler("aithinking",  cmd_aithinking))
    app.add_handler(CommandHandler("aigrounding", cmd_aigrounding))
    app.add_handler(CommandHandler("encstatus",   cmd_encstatus))
    app.add_handler(CommandHandler("encrekey",    cmd_encrekey))
    app.add_handler(CommandHandler("dbstats",     cmd_dbstats))

    # AI Gemini
    for cmd, fn in [
        ("ai",        cmd_ai_toggle),
        ("aikey",     cmd_ai_key),
        ("aiaddkey",  cmd_ai_add_key),
        ("aikeys",    cmd_ai_list_keys),
        ("aidelkey",  cmd_ai_del_key),
        ("model",  cmd_ai_models),
        ("aimodel",   cmd_ai_model),
        ("tinhcach", cmd_ai_persona),
        ("aisys",     cmd_ai_sys),
        ("aiclear",   cmd_ai_clear),
        ("aipersonanew",  cmd_ai_persona_new),
        ("aipersonadel",  cmd_ai_persona_del),
        ("aipersonaedit", cmd_ai_persona_edit),
        ("aimemory",      cmd_ai_memory),
        ("aimemoryclear", cmd_ai_memory_clear),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    # Bắt mọi join/leave/đổi quyền trong nhóm để cache username NGAY khi
    # họ vào nhóm — không cần chờ họ gõ tin nhắn đầu tiên mới track_user()
    # được. Đây vẫn là cơ chế cache nội bộ, Telegram không cấp API nào để
    # bot tự "liệt kê toàn bộ username thành viên" — bot chỉ biết về user
    # nào đó (id + username) qua: (1) tin nhắn họ gửi, (2) event chat_member
    # khi họ join/leave/được lên/xuống quyền, hoặc (3) reply/mention trực
    # tiếp. Không có cách nào enumerate hết member ẩn danh chưa từng tương
    # tác — Rose/Combot cũng bị giới hạn này, chỉ khác là họ cache sớm hơn.
    app.add_handler(ChatMemberHandler(on_chat_member_update, ChatMemberHandler.CHAT_MEMBER))

    # ── Banner ──
    all_ai_keys  = _get_all_ai_keys()
    ai_key_info  = f"✅ {len(all_ai_keys)} keys" if all_ai_keys else "❌ Chưa đặt"
    ai_persona   = CONFIG.get("ai_persona", "custom")
    persona_name = get_all_personas().get(ai_persona, {}).get("name", ai_persona)
    enc_s        = "🟢 cryptography" if CRYPTO_AVAILABLE else "🔴 pip install cryptography"

    print("=" * 55)
    print(f"   🌸🌸🌸 MAID BOT {BOT_VERSION} - ĐANG CHẠY 🌸🌸🌸")
    print("=" * 55)
    print(f"  👑 Owner      : {CONFIG['owner_id']}")
    print(f"  🤖 Username   : @{CONFIG.get('bot_username', 'chưa đặt')}")
    print(f"  🕐 Giờ VN     : {now_vn().strftime('%H:%M %d/%m/%Y')} ICT")
    print(f"  🧠 AI Gemini  : {'🟢 Bật' if CONFIG.get('ai_mode') else '🔴 Tắt'} | {ai_key_info} | {CONFIG.get('gemini_model', '-')}")
    print(f"  🎭 AI Nhân vật: {persona_name}")
    print(f"  📡 Proxy      : {'🟢 Bật' if CONFIG.get('proxy_mode') else '🔴 Tắt'}")
    print(f"  👁️  Monitor    : {'🟢 Bật' if CONFIG.get('monitor_mode') else '🔴 Tắt'}")
    print(f"  🏠 Group      : {CONFIG.get('target_group_id', 0)}")
    print(f"  💾 Data       : {DATA_DIR}")
    print(f"  🗄️  PostgreSQL : {'🟢 Kết nối — config & DB tự đồng bộ' if db_ok else '🔴 Chưa kết nối — dữ liệu mất khi restart!'}")
    print(f"  🔐 Mã hóa     : {enc_s}")
    print("-" * 55)
    print("  💝 Em đã sẵn sàng phục vụ ngài! 💝")
    print("=" * 55)
    print()

    # Python 3.11 — run_polling hoạt động bình thường
    print("  📡 Polling mode")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
