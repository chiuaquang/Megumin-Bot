"""
utils.py — Trạng thái runtime, helper dùng chung, lời thoại maid, HELP_TEXT.
"""

import re
import time
import random
from collections import deque
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions,
    InputMediaPhoto, InputMediaVideo,
)
from telegram.constants import ParseMode

from config import CONFIG, DB, now_vn, save_db

# ── MarkdownV2 an toàn cho nội dung TỰ DO của người dùng (filter/note) ────
# Lý do đổi từ Markdown (v1) sang MarkdownV2:
# Markdown v1 hầu như không escape được — 1 ký tự *, _, ` lẻ (không khép
# cặp đúng) ở bất kỳ đâu trong nội dung khiến Telegram trả lỗi parse cho
# TOÀN BỘ tin nhắn; code cũ fallback bằng cách xoá sạch [ ] * _ ` — phá
# luôn cú pháp link [chữ](url) người dùng cố ý gõ (mất link, chỉ còn text
# trần). Nội dung dài/nhiều dòng/nhiều ký tự đặc biệt (URL, gạch dưới
# trong tên file, dấu chấm, dấu gạch ngang...) có xác suất dính lỗi này
# cao hơn nhiều — đúng hiện tượng "viết dài thì link [] () biến mất".
#
# MarkdownV2 escape được TẤT CẢ ký tự đặc biệt bằng "\", nên ta có thể giữ
# nguyên 100% các cú pháp người dùng cố ý gõ (link, bold, italic, code) và
# escape an toàn mọi ký tự đặc biệt còn lại — không còn cách nào làm hỏng
# parse của cả tin nhắn nữa.
_MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
_MDV2_SPECIAL_RE = re.compile("[" + re.escape(_MDV2_SPECIAL) + "]")

# [FIX v2] Có 2 bug cần xử lý đồng thời:
#
# Bug 1 — _MD_LINK_RE cũ dùng [^\[\]]* cấm [ ] bên trong phần text → không
# match link có tiêu đề dạng "[Anime OP | Full Song](url)".
# Fix: dùng lazy [^\n]*? thay vì character class cứng nhắc.
#
# Bug 2 — AI thường wrap link bằng ** thành **[text](url)**. Bước tiền xử lý
# normalize ** → * biến thành *[text](url)* — lúc này _MD_BOLD_RE match toàn
# bộ *[text](url)* trước, nội dung bên trong bị escape thô → link chết.
# Fix: thêm _MD_BOLD_LINK_RE nhận dạng *[text](url)* / **[text](url)** ngay
# trong tokenizer, đặt ĐẦU TIÊN để ưu tiên match trước _MD_BOLD_RE.
#
# Dùng named groups cho TẤT CẢ pattern để to_markdown_v2 không phụ thuộc vào
# số thứ tự group (số này dễ lệch khi thêm/sửa pattern về sau).
_MD_BOLD_LINK_RE = re.compile(
    r'\*{1,2}\[(?P<bl_text>[^\n]*?)\]\((?P<bl_url>https?://[^\s()]+)\)\*{1,2}'
)
_MD_LINK_RE    = re.compile(r"\[(?P<link_text>[^\n]*?)\]\((?P<link_url>https?://[^\s()]+)\)")
_MD_BOLD_RE    = re.compile(r"\*(?P<bold_text>[^*\n]+)\*")
_MD_ITALIC_RE  = re.compile(r"(?<!\w)_(?P<italic_text>[^_\n]+)_(?!\w)")
_MD_CODE_RE    = re.compile(r"`(?P<code_text>[^`\n]+)`")
# URL TRẦN (không bọc [text](url)) — vd AI tự ý nhắc URL ngay trong câu chữ
# thay vì dùng cú pháp markdown link. Nếu không xử lý riêng, URL trần sẽ bị
# escape toàn bộ ký tự đặc biệt (. _ = ! ...) như text thường, làm vỡ link.
# Match TRƯỚC bold/italic/code (nhưng SAU link thật — đặt cuối union để
# link thật ưu tiên match hết, URL trần chỉ bắt phần chưa được [..](..) bọc).
_MD_BARE_URL_RE = re.compile(r"https?://[^\s()<>\[\]]+[^\s()<>\[\].,;:!?'\"]")
# Quét token theo thứ tự ưu tiên: bold-link → link → bold → italic → code → url-trần
_MD_TOKEN_RE = re.compile(
    "|".join([
        _MD_BOLD_LINK_RE.pattern,  # ← ĐẦU TIÊN: **[text](url)** / *[text](url)*
        _MD_LINK_RE.pattern,
        _MD_BOLD_RE.pattern,
        _MD_ITALIC_RE.pattern,
        _MD_CODE_RE.pattern,
        _MD_BARE_URL_RE.pattern,
    ])
)


def _escape_mdv2(raw: str) -> str:
    """Escape thô — TOÀN BỘ ký tự đặc biệt MarkdownV2, không nhận diện cú
    pháp gì cả. Dùng cho phần văn bản nằm NGOÀI mọi cú pháp markdown, và
    cho URL bên trong link (URL chỉ cần escape ')' và '\\', theo spec
    Telegram, không escape các ký tự khác)."""
    return _MDV2_SPECIAL_RE.sub(lambda c: "\\" + c.group(0), raw)


# Alias công khai — dùng khi cần escape THÔ một chuỗi dữ liệu hoàn toàn
# không kiểm soát được (vd nội dung exception message) mà KHÔNG muốn
# to_markdown_v2() cố nhận diện markup bên trong nó (tránh trường hợp 1
# backtick/ngoặc lẻ trong dữ liệu đó làm lệch ranh giới token).
escape_markdown_v2 = _escape_mdv2


def _escape_mdv2_url(url: str) -> str:
    """URL trong [text](url): theo spec Telegram, chỉ ')' và '\\' cần
    escape — escape thêm ký tự khác sẽ làm hỏng chính URL đó."""
    return url.replace("\\", "\\\\").replace(")", "\\)")


def to_markdown_v2(text: str) -> str:
    """Chuyển nội dung filter/note (gõ tự do, có thể chứa *bold*, _italic_,
    `code`, [chữ](url)) hoặc nội dung AI sinh ra (có thể chứa URL trần,
    không bọc markdown) thành MarkdownV2 hợp lệ: escape an toàn mọi ký tự
    đặc biệt KHÔNG thuộc 1 trong các cú pháp markup, giữ nguyên các cú pháp
    đó. URL trần (không [text](url)) được TỰ ĐỘNG bọc thành [url](url) để
    vẫn bấm được, vì escape thô 1 URL trần sẽ phá hỏng chính URL đó (vd
    chuyển youtube.com/watch?v=xxx thành youtube\\.com/watch?v\\=xxx — không
    còn là link hợp lệ). Nhờ vậy nội dung dài/nhiều ký tự đặc biệt vẫn luôn
    parse được, link không bao giờ bị mất hay đứt giữa đường.

    Tiền xử lý (trước regex chính):
    • **text** → *text*  (AI hay dùng double-asterisk bold kiểu CommonMark;
      nếu không convert, dấu * bên ngoài bị escape thành \\* → hiện ra
      literal *** quanh chữ thay vì in đậm đúng cách).
    • __text__ → _text_  (tương tự cho italic double-underscore).
    """
    if not text:
        return text
    # ── Tiền xử lý: chuẩn hoá Markdown kiểu CommonMark → Telegram MarkdownV2 ──
    # Chỉ normalize **text** KHÔNG phải link (trường hợp **[text](url)** được
    # _MD_BOLD_LINK_RE trong tokenizer xử lý trực tiếp — không normalize ở đây
    # để tránh tạo ra *[text](url)* rồi lại bị _MD_BOLD_RE match nhầm).
    text = re.sub(r'\*\*([^*\n\[]+?)\*\*', r'*\1*', text)
    text = re.sub(r'(?<!\w)__([^_\n]+?)__(?!\w)', r'_\1_', text)
    out = []
    last = 0
    for m in _MD_TOKEN_RE.finditer(text):
        out.append(_escape_mdv2(text[last:m.start()]))
        gd = m.groupdict()
        if gd.get("bl_text") is not None:
            # **[text](url)** hoặc *[text](url)* → bold hyperlink MarkdownV2
            out.append(f"*[{_escape_mdv2(gd['bl_text'])}]({_escape_mdv2_url(gd['bl_url'])})*")
        elif gd.get("link_text") is not None:
            # [text](url) thường — kể cả text có chứa [ ] bên trong
            out.append(f"[{_escape_mdv2(gd['link_text'])}]({_escape_mdv2_url(gd['link_url'])})")
        elif gd.get("bold_text") is not None:
            out.append(f"*{_escape_mdv2(gd['bold_text'])}*")
        elif gd.get("code_text") is not None:
            out.append(f"`{gd['code_text']}`")  # nội dung code: chỉ ` và \ cần escape, hiếm gặp trong nội dung tự do
        elif m.group(0).startswith("http"):
            # URL trần — bọc thành [url](url) để vẫn bấm được
            url = m.group(0)
            out.append(f"[{_escape_mdv2(url)}]({_escape_mdv2_url(url)})")
        elif gd.get("italic_text") is not None:
            out.append(f"_{_escape_mdv2(gd['italic_text'])}_")
        last = m.end()
    out.append(_escape_mdv2(text[last:]))
    return "".join(out)

# ── Phiên bản & thời điểm khởi động (dùng cho /version, /uptime, /stats) ──
BOT_VERSION   = "2.9.0"
PROCESS_START = now_vn()

# ── Logging (dùng chung toggle search_debug, prefix [VISION-DBG]) ──
def vlog(*args) -> None:
    if CONFIG.get("search_debug", False):
        ts = now_vn().strftime("%H:%M:%S")
        print(f"[VISION-DBG {ts}]", *args, flush=True)


# ── Media: CHỈ dùng file_id Telegram, KHÔNG lưu file vật lý nào ───────────
# Trước đây bot tải ảnh/video về đĩa cục bộ (media/) để dự phòng khi
# file_id "biến mất" — nhưng file_id Telegram là tham chiếu ổn định, KHÔNG
# hết hạn, dùng gửi lại được ngay (kể cả lấy thẳng từ tin người dùng gửi,
# không cần qua bot gửi lại trước). Lưu file vật lý chỉ tốn thời gian
# (network round-trip get_file + download_to_drive khi tạo, rồi lại phải
# upload khi gửi) và vô nghĩa trên host không có persistent disk (Render
# free tier) — file mất ngay khi restart. Bỏ hẳn, chỉ lưu {"type","file_id"}.

# ── In-memory buffers ─────────────────────────────────────────────
MSG_BUFFER:          deque = deque(maxlen=200)
MEDIA_GROUP_PENDING: dict  = {}

# Lưu tạm các yêu cầu /note hoặc /filter đang chờ xác nhận ghi đè (khi note/
# filter đó đã có ≥2 câu random %%% và người dùng gõ lại KHÔNG kèm %%% — cần
# hỏi trước khi xoá hết câu cũ). Key: id ngắn (uuid hex) dùng làm callback_data
# (Telegram giới hạn 64 byte nên không nhét thẳng tên note/trigger vào key).
# Value: {"kind", "chat_key", "name"/"primary", ...dữ liệu cần để ghi đè, "ts"}.
PENDING_OVERWRITE: dict = {}
PENDING_OVERWRITE_TTL = 120  # giây — hết hạn tự huỷ nếu không xác nhận kịp


def set_pending_overwrite(pkey: str, payload: dict) -> None:
    now_ts = time.time()
    stale = [k for k, v in PENDING_OVERWRITE.items() if now_ts - v.get("ts", 0) > PENDING_OVERWRITE_TTL]
    for k in stale:
        PENDING_OVERWRITE.pop(k, None)
    payload["ts"] = now_ts
    PENDING_OVERWRITE[pkey] = payload


def pop_pending_overwrite(pkey: str):
    payload = PENDING_OVERWRITE.pop(pkey, None)
    if payload and time.time() - payload.get("ts", 0) > PENDING_OVERWRITE_TTL:
        return None  # hết hạn — coi như không có
    return payload


# Map id ngắn (callback_data) → key filter thật, dùng cho nút "xem chi tiết"
# trong /filters (callback_data Telegram giới hạn 64 byte, không nhét thẳng
# được key filter dài). TTL dài hơn PENDING_OVERWRITE vì người dùng có thể
# bấm xem nhiều filter lần lượt trong cùng 1 lần gọi /filters.
FILTER_VIEW_CACHE: dict = {}
FILTER_VIEW_CACHE_TTL = 3600  # 1 giờ


def register_filter_view(pkey: str, filter_key: str) -> None:
    now_ts = time.time()
    stale = [k for k, v in FILTER_VIEW_CACHE.items() if now_ts - v[1] > FILTER_VIEW_CACHE_TTL]
    for k in stale:
        FILTER_VIEW_CACHE.pop(k, None)
    FILTER_VIEW_CACHE[pkey] = (filter_key, now_ts)


def get_filter_view(pkey: str):
    entry = FILTER_VIEW_CACHE.get(pkey)
    if not entry:
        return None
    filter_key, ts = entry
    if time.time() - ts > FILTER_VIEW_CACHE_TTL:
        FILTER_VIEW_CACHE.pop(pkey, None)
        return None
    return filter_key


# Buffer ảnh theo media_group_id để hỗ trợ phân tích nhiều ảnh cùng lúc
# {mg_id: {"msgs": [msg, ...], "ts": timestamp}}
VISION_MEDIA_GROUP: dict = {}
VISION_MEDIA_GROUP_TTL = 10  # giây — album cũ hơn sẽ bị bỏ qua


def vision_register_album_photo(msg) -> None:
    """Lưu lại 1 message ảnh thuộc album để dùng khi có trigger phân tích."""
    mg_id = getattr(msg, "media_group_id", None)
    if not mg_id:
        return
    now_ts = time.time()
    entry = VISION_MEDIA_GROUP.setdefault(mg_id, {"msgs": [], "ts": now_ts})
    entry["msgs"].append(msg)
    entry["ts"] = now_ts
    # Dọn rác album cũ
    stale = [k for k, v in VISION_MEDIA_GROUP.items() if now_ts - v["ts"] > VISION_MEDIA_GROUP_TTL]
    for k in stale:
        VISION_MEDIA_GROUP.pop(k, None)

def vision_get_album_photos(mg_id: str):
    """Lấy danh sách message ảnh trong album (nếu còn trong buffer)."""
    if not mg_id:
        return []
    entry = VISION_MEDIA_GROUP.get(mg_id)
    return list(entry["msgs"]) if entry else []


# Buffer album riêng cho /note và /filter (TTL dài hơn nhiều so với
# VISION_MEDIA_GROUP — người dùng có thể gửi 1 album ảnh rồi vài tiếng/qua
# ngày sau mới quay lại reply để gõ /note hoặc /filter). Chỉ cache tạm thời
# để GOM ĐỦ album lúc tạo note/filter; sau khi lưu xong, ảnh/video đã được
# tải về cố định trong note/filter đó và không phụ thuộc buffer này nữa.
# {mg_id: {"msgs": [msg, ...], "ts": timestamp}}
NF_MEDIA_GROUP: dict = {}
NF_MEDIA_GROUP_TTL = 24 * 3600  # 24 giờ


def nf_register_album_msg(msg) -> None:
    """Lưu lại 1 message ảnh/video/document thuộc album, để /note hoặc
    /filter có thể gom đủ cả album khi reply vào 1 trong các ảnh đó."""
    mg_id = getattr(msg, "media_group_id", None)
    if not mg_id:
        return
    now_ts = time.time()
    entry = NF_MEDIA_GROUP.setdefault(mg_id, {"msgs": [], "ts": now_ts})
    # Tránh trùng (Telegram đôi khi gửi update lặp cho cùng message)
    if not any(getattr(m, "message_id", None) == msg.message_id for m in entry["msgs"]):
        entry["msgs"].append(msg)
    entry["ts"] = now_ts
    stale = [k for k, v in NF_MEDIA_GROUP.items() if now_ts - v["ts"] > NF_MEDIA_GROUP_TTL]
    for k in stale:
        NF_MEDIA_GROUP.pop(k, None)


def nf_get_album_msgs(mg_id: str):
    """Lấy danh sách message trong album (sắp theo message_id tăng dần để
    giữ đúng thứ tự gốc người dùng đã gửi)."""
    if not mg_id:
        return []
    entry = NF_MEDIA_GROUP.get(mg_id)
    if not entry:
        return []
    return sorted(entry["msgs"], key=lambda m: getattr(m, "message_id", 0))

# ── Utilities ─────────────────────────────────────────────────────
def is_primary_owner(uid: int) -> bool:
    """Owner CHÍNH (OWNER_ID) — duy nhất được thêm/xoá owner phụ."""
    return bool(uid) and uid == CONFIG.get("owner_id", 0)

def all_owner_ids() -> list:
    """Owner chính + toàn bộ owner phụ (thêm bằng /addowner)."""
    ids = []
    primary = CONFIG.get("owner_id", 0)
    if primary:
        ids.append(primary)
    for x in CONFIG.get("extra_owners") or []:
        if x not in ids:
            ids.append(x)
    return ids

def is_owner(uid: int) -> bool:
    """True nếu uid là owner chính HOẶC owner phụ. Mọi gate quyền trong bot
    đều đi qua hàm này nên owner phụ có đủ quyền như owner chính."""
    if not uid:
        return False
    return uid == CONFIG.get("owner_id", 0) or uid in (CONFIG.get("extra_owners") or [])

def is_bot_admin(uid: int) -> bool:
    return uid in DB.get("admins", [])

async def is_chat_admin(bot, chat_id: int, user_id: int) -> bool:
    """True nếu user là owner, bot-admin (đã /promote), hoặc admin/creator
    thật trên Telegram của chat này. Dùng cho filter {admin}/{user}."""
    if is_owner(user_id) or is_bot_admin(user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

def get_chat_key(chat_id) -> str:
    return str(chat_id)

def is_group_scope(chat) -> bool:
    """True nếu note/filter được phép hoạt động ở đây: nhóm, supergroup (kể cả
    topic — vẫn là supergroup), hoặc channel. KHÔNG bao gồm chat riêng (DM)."""
    return getattr(chat, "type", None) in ("group", "supergroup", "channel")

# Note & filter dùng CHUNG 1 kho duy nhất (không tách theo từng chat_id) —
# owner lưu 1 lần ở bất kỳ nhóm/kênh/topic nào, dùng được ở MỌI nhóm/kênh/
# topic khác mà bot có mặt (trừ DM, do is_group_scope() loại trừ riêng).
NOTES_FILTERS_KEY = "_global"


def migrate_notes_filters_to_global() -> None:
    """Gộp note/filter cũ đang lưu rải theo từng chat_id vào 1 kho `_global`
    chung duy nhất. Idempotent — gọi nhiều lần không sao, chỉ làm việc khi
    còn key cũ khác `_global`. Trùng tên: chat xử lý sau ghi đè/gộp %%% vào
    chat xử lý trước (qua merge_variants), media giữ cái có sẵn nếu cái mới
    không có media riêng."""
    for store_name in ("notes", "filters"):
        store = DB.get(store_name)
        if not isinstance(store, dict):
            continue
        legacy_keys = [k for k in store if k != NOTES_FILTERS_KEY]
        if not legacy_keys:
            continue
        glob = store.setdefault(NOTES_FILTERS_KEY, {})
        for ck in legacy_keys:
            chat_data = store.pop(ck)
            if not isinstance(chat_data, dict):
                continue
            for name, entry in chat_data.items():
                if name not in glob:
                    glob[name] = entry
                    continue
                old, new = glob[name], entry
                if store_name == "notes":
                    old_n, new_n = normalize_note(old), normalize_note(new)
                    glob[name] = {
                        "variants": merge_variants(old_n["variants"], new_n["variants"]),
                        "media": old_n.get("media") or new_n.get("media"),
                    }
                else:  # filters — chỉ gộp khi cùng "shape" (dict có triggers)
                    if isinstance(old, dict) and isinstance(new, dict) and "triggers" in old and "triggers" in new:
                        old["variants"] = merge_variants(old.get("variants", []), new.get("variants", []))
                        old["media"] = old.get("media") or new.get("media")
                        glob[name] = old
                    else:
                        glob[name] = new  # ưu tiên bản mới nếu format không khớp
    save_db(DB)


def track_user(user) -> None:
    DB["users"][str(user.id)] = {
        "name": user.full_name,
        "username": user.username or "",
    }

def lookup_user_id_by_username(username: str) -> int | None:
    """Tra cứu user_id từ @username trong DB["users"] (do track_user() ghi
    lại mỗi khi ai đó chat). CẦN dùng cách này thay vì gọi thẳng Telegram
    API vì get_chat_member() KHÔNG hỗ trợ tham số username cho user thường
    — Bot API chỉ resolve theo user_id số; @username chỉ work qua get_chat()
    với public channel/group, không phải với member. Vì vậy nếu không tự
    lưu cache username→id từ tin nhắn thật, bot sẽ luôn "không tìm thấy"
    bất kỳ ai chưa từng /start DM riêng với bot — kể cả khi họ đang chat
    sống trong nhóm. Đây là hạn chế cố định của Telegram, mọi bot (Rose,
    Combot,...) đều né bằng đúng cơ chế cache này, không có "API mới" nào
    khác để bot tự enumerate username của thành viên.
    Trả về None nếu username chưa từng được thấy chat lần nào."""
    uname = username.lstrip("@").lower()
    for uid, info in DB.get("users", {}).items():
        stored = (info.get("username") or "").lower()
        if stored and stored == uname:
            try:
                return int(uid)
            except (TypeError, ValueError):
                continue
    return None

async def require_owner(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_owner(user.id):
        return False
    return True

def _dm_echo(sent) -> None:
    """[DM MONITOR] Bot vừa trả lời trong DM → chuyển bản sao sang DM owner (chạy nền)."""
    try:
        if sent is not None and sent.chat.type == "private":
            from handlers_monitor import schedule_dm_monitor   # import muộn: tránh vòng import
            schedule_dm_monitor(sent.get_bot(), sent, outgoing=True)
    except Exception:
        pass


# @username sau khi qua to_markdown_v2() sẽ bị escape dấu gạch dưới
# (vd @Anh_Yeu → @Anh\_Yeu) khiến Telegram không linkify được mention.
# Regex này khôi phục lại các @mention về dạng nguyên bản.
_MENTION_RE = re.compile(r"@([A-Za-z0-9_]+(?:\\_[A-Za-z0-9_]+)*)")

def _fix_md_mentions(converted: str) -> str:
    """Khôi phục @username bị escape trong text đã convert MarkdownV2.
    Telegram chỉ tạo mention bấm được khi các ký tự @username còn nguyên."""
    if not converted or "@" not in converted:
        return converted
    return _MENTION_RE.sub(lambda m: "@" + m.group(1).replace("\\_", "_"), converted)


async def safe_reply(update: Update, text: str, **kw):
    """Trả lời tin nhắn. Trả về Message đã gửi (hoặc None nếu thất bại) để nơi
    gọi có thể ghi lại message_id — các caller cũ bỏ qua giá trị trả về nên
    không bị ảnh hưởng."""
    message = getattr(update, "message", None) or getattr(update, "effective_message", None)
    if not message:
        return None
    try:
        sent = await message.reply_text(
            _fix_md_mentions(to_markdown_v2(text)), parse_mode=ParseMode.MARKDOWN_V2, **kw
        )
        _dm_echo(sent)
        return sent
    except Exception:
        try:
            sent = await message.reply_text(text, **kw)
            _dm_echo(sent)
            return sent
        except Exception:
            return None

# ── Media đính kèm (ảnh/video/gif) cho câu thoại / filter / status AI ─────
def _detect_message_media(message):
    """Phát hiện media hỗ trợ (photo/video/animation) trong 1 message
    Telegram. Trả về (loại, file_id) hoặc None nếu không có gì phù hợp."""
    if not message:
        return None
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.animation:
        return "animation", message.animation.file_id
    if message.document and message.document.mime_type:
        mt = message.document.mime_type
        if mt.startswith("image/"):
            return "photo", message.document.file_id
        if mt.startswith("video/"):
            return "video", message.document.file_id
    if message.sticker and not message.sticker.is_animated and not message.sticker.is_video:
        return "photo", message.sticker.file_id
    return None


def extract_message_media(message) -> dict | None:
    """Lấy {"type","file_id"} TRỰC TIẾP từ 1 message Telegram — KHÔNG gọi
    API tải file, KHÔNG lưu gì xuống đĩa. file_id Telegram dùng gửi lại được
    ngay (kể cả lấy từ tin người dùng gửi, không cần qua bot gửi lại trước),
    và không bao giờ hết hạn — nên việc tải về lưu cục bộ là dư thừa, chỉ
    tốn thời gian (network round-trip get_file + download_to_drive) và
    không bền vững trên host không có persistent disk (Render free tier).
    Trả None nếu message không chứa media hỗ trợ."""
    info = _detect_message_media(message)
    if not info:
        return None
    mtype, file_id = info
    return {"type": mtype, "file_id": file_id}


def extract_message_album(message) -> list[dict]:
    """Lấy TOÀN BỘ {"type","file_id"} trong album (media_group) chứa
    `message`, nếu có — dùng buffer `NF_MEDIA_GROUP` đã gom các message cùng
    album trước đó (xem `nf_register_album_msg`). Nếu `message` không thuộc
    album nào (hoặc buffer đã hết hạn/trống), fallback lấy đúng 1 media của
    chính `message`. Không tải/lưu file nào — chỉ lấy file_id.
    Trả về list[{"type","file_id"}], rỗng nếu không có media nào."""
    mg_id = getattr(message, "media_group_id", None)
    msgs = nf_get_album_msgs(mg_id) if mg_id else []
    if not msgs:
        msgs = [message]  # không phải album (hoặc cache hết hạn) — chỉ 1 ảnh như cũ
    out: list[dict] = []
    for m in msgs:
        media = extract_message_media(m)
        if media:
            out.append(media)
    return out


def _extract_sent_file_id(sent_message, mtype: str):
    try:
        if mtype == "photo" and sent_message.photo:
            return sent_message.photo[-1].file_id
        if mtype == "video" and sent_message.video:
            return sent_message.video.file_id
        if mtype == "animation" and sent_message.animation:
            return sent_message.animation.file_id
    except Exception:
        pass
    return None


def _media_sender(message, mtype: str):
    return {
        "photo":     getattr(message, "reply_photo", None),
        "video":     getattr(message, "reply_video", None),
        "animation": getattr(message, "reply_animation", None),
    }.get(mtype)


TG_CAPTION_LIMIT = 1024  # giới hạn Telegram cho caption ảnh/video/gif
_GROUP_INPUT_CLS = {"photo": InputMediaPhoto, "video": InputMediaVideo}  # animation KHÔNG được phép trong media group


async def _send_one_media(message, media: dict, caption, use_markdown: bool, **send_kwargs):
    """Gửi đúng 1 media (ảnh/video/gif đơn lẻ) — không phải album. Trả
    Message đã gửi, hoặc ném exception nếu lỗi (nơi gọi tự xử lý retry).
    Chỉ dùng `file_id` Telegram — không lưu/đọc file vật lý nào (xem
    `extract_message_media`): Telegram chỉ copy reference nội bộ giữa các
    chat, gửi gần như tức thì, không tốn băng thông tải lên lại, và
    `file_id` không hết hạn nên không cần fallback nào khác.
    `caption` PHẢI đã qua to_markdown_v2() nếu use_markdown=True — hàm này
    không tự escape, chỉ gửi đi."""
    sender = _media_sender(message, media.get("type"))
    if not sender:
        raise ValueError(f"không hỗ trợ loại media: {media.get('type')}")
    file_id = media.get("file_id")
    if not file_id:
        raise ValueError("media không có file_id")
    kwargs = dict(send_kwargs)
    if use_markdown:
        kwargs["parse_mode"] = ParseMode.MARKDOWN_V2
    return await sender(file_id, caption=caption, **kwargs)


async def _send_media_caption(message, media, text: str, **send_kwargs) -> bool:
    """Gửi `media` — list[dict] (1 ảnh/video/gif, hoặc ALBUM nhiều cái) —
    kèm `text` làm caption ra `message`. `text` là PLAIN TEXT thô (chưa
    escape) — hàm này tự escape bằng to_markdown_v2() và gửi MarkdownV2.
    Chỉ dùng `file_id` Telegram — không đọc/lưu file vật lý nào, gửi nhanh
    vì Telegram chỉ copy reference nội bộ giữa các chat.
    Trả True nếu gửi thành công (kèm hoặc không kèm caption), False nếu
    KHÔNG media nào gửi được (item không có file_id hợp lệ) — trường hợp đó
    nơi gọi tự lo fallback gửi text.
    Ảnh/video/gif LUÔN được gửi TRỌN VẸN, không bao giờ bị cắt: nếu caption
    quá dài (>1024 ký tự sau khi escape) thì media gửi không kèm caption,
    text được tách ra gửi thành 1 tin nhắn riêng ngay sau đó — full ảnh,
    full video, full text, không mất phần nào.
    Album (≥2 ảnh/video) gửi bằng `send_media_group` THẬT của Telegram —
    caption chỉ hiện ở item đầu (giới hạn của Telegram, không phải bug).
    Animation (gif) không được Telegram cho phép trong media group nên luôn
    tách gửi riêng (sau album, hoặc 1 mình nếu cả câu chỉ có gif)."""
    items = [m for m in (media or []) if m and m.get("file_id")]
    if not items:
        return False

    safe_text = to_markdown_v2(text) if text else ""
    fits_caption = bool(safe_text) and len(safe_text) <= TG_CAPTION_LIMIT
    caption_consumed = False  # True khi `text` đã được gắn thành công làm caption ở đâu đó
    has_spoiler = bool(send_kwargs.get("has_spoiler"))  # Telegram: spoiler là field PER-ITEM trong album, không phải tham số sendMediaGroup

    groupable = [m for m in items if m.get("type") in _GROUP_INPUT_CLS]
    groupable_ids = {id(m) for m in groupable}
    solo = [m for m in items if id(m) not in groupable_ids]  # animation, hoặc không group-được

    sent_any = False

    if len(groupable) >= 2:
        cap_for_album = safe_text if fits_caption else None
        try:
            input_group = [
                _GROUP_INPUT_CLS[m["type"]](
                    media=m["file_id"], caption=(cap_for_album if i == 0 else None),
                    parse_mode=ParseMode.MARKDOWN_V2 if (i == 0 and cap_for_album) else None,
                    has_spoiler=has_spoiler,
                )
                for i, m in enumerate(groupable)
            ]
            gkwargs = {k: v for k, v in send_kwargs.items() if k != "has_spoiler"}
            sent_list = await message.reply_media_group(media=input_group, **gkwargs)
            for m, sent in zip(groupable, sent_list or []):
                fid = _extract_sent_file_id(sent, m.get("type"))
                if fid and fid != m.get("file_id"):
                    m["file_id"] = fid
            sent_any = True
            if cap_for_album:
                caption_consumed = True
        except Exception as e1:
            # Lỗi gửi album KÈM caption (vd 1 file_id thật sự hỏng) → thử lại
            # album KHÔNG caption, để không làm mất cả album vì 1 lỗi text.
            try:
                input_group2 = [
                    _GROUP_INPUT_CLS[m["type"]](media=m["file_id"], has_spoiler=has_spoiler)
                    for m in groupable
                ]
                gkwargs = {k: v for k, v in send_kwargs.items() if k != "has_spoiler"}
                sent_list = await message.reply_media_group(media=input_group2, **gkwargs)
                for m, sent in zip(groupable, sent_list or []):
                    fid = _extract_sent_file_id(sent, m.get("type"))
                    if fid and fid != m.get("file_id"):
                        m["file_id"] = fid
                sent_any = True
            except Exception as e2:
                vlog("media: gửi album lỗi:", repr(e1), repr(e2))
    elif len(groupable) == 1:
        solo = groupable + solo  # chỉ 1 ảnh/video group-được — gửi như media đơn, gộp chung vòng dưới

    # Phần còn lại (gif/animation, hoặc fallback nếu album ở trên chưa gửi
    # được gì) gửi tuần tự từng cái. Caption (nếu chưa dùng) gắn vào CÁI
    # ĐẦU TIÊN gửi thành công ở vòng này.
    for m in solo:
        cap = safe_text if (fits_caption and not caption_consumed) else None
        try:
            sent = await _send_one_media(message, m, cap, use_markdown=bool(cap), **send_kwargs)
        except Exception as e1:
            try:
                sent = await _send_one_media(message, m, None, use_markdown=False, **send_kwargs)
            except Exception as e2:
                vlog("media: gửi lẻ lỗi:", repr(e1), repr(e2))
                continue
            cap = None
        fid = _extract_sent_file_id(sent, m.get("type")) if sent else None
        if fid and fid != m.get("file_id"):
            m["file_id"] = fid
        if cap:
            caption_consumed = True
        sent_any = True

    if not sent_any:
        return False

    save_db(DB)  # cache file_id mới (nếu có) cho mọi item vừa gửi thành công

    # `text` chưa từng được gắn làm caption ở đâu cả (quá dài ngay từ đầu,
    # hoặc mọi nỗ lực gắn caption đều lỗi) → gửi rời ra tin nhắn riêng,
    # KHÔNG ảnh hưởng gì đến media đã gửi (media luôn ra đủ, đúng, full).
    if safe_text and not caption_consumed and hasattr(message, "reply_text"):
        try:
            await message.reply_text(safe_text, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception:
            try:
                await message.reply_text(text)  # fallback cuối: text thô, không format
            except Exception:
                pass
    return True


async def send_styled(target, text: str, media=None) -> None:
    """Gửi `text` (plain, sẽ tự escape MarkdownV2), kèm media local nếu có
    (ảnh/video/gif, hoặc album nhiều cái). Nhận cả dict đơn kiểu cũ (vd
    phrase_media của /setmedia) lẫn list (note/filter). Nếu file local đã
    mất (vd sau khi host restart), tự fallback gửi bằng file_id đã cache.
    `target` có thể là Update hoặc 1 Message (vd query.message)."""
    message = getattr(target, "message", None) or getattr(target, "effective_message", None) or target
    media_list = normalize_media_list(media)
    if media_list and hasattr(message, "reply_text"):
        ok = await _send_media_caption(message, media_list, text)
        if ok:
            return
    if hasattr(message, "reply_text"):
        try:
            _dm_echo(await message.reply_text(to_markdown_v2(text), parse_mode=ParseMode.MARKDOWN_V2))
            return
        except Exception:
            try:
                _dm_echo(await message.reply_text(text))
                return
            except Exception:
                return
    await safe_reply(target, text)


# ════════════════════════════════════════════════════════════════
# FILTER ENGINE (cú pháp kiểu Rose) ───────────────────────────────
# ════════════════════════════════════════════════════════════════
# Cờ định dạng đặt trong nội dung phản hồi, sẽ bị bóc ra & không hiện
# trong tin nhắn gửi đi.
FILTER_FLAG_NAMES = ("admin", "user", "allow_bot", "protect", "replytag", "nonotif", "mediaspoiler")
_FLAG_RE    = re.compile(r"\{(admin|user|allow_bot|protect|replytag|nonotif|mediaspoiler)\}", re.IGNORECASE)
_PREVIEW_RE = re.compile(r"\{preview(:top)?\}", re.IGNORECASE)
_RULES_RE   = re.compile(r"\{rules(:same)?\}", re.IGNORECASE)
_VAR_RE     = re.compile(r"\{(first|last|fullname|username|mention|id|chatname)\}", re.IGNORECASE)


def parse_filter_flags(text: str):
    """Bóc các cờ {admin}/{user}/{allow_bot}/{protect}/{replytag}/{nonotif}/
    {mediaspoiler}, {preview}/{preview:top}, {rules}/{rules:same} ra khỏi
    `text`. Trả về (text_đã_dọn, flags_dict, preview, rules_btn)."""
    flags = {f: False for f in FILTER_FLAG_NAMES}

    def _flag_sub(m):
        flags[m.group(1).lower()] = True
        return ""
    text = _FLAG_RE.sub(_flag_sub, text)

    preview = None
    pm = _PREVIEW_RE.search(text)
    if pm:
        preview = "top" if pm.group(1) else "normal"
        text = _PREVIEW_RE.sub("", text)

    rules_btn = None
    rm = _RULES_RE.search(text)
    if rm:
        rules_btn = "same" if rm.group(1) else "new"
        text = _RULES_RE.sub("", text)

    return text.strip(), flags, preview, rules_btn


def fill_filter_vars(text: str, user, chat) -> str:
    """Thay {first}/{last}/{fullname}/{username}/{mention}/{id}/{chatname}
    bằng thông tin thật của user/chat hiện tại."""
    def repl(m):
        key = m.group(1).lower()
        if key == "first":
            return user.first_name or ""
        if key == "last":
            return user.last_name or ""
        if key == "fullname":
            return user.full_name
        if key == "username":
            return f"@{user.username}" if user.username else f"[{user.first_name}](tg://user?id={user.id})"
        if key == "mention":
            return f"[{user.first_name}](tg://user?id={user.id})"
        if key == "id":
            return str(user.id)
        if key == "chatname":
            return getattr(chat, "title", None) or getattr(chat, "first_name", None) or ""
        return m.group(0)
    return _VAR_RE.sub(repl, text)


def _split_csv_respecting_quotes(s: str):
    parts, buf, in_q = [], "", False
    for ch in s:
        if ch == '"':
            in_q = not in_q
            continue
        if ch == "," and not in_q:
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf.strip())
    return [p for p in parts if p]


def _parse_single_trigger(raw: str):
    raw = raw.strip().strip('"')
    low = raw.lower()
    if low.startswith("exact:"):
        return (raw[6:].strip().lower(), "exact")
    if low.startswith("prefix:"):
        return (raw[7:].strip().lower(), "prefix")
    return (raw.lower(), "contains")


def parse_filter_spec(raw: str):
    """Phân tích phần sau '/filter '. Hỗ trợ 1 từ khóa đơn, cụm từ trong
    ngoặc kép, hoặc nhiều từ khóa trong dấu ( ... ) phân tách bằng dấu phẩy.
    Trả về (triggers, phần_còn_lại) — triggers là list[(nội_dung, mode)]."""
    raw = raw.strip()
    if not raw:
        return [], ""
    if raw.startswith("("):
        end = raw.find(")")
        if end == -1:
            end = len(raw)
            group, rest = raw[1:end], ""
        else:
            group, rest = raw[1:end], raw[end + 1:]
        triggers = [_parse_single_trigger(p) for p in _split_csv_respecting_quotes(group)]
        return [t for t in triggers if t[0]], rest.strip()
    if raw.startswith('"'):
        end = raw.find('"', 1)
        if end == -1:
            first, _, rest = raw.partition(" ")
            return [_parse_single_trigger(first)], rest.strip()
        content, rest = raw[1:end], raw[end + 1:]
        return [_parse_single_trigger(content)], rest.strip()
    m = re.match(r"(\S+)\s*(.*)$", raw, re.DOTALL)
    if not m:
        return [_parse_single_trigger(raw)], ""
    return [_parse_single_trigger(m.group(1))], m.group(2).strip()


def has_variant_marker(body: str) -> bool:
    """True nếu nội dung có chứa dòng `%%%` — tức người dùng đang muốn THÊM
    1 câu random mới, chứ không phải ghi đè toàn bộ."""
    return bool(body) and bool(re.search(r"(?:^|\n)\s*%%%\s*(?:\n|$)", body))


def split_variants(body: str) -> list[str]:
    """Tách nội dung theo dòng `%%%` thành list các câu trả lời (chuỗi thô,
    chưa kèm media). Dùng nội bộ trước khi gắn media vào câu cuối."""
    return [v.strip() for v in re.split(r"\n?%%%\n?", body) if v.strip()] if body else []


def normalize_media_list(media) -> list[dict]:
    """Chuẩn hoá field `media` của 1 variant về dạng list[dict] luôn — dù dữ
    liệu cũ lưu 1 dict đơn (trước khi hỗ trợ album), list rỗng, hay None."""
    if not media:
        return []
    if isinstance(media, list):
        return [m for m in media if m]
    return [media]  # dict đơn kiểu cũ — bọc thành list 1 phần tử


def normalize_variants(variants) -> list[dict]:
    """Chuẩn hoá list variants (dù cũ — list[str] hoặc media-dict-đơn — hay
    mới — media-list) về dạng list[{"text": str, "media": list[dict]}].
    Tương thích ngược cho note/filter đã lưu trước khi nâng cấp tính năng
    media-riêng-từng-câu và hỗ trợ album."""
    out = []
    for v in variants or []:
        if isinstance(v, dict):
            out.append({"text": v.get("text", ""), "media": normalize_media_list(v.get("media"))})
        elif v:
            out.append({"text": v, "media": []})
    return out


def build_new_variants(body: str, media) -> list[dict]:
    """Tách `body` theo `%%%` thành list[{"text","media"}]. `media` (list các
    ảnh/video — cả album — của lần gõ lệnh hiện tại) chỉ được gắn vào CÂU
    CUỐI CÙNG (câu vừa thêm/sửa trong lần gõ này) — mỗi câu random có thể có
    album ảnh riêng, gắn dần qua từng lần `/note` hoặc `/filter ... %%% câu`
    kèm reply ảnh/album."""
    media = normalize_media_list(media)
    texts = split_variants(body)
    if not texts:
        return [{"text": "", "media": media}] if media else []
    out = [{"text": t, "media": []} for t in texts]
    if media:
        out[-1]["media"] = media
    return out


def merge_variants(old_variants: list, new_variants: list) -> list[dict]:
    """Hợp nhất variants mới vào danh sách cũ — THÊM VÀO (dùng khi nội dung
    có `%%%`). Bỏ trùng theo text (giữ câu cũ — và media của câu cũ — nếu
    trùng text với câu mới không kèm media; nếu câu mới có media riêng thì
    THAY THẾ toàn bộ album của câu đó). Dùng chung cho note (`/note`) và
    filter (`/filter`)."""
    old = normalize_variants(old_variants)
    merged = list(old)
    index_by_text = {v["text"]: i for i, v in enumerate(merged)}
    for v in normalize_variants(new_variants):
        if v["text"] in index_by_text:
            if v.get("media"):
                merged[index_by_text[v["text"]]]["media"] = v["media"]
        else:
            merged.append(v)
            index_by_text[v["text"]] = len(merged) - 1
    return merged


def cleanup_variant_media(old_variants, old_shared_media=None, new_variants=None) -> None:
    """KHÔNG CÒN CẦN LÀM GÌ: media giờ chỉ lưu `file_id` Telegram (không có
    file vật lý trên đĩa nữa — xem `extract_message_media`), nên không có gì
    để dọn khi ghi đè/xoá note/filter. Giữ lại hàm (no-op) để không phải sửa
    mọi nơi đang gọi nó."""
    return


def filter_matches(trigger_content: str, mode: str, text_lower: str) -> bool:
    if mode == "exact":
        return text_lower == trigger_content
    if mode == "prefix":
        return text_lower.startswith(trigger_content)
    # contains — khớp theo "từ/cụm từ" (word-boundary), giống Rose, tránh
    # khớp nhầm vào giữa 1 từ khác (vd "hi" không khớp "history").
    pattern = r"(?:^|\W)" + re.escape(trigger_content) + r"(?:$|\W)"
    return re.search(pattern, text_lower) is not None


def find_matching_filter(flist: dict, text_lower: str):
    """Quét toàn bộ filter trong 1 chat (cả format mới [triggers] và format
    cũ [keyword phẳng]) và trả về entry đầu tiên khớp, hoặc None."""
    for key, v in flist.items():
        if isinstance(v, dict) and "triggers" in v:
            for t in v["triggers"]:
                if filter_matches(t["content"], t["mode"], text_lower):
                    return v
        else:
            if key in text_lower:
                return v
    return None


async def filter_is_allowed(entry, bot, chat_id: int, user) -> bool:
    """Kiểm tra quyền kích hoạt 1 filter (format mới) ứng với người gửi."""
    if not isinstance(entry, dict) or "triggers" not in entry:
        return True  # filter format cũ → ai cũng dùng được như trước
    if getattr(user, "is_bot", False) and not entry.get("allow_bot"):
        return False
    access = entry.get("access", "all")
    if access == "all":
        return True
    is_adm = await is_chat_admin(bot, chat_id, user.id)
    if access == "admin":
        return is_adm
    if access == "user":
        return not is_adm
    return True


async def send_filter_response(update, entry) -> None:
    """Gửi phản hồi cho 1 filter format mới (đã đỗ qua filter_is_allowed),
    áp dụng toàn bộ cờ {protect}/{nonotif}/{mediaspoiler}/{preview}/{rules}/
    {replytag} và biến {first}/{mention}/... đã lưu trong entry.
    `update.message` có thể là None (bài đăng kênh) — dùng effective_message,
    và bỏ qua biến {first}/{mention}/... nếu không có user thật (kênh).
    Mỗi câu random (%%%) có thể kèm media riêng — random chọn 1 variant rồi
    dùng đúng cặp text+media của variant đó, KHÔNG random text/media riêng lẻ."""
    msg  = getattr(update, "message", None) or update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    variants = normalize_variants(entry.get("variants"))
    chosen   = random.choice(variants) if variants else {"text": "", "media": []}
    text     = chosen.get("text", "")
    media    = chosen.get("media") or normalize_media_list(entry.get("media"))  # tương thích filter cũ (media chung)

    # {replytag}: khi filter trả lời vào người mà tin nhắn trigger đang reply
    # tới (xem dưới), các biến {first}/{fullname}/{username}/{mention}/{id}
    # cũng phải lấy thông tin của NGƯỜI ĐÓ — không phải người gõ trigger.
    var_user = user
    if entry.get("replytag") and msg.reply_to_message and msg.reply_to_message.from_user:
        var_user = msg.reply_to_message.from_user
    if text and var_user:
        text = fill_filter_vars(text, var_user, chat)

    reply_markup = None
    if entry.get("rules_btn"):
        bot_uname = CONFIG.get("bot_username", "").lstrip("@")
        if bot_uname:
            url = f"https://t.me/{bot_uname}?start=rules_{chat.id}"
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("📜 Quy tắc", url=url)]])

    # {replytag}: trả lời cho người mà tin nhắn trigger đang reply tới,
    # thay vì trả lời người gõ trigger.
    target_msg = msg
    if entry.get("replytag") and msg.reply_to_message:
        target_msg = msg.reply_to_message

    send_kwargs = {}
    if entry.get("nonotif"):
        send_kwargs["disable_notification"] = True
    if entry.get("protect"):
        send_kwargs["protect_content"] = True
    preview = entry.get("preview")
    if preview == "top":
        send_kwargs["link_preview_options"] = LinkPreviewOptions(is_disabled=False, show_above_text=True)
    elif preview == "normal":
        send_kwargs["link_preview_options"] = LinkPreviewOptions(is_disabled=False)
    else:
        send_kwargs["link_preview_options"] = LinkPreviewOptions(is_disabled=True)
    if reply_markup is not None:
        send_kwargs["reply_markup"] = reply_markup

    if media:
        media_kwargs = dict(send_kwargs)
        media_kwargs.pop("link_preview_options", None)  # không hợp lệ cho reply_photo/video/animation
        media_kwargs["has_spoiler"] = entry.get("mediaspoiler", False)
        ok = await _send_media_caption(target_msg, media, text, **media_kwargs)
        if ok:
            return
        vlog("filter: media fail, fallback text:", entry.get("triggers"))

    if text:
        try:
            await target_msg.reply_text(to_markdown_v2(text), parse_mode=ParseMode.MARKDOWN_V2, **send_kwargs)
        except Exception:
            try:
                send_kwargs.pop("reply_markup", None)
                await target_msg.reply_text(text, **send_kwargs)
            except Exception:
                pass


# ── Notes (giống Rose: %%% random reply, mỗi câu có thể kèm album media riêng) ──
# Note lưu ở 4 dạng (đều đọc được, tự nâng cấp khi ghi lại):
#   • Rất cũ (string thuần)              — 1 câu, không media.
#   • Cũ (dict variants=list[str])       — nhiều câu %%%, 1 media DÙNG CHUNG.
#   • Cũ hơn (variant media=dict đơn)    — mỗi câu 1 media, nhưng chỉ 1 file.
#   • Mới (variant media=list[dict])     — mỗi câu 1 ALBUM media (nhiều file).
# `/note` luôn lưu ở dạng mới nhất; các dạng cũ chỉ còn xuất hiện với note đã
# lưu trước khi nâng cấp tính năng, và vẫn gửi đúng nhờ normalize_note.
def normalize_note(note) -> dict:
    """Chuẩn hoá 1 note (dù cũ hay mới) về dạng dict
    {"variants": list[{"text","media":list}], "media": list_media_chung_cũ}."""
    if isinstance(note, dict):
        return {
            "variants": normalize_variants(note.get("variants")),
            "media":    normalize_media_list(note.get("media")),  # media chung kiểu cũ, fallback
        }
    if note:
        return {"variants": [{"text": note, "media": []}], "media": []}
    return {"variants": [], "media": []}


async def send_note_response(target, note, name: str) -> None:
    """Gửi 1 note (đã chuẩn hoá hoặc thô) ra `target` (Update hoặc Message),
    chọn ngẫu nhiên 1 trong các variants %%% nếu có (kèm đúng album media
    riêng của câu đó, nếu có), hoặc media chung kiểu cũ nếu câu đó không có
    media riêng. Album (≥2 media) gửi bằng send_media_group thật của
    Telegram; 1 media thì gửi như ảnh/video đơn."""
    data = normalize_note(note)
    variants = data["variants"]
    chosen   = random.choice(variants) if variants else {"text": "", "media": []}
    text     = chosen.get("text", "")
    media    = chosen.get("media") or data.get("media") or []
    caption  = f"📝 *#{name}*\n\n{text}" if text else f"📝 *#{name}*"
    message  = getattr(target, "message", None) or getattr(target, "effective_message", None) or target

    if media:
        ok = await _send_media_caption(message, media, caption)
        if ok:
            return
        vlog("note: media fail, fallback text:", name)

    if hasattr(message, "reply_text"):
        try:
            await message.reply_text(to_markdown_v2(caption), parse_mode=ParseMode.MARKDOWN_V2)
        except Exception:
            try:
                await message.reply_text(caption)
            except Exception:
                pass


# ── Media riêng cho câu thoại maid (MAID_PHRASES) ─────────────────
def get_phrase_media(key: str):
    return DB.get("phrase_media", {}).get(key)

def set_phrase_media(message, key: str) -> bool:
    media = extract_message_media(message)
    if not media:
        return False
    DB.setdefault("phrase_media", {})[key] = media
    save_db(DB)
    return True

def del_phrase_media(key: str) -> bool:
    store = DB.get("phrase_media", {})
    if key not in store:
        return False
    del store[key]
    save_db(DB)
    return True


# ── Media riêng cho status "đang suy nghĩ / đang phân tích" của AI ────
def get_thinking_media():
    return DB.get("thinking_media")

def set_thinking_media(message) -> bool:
    media = extract_message_media(message)
    if not media:
        return False
    DB["thinking_media"] = media
    save_db(DB)
    return True

def del_thinking_media() -> bool:
    if not DB.get("thinking_media"):
        return False
    DB["thinking_media"] = None
    save_db(DB)
    return True

async def send_thinking_status(message_or_update, status_text: str):
    """Gửi tin nhắn trạng thái 'đang suy nghĩ/phân tích' của AI. Nếu owner đã
    set media riêng qua /setthinkingmedia thì gửi kèm media đó; ngược lại
    chỉ gửi text thường. Trả về message đã gửi để caller xóa đi sau khi AI
    trả lời xong."""
    message = getattr(message_or_update, "message", None) or message_or_update
    media   = get_thinking_media()
    safe_status = to_markdown_v2(status_text)
    if media and media.get("file_id"):
        sender = _media_sender(message, media.get("type"))
        if sender:
            try:
                sent = await sender(media["file_id"], caption=safe_status, parse_mode=ParseMode.MARKDOWN_V2)
                new_fid = _extract_sent_file_id(sent, media.get("type"))
                if new_fid and new_fid != media["file_id"]:
                    media["file_id"] = new_fid
                    save_db(DB)
                return sent
            except Exception:
                pass  # rớt xuống dùng mặc định bên dưới
    try:
        return await message.reply_text(safe_status, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        return await message.reply_text(status_text)

async def send_thinking_status_to_chat(bot, chat_id: int, status_text: str):
    """Giống `send_thinking_status` nhưng gửi thẳng vào `chat_id` (không cần có 1
    message để reply) — dùng cho minigame, nơi Bot tự lên tiếng trong nhóm mà không
    trả lời tin nhắn nào. ĐỒNG BỘ với /setthinkingmedia: owner đã set media riêng thì
    gửi kèm đúng media đó, chưa set thì chỉ gửi text. Trả về message đã gửi (hoặc
    None nếu gửi thất bại) để caller xoá đi sau khi AI trả lời xong."""
    media       = get_thinking_media()
    safe_status = to_markdown_v2(status_text)
    if media and media.get("file_id"):
        mtype = media.get("type")
        sender = {
            "photo":     getattr(bot, "send_photo", None),
            "video":     getattr(bot, "send_video", None),
            "animation": getattr(bot, "send_animation", None),
        }.get(mtype)
        if sender:
            try:
                sent = await sender(chat_id, media["file_id"], caption=safe_status,
                                    parse_mode=ParseMode.MARKDOWN_V2)
                new_fid = _extract_sent_file_id(sent, mtype)
                if new_fid and new_fid != media["file_id"]:
                    media["file_id"] = new_fid
                    save_db(DB)
                return sent
            except Exception:
                pass  # rớt xuống gửi text thường bên dưới
    try:
        return await bot.send_message(chat_id, safe_status, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception:
        try:
            return await bot.send_message(chat_id, status_text)
        except Exception:
            return None

def extract_msg_id_from_link(raw: str) -> int | None:
    """Trích message_id từ link Telegram hoặc số nguyên."""
    raw = raw.strip()
    # Link dạng t.me/c/chatid/msgid hoặc t.me/username/msgid
    m = re.search(r"/(\d+)\s*$", raw)
    if m:
        return int(m.group(1))
    if raw.isdigit():
        return int(raw)
    return None

def fmt_duration(total_seconds: float) -> str:
    """Định dạng số giây thành chuỗi dễ đọc kiểu '2 ngày 3 giờ 12 phút' (dùng cho
    /stats, /version — hiển thị thời gian tiến trình đã chạy)."""
    total_seconds = int(max(0, total_seconds))
    days,    rem = divmod(total_seconds, 86400)
    hours,   rem = divmod(rem, 3600)
    minutes, _   = divmod(rem, 60)
    parts = []
    if days:    parts.append(f"{days} ngày")
    if hours:   parts.append(f"{hours} giờ")
    if minutes or not parts: parts.append(f"{minutes} phút")
    return " ".join(parts)


def fmt_bytes(n: float) -> str:
    """Định dạng số byte thành chuỗi dễ đọc (KB/MB/GB) — dùng cho /stats."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def get_process_memory_mb() -> float:
    """RAM tiến trình bot đang dùng (MB) — qua resource.getrusage (Python
    stdlib, không cần cài thêm psutil). ru_maxrss đơn vị KB trên Linux
    (Render chạy Linux), byte trên macOS — tự nhận diện qua platform."""
    try:
        import resource, sys as _sys
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return usage / 1024 if _sys.platform == "linux" else usage / (1024 * 1024)
    except Exception:
        return 0.0


def get_db_size_bytes() -> int:
    """Kích thước DB hiện tại (bytes) — tính trực tiếp từ JSON serialize của
    DB trong RAM (không phụ thuộc file đĩa local, vốn có thể đã mất nếu host
    không có persistent disk)."""
    try:
        import json
        return len(json.dumps(DB, ensure_ascii=False).encode("utf-8"))
    except Exception:
        return 0

# ── Action keyboard ───────────────────────────────────────────────
def _action_kb(msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Trả lời", callback_data=f"mr:{msg_id}"),
        InlineKeyboardButton("🗑 Xóa",     callback_data=f"md:{msg_id}"),
        InlineKeyboardButton("✏️ Sửa",     callback_data=f"me:{msg_id}"),
    ]])

def strip_bot_mention(text: str, bot_username: str) -> str:
    return re.sub(
        rf"^@{re.escape(bot_username)}\s*", "", text, flags=re.IGNORECASE
    ).strip()

# Các từ gọi bot thay thế cho @username (không cần dấu @)
# Dynamic — đọc trigger từ persona đang chọn trong CONFIG thay vì hardcode.
def _get_alt_triggers() -> list[str]:
    """Trả về list trigger tương ứng persona đang chọn.
    Ưu tiên: field 'trigger' trong AI_PERSONA_PRESETS[persona_key].
    Fallback về ["megumin"] nếu không tìm được."""
    try:
        from ai_module import get_all_personas
        persona_key = CONFIG.get("ai_persona", "")
        preset = get_all_personas().get(persona_key, {})
        trigger = preset.get("trigger", "")
        if trigger:
            # Loại bỏ trùng lặp, giữ thứ tự
            seen: set = set()
            result = []
            for t in ([trigger] if isinstance(trigger, str) else list(trigger)):
                t = t.strip().lower()
                if t and t not in seen:
                    seen.add(t)
                    result.append(t)
            return result if result else ["megumin"]
    except Exception:
        pass
    return ["megumin"]

def match_bot_trigger(text: str, bot_username: str) -> tuple[bool, str]:
    """Kiểm tra tin nhắn có đang 'gọi' bot không — qua @bot_username hoặc một
    trong các từ trigger thay thế (vd: 'megumin'). Trả về (matched, remainder),
    với remainder là phần text còn lại sau khi bỏ phần gọi tên."""
    if bot_username:
        m = re.match(rf"^@{re.escape(bot_username)}\b[\s,:]*", text, flags=re.IGNORECASE)
        if m:
            return True, text[m.end():].strip()
    for trig in _get_alt_triggers():
        m = re.match(rf"^{re.escape(trig)}\b[\s,:]*", text, flags=re.IGNORECASE)
        if m:
            return True, text[m.end():].strip()
    return False, ""

# ── Maid phrases ──────────────────────────────────────────────────
MAID_PHRASES = {
    "welcome": (
        "🌸🌸🌸 *CHÀO MỪNG NGÀI TRỞ LẠI* 🌸🌸🌸\n\n"
        "✨ Em là *Megu - Maid Bot* ✨\n"
        "💝 Luôn sẵn sàng phục vụ ngài 24/7!\n\n"
        "📖 Gõ `/help` để xem sách lệnh chi tiết ạ~\n"
        "🎀 Chúc ngài một ngày tốt lành!"
    ),
    "ok":            "✨ *Vâng ạ!* Em xử lý ngay đây ạ~",
    "done":          "✅ *Hoàn tất rồi ạ!* Ngài hài lòng chứ ạ~ 💕",
    "error":         "😭 *Ối không!* Có lỗi xảy ra rồi ạ... Ngài thử lại nhé?",
    "no_target":     "🎯 *Thưa ngài,* em không biết nhắm vào ai cả... Vui lòng chọn mục tiêu ạ~",
    "not_group":     "🏠 *Lệnh này* chỉ dùng trong nhóm thôi ạ! Ngài vào nhóm đi ạ~",
    "warn_add":      "⚠️ *Đã cảnh cáo thành công!* ⚠️\nHãy cẩn thận kẻo bị ban đó ạ~",
    "ban_done":      "🔨 *Đã ban người dùng!* 🔨\nTiễn về nơi xa lắm rồi ạ~ 👋",
    "kick_done":     "👢 *Đã đá ra khỏi nhóm!* 👢\nHẹn gặp lại... ủa mà thôi khỏi gặp ạ~",
    "mute_done":     "🔇 *Đã khóa mồm thành công!* 🔇\nYên lặng nào các bé ơi~",
    "unmute_done":   "🔊 *Mở khóa mồm rồi ạ!* 🔊\nGiờ thì nói chuyện được rồi nhé~",
    "pin_done":      "📌 *Đã ghim tin nhắn!* 📌\nTin quan trọng được lưu lại rồi ạ~",
    "unpin_done":    "📍 *Đã gỡ ghim!* 📍\nHết thời gian trưng bày rồi ạ~",
    "proxy_on":      (
        "📡 *BẬT CHẾ ĐỘ RELAY* 📡\n\n"
        "Em sẽ chuyển tiếp tin nhắn từ ngài sang nhóm đích ạ!\n"
        "💬 Cứ nhắn gì em cũng chuyển hết~"
    ),
    "proxy_off":     "📴 *TẮT CHẾ ĐỘ RELAY* 📴\n\nEm không chuyển tiếp nữa ạ. Nếu cần thì /proxy lại nhé~",
    "monitor_on":    (
        "👁️ *BẬT CHẾ ĐỘ GIÁM SÁT* 👁️\n\n"
        "Em sẽ canh chừng nhóm và báo cáo cho ngài ạ!\n"
        "🛡️ Không gì thoát khỏi mắt em được đâu~"
    ),
    "monitor_off":   "😴 *TẮT CHẾ ĐỘ GIÁM SÁT* 😴\n\nEm nghỉ ngơi chút nhé. Bật lại khi cần ạ~",
    "blocked":       "🛡️ *ĐÃ CHẶN NGƯỜI DÙNG* 🛡️\nHọ sẽ không thể liên lạc với em nữa ạ~",
    "rules_set":     "📜 *NỘI QUY ĐÃ ĐƯỢC LƯU* 📜\nThành viên nào vi phạm sẽ bị xử lý ạ!",
    "note_saved":    "🗒️ *GHI CHÚ ĐÃ LƯU* 🗒️\nCần nhắc lại thì gõ `#tên` là có ngay ạ~",
    "filter_saved":  "🔍 *BỘ LỌC ĐÃ KÍCH HOẠT* 🔍\nEm sẽ tự động trả lời khi thấy từ khóa ạ!",
    "promote_full":  "👑 *ĐÃ NÂNG NGÀI LÊN ADMIN THÀNH CÔNG* 👑\nGiờ ngài có thể làm mọi thứ mà ngài muốn rồi~",
    "promote_basic": "⭐ *PHONG CHỨC ADMIN CƠ BẢN* ⭐\nCó quyền quản lý cơ bản nhưng không phá được đâu ạ~",
    "demote_done":   "📉 *THU HỒI QUYỀN ADMIN* 📉\nQuay lại làm công dân bình thường thôi ạ~",
    "reply_done":    "💬 *Đã reply vào tin nhắn!*\nTin nhắn của ngài đã đến đúng người nhận ạ~",
    "delete_done":   "🗑️ *Đã xóa tin nhắn!* 🗑️\nBay màu vĩnh viễn rồi ạ~",
    "edit_done":     "✏️ *Đã sửa tin nhắn!* ✏️\nNội dung mới đã được cập nhật ạ~",
    "relay_sent":    "📨 *Đã chuyển tiếp vào nhóm!*\n📩 Msg ID: `{msg_id}`\nNgài cần gì nữa không ạ~",
    "bot_sleeping":  "😴 *Em đang nghỉ ngơi...* 😴\nGõ `/proxy` hoặc `/monitor` để đánh thức em dậy làm việc nhé ạ~",
    "no_group_set":  "🏚️ *Chưa có nhóm đích!* 🏚️\nDùng `/setgroup <group_id>` để em biết đường mà làm việc ạ~",
    "media_unsupported": "📎 *Xin lỗi ngài,* em chưa hỗ trợ loại media này ạ!\nCó thể gửi ảnh, video, sticker, file đều được nha~",
    "lock_done":     "🔒 *ĐÃ KHÓA NHÓM* 🔒\nChỉ admin mới nhắn được thôi ạ, thành viên thường tạm yên lặng nhé~",
    "unlock_done":   "🔓 *ĐÃ MỞ KHÓA NHÓM* 🔓\nMọi người nhắn chuyện lại được rồi ạ~",
    # cancel
    "cancel_done":   "✅ *Đã hủy thao tác đang chờ rồi ạ~* Ngài cứ tiếp tục bình thường nhé!",
    "cancel_none":   "ℹ️ *Không có thao tác nào đang chờ cả ạ~*",
}

def maid(key: str, **kwargs) -> str:
    text = MAID_PHRASES.get(key, "✨ Vâng ạ, em đã thực hiện xong rồi~")
    return text.format(**kwargs) if kwargs else text

async def maid_reply(target, key: str, extra: str = "", **kwargs) -> None:
    """Gửi câu thoại maid theo `key` (tự kèm ảnh/video/gif nếu owner đã set
    qua /setmedia <key>). `extra` là phần text nối thêm phía dưới (vd tên
    người bị ban, lý do...). `target` có thể là Update hoặc 1 Message."""
    text = maid(key, **kwargs)
    if extra:
        text = f"{text}\n{extra}"
    await send_styled(target, text, get_phrase_media(key))


# ── HELP — Tổng quan gọn + chi tiết riêng từng nhóm (bấm nút mở) ───
# Mỗi section: emoji, tiêu đề, 1 dòng đại diện hiện trong bảng tổng quan,
# và "body" là nội dung CHI TIẾT đầy đủ — hiện khi bấm nút tương ứng.
HELP_OVERVIEW_HEADER = "*🌸 SÁCH LỆNH BOT 🌸*\n_Bấm vào nút bên dưới để xem hướng dẫn chi tiết từng nhóm lệnh ạ~_\n"
HELP_OVERVIEW_FOOTER = (
    "\n📌 *Quy ước chung:*\n"
    "• `[t]` = thời gian: `30m` `1h` `6h` `2d`\n"
    "• _(reply)_ = reply vào tin cần xử lý rồi gõ lệnh\n"
    "• Lệnh có dấu `*` trong hướng dẫn chi tiết = bắt buộc owner mới dùng được"
)

HELP_SECTIONS = {
    "basic": {
        "emoji": "🔹", "title": "CƠ BẢN",
        "overview": "/start, /help, /id, /stats, /info, /version",
        "body": (
            "*🔹 LỆNH CƠ BẢN*\n\n"
            "• `/start` — Khởi động bot, hiện lời chào\n"
            "• `/help` — Mở lại bảng lệnh tổng quan này\n"
            "• `/id` — Xem ID của ngài / ID nhóm hiện tại\n"
            "• `/stats` — Thống kê hoạt động của bot\n"
            "• `/version` — Phiên bản bot & thời gian uptime\n"
            "• `/info` _(reply vào ai đó, hoặc để trống = chính ngài)_\n"
            "  → Xem thông tin chi tiết 1 thành viên (ID, username, quyền...)\n\n"
            "_Các lệnh này chỉ owner dùng được._"
        ),
    },
    "notes": {
        "emoji": "📝", "title": "GHI CHÚ (NOTE)",
        "overview": "/note, /get, /notes, /delnote",
        "body": (
            "*📝 GHI CHÚ (NOTE)*\n"
            "_Lưu sẵn nội dung, gọi lại bằng `/get #tên` bất cứ lúc nào._\n\n"
            "• `/note #tên nội_dung` — Tạo/ghi đè ghi chú\n"
            "  💡 Reply vào ảnh/video/gif rồi gõ lệnh → gắn media cho note\n"
            "• `/get #tên` — Lấy nội dung ghi chú ra\n"
            "• `/notes` — Xem danh sách toàn bộ ghi chú đang có\n"
            "• `/delnote #tên` — Xóa hẳn 1 ghi chú\n\n"
            "*🎲 Nhiều câu trả lời ngẫu nhiên (random):*\n"
            "• Gõ lại `/note #tên` *không* kèm `%%%` → *ghi đè* toàn bộ nội "
            "dung cũ (nếu đang có ≥2 câu random, bot sẽ hỏi xác nhận trước khi xoá)\n"
            "• Thêm dòng `%%%` vào nội dung → *thêm 1 câu random mới*, giữ "
            "nguyên các câu cũ. Mỗi lần gọi `/get`, bot chọn ngẫu nhiên 1 câu\n"
            "• Mỗi câu random có thể kèm ảnh/video riêng — reply ảnh đúng lúc "
            "thêm câu đó (không ảnh hưởng câu khác)\n\n"
            "*🔗 Định dạng nội dung:*\n"
            "• Chèn link: `[chữ hiển thị](https://example.com)`\n"
            "• Biến cá nhân hoá: `{first}` `{last}` `{fullname}` `{username}` "
            "`{mention}` `{id}` `{chatname}`\n\n"
            "_Ví dụ:_\n"
            "`/note #chao Xin chào {first}!`\n"
            "`/note #chao\\n%%%\\nChào bạn ghé chơi nè!`"
        ),
    },
    "filters": {
        "emoji": "🔍", "title": "BỘ LỌC (FILTER)",
        "overview": "/filter, /stop, /filters, /fn",
        "body": (
            "*🔍 BỘ LỌC TỰ ĐỘNG TRẢ LỜI (FILTER)*\n"
            "_Bot tự nhắn khi ai đó gõ trúng từ khóa, không cần gọi lệnh._\n\n"
            "• `/filter từ_khóa nội_dung` — Tạo bộ lọc\n"
            "  💡 Reply vào ảnh/video/gif rồi gõ lệnh → gắn media cho filter\n"
            "• `/stop từ_khóa` — Xóa 1 bộ lọc\n"
            "• `/filters` — Xem danh sách gọn (bấm nút xem chi tiết từng filter)\n"
            "• `/fn on` / `/fn off` — Bật/tắt cho *mọi thành viên* trong nhóm "
            "trigger được filter/note (mặc định *tắt*, chỉ owner trigger được). "
            "Không ảnh hưởng quyền tạo/sửa/xoá — luôn chỉ owner làm được\n\n"
            "*🎯 Kiểu khớp trigger:*\n"
            "• Mặc định (`contains`) — từ khóa xuất hiện ở đâu trong câu cũng nổ\n"
            "• `\"exact:từ\"` — câu gõ phải *giống y hệt*, không thêm không thiếu\n"
            "• `\"prefix:từ\"` — câu phải *bắt đầu bằng* từ khóa đó\n"
            "• Nhiều trigger 1 lần: `/filter (hi, hey, \"exact:yo\") Hello!`\n\n"
            "*🎲 Nhiều câu trả lời ngẫu nhiên (random):* — y hệt note\n"
            "• Không `%%%` → ghi đè toàn bộ (hỏi xác nhận nếu đang có ≥2 câu)\n"
            "• Có `%%%` → thêm 1 câu random mới, mỗi câu có thể kèm ảnh riêng\n\n"
            "*🚩 Cờ định dạng* _(thêm vào cuối nội dung, không hiện ra khi gửi)_:\n"
            "• `{admin}` — chỉ admin nhóm mới kích hoạt được\n"
            "• `{user}` — chỉ thành viên thường mới kích hoạt được\n"
            "• `{allow_bot}` — cho phép tin nhắn từ bot khác cũng trigger\n"
            "• `{protect}` — bật nội dung được bảo vệ (chống forward/save)\n"
            "• `{replytag}` — trả lời vào tin mà người gõ trigger đang reply tới\n"
            "• `{nonotif}` — gửi tin im lặng, không kêu thông báo\n"
            "• `{mediaspoiler}` — che ảnh/video dạng spoiler\n"
            "• `{preview}` / `{preview:top}` — bật hiện preview link\n"
            "• `{rules}` — thêm nút bấm mở quy tắc nhóm\n\n"
            "*🔗 Biến & link:*\n"
            "• `{first}` `{last}` `{fullname}` `{username}` `{mention}` `{id}` `{chatname}`\n"
            "• Chèn link: `[chữ hiển thị](https://example.com)`\n\n"
            "_Ví dụ:_\n"
            "`/filter \"exact:rule\" Chào {first}! {admin}{rules}{nonotif}`"
        ),
    },
    "moderation": {
        "emoji": "🛡️", "title": "QUẢN LÝ NHÓM",
        "overview": "/warn, /mute, /kick, /ban, /pin, /del, /lock...",
        "body": (
            "*🛡️ QUẢN LÝ NHÓM*\n\n"
            "*Cảnh cáo:*\n"
            "• `/warn @user [lý do]` — Cảnh cáo (đủ 3 lần → tự động ban)\n"
            "• `/unwarn @user` — Xóa bớt 1 cảnh cáo\n"
            "• `/warns @user` — Xem số cảnh cáo hiện tại\n\n"
            "*Hạn chế / loại bỏ thành viên:*\n"
            "• `/mute @user [t]` — Cấm chat (vd `/mute @user 1h`, để trống = vĩnh viễn)\n"
            "• `/unmute @user` — Bỏ cấm chat\n"
            "• `/kick @user` — Đá khỏi nhóm (vào lại được)\n"
            "• `/ban @user` — Cấm vĩnh viễn (không vào lại được)\n"
            "• `/unban @user` — Bỏ cấm vĩnh viễn\n\n"
            "*Tin nhắn:*\n"
            "• `/pin` _(reply)_ — Ghim tin nhắn\n"
            "• `/unpin` _(reply)_ — Gỡ ghim\n"
            "• `/del` hoặc `/xoa` _(reply)_ — Xóa 1 tin nhắn\n"
            "• `/purge` _(reply)_ — Xóa hàng loạt từ tin đó trở đi\n\n"
            "*Khóa nhóm:*\n"
            "• `/lock` — Khóa nhóm, chỉ admin nhắn được\n"
            "• `/unlock` — Mở khóa lại bình thường\n\n"
            "*Nội quy:*\n"
            "• `/setrules nội_dung` — Đặt/cập nhật nội quy nhóm\n"
            "• `/rules` — Xem nội quy hiện tại\n\n"
            "_`[t]` = thời gian: `30m` `1h` `6h` `2d`_"
        ),
    },
    "media": {
        "emoji": "🎞️", "title": "MEDIA CHO CÂU THOẠI",
        "overview": "/setmedia, /listmedia, /setthinkingmedia",
        "body": (
            "*🎞️ MEDIA CHO CÂU THOẠI BOT*\n"
            "_Gắn ảnh/video/gif cho các câu thoại có sẵn của bot (vd: khi ban "
            "xong, kick xong...) hoặc cho trạng thái \"đang suy nghĩ\" của AI._\n\n"
            "• `/setmedia key` _(reply ảnh/video/gif)_ — Gắn media cho 1 câu thoại\n"
            "• `/delmedia key` — Gỡ media của câu thoại đó\n"
            "• `/listmedia` — Xem danh sách key + media đã gắn\n\n"
            "*Trạng thái AI \"đang suy nghĩ\":*\n"
            "• `/setthinkingmedia` _(reply ảnh/video/gif)_ — Đặt media riêng\n"
            "• `/delthinkingmedia` — Gỡ về mặc định\n\n"
            "_Gõ `/listmedia` để biết hết các `key` câu thoại đang hỗ trợ gắn media._"
        ),
    },
    "monitor": {
        "emoji": "👁️", "title": "GIÁM SÁT & TÓM TẮT",
        "overview": "/monitor, /tomtat, /feed, /proxy",
        "body": (
            "*👁️ GIÁM SÁT NHÓM*\n\n"
            "• `/monitor` — Bật/tắt giám sát tin nhắn trong nhóm\n"
            "• `/proxy` — Bật/tắt relay tin nhắn DM ↔ nhóm\n"
            "• `/groupstatus` — Xem trạng thái giám sát hiện tại\n\n"
            "*Buffer tin nhắn đã giám sát:*\n"
            "• `/feed [n]` — Xem `n` tin gần nhất _(mặc định 1 số lượng cố định)_\n"
            "• `/search từ_khóa` — Tìm tin trong buffer đã lưu\n"
            "• `/clearfeed` — Xóa sạch buffer trong RAM\n\n"
            "*Từ khóa cần theo dõi riêng:*\n"
            "• `/addkw từ_khóa` — Thêm từ khóa cần lọc/cảnh báo\n"
            "• `/delkw từ_khóa` — Xóa từ khóa khỏi danh sách\n"
            "• `/listkw` — Xem toàn bộ từ khóa đang theo dõi\n\n"
            "*📋 Tóm tắt bằng AI:*\n"
            "• `/tomtat` — Tóm tắt 50 tin gần nhất\n"
            "• `/tomtat 100` — Tóm tắt N tin gần nhất (thay 100 bằng số bất kỳ)\n"
            "• `/tomtat ngay` — Tóm tắt toàn bộ tin trong ngày\n"
            "• `/tomtat moi` — Tóm tắt tin mới kể từ lần `/tomtat` gần nhất "
            "_(tính chung mọi mode)_"
        ),
    },
    "database": {
        "emoji": "🗄️", "title": "CƠ SỞ DỮ LIỆU",
        "overview": "/dbstats, /cleandb, /backup, /restore",
        "body": (
            "*🗄️ CƠ SỞ DỮ LIỆU*\n\n"
            "• `/dbstats` — Xem dung lượng & số tin nhắn đang lưu trong DB\n"
            "• `/setbufferlimit N` — Đặt giới hạn N tin nhắn lưu trong buffer\n"
            "• `/cleandb` — Xem hướng dẫn dọn dẹp DB\n"
            "• `/cleandb N` — Xóa tin nhắn cũ hơn N ngày\n"
            "• `/cleandb all` — Xóa toàn bộ buffer tin nhắn\n\n"
            "*💾 Sao lưu / khôi phục* _(gửi qua DM owner)_:\n"
            "• `/backup` — Xuất toàn bộ dữ liệu ra file JSON, gửi về DM owner\n"
            "• `/restore` _(reply vào file backup)_ — Nạp lại dữ liệu ngay lập tức\n\n"
            "_⚠️ `/restore` ghi đè toàn bộ dữ liệu hiện tại — chỉ dùng khi chắc chắn._"
        ),
    },
    "permissions": {
        "emoji": "👑", "title": "PHÂN QUYỀN & OWNER",
        "overview": "/adm, /apromote, /deladm, /setgroup, /broadcast",
        "body": (
            "*👑 PHÂN QUYỀN ADMIN*\n\n"
            "• `/adm @user` — Phong admin đầy đủ quyền\n"
            "• `/apromote @user` — Phong admin cơ bản (quyền giới hạn)\n"
            "• `/deladm @user` — Thu hồi quyền admin\n"
            "• `/the @user chức_danh` — Đặt chức danh tùy chỉnh cho admin\n\n"
            "*⚙️ Cấu hình & quản trị bot (chỉ owner):*\n"
            "• `/setgroup id` — Đặt nhóm đích cho bot hoạt động\n"
            "• `/block id` — Chặn 1 user nhắn DM cho bot\n"
            "• `/unblock id` — Bỏ chặn\n"
            "• `/listusers` — Danh sách user đã từng dùng bot\n"
            "• `/broadcast nội_dung` — Gửi thông báo hàng loạt tới mọi user\n"
            "• `/deldm` _(reply)_ — Xóa 1 tin DM bot đã gửi\n"
            "• `/editdm id nội_dung` — Sửa nội dung 1 tin DM đã gửi\n"
            "• `/cancel` — Hủy thao tác reply/edit đang chờ dở dang\n\n"
            "*🔐 Mã hóa cấu hình:*\n"
            "• `/encstatus` — Xem trạng thái mã hóa config hiện tại\n"
            "• `/encrekey` — Tạo lại encryption key mới"
        ),
    },
    "ai": {
        "emoji": "🤖", "title": "AI GEMINI",
        "overview": "/ai, /model, /tinhcach, /aikey",
        "body": (
            "*🤖 TRỢ LÝ AI (GEMINI)*\n\n"
            "• `/ai` — Bật/tắt AI trong nhóm\n"
            "• `💬 Chat thường với bot` → AI tự động tìm kiếm Google, tin tức, "
            "Facebook khi cần để trả lời chính xác hơn\n\n"
            "*Model & nhân vật:*\n"
            "• `/model` — Xem danh sách & chọn model AI\n"
            "• `/aimodel tên_model` — Đổi model trực tiếp\n"
            "• `/tinhcach` — Chọn nhân vật/persona cho AI\n"
            "• `/aisys nội_dung` — Đặt system prompt tùy chỉnh riêng\n"
            "• `/aiclear` — Xóa lịch sử hội thoại AI hiện tại\n"
            "• `/aithinking low|medium|high` — Mức độ suy nghĩ (thinking level), "
            "chỉ có tác dụng với model dòng Gemini 3.x; model cũ tự bỏ qua, không lỗi. "
            "Áp dụng đồng bộ cho chat thường, agent mode và Search Grounding\n\n"
            "*🎭 Quản lý nhân vật AI:*\n"
            "• `/aipersonanew key | Tên | [emoji] | Prompt` — Tạo nhân vật mới hoàn toàn "
            "(emoji không bắt buộc, mặc định 🎭)\n"
            "• `/aipersonadel key` — Xóa nhân vật tự tạo (không xóa được nhân vật gốc)\n"
            "• `/aipersonaedit key | Prompt mới` — Sửa prompt của 1 nhân vật bất kỳ "
            "(cả nhân vật gốc — bản gốc trong code không mất, chỉ override riêng)\n"
            "• `/aipersonaedit key | trigger: từ1, từ2` — Đặt từ gọi bot riêng cho nhân vật "
            "(gõ từ này trong nhóm là gọi được bot, không cần tag @bot)\n"
            "• `/aimemory key | nội_dung` — Thêm 1 dòng ghi nhớ lâu dài vào nhân vật "
            "(nối vào cuối prompt mỗi lượt chat, không ghi đè prompt gốc)\n"
            "• `/aimemory key` — Xem ghi nhớ hiện tại của nhân vật\n"
            "• `/aimemoryclear key` — Xóa hết ghi nhớ của nhân vật\n\n"
            "*API key:*\n"
            "• `/aikey key` — Đặt API key chính\n"
            "• `/aiaddkey key` — Thêm API key phụ (dự phòng khi hết quota)\n"
            "• `/aikeys` — Xem toàn bộ key đang có\n"
            "• `/aidelkey n` — Xóa key phụ thứ n\n\n"
            "*🧠 Bối cảnh & lịch sử nhóm:*\n"
            "• Trong nhóm, AI tự đọc các tin gần nhất (kể cả tin của bot/AI khác như Mira) "
            "nhưng chỉ nói về chủ đề đang chat khi ngài hỏi tới\n"
            "• Hỏi tự nhiên để tra tin cũ: \"hôm qua ai nói gì về X\", \"A nhắn gì lúc sáng\"\n"
            "• `/aictx` — Xem cấu hình · `/aictx 40` — số tin bối cảnh (0 = tắt)\n"
            "• `/aictx chars 8000` — trần ký tự · `/aictx log on|off` — ghi lịch sử nhóm\n"
            "• `/aictx clear` — Xoá lịch sử đã lưu của nhóm\n\n"
            "*🔍 Tìm kiếm:*\n"
            "• `/searchdebug` — Bật/tắt log debug quá trình tìm kiếm\n"
            "• `/aigrounding` — Bật/tắt Google Search Grounding gốc của Gemini. "
            "Khi bật (mặc định): câu hỏi cần tra web/tin tức dùng tool tìm kiếm chính chủ "
            "Gemini, tự kèm nguồn thật, không scrape DDG/Bing nữa. Facebook và YouTube vẫn "
            "giữ scrape thật như cũ (Grounding không trả đúng link video cụ thể). "
            "⚠️ Tính phí riêng theo lượt ngoài hạn mức free — khác hoàn toàn scrape cũ"
        ),
    },
    "owners": {
        "emoji": "👑", "title": "OWNER",
        "overview": "/addowner, /delowner, /owners",
        "body": (
            "*👑 QUẢN LÝ OWNER*\n\n"
            "• `/addowner id` _(hoặc `@username`, hoặc reply)_ — Thêm owner phụ bằng ID Telegram\n"
            "• `/delowner id` — Xoá owner phụ\n"
            "• `/owners` — Xem danh sách owner\n\n"
            "_Owner phụ dùng bot đầy đủ như owner chính, chỉ không thêm/xoá được owner. "
            "Chỉ owner chính (biến `OWNER_ID`) mới dùng được `/addowner`, `/delowner`. "
            "Có thể khai báo sẵn bằng env `EXTRA_OWNER_IDS=id1,id2`._"
        ),
    },
    "games": {
        "emoji": "🎲", "title": "MINIGAME BÀI",
        "overview": "/game, /masoi, /endgame",
        "body": (
            "*🎲 MINIGAME BÀI (BLACKJACK / TIẾN LÊN / UNO)*\n\n"
            "• `/game` — Mở menu, mỗi game 1 nút (Blackjack / Tiến Lên / UNO); "
            "bấm vào game rồi chọn *Chơi đơn* (1v1 với Bot) hoặc *Phòng* nhiều người\n"
            "• `/masoi` — Mở phòng *Ma Sói* 🐺 \n"
            "• `/endgame` — Huỷ phòng/ván đang chạy trong chat này (cả Ma Sói)\n\n"
            "*🐺 Ma Sói:* ~20 vai (Tiên Tri, Phù Thuỷ, Thợ Săn, Thần Tình Yêu, Sói Nằm Vùng, Đao Phủ, Kẻ Ngốc...), "
            "chế độ Cổ điển/Cân bằng/Drama. Mỗi sáng có *Vòng Mô Tả — Tìm Kẻ Giả Mạo*: bot nhắn riêng từ khoá "
            "(kho ~1.600 từ), mô tả trong DM, ai lệch số đông bị nghi. Thêm thư nặc danh, vote công khai/kín, "
            "chế độ câm lặng (bot cần quyền xoá tin nhắn).\n\n"
            "*Phòng nhiều người (bài):*\n"
            "• Chủ phòng bấm ➕ để mời, người khác bấm *Tham gia* — cần đã "
            "từng `/start` với bot ở tin nhắn riêng (để bot chia bài kín được)\n"
            "• Tối đa 5 người (Blackjack) / 4 người (Tiến Lên — đúng luật 13 lá/người)\n"
            "• Bài được chia RIÊNG qua tin nhắn riêng, chỉ khi đánh ra mới "
            "hiện trong nhóm — không lộ bài cho người khác\n\n"
            "*Chơi với Bot:* Bot ra bài theo thuật toán (đúng luật, không tự "
            "chế bừa). Lời thoại của Bot do AI sinh ra theo ĐÚNG nhân vật đang chọn ở "
            "`/tinhcach` — không có câu soạn sẵn: chưa có API key hoặc AI lỗi thì Bot "
            "im lặng, chỉ chơi bài. Trong lúc nghĩ lời thoại, Bot hiện status \"đang "
            "suy nghĩ\" kèm media của `/setthinkingmedia` (nếu có). Nhắn tin thường "
            "trong lúc chơi = chat phiếm với Bot, không ảnh hưởng ván đang chơi.\n\n"
            "_Lưu ý: Tiến Lên dùng luật đầy đủ miền Nam (sảnh, tứ quý, 3/4 đôi "
            "thông chặt heo, sảnh rồng tới trắng)._"
        ),
    },
}

# Thứ tự hiện trong bảng tổng quan
HELP_SECTION_ORDER = [
    "basic", "notes", "filters", "moderation",
    "media", "monitor", "database", "permissions", "ai", "owners", "games",
]


def build_help_overview() -> str:
    """Build bảng tổng quan: mỗi nhóm CHỈ 1 dòng đại diện, không liệt kê hết
    lệnh — bấm nút bên dưới để xem hướng dẫn đầy đủ từng nhóm. Trả về
    MarkdownV2 đã sẵn sàng gửi (escape an toàn)."""
    lines = [HELP_OVERVIEW_HEADER]
    for key in HELP_SECTION_ORDER:
        sec = HELP_SECTIONS[key]
        lines.append(f"{sec['emoji']} *{sec['title']}*\n   `{sec['overview']}`")
    lines.append(HELP_OVERVIEW_FOOTER)
    return to_markdown_v2("\n\n".join(lines))


def build_help_keyboard():
    """Inline keyboard 2 nút/dòng, 1 nút cho mỗi nhóm help."""
    buttons = [
        InlineKeyboardButton(f"{HELP_SECTIONS[k]['emoji']} {HELP_SECTIONS[k]['title']}", callback_data=f"help:{k}")
        for k in HELP_SECTION_ORDER
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


def build_help_detail_keyboard():
    """Nút quay lại bảng tổng quan, hiện ở cuối mỗi trang chi tiết."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Quay lại tổng quan", callback_data="help:home")]])


# Giữ HELP_TEXT (string đơn) cho tương thích ngược nếu nơi khác còn import —
# trỏ thẳng sang bảng tổng quan mới.
HELP_TEXT = build_help_overview()
