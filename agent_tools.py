"""
agent_tools.py — Bộ "tool" (function calling) cho AI Gemini tự thực thi hành
động quản trị nhóm theo lệnh tự nhiên của owner.

Khác với handlers_basic.py (các hàm cmd_* nhận Update/context, gắn chặt với
cú pháp lệnh /ban, /mute...), các hàm ở đây nhận tham số THUẦN (bot, chat_id,
user_id, ...) để Gemini có thể gọi trực tiếp qua function calling — owner chỉ
cần nói tự nhiên ("mute Long 10 phút vì spam"), không cần nhớ cú pháp lệnh.

Nguyên tắc:
  • KHÔNG có hàm nào ở đây tự quyết định HÀNH ĐỘNG NÀO cần làm — đó là việc
    của Gemini (model tự chọn tool theo lệnh của owner). File này chỉ cung
    cấp khả năng THỰC THI an toàn, đúng, có log rõ ràng.
  • Mọi hàm trả về dict {"ok": bool, ...} bằng tiếng Việt thuần dữ liệu —
    KHÔNG có văn phong nhân vật ở tầng này. Văn phong/tính cách được áp dụng
    bởi system prompt persona khi Gemini tổng hợp câu trả lời cuối, không
    can thiệp vào tầng thực thi.
  • Toàn bộ tool chỉ được expose khi người gọi đã xác minh là OWNER (kiểm
    tra ở handlers_ai.py / bot.py trước khi gọi tới _gemini_chat_with_tools).
"""

from datetime import datetime, timedelta

from telegram import ChatPermissions
from telegram.error import TelegramError

from config import CONFIG, DB, save_db, now_vn
from utils import is_owner, get_chat_key

WARN_LIMIT = 3


# ════════════════════════════════════════════════════════════════
# TARGET RESOLUTION — tìm user_id từ tên/username/id do AI truyền vào
# ════════════════════════════════════════════════════════════════

async def resolve_user(bot, chat_id: int, who: str):
    """
    Tìm user trong nhóm `chat_id` theo `who` — có thể là:
      - "@username"
      - id số dạng chuỗi ("123456789")
      - tên hiển thị gần đúng (tra trong DB["users"] đã track được)
    Trả về dict {"id": int, "name": str, "username": str} hoặc None.
    """
    who = (who or "").strip()
    if not who:
        return None

    # 1. @username hoặc id số → hỏi trực tiếp Telegram
    candidate = who
    if candidate.lstrip("-").isdigit():
        try:
            member = await bot.get_chat_member(chat_id, int(candidate))
            u = member.user
            return {"id": u.id, "name": u.full_name, "username": u.username or ""}
        except TelegramError:
            pass
    if candidate.startswith("@"):
        try:
            member = await bot.get_chat_member(chat_id, candidate)
            u = member.user
            return {"id": u.id, "name": u.full_name, "username": u.username or ""}
        except TelegramError:
            pass

    # 2. Tên gần đúng → tra trong DB["users"] (đã track qua track_user())
    needle = who.lstrip("@").lower()
    best = None
    for uid_str, info in DB.get("users", {}).items():
        name = (info.get("name") or "").lower()
        uname = (info.get("username") or "").lower()
        if needle == uname or needle == name:
            best = (uid_str, info)
            break
        if needle in name or needle in uname:
            best = best or (uid_str, info)
    if best:
        uid_str, info = best
        try:
            member = await bot.get_chat_member(chat_id, int(uid_str))
            u = member.user
            return {"id": u.id, "name": u.full_name, "username": u.username or ""}
        except TelegramError:
            # User có thể đã rời nhóm — vẫn trả thông tin đã lưu để AI báo lại
            return {"id": int(uid_str), "name": info.get("name", who), "username": info.get("username", "")}

    return None


def _parse_duration_text(text: str) -> int:
    """'10m'/'2h'/'1d'/'30' (giây/phút mặc định phút nếu không có đơn vị
    và số nhỏ) → số giây. '0'/'' /'vĩnh viễn' → 0 (vĩnh viễn)."""
    text = (text or "").strip().lower()
    if not text or text in ("0", "vĩnh viễn", "vinh vien", "permanent", "forever"):
        return 0
    if text.isdigit():
        # Số trần không đơn vị → hiểu là PHÚT (đơn vị tự nhiên khi nói chuyện)
        return int(text) * 60
    if len(text) > 1 and text[:-1].isdigit():
        n, u = int(text[:-1]), text[-1]
        if u == "s": return n
        if u == "m": return n * 60
        if u == "h": return n * 3600
        if u == "d": return n * 86400
    return 0


# ════════════════════════════════════════════════════════════════
# TOOLS — mỗi hàm tương ứng 1 function-declaration cho Gemini
# ════════════════════════════════════════════════════════════════

async def tool_ban_user(bot, chat_id: int, who: str, reason: str = "") -> dict:
    target = await resolve_user(bot, chat_id, who)
    if not target:
        return {"ok": False, "error": f"Không tìm thấy người dùng '{who}' trong nhóm này."}
    if is_owner(target["id"]):
        return {"ok": False, "error": "Không thể ban chủ nhân (owner)."}
    try:
        await bot.ban_chat_member(chat_id, target["id"])
        return {"ok": True, "action": "ban", "user": target["name"], "reason": reason or "Không có lý do"}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_unban_user(bot, chat_id: int, who: str) -> dict:
    target = await resolve_user(bot, chat_id, who)
    if not target:
        return {"ok": False, "error": f"Không tìm thấy người dùng '{who}'."}
    try:
        await bot.unban_chat_member(chat_id, target["id"], only_if_banned=True)
        return {"ok": True, "action": "unban", "user": target["name"]}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_kick_user(bot, chat_id: int, who: str, reason: str = "") -> dict:
    target = await resolve_user(bot, chat_id, who)
    if not target:
        return {"ok": False, "error": f"Không tìm thấy người dùng '{who}' trong nhóm này."}
    if is_owner(target["id"]):
        return {"ok": False, "error": "Không thể kick chủ nhân (owner)."}
    try:
        await bot.ban_chat_member(chat_id, target["id"])
        await bot.unban_chat_member(chat_id, target["id"])
        return {"ok": True, "action": "kick", "user": target["name"], "reason": reason or "Không có lý do"}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_mute_user(bot, chat_id: int, who: str, duration: str = "0", reason: str = "") -> dict:
    target = await resolve_user(bot, chat_id, who)
    if not target:
        return {"ok": False, "error": f"Không tìm thấy người dùng '{who}' trong nhóm này."}
    if is_owner(target["id"]):
        return {"ok": False, "error": "Không thể mute chủ nhân (owner)."}
    duration_sec = _parse_duration_text(duration)
    until_date = datetime.now() + timedelta(seconds=duration_sec) if duration_sec > 0 else None
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id, user_id=target["id"],
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_date,
        )
        dur_label = (
            f"{duration_sec // 86400} ngày" if duration_sec >= 86400 else
            f"{duration_sec // 3600} giờ" if duration_sec >= 3600 else
            f"{duration_sec // 60} phút" if duration_sec >= 60 else
            "vĩnh viễn" if duration_sec == 0 else f"{duration_sec} giây"
        )
        return {"ok": True, "action": "mute", "user": target["name"], "duration": dur_label,
                "reason": reason or "Không có lý do"}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_unmute_user(bot, chat_id: int, who: str) -> dict:
    target = await resolve_user(bot, chat_id, who)
    if not target:
        return {"ok": False, "error": f"Không tìm thấy người dùng '{who}'."}
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id, user_id=target["id"],
            permissions=ChatPermissions(
                can_send_messages=True, can_send_polls=True,
                can_send_other_messages=True, can_add_web_page_previews=True,
            ),
        )
        return {"ok": True, "action": "unmute", "user": target["name"]}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_warn_user(bot, chat_id: int, who: str, reason: str = "") -> dict:
    target = await resolve_user(bot, chat_id, who)
    if not target:
        return {"ok": False, "error": f"Không tìm thấy người dùng '{who}' trong nhóm này."}
    if is_owner(target["id"]):
        return {"ok": False, "error": "Không thể cảnh cáo chủ nhân (owner)."}
    chat_key = get_chat_key(chat_id)
    DB["warns"].setdefault(chat_key, {})
    uid = str(target["id"])
    DB["warns"][chat_key][uid] = DB["warns"][chat_key].get(uid, 0) + 1
    current = DB["warns"][chat_key][uid]
    save_db(DB)
    if current >= WARN_LIMIT:
        try:
            await bot.ban_chat_member(chat_id, target["id"])
            DB["warns"][chat_key][uid] = 0
            save_db(DB)
            return {"ok": True, "action": "warn_then_ban", "user": target["name"],
                     "warns": current, "limit": WARN_LIMIT, "reason": reason or "Không có lý do"}
        except TelegramError as e:
            return {"ok": False, "error": f"Đã ghi cảnh cáo nhưng ban lỗi: {e}"}
    return {"ok": True, "action": "warn", "user": target["name"], "warns": current,
             "limit": WARN_LIMIT, "reason": reason or "Không có lý do"}


async def tool_unwarn_user(bot, chat_id: int, who: str) -> dict:
    target = await resolve_user(bot, chat_id, who)
    if not target:
        return {"ok": False, "error": f"Không tìm thấy người dùng '{who}'."}
    chat_key = get_chat_key(chat_id)
    uid = str(target["id"])
    current = DB.get("warns", {}).get(chat_key, {}).get(uid, 0)
    if current <= 0:
        return {"ok": True, "action": "unwarn", "user": target["name"], "warns": 0, "note": "Đã 0 từ trước"}
    DB["warns"][chat_key][uid] = current - 1
    save_db(DB)
    return {"ok": True, "action": "unwarn", "user": target["name"], "warns": current - 1}


async def tool_get_warns(bot, chat_id: int, who: str) -> dict:
    target = await resolve_user(bot, chat_id, who)
    if not target:
        return {"ok": False, "error": f"Không tìm thấy người dùng '{who}'."}
    chat_key = get_chat_key(chat_id)
    current = DB.get("warns", {}).get(chat_key, {}).get(str(target["id"]), 0)
    return {"ok": True, "user": target["name"], "warns": current, "limit": WARN_LIMIT}


async def tool_pin_last_message(bot, chat_id: int, message_id: int) -> dict:
    try:
        await bot.pin_chat_message(chat_id, message_id, disable_notification=True)
        return {"ok": True, "action": "pin", "message_id": message_id}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_unpin_message(bot, chat_id: int, message_id: int = 0) -> dict:
    try:
        if message_id:
            await bot.unpin_chat_message(chat_id, message_id)
        else:
            await bot.unpin_all_chat_messages(chat_id)
        return {"ok": True, "action": "unpin"}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_delete_message(bot, chat_id: int, message_id: int) -> dict:
    try:
        await bot.delete_message(chat_id, message_id)
        return {"ok": True, "action": "delete", "message_id": message_id}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_set_note(name: str, content: str) -> dict:
    """Tạo/ghi đè 1 note đơn giản (không media, không random %%%) — dùng khi
    owner nói 'lưu note #x là ...' qua chat tự nhiên."""
    name = name.strip().lstrip("#").lower()
    content = content.strip()
    if not name or not content:
        return {"ok": False, "error": "Thiếu tên note hoặc nội dung."}
    from utils import NOTES_FILTERS_KEY
    chat_key = NOTES_FILTERS_KEY
    DB["notes"].setdefault(chat_key, {})[name] = {
        "variants": [{"text": content, "media": []}], "media": [],
    }
    save_db(DB)
    return {"ok": True, "action": "set_note", "name": name}


async def tool_get_note(name: str) -> dict:
    name = name.strip().lstrip("#").lower()
    from utils import NOTES_FILTERS_KEY, normalize_note
    chat_key = NOTES_FILTERS_KEY
    raw = DB.get("notes", {}).get(chat_key, {}).get(name)
    if raw is None:
        return {"ok": False, "error": f"Không có note #{name}."}
    note = normalize_note(raw)
    texts = [v.get("text", "") for v in note["variants"]]
    return {"ok": True, "name": name, "content": " | ".join(texts)}


async def tool_get_group_info(bot, chat_id: int) -> dict:
    """Trả thông tin tổng quan nhóm — số thành viên, số note/filter, v.v.
    để AI có context khi owner hỏi chung."""
    try:
        chat = await bot.get_chat(chat_id)
        member_count = await bot.get_chat_member_count(chat_id)
        return {
            "ok": True,
            "title": chat.title or "",
            "member_count": member_count,
            "tracked_users": len(DB.get("users", {})),
        }
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


# ── Search (dùng lại đúng pipeline scrape thật của search_module.py —
#    KHÔNG để Gemini tự bịa link, mọi link trả về đều có thật trong pool) ──

async def tool_search_youtube(query: str, count: str = "1") -> dict:
    """Tìm video YouTube thật qua pipeline scrape có sẵn. Trả về pool video
    (title/url/channel/...) để Gemini tự chọn — _gemini_chat_with_tools sẽ
    hậu kiểm link trả lời cuối cùng khớp đúng URL trong pool này, không tin
    Gemini tự gõ lại link."""
    import search_module
    try:
        n = max(1, min(int(str(count).strip() or "1"), 10))
    except (ValueError, TypeError):
        n = 1
    pool_size = max(10, n * 3)
    results = await search_module._search_youtube(query, max_results=pool_size)
    if not results:
        return {"ok": False, "error": "Không tìm được video YouTube nào phù hợp với từ khoá này."}
    return {
        "ok": True,
        "action": "search_youtube",
        "videos": [
            {"title": v["title"], "url": v["url"], "channel": v.get("channel", ""),
             "duration": v.get("duration", ""), "views": v.get("views", "")}
            for v in results
        ],
        "note": "CHỈ được dùng link 'url' lấy NGUYÊN VẸN từ danh sách 'videos' này. "
                "TUYỆT ĐỐI không tự đoán/sửa video ID.",
    }


async def tool_search_web(query: str) -> dict:
    """Tìm kiếm web thật (Google) qua pipeline scrape có sẵn — dùng khi
    owner hỏi thông tin/tin tức cần dữ liệu mới mà không phải video."""
    import search_module
    try:
        results = await search_module._search_google(query, max_results=8)
    except AttributeError:
        return {"ok": False, "error": "Chức năng tìm web hiện không khả dụng."}
    if not results:
        return {"ok": False, "error": "Không tìm được kết quả phù hợp."}
    return {
        "ok": True,
        "action": "search_web",
        "results": results,
        "note": "Mọi link trong 'results' đều lấy NGUYÊN VẸN từ kết quả tìm kiếm thật, không tự sửa.",
    }


# ── Thông tin / cấu hình nhóm (tên, mô tả, ảnh, link mời, khoá nhóm) ──

async def tool_set_group_title(bot, chat_id: int, title: str) -> dict:
    title = (title or "").strip()
    if not title:
        return {"ok": False, "error": "Thiếu tên nhóm mới."}
    if len(title) > 128:
        return {"ok": False, "error": "Tên nhóm quá dài (Telegram giới hạn 128 ký tự)."}
    try:
        await bot.set_chat_title(chat_id, title)
        return {"ok": True, "action": "set_group_title", "title": title}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_set_group_description(bot, chat_id: int, description: str) -> dict:
    description = (description or "").strip()
    try:
        await bot.set_chat_description(chat_id, description)
        return {"ok": True, "action": "set_group_description", "description": description}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_delete_group_photo(bot, chat_id: int) -> dict:
    """Xoá ảnh đại diện nhóm hiện tại. (Đặt ảnh MỚI không khả thi qua tool
    text-only — AI không có cách lấy file ảnh thật để upload.)"""
    try:
        await bot.delete_chat_photo(chat_id)
        return {"ok": True, "action": "delete_group_photo"}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_get_invite_link(bot, chat_id: int) -> dict:
    try:
        chat = await bot.get_chat(chat_id)
        link = chat.invite_link
        if not link:
            link = await bot.export_chat_invite_link(chat_id)
        return {"ok": True, "action": "get_invite_link", "invite_link": link}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_revoke_invite_link(bot, chat_id: int) -> dict:
    """Thu hồi link mời hiện tại và TỰ TẠO link mới (Telegram bắt buộc phải
    có link thay thế khi revoke — không thể chỉ revoke mà không có link mới)."""
    try:
        chat = await bot.get_chat(chat_id)
        old_link = chat.invite_link
        if old_link:
            await bot.revoke_chat_invite_link(chat_id, old_link)
        new_link = await bot.export_chat_invite_link(chat_id)
        return {"ok": True, "action": "revoke_invite_link", "new_invite_link": new_link}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_lock_group(bot, chat_id: int) -> dict:
    """Khoá nhóm — chỉ admin được gửi tin (chống raid/giữ trật tự tạm thời)."""
    try:
        await bot.set_chat_permissions(
            chat_id,
            ChatPermissions(can_send_messages=False, can_send_polls=False,
                             can_send_other_messages=False, can_add_web_page_previews=False,
                             can_change_info=False, can_invite_users=False, can_pin_messages=False),
        )
        return {"ok": True, "action": "lock_group"}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_unlock_group(bot, chat_id: int) -> dict:
    try:
        await bot.set_chat_permissions(
            chat_id,
            ChatPermissions(can_send_messages=True, can_send_polls=True,
                             can_send_other_messages=True, can_add_web_page_previews=True,
                             can_change_info=False, can_invite_users=True, can_pin_messages=False),
        )
        return {"ok": True, "action": "unlock_group"}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


# ── Quản lý Topic (chỉ áp dụng cho supergroup đã bật chế độ Forum) ───

async def tool_create_topic(bot, chat_id: int, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "Thiếu tên topic."}
    try:
        topic = await bot.create_forum_topic(chat_id, name=name)
        return {"ok": True, "action": "create_topic", "name": name,
                 "topic_id": topic.message_thread_id}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_rename_topic(bot, chat_id: int, topic_id: int, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "Thiếu tên topic mới."}
    try:
        await bot.edit_forum_topic(chat_id, message_thread_id=topic_id, name=name)
        return {"ok": True, "action": "rename_topic", "topic_id": topic_id, "name": name}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_close_topic(bot, chat_id: int, topic_id: int) -> dict:
    try:
        await bot.close_forum_topic(chat_id, message_thread_id=topic_id)
        return {"ok": True, "action": "close_topic", "topic_id": topic_id}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_reopen_topic(bot, chat_id: int, topic_id: int) -> dict:
    try:
        await bot.reopen_forum_topic(chat_id, message_thread_id=topic_id)
        return {"ok": True, "action": "reopen_topic", "topic_id": topic_id}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_delete_topic(bot, chat_id: int, topic_id: int) -> dict:
    try:
        await bot.delete_forum_topic(chat_id, message_thread_id=topic_id)
        return {"ok": True, "action": "delete_topic", "topic_id": topic_id}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


# ── Promote / Demote / Custom title ─────────────────────────────────

async def tool_promote_user(bot, chat_id: int, who: str, level: str = "full") -> dict:
    """Promote thành viên lên admin. level='full' (đầy đủ quyền) hoặc 'basic' (quyền cơ bản)."""
    target = await resolve_user(bot, chat_id, who)
    if not target:
        return {"ok": False, "error": f"Không tìm thấy người dùng '{who}'."}
    try:
        if level == "basic":
            await bot.promote_chat_member(
                chat_id=chat_id, user_id=target["id"], is_anonymous=False,
                can_manage_chat=True, can_post_messages=True, can_edit_messages=True,
                can_delete_messages=True, can_manage_video_chats=True,
                can_restrict_members=False, can_promote_members=False,
                can_change_info=False, can_invite_users=True,
                can_pin_messages=True, can_manage_topics=False, can_manage_tags=False,
            )
        else:
            await bot.promote_chat_member(
                chat_id=chat_id, user_id=target["id"], is_anonymous=False,
                can_manage_chat=True, can_post_messages=True, can_edit_messages=True,
                can_delete_messages=True, can_manage_video_chats=True,
                can_restrict_members=True, can_promote_members=True,
                can_change_info=True, can_invite_users=True,
                can_pin_messages=True, can_manage_topics=True, can_manage_tags=True,
                can_delete_stories=True, can_edit_stories=True, can_post_stories=True,
            )
        return {"ok": True, "action": "promote", "user": target["name"], "level": level}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_demote_user(bot, chat_id: int, who: str) -> dict:
    """Hạ cấp admin về thành viên thường (xóa toàn bộ quyền admin)."""
    target = await resolve_user(bot, chat_id, who)
    if not target:
        return {"ok": False, "error": f"Không tìm thấy người dùng '{who}'."}
    if is_owner(target["id"]):
        return {"ok": False, "error": "Không thể hạ cấp chủ nhân (owner)."}
    try:
        await bot.promote_chat_member(
            chat_id=chat_id, user_id=target["id"], is_anonymous=False,
            can_manage_chat=False, can_post_messages=False, can_edit_messages=False,
            can_delete_messages=False, can_manage_video_chats=False,
            can_restrict_members=False, can_promote_members=False,
            can_change_info=False, can_invite_users=False,
            can_pin_messages=False, can_manage_topics=False, can_manage_tags=False,
            can_delete_stories=False, can_edit_stories=False, can_post_stories=False,
        )
        return {"ok": True, "action": "demote", "user": target["name"]}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_set_admin_title(bot, chat_id: int, who: str, title: str) -> dict:
    """Đặt chức danh hiển thị dưới tên trong nhóm (tối đa 16 ký tự).

    Telegram có 2 method khác nhau tùy trạng thái người dùng:
      • Admin  → set_chat_administrator_custom_title (custom_title, chỉ áp
        dụng cho người ĐANG là admin do bot promote).
      • Member thường → set_chat_member_tag (method mới của Bot API, áp
        dụng cho thành viên thường, cần bot có quyền can_manage_tags).

    Hàm này tự thử custom_title trước (đúng cho đa số trường hợp gọi tới
    là sửa chức danh admin); nếu Telegram báo lỗi vì người đó không phải
    admin, tự fallback sang set_chat_member_tag cho member thường — owner
    không cần biết người đó đang là admin hay member để ra lệnh."""
    target = await resolve_user(bot, chat_id, who)
    if not target:
        return {"ok": False, "error": f"Không tìm thấy người dùng '{who}'."}
    title = (title or "").strip()[:16]
    if not title:
        return {"ok": False, "error": "Thiếu tên chức vụ."}
    try:
        await bot.set_chat_administrator_custom_title(chat_id, target["id"], custom_title=title)
        return {"ok": True, "action": "set_admin_title", "user": target["name"], "title": title}
    except TelegramError as e:
        err = str(e).lower()
        if "not an administrator" not in err and "chat_admin_required" not in err:
            return {"ok": False, "error": str(e)}
        # Không phải admin → fallback method mới dành cho member thường.
        try:
            await bot.set_chat_member_tag(chat_id, target["id"], tag=title)
            return {"ok": True, "action": "set_member_tag", "user": target["name"], "title": title}
        except TelegramError as e2:
            return {"ok": False, "error": str(e2)}
        except AttributeError:
            return {
                "ok": False,
                "error": (
                    f"'{target['name']}' không phải admin nên không thể đặt chức danh admin "
                    "kiểu cũ; bot cần hỗ trợ method set_chat_member_tag (Bot API mới) để đặt "
                    "thẻ cho thành viên thường, nhưng thư viện hiện tại chưa có method này."
                ),
            }


# ── Set admin permissions — đổi từng quyền admin tùy ý ────────────

async def tool_set_admin_permissions(bot, chat_id: int, who: str,
                                      can_manage_chat: str = "",
                                      can_delete_messages: str = "",
                                      can_manage_video_chats: str = "",
                                      can_restrict_members: str = "",
                                      can_promote_members: str = "",
                                      can_change_info: str = "",
                                      can_invite_users: str = "",
                                      can_pin_messages: str = "",
                                      can_manage_tags: str = "",
                                      can_manage_topics: str = "",
                                      can_post_messages: str = "",
                                      can_edit_messages: str = "",
                                      can_post_stories: str = "",
                                      can_edit_stories: str = "",
                                      can_delete_stories: str = "",
                                      is_anonymous: str = "") -> dict:
    """Đặt quyền admin tùy chỉnh chi tiết cho 1 thành viên (bao gồm 'Sửa thẻ thành viên' = can_manage_tags).
    Chỉ truyền vào các quyền muốn thay đổi — quyền nào không truyền sẽ giữ nguyên theo Telegram.
    Hoạt động cả khi người đó chưa là admin (sẽ tự promote lên với đúng các quyền đó).
    """
    target = await resolve_user(bot, chat_id, who)
    if not target:
        return {"ok": False, "error": f"Không tìm thấy người dùng '{who}'."}
    if is_owner(target["id"]):
        return {"ok": False, "error": "Không thể thay đổi quyền chủ nhân (owner)."}

    def to_bool_or_none(v: str):
        if v == "":
            return None
        return str(v).strip().lower() not in ("false", "0", "no", "off")

    kwargs = {"chat_id": chat_id, "user_id": target["id"]}

    # Chỉ set param nào được owner truyền vào (không None)
    mapping = {
        "can_manage_chat":       to_bool_or_none(can_manage_chat),
        "can_delete_messages":   to_bool_or_none(can_delete_messages),
        "can_manage_video_chats":to_bool_or_none(can_manage_video_chats),
        "can_restrict_members":  to_bool_or_none(can_restrict_members),
        "can_promote_members":   to_bool_or_none(can_promote_members),
        "can_change_info":       to_bool_or_none(can_change_info),
        "can_invite_users":      to_bool_or_none(can_invite_users),
        "can_pin_messages":      to_bool_or_none(can_pin_messages),
        "can_manage_tags":       to_bool_or_none(can_manage_tags),
        "can_manage_topics":     to_bool_or_none(can_manage_topics),
        "can_post_messages":     to_bool_or_none(can_post_messages),
        "can_edit_messages":     to_bool_or_none(can_edit_messages),
        "can_post_stories":      to_bool_or_none(can_post_stories),
        "can_edit_stories":      to_bool_or_none(can_edit_stories),
        "can_delete_stories":    to_bool_or_none(can_delete_stories),
    }
    if to_bool_or_none(is_anonymous) is not None:
        kwargs["is_anonymous"] = to_bool_or_none(is_anonymous)

    for k, v in mapping.items():
        if v is not None:
            kwargs[k] = v

    if len(kwargs) <= 2:  # chỉ có chat_id + user_id, không có quyền nào
        return {"ok": False, "error": "Cần truyền vào ít nhất 1 quyền muốn thay đổi."}

    try:
        await bot.promote_chat_member(**kwargs)
        changed = {k: v for k, v in mapping.items() if v is not None}
        return {
            "ok": True,
            "action": "set_admin_permissions",
            "user": target["name"],
            "changed": changed,
        }
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


# ── Purge messages ─────────────────────────────────────────────────

async def tool_purge_messages(bot, chat_id: int, from_message_id: str, to_message_id: str) -> dict:
    """Xóa hàng loạt tin nhắn từ from_message_id đến to_message_id (tối đa 200 tin/lần).
    Dùng khi owner muốn dọn dẹp một đoạn chat liên tiếp. Cần biết cả 2 message_id."""
    try:
        start_id = int(str(from_message_id).strip())
        end_id   = int(str(to_message_id).strip())
    except (ValueError, TypeError):
        return {"ok": False, "error": "from_message_id và to_message_id phải là số nguyên."}
    if start_id > end_id:
        start_id, end_id = end_id, start_id
    total = end_id - start_id + 1
    if total > 200:
        return {"ok": False, "error": f"Phạm vi quá lớn ({total} tin). Giới hạn an toàn là 200 tin/lần."}
    import asyncio
    deleted = fail = 0
    for msg_id in range(start_id, end_id + 1):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            deleted += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    return {
        "ok": True, "action": "purge",
        "deleted": deleted, "failed": fail,
        "range": f"#{start_id} → #{end_id}",
    }


# ── Rules ─────────────────────────────────────────────────────────

async def tool_set_rules(bot, chat_id: int, rules_text: str) -> dict:
    """Đặt/cập nhật nội quy nhóm. Nội quy này sẽ hiển thị khi ai dùng /rules."""
    rules_text = (rules_text or "").strip()
    if not rules_text:
        return {"ok": False, "error": "Thiếu nội dung nội quy."}
    chat_key = get_chat_key(chat_id)
    DB.setdefault("rules", {})[chat_key] = rules_text
    save_db(DB)
    return {"ok": True, "action": "set_rules", "preview": rules_text[:100]}


async def tool_get_rules(bot, chat_id: int) -> dict:
    """Lấy nội quy nhóm hiện tại."""
    chat_key = get_chat_key(chat_id)
    rules = DB.get("rules", {}).get(chat_key)
    if not rules:
        return {"ok": True, "rules": None, "note": "Nhóm chưa có nội quy."}
    return {"ok": True, "rules": rules}


# ── Broadcast ─────────────────────────────────────────────────────

async def tool_broadcast(bot, chat_id: int, message: str) -> dict:
    """Gửi thông báo tới TẤT CẢ người dùng đã từng tương tác với bot (từ DB users).
    Dùng khi owner muốn gửi thông báo quan trọng tới mọi người."""
    import asyncio
    message = (message or "").strip()
    if not message:
        return {"ok": False, "error": "Thiếu nội dung thông báo."}
    ok = fail = 0
    for uid_str in DB.get("users", {}):
        try:
            await bot.send_message(int(uid_str), f"📢 *THÔNG BÁO:*\n{message}", parse_mode="Markdown")
            ok += 1
            await asyncio.sleep(0.1)
        except Exception:
            fail += 1
    return {"ok": True, "action": "broadcast", "sent": ok, "failed": fail, "total": ok + fail}


# ── User info & admin list ─────────────────────────────────────────

async def tool_get_user_info(bot, chat_id: int, who: str) -> dict:
    """Lấy thông tin chi tiết một thành viên: ID, tên, username, trạng thái trong nhóm, số cảnh cáo."""
    target = await resolve_user(bot, chat_id, who)
    if not target:
        return {"ok": False, "error": f"Không tìm thấy người dùng '{who}'."}
    try:
        member = await bot.get_chat_member(chat_id, target["id"])
        chat_key = get_chat_key(chat_id)
        warns = DB.get("warns", {}).get(chat_key, {}).get(str(target["id"]), 0)
        return {
            "ok": True,
            "id": target["id"],
            "name": target["name"],
            "username": target.get("username", ""),
            "status": member.status,
            "is_owner": is_owner(target["id"]),
            "warns": warns,
            "warn_limit": WARN_LIMIT,
        }
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


async def tool_get_admins(bot, chat_id: int) -> dict:
    """Lấy danh sách admin hiện tại trong nhóm (tên, username, quyền hạn)."""
    try:
        admins = await bot.get_chat_administrators(chat_id)
        result = []
        for a in admins:
            result.append({
                "id": a.user.id,
                "name": a.user.full_name,
                "username": a.user.username or "",
                "is_bot": a.user.is_bot,
                "status": a.status,
                "custom_title": getattr(a, "custom_title", "") or "",
            })
        return {"ok": True, "admins": result, "count": len(result)}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


# ── Block / Unblock user (chặn DM tới bot) ────────────────────────

async def tool_block_user(bot, chat_id: int, who: str) -> dict:
    """Chặn một user_id khỏi việc gửi tin nhắn DM tới bot (thêm vào danh sách blocked)."""
    target = await resolve_user(bot, chat_id, who)
    if not target:
        return {"ok": False, "error": f"Không tìm thấy người dùng '{who}'."}
    blocked = DB.setdefault("blocked", [])
    if target["id"] not in blocked:
        blocked.append(target["id"])
        save_db(DB)
    return {"ok": True, "action": "block_user", "user": target["name"], "user_id": target["id"]}


async def tool_unblock_user(bot, chat_id: int, who: str) -> dict:
    """Bỏ chặn một user_id (gỡ khỏi danh sách blocked)."""
    target = await resolve_user(bot, chat_id, who)
    if not target:
        return {"ok": False, "error": f"Không tìm thấy người dùng '{who}'."}
    blocked = DB.get("blocked", [])
    if target["id"] in blocked:
        blocked.remove(target["id"])
        save_db(DB)
        return {"ok": True, "action": "unblock_user", "user": target["name"]}
    return {"ok": True, "action": "unblock_user", "user": target["name"], "note": "User này chưa bị chặn."}


# ── Monitor & feed controls ────────────────────────────────────────

async def tool_toggle_monitor(bot, chat_id: int, enable: str = "") -> dict:
    """Bật/tắt chế độ giám sát nhóm real-time (đẩy tin nhắn nhóm vào DM owner).
    enable='on' để bật, 'off' để tắt, để trống để toggle."""
    from config import save_config
    enable = (enable or "").strip().lower()
    if enable == "on":
        CONFIG["monitor_mode"] = True
    elif enable == "off":
        CONFIG["monitor_mode"] = False
    else:
        CONFIG["monitor_mode"] = not CONFIG.get("monitor_mode", False)
    save_config(CONFIG)
    return {"ok": True, "action": "toggle_monitor", "monitor_mode": CONFIG["monitor_mode"]}


async def tool_get_bot_stats(bot, chat_id: int) -> dict:
    """Lấy thống kê tổng quan của bot: số tin đã xử lý, số user, trạng thái các chế độ..."""
    from utils import MSG_BUFFER
    st = CONFIG.get("stats", {})
    try:
        member_count = await bot.get_chat_member_count(chat_id)
    except Exception:
        member_count = 0
    return {
        "ok": True,
        "total_messages": st.get("total_messages", 0),
        "ai_messages": st.get("ai_messages", 0),
        "tracked_users": len(DB.get("users", {})),
        "member_count": member_count,
        "buffer_size": len(MSG_BUFFER),
        "monitor_mode": CONFIG.get("monitor_mode", False),
        "proxy_mode": CONFIG.get("proxy_mode", False),
        "ai_mode": CONFIG.get("ai_mode", False),
        "fn_open": CONFIG.get("fn_open", False),
        "started_at": st.get("started_at", ""),
    }


# ── Antiflood & welcome config ─────────────────────────────────────

async def tool_set_welcome(bot, chat_id: int, text: str = "", enable: str = "") -> dict:
    """Bật/tắt tin chào thành viên mới và/hoặc đặt nội dung tin chào.
    enable='on'/'off' để bật/tắt. text để trống thì dùng mẫu mặc định."""
    from config import save_config
    enable = (enable or "").strip().lower()
    if enable == "on":
        CONFIG["welcome_mode"] = True
    elif enable == "off":
        CONFIG["welcome_mode"] = False
    text = (text or "").strip()
    if text:
        CONFIG["welcome_text"] = text
    save_config(CONFIG)
    return {
        "ok": True, "action": "set_welcome",
        "welcome_mode": CONFIG.get("welcome_mode", False),
        "welcome_text": CONFIG.get("welcome_text", "") or "(dùng mẫu mặc định)",
    }


async def tool_set_antiflood(bot, chat_id: int, enable: str = "", limit: str = "", window: str = "") -> dict:
    """Cấu hình antiflood (tự mute khi spam). enable='on'/'off', limit=số tin tối đa, window=số giây."""
    from config import save_config
    enable = (enable or "").strip().lower()
    if enable == "on":
        CONFIG["antiflood_mode"] = True
    elif enable == "off":
        CONFIG["antiflood_mode"] = False
    try:
        if limit:
            CONFIG["antiflood_limit"] = max(2, int(limit))
    except ValueError:
        pass
    try:
        if window:
            CONFIG["antiflood_window"] = max(3, int(window))
    except ValueError:
        pass
    save_config(CONFIG)
    return {
        "ok": True, "action": "set_antiflood",
        "antiflood_mode": CONFIG.get("antiflood_mode", False),
        "limit": CONFIG.get("antiflood_limit", 6),
        "window_seconds": CONFIG.get("antiflood_window", 10),
    }


# ── Report mode ────────────────────────────────────────────────────

async def tool_set_report_mode(bot, chat_id: int, enable: str) -> dict:
    """Bật/tắt chức năng /report cho phép thành viên báo cáo vi phạm tới owner.
    enable='on' để bật, 'off' để tắt."""
    from config import save_config
    enable = (enable or "").strip().lower()
    if enable not in ("on", "off"):
        return {"ok": False, "error": "Giá trị enable phải là 'on' hoặc 'off'."}
    CONFIG["report_mode"] = (enable == "on")
    save_config(CONFIG)
    return {"ok": True, "action": "set_report_mode", "report_mode": CONFIG["report_mode"]}


# ── Filter/Note open mode ──────────────────────────────────────────

async def tool_set_fn_open(bot, chat_id: int, enable: str) -> dict:
    """Bật/tắt chế độ cho phép mọi thành viên trong nhóm trigger filter/note
    (không chỉ riêng owner). enable='on'/'off'."""
    from config import save_config
    enable = (enable or "").strip().lower()
    if enable not in ("on", "off"):
        return {"ok": False, "error": "Giá trị enable phải là 'on' hoặc 'off'."}
    CONFIG["fn_open"] = (enable == "on")
    save_config(CONFIG)
    return {"ok": True, "action": "set_fn_open", "fn_open": CONFIG["fn_open"]}


# ── Restrict user — đổi quyền thành viên chi tiết ─────────────────

async def tool_restrict_user(bot, chat_id: int, who: str,
                              can_send_messages: str = "true",
                              can_send_media: str = "true",
                              can_send_polls: str = "true",
                              can_send_other_messages: str = "true",
                              can_add_web_page_previews: str = "true",
                              can_change_info: str = "false",
                              can_invite_users: str = "true",
                              can_pin_messages: str = "false") -> dict:
    """Đặt quyền chi tiết cho 1 thành viên (restrict). Mỗi quyền là 'true'/'false'.
    Dùng khi owner muốn hạn chế riêng từng quyền mà không mute hoàn toàn.
    Ví dụ: cấm gửi media nhưng vẫn cho nhắn text."""
    target = await resolve_user(bot, chat_id, who)
    if not target:
        return {"ok": False, "error": f"Không tìm thấy người dùng '{who}'."}
    if is_owner(target["id"]):
        return {"ok": False, "error": "Không thể restrict chủ nhân (owner)."}

    def to_bool(v: str) -> bool:
        return str(v).strip().lower() not in ("false", "0", "no", "off")

    try:
        perms = ChatPermissions(
            can_send_messages=to_bool(can_send_messages),
            can_send_polls=to_bool(can_send_polls),
            can_send_other_messages=to_bool(can_send_other_messages),
            can_add_web_page_previews=to_bool(can_add_web_page_previews),
            can_change_info=to_bool(can_change_info),
            can_invite_users=to_bool(can_invite_users),
            can_pin_messages=to_bool(can_pin_messages),
            # PTB 22.x / Bot API 6.x — quyền media chi tiết
            can_send_audios=to_bool(can_send_media),
            can_send_documents=to_bool(can_send_media),
            can_send_photos=to_bool(can_send_media),
            can_send_videos=to_bool(can_send_media),
            can_send_video_notes=to_bool(can_send_media),
            can_send_voice_notes=to_bool(can_send_media),
        )
        await bot.restrict_chat_member(chat_id=chat_id, user_id=target["id"], permissions=perms)
        summary = {
            "gửi_tin": to_bool(can_send_messages),
            "gửi_media": to_bool(can_send_media),
            "gửi_poll": to_bool(can_send_polls),
            "mời_thành_viên": to_bool(can_invite_users),
            "ghim_tin": to_bool(can_pin_messages),
        }
        return {"ok": True, "action": "restrict_user", "user": target["name"], "permissions": summary}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


# ── Note management (list & delete) ────────────────────────────────

async def tool_list_notes(bot, chat_id: int) -> dict:
    """Xem danh sách tất cả ghi chú (note) đang được lưu."""
    from utils import NOTES_FILTERS_KEY
    chat_key = NOTES_FILTERS_KEY
    note_list = DB.get("notes", {}).get(chat_key, {})
    if not note_list:
        return {"ok": True, "notes": [], "count": 0}
    result = []
    for name, v in note_list.items():
        from utils import normalize_note
        data = normalize_note(v)
        result.append({
            "name": name,
            "variants": len(data["variants"]),
            "has_media": bool(data.get("media") or any(x.get("media") for x in data["variants"])),
        })
    return {"ok": True, "notes": result, "count": len(result)}


async def tool_delete_note(bot, chat_id: int, name: str) -> dict:
    """Xóa hẳn 1 ghi chú theo tên."""
    from utils import NOTES_FILTERS_KEY
    name = (name or "").strip().lstrip("#").lower()
    if not name:
        return {"ok": False, "error": "Thiếu tên note."}
    chat_key = NOTES_FILTERS_KEY
    if chat_key in DB.get("notes", {}) and name in DB["notes"][chat_key]:
        DB["notes"][chat_key].pop(name)
        save_db(DB)
        return {"ok": True, "action": "delete_note", "name": name}
    return {"ok": False, "error": f"Không tìm thấy note #{name}."}


# ── Filter management (list & delete) ──────────────────────────────

async def tool_list_filters(bot, chat_id: int) -> dict:
    """Xem danh sách tất cả filter (bộ lọc tự động) đang hoạt động."""
    from utils import NOTES_FILTERS_KEY, normalize_variants
    chat_key = NOTES_FILTERS_KEY
    flist = DB.get("filters", {}).get(chat_key, {})
    if not flist:
        return {"ok": True, "filters": [], "count": 0}
    result = []
    for key, v in flist.items():
        if isinstance(v, dict) and "triggers" in v:
            triggers = [t["content"] for t in v.get("triggers", [])]
            variants = normalize_variants(v.get("variants"))
            result.append({
                "key": key,
                "triggers": triggers,
                "variants": len(variants),
                "access": v.get("access", "all"),
                "has_media": bool(v.get("media") or any(x.get("media") for x in variants)),
            })
        else:
            result.append({"key": key, "triggers": [key], "variants": 1, "access": "all", "has_media": False})
    return {"ok": True, "filters": result, "count": len(result)}


async def tool_delete_filter(bot, chat_id: int, keyword: str) -> dict:
    """Xóa 1 filter theo từ khóa trigger."""
    from utils import NOTES_FILTERS_KEY
    keyword = (keyword or "").strip().lower()
    if not keyword:
        return {"ok": False, "error": "Thiếu từ khóa filter cần xóa."}
    chat_key = NOTES_FILTERS_KEY
    flist = DB.get("filters", {}).get(chat_key, {})
    found_key = None
    for k, v in flist.items():
        if isinstance(v, dict) and "triggers" in v:
            if any(t["content"] == keyword for t in v["triggers"]):
                found_key = k
                break
        elif k == keyword:
            found_key = k
            break
    if found_key:
        flist.pop(found_key)
        save_db(DB)
        return {"ok": True, "action": "delete_filter", "keyword": keyword}
    return {"ok": False, "error": f"Không tìm thấy filter '{keyword}'."}


# ── Bot config: setgroup / buffer limit / list users ───────────────

async def tool_set_target_group(bot, chat_id: int, group_id: str) -> dict:
    """Đặt nhóm đích cho bot hoạt động (relay, monitor, tomtat...). group_id là ID số của nhóm."""
    from config import save_config
    try:
        gid = int(str(group_id).strip())
    except (ValueError, TypeError):
        return {"ok": False, "error": "group_id phải là số nguyên (vd: -1001234567890)."}
    CONFIG["target_group_id"] = gid
    save_config(CONFIG)
    return {"ok": True, "action": "set_target_group", "target_group_id": gid}


async def tool_set_buffer_limit(bot, chat_id: int, limit: str) -> dict:
    """Đặt số lượng tin nhắn tối đa lưu trong msg_buffer mỗi nhóm.
    Tối thiểu 50, tối đa 200000. Ảnh hưởng tới /tomtat."""
    from config import save_config
    try:
        n = int(str(limit).strip())
    except (ValueError, TypeError):
        return {"ok": False, "error": "limit phải là số nguyên."}
    if n < 50:
        return {"ok": False, "error": "Tối thiểu 50 tin nhắn."}
    if n > 200000:
        return {"ok": False, "error": "Tối đa 200000 tin nhắn."}
    old = CONFIG.get("msg_buffer_limit", 5000)
    CONFIG["msg_buffer_limit"] = n
    save_config(CONFIG)
    return {"ok": True, "action": "set_buffer_limit", "old": old, "new": n}


async def tool_list_users(bot, chat_id: int) -> dict:
    """Xem danh sách user đã từng tương tác với bot (tối đa 50 user đầu)."""
    users = DB.get("users", {})
    if not users:
        return {"ok": True, "users": [], "count": 0}
    result = []
    for uid, info in list(users.items())[:50]:
        result.append({
            "id": uid,
            "name": info.get("name", "?"),
            "username": info.get("username", ""),
        })
    return {"ok": True, "users": result, "count": len(users), "shown": len(result)}


# ── AI history clear (per-chat) ────────────────────────────────────

async def tool_clear_ai_history(bot, chat_id: int) -> dict:
    """Xóa lịch sử hội thoại AI cho chat hiện tại (giúp AI quên ngữ cảnh cũ, bắt đầu mới)."""
    from ai_module import AI_HISTORY
    count = 0
    if chat_id in AI_HISTORY:
        count = len(AI_HISTORY[chat_id]) // 2
        del AI_HISTORY[chat_id]
    try:
        import db_postgres
        if db_postgres.is_ready():
            db_postgres.delete_history(chat_id)
    except Exception:
        pass
    return {"ok": True, "action": "clear_ai_history", "turns_cleared": count}


# ── Slowmode ────────────────────────────────────────────────────────

async def tool_set_slowmode(bot, chat_id: int, seconds: str = "0") -> dict:
    """Đặt chế độ slowmode cho nhóm — mỗi thành viên phải chờ N giây giữa các tin.
    seconds='0' để tắt slowmode. Hỗ trợ: 0, 10, 30, 60, 300, 900, 3600 giây."""
    try:
        secs = int(str(seconds).strip())
    except (ValueError, TypeError):
        return {"ok": False, "error": "seconds phải là số nguyên (giây)."}
    VALID = {0, 10, 30, 60, 300, 900, 3600}
    if secs not in VALID:
        return {"ok": False, "error": f"Telegram chỉ cho phép các giá trị: {sorted(VALID)} giây."}
    try:
        await bot.set_chat_slow_mode_delay(chat_id, secs)
        label = f"{secs} giây" if secs > 0 else "tắt"
        return {"ok": True, "action": "set_slowmode", "seconds": secs, "label": label}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


# ── Delete all messages from a specific user (purge by user) ───────

async def tool_purge_user_messages(bot, chat_id: int, who: str) -> dict:
    """Xóa TOÀN BỘ tin nhắn của 1 thành viên trong nhóm — bao gồm cả tin
    cũ từ trước khi bot theo dõi. Gọi Bot API endpoint
    deleteChatMessages trực tiếp (cần bot có quyền xóa tin nhắn).
    Dùng khi owner muốn 'xóa hết tin của X', 'dọn sạch tin của X',
    'xóa toàn bộ mess của X'."""
    target = await resolve_user(bot, chat_id, who)
    if not target:
        return {"ok": False, "error": f"Không tìm thấy người dùng '{who}'."}
    try:
        # Thử method PTB nếu có (PTB >= 22.x mới nhất)
        if hasattr(bot, "delete_all_chat_messages_for_user"):
            await bot.delete_all_chat_messages_for_user(chat_id, target["id"])
        else:
            # Fallback: gọi Bot API raw qua httpx
            import httpx
            url = f"https://api.telegram.org/bot{bot.token}/deleteChatMessagesForUser"
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json={
                    "chat_id": chat_id,
                    "user_id": target["id"],
                })
            data = resp.json()
            if not data.get("ok"):
                return {"ok": False, "error": data.get("description", "Bot API lỗi")}

        # Dọn luôn buffer nội bộ cho sạch
        try:
            from utils import MSG_BUFFER
            keep = [
                e for e in MSG_BUFFER
                if not (e.get("user_id") == target["id"] and e.get("chat_id") == chat_id)
            ]
            MSG_BUFFER.clear()
            MSG_BUFFER.extend(keep)
        except Exception:
            pass

        return {
            "ok": True,
            "action": "purge_user_messages",
            "user": target["name"],
            "note": "Đã xóa toàn bộ tin nhắn (kể cả tin cũ trước khi bot theo dõi).",
        }
    except TelegramError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Timed restrict — tắt quyền cơ bản trong N giây/phút/giờ ──────────

async def tool_timed_restrict(bot, chat_id: int, who: str,
                               duration: str = "1h",
                               can_send_messages: str = "false",
                               can_send_media: str = "false",
                               can_send_polls: str = "false",
                               can_send_other_messages: str = "false",
                               can_invite_users: str = "true",
                               can_pin_messages: str = "false") -> dict:
    """Hạn chế quyền thành viên trong khoảng thời gian — tự động khôi phục sau.
    Khác mute (chỉ tắt nhắn tin), tool này cho phép chọn riêng từng quyền + có hẹn giờ.
    Ví dụ: 'cấm gửi media của X trong 2 giờ', 'tắt quyền mời người của Y trong 1 ngày'."""
    target = await resolve_user(bot, chat_id, who)
    if not target:
        return {"ok": False, "error": f"Không tìm thấy người dùng '{who}'."}
    if is_owner(target["id"]):
        return {"ok": False, "error": "Không thể hạn chế quyền chủ nhân (owner)."}

    duration_sec = _parse_duration_text(duration)
    until_date = datetime.now() + timedelta(seconds=duration_sec) if duration_sec > 0 else None

    def to_bool(v: str) -> bool:
        return str(v).strip().lower() not in ("false", "0", "no", "off")

    dur_label = (
        f"{duration_sec // 86400} ngày" if duration_sec >= 86400 else
        f"{duration_sec // 3600} giờ" if duration_sec >= 3600 else
        f"{duration_sec // 60} phút" if duration_sec >= 60 else
        "vĩnh viễn" if duration_sec == 0 else f"{duration_sec} giây"
    )

    try:
        perms = ChatPermissions(
            can_send_messages=to_bool(can_send_messages),
            can_send_polls=to_bool(can_send_polls),
            can_send_other_messages=to_bool(can_send_other_messages),
            can_add_web_page_previews=to_bool(can_send_media),
            can_invite_users=to_bool(can_invite_users),
            can_pin_messages=to_bool(can_pin_messages),
            can_send_audios=to_bool(can_send_media),
            can_send_documents=to_bool(can_send_media),
            can_send_photos=to_bool(can_send_media),
            can_send_videos=to_bool(can_send_media),
            can_send_video_notes=to_bool(can_send_media),
            can_send_voice_notes=to_bool(can_send_media),
        )
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target["id"],
            permissions=perms,
            until_date=until_date,
        )
        applied = {
            "gửi_tin": to_bool(can_send_messages),
            "gửi_media": to_bool(can_send_media),
            "gửi_poll": to_bool(can_send_polls),
            "mời_thành_viên": to_bool(can_invite_users),
            "ghim_tin": to_bool(can_pin_messages),
        }
        return {
            "ok": True,
            "action": "timed_restrict",
            "user": target["name"],
            "duration": dur_label,
            "permissions": applied,
            "auto_restore": until_date is not None,
        }
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


# ── Revoke all invite links (tạo link hoàn toàn mới, thu hồi mọi link cũ) ─

async def tool_revoke_all_links(bot, chat_id: int) -> dict:
    """Thu hồi link mời hiện tại và cấp link mới. Dùng khi nhóm bị leak link hoặc
    owner muốn đảm bảo không ai dùng link cũ vào được nữa."""
    try:
        new_link = await bot.export_chat_invite_link(chat_id)
        return {"ok": True, "action": "revoke_all_links", "new_invite_link": new_link,
                "note": "Link mời cũ đã bị vô hiệu hóa. Chỉ link mới này hoạt động."}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


# ── Set group permissions (đổi quyền mặc định của TẤT CẢ thành viên) ─────

async def tool_set_group_permissions(bot, chat_id: int,
                                      can_send_messages: str = "",
                                      can_send_media: str = "",
                                      can_send_polls: str = "",
                                      can_send_other_messages: str = "",
                                      can_add_web_page_previews: str = "",
                                      can_change_info: str = "",
                                      can_invite_users: str = "",
                                      can_pin_messages: str = "") -> dict:
    """Đổi quyền mặc định cho TẤT CẢ thành viên trong nhóm (không phải 1 người).
    Khác lock_group (tắt hết), tool này cho phép chọn riêng từng quyền muốn thay đổi.
    Ví dụ: 'tắt quyền mời người', 'cấm gửi poll', 'không cho thay đổi info nhóm'.
    Chỉ truyền quyền muốn thay đổi, quyền không truyền giữ nguyên hiện tại."""

    def to_bool_or_none(v: str):
        if v == "":
            return None
        return str(v).strip().lower() not in ("false", "0", "no", "off")

    # Lấy quyền hiện tại của nhóm để merge
    try:
        chat = await bot.get_chat(chat_id)
        current = chat.permissions or ChatPermissions()
    except TelegramError as e:
        return {"ok": False, "error": f"Không lấy được quyền nhóm hiện tại: {e}"}

    def pick(new_val, current_val):
        return new_val if new_val is not None else (current_val or False)

    msg_val    = to_bool_or_none(can_send_messages)
    media_val  = to_bool_or_none(can_send_media)
    polls_val  = to_bool_or_none(can_send_polls)
    other_val  = to_bool_or_none(can_send_other_messages)
    preview_val= to_bool_or_none(can_add_web_page_previews)
    info_val   = to_bool_or_none(can_change_info)
    invite_val = to_bool_or_none(can_invite_users)
    pin_val    = to_bool_or_none(can_pin_messages)

    if all(v is None for v in [msg_val, media_val, polls_val, other_val, preview_val, info_val, invite_val, pin_val]):
        return {"ok": False, "error": "Cần truyền ít nhất 1 quyền muốn thay đổi."}

    try:
        new_perms = ChatPermissions(
            can_send_messages=pick(msg_val, current.can_send_messages),
            can_send_polls=pick(polls_val, current.can_send_polls),
            can_send_other_messages=pick(other_val, current.can_send_other_messages),
            can_add_web_page_previews=pick(preview_val, current.can_add_web_page_previews),
            can_change_info=pick(info_val, current.can_change_info),
            can_invite_users=pick(invite_val, current.can_invite_users),
            can_pin_messages=pick(pin_val, current.can_pin_messages),
            can_send_audios=pick(media_val, current.can_send_audios),
            can_send_documents=pick(media_val, current.can_send_documents),
            can_send_photos=pick(media_val, current.can_send_photos),
            can_send_videos=pick(media_val, current.can_send_videos),
            can_send_video_notes=pick(media_val, current.can_send_video_notes),
            can_send_voice_notes=pick(media_val, current.can_send_voice_notes),
        )
        await bot.set_chat_permissions(chat_id, new_perms)
        changed = {}
        if msg_val   is not None: changed["gửi_tin"]        = msg_val
        if media_val is not None: changed["gửi_media"]      = media_val
        if polls_val is not None: changed["gửi_poll"]       = polls_val
        if info_val  is not None: changed["thay_info_nhóm"] = info_val
        if invite_val is not None: changed["mời_thành_viên"]= invite_val
        if pin_val   is not None: changed["ghim_tin"]       = pin_val
        return {"ok": True, "action": "set_group_permissions", "changed": changed}
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


# ── Get current group permissions ─────────────────────────────────────────

async def tool_get_group_permissions(bot, chat_id: int) -> dict:
    """Lấy quyền mặc định hiện tại của tất cả thành viên trong nhóm.
    Dùng khi owner hỏi 'nhóm đang mở/đóng quyền gì', 'thành viên được làm gì'."""
    try:
        chat = await bot.get_chat(chat_id)
        p = chat.permissions
        if not p:
            return {"ok": True, "permissions": None, "note": "Không lấy được quyền nhóm."}
        return {
            "ok": True,
            "permissions": {
                "gửi_tin": p.can_send_messages,
                "gửi_media": p.can_send_photos or p.can_send_videos or p.can_send_documents,
                "gửi_poll": p.can_send_polls,
                "gửi_sticker_gif": p.can_send_other_messages,
                "preview_link": p.can_add_web_page_previews,
                "thay_info_nhóm": p.can_change_info,
                "mời_thành_viên": p.can_invite_users,
                "ghim_tin": p.can_pin_messages,
            }
        }
    except TelegramError as e:
        return {"ok": False, "error": str(e)}


# ── Delete messages by count (xóa N tin gần nhất) ────────────────────────

async def tool_delete_last_n_messages(bot, chat_id: int, count: str = "10",
                                       from_message_id: str = "") -> dict:
    """Xóa N tin nhắn gần nhất trong nhóm (dựa theo buffer đã track).
    Dùng khi owner nói 'xóa 20 tin gần nhất', 'dọn 10 tin cuối'.
    Nếu truyền from_message_id, xóa N tin trước tin đó."""
    import asyncio
    try:
        n = max(1, min(int(str(count).strip()), 500))
    except (ValueError, TypeError):
        return {"ok": False, "error": "count phải là số nguyên (1–500)."}

    from utils import MSG_BUFFER
    # MSG_BUFFER là deque PHẲNG — lọc theo chat_id, key là msg_id
    buffer = [e for e in MSG_BUFFER if e.get("chat_id") == chat_id]

    if from_message_id:
        try:
            ref_id = int(str(from_message_id).strip())
            buffer = [e for e in buffer if e.get("msg_id", 0) <= ref_id]
        except (ValueError, TypeError):
            pass

    # Lấy N tin gần nhất (cuối buffer)
    to_delete = [e["msg_id"] for e in buffer[-n:] if e.get("msg_id")]
    if not to_delete:
        return {"ok": True, "action": "delete_last_n", "deleted": 0,
                "note": "Không có tin nhắn nào trong buffer để xóa."}

    deleted = fail = 0
    for msg_id in reversed(to_delete):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            deleted += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1

    return {
        "ok": True,
        "action": "delete_last_n_messages",
        "requested": n,
        "deleted": deleted,
        "failed": fail,
    }


# ── Antilink — tự động xóa link ngoài ────────────────────────────────────

async def tool_set_antilink(bot, chat_id: int, enable: str,
                             whitelist: str = "") -> dict:
    """Bật/tắt chế độ tự động xóa link ngoài (t.me, http...) trong nhóm.
    Khi bật, bot sẽ xóa bất kỳ tin nhắn nào chứa link không thuộc whitelist.
    whitelist: danh sách domain cho phép, cách nhau bằng dấu phẩy (ví dụ: 'youtube.com,google.com').
    enable='on' để bật, 'off' để tắt."""
    from config import save_config
    enable = (enable or "").strip().lower()
    if enable not in ("on", "off"):
        return {"ok": False, "error": "Giá trị enable phải là 'on' hoặc 'off'."}
    CONFIG["antilink_mode"] = (enable == "on")
    if whitelist:
        domains = [d.strip().lower() for d in whitelist.split(",") if d.strip()]
        CONFIG["antilink_whitelist"] = domains
    save_config(CONFIG)
    return {
        "ok": True,
        "action": "set_antilink",
        "antilink_mode": CONFIG["antilink_mode"],
        "whitelist": CONFIG.get("antilink_whitelist", []),
    }


# ── Set join approval (phê duyệt thành viên mới) ─────────────────────────

async def tool_set_join_approval(bot, chat_id: int, enable: str) -> dict:
    """Bật/tắt chế độ yêu cầu phê duyệt khi có người xin vào nhóm.
    Khi bật, mỗi yêu cầu join sẽ được forward tới owner DM để duyệt thủ công.
    enable='on' để bật, 'off' để tắt."""
    from config import save_config
    enable = (enable or "").strip().lower()
    if enable not in ("on", "off"):
        return {"ok": False, "error": "Giá trị enable phải là 'on' hoặc 'off'."}

    enabled = (enable == "on")
    CONFIG["join_approval_mode"] = enabled
    save_config(CONFIG)

    # Thử dùng PTB để bật/tắt join_by_request nếu Bot API hỗ trợ
    note = ""
    try:
        if enabled:
            # Tắt quyền tự mời để buộc dùng link có approval
            await bot.set_chat_permissions(
                chat_id,
                __import__("telegram").ChatPermissions(can_invite_users=False),
            )
        else:
            # Khôi phục quyền mời
            await bot.set_chat_permissions(
                chat_id,
                __import__("telegram").ChatPermissions(can_invite_users=True),
            )
    except TelegramError as e:
        note = f"Lưu ý: không đổi được quyền mời tự động ({e})"

    return {
        "ok": True,
        "action": "set_join_approval",
        "enabled": enabled,
        "note": note or (
            "Bật: mọi yêu cầu join sẽ được forward tới DM owner để duyệt thủ công." if enabled
            else "Tắt: thành viên mới vào thẳng không cần duyệt."
        ),
    }


# ════════════════════════════════════════════════════════════════
# TOOL REGISTRY — map tên tool (Gemini gọi theo tên) → hàm thực thi
# ════════════════════════════════════════════════════════════════

# Các tool cần `bot` + `chat_id` (luôn được tiêm tự động trước khi gọi)
TOOLS_NEEDING_CHAT = {
    "ban_user": tool_ban_user,
    "unban_user": tool_unban_user,
    "kick_user": tool_kick_user,
    "mute_user": tool_mute_user,
    "unmute_user": tool_unmute_user,
    "warn_user": tool_warn_user,
    "unwarn_user": tool_unwarn_user,
    "get_warns": tool_get_warns,
    "pin_last_message": tool_pin_last_message,
    "unpin_message": tool_unpin_message,
    "delete_message": tool_delete_message,
    "get_group_info": tool_get_group_info,
    "set_group_title": tool_set_group_title,
    "set_group_description": tool_set_group_description,
    "delete_group_photo": tool_delete_group_photo,
    "get_invite_link": tool_get_invite_link,
    "revoke_invite_link": tool_revoke_invite_link,
    "lock_group": tool_lock_group,
    "unlock_group": tool_unlock_group,
    "create_topic": tool_create_topic,
    "rename_topic": tool_rename_topic,
    "close_topic": tool_close_topic,
    "reopen_topic": tool_reopen_topic,
    "delete_topic": tool_delete_topic,
    # ── Promote / Demote / Title ──
    "promote_user": tool_promote_user,
    "demote_user": tool_demote_user,
    "set_admin_title": tool_set_admin_title,
    "set_admin_permissions": tool_set_admin_permissions,
    # ── Purge ──
    "purge_messages": tool_purge_messages,
    # ── Rules ──
    "set_rules": tool_set_rules,
    "get_rules": tool_get_rules,
    # ── Broadcast ──
    "broadcast": tool_broadcast,
    # ── User info / Admins ──
    "get_user_info": tool_get_user_info,
    "get_admins": tool_get_admins,
    # ── Block / Unblock ──
    "block_user": tool_block_user,
    "unblock_user": tool_unblock_user,
    # ── Monitor / Stats ──
    "toggle_monitor": tool_toggle_monitor,
    "get_bot_stats": tool_get_bot_stats,
    # ── Config nhóm ──
    "set_welcome": tool_set_welcome,
    "set_antiflood": tool_set_antiflood,
    "set_report_mode": tool_set_report_mode,
    "set_fn_open": tool_set_fn_open,
    # ── Restrict (đổi quyền thành viên chi tiết) ──
    "restrict_user": tool_restrict_user,
    # ── Note management ──
    "list_notes": tool_list_notes,
    "delete_note": tool_delete_note,
    # ── Filter management ──
    "list_filters": tool_list_filters,
    "delete_filter": tool_delete_filter,
    # ── Bot config ──
    "set_target_group": tool_set_target_group,
    "set_buffer_limit": tool_set_buffer_limit,
    "list_users": tool_list_users,
    # ── AI history ──
    "clear_ai_history": tool_clear_ai_history,
    # ── Slowmode ──
    "set_slowmode": tool_set_slowmode,
    # ── Purge by user ──
    "purge_user_messages": tool_purge_user_messages,
    # ── Timed restrict ──
    "timed_restrict": tool_timed_restrict,
    # ── Revoke all links ──
    "revoke_all_links": tool_revoke_all_links,
    # ── Group permissions (all members) ──
    "set_group_permissions": tool_set_group_permissions,
    "get_group_permissions": tool_get_group_permissions,
    # ── Delete last N messages ──
    "delete_last_n_messages": tool_delete_last_n_messages,
    # ── Antilink ──
    "set_antilink": tool_set_antilink,
    # ── Join approval ──
    "set_join_approval": tool_set_join_approval,
}

# Tool không cần chat_id (note là global, không theo nhóm; search cũng vậy)
TOOLS_GLOBAL = {
    "set_note": tool_set_note,
    "get_note": tool_get_note,
    "search_youtube": tool_search_youtube,
    "search_web": tool_search_web,
}

ALL_TOOL_NAMES = set(TOOLS_NEEDING_CHAT) | set(TOOLS_GLOBAL)


async def execute_tool(name: str, args: dict, bot=None, chat_id: int = None) -> dict:
    """Dispatcher chung — gọi đúng hàm tool theo tên, tiêm bot/chat_id nếu cần."""
    if name in TOOLS_NEEDING_CHAT:
        if bot is None or chat_id is None:
            return {"ok": False, "error": "Thiếu context nhóm (chat_id) để thực hiện hành động này."}
        return await TOOLS_NEEDING_CHAT[name](bot, chat_id, **args)
    if name in TOOLS_GLOBAL:
        return await TOOLS_GLOBAL[name](**args)
    return {"ok": False, "error": f"Tool không tồn tại: {name}"}


# ════════════════════════════════════════════════════════════════
# GEMINI FUNCTION-DECLARATION SCHEMA
# ════════════════════════════════════════════════════════════════

def build_gemini_tools(genai_types):
    """Trả về list[genai_types.Tool] khai báo toàn bộ function cho Gemini.
    `genai_types` được truyền vào để tránh import vòng (ai_module đã import
    sẵn `google.genai.types`)."""
    FunctionDeclaration = genai_types.FunctionDeclaration
    Schema = genai_types.Schema
    Type = genai_types.Type

    def s(desc, **props):
        required = [k for k, v in props.items() if v.get("required")]
        properties = {
            k: Schema(type=Type.STRING, description=v.get("desc", ""))
            for k, v in props.items()
        }
        return Schema(type=Type.OBJECT, description=desc, properties=properties, required=required)

    decls = [
        FunctionDeclaration(
            name="ban_user",
            description="Cấm vĩnh viễn 1 thành viên khỏi nhóm (ban). Dùng khi owner yêu cầu ban/cấm/đuổi vĩnh viễn ai đó.",
            parameters=s("ban user",
                who={"desc": "Tên, @username, hoặc id của người cần ban", "required": True},
                reason={"desc": "Lý do ban (nếu owner có nói)"}),
        ),
        FunctionDeclaration(
            name="unban_user",
            description="Bỏ ban cho 1 thành viên đã bị ban trước đó.",
            parameters=s("unban user", who={"desc": "Tên, @username, hoặc id", "required": True}),
        ),
        FunctionDeclaration(
            name="kick_user",
            description="Đuổi 1 thành viên khỏi nhóm nhưng KHÔNG ban vĩnh viễn (họ có thể vào lại). Dùng khi owner nói 'kick'/'đuổi' (không nói cấm vĩnh viễn).",
            parameters=s("kick user",
                who={"desc": "Tên, @username, hoặc id", "required": True},
                reason={"desc": "Lý do kick"}),
        ),
        FunctionDeclaration(
            name="mute_user",
            description="Tắt quyền gửi tin nhắn (mute) của 1 thành viên trong khoảng thời gian, hoặc vĩnh viễn nếu không nói thời gian.",
            parameters=s("mute user",
                who={"desc": "Tên, @username, hoặc id", "required": True},
                duration={"desc": "Thời gian mute, ví dụ '10m', '2h', '1d'. Để '0' nếu vĩnh viễn hoặc owner không nói rõ thời gian."},
                reason={"desc": "Lý do mute"}),
        ),
        FunctionDeclaration(
            name="unmute_user",
            description="Mở lại quyền gửi tin nhắn cho 1 thành viên đang bị mute.",
            parameters=s("unmute user", who={"desc": "Tên, @username, hoặc id", "required": True}),
        ),
        FunctionDeclaration(
            name="warn_user",
            description=f"Ghi 1 lượt cảnh cáo cho thành viên. Đủ {WARN_LIMIT} lượt sẽ tự động ban.",
            parameters=s("warn user",
                who={"desc": "Tên, @username, hoặc id", "required": True},
                reason={"desc": "Lý do cảnh cáo"}),
        ),
        FunctionDeclaration(
            name="unwarn_user",
            description="Xoá bớt 1 lượt cảnh cáo của thành viên.",
            parameters=s("unwarn user", who={"desc": "Tên, @username, hoặc id", "required": True}),
        ),
        FunctionDeclaration(
            name="get_warns",
            description="Xem số lượt cảnh cáo hiện tại của 1 thành viên.",
            parameters=s("get warns", who={"desc": "Tên, @username, hoặc id", "required": True}),
        ),
        FunctionDeclaration(
            name="pin_last_message",
            description="Ghim 1 tin nhắn trong nhóm theo message_id. Chỉ dùng khi đã biết rõ message_id (ví dụ owner reply vào tin cần ghim).",
            parameters=s("pin message", message_id={"desc": "ID tin nhắn cần ghim (số nguyên dạng chuỗi)", "required": True}),
        ),
        FunctionDeclaration(
            name="unpin_message",
            description="Bỏ ghim 1 tin nhắn cụ thể (hoặc tất cả nếu không có message_id).",
            parameters=s("unpin message", message_id={"desc": "ID tin nhắn cần bỏ ghim, để trống nếu muốn bỏ ghim tất cả"}),
        ),
        FunctionDeclaration(
            name="delete_message",
            description="Xoá 1 tin nhắn cụ thể trong nhóm theo message_id.",
            parameters=s("delete message", message_id={"desc": "ID tin nhắn cần xoá", "required": True}),
        ),
        FunctionDeclaration(
            name="set_note",
            description="Lưu/ghi đè 1 ghi chú văn bản đơn giản (note) với tên gợi nhớ, để gọi lại sau bằng #tên.",
            parameters=s("set note",
                name={"desc": "Tên note (không cần dấu #)", "required": True},
                content={"desc": "Nội dung note", "required": True}),
        ),
        FunctionDeclaration(
            name="get_note",
            description="Lấy nội dung 1 note đã lưu theo tên.",
            parameters=s("get note", name={"desc": "Tên note (không cần dấu #)", "required": True}),
        ),
        FunctionDeclaration(
            name="get_group_info",
            description="Lấy thông tin tổng quan của nhóm hiện tại (tên nhóm, số thành viên...).",
            parameters=s("get group info"),
        ),
        FunctionDeclaration(
            name="set_group_title",
            description="Đổi tên nhóm hiện tại. Dùng khi owner yêu cầu đổi tên/đặt tên nhóm.",
            parameters=s("set group title", title={"desc": "Tên nhóm mới", "required": True}),
        ),
        FunctionDeclaration(
            name="set_group_description",
            description="Đổi mô tả (description) của nhóm hiện tại.",
            parameters=s("set group description", description={"desc": "Mô tả mới của nhóm", "required": True}),
        ),
        FunctionDeclaration(
            name="delete_group_photo",
            description="Xoá ảnh đại diện hiện tại của nhóm (không thể tự upload ảnh mới qua tool này).",
            parameters=s("delete group photo"),
        ),
        FunctionDeclaration(
            name="get_invite_link",
            description="Lấy link mời (invite link) hiện tại của nhóm.",
            parameters=s("get invite link"),
        ),
        FunctionDeclaration(
            name="revoke_invite_link",
            description="Thu hồi link mời hiện tại của nhóm và tạo link mời mới (link cũ sẽ không còn dùng được).",
            parameters=s("revoke invite link"),
        ),
        FunctionDeclaration(
            name="lock_group",
            description="Khoá nhóm — chỉ admin được gửi tin nhắn. Dùng khi owner muốn tạm khoá/chống raid/giữ trật tự.",
            parameters=s("lock group"),
        ),
        FunctionDeclaration(
            name="unlock_group",
            description="Mở khoá nhóm — cho phép thành viên gửi tin nhắn lại bình thường.",
            parameters=s("unlock group"),
        ),
        FunctionDeclaration(
            name="create_topic",
            description="Tạo 1 topic mới trong nhóm (chỉ áp dụng nếu nhóm đã bật chế độ Forum/Topics).",
            parameters=s("create topic", name={"desc": "Tên topic mới", "required": True}),
        ),
        FunctionDeclaration(
            name="rename_topic",
            description="Đổi tên 1 topic đã có theo topic_id.",
            parameters=s("rename topic",
                topic_id={"desc": "ID của topic cần đổi tên (số nguyên dạng chuỗi)", "required": True},
                name={"desc": "Tên mới cho topic", "required": True}),
        ),
        FunctionDeclaration(
            name="close_topic",
            description="Đóng (khoá) 1 topic theo topic_id — không cho gửi tin mới vào topic đó nữa.",
            parameters=s("close topic", topic_id={"desc": "ID của topic cần đóng", "required": True}),
        ),
        FunctionDeclaration(
            name="reopen_topic",
            description="Mở lại 1 topic đã bị đóng theo topic_id.",
            parameters=s("reopen topic", topic_id={"desc": "ID của topic cần mở lại", "required": True}),
        ),
        FunctionDeclaration(
            name="delete_topic",
            description="Xoá hẳn 1 topic theo topic_id (không thể khôi phục).",
            parameters=s("delete topic", topic_id={"desc": "ID của topic cần xoá", "required": True}),
        ),
        FunctionDeclaration(
            name="search_youtube",
            description="Tìm video THẬT trên YouTube theo từ khoá. LUÔN dùng tool này khi owner muốn xem/nghe/tìm video, bài hát, MV, clip — KHÔNG tự đoán/bịa link YouTube từ trí nhớ.",
            parameters=s("search youtube",
                query={"desc": "Từ khoá tìm kiếm video", "required": True},
                count={"desc": "Số lượng video muốn lấy (mặc định '1' nếu owner không nói rõ số lượng)"}),
        ),
        FunctionDeclaration(
            name="search_web",
            description="Tìm kiếm thông tin/tin tức THẬT trên web theo từ khoá. Dùng khi owner hỏi thông tin cần dữ liệu mới, cập nhật, hoặc ngoài kiến thức đã biết.",
            parameters=s("search web", query={"desc": "Từ khoá cần tìm kiếm", "required": True}),
        ),
        # ── Promote / Demote / Title ──────────────────────────────────────
        FunctionDeclaration(
            name="promote_user",
            description="Thăng cấp thành viên lên admin. Dùng khi owner muốn 'thêm admin', 'promote', 'cho làm admin'. "
                        "level='full' cho toàn bộ quyền admin, level='basic' cho quyền cơ bản (không promote người khác).",
            parameters=s("promote user",
                who={"desc": "Tên, @username, hoặc id", "required": True},
                level={"desc": "Mức quyền: 'full' (đầy đủ, mặc định) hoặc 'basic' (cơ bản)"}),
        ),
        FunctionDeclaration(
            name="demote_user",
            description="Hạ cấp admin về thành viên thường (xóa toàn bộ quyền admin). Dùng khi owner muốn 'xóa admin', 'hạ cấp', 'deladm'.",
            parameters=s("demote user", who={"desc": "Tên, @username, hoặc id", "required": True}),
        ),
        FunctionDeclaration(
            name="set_admin_title",
            description="Đặt chức danh tùy chỉnh hiển thị dưới tên trong nhóm (tối đa 16 ký tự). "
                        "Hoạt động cho CẢ admin (dùng custom title) VÀ thành viên thường (dùng "
                        "tag member — tính năng mới của Telegram). Owner không cần biết người đó "
                        "đang là admin hay member thường, tool tự xử lý đúng cách.",
            parameters=s("set admin title",
                who={"desc": "Tên, @username, hoặc id", "required": True},
                title={"desc": "Chức danh muốn đặt (tối đa 16 ký tự)", "required": True}),
        ),
        FunctionDeclaration(
            name="set_admin_permissions",
            description=(
                "Đặt quyền admin chi tiết cho 1 người — bật/tắt từng quyền riêng lẻ, "
                "bao gồm CẢ 'Sửa thẻ thành viên' (can_manage_tags). "
                "Dùng khi owner muốn:\n"
                "- 'bật/tắt sửa thẻ thành viên cho X' → can_manage_tags=true/false\n"
                "- 'cho X xóa tin' → can_delete_messages=true\n"
                "- 'cấm X ban người' → can_restrict_members=false\n"
                "- 'cho X thêm admin' → can_promote_members=true\n"
                "- 'bật/tắt quản lý livestream cho X' → can_manage_video_chats=true/false\n"
                "- 'cho X ghim tin' → can_pin_messages=true\n"
                "- 'bật/tắt mời người bằng link cho X' → can_invite_users=true/false\n"
                "- 'chỉnh quyền admin của X', 'đổi thẻ admin của X'\n"
                "- 'không cho X quyền nào cả'/'tắt hết quyền của X' → set TẤT CẢ tham số can_* "
                "bên dưới = false (và is_anonymous=false) trong CÙNG 1 lần gọi.\n"
                "- 'chỉ cho X quyền ghim tin' / 'X chỉ được xóa tin và mời link, còn lại không' "
                "→ ĐÂY LÀ YÊU CẦU LOẠI TRỪ: set các quyền được nêu = true, và BẮT BUỘC set "
                "TẤT CẢ các quyền KHÔNG được nêu = false trong CÙNG 1 lần gọi (không bỏ trống), "
                "vì bỏ trống nghĩa là 'giữ nguyên quyền cũ', không phải 'không cho'.\n"
                "Owner có thể yêu cầu theo đúng danh sách quyền hiển thị trong app Telegram "
                "('Bot này có thể làm gì?'): Đổi thông tin nhóm=can_change_info, Xóa tin nhắn="
                "can_delete_messages, Cấm người dùng=can_restrict_members, Mời người bằng link="
                "can_invite_users, Ghim tin nhắn=can_pin_messages, Sửa thẻ thành viên="
                "can_manage_tags, Quản lý tin (đăng/sửa/xóa)=can_post_messages+can_edit_messages"
                "+can_delete_messages, Quản lý livestream=can_manage_video_chats, Thêm admin mới="
                "can_promote_members, Gửi ẩn danh=is_anonymous, Quản lý chủ đề=can_manage_topics.\n"
                "Chỉ khi owner KHÔNG nói rõ 'chỉ'/'duy nhất'/'không cho ... còn lại' — tức là "
                "chỉ muốn ĐIỀU CHỈNH THÊM một vài quyền mà không đụng tới quyền khác — mới bỏ "
                "trống các tham số không được nhắc tới để giữ nguyên. "
                "Hoạt động cả khi người đó chưa là admin (tự promote với đúng quyền đó)."
            ),
            parameters=s("set admin permissions",
                who={"desc": "Tên, @username, hoặc id người cần chỉnh quyền", "required": True},
                can_manage_tags={
                    "desc": "'true'/'false' — Sửa thẻ thành viên (hiện/ẩn tag người dùng trong nhóm)"},
                can_manage_chat={
                    "desc": "'true'/'false' — Quản lý nhóm (quyền cơ bản nhất của admin)"},
                can_delete_messages={
                    "desc": "'true'/'false' — Quản lý tin nhắn (xóa tin của người khác)"},
                can_manage_video_chats={
                    "desc": "'true'/'false' — Quản lý livestream/video chat"},
                can_restrict_members={
                    "desc": "'true'/'false' — Cấm người dùng (ban/mute/restrict thành viên)"},
                can_promote_members={
                    "desc": "'true'/'false' — Thêm admin mới (promote người khác lên admin)"},
                can_change_info={
                    "desc": "'true'/'false' — Đổi thông tin nhóm (tên, ảnh, mô tả)"},
                can_invite_users={
                    "desc": "'true'/'false' — Mời người bằng link"},
                can_pin_messages={
                    "desc": "'true'/'false' — Ghim tin nhắn"},
                can_manage_topics={
                    "desc": "'true'/'false' — Quản lý topic (chỉ dùng cho nhóm Forum)"},
                can_post_messages={
                    "desc": "'true'/'false' — Đăng tin (dùng cho channel/broadcast group)"},
                can_edit_messages={
                    "desc": "'true'/'false' — Sửa tin của người khác (channel)"},
                can_post_stories={
                    "desc": "'true'/'false' — Đăng story"},
                can_edit_stories={
                    "desc": "'true'/'false' — Sửa story"},
                can_delete_stories={
                    "desc": "'true'/'false' — Xóa story"},
                is_anonymous={
                    "desc": "'true'/'false' — Ẩn danh (tin nhắn hiện tên nhóm thay tên admin)"},
            ),
        ),
        # ── Purge ─────────────────────────────────────────────────────────
        FunctionDeclaration(
            name="purge_messages",
            description="Xóa hàng loạt tin nhắn liên tiếp theo khoảng message_id (tối đa 200 tin/lần). "
                        "Dùng khi owner muốn dọn một đoạn chat, cần biết từ message_id nào đến message_id nào.",
            parameters=s("purge messages",
                from_message_id={"desc": "ID tin nhắn đầu tiên trong đoạn cần xóa", "required": True},
                to_message_id={"desc": "ID tin nhắn cuối trong đoạn cần xóa", "required": True}),
        ),
        # ── Rules ─────────────────────────────────────────────────────────
        FunctionDeclaration(
            name="set_rules",
            description="Đặt hoặc cập nhật nội quy nhóm. Dùng khi owner muốn 'đặt nội quy', 'setrules', 'viết rules'.",
            parameters=s("set rules", rules_text={"desc": "Nội dung nội quy nhóm", "required": True}),
        ),
        FunctionDeclaration(
            name="get_rules",
            description="Xem nội quy nhóm hiện tại.",
            parameters=s("get rules"),
        ),
        # ── Broadcast ─────────────────────────────────────────────────────
        FunctionDeclaration(
            name="broadcast",
            description="Gửi thông báo tới TẤT CẢ người dùng đã từng tương tác với bot. "
                        "Dùng khi owner muốn thông báo chung cho mọi người. "
                        "⚠️ Chỉ dùng khi owner yêu cầu rõ ràng — thao tác này ảnh hưởng tới nhiều người.",
            parameters=s("broadcast", message={"desc": "Nội dung thông báo cần gửi đến tất cả người dùng", "required": True}),
        ),
        # ── User info / Admins ────────────────────────────────────────────
        FunctionDeclaration(
            name="get_user_info",
            description="Lấy thông tin chi tiết một thành viên: ID, tên, username, trạng thái trong nhóm, số cảnh cáo. "
                        "Dùng khi owner hỏi 'thông tin của X', 'X là ai', 'X có bao nhiêu warn'.",
            parameters=s("get user info", who={"desc": "Tên, @username, hoặc id cần xem thông tin", "required": True}),
        ),
        FunctionDeclaration(
            name="get_admins",
            description="Lấy danh sách admin hiện tại trong nhóm kèm thông tin tên, username, chức vụ.",
            parameters=s("get admins"),
        ),
        # ── Block / Unblock ───────────────────────────────────────────────
        FunctionDeclaration(
            name="block_user",
            description="Chặn user không cho gửi tin DM tới bot (thêm vào blacklist bot). "
                        "Khác với ban_user (đuổi khỏi nhóm) — lệnh này chỉ chặn DM với bot.",
            parameters=s("block user", who={"desc": "Tên, @username, hoặc id", "required": True}),
        ),
        FunctionDeclaration(
            name="unblock_user",
            description="Bỏ chặn user khỏi blacklist bot (cho phép DM lại).",
            parameters=s("unblock user", who={"desc": "Tên, @username, hoặc id", "required": True}),
        ),
        # ── Monitor / Stats ───────────────────────────────────────────────
        FunctionDeclaration(
            name="toggle_monitor",
            description="Bật hoặc tắt chế độ giám sát nhóm real-time (đẩy tin nhắn nhóm vào DM owner). "
                        "enable='on' để bật, 'off' để tắt, để trống để đảo trạng thái.",
            parameters=s("toggle monitor", enable={"desc": "'on' để bật, 'off' để tắt, để trống để đảo (toggle)"}),
        ),
        FunctionDeclaration(
            name="get_bot_stats",
            description="Lấy thống kê tổng quan của bot: số tin nhắn đã xử lý, số user, trạng thái các chế độ... "
                        "Dùng khi owner hỏi 'bot đang làm gì', 'thống kê', 'stats'.",
            parameters=s("get bot stats"),
        ),
        # ── Config nhóm ───────────────────────────────────────────────────
        FunctionDeclaration(
            name="set_welcome",
            description="Bật/tắt tin chào thành viên mới và/hoặc đặt nội dung tin chào tùy chỉnh. "
                        "enable='on'/'off' để bật/tắt, text để đặt nội dung (để trống dùng mẫu mặc định).",
            parameters=s("set welcome",
                enable={"desc": "'on' để bật, 'off' để tắt (có thể để trống nếu chỉ đổi text)"},
                text={"desc": "Nội dung tin chào tùy chỉnh (để trống để dùng mẫu mặc định)"}),
        ),
        FunctionDeclaration(
            name="set_antiflood",
            description="Cấu hình antiflood — tự động mute thành viên spam tin liên tục. "
                        "enable='on'/'off', limit=số tin tối đa, window=số giây cho phép.",
            parameters=s("set antiflood",
                enable={"desc": "'on' để bật, 'off' để tắt"},
                limit={"desc": "Số tin tối đa trong khoảng thời gian (mặc định 6)"},
                window={"desc": "Khoảng thời gian tính bằng giây (mặc định 10)"}),
        ),
        FunctionDeclaration(
            name="set_report_mode",
            description="Bật/tắt chức năng /report cho phép thành viên báo cáo vi phạm tới owner. "
                        "enable='on'/'off'.",
            parameters=s("set report mode", enable={"desc": "'on' để bật, 'off' để tắt", "required": True}),
        ),
        FunctionDeclaration(
            name="set_fn_open",
            description="Bật/tắt chế độ cho phép mọi thành viên trong nhóm trigger filter/note "
                        "(không chỉ owner). enable='on' = mọi người trigger được, 'off' = chỉ owner.",
            parameters=s("set fn open", enable={"desc": "'on' hoặc 'off'", "required": True}),
        ),
        # ── Restrict (đổi quyền thành viên chi tiết) ─────────────────────
        FunctionDeclaration(
            name="restrict_user",
            description="Đổi quyền (thẻ thành viên) chi tiết cho 1 người — hạn chế riêng từng loại "
                        "mà không cần mute hoàn toàn. Ví dụ: cấm gửi media nhưng vẫn cho nhắn text, "
                        "hoặc cấm mời người khác, cấm ghim tin... "
                        "Mỗi quyền là 'true' (được phép) hoặc 'false' (bị hạn chế). "
                        "Dùng khi owner nói 'cấm media của X', 'không cho X ghim tin', "
                        "'hạn chế quyền của X', 'đổi thẻ thành viên của X'.",
            parameters=s("restrict user",
                who={"desc": "Tên, @username, hoặc id", "required": True},
                can_send_messages={"desc": "Cho phép gửi tin nhắn văn bản: 'true'/'false' (mặc định 'true')"},
                can_send_media={"desc": "Cho phép gửi ảnh/video/audio/document: 'true'/'false' (mặc định 'true')"},
                can_send_polls={"desc": "Cho phép tạo poll: 'true'/'false' (mặc định 'true')"},
                can_send_other_messages={"desc": "Cho phép gửi sticker/gif/game: 'true'/'false' (mặc định 'true')"},
                can_add_web_page_previews={"desc": "Cho phép preview link: 'true'/'false' (mặc định 'true')"},
                can_change_info={"desc": "Cho phép đổi info nhóm: 'true'/'false' (mặc định 'false')"},
                can_invite_users={"desc": "Cho phép mời thành viên: 'true'/'false' (mặc định 'true')"},
                can_pin_messages={"desc": "Cho phép ghim tin nhắn: 'true'/'false' (mặc định 'false')"}),
        ),
        # ── Note management ───────────────────────────────────────────────
        FunctionDeclaration(
            name="list_notes",
            description="Xem danh sách tất cả ghi chú (note) đang được lưu trong bot.",
            parameters=s("list notes"),
        ),
        FunctionDeclaration(
            name="delete_note",
            description="Xóa hẳn 1 ghi chú (note) theo tên. Dùng khi owner muốn 'xóa note #tên', 'delnote'.",
            parameters=s("delete note", name={"desc": "Tên note cần xóa (không cần dấu #)", "required": True}),
        ),
        # ── Filter management ─────────────────────────────────────────────
        FunctionDeclaration(
            name="list_filters",
            description="Xem danh sách tất cả filter (bộ lọc tự động trả lời) đang hoạt động.",
            parameters=s("list filters"),
        ),
        FunctionDeclaration(
            name="delete_filter",
            description="Xóa 1 filter theo từ khóa trigger. Dùng khi owner muốn 'xóa filter X', 'stop filter X'.",
            parameters=s("delete filter", keyword={"desc": "Từ khóa trigger của filter cần xóa", "required": True}),
        ),
        # ── Bot config ────────────────────────────────────────────────────
        FunctionDeclaration(
            name="set_target_group",
            description="Đặt nhóm đích cho bot hoạt động (ID nhóm Telegram). "
                        "Dùng khi owner muốn đổi nhóm bot đang theo dõi.",
            parameters=s("set target group",
                group_id={"desc": "ID nhóm Telegram (số âm, vd: -1001234567890)", "required": True}),
        ),
        FunctionDeclaration(
            name="set_buffer_limit",
            description="Đặt số lượng tin nhắn tối đa lưu trong msg_buffer mỗi nhóm (tối thiểu 50, tối đa 200000). "
                        "Ảnh hưởng tới /tomtat. Dùng khi owner muốn 'setbufferlimit', 'đổi giới hạn buffer'.",
            parameters=s("set buffer limit",
                limit={"desc": "Số tin nhắn tối đa (50–200000)", "required": True}),
        ),
        FunctionDeclaration(
            name="list_users",
            description="Xem danh sách user đã từng tương tác với bot (tối đa 50 user đầu kèm ID và username).",
            parameters=s("list users"),
        ),
        # ── AI history ────────────────────────────────────────────────────
        FunctionDeclaration(
            name="clear_ai_history",
            description="Xóa lịch sử hội thoại AI của chat hiện tại — giúp AI quên ngữ cảnh cũ, bắt đầu cuộc trò chuyện mới. "
                        "Dùng khi owner nói 'xóa lịch sử AI', 'aiclear', 'quên hết đi'.",
            parameters=s("clear ai history"),
        ),
        # ── Slowmode ──────────────────────────────────────────────────────
        FunctionDeclaration(
            name="set_slowmode",
            description="Đặt chế độ slowmode cho nhóm — giới hạn tần suất gửi tin của thành viên. "
                        "seconds=0 để tắt. Các giá trị hợp lệ: 0, 10, 30, 60, 300 (5 phút), 900 (15 phút), 3600 (1 giờ). "
                        "Dùng khi owner muốn 'bật slowmode 30 giây', 'chống spam bằng slowmode', 'tắt slowmode'.",
            parameters=s("set slowmode",
                seconds={"desc": "Số giây chờ giữa các tin: 0 (tắt), 10, 30, 60, 300, 900, hoặc 3600", "required": True}),
        ),
        # ── Purge user messages ───────────────────────────────────────────
        FunctionDeclaration(
            name="purge_user_messages",
            description="Xóa toàn bộ tin nhắn của 1 thành viên cụ thể còn trong buffer. "
                        "Dùng khi owner nói 'xóa hết tin của X', 'dọn tin nhắn của X', 'xóa toàn bộ mess của X'. "
                        "Chỉ xóa được tin đã được bot track (còn trong msg_buffer).",
            parameters=s("purge user messages",
                who={"desc": "Tên, @username, hoặc id của người cần xóa tin nhắn", "required": True}),
        ),
        # ── Timed restrict ────────────────────────────────────────────────
        FunctionDeclaration(
            name="timed_restrict",
            description="Hạn chế quyền của 1 thành viên trong khoảng thời gian — tự động khôi phục sau. "
                        "Khác mute (chỉ tắt nhắn tin), tool này cho phép chọn riêng từng quyền + có hẹn giờ. "
                        "Dùng khi owner nói 'cấm gửi media của X trong 2 giờ', 'tắt quyền mời người của Y 1 ngày', "
                        "'hạn chế X gửi poll 30 phút'.",
            parameters=s("timed restrict",
                who={"desc": "Tên, @username, hoặc id", "required": True},
                duration={"desc": "Thời gian hạn chế, ví dụ '30m', '2h', '1d'. Để '0' nếu vĩnh viễn."},
                can_send_messages={"desc": "Cho phép gửi tin nhắn văn bản: 'true'/'false' (mặc định 'false')"},
                can_send_media={"desc": "Cho phép gửi ảnh/video/audio/doc: 'true'/'false' (mặc định 'false')"},
                can_send_polls={"desc": "Cho phép tạo poll: 'true'/'false' (mặc định 'false')"},
                can_send_other_messages={"desc": "Cho phép sticker/gif: 'true'/'false' (mặc định 'false')"},
                can_invite_users={"desc": "Cho phép mời người: 'true'/'false' (mặc định 'true')"},
                can_pin_messages={"desc": "Cho phép ghim tin: 'true'/'false' (mặc định 'false')"}),
        ),
        # ── Revoke all links ──────────────────────────────────────────────
        FunctionDeclaration(
            name="revoke_all_links",
            description="Thu hồi toàn bộ link mời cũ và tạo link mời mới. "
                        "Dùng khi nhóm bị leak link, 'thay link nhóm', 'thu hồi link mời', 'tạo link mới'.",
            parameters=s("revoke all links"),
        ),
        # ── Set/get group permissions (toàn bộ thành viên) ───────────────
        FunctionDeclaration(
            name="set_group_permissions",
            description="Đổi quyền mặc định cho TẤT CẢ thành viên trong nhóm (không phải 1 người). "
                        "Khác lock_group (tắt hết) — tool này cho phép chọn riêng từng quyền. "
                        "Dùng khi owner muốn 'tắt quyền mời người trong nhóm', 'cấm gửi poll', "
                        "'không cho thành viên thay info nhóm', 'bật lại quyền gửi media'. "
                        "Chỉ truyền quyền muốn thay đổi.",
            parameters=s("set group permissions",
                can_send_messages={"desc": "Cho phép gửi tin văn bản: 'true'/'false'"},
                can_send_media={"desc": "Cho phép gửi ảnh/video/audio/doc: 'true'/'false'"},
                can_send_polls={"desc": "Cho phép tạo poll: 'true'/'false'"},
                can_send_other_messages={"desc": "Cho phép sticker/gif/game: 'true'/'false'"},
                can_add_web_page_previews={"desc": "Cho phép preview link: 'true'/'false'"},
                can_change_info={"desc": "Cho phép thay đổi info nhóm: 'true'/'false'"},
                can_invite_users={"desc": "Cho phép mời thành viên: 'true'/'false'"},
                can_pin_messages={"desc": "Cho phép ghim tin: 'true'/'false'"}),
        ),
        FunctionDeclaration(
            name="get_group_permissions",
            description="Xem quyền mặc định hiện tại của tất cả thành viên trong nhóm. "
                        "Dùng khi owner hỏi 'nhóm đang mở/đóng quyền gì', 'thành viên được làm gì', "
                        "'quyền nhóm hiện tại'.",
            parameters=s("get group permissions"),
        ),
        # ── Delete last N messages ────────────────────────────────────────
        FunctionDeclaration(
            name="delete_last_n_messages",
            description="Xóa N tin nhắn gần nhất trong nhóm (dựa theo buffer đã track). "
                        "Dùng khi owner nói 'xóa 20 tin gần nhất', 'dọn 10 tin cuối', 'xóa N tin'. "
                        "Tối đa 500 tin/lần. Khác purge_messages (cần 2 ID) — tool này chỉ cần số lượng.",
            parameters=s("delete last n messages",
                count={"desc": "Số tin nhắn cần xóa (1–500)", "required": True},
                from_message_id={"desc": "Nếu có, xóa N tin trước tin nhắn này (tùy chọn)"}),
        ),
        # ── Antilink ─────────────────────────────────────────────────────
        FunctionDeclaration(
            name="set_antilink",
            description="Bật/tắt chế độ tự động xóa link ngoài trong nhóm (antilink). "
                        "Khi bật, bot xóa mọi tin chứa link http/t.me không thuộc whitelist. "
                        "Dùng khi owner muốn 'bật antilink', 'chặn link ngoài', 'không cho spam link', "
                        "'tắt antilink', 'cho phép youtube.com'.",
            parameters=s("set antilink",
                enable={"desc": "'on' để bật, 'off' để tắt", "required": True},
                whitelist={"desc": "Domain cho phép, cách nhau bằng dấu phẩy. VD: 'youtube.com,google.com'"}),
        ),
        # ── Join approval ─────────────────────────────────────────────────
        FunctionDeclaration(
            name="set_join_approval",
            description="Bật/tắt yêu cầu phê duyệt khi có người xin vào nhóm. "
                        "Khi bật, owner phải duyệt thủ công mỗi người xin join. "
                        "Dùng khi owner muốn 'bật duyệt thành viên', 'require approval', "
                        "'tắt join request', 'cho vào thẳng không cần duyệt'.",
            parameters=s("set join approval",
                enable={"desc": "'on' để bật yêu cầu duyệt, 'off' để cho vào thẳng", "required": True}),
        ),
    ]
    return [genai_types.Tool(function_declarations=decls)]
