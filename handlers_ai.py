"""
handlers_ai.py — Lệnh AI Gemini, /searchdebug, /tomtat.
"""

import asyncio
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import CONFIG, DB, save_config, now_vn, VN_TZ
from utils import require_owner, safe_reply, MSG_BUFFER, to_markdown_v2
from ai_module import (
    GEMINI_AVAILABLE, AI_HISTORY, AI_PERSONA_PRESETS, GEMINI_MODELS_FREE,
    _get_all_ai_keys, _gemini_chat, _gemini_get_sys_prompt, _gemini_chat_vision,
    _validate_api_key,
    get_all_personas, is_builtin_persona, create_custom_persona,
    delete_custom_persona, edit_persona_prompt, add_persona_memory, clear_persona_memory,
    set_persona_triggers,
)

try:
    from google import genai
except ImportError:
    genai = None

# ════════════════════════════════════════════════════════════════
# AI TOGGLE & CONFIG
# ════════════════════════════════════════════════════════════════

async def cmd_ai_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not GEMINI_AVAILABLE:
        await safe_reply(update, "❌ *Thiếu thư viện!* Chạy:\n`pip install google-genai`"); return
    CONFIG["ai_mode"] = not CONFIG["ai_mode"]
    save_config(CONFIG)
    if CONFIG["ai_mode"]:
        all_keys   = _get_all_ai_keys()
        model      = CONFIG.get("gemini_model", "gemini-3.1-flash-lite")
        persona    = CONFIG.get("ai_persona", "custom")
        persona_name = get_all_personas().get(persona, {}).get("name", persona)
        key_status = f"✅ {len(all_keys)} key" if all_keys else "❌ Chưa có — dùng `/aikey KEY`"
        await safe_reply(update,
            f"🤖 *AI GEMINI ĐÃ BẬT* 🤖\n\n"
            f"📦 *Model:* `{model}`\n🔑 *Keys:* {key_status}\n"
            f"🎭 *Nhân vật:* {persona_name}\n\n"
            f"💬 *Cách dùng:*\n• Tag @bot hoặc reply bot trong nhóm\n• Nhắn bất kỳ trong DM (khi proxy tắt)\n\n"
            f"📋 `/aimodel` · `/tinhcach` · `/aikeys` · `/aiclear`")
    else:
        await safe_reply(update, "😴 *AI Gemini đã tắt rồi ạ~*")

async def cmd_ai_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not context.args:
        all_keys = _get_all_ai_keys()
        await safe_reply(update,
            f"🔑 *Quản lý API Keys*\n\n"
            f"📊 *Số keys:* `{len(all_keys)}`\n"
            f"• Key chính: {'✅' if CONFIG.get('gemini_api_key', '').strip() else '❌ Chưa có'}\n"
            f"• Key phụ: `{len(CONFIG.get('gemini_api_keys_extra', []))}` keys\n\n"
            f"  `/aikey KEY` → Đặt key chính\n  `/aiaddkey KEY` → Thêm key phụ\n"
            f"  `/aikeys` → Xem danh sách\n  `/aidelkey SỐ` → Xóa key phụ\n"
            f"🌐 https://aistudio.google.com/"); return
    key = context.args[0].strip()
    CONFIG["gemini_api_key"] = key
    save_config(CONFIG)
    if GEMINI_AVAILABLE:
        ok, n_models, err = await _validate_api_key(key)
        if ok:
            await safe_reply(update,
                f"✅ *Key chính đã lưu & xác thực!*\n"
                f"🤖 `{n_models}` models khả dụng\n"
                f"🔑 Tổng `{len(_get_all_ai_keys())}` key đang hoạt động ạ~")
        else:
            await safe_reply(update, f"⚠️ *Key đã lưu* nhưng xác thực thất bại:\n`{err[:150]}`")
    else:
        await safe_reply(update, "✅ *Key chính đã lưu!*\n⚠️ Cần cài: `pip install google-genai`")

async def cmd_ai_add_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not context.args:
        await safe_reply(update, "➕ *Thêm API key phụ:*\n`/aiaddkey YOUR_KEY`\n\nKey phụ tự dùng khi hết quota ạ~"); return
    key = context.args[0].strip()
    extra_keys = CONFIG.setdefault("gemini_api_keys_extra", [])
    if key in extra_keys or key == CONFIG.get("gemini_api_key", ""):
        await safe_reply(update, "ℹ️ *Key này đã có rồi ạ~*"); return
    extra_keys.append(key)
    save_config(CONFIG)
    await safe_reply(update,
        f"✅ *Đã thêm key phụ #{len(extra_keys)}!*\n"
        f"🔑 Tổng `{len(_get_all_ai_keys())}` keys đang trong vòng xoay ạ~")

async def cmd_ai_list_keys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    primary = CONFIG.get("gemini_api_key", "").strip()
    extras  = CONFIG.get("gemini_api_keys_extra", [])
    lines   = ["🔑 *DANH SÁCH API KEYS:*\n"]
    if primary:
        short = primary[:8] + "..." + primary[-4:] if len(primary) > 12 else primary
        lines.append(f"`1.` 🔑 Key chính: `{short}`")
    else:
        lines.append("`1.` ❌ Chưa có key chính")
    for i, k in enumerate(extras, 2):
        short = k[:8] + "..." + k[-4:] if len(k) > 12 else k
        lines.append(f"`{i}.` 🗝️ Key phụ: `{short}`")
    lines.append(f"\n📊 *Tổng:* `{len(_get_all_ai_keys())}` keys")
    lines.append(f"➕ Thêm: `/aiaddkey KEY`\n❌ Xóa phụ: `/aidelkey SỐ`")
    await safe_reply(update, "\n".join(lines))

async def cmd_ai_del_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    extras = CONFIG.get("gemini_api_keys_extra", [])
    if not context.args or not context.args[0].isdigit():
        await safe_reply(update, f"❌ *Dùng:* `/aidelkey SỐ`\nXem: `/aikeys`\n*Có* `{len(extras)}` key phụ ạ~"); return
    idx = int(context.args[0]) - 1
    if idx < 0 or idx >= len(extras):
        await safe_reply(update, f"❓ *Số không hợp lệ!* Có `{len(extras)}` key phụ ạ~"); return
    removed = extras.pop(idx)
    CONFIG["gemini_api_keys_extra"] = extras
    save_config(CONFIG)
    await safe_reply(update, f"✅ *Đã xóa key phụ #{idx+1}* (`{removed[:8]}...`)\n🔑 Còn `{len(_get_all_ai_keys())}` keys ạ~")

async def cmd_ai_models(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    current = CONFIG.get("gemini_model", "gemini-3.1-flash-lite")
    kb_rows = []
    for i in range(0, len(GEMINI_MODELS_FREE), 2):
        row = []
        for m in GEMINI_MODELS_FREE[i:i+2]:
            mark = "✅ " if m == current else ""
            row.append(InlineKeyboardButton(f"{mark}{m}", callback_data=f"aimdl:{m}"))
        kb_rows.append(row)
    kb = InlineKeyboardMarkup(kb_rows)
    lines = [f"🤖 *CHỌN MODEL GEMINI:*\n", f"📦 *Hiện tại:* `{current}`\n"]
    for i, m in enumerate(GEMINI_MODELS_FREE, 1):
        mark = " ✅ *đang dùng*" if m == current else ""
        lines.append(f"`{i}.` `{m}`{mark}")
    lines.append("\n💡 Nhấn nút bên dưới để chuyển model nhanh:")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb)

async def cmd_ai_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not context.args:
        await cmd_ai_models(update, context); return
    model = context.args[0].strip()
    if model not in GEMINI_MODELS_FREE:
        names = "\n".join(f"• `{m}`" for m in GEMINI_MODELS_FREE)
        await safe_reply(update,
            f"⚠️ *`{model}` không có trong danh sách model đã biết!*\n\n"
            f"Các model hợp lệ:\n{names}\n\n"
            f"Nếu chắc chắn model này tồn tại, gõ lại kèm `force`:\n"
            f"`/aimodel {model} force`")
        if not (len(context.args) > 1 and context.args[1].strip().lower() == "force"):
            return
    CONFIG["gemini_model"] = model
    save_config(CONFIG)
    await safe_reply(update, f"✅ *Đã đổi model sang:* `{model}` ạ~")

async def cmd_ai_persona(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    current_persona = CONFIG.get("ai_persona", "custom")
    all_personas = get_all_personas()
    if context.args:
        persona_key = context.args[0].strip().lower()
        if persona_key not in all_personas:
            names = ", ".join(f"`{k}`" for k in all_personas)
            await safe_reply(update, f"❓ *Nhân vật không hợp lệ!*\nCác nhân vật: {names}"); return
        CONFIG["ai_persona"] = persona_key
        if persona_key != "custom":
            CONFIG["ai_system_prompt"] = all_personas[persona_key]["prompt"]
        save_config(CONFIG)
        AI_HISTORY.clear()
        # Xóa toàn bộ DB history khi đổi nhân vật
        try:
            import db_postgres
            if db_postgres.is_ready():
                db_postgres.delete_all_history()
        except Exception:
            pass
        persona = all_personas[persona_key]
        await safe_reply(update,
            f"🎭 *Đã đổi nhân vật AI!*\n\n{persona['emoji']} *{persona['name']}*\n_(Lịch sử đã xóa)_")
        return
    kb_rows = []
    row = []
    for i, (key, info) in enumerate(all_personas.items()):
        mark = "✅ " if key == current_persona else ""
        btn  = InlineKeyboardButton(f"{mark}{info['emoji']} {info['name']}", callback_data=f"aipersona:{key}")
        row.append(btn)
        if len(row) == 2:
            kb_rows.append(row); row = []
    if row: kb_rows.append(row)
    kb = InlineKeyboardMarkup(kb_rows)
    current_info = all_personas.get(current_persona, {})
    lines = [
        "🎭 *CHỌN NHÂN VẬT AI:*\n",
        f"*Đang dùng:* {current_info.get('emoji', '')} {current_info.get('name', current_persona)}\n",
    ]
    for key, info in all_personas.items():
        mark = " ✅" if key == current_persona else ""
        tag  = "" if is_builtin_persona(key) else " _(tự tạo)_"
        lines.append(f"• `{key}` → {info['emoji']} {info['name']}{mark}{tag}")
    lines.append("\n💡 Nhấn nút để chọn nhanh, hoặc gõ `/tinhcach key`.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb)

async def cmd_ai_sys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not context.args:
        persona     = CONFIG.get("ai_persona", "custom")
        actual_prompt = _gemini_get_sys_prompt()
        persona_info  = get_all_personas().get(persona, {})
        await safe_reply(update,
            f"📝 *System Prompt AI*\n\n"
            f"🎭 *Nhân vật:* {persona_info.get('name', persona)}\n"
            f"📋 *Prompt:*\n`{actual_prompt[:300]}{'...' if len(actual_prompt) > 300 else ''}`\n\n"
            f"1. `/tinhcach custom` để chọn chế độ tùy chỉnh\n"
            f"2. `/aisys nội_dung_prompt` để đặt prompt cho chế độ `custom`\n\n"
            f"_(Xem `/help` mục AI để biết các lệnh quản lý nhân vật khác)_"); return
    prompt = " ".join(context.args)
    CONFIG["ai_system_prompt"] = prompt
    CONFIG["ai_persona"] = "custom"
    save_config(CONFIG)
    AI_HISTORY.clear()
    await safe_reply(update, "✅ *Đã đặt prompt tùy chỉnh!*\n_(Lịch sử hội thoại đã xóa)_")

def _split_pipe_args(raw: str, n: int) -> list[str] | None:
    """Tách chuỗi theo dấu `|` thành đúng n phần, mỗi phần đã strip().
    Trả về None nếu không đủ số phần yêu cầu."""
    raw_parts = raw.split("|")
    if len(raw_parts) < n:
        return None
    # Phần cuối giữ nguyên phần còn lại NGUYÊN VẸN (join lại bằng "|" để không
    # mất khoảng trắng gốc quanh dấu | nếu prompt người dùng tự chứa ký tự này)
    if len(raw_parts) > n:
        head = [p.strip() for p in raw_parts[:n-1]]
        tail = "|".join(raw_parts[n-1:]).strip()
        return head + [tail]
    return [p.strip() for p in raw_parts]

async def cmd_ai_persona_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tạo nhân vật AI mới:
    /aipersonanew key | Tên hiển thị | Nội dung prompt              (emoji mặc định 🎭)
    /aipersonanew key | Tên hiển thị | emoji | Nội dung prompt      (tự chọn emoji)
    Phân biệt 2 dạng bằng ĐỘ DÀI đoạn thứ 3: nếu ngắn (≤ 8 ký tự, không có
    khoảng trắng) → coi là emoji, phần còn lại là prompt (phần 4). Nếu dài hơn
    → đoạn thứ 3 chính là prompt luôn (dạng 3 phần, emoji mặc định 🎭). Cách
    này tránh nhầm khi PROMPT bản thân chứa dấu `|` (vd: trích lời thoại)."""
    if not await require_owner(update): return
    raw = " ".join(context.args)
    parts4 = _split_pipe_args(raw, 4)
    key = name = emoji = prompt = None
    if parts4:
        cand_emoji = parts4[2].strip()
        if cand_emoji and len(cand_emoji) <= 8 and " " not in cand_emoji:
            key, name, emoji, prompt = parts4
            emoji = emoji.strip() or "🎭"
    if key is None:
        parts3 = _split_pipe_args(raw, 3)
        if parts3:
            key, name, prompt = parts3
            emoji = "🎭"
    if key is None:
        await safe_reply(update,
            "🆕 *Tạo nhân vật AI mới:*\n"
            "`/aipersonanew key | Tên hiển thị | Nội dung prompt`\n"
            "`/aipersonanew key | Tên hiển thị | emoji | Nội dung prompt` _(tự chọn emoji)_\n\n"
            "*Ví dụ:*\n"
            "`/aipersonanew rin | Rin Tohsaka | Bạn là Rin Tohsaka, một pháp sư kiêu "
            "ngạo nhưng tốt bụng từ Fate/Stay Night...`\n"
            "`/aipersonanew rin | Rin Tohsaka | 🦊 | Bạn là Rin Tohsaka...`\n\n"
            "⚠️ Key chỉ gồm chữ/số liền nhau, không dấu cách, không trùng nhân vật có sẵn.\n"
            "💡 Không cần đặt emoji thì mặc định là 🎭."); return
    ok, err = create_custom_persona(key.lower(), name, prompt, emoji)
    if not ok:
        await safe_reply(update, f"❌ *Không tạo được!*\n{err}"); return
    await safe_reply(update,
        f"✅ *Đã tạo nhân vật mới!*\n\n{emoji} *{name}* (key: `{key.lower()}`)\n"
        f"🔔 Trigger gọi AI: `{name.split(' (')[0].strip().lower()}`\n\n"
        f"Dùng `/tinhcach {key.lower()}` để chuyển sang nhân vật này ạ~")

async def cmd_ai_persona_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xóa nhân vật AI tự tạo: /aipersonadel key"""
    if not await require_owner(update): return
    if not context.args:
        customs = get_all_personas()
        custom_keys = [k for k in customs if not is_builtin_persona(k)]
        names = ", ".join(f"`{k}`" for k in custom_keys) if custom_keys else "_(chưa có nhân vật tự tạo nào)_"
        await safe_reply(update, f"❌ *Dùng:* `/aipersonadel key`\nNhân vật tự tạo hiện có: {names}"); return
    key = context.args[0].strip().lower()
    ok, err = delete_custom_persona(key)
    if not ok:
        await safe_reply(update, f"❌ *Không xóa được!*\n{err}"); return
    await safe_reply(update, f"🗑️ *Đã xóa nhân vật* `{key}` *rồi ạ~*")

async def cmd_ai_persona_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sửa nhân vật (built-in hoặc tự tạo) — dùng chung cho cả prompt VÀ trigger:
    /aipersonaedit key | Prompt mới                    → sửa prompt
    /aipersonaedit key | trigger: từ1, từ2             → sửa trigger (từ gọi bot)
    Nhận biết bằng tiền tố `trigger:` ở đầu phần thứ 2 (không phân biệt hoa/thường)."""
    if not await require_owner(update): return
    raw = " ".join(context.args)
    parts = _split_pipe_args(raw, 2)
    if not parts:
        all_personas = get_all_personas()
        names = ", ".join(f"`{k}`" for k in all_personas)
        lines = ["✏️ *Sửa nhân vật:*",
                  "`/aipersonaedit key | Nội dung prompt mới` — sửa prompt",
                  "`/aipersonaedit key | trigger: từ1, từ2` — sửa từ gọi bot\n",
                  f"Các nhân vật hiện có: {names}\n",
                  "ℹ️ Sửa cả nhân vật built-in (megumin, tro_ly...) đều được — bản gốc trong "
                  "code không bị mất, chỉ override riêng cho bot của bạn ạ~\n",
                  "*Trigger hiện tại:*"]
        for key, info in all_personas.items():
            trig = info.get("trigger", key)
            trig_str = trig if isinstance(trig, str) else ", ".join(trig)
            lines.append(f"• `{key}` → {trig_str}")
        await safe_reply(update, "\n".join(lines)); return
    key, second = parts
    key = key.lower()

    if second.lower().startswith("trigger:"):
        trig_raw = second.split(":", 1)[1]
        triggers = [t.strip() for t in trig_raw.split(",") if t.strip()]
        ok, err = set_persona_triggers(key, triggers)
        if not ok:
            await safe_reply(update, f"❌ *Không sửa được!*\n{err}"); return
        trig_display = ", ".join(f"`{t}`" for t in triggers)
        await safe_reply(update,
            f"✅ *Đã đặt trigger cho* `{key}`*!*\n🔔 {trig_display}\n\n"
            f"_Chỉ có hiệu lực khi `{key}` là nhân vật đang được chọn (`/tinhcach {key}`)._")
        return

    new_prompt = second
    ok, err = edit_persona_prompt(key, new_prompt)
    if not ok:
        await safe_reply(update, f"❌ *Không sửa được!*\n{err}"); return
    AI_HISTORY.clear()
    if CONFIG.get("ai_persona") == key:
        CONFIG["ai_system_prompt"] = new_prompt.strip()
        save_config(CONFIG)
    await safe_reply(update,
        f"✅ *Đã cập nhật prompt cho nhân vật* `{key}`*!*\n_(Lịch sử hội thoại đã xóa)_")

async def cmd_ai_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Thêm/xem ghi nhớ riêng cho 1 nhân vật: /aimemory key | nội dung ghi nhớ"""
    if not await require_owner(update): return
    raw = " ".join(context.args)
    if not raw.strip():
        names = ", ".join(f"`{k}`" for k in get_all_personas())
        await safe_reply(update,
            "🧠 *Quản lý ghi nhớ nhân vật:*\n"
            "`/aimemory key | nội dung cần nhớ` — Thêm 1 dòng ghi nhớ\n"
            "`/aimemory key` — Xem ghi nhớ hiện tại của nhân vật\n"
            "`/aimemoryclear key` — Xóa hết ghi nhớ\n\n"
            f"Các nhân vật hiện có: {names}\n\n"
            "💡 Ghi nhớ được nối thêm vào CUỐI prompt nhân vật ở mọi lượt chat — dùng để "
            "dạy AI nhớ lâu dài (vd: tên riêng người dùng muốn được gọi, quy tắc cố định...) "
            "mà không cần viết lại toàn bộ prompt."); return
    parts = _split_pipe_args(raw, 2)
    if not parts:
        # Chỉ có key — xem ghi nhớ hiện tại
        key = raw.strip().lower()
        all_personas = get_all_personas()
        if key not in all_personas:
            await safe_reply(update, f"❓ *Không tìm thấy nhân vật* `{key}`*!*"); return
        memory = all_personas[key].get("memory", "")
        if memory:
            await safe_reply(update, f"🧠 *Ghi nhớ của* `{key}`*:*\n\n{memory}")
        else:
            await safe_reply(update, f"ℹ️ *Nhân vật* `{key}` *chưa có ghi nhớ nào ạ~*")
        return
    key, memory_text = parts
    ok, err = add_persona_memory(key.lower(), memory_text)
    if not ok:
        await safe_reply(update, f"❌ *Không thêm được!*\n{err}"); return
    await safe_reply(update, f"✅ *Đã thêm ghi nhớ cho* `{key.lower()}`*!*\n📝 _{memory_text.strip()}_")

async def cmd_ai_memory_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xóa hết ghi nhớ của 1 nhân vật: /aimemoryclear key"""
    if not await require_owner(update): return
    if not context.args:
        await safe_reply(update, "❌ *Dùng:* `/aimemoryclear key`"); return
    key = context.args[0].strip().lower()
    ok, err = clear_persona_memory(key)
    if not ok:
        await safe_reply(update, f"ℹ️ {err}"); return
    await safe_reply(update, f"🗑️ *Đã xóa hết ghi nhớ của* `{key}` *rồi ạ~*")

async def cmd_ai_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    chat_id = update.effective_chat.id
    if chat_id in AI_HISTORY:
        count = len(AI_HISTORY[chat_id]) // 2
        del AI_HISTORY[chat_id]
        # Xóa khỏi DB
        try:
            import db_postgres
            if db_postgres.is_ready():
                db_postgres.delete_history(chat_id)
        except Exception:
            pass
        await safe_reply(update, f"🗑️ *Đã xóa {count} lượt hội thoại AI rồi ạ~*")
    else:
        await safe_reply(update, "ℹ️ *Chưa có lịch sử hội thoại nào ạ~*")

# ════════════════════════════════════════════════════════════════
# SEARCH COMMANDS
# ════════════════════════════════════════════════════════════════

async def cmd_searchdebug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    CONFIG["search_debug"] = not CONFIG.get("search_debug", False)
    save_config(CONFIG)
    if CONFIG["search_debug"]:
        await safe_reply(update,
            "🐛 *DEBUG: BẬT* 🐛\n\nLog sẽ in ra terminal với prefix `[SEARCH-DBG]` và `[VISION-DBG]`\n"
            "Dùng `/searchdebug` lần nữa để tắt ạ~")
    else:
        await safe_reply(update, "🔕 *SEARCH DEBUG: TẮT* 🔕")


async def cmd_aithinking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Đặt mức độ suy luận sâu (thinking_level/thinking_budget) — áp dụng cho
    Gemini 3.x (thinking_level) và Gemini 2.5 (thinking_budget)."""
    if not await require_owner(update): return
    args = context.args
    if not args:
        cur = CONFIG.get("ai_thinking_level", "medium")
        await safe_reply(update,
            f"🧠 *Thinking level hiện tại:* `{cur}`\n\n"
            "*Dùng:* `/aithinking low` · `/aithinking medium` · `/aithinking high`\n\n"
            "_low_ = trả lời nhanh, ít suy luận (câu đơn giản)\n"
            "_medium_ = cân bằng (mặc định)\n"
            "_high_ = suy luận sâu hơn, chậm hơn (câu khó/phức tạp)\n\n"
            "⚙️ *Gemini 3.x:* dùng `thinking_level` (low/medium/high trực tiếp)\n"
            "⚙️ *Gemini 2.5:* dùng `thinking_budget` — low = tắt thinking (nhanh nhất), "
            "medium = tự động, high = budget lớn (8192 tokens)\n"
            "⚠️ Model cũ hơn Gemini 2.5 không hỗ trợ điều chỉnh thinking ạ~")
        return
    level = args[0].strip().lower()
    if level not in ("low", "medium", "high"):
        await safe_reply(update, "❌ Chỉ nhận `low` / `medium` / `high` ạ~")
        return
    CONFIG["ai_thinking_level"] = level
    save_config(CONFIG)
    await safe_reply(update, f"🧠 *Đã đặt thinking level:* `{level}` ạ~")


async def cmd_aigrounding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bật/tắt Google Search Grounding gốc cho tìm kiếm web/tin tức (thay
    scrape DDG/Bing). Facebook và YouTube luôn dùng scrape thật, không bị
    ảnh hưởng bởi cờ này."""
    if not await require_owner(update): return
    CONFIG["ai_grounding"] = not CONFIG.get("ai_grounding", True)
    save_config(CONFIG)
    if CONFIG["ai_grounding"]:
        await safe_reply(update,
            "🔍 *GOOGLE SEARCH GROUNDING: BẬT* 🔍\n\n"
            "Tìm kiếm web/tin tức giờ dùng tool tìm kiếm chính chủ của Gemini — "
            "nhanh hơn, chính xác hơn, tự kèm nguồn thật. (Facebook/YouTube vẫn "
            "dùng scrape riêng như cũ.)\n\n"
            "⚠️ Lưu ý: tính phí riêng theo lượt tìm kiếm ngoài hạn mức miễn phí "
            "của Google (khác với scrape DDG/Bing hoàn toàn miễn phí trước đây).")
    else:
        await safe_reply(update,
            "🔕 *GOOGLE SEARCH GROUNDING: TẮT* 🔕\n\n"
            "Chuyển sang chế độ *DDG/Bing Multi-Search* ạ~\n"
            "Bot sẽ tự động scrape DDG + Bing + Google News song song (vi + en) "
            "khi câu hỏi cần thông tin mới, hoàn toàn miễn phí 🆓")


# ════════════════════════════════════════════════════════════════
# MONITOR COMMANDS
# ════════════════════════════════════════════════════════════════

async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /monitor giờ CHỈ điều khiển việc đẩy tin nhắn real-time ra DM owner
    để quan sát nhóm. Việc lưu tin nhắn vào maid-bot-db (PostgreSQL) luôn
    diễn ra bất kể lệnh này đang bật hay tắt — /tomtat vẫn hoạt động dù
    /monitor chưa từng được bật.
    """
    if not await require_owner(update): return
    if update.effective_chat.type != "private":
        await safe_reply(update, "👁️ *Dùng lệnh này trong DM nhé~*"); return
    gid = CONFIG.get("target_group_id", 0)
    if not gid:
        await safe_reply(update, "🏚️ *Chưa có nhóm đích!* Dùng `/setgroup group_id` ạ~"); return
    CONFIG["monitor_mode"] = not CONFIG["monitor_mode"]; save_config(CONFIG)
    if CONFIG["monitor_mode"]:
        kws = CONFIG.get("monitor_keywords", [])
        kw_str = ", ".join(kws) if kws else "tất cả tin nhắn"
        await safe_reply(update,
            f"👁️ *BẬT CHẾ ĐỘ GIÁM SÁT (xem trực tiếp qua DM)* 👁️\n🏠 *Nhóm:* `{gid}`\n🔍 *Lọc:* {kw_str}\n\n"
            f"📋 `/feed [n]` · `/search kw` · `/clearfeed` · `/tomtat`\n"
            f"💬 Hỏi AI bình thường để tự tìm Google/Tin tức/Facebook khi cần\n"
            f"➕ `/addkw` · `/delkw` · `/listkw` · `/monitor` (tắt)\n\n"
            f"_Lưu ý: tin nhắn vẫn được lưu vào DB kể cả khi tắt /monitor ạ~_")
    else:
        await safe_reply(update,
            "😴 *TẮT CHẾ ĐỘ GIÁM SÁT (DM)* 😴\n\n"
            "_Tin nhắn vẫn được lưu vào maid-bot-db như thường, "
            "chỉ là không đẩy ra DM nữa thôi ạ~_")

async def cmd_feed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    from utils import MSG_BUFFER
    from handlers_monitor import _render_entry
    n = min(int(context.args[0]), 50) if context.args and context.args[0].isdigit() else 10
    entries = list(MSG_BUFFER)[-n:]
    if not entries:
        await safe_reply(update, "📭 *Buffer trống ạ~* Bật `/monitor` để thu thập tin!"); return
    sep = "─" * 30
    chunks, current = [], f"📋 *{len(entries)} tin gần nhất* (buffer: {len(MSG_BUFFER)}):\n`{sep}`\n"
    for i, e in enumerate(entries, 1):
        line = _render_entry(e, i) + f"\n`{sep}`\n"
        if len(current) + len(line) > 3800:
            chunks.append(current); current = line
        else:
            current += line
    if current: chunks.append(current)
    for chunk in chunks:
        await safe_reply(update, chunk)

async def cmd_search_feed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    from handlers_monitor import _render_entry
    if not context.args:
        await safe_reply(update, "🔍 *Dùng:* `/search từ_khóa` ạ~"); return
    kw      = " ".join(context.args).lower()
    results = [e for e in MSG_BUFFER if kw in e["text"].lower() or kw in e["name"].lower() or kw in e["username"].lower()]
    if not results:
        await safe_reply(update, f"🔍 *Không tìm thấy tin nào chứa '{kw}' ạ~*"); return
    sep = "─" * 30
    chunks, current = [], f"🔍 *Kết quả tìm '{kw}'* ({len(results)} tin):\n`{sep}`\n"
    for i, e in enumerate(results[-20:], 1):
        line = _render_entry(e, i) + f"\n`{sep}`\n"
        if len(current) + len(line) > 3800:
            chunks.append(current); current = line
        else:
            current += line
    if current: chunks.append(current)
    for chunk in chunks:
        await safe_reply(update, chunk)

async def cmd_clearfeed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    MSG_BUFFER.clear()
    await safe_reply(update, "🗑️ *Đã xóa sạch buffer rồi ạ~*")

async def cmd_addkw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not context.args:
        await safe_reply(update, "➕ *Dùng:* `/addkw từ_khóa` ạ~"); return
    kw  = " ".join(context.args).lower().strip()
    kws = CONFIG.setdefault("monitor_keywords", [])
    if kw not in kws:
        kws.append(kw); save_config(CONFIG)
        await safe_reply(update, f"✅ *Đã thêm từ khóa:* `[{kw}]` — Tổng: `{len(kws)}` ạ~")
    else:
        await safe_reply(update, f"ℹ️ *Từ khóa [{kw}] đã có rồi ạ~*")

async def cmd_delkw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    if not context.args:
        await safe_reply(update, "❌ *Dùng:* `/delkw từ_khóa` ạ~"); return
    kw  = " ".join(context.args).lower().strip()
    kws = CONFIG.get("monitor_keywords", [])
    if kw in kws:
        kws.remove(kw); save_config(CONFIG)
        await safe_reply(update, f"✅ *Đã xóa từ khóa:* `[{kw}]` *rồi ạ~*")
    else:
        await safe_reply(update, f"❓ *Không tìm thấy từ khóa [{kw}] ạ~*")

async def cmd_listkw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    kws = CONFIG.get("monitor_keywords", [])
    if not kws:
        await safe_reply(update, "🔍 *Chưa có từ khóa lọc* (đang forward tất cả)~"); return
    await safe_reply(update, "🔍 *TỪ KHÓA LỌC MONITOR:*\n\n" + "\n".join(f"• `{kw}`" for kw in kws))

async def cmd_groupstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return
    gid = CONFIG.get("target_group_id", 0)
    if not gid:
        await safe_reply(update, "🏚️ *Chưa có nhóm đích!*"); return
    try:
        chat, count, admins = await asyncio.gather(
            context.bot.get_chat(gid),
            context.bot.get_chat_member_count(gid),
            context.bot.get_chat_administrators(gid),
        )
        admin_names = [f"• {a.user.full_name} (@{a.user.username or '—'})" for a in admins if not a.user.is_bot]
        pinned = ""
        if chat.pinned_message:
            pm     = chat.pinned_message
            pinned = f"\n📌 *Tin ghim:* #{pm.message_id} — {(pm.text or pm.caption or '[media]')[:60]}"
        await safe_reply(update,
            f"📊 *TRẠNG THÁI NHÓM*\n\n🏠 *{chat.title}*\n🆔 `{gid}`\n"
            f"👥 *Thành viên:* {count}\n👑 *Admins ({len(admin_names)}):*\n"
            + "\n".join(admin_names[:10]) + pinned
            + f"\n\n📦 *Buffer:* {len(MSG_BUFFER)} tin\n"
            f"👁️ *Monitor:* {'🟢 Bật' if CONFIG.get('monitor_mode') else '🔴 Tắt'}"
        )
    except Exception as e:
        await safe_reply(update, f"❌ *Lỗi:* `{e}`")

def _last_tomtat_get(gid: int) -> str:
    """Đọc mốc thời gian (ISO) của lần /tomtat moi gần nhất cho 1 nhóm, từ bot_config."""
    try:
        import db_postgres
        if not db_postgres.is_ready():
            return ""
        data = db_postgres.config_load_all().get("last_tomtat", {})
        return data.get(str(gid), "")
    except Exception:
        return ""

def _last_tomtat_set(gid: int, ts_iso: str) -> None:
    """Lưu mốc thời gian tóm tắt mới nhất cho 1 nhóm vào bot_config."""
    try:
        import db_postgres
        if not db_postgres.is_ready():
            return
        data = db_postgres.config_load_all().get("last_tomtat", {})
        data[str(gid)] = ts_iso
        db_postgres.config_save("last_tomtat", data)
    except Exception:
        pass

async def cmd_tomtat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_owner(update): return

    mode = "n"     # "n" = N tin gần nhất | "ngay" = trong ngày | "moi" = từ lần tóm tắt cuối
    n    = 50
    since_iso = ""

    if context.args:
        arg = context.args[0].strip().lower()
        if arg.isdigit():
            n = max(5, min(int(arg), 200))
        elif arg in ("ngay", "today", "hom_nay", "homnay"):
            mode = "ngay"
        elif arg in ("moi", "new", "latest"):
            mode = "moi"
        else:
            await safe_reply(update,
                "📋 *Tóm tắt lịch sử nhóm:*\n\n"
                "`/tomtat` → 50 tin gần nhất\n"
                "`/tomtat 100` → 100 tin gần nhất (tối đa 200)\n"
                "`/tomtat ngay` → toàn bộ tin *trong ngày hôm nay*\n"
                "`/tomtat moi` → toàn bộ tin *từ lần /tomtat moi gần nhất*\n\n"
                "_Dữ liệu lấy từ maid-bot-db, không cần bật /monitor ạ~_\n"
                "_Tóm tắt được viết theo đúng tính cách AI đang chọn (`/tinhcach`) ạ~_"); return

    if not GEMINI_AVAILABLE:
        await safe_reply(update, "❌ *Thiếu thư viện!* Cài: `pip install google-genai` ạ~"); return
    if not _get_all_ai_keys():
        await safe_reply(update, "⚠️ *Chưa có API key!* Dùng `/aikey KEY` để đặt ạ~"); return

    gid = CONFIG.get("target_group_id", 0)
    if not gid:
        await safe_reply(update, "🏚️ *Chưa có nhóm đích!* Dùng `/setgroup group_id` ạ~"); return

    import db_postgres
    db_ready = False
    try:
        db_ready = db_postgres.is_ready()
    except Exception:
        db_ready = False

    entries = []
    new_since_ts = now_vn()   # mốc sẽ lưu lại sau khi tóm tắt xong (chỉ dùng cho mode "moi")

    if mode == "ngay":
        if not db_ready:
            await safe_reply(update, "⚠️ *PostgreSQL chưa kết nối nên không tra được tin theo ngày!* Dùng `/tomtat [n]` thay thế ạ~"); return
        start_of_day = now_vn().replace(hour=0, minute=0, second=0, microsecond=0)
        entries = db_postgres.msg_buffer_load_since(gid, start_of_day.isoformat(), limit=2000)
    elif mode == "moi":
        if not db_ready:
            await safe_reply(update, "⚠️ *PostgreSQL chưa kết nối nên không tra được mốc tóm tắt cuối!* Dùng `/tomtat [n]` thay thế ạ~"); return
        since_iso = _last_tomtat_get(gid)
        if not since_iso:
            # Chưa từng /tomtat moi → lấy từ đầu ngày hôm nay làm mốc khởi điểm hợp lý
            since_iso = now_vn().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        entries = db_postgres.msg_buffer_load_since(gid, since_iso, limit=2000)
    else:
        # Lấy tin nhắn trực tiếp từ maid-bot-db (PostgreSQL) — luôn có dữ liệu
        # bất kể /monitor đang bật hay tắt, vì mọi tin nhắn đều được lưu DB
        # ngay khi nhận, độc lập với chế độ giám sát.
        try:
            if db_ready:
                entries = db_postgres.msg_buffer_load(gid, limit=n)
        except Exception:
            entries = []
        # Fallback: nếu DB chưa sẵn sàng, dùng buffer RAM hiện có (best-effort)
        if not entries:
            entries = list(MSG_BUFFER)[-n:]

    if not entries:
        if mode == "ngay":
            await safe_reply(update, "📭 *Chưa có tin nhắn nào trong nhóm hôm nay ạ~*")
        elif mode == "moi":
            await safe_reply(update, "📭 *Chưa có tin nhắn mới nào từ lần tóm tắt trước ạ~*")
        else:
            await safe_reply(update, "📭 *Chưa có tin nhắn nào trong maid-bot-db ạ~*")
        # [FIX] Mọi mode đều cập nhật mốc — xem giải thích chi tiết ở khối dưới.
        _last_tomtat_set(gid, new_since_ts.isoformat())
        return

    text_entries = [e for e in entries if e.get("text") or e.get("type", "text") != "text"]
    if not text_entries:
        await safe_reply(update, "📭 *Không có tin nhắn nào để tóm tắt ạ~*")
        # [FIX] Trước đây chỉ mode "moi" mới set mốc — khiến /tomtat moi chạy
        # sau /tomtat n hoặc /tomtat ngay vẫn coi những tin đã tóm tắt là "mới".
        # Giờ MỌI mode đều cập nhật mốc khi đã xử lý xong tới đây, để lần
        # /tomtat moi kế tiếp luôn tính từ lần /tomtat BẤT KỲ gần nhất.
        _last_tomtat_set(gid, new_since_ts.isoformat())
        return

    vn_time     = now_vn().strftime("%H:%M %d/%m/%Y")
    status_label = {
        "ngay": "tin trong ngày hôm nay", "moi": "tin mới kể từ lần tóm tắt trước",
        "n":    f"{len(text_entries)} tin gần nhất",
    }[mode]
    _persona_name = get_all_personas().get(CONFIG.get("ai_persona", ""), {}).get("name", "AI").split(" (")[0]
    status_msg = await update.message.reply_text(
        to_markdown_v2(
            f"🤖 *{_persona_name} đang đọc {len(text_entries)} {status_label if mode != 'n' else ''} và tóm tắt...*"
            if mode == "n" else f"🤖 *{_persona_name} đang đọc {status_label} ({len(text_entries)} tin) và tóm tắt...*"
        ),
        parse_mode=ParseMode.MARKDOWN_V2)

    from handlers_monitor import _ai_summarize_chat
    summary = await _ai_summarize_chat(text_entries, n, mode=mode)
    try: await status_msg.delete()
    except Exception: pass

    if not summary:
        await safe_reply(update, "⚠️ *AI không thể tóm tắt lúc này. Kiểm tra API key hoặc thử lại ạ~*")
        return

    # Chuẩn hoá `**bold**` (Markdown chuẩn/GitHub, AI hay dùng theo thói quen
    # dù đã hướng dẫn trong prompt chỉ dùng 1 dấu sao) thành `*bold*` TRƯỚC
    # khi escape MarkdownV2 — to_markdown_v2() chỉ nhận diện đúng `*bold*`
    # (1 dấu sao) làm markup, còn lại escape an toàn mọi ký tự đặc biệt khác
    # AI có thể đã dùng (-, ., !, (, )...).
    summary = summary.replace("**", "*")

    try:
        dt_first = datetime.fromisoformat(text_entries[0]["ts"]).astimezone(VN_TZ).strftime("%H:%M %d/%m")
        dt_last  = datetime.fromisoformat(text_entries[-1]["ts"]).astimezone(VN_TZ).strftime("%H:%M %d/%m")
        time_range = f"{dt_first} → {dt_last}"
    except Exception:
        time_range = vn_time

    title = {
        "ngay": "📋 *TÓM TẮT TRONG NGÀY*",
        "moi":  "📋 *TÓM TẮT TỪ LẦN TÓM TẮT TRƯỚC*",
        "n":    "📋 *TÓM TẮT LỊCH SỬ NHÓM*",
    }[mode]
    header = f"{title}\n🕐 _{time_range} ICT_ · _{len(text_entries)} tin nhắn_\n{'─' * 32}\n"
    full_raw = header + summary
    # Cắt theo TEXT THÔ (trước khi escape) để mỗi phần độc lập escape đúng,
    # tránh cắt giữa chừng 1 chuỗi escape (vd ngay sau dấu \) làm vỡ cú pháp.
    raw_parts = [full_raw[:4000], full_raw[4000:]] if len(full_raw) > 4000 else [full_raw]
    for raw_part in raw_parts:
        if raw_part.strip():
            try:
                await update.message.reply_text(to_markdown_v2(raw_part), parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True)
            except Exception:
                await safe_reply(update, raw_part)

    # [FIX] Trước đây CHỈ mode "moi" mới cập nhật mốc last_tomtat, khiến
    # /tomtat ngay hoặc /tomtat [n] không ghi nhận gì — nên lần /tomtat moi
    # kế tiếp vẫn tính từ mốc CŨ (hoặc từ đầu ngày nếu chưa từng /tomtat moi),
    # dẫn tới tóm tắt LẶP LẠI những tin đã được /tomtat ngay xử lý rồi.
    # Giờ MỌI mode, sau khi tóm tắt THÀNH CÔNG, đều cập nhật mốc — vì về bản
    # chất người dùng đã "xem" tin nhắn tới thời điểm này rồi, bất kể họ gọi
    # bằng lệnh nào. /tomtat moi luôn tính từ lần /tomtat BẤT KỲ gần nhất.
    _last_tomtat_set(gid, new_since_ts.isoformat())


# ════════════════════════════════════════════════════════════════
# DỌN DỮ LIỆU DB (msg_buffer) — dùng khi PostgreSQL free tier (1GB) gần đầy
# ════════════════════════════════════════════════════════════════

async def cmd_cleandb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /cleandb            → hiện hướng dẫn + dung lượng DB hiện tại
    /cleandb N           → xóa tin nhắn (msg_buffer) cũ hơn N ngày, TẤT CẢ nhóm
    /cleandb N nhom      → xóa tin nhắn cũ hơn N ngày, CHỈ nhóm đích hiện tại
    /cleandb all         → xóa TOÀN BỘ msg_buffer (mọi nhóm) — dùng khi DB quá đầy
    """
    if not await require_owner(update): return

    try:
        import db_postgres
    except Exception:
        await safe_reply(update, "❌ *Không import được db_postgres ạ~*"); return

    if not db_postgres.is_ready():
        await safe_reply(update, "❌ *PostgreSQL chưa kết nối!* Kiểm tra `DATABASE_URL` ạ~"); return

    if not context.args:
        size_info = db_postgres.get_db_size_info()
        stats     = db_postgres.get_stats()
        size_text = ""
        if size_info:
            size_text = (
                f"\n💽 *Dung lượng DB:* `{size_info.get('total_mb', '?')} MB` / `1024 MB` (free tier)\n"
                f"📦 *Riêng msg_buffer:* `{size_info.get('msg_buffer_mb', '?')} MB`\n"
            )
        await safe_reply(update,
            "🧹 *DỌN DỮ LIỆU msg_buffer (maid-bot-db)*\n"
            f"📊 *Hiện có:* `{stats.get('msg_rows', 0):,}` tin nhắn trong DB"
            f"{size_text}\n"
            "📖 *Cách dùng:*\n"
            "`/cleandb 30` → xóa tin cũ hơn 30 ngày (mọi nhóm)\n"
            "`/cleandb 30 nhom` → xóa tin cũ hơn 30 ngày (chỉ nhóm đích hiện tại)\n"
            "`/cleandb all` → ⚠️ xóa SẠCH toàn bộ msg_buffer (mọi nhóm)\n\n"
            "_Lưu ý: lệnh này KHÔNG ảnh hưởng tới ai_history (lịch sử chat AI) ạ~_"
        ); return

    arg0 = context.args[0].strip().lower()

    if arg0 == "all":
        deleted = db_postgres.msg_buffer_clear_all()
        if deleted < 0:
            await safe_reply(update, "❌ *Lỗi khi xóa msg_buffer ạ~*"); return
        await safe_reply(update, f"🗑️ *Đã xóa SẠCH msg_buffer!* (`{deleted:,}` tin nhắn) ạ~")
        return

    if not arg0.isdigit():
        await safe_reply(update, "❌ *Dùng:* `/cleandb N` (số ngày) hoặc `/cleandb all` ạ~"); return

    days = max(1, int(arg0))
    only_target_group = len(context.args) > 1 and context.args[1].strip().lower() in ("nhom", "nhóm", "group")
    gid = CONFIG.get("target_group_id", 0) if only_target_group else None

    if only_target_group and not gid:
        await safe_reply(update, "🏚️ *Chưa có nhóm đích!* Dùng `/setgroup group_id` trước ạ~"); return

    deleted = db_postgres.msg_buffer_delete_older_than(days, chat_id=gid)
    if deleted < 0:
        await safe_reply(update, "❌ *Lỗi khi xóa msg_buffer ạ~*"); return

    scope = f"nhóm `{gid}`" if gid else "tất cả nhóm"
    await safe_reply(update,
        f"🗑️ *Đã xóa* `{deleted:,}` *tin nhắn cũ hơn {days} ngày* ({scope}) ạ~")


# ════════════════════════════════════════════════════════════════
# DATABASE STATS
# ════════════════════════════════════════════════════════════════

async def cmd_dbstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xem thống kê PostgreSQL ngay trong Telegram."""
    if not await require_owner(update): return

    try:
        import db_postgres
        if not db_postgres.is_ready():
            await safe_reply(update,
                "❌ *PostgreSQL chưa kết nối!*\n\n"
                "Kiểm tra biến môi trường `DATABASE_URL` trên Render ạ~"
            )
            return

        s = db_postgres.get_stats()

        # Lấy thêm vài tin nhắn gần nhất
        gid = CONFIG.get("target_group_id", 0)
        recent_msgs = ""
        if gid:
            msgs = db_postgres.msg_buffer_load(gid, limit=3)
            if msgs:
                lines = []
                for m in msgs:
                    name = m.get("name", "?")
                    text = (m.get("text") or f"[{m.get('type','?')}]")[:40]
                    lines.append(f"• *{name}:* {text}")
                recent_msgs = "\n\n📨 *3 tin nhắn gần nhất:*\n" + "\n".join(lines)

        # Lấy vài cuộc hội thoại AI gần nhất
        from ai_module import AI_HISTORY
        ai_chats = len(AI_HISTORY)
        ai_turns = sum(len(v) // 2 for v in AI_HISTORY.values())

        await safe_reply(update,
            f"🗄️ *THỐNG KÊ POSTGRESQL*\n\n"
            f"🔌 *Kết nối:* ✅ Đang hoạt động\n\n"
            f"📊 *Bảng ai_history* (lịch sử AI):\n"
            f"   • `{s.get('hist_rows', 0):,}` dòng trong DB\n"
            f"   • `{s.get('hist_chats', 0)}` cuộc hội thoại\n"
            f"   • RAM hiện tại: `{ai_chats}` chat, `{ai_turns}` lượt\n\n"
            f"📦 *Bảng msg_buffer* (tin nhắn nhóm):\n"
            f"   • `{s.get('msg_rows', 0):,}` tin nhắn đã lưu\n"
            f"   • Nhóm đang theo dõi: `{gid or 'Chưa set'}`"
            f"{recent_msgs}\n\n"
            f"💡 *Lệnh liên quan:*\n"
            f"`/aiclear` → xóa lịch sử AI\n"
            f"`/clearfeed` → xóa buffer nhóm"
        )

    except Exception as e:
        await safe_reply(update, f"❌ *Lỗi đọc DB:* `{e}`")


# ════════════════════════════════════════════════════════════════
# CLAUDE (Anthropic) — nhà cung cấp AI thứ hai
# ════════════════════════════════════════════════════════════════

async def cmd_ai_provider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/aiprovider [gemini|claude] — chọn AI nào trả lời."""
    if not await require_owner(update): return
    from claude_module import get_key, CLAUDE_MODELS
    cur = CONFIG.get("ai_provider", "gemini")
    if not context.args:
        await safe_reply(update,
            f"🧠 *Nhà cung cấp AI hiện tại:* `{cur}`\n"
            f"• Gemini key: {'✅' if _get_all_ai_keys() else '❌'}\n"
            f"• Claude key: {'✅' if get_key() else '❌'} · model `{CONFIG.get('claude_model', CLAUDE_MODELS[0])}`\n\n"
            f"`/aiprovider claude` · `/aiprovider gemini`\n"
            f"`/claudekey KEY` → đặt key Claude · `/claudemodel` → đổi model\n"
            f"_Nếu Claude lỗi, bot tự rơi về Gemini ạ~_"); return
    choice = context.args[0].strip().lower()
    if choice not in ("gemini", "claude"):
        await safe_reply(update, "❓ Chỉ nhận `gemini` hoặc `claude` ạ~"); return
    if choice == "claude" and not get_key():
        await safe_reply(update, "⚠️ *Chưa có key Claude!* Dùng `/claudekey KEY` trước ạ~\n🌐 https://console.anthropic.com/"); return
    CONFIG["ai_provider"] = choice
    save_config(CONFIG)
    await safe_reply(update, f"✅ *Đã chuyển AI sang:* `{choice}` ạ~")


async def cmd_claude_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/claudekey KEY — lưu & xác thực key Claude (tin nhắn chứa key sẽ bị xoá)."""
    if not await require_owner(update): return
    if not context.args:
        await safe_reply(update,
            "🔑 `/claudekey KEY` → đặt key Claude\n"
            "⚠️ API Claude *tính phí* (không có gói API miễn phí). Lấy key: https://console.anthropic.com/"); return
    key = context.args[0].strip()
    try:
        await update.message.delete()   # tránh để lộ key trong chat
    except Exception:
        pass
    from claude_module import validate_key
    ok, err = await validate_key(key)
    CONFIG["claude_api_key"] = key
    save_config(CONFIG)
    if ok:
        await context.bot.send_message(update.effective_chat.id,
            "✅ Key Claude đã lưu & xác thực! Bật bằng `/aiprovider claude` ạ~", parse_mode="Markdown")
    else:
        await context.bot.send_message(update.effective_chat.id,
            f"⚠️ Key đã lưu nhưng xác thực thất bại:\n`{err}`")


async def cmd_claude_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/claudemodel [tên] — xem/đổi model Claude."""
    if not await require_owner(update): return
    from claude_module import CLAUDE_MODELS
    cur = CONFIG.get("claude_model", CLAUDE_MODELS[0])
    if not context.args:
        names = "\n".join(f"• `{m}`{' ✅' if m == cur else ''}" for m in CLAUDE_MODELS)
        await safe_reply(update, f"🤖 *Model Claude:*\n{names}\n\nĐổi: `/claudemodel claude-sonnet-5-5`"); return
    m = context.args[0].strip()
    CONFIG["claude_model"] = m
    save_config(CONFIG)
    await safe_reply(update, f"✅ *Model Claude:* `{m}` ạ~")
