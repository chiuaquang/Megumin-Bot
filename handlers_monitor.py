"""
handlers_monitor.py — Buffer tin nhắn, render, push monitor DM, AI tóm tắt chat.

Lưu ý về kiến trúc lưu trữ (từ bản cập nhật này):
  - Mọi tin nhắn trong nhóm đích đều được lưu vào maid-bot-db (PostgreSQL)
    NGAY KHI nhận được, bất kể /monitor đang BẬT hay TẮT.
  - /monitor giờ CHỈ còn chức năng "quan sát nhóm": khi BẬT, mỗi tin nhắn
    mới sẽ được đẩy real-time vào DM của owner (qua _push_monitor). Khi
    TẮT, tin nhắn vẫn được lưu DB như thường, chỉ là không đẩy DM.
  - /tomtat (tóm tắt AI) đọc dữ liệu trực tiếp từ DB, nên hoạt động được
    kể cả khi /monitor chưa từng được bật.
  - Bản tóm tắt giờ được viết theo ĐÚNG persona AI đang chọn (/tinhcach),
    kể chi tiết sự kiện trong nhóm — không còn dùng giọng văn khách quan cố định.
"""

import asyncio
import re
from datetime import datetime, timezone

from telegram import Bot, InputMediaPhoto, InputMediaVideo
from telegram.error import TelegramError

from config import CONFIG, now_vn, VN_TZ
from rich_text import get_rich_text
import bot_identity
from utils import MSG_BUFFER, MEDIA_GROUP_PENDING, _action_kb, is_owner
from ai_module import GEMINI_AVAILABLE, GEMINI_MODELS_FREE, _get_all_ai_keys, _resolve_gemini_model

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    genai = None
    genai_types = None

# ── Buffer entry builder ──────────────────────────────────────────
def _buf_entry(msg) -> dict:
    user = msg.from_user
    name = user.full_name if user else "Unknown"
    uname = (user.username or "") if user else ""

    text  = msg.text or msg.caption or get_rich_text(msg) or ""
    mtype = "text"
    if    msg.photo:      mtype = "photo"
    elif  msg.video:      mtype = "video"
    elif  msg.sticker:    mtype = "sticker"
    elif  msg.voice:      mtype = "voice"
    elif  msg.audio:      mtype = "audio"
    elif  msg.document:   mtype = "file"
    elif  msg.animation:  mtype = "gif"
    elif  msg.video_note: mtype = "vidnote"
    elif  msg.poll:       mtype = "poll"

    return {
        "msg_id":   msg.message_id,
        "chat_id":  msg.chat.id,
        "user_id":  user.id if user else 0,
        "name":     name,
        "username": uname,
        "text":     text,
        "type":     mtype,
        "is_bot":   bool(user and user.is_bot),
        "ts":       now_vn().isoformat(),
        "msg":      msg,          # giữ object gốc để forward media
    }

# ── Entry renderer ────────────────────────────────────────────────
def _render_entry(entry: dict, idx: int = 0) -> str:
    uname    = f" @{entry['username']}" if entry.get("username") else ""
    name_str = f"*{entry['name']}*{uname}"
    try:
        dt = datetime.fromisoformat(entry["ts"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts_str = dt.astimezone(VN_TZ).strftime("%H:%M %d/%m")
    except Exception:
        ts_str = entry.get("ts", "")[:16].replace("T", " ")

    type_icon = {
        "photo": "📷", "video": "🎬", "sticker": "🎭",
        "voice": "🎙", "audio": "🎵", "file": "📎",
        "gif": "🎞", "vidnote": "📹", "poll": "📊",
    }.get(entry.get("type", "text"), "💬")

    text_preview = ""
    if entry.get("text"):
        preview = entry["text"][:200].replace("\n", " ")
        text_preview = f"\n_{preview}_"
    elif entry.get("type", "text") != "text":
        text_preview = f"\n_{type_icon} [{entry['type']}]_"

    idx_str = f"`#{idx}` " if idx else ""
    return f"{idx_str}{name_str} · 🕐`{ts_str}` · 📩`#{entry['msg_id']}`{text_preview}"

# ── Push single entry to owner DM ────────────────────────────────
# Lưu ý: việc lưu tin nhắn vào PostgreSQL (msg_buffer_push) KHÔNG nằm ở đây
# nữa — nó được gọi ngay khi buffer tin nhắn trong bot.py, độc lập với
# monitor_mode. _push_monitor() giờ chỉ lo việc đẩy tin nhắn ra DM owner
# để "quan sát nhóm" theo thời gian thực khi /monitor đang BẬT.
async def _push_monitor(bot: Bot, entry: dict, original_msg=None) -> None:
    owner_id = CONFIG.get("owner_id", 0)
    if not owner_id:
        return
    if bot_identity.is_self_id(entry.get("user_id", 0)):
        return                                   # tin của CHÍNH bot này: không chuyển sang DM

    msg_id  = entry["msg_id"]
    chat_id = entry["chat_id"]
    uname   = f" @{entry['username']}" if entry.get("username") else ""
    ts_str  = entry.get("ts", "")

    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts_str = dt.astimezone(VN_TZ).strftime("%H:%M %d/%m")
    except Exception:
        ts_str = ts_str[:16].replace("T", " ")

    header = (
        f"👁️ *[MONITOR]*\n"
        f"{'🤖' if entry.get('is_bot') else '👤'} *{entry['name']}*{uname}{' `[BOT]`' if entry.get('is_bot') else ''}\n"
        f"🕐 {ts_str} · 📩 `#{msg_id}`"
    )

    action_kb = _action_kb(msg_id)
    orig      = original_msg

    try:
        mtype = entry.get("type", "text")

        if mtype == "text" or not orig:
            text_body = entry.get("text", "")
            full = f"{header}\n\n{text_body[:3500]}" if text_body else header
            try:
                await bot.send_message(owner_id, full, parse_mode="Markdown", reply_markup=action_kb)
            except TelegramError:
                # Text của bot khác có thể chứa _ * ` làm Markdown lỗi → gửi lại dạng thường, KHÔNG làm mất tin
                plain = full.replace("*", "").replace("`", "")
                await bot.send_message(owner_id, plain, reply_markup=action_kb)

        elif mtype == "photo":
            caption_text = entry.get("text", "")
            caption = f"{header}\n\n{caption_text[:900]}" if caption_text else header
            await bot.send_photo(
                owner_id, orig.photo[-1].file_id,
                caption=caption[:1024], parse_mode="Markdown",
                reply_markup=action_kb,
            )

        elif mtype == "video":
            caption_text = entry.get("text", "")
            caption = f"{header}\n\n{caption_text[:900]}" if caption_text else header
            await bot.send_video(
                owner_id, orig.video.file_id,
                caption=caption[:1024], parse_mode="Markdown",
                reply_markup=action_kb,
            )

        elif mtype == "sticker":
            await bot.send_sticker(owner_id, orig.sticker.file_id)
            await bot.send_message(owner_id, header, parse_mode="Markdown", reply_markup=action_kb)

        elif mtype == "voice":
            await bot.send_voice(
                owner_id, orig.voice.file_id,
                caption=header[:1024], parse_mode="Markdown",
                reply_markup=action_kb,
            )

        elif mtype == "audio":
            await bot.send_audio(
                owner_id, orig.audio.file_id,
                caption=header[:1024], parse_mode="Markdown",
                reply_markup=action_kb,
            )

        elif mtype == "file":
            await bot.send_document(
                owner_id, orig.document.file_id,
                caption=header[:1024], parse_mode="Markdown",
                reply_markup=action_kb,
            )

        elif mtype == "gif":
            await bot.send_animation(
                owner_id, orig.animation.file_id,
                caption=header[:1024], parse_mode="Markdown",
                reply_markup=action_kb,
            )

        elif mtype == "vidnote":
            await bot.send_video_note(owner_id, orig.video_note.file_id)
            await bot.send_message(owner_id, header, parse_mode="Markdown", reply_markup=action_kb)

        elif mtype == "poll":
            await bot.send_message(
                owner_id,
                f"{header}\n\n📊 *[POLL]* {orig.poll.question}",
                parse_mode="Markdown", reply_markup=action_kb,
            )

        else:
            await bot.send_message(owner_id, header, parse_mode="Markdown", reply_markup=action_kb)

    except TelegramError as e:
        # Fallback: gửi text nếu media fail
        try:
            await bot.send_message(owner_id, f"{header}\n⚠️ _Lỗi forward media: {e}_",
                                   parse_mode="Markdown", reply_markup=action_kb)
        except Exception:
            pass

# ── Media group flush (album) ─────────────────────────────────────
async def _flush_media_group(bot: Bot, mg_id: str) -> None:
    await asyncio.sleep(1.2)  # đợi tất cả ảnh/video trong album về
    pending = MEDIA_GROUP_PENDING.pop(mg_id, None)
    if not pending:
        return

    msgs  = pending.get("msgs", [])
    entry = pending.get("entry", {})
    if not msgs:
        return

    owner_id = CONFIG.get("owner_id", 0)
    if not owner_id:
        return

    uname   = f" @{entry['username']}" if entry.get("username") else ""
    ts_str  = entry.get("ts", "")
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts_str = dt.astimezone(VN_TZ).strftime("%H:%M %d/%m")
    except Exception:
        ts_str = ts_str[:16].replace("T", " ")

    header = (
        f"👁️ *[MONITOR — ALBUM {len(msgs)} ảnh/video]*\n"
        f"{'🤖' if entry.get('is_bot') else '👤'} *{entry['name']}*{uname}{' `[BOT]`' if entry.get('is_bot') else ''}\n"
        f"🕐 {ts_str} · 📩 `#{entry['msg_id']}`"
    )

    media_group = []
    caption_used = False
    for i, m in enumerate(msgs[:10]):
        cap = header if not caption_used else None
        if m.photo:
            media_group.append(InputMediaPhoto(m.photo[-1].file_id, caption=cap, parse_mode="Markdown"))
            caption_used = True
        elif m.video:
            media_group.append(InputMediaVideo(m.video.file_id, caption=cap, parse_mode="Markdown"))
            caption_used = True

    try:
        if media_group:
            await bot.send_media_group(owner_id, media=media_group)
        else:
            await bot.send_message(owner_id, header, parse_mode="Markdown")
    except TelegramError as e:
        try:
            await bot.send_message(owner_id, f"{header}\n⚠️ _Lỗi album: {e}_", parse_mode="Markdown")
        except Exception:
            pass

# ── AI Chat Summarizer ────────────────────────────────────────────
async def _ai_summarize_chat(entries: list, n: int, mode: str = "n") -> str:
    """
    Tóm tắt lịch sử nhóm bằng Gemini, theo ĐÚNG tính cách (persona) AI đang
    được chọn trong /tinhcach — không còn dùng giọng văn khách quan cố định.
    Yêu cầu AI kể chi tiết tất cả sự kiện/diễn biến đáng chú ý trong nhóm,
    không chỉ tóm lược chung chung.

    `mode`: "n" (N tin gần nhất) | "ngay" (tin trong hôm nay) | "moi" (từ lần
    tóm tắt gần nhất) — chỉ ảnh hưởng tới câu mở đầu prompt, không đổi logic.

    Trả về chuỗi tóm tắt hoặc ''.
    """
    if not GEMINI_AVAILABLE or not _get_all_ai_keys():
        return ""

    lines: list[str] = []
    for e in entries:
        try:
            dt = datetime.fromisoformat(e["ts"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts_str = dt.astimezone(VN_TZ).strftime("%H:%M %d/%m")
        except Exception:
            ts_str = "??:?? ??/??"

        name  = e["name"]
        text  = e["text"].strip()
        mtype = e.get("type", "text")

        # Nếu người gửi là owner, KHÔNG đưa tên thật vào transcript — chỉ dùng
        # một nhãn cố định để AI buộc phải xưng "ngài"/"master" khi tường thuật,
        # không bị lẫn sang gọi tên thật.
        if is_owner(e.get("user_id", 0)):
            label = "NGÀI/CHỦ NHÂN CỦA BẠN"
        else:
            label = name

        if text:
            lines.append(f"{ts_str} [{label}]: {text[:300]}")
        elif mtype != "text":
            type_label = {
                "photo": "📷 [gửi ảnh]", "video": "🎬 [gửi video]",
                "sticker": "🎭 [gửi sticker]", "voice": "🎙 [gửi voice]",
                "audio": "🎵 [gửi audio]", "file": "📎 [gửi file]",
                "gif": "🎞 [gửi gif]", "vidnote": "📹 [video note]",
                "poll": "📊 [tạo poll]",
            }.get(mtype, f"[{mtype}]")
            lines.append(f"{ts_str} [{label}]: {type_label}")

    if not lines:
        return ""

    transcript = "\n".join(lines)
    vn_time    = now_vn().strftime("%H:%M ngày %d/%m/%Y")

    # ── Dùng đúng persona đang chọn (megumin/megumin_serious/custom...) ──
    # _gemini_get_sys_prompt() đã tự gắn thêm chỉ thị "phải chính xác, không
    # né tránh bằng tính cách" + thông tin thời gian thực, nên ở đây chỉ cần
    # thêm phần chỉ thị riêng cho việc TÓM TẮT NHÓM.
    from ai_module import _gemini_get_sys_prompt
    persona_sys_prompt = _gemini_get_sys_prompt()

    summary_instruction = (
        "\n\n[CHỈ THỊ RIÊNG CHO TÓM TẮT NHÓM]\n"
        "Bạn đang được giao nhiệm vụ ĐỌC LỊCH SỬ CHAT của một nhóm Telegram và "
        "TƯỜNG THUẬT LẠI một cách CHI TIẾT, ĐẦY ĐỦ mọi sự kiện/diễn biến đáng chú ý "
        "đã xảy ra trong nhóm — vẫn theo đúng văn phong, xưng hô và tính cách của bạn. "
        "KHÔNG bỏ sót sự kiện quan trọng nào (chủ đề được bàn, ai nói gì đáng chú ý, "
        "tranh luận/drama nếu có, ảnh/video/file được chia sẻ, quyết định hay kết luận). "
        "Vẫn phải khách quan về DỮ KIỆN (không bịa thêm chuyện không có trong chat), "
        "nhưng cách kể, bình luận, cảm xúc thì theo đúng tính cách của bạn.\n\n"
        "[QUAN TRỌNG VỀ XƯNG HÔ VỚI CHỦ NHÂN]\n"
        "Trong transcript dưới đây, bất kỳ dòng nào có tên người gửi là "
        "'NGÀI/CHỦ NHÂN CỦA BẠN' đều là tin nhắn của CHÍNH chủ nhân/owner của "
        "bạn — người bạn vẫn xưng hô là 'ngài' hoặc 'master' khi chat riêng. "
        "Khi tường thuật lại những gì người này nói hoặc làm trong tóm tắt, "
        "CHỈ gọi họ là 'ngài' hoặc 'master' (ví dụ: 'ngài đã nói...', "
        "'master đã chia sẻ...') — TUYỆT ĐỐI KHÔNG dùng tên thật nào khác cho "
        "họ, vì bạn không được biết tên thật của họ trong transcript này. "
        "Những người khác trong nhóm thì vẫn gọi bằng tên hiển thị của họ như "
        "bình thường."
    )

    mode_intro = {
        "ngay":  f"Đây là toàn bộ tin nhắn của nhóm TỪ ĐẦU NGÀY HÔM NAY ({now_vn().strftime('%d/%m/%Y')}) đến {vn_time}.",
        "moi":   "Đây là toàn bộ tin nhắn của nhóm KỂ TỪ LẦN TÓM TẮT GẦN NHẤT cho đến bây giờ.",
        "n":     f"Đây là {len(lines)} tin nhắn gần nhất trong nhóm.",
    }.get(mode, f"Đây là {len(lines)} tin nhắn gần nhất trong nhóm.")

    prompt = (
        f"Hiện tại là {vn_time} (giờ Việt Nam).\n\n"
        f"{mode_intro}\n\n"
        f"---\n{transcript[:8000]}\n---\n\n"
        "Hãy tường thuật/tóm tắt CHI TIẾT theo cấu trúc:\n"
        "• *Tổng quan*: (chuyện gì đã diễn ra trong khoảng thời gian này, 2-3 câu)\n"
        "• *Diễn biến chi tiết*: kể đầy đủ các sự kiện/chủ đề theo thứ tự thời gian, "
        "không gộp tắt, không bỏ sót — có thể nhiều mục nếu nhóm có nhiều chuyện\n"
        "• *Đáng chú ý/Kết luận*: (nếu có quyết định, kết quả, hoặc điều cần lưu ý)\n\n"
        "Độ dài tùy theo lượng nội dung thực tế, không cần gò ép ngắn — nhưng đừng lan man, "
        "lặp lại hoặc bịa thêm chi tiết không có trong chat.\n\n"
        "QUAN TRỌNG VỀ ĐỊNH DẠNG: tin nhắn sẽ hiển thị trên Telegram (Markdown bản cũ) — "
        "chỉ dùng MỘT dấu `*` để in đậm (vd `*chữ đậm*`), TUYỆT ĐỐI không dùng `**` (hai dấu "
        "sao) vì Telegram không hỗ trợ, sẽ hiện sai hoặc lỗi."
    )

    model_name = _resolve_gemini_model("gemini-2.0-flash")
    all_keys   = _get_all_ai_keys()
    sys_instruction = persona_sys_prompt + summary_instruction

    for key in all_keys:
        try:
            client   = genai.Client(api_key=key)
            config   = genai_types.GenerateContentConfig(system_instruction=sys_instruction)
            response = await asyncio.to_thread(
                client.models.generate_content, model=model_name, contents=prompt, config=config,
            )
            return response.text.strip()
        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in ["quota", "rate", "429", "exhausted", "resource_exhausted"]):
                continue
            for fallback in GEMINI_MODELS_FREE:
                if fallback == model_name:
                    continue
                try:
                    client2  = genai.Client(api_key=key)
                    config2  = genai_types.GenerateContentConfig(system_instruction=sys_instruction)
                    response = await asyncio.to_thread(
                        client2.models.generate_content, model=fallback, contents=prompt, config=config2,
                    )
                    return response.text.strip()
                except Exception:
                    continue
            break
    return ""


# ════════════════════════════════════════════════════════════════
# DM MONITOR — chuyển TẤT CẢ tin DM của bot này sang DM owner (mỗi bot dùng owner_id riêng)
#   • 📥 người dùng nhắn DM cho bot (kể cả lệnh /…, tin sửa, media) — bot có xử lý hay bỏ qua cũng chuyển
#   • 📤 bot trả lời người đó trong DM
#   Không chuyển DM của chính owner (tránh tự chép vào chính cuộc chat đó).
#   Tắt riêng DM: đặt CONFIG["monitor_dm"] = False. Vẫn cần /monitor đang bật.
# ════════════════════════════════════════════════════════════════
import logging as _logging
_dm_logger = _logging.getLogger(__name__)
_DM_BG: set = set()
_DM_MEDIA_ATTRS = ("photo", "video", "sticker", "voice", "audio", "animation",
                   "video_note", "document", "poll", "location", "contact", "dice", "venue")


async def push_dm_monitor(bot, msg, outgoing: bool = False, edited: bool = False) -> None:
    try:
        owner_id = CONFIG.get("owner_id", 0)
        chat = getattr(msg, "chat", None)
        if not owner_id or chat is None or chat.type != "private":
            return
        if chat.id == owner_id or is_owner(chat.id):
            return
        if not CONFIG.get("monitor_mode") or not CONFIG.get("monitor_dm", True):
            return
        sender = getattr(msg, "from_user", None)
        if not outgoing and bot_identity.is_self_user(sender):
            return                               # tin của CHÍNH bot này
        if outgoing and not CONFIG.get("monitor_dm_outgoing", False):
            return                               # mặc định KHÔNG chuyển lời bot tự nói
        name  = getattr(chat, "full_name", None) or chat.first_name or "?"
        uname = f" @{chat.username}" if chat.username else ""
        if bot_identity.is_other_bot(sender):
            uname += " 🤖 [BOT]"
        tag   = "📤 [DM · BOT TRẢ LỜI]" if outgoing else ("✏️ [DM · ĐÃ SỬA]" if edited else "📥 [DM]")
        header = f"{tag}\n👤 {name}{uname} · id {chat.id}\n🕐 {now_vn().strftime('%H:%M %d/%m')} · #{msg.message_id}"
        text = (msg.text or msg.caption or get_rich_text(msg) or "").strip()
        if any(getattr(msg, a, None) for a in _DM_MEDIA_ATTRS):
            await bot.send_message(owner_id, header)
            await bot.copy_message(owner_id, chat.id, msg.message_id)   # giữ nguyên media + caption
        else:
            await bot.send_message(owner_id, f"{header}\n\n{text[:3500]}" if text else header)
    except Exception as e:
        _dm_logger.warning("[dm-monitor] lỗi: %s", e)


def schedule_dm_monitor(bot, msg, **kw) -> None:
    """Chạy nền, không làm chậm luồng xử lý chính."""
    t = asyncio.ensure_future(push_dm_monitor(bot, msg, **kw))
    _DM_BG.add(t)
    t.add_done_callback(_DM_BG.discard)


async def dm_monitor_tap(update, context) -> None:
    """Handler group=-1: thấy mọi tin DM tới bot (cả lệnh), không chặn handler khác."""
    msg = update.message or update.edited_message
    if msg is None or msg.chat.type != "private":
        return
    schedule_dm_monitor(context.bot, msg, edited=update.edited_message is not None)
