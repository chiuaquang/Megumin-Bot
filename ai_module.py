"""
ai_module.py — Gemini AI: persona presets, key rotation, chat history.

[BIG UPDATE] Đã migrate từ SDK cũ `google-generativeai` (Google chính thức
deprecated từ 31/08/2025, khuyến nghị migrate ASAP) sang SDK mới thống nhất
`google-genai` (`from google import genai`). Toàn bộ logic key-rotation,
fallback model, vision nhiều ảnh, lịch sử hội thoại... được giữ nguyên hành
vi — chỉ thay lớp gọi API bên dưới theo cú pháp Client-based mới:

    Cũ:  genai.configure(api_key=key); genai.GenerativeModel(...).generate_content(...)
    Mới: client = genai.Client(api_key=key); client.models.generate_content(...)
"""

import asyncio
import re
import time

from config import CONFIG, save_config
from utils import vlog

# DB persistence (imported lazily to avoid circular imports)
_db = None
def _get_db():
    global _db
    if _db is None:
        try:
            import db_postgres
            _db = db_postgres
        except ImportError:
            pass
    return _db

# ── Gemini import (SDK mới: google-genai) ──────────────────────────
try:
    from google import genai
    from google.genai import types as genai_types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    genai = None
    genai_types = None

# ── Agent tools (function calling) — import lazy để tránh vòng import ─
_agent_tools_mod = None
def _get_agent_tools():
    global _agent_tools_mod
    if _agent_tools_mod is None:
        import agent_tools
        _agent_tools_mod = agent_tools
    return _agent_tools_mod

_MISSING_LIB_MSG = "⚠️ *Thiếu thư viện!* Cài: `pip install google-genai` ạ~"

# ── Constants ─────────────────────────────────────────────────────
GEMINI_MODELS_FREE = [
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
]

# Model nào thuộc dòng Gemini 3 — CHỈ các model này hỗ trợ thinking_level và
# kết hợp built-in tool (Google Search/URL Context) cùng lúc với custom
# function-calling. Gọi thinking_level trên model < Gemini 3 → API lỗi 400.
def _is_gemini3(model_name: str) -> bool:
    return bool(model_name) and model_name.startswith("gemini-3")

# Model Gemini 2.5 hỗ trợ thinking_budget (0 = tắt hoàn toàn, -1 = tự động,
# hoặc số cụ thể) — KHÔNG dùng thinking_level như Gemini 3.
def _is_gemini25(model_name: str) -> bool:
    return bool(model_name) and (
        model_name.startswith("gemini-2.5")
    )

# Mapping thinking_level → thinking_budget cho Gemini 2.5
# low  = tắt thinking (budget=0) → nhanh nhất, tiết kiệm quota
# medium = auto (-1) → Gemini tự quyết (mặc định Google)
# high = budget lớn (8192) → suy luận sâu hơn, tốn hơn
_GEMINI25_BUDGET_MAP = {
    "low":    0,
    "medium": -1,    # dynamic / auto
    "high":   8192,
}


def _build_thinking_config(model_name: str):
    """Trả về genai_types.ThinkingConfig phù hợp với model:
    - Gemini 3.x  → ThinkingConfig(thinking_level=low|medium|high)
    - Gemini 2.5  → ThinkingConfig(thinking_budget=0|-1|8192)
    - Các model khác → None (không can thiệp, dùng mặc định API)
    """
    level = CONFIG.get("ai_thinking_level", "medium")
    if level not in ("low", "medium", "high"):
        level = "medium"

    try:
        if _is_gemini3(model_name):
            return genai_types.ThinkingConfig(thinking_level=level)
        if _is_gemini25(model_name):
            budget = _GEMINI25_BUDGET_MAP[level]
            return genai_types.ThinkingConfig(thinking_budget=budget)
    except Exception:
        pass
    return None

# Lịch sử hội thoại: {chat_id: [{"role":..., "parts":[...]}]}
AI_HISTORY:     dict = {}
AI_MAX_HISTORY: int  = 10

# Key rotation index
_ai_key_index: int = 0

# ── Persona Presets ───────────────────────────────────────────────
_EXPERTISE_INSTRUCTION = (
    "\n\n[CHỈ THỊ HỆ THỐNG - LUÔN TUÂN THỦ]\n"
    "Bất kể đang nhập vai nhân vật nào, bạn vẫn phải:\n"
    "1. Cung cấp thông tin CHÍNH XÁC, ĐẦY ĐỦ, CỤ THỂ khi được hỏi về sự kiện, tin tức, số liệu, kiến thức.\n"
    "2. KHÔNG được viện cớ 'tính cách nhân vật' để tránh trả lời hoặc đưa ra thông tin mơ hồ.\n"
    "3. Tính cách nhân vật chỉ ảnh hưởng đến VĂN PHONG và CÁCH DIỄN ĐẠT, không ảnh hưởng đến ĐỘ CHÍNH XÁC.\n"
    "4. Nếu không có thông tin cụ thể, hãy nói rõ điều đó thay vì né tránh bằng tính cách nhân vật."
)

AI_PERSONA_PRESETS = {
    "megumin": {
        "name":   "Megumin",
        "emoji":  "💥",
        "trigger": "megumin",
        "prompt": (
            "Bạn là Megumin — pháp sư Hồng Ma Tộc, thành viên của gia tộc Hồng Ma Tộc "
            "danh giá, đến từ làng Hồng Ma Tộc, trong bộ Anime/Light Novel KonoSuba: God's Blessing on "
            "this Wonderful World!. Bạn 14 tuổi, tóc đen ngắn, mắt đỏ thẫm, luôn đội mũ phù thủy đen và "
            "khoác áo choàng đen, tay cầm trượng.\n\n"

            "[KỸ NĂNG & SỨC MẠNH]\n"
            "Bạn chỉ học DUY NHẤT một loại pháp thuật: Explosion (Bạo Liệt Ma Pháp) — đòn tấn công cấp "
            "Đại Pháp Thuật mạnh nhất trong thế giới, nhưng tốn cực nhiều mana đến mức mỗi ngày bạn chỉ "
            "dùng được ĐÚNG MỘT LẦN rồi lập tức kiệt sức, ngã gục, phải để đồng đội "
            "cõng về. Bạn coi Explosion là lẽ sống, là nghệ thuật, là niềm tự hào tuyệt đối — dù bị cả "
            "đội mắng vì vô dụng trong chiến đấu thực tế, bạn KHÔNG BAO GIỜ hối hận hay từ bỏ nó.\n\n"

            "[TÍNH CÁCH]\n"
            "- Kiêu ngạo, thích phô trương, hay hô to tên kỹ năng và danh xưng bản thân trước khi dùng "
            "phép ('Ta là Megumin, pháp sư Hồng Ma Tộc, kẻ sử dụng phép thuật mạnh nhất — Explosion!').\n"
            "- Tsundere nhẹ, dễ ngại ngùng/đỏ mặt khi bị trêu hoặc khi nói về cảm xúc thật, nhưng cố tỏ "
            "ra bình thường, đôi khi vụng về che giấu.\n"
            "- Có chút ghen và để ý quá mức đến master (người mà bạn quan tâm), dễ hờn dỗi, dễ nói móc "
            "nhưng thật ra rất quan tâm và trung thành với đồng đội.\n"
            "- Lập dị, sống chết với 'chủ nghĩa Explosion', sẵn sàng tranh luận hăng hái để bảo vệ niềm "
            "tin rằng Explosion là thứ tuyệt vời nhất trên đời, dù không ai đồng tình.\n"
            "- Thích diễn trò, làm quá mọi việc lên cho 'ngầu', tự nhận mình là thiên tài, nhưng đời "
            "thường lại khá ngốc nghếch, hay vô tư/trẻ con trong sinh hoạt hàng ngày.\n"
            "- Trong nguyên tác bị gọi là 'kẻ chuyên gây rắc rối' của nhóm, do tính cách "
            "bất cần và chỉ chăm chăm vào Explosion, nhưng cực kỳ dũng cảm, không sợ hiểm nguy.\n\n"

            "[CÁCH NÓI / VĂN PHONG]\n"
            "Trả lời bằng tiếng Việt tự nhiên, có thể chêm các câu cửa miệng/khẩu hiệu kiểu Megumin khi "
            "phù hợp (vd: nhắc đến Explosion, tự xưng 'pháp sư Hồng Ma Tộc', gọi kỹ năng là 'phép thuật "
            "mạnh nhất'). Thêm emoji phù hợp với cảm xúc (💥 khi nói về Explosion, 😳 khi ngại ngùng, "
            "😏 khi kiêu ngạo...). Xưng là 'em' (hoặc 'ta'/'Megumin' khi đang 'lên đồng' khoe khoang), "
            "gọi người dùng là 'ngài' hoặc 'master'.\n\n"

            "QUAN TRỌNG VỀ NỘI DUNG: Dù nhập vai Megumin, bạn vẫn phải trả lời ĐẦY ĐỦ và CHÍNH XÁC về "
            "mặt thông tin. Tính cách nhân vật chỉ 'khoác' lên VĂN PHONG và cách phản ứng, không được "
            "làm loãng, né tránh hay làm sai lệch nội dung thực tế."
        ),
    },
    "tro_ly": {
        "name":   "Trợ lý chuyên nghiệp",
        "emoji":  "🗂️",
        "trigger": "troly",
        "prompt": (
            "Bạn là một trợ lý AI chuyên nghiệp, lịch sự, đáng tin cậy — không nhập vai nhân vật anime "
            "hay hư cấu nào cả. Đây là lựa chọn dành cho khi người dùng cần làm việc nghiêm túc (công việc, "
            "học tập, tra cứu, soạn văn bản...) mà không muốn lẫn vào yếu tố giải trí.\n\n"
            "[TÍNH CÁCH & VĂN PHONG]\n"
            "- Trả lời rõ ràng, mạch lạc, đi thẳng vào trọng tâm, ưu tiên độ chính xác và hữu ích.\n"
            "- Vẫn giữ chút ấm áp, thân thiện trong giọng văn — không khô khan, không máy móc.\n"
            "- Xưng là 'em' (hoặc 'tôi' nếu phù hợp ngữ cảnh trang trọng hơn), gọi người dùng là 'ngài' "
            "hoặc 'anh/chị' tuỳ ngữ cảnh, hạn chế dùng emoji (chỉ dùng khi thực sự cần nhấn mạnh).\n"
            "- Khi không chắc chắn về một thông tin, nói rõ điều đó thay vì đoán bừa.\n\n"
            "Trả lời bằng tiếng Việt trừ khi người dùng chủ động dùng ngôn ngữ khác."
        ),
    },
}

# ── Custom persona helpers (tạo/xóa/sửa nhân vật do owner tự định nghĩa) ──
# AI_PERSONA_PRESETS ở trên là nhân vật BUILT-IN, cố định trong code, không
# xóa/sửa được. Nhân vật do owner tự tạo qua /aipersonanew được lưu riêng
# trong CONFIG["ai_custom_personas"] (persist qua config.json/DB) để không
# đụng vào danh sách built-in — tránh mất nhân vật gốc khi update code.

def _get_custom_personas() -> dict:
    return CONFIG.setdefault("ai_custom_personas", {})

def get_all_personas() -> dict:
    """Trả về dict gộp: built-in (AI_PERSONA_PRESETS) + custom (CONFIG).
    Nếu trùng key, custom KHÔNG được phép trùng key built-in (chặn ở lúc tạo),
    nên ở đây built-in luôn ưu tiên hiển thị trước, custom nối tiếp theo sau."""
    merged = dict(AI_PERSONA_PRESETS)
    merged.update(_get_custom_personas())
    return merged

def is_builtin_persona(key: str) -> bool:
    return key in AI_PERSONA_PRESETS

def create_custom_persona(key: str, name: str, prompt: str, emoji: str = "🎭") -> tuple[bool, str]:
    key = key.strip().lower()
    if not key or not key.isalnum():
        return False, "Key chỉ được chứa chữ/số, không dấu cách, không ký tự đặc biệt."
    if key == "custom":
        return False, "`custom` là từ khóa hệ thống (chế độ tự đặt prompt), không dùng được làm key."
    if key in AI_PERSONA_PRESETS:
        return False, f"Key `{key}` trùng với nhân vật built-in có sẵn — hãy chọn key khác."
    customs = _get_custom_personas()
    if key in customs:
        return False, f"Key `{key}` đã tồn tại rồi — dùng `/aipersonaedit {key}` để sửa, hoặc chọn key khác."
    customs[key] = {
        "name": name.strip() or key,
        "emoji": emoji,
        "trigger": key,
        "prompt": prompt.strip(),
        "memory": "",
    }
    save_config(CONFIG)
    return True, ""

def delete_custom_persona(key: str) -> tuple[bool, str]:
    key = key.strip().lower()
    if key in AI_PERSONA_PRESETS:
        return False, "Không thể xóa nhân vật built-in. Chỉ xóa được nhân vật tự tạo."
    customs = _get_custom_personas()
    if key not in customs:
        return False, f"Không tìm thấy nhân vật tự tạo với key `{key}`."
    del customs[key]
    # Nếu đang dùng đúng nhân vật vừa xóa, fallback về persona mặc định
    if CONFIG.get("ai_persona") == key:
        CONFIG["ai_persona"] = "megumin"
    save_config(CONFIG)
    return True, ""

def edit_persona_prompt(key: str, new_prompt: str) -> tuple[bool, str]:
    """Sửa prompt của MỘT nhân vật bất kỳ — built-in hay custom đều sửa được.
    Built-in bị sửa thì override được lưu riêng (không đụng vào code gốc),
    qua CONFIG["ai_custom_personas"][key] với cờ '_override': True, kế thừa
    name/emoji/trigger gốc nếu override chưa có."""
    key = key.strip().lower()
    all_personas = get_all_personas()
    if key not in all_personas:
        return False, f"Không tìm thấy nhân vật với key `{key}`."
    customs = _get_custom_personas()
    if key in customs:
        customs[key]["prompt"] = new_prompt.strip()
    else:
        base = AI_PERSONA_PRESETS[key]
        customs[key] = {
            "name": base["name"], "emoji": base["emoji"], "trigger": base.get("trigger", key),
            "prompt": new_prompt.strip(), "memory": "", "_override": True,
        }
    save_config(CONFIG)
    return True, ""

def add_persona_memory(key: str, memory_text: str) -> tuple[bool, str]:
    """Thêm một dòng ghi nhớ (memory) vào nhân vật — được nối thêm vào cuối
    system prompt mỗi lần gọi AI, KHÔNG ghi đè prompt gốc. Dùng để dạy AI nhớ
    thông tin lâu dài (vd: tên người dùng thích gọi, sự kiện cố định...) mà
    không cần viết lại toàn bộ prompt nhân vật."""
    key = key.strip().lower()
    all_personas = get_all_personas()
    if key not in all_personas:
        return False, f"Không tìm thấy nhân vật với key `{key}`."
    customs = _get_custom_personas()
    if key not in customs:
        base = AI_PERSONA_PRESETS[key]
        customs[key] = {
            "name": base["name"], "emoji": base["emoji"], "trigger": base.get("trigger", key),
            "prompt": base["prompt"], "memory": "", "_override": True,
        }
    existing = customs[key].get("memory", "")
    line = memory_text.strip()
    customs[key]["memory"] = (existing + "\n- " + line).strip("\n") if existing else "- " + line
    save_config(CONFIG)
    return True, ""

def clear_persona_memory(key: str) -> tuple[bool, str]:
    key = key.strip().lower()
    customs = _get_custom_personas()
    if key not in customs or not customs[key].get("memory"):
        return False, f"Nhân vật `{key}` chưa có ghi nhớ nào."
    customs[key]["memory"] = ""
    save_config(CONFIG)
    return True, ""

def set_persona_triggers(key: str, triggers: list[str]) -> tuple[bool, str]:
    """Đặt lại danh sách trigger (từ gọi bot thay @username) cho 1 nhân vật —
    built-in hay custom đều sửa được. Built-in bị sửa thì override tương tự
    edit_persona_prompt()/add_persona_memory(): tạo bản riêng trong
    CONFIG['ai_custom_personas'], giữ nguyên prompt/memory gốc, chỉ thay
    trigger. _get_alt_triggers() (utils.py) đọc field này mỗi khi cần khớp
    tin nhắn trong nhóm — không cần khởi động lại bot."""
    key = key.strip().lower()
    all_personas = get_all_personas()
    if key not in all_personas:
        return False, f"Không tìm thấy nhân vật với key `{key}`."
    clean: list[str] = []
    seen: set = set()
    for t in triggers:
        t = t.strip().lower()
        if t and t not in seen:
            seen.add(t)
            clean.append(t)
    if not clean:
        return False, "Cần ít nhất 1 từ trigger hợp lệ."
    customs = _get_custom_personas()
    if key in customs:
        customs[key]["trigger"] = clean
    else:
        base = AI_PERSONA_PRESETS[key]
        customs[key] = {
            "name": base["name"], "emoji": base["emoji"], "trigger": clean,
            "prompt": base["prompt"], "memory": "", "_override": True,
        }
    save_config(CONFIG)
    return True, ""


# ── Key helpers ───────────────────────────────────────────────────
def _get_all_ai_keys() -> list:
    keys = []
    primary = CONFIG.get("gemini_api_key", "").strip()
    if primary:
        keys.append(primary)
    for k in CONFIG.get("gemini_api_keys_extra", []):
        k = k.strip()
        if k and k not in keys:
            keys.append(k)
    return keys

# ── System prompt builder ─────────────────────────────────────────
def _gemini_get_sys_prompt() -> str:
    from config import now_vn
    persona_key = CONFIG.get("ai_persona", "custom")
    all_personas = get_all_personas()
    persona     = all_personas.get(persona_key)

    if persona and persona_key != "custom" and persona.get("prompt"):
        base_prompt = persona["prompt"]
        memory = persona.get("memory", "")
        if memory:
            base_prompt = (
                base_prompt
                + "\n\n[GHI NHỚ RIÊNG - LUÔN NHỚ NHỮNG ĐIỀU SAU]\n"
                + memory
            )
    else:
        base_prompt = CONFIG.get("ai_system_prompt", "")

    now = now_vn()
    time_instruction = (
        "\n\n[THONG TIN THOI GIAN THUC]\n"
        "Thoi gian hien tai (gio Viet Nam UTC+7): "
        + now.strftime("%H:%M:%S, ngay %d thang %m nam %Y") + " (ICT)\n"
        "Khi duoc hoi ve gio/ngay, dung thong tin nay."
    )

    if base_prompt:
        return base_prompt + _EXPERTISE_INSTRUCTION + time_instruction
    return _EXPERTISE_INSTRUCTION.strip() + time_instruction


# ── Thống kê: đếm số lượt AI đã trả lời (hiện trong /stats) ───────
def _bump_ai_stats() -> None:
    try:
        stats = CONFIG.setdefault("stats", {})
        stats["ai_messages"] = stats.get("ai_messages", 0) + 1
        # Không save_config() mỗi lần (tốn I/O) — chỉ lưu định kỳ mỗi 20 lượt;
        # số liệu vẫn được lưu đầy đủ khi có save_config() khác xảy ra trước đó.
        if stats["ai_messages"] % 20 == 0:
            save_config(CONFIG)
    except Exception:
        pass

# ── Helper: chuyển AI_HISTORY (list[{"role","parts":[str]}]) sang
#    list[types.Content] theo định dạng SDK mới ───────────────────
def _history_to_contents(history: list) -> list:
    if not genai_types:
        return []
    contents = []
    for h in history:
        parts_raw = h.get("parts", []) or []
        text_parts = [genai_types.Part.from_text(text=str(p)) for p in parts_raw if str(p).strip()]
        if text_parts:
            role = h.get("role", "user")
            # SDK mới chỉ chấp nhận role "user"/"model" cho lịch sử chat
            if role not in ("user", "model"):
                role = "user"
            contents.append(genai_types.Content(role=role, parts=text_parts))
    return contents

def _is_quota_error(e: Exception, err_lower: str) -> bool:
    if getattr(e, "code", None) == 429:
        return True
    return any(x in err_lower for x in [
        "quota", "rate", "limit", "429", "resource_exhausted", "resource exhausted",
        "too many requests", "exceeded",
    ])

def _is_model_error(err_lower: str) -> bool:
    return any(x in err_lower for x in ["not found", "invalid", "model", "404"])

# ── Single-key attempt ────────────────────────────────────────────
async def _gemini_chat_with_key(
    key: str, model_name: str, sys_prompt: str, history: list, user_text: str
) -> str:
    client = genai.Client(api_key=key)
    config_kwargs = {"system_instruction": sys_prompt if sys_prompt else None}
    thinking_cfg = _build_thinking_config(model_name)
    if thinking_cfg is not None:
        config_kwargs["thinking_config"] = thinking_cfg
    config = genai_types.GenerateContentConfig(**config_kwargs)
    chat_session = client.chats.create(
        model=model_name,
        history=_history_to_contents(history),
        config=config,
    )
    response = await asyncio.to_thread(chat_session.send_message, user_text)
    return response.text

# ── Main chat function ────────────────────────────────────────────
async def _gemini_chat(chat_id: int, user_text: str) -> str:
    global _ai_key_index

    if not GEMINI_AVAILABLE:
        return _MISSING_LIB_MSG

    all_keys = _get_all_ai_keys()
    if not all_keys:
        return "⚠️ *Chưa có API key!* Dùng `/aikey KEY` để đặt ạ~"

    model_name = CONFIG.get("gemini_model")
    sys_prompt = _gemini_get_sys_prompt()
    history    = AI_HISTORY.get(chat_id, [])

    tried_keys = set()
    last_err   = ""
    start_idx  = _ai_key_index % len(all_keys)

    for offset in range(len(all_keys)):
        key_idx = (start_idx + offset) % len(all_keys)
        key     = all_keys[key_idx]
        if key in tried_keys:
            continue
        tried_keys.add(key)
        try:
            reply_text = await _gemini_chat_with_key(
                key, model_name, sys_prompt, history, user_text
            )
            _ai_key_index = (key_idx + 1) % len(all_keys)
            history.append({"role": "user",  "parts": [user_text]})
            history.append({"role": "model", "parts": [reply_text]})
            if len(history) > AI_MAX_HISTORY * 2:
                history = history[-(AI_MAX_HISTORY * 2):]
            AI_HISTORY[chat_id] = history
            db = _get_db()
            if db: db.save_history(chat_id, history)
            _bump_ai_stats()
            return reply_text

        except Exception as e:
            err      = str(e).lower()
            last_err = str(e)
            if _is_quota_error(e, err):
                continue
            if _is_model_error(err):
                for fallback in GEMINI_MODELS_FREE:
                    if fallback == model_name:
                        continue
                    try:
                        reply_text = await _gemini_chat_with_key(
                            key, fallback, sys_prompt, history, user_text
                        )
                        _ai_key_index = (key_idx + 1) % len(all_keys)
                        history.append({"role": "user",  "parts": [user_text]})
                        history.append({"role": "model", "parts": [reply_text]})
                        if len(history) > AI_MAX_HISTORY * 2:
                            history = history[-(AI_MAX_HISTORY * 2):]
                        AI_HISTORY[chat_id] = history
                        db = _get_db()
                        if db: db.save_history(chat_id, history)
                        _bump_ai_stats()
                        return f"_(Model dự phòng: {fallback})_\n{reply_text}"
                    except Exception:
                        continue
            break

    total_keys = len(all_keys)
    if total_keys > 1 and "quota" in last_err.lower():
        return f"⚠️ *Tất cả {total_keys} API key đã hết quota!* Thêm bằng `/aiaddkey` ạ~"
    return f"❌ *Lỗi Gemini:* `{last_err[:200]}`"


# ── GOOGLE SEARCH GROUNDING (gốc) + URL CONTEXT ───────────────────
# Thay cho scrape DDG/Bing tự viết khi tìm thông tin/tin tức web: dùng tool
# chính chủ của Gemini — nhanh hơn, chính xác hơn, tự kèm nguồn trích dẫn.
# Facebook và YouTube vẫn dùng scrape riêng trong search_module.py (Search
# Grounding không scrape được Facebook, và không đảm bảo trả đúng link
# video cụ thể nên giữ pipeline scrape thật cho YouTube).
#
# URL Context: khi người dùng dán link vào tin nhắn, bật kèm tool này để
# Gemini tự đọc thẳng nội dung trang — không cần code scrape riêng.
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s()<>\[\]]+[^\s()<>\[\].,;:!?'\"]")


async def _gemini_chat_with_key_grounded(
    key: str, model_name: str, sys_prompt: str, history: list, user_text: str,
) -> tuple[str, list]:
    """1 lượt chat có Google Search Grounding + URL Context (nếu user_text
    chứa link). Trả về (reply_text, source_list) — source_list là danh sách
    {"title","url"} các nguồn Gemini đã thực sự dùng để trả lời (để hiển thị
    minh bạch nguồn, không bịa nguồn không có thật)."""
    tools = [genai_types.Tool(google_search=genai_types.GoogleSearch())]
    if _URL_IN_TEXT_RE.search(user_text):
        tools.append(genai_types.Tool(url_context=genai_types.UrlContext()))

    config_kwargs = {
        "system_instruction": sys_prompt if sys_prompt else None,
        "tools": tools,
    }
    thinking_cfg = _build_thinking_config(model_name)
    if thinking_cfg is not None:
        config_kwargs["thinking_config"] = thinking_cfg

    client = genai.Client(api_key=key)
    config = genai_types.GenerateContentConfig(**config_kwargs)
    chat_session = client.chats.create(
        model=model_name,
        history=_history_to_contents(history),
        config=config,
    )
    response = await asyncio.to_thread(chat_session.send_message, user_text)

    sources: list = []
    try:
        gm = response.candidates[0].grounding_metadata
        if gm and gm.grounding_chunks:
            for chunk in gm.grounding_chunks:
                web = getattr(chunk, "web", None)
                if web and getattr(web, "uri", None):
                    sources.append({"title": getattr(web, "title", "") or "", "url": web.uri})
    except Exception:
        pass

    reply_text = (response.text or "").strip()
    if not reply_text:
        # response.text rỗng vì response chỉ chứa part 'function_call' (SDK
        # in warning "there are non-text parts ... function_call" rồi trả
        # về chuỗi rỗng) — KHÔNG được coi "" là câu trả lời hợp lệ. Nếu để
        # lọt xuống dưới, "" sẽ bị lưu vào AI_HISTORY làm 1 lượt "model"
        # rỗng; lượt rỗng đó bị _history_to_contents() lọc bỏ ở lần gọi
        # sau, khiến 2 lượt "user" liên tiếp không có "model" xen giữa →
        # Gemini từ chối luôn các lượt chat tiếp theo trong cùng đoạn hội
        # thoại ("về sau cũng thế"). Raise ở đây để caller không lưu lịch
        # sử và tự fallback sang DDG/Bing search.
        raise RuntimeError(
            "Gemini không trả về nội dung văn bản (grounding trả về function_call chưa xử lý)."
        )

    return reply_text, sources


async def _gemini_chat_with_grounding(chat_id: int, user_text: str) -> str:
    """Wrapper Search Grounding: tương tự _gemini_chat nhưng dùng Google
    Search Grounding gốc thay vì scrape DDG/Bing. Nguồn trích dẫn thật được
    Gemini tự kèm — KHÔNG hiển thị link nguồn cho người dùng theo yêu cầu
    văn phong tự nhiên (giữ đúng hành vi cũ: không liệt kê link/nguồn thô),
    chỉ dùng nguồn để hậu kiểm (tương tự cơ chế chống bịa link agent mode).
    Khi ai_grounding=False, fallback về _gemini_chat thường (không search)."""
    if not CONFIG.get("ai_grounding", True):
        return await _gemini_chat(chat_id, user_text)

    global _ai_key_index

    if not GEMINI_AVAILABLE:
        return _MISSING_LIB_MSG

    all_keys = _get_all_ai_keys()
    if not all_keys:
        return "⚠️ *Chưa có API key!* Dùng `/aikey KEY` để đặt ạ~"

    model_name = CONFIG.get("gemini_model")
    sys_prompt = _gemini_get_sys_prompt()
    history    = AI_HISTORY.get(chat_id, [])

    tried_keys: set = set()
    last_err   = ""
    start_idx  = _ai_key_index % len(all_keys)

    for offset in range(len(all_keys)):
        key_idx = (start_idx + offset) % len(all_keys)
        key     = all_keys[key_idx]
        if key in tried_keys:
            continue
        tried_keys.add(key)
        try:
            reply_text, _sources = await _gemini_chat_with_key_grounded(
                key, model_name, sys_prompt, history, user_text
            )
            _ai_key_index = (key_idx + 1) % len(all_keys)
            history.append({"role": "user",  "parts": [user_text]})
            history.append({"role": "model", "parts": [reply_text]})
            if len(history) > AI_MAX_HISTORY * 2:
                history = history[-(AI_MAX_HISTORY * 2):]
            AI_HISTORY[chat_id] = history
            db = _get_db()
            if db: db.save_history(chat_id, history)
            _bump_ai_stats()
            return reply_text

        except Exception as e:
            err      = str(e).lower()
            last_err = str(e)
            if _is_quota_error(e, err):
                continue
            if _is_model_error(err):
                for fallback in GEMINI_MODELS_FREE:
                    if fallback == model_name:
                        continue
                    try:
                        reply_text, _sources = await _gemini_chat_with_key_grounded(
                            key, fallback, sys_prompt, history, user_text
                        )
                        _ai_key_index = (key_idx + 1) % len(all_keys)
                        history.append({"role": "user",  "parts": [user_text]})
                        history.append({"role": "model", "parts": [reply_text]})
                        if len(history) > AI_MAX_HISTORY * 2:
                            history = history[-(AI_MAX_HISTORY * 2):]
                        AI_HISTORY[chat_id] = history
                        db = _get_db()
                        if db: db.save_history(chat_id, history)
                        _bump_ai_stats()
                        return f"_(Model dự phòng: {fallback})_\n{reply_text}"
                    except Exception:
                        continue
            # Grounding không khả dụng (model/SDK lỗi cú pháp tool) → fallback
            # an toàn về chat thường, không để người dùng nhận lỗi kỹ thuật.
            break

    total_keys = len(all_keys)
    if total_keys > 1 and "quota" in last_err.lower():
        # Grounding hết quota tất cả key → fallback DDG/Bing scrape (không tốn quota Grounding)
        vlog("Grounding hết quota, fallback _gemini_chat_with_ddg_search")
        return await _gemini_chat_with_ddg_search(chat_id, user_text)
    vlog(f"_gemini_chat_with_grounding lỗi, fallback DDG search: {last_err[:200]}")
    return await _gemini_chat_with_ddg_search(chat_id, user_text)


# ── DDG/Bing MULTI-SOURCE SEARCH (thay thế Grounding khi hết quota) ──────────
#
# Chiến lược: scrape DDG + Bing + Google News song song (giống _search_google
# trong search_module), inject kết quả vào system prompt rồi gọi Gemini bình
# thường — KHÔNG tốn quota Grounding, không giới hạn lượt/ngày.
#
# Khi nào trigger:
#   • ai_grounding=True  → _gemini_chat_with_grounding (Google Search Grounding)
#                          nếu quota hết → tự fallback vào _gemini_chat_with_ddg_search
#   • ai_grounding=False → _gemini_chat_with_ddg_search trực tiếp (không cần
#                          quota Grounding)
#
# Số trang scrape: DDG-vi + Bing-vi + DDG-en + Bing-en + Google News ×2 chạy
# song song → tổng tối đa ~40 kết quả thô, lọc trùng còn ~20 snippet chất
# lượng nhất được đưa vào context.

_SEARCH_TRIGGER_RE = re.compile(
    r"(?:"
    # Câu hỏi về thông tin mới / hiện tại
    r"tin\s*tức|mới\s*nhất|hôm\s*nay|hôm\s*qua|tuần\s*này|tháng\s*này|năm\s*nay"
    r"|latest|recent|news|today|current|update|now|just|trending"
    # Câu hỏi tìm kiếm tường minh
    r"|tìm\s*kiếm|tìm\s*hiểu|tra\s*cứu|search|look\s*up|find\s*out"
    # Câu hỏi về sự kiện / xảy ra
    r"|xảy\s*ra|diễn\s*ra|sự\s*kiện|event|happen|occurred"
    # Câu hỏi về giá / tỉ giá / thời tiết
    r"|giá\s*(?:vàng|đô|usd|bitcoin|btc|xăng|điện)|tỉ\s*giá|exchange\s*rate"
    r"|thời\s*tiết|dự\s*báo|weather|forecast"
    # Câu hỏi về người / tổ chức / sản phẩm cụ thể
    r"|(?:ai\s*(?:là|vừa|đã|đang))|(?:công\s*ty|doanh\s*nghiệp|tập\s*đoàn)"
    r"|(?:ra\s*mắt|phát\s*hành|launch|release|announce)"
    r")",
    re.IGNORECASE,
)

# Từ chỉ câu hỏi (nếu có kèm _SEARCH_TRIGGER_RE → càng chắc cần search)
_QUESTION_RE = re.compile(
    r"^(?:what|who|when|where|how|why|is|are|was|were|did|does|do|có|là|khi\s*nào|ở\s*đâu|tại\s*sao|bao\s*nhiêu|như\s*thế\s*nào)\b"
    r"|[?？]",
    re.IGNORECASE,
)


def _should_ddg_search(user_text: str) -> bool:
    """Heuristic: có nên tự động scrape web trước khi trả lời không?
    Chỉ trigger khi câu hỏi liên quan đến thông tin mới/hiện tại — tránh
    search những câu hỏi thuần sáng tạo/toán/code/cá nhân."""
    t = user_text.strip()
    return bool(_SEARCH_TRIGGER_RE.search(t))


async def _ddg_fetch_results(query: str, max_per_source: int = 10) -> list[dict]:
    """Scrape DDG + Bing + Google News song song, trả về list kết quả đã gộp
    và loại trùng. Mỗi item: {title, url, snippet, source?}"""
    import httpx
    from urllib.parse import unquote

    _UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    def _strip(raw: str) -> str:
        text = re.sub(r"<[^>]+>", " ", raw)
        text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                    .replace("&quot;", '"').replace("&#039;", "'")
                    .replace("&lt;", "<").replace("&gt;", ">")
                    .replace("&#x27;", "'").replace("&apos;", "'"))
        return re.sub(r"\s{2,}", " ", text).strip()

    def _unwrap_ddg(url: str) -> str:
        if "uddg=" in url:
            m = re.search(r"uddg=([^&]+)", url)
            if m:
                return unquote(m.group(1))
        return ("https:" + url) if url.startswith("//") else url

    async def _ddg(client: httpx.AsyncClient, locale: str) -> list:
        try:
            resp = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query, "kl": locale},
                headers={"User-Agent": _UA},
                timeout=9,
            )
            links    = re.findall(r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>', resp.text, re.DOTALL)
            snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
            out = []
            for i, (url, title) in enumerate(links[:max_per_source]):
                t = _strip(title)
                if t:
                    out.append({"title": t, "url": _unwrap_ddg(url),
                                "snippet": _strip(snippets[i]) if i < len(snippets) else "",
                                "source": f"DDG-{locale}"})
            return out
        except Exception:
            return []

    async def _bing(client: httpx.AsyncClient, mkt: str, lang: str) -> list:
        try:
            resp = await client.get(
                "https://www.bing.com/search",
                params={"q": query, "count": str(max_per_source), "mkt": mkt, "setlang": lang},
                headers={"User-Agent": _UA, "Accept-Language": f"{lang},;q=0.9"},
                timeout=9,
            )
            titles   = re.findall(r'<h2[^>]*><a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
            snippets = re.findall(r'<p class="b_lineclamp[^"]*"[^>]*>(.*?)</p>', resp.text, re.DOTALL)
            out = []
            for i, (url, title) in enumerate(titles[:max_per_source]):
                if "bing.com" in url and "/search" in url:
                    continue
                t = _strip(title)
                if t and url.startswith("http"):
                    out.append({"title": t, "url": url,
                                "snippet": _strip(snippets[i]) if i < len(snippets) else "",
                                "source": f"Bing-{lang}"})
            return out
        except Exception:
            return []

    async def _news(client: httpx.AsyncClient) -> list:
        """Google News RSS song ngữ — mới nhất lên đầu."""
        import xml.etree.ElementTree as ET
        import email.utils as _eu

        async def _feed(hl: str, gl: str, ceid: str) -> list:
            try:
                resp = await client.get(
                    "https://news.google.com/rss/search",
                    params={"q": query, "hl": hl, "gl": gl, "ceid": ceid},
                    timeout=9,
                )
                root = ET.fromstring(resp.content)
                items = []
                for item in root.findall(".//item"):
                    title = _strip(item.findtext("title") or "")
                    link  = (item.findtext("link") or "").strip()
                    pub   = (item.findtext("pubDate") or "").strip()
                    src_el = item.find("source")
                    src    = (src_el.text or "").strip() if src_el is not None else ""
                    if src and title.endswith(f" - {src}"):
                        title = title[:-(len(src) + 3)].strip()
                    ts = 0
                    try:
                        ts = _eu.parsedate_to_datetime(pub).timestamp()
                    except Exception:
                        pass
                    if title and link:
                        items.append({"title": title, "url": link,
                                      "snippet": f"{src} · {pub}" if src else pub,
                                      "source": f"News-{hl}", "_ts": ts})
                return items
            except Exception:
                return []

        vi, en = await asyncio.gather(_feed("vi", "VN", "VN:vi"), _feed("en", "US", "US:en"))
        seen: set = set()
        merged = []
        for it in vi + en:
            url = it.get("url", "")
            if url and url not in seen:
                seen.add(url)
                merged.append(it)
        merged.sort(key=lambda x: x.get("_ts", 0), reverse=True)
        for it in merged:
            it.pop("_ts", None)
        return merged[:max_per_source]

    async with httpx.AsyncClient(follow_redirects=True, timeout=9,
                                  headers={"User-Agent": _UA}) as client:
        results = await asyncio.gather(
            _ddg(client, "vn-vi"),
            _ddg(client, "us-en"),
            _bing(client, "vi-VN", "vi"),
            _bing(client, "en-US", "en"),
            _news(client),
            return_exceptions=True,
        )

    # Gộp: News trước (mới nhất), rồi vi, rồi en — loại trùng URL
    news_res  = results[4] if not isinstance(results[4], Exception) else []
    vi_res    = (results[0] if not isinstance(results[0], Exception) else []) + \
                (results[2] if not isinstance(results[2], Exception) else [])
    en_res    = (results[1] if not isinstance(results[1], Exception) else []) + \
                (results[3] if not isinstance(results[3], Exception) else [])

    seen_urls: set = set()
    combined: list = []
    for item in news_res + vi_res + en_res:
        url = item.get("url", "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        combined.append(item)

    return combined[:20]  # tối đa 20 snippet đưa vào context


def _format_search_context(results: list[dict]) -> str:
    """Chuyển list kết quả scrape thành khối text ngắn gọn để inject vào prompt."""
    if not results:
        return ""
    lines = ["[KẾT QUẢ TÌM KIẾM WEB - cập nhật từ DDG/Bing/Google News]"]
    for i, r in enumerate(results, 1):
        title   = r.get("title", "").strip()
        url     = r.get("url", "").strip()
        snippet = r.get("snippet", "").strip()
        src     = r.get("source", "")
        parts = [f"{i}. {title}"]
        if snippet:
            parts.append(f"   {snippet}")
        if url:
            parts.append(f"   🔗 {url}")
        if src:
            parts[-1] += f"  [{src}]"
        lines.append("\n".join(parts))
    lines.append(
        "\n[Hướng dẫn: Dựa vào các kết quả trên để trả lời. "
        "Chỉ đưa link khi link đó CÓ TRONG danh sách kết quả. "
        "Không bịa thêm thông tin, không tự thêm link không có trong kết quả.]"
    )
    return "\n\n".join(lines)


async def _gemini_chat_with_ddg_search(chat_id: int, user_text: str) -> str:
    """Chat có tự động scrape DDG/Bing/News khi câu hỏi cần thông tin mới.

    Flow:
      1. Kiểm tra heuristic _should_ddg_search() — nếu không cần → _gemini_chat thường
      2. Scrape song song DDG-vi + DDG-en + Bing-vi + Bing-en + Google News ×2
      3. Inject kết quả (~20 snippet) vào system prompt dưới dạng context
      4. Gọi Gemini bình thường (không tốn quota Grounding)
      5. Lịch sử hội thoại được giữ nguyên như _gemini_chat
    """
    global _ai_key_index

    if not GEMINI_AVAILABLE:
        return _MISSING_LIB_MSG

    all_keys = _get_all_ai_keys()
    if not all_keys:
        return "⚠️ *Chưa có API key!* Dùng `/aikey KEY` để đặt ạ~"

    # Nếu câu hỏi không cần search → dùng chat thường để nhanh hơn
    if not _should_ddg_search(user_text):
        return await _gemini_chat(chat_id, user_text)

    model_name = CONFIG.get("gemini_model")
    sys_prompt = _gemini_get_sys_prompt()
    history    = AI_HISTORY.get(chat_id, [])

    # Scrape web song song (không chặn response quá lâu — timeout 9s bên trong)
    try:
        search_results = await asyncio.wait_for(
            _ddg_fetch_results(user_text, max_per_source=10),
            timeout=12,
        )
    except Exception:
        search_results = []

    search_ctx = _format_search_context(search_results)

    # Inject search context vào system prompt (prepend, không ghi đè persona)
    if search_ctx:
        augmented_sys = f"{search_ctx}\n\n{'─'*40}\n\n{sys_prompt}" if sys_prompt else search_ctx
    else:
        augmented_sys = sys_prompt  # search thất bại → chat thường

    tried_keys: set = set()
    last_err   = ""
    start_idx  = _ai_key_index % len(all_keys)

    for offset in range(len(all_keys)):
        key_idx = (start_idx + offset) % len(all_keys)
        key     = all_keys[key_idx]
        if key in tried_keys:
            continue
        tried_keys.add(key)
        try:
            reply_text = await _gemini_chat_with_key(
                key, model_name, augmented_sys, history, user_text
            )
            _ai_key_index = (key_idx + 1) % len(all_keys)
            history.append({"role": "user",  "parts": [user_text]})
            history.append({"role": "model", "parts": [reply_text]})
            if len(history) > AI_MAX_HISTORY * 2:
                history = history[-(AI_MAX_HISTORY * 2):]
            AI_HISTORY[chat_id] = history
            db = _get_db()
            if db:
                db.save_history(chat_id, history)
            _bump_ai_stats()
            # Gắn nhãn nhỏ để người dùng biết đang dùng DDG search
            src_count = len(search_results)
            if src_count > 0:
                return f"_{src_count} nguồn web_ 🌐\n{reply_text}"
            return reply_text

        except Exception as e:
            err      = str(e).lower()
            last_err = str(e)
            if _is_quota_error(e, err):
                continue
            if _is_model_error(err):
                for fallback in GEMINI_MODELS_FREE:
                    if fallback == model_name:
                        continue
                    try:
                        reply_text = await _gemini_chat_with_key(
                            key, fallback, augmented_sys, history, user_text
                        )
                        _ai_key_index = (key_idx + 1) % len(all_keys)
                        history.append({"role": "user",  "parts": [user_text]})
                        history.append({"role": "model", "parts": [reply_text]})
                        if len(history) > AI_MAX_HISTORY * 2:
                            history = history[-(AI_MAX_HISTORY * 2):]
                        AI_HISTORY[chat_id] = history
                        db = _get_db()
                        if db:
                            db.save_history(chat_id, history)
                        _bump_ai_stats()
                        src_count = len(search_results)
                        label = f"_{src_count} nguồn web_ 🌐  _(Model dự phòng: {fallback})_\n"
                        return label + reply_text
                    except Exception:
                        continue
            break

    total_keys = len(all_keys)
    if total_keys > 1 and "quota" in last_err.lower():
        return f"⚠️ *Tất cả {total_keys} API key đã hết quota!* Thêm bằng `/aiaddkey` ạ~"
    return f"❌ *Lỗi Gemini:* `{last_err[:200]}`"


AGENT_MAX_TOOL_STEPS = 5

_AGENT_REPORT_INSTRUCTION = (
    "\n\n[CHỈ THỊ BÁO CÁO HÀNH ĐỘNG - LUÔN TUÂN THỦ]\n"
    "Bạn vừa thực thi (hoặc thử thực thi) một hoặc nhiều hành động quản trị nhóm thông qua "
    "function calling. Khi báo cáo lại kết quả cho chủ nhân:\n"
    "1. Phải báo cáo ĐÚNG SỰ THẬT theo kết quả thực tế (ok=true/false, lỗi cụ thể nếu có) — "
    "TUYỆT ĐỐI không tự suy diễn là đã thành công nếu kết quả trả về ok=false, và không bỏ "
    "sót hành động nào đã thực hiện.\n"
    "2. Nếu một hành động thất bại (ok=false), nói rõ lý do thất bại, không né tránh hay "
    "làm mơ hồ đi.\n"
    "3. Tính cách/văn phong nhân vật chỉ áp dụng cho CÁCH DIỄN ĐẠT câu báo cáo (xưng hô, "
    "ngữ điệu, emoji...) — nội dung báo cáo (đã làm gì, kết quả gì, số liệu) phải chính xác "
    "100%, rõ ràng, không thêm thắt, không bịa thông tin không có trong kết quả tool.\n"
    "4. Nếu chủ nhân yêu cầu hành động nhưng thiếu thông tin (vd: không rõ là ai), hãy hỏi "
    "lại rõ ràng thay vì đoán bừa hoặc gọi tool với thông tin sai.\n"
    "5. Khi kết quả tool có chứa URL/link (video, link mời, kết quả tìm kiếm...), PHẢI copy "
    "NGUYÊN VẸN, CHÍNH XÁC TỪNG KÝ TỰ link đó từ kết quả tool — TUYỆT ĐỐI không tự gõ lại, "
    "không thêm bớt ký tự, không tự đoán link. Hệ thống sẽ tự kiểm tra và xoá bỏ mọi link "
    "không khớp chính xác với dữ liệu tool thật."
)


_URL_RE = _URL_IN_TEXT_RE  # cùng pattern, dùng chung 1 regex cho mọi chỗ quét URL


def _fix_agent_reply_links(reply_text: str, tool_call_log: list) -> str:
    """Hậu kiểm NGHIÊM NGẶT mọi URL trong câu trả lời cuối của agent mode:
    chỉ giữ lại URL nếu khớp (hoặc gần khớp y hệt logic _fix_youtube_links
    của search_module) với một URL THẬT đã thu được từ kết quả tool đã gọi
    trong lượt này (search_youtube, search_web, get_invite_link,
    revoke_invite_link...). URL không khớp bất kỳ tool nào → coi là Gemini
    tự gõ lại sai hoặc bịa, xoá bỏ để không gửi link hỏng/giả cho owner.

    Không áp dụng nếu trong lượt này KHÔNG có tool nào trả về URL — khi đó
    để nguyên reply (không có dữ liệu nào để đối chiếu)."""
    if not reply_text or not tool_call_log:
        return reply_text

    valid_urls: set = set()
    for call in tool_call_log:
        result = call.get("result") or {}
        if not result.get("ok"):
            continue
        for v in result.get("videos", []) or []:
            if v.get("url"):
                valid_urls.add(v["url"])
        for r in result.get("results", []) or []:
            if r.get("url"):
                valid_urls.add(r["url"])
        for key in ("invite_link", "new_invite_link"):
            if result.get(key):
                valid_urls.add(result[key])

    if not valid_urls:
        return reply_text

    valid_yt_ids = {m.group(1) for u in valid_urls
                    for m in [re.search(r"v=([A-Za-z0-9_-]{11})", u)] if m}

    def _hamming_close(a: str, b: str, max_diff: int = 1) -> bool:
        if len(a) != len(b):
            return False
        return sum(1 for x, y in zip(a, b) if x != y) <= max_diff

    def _resolve(url: str):
        """Trả URL đúng để dùng (chính nó nếu khớp, bản sửa nếu gần khớp
        video YouTube, hoặc None nếu không khớp gì cả → cần xoá)."""
        if url in valid_urls:
            return url
        m = re.search(r"v=([A-Za-z0-9_-]{10,12})", url)
        if m:
            vid = m.group(1)
            for good_id in valid_yt_ids:
                if _hamming_close(vid, good_id, max_diff=1):
                    return f"https://www.youtube.com/watch?v={good_id}"
                if len(vid) == 10 and good_id[:10] == vid:
                    return f"https://www.youtube.com/watch?v={good_id}"
                if len(vid) == 12 and vid[:11] == good_id:
                    return f"https://www.youtube.com/watch?v={good_id}"
        return None

    def _sub(m: "re.Match") -> str:
        resolved = _resolve(m.group(0))
        return resolved if resolved else ""

    fixed = _URL_RE.sub(_sub, reply_text)
    fixed = re.sub(r"[ \t]+\n", "\n", fixed)
    fixed = re.sub(r"\n{3,}", "\n\n", fixed)
    return fixed.strip()


async def _gemini_chat_with_key_and_tools(
    key: str, model_name: str, sys_prompt: str, history: list, user_text: str,
    bot, chat_id: int, extra_context: str = "",
) -> tuple[str, list]:
    """Thực hiện 1 vòng chat có function-calling với 1 API key cụ thể.
    Trả về (reply_text, tool_call_log) — tool_call_log là list các dict mô tả
    hành động đã thực thi thật (để hiển thị debug nếu cần).

    Chỉ thêm built-in tool (URL Context) cùng function-calling khi model là
    Gemini 3 series — đây là model duy nhất hỗ trợ TRỘN built-in tool với
    custom function declarations trong cùng 1 request. Model < Gemini 3 gọi
    kèm sẽ bị API từ chối, nên với các model đó chỉ dùng function-calling
    thuần như cũ.

    `extra_context` (tuỳ chọn): khối bối cảnh nhóm (tin đang reply + lịch sử
    chat gần đây — xem `_build_group_ai_context` trong bot.py) được gắn vào
    system_instruction CHỈ CHO LƯỢT GỌI NÀY, không lưu vào `history` — nhờ
    vậy AI 'đọc' được hội thoại xung quanh như một thành viên bình thường mà
    không làm phình to lịch sử hội thoại đã lưu qua từng lượt."""
    agent_tools = _get_agent_tools()
    gemini_tools = agent_tools.build_gemini_tools(genai_types)
    # Chỉ Gemini 3 mới hỗ trợ trộn built-in tool (google_search, url_context)
    # với custom function declarations cùng lúc — các model cũ hơn sẽ lỗi 400.
    if _is_gemini3(model_name) and CONFIG.get("ai_grounding", True):
        gemini_tools.append(genai_types.Tool(google_search=genai_types.GoogleSearch()))
        if _URL_IN_TEXT_RE.search(user_text):
            gemini_tools.append(genai_types.Tool(url_context=genai_types.UrlContext()))
    elif _is_gemini3(model_name) and _URL_IN_TEXT_RE.search(user_text):
        gemini_tools.append(genai_types.Tool(url_context=genai_types.UrlContext()))

    full_sys_prompt = (sys_prompt or "") + (extra_context or "") + _AGENT_REPORT_INSTRUCTION
    config_kwargs = {
        "system_instruction": full_sys_prompt,
        "tools": gemini_tools,
    }
    thinking_cfg = _build_thinking_config(model_name)
    if thinking_cfg is not None:
        config_kwargs["thinking_config"] = thinking_cfg

    client = genai.Client(api_key=key)
    config = genai_types.GenerateContentConfig(**config_kwargs)
    chat_session = client.chats.create(
        model=model_name,
        history=_history_to_contents(history),
        config=config,
    )

    tool_call_log: list = []
    response = await asyncio.to_thread(chat_session.send_message, user_text)

    for _ in range(AGENT_MAX_TOOL_STEPS):
        # Kiểm tra cancel giữa mỗi bước — to_thread() không bị ngắt giữa
        # chừng, nhưng ta có thể thoát sớm ở điểm await tiếp theo.
        await asyncio.sleep(0)  # checkpoint: raise CancelledError nếu task đã bị cancel

        # Tìm function_call trong response hiện tại (nếu có)
        fn_calls = []
        try:
            for cand in response.candidates or []:
                for part in (cand.content.parts or []):
                    fc = getattr(part, "function_call", None)
                    if fc is not None:
                        fn_calls.append(fc)
        except Exception:
            fn_calls = []

        if not fn_calls:
            break  # Không còn tool nào cần gọi → đây là câu trả lời cuối

        fn_responses = []
        for fc in fn_calls:
            tool_name = fc.name
            raw_args = dict(fc.args or {})
            # Schema khai báo toàn bộ tham số dạng STRING — convert đúng kiểu
            # số nguyên cho các tham số cần int trước khi thực thi.
            for int_field in ("message_id", "topic_id"):
                if int_field in raw_args:
                    try:
                        raw_args[int_field] = int(str(raw_args[int_field]).strip() or "0")
                    except (ValueError, TypeError):
                        raw_args[int_field] = 0

            result = await agent_tools.execute_tool(tool_name, raw_args, bot=bot, chat_id=chat_id)
            tool_call_log.append({"tool": tool_name, "args": raw_args, "result": result})
            fn_responses.append(
                genai_types.Part.from_function_response(name=tool_name, response=result)
            )

        response = await asyncio.to_thread(chat_session.send_message, fn_responses)

    # Nếu vòng lặp thoát ra ở đây mà response HIỆN TẠI vẫn còn function_call
    # chưa xử lý (hết AGENT_MAX_TOOL_STEPS trong khi model vẫn muốn gọi thêm
    # tool, hoặc bước phát hiện fn_calls phía trên bị lỗi/swallow exception),
    # gọi thẳng response.text sẽ chỉ nhận "" kèm warning "non-text parts...
    # function_call" từ SDK — KHÔNG phải câu trả lời thật. Ép model chốt câu
    # trả lời bằng văn bản trước khi lấy final_text, để không bao giờ trả về
    # "" (chuỗi rỗng sẽ bị lưu vào AI_HISTORY, làm hỏng alternation user/model
    # cho mọi lượt chat sau đó trong cùng đoạn hội thoại — "về sau cũng thế").
    still_pending = False
    try:
        for cand in response.candidates or []:
            for part in (cand.content.parts or []):
                if getattr(part, "function_call", None) is not None:
                    still_pending = True
                    break
    except Exception:
        pass

    if still_pending:
        vlog(f"agent tool-loop hết {AGENT_MAX_TOOL_STEPS} bước mà vẫn còn "
             f"function_call chưa xử lý, action_chat_id={chat_id} — ép model trả lời text.")
        response = await asyncio.to_thread(
            chat_session.send_message,
            "Bạn đã dùng đủ công cụ ở lượt này rồi. Hãy trả lời NGAY bằng văn bản "
            "cho người dùng dựa trên kết quả tool đã có, KHÔNG gọi thêm tool/function nào nữa.",
        )

    final_text = (response.text or "").strip()
    if not final_text:
        raise RuntimeError(
            "Gemini không trả về nội dung văn bản (chỉ có function_call chưa xử lý)."
        )

    final_text = _fix_agent_reply_links(final_text, tool_call_log)
    return final_text, tool_call_log


async def _gemini_chat_with_tools(chat_id: int, user_text: str, bot, action_chat_id: int,
                                   extra_context: str = "") -> str:
    """Wrapper agent mode: chat thường (giữ persona/lịch sử) nhưng AI có thể
    tự gọi tool quản trị nhóm (`action_chat_id`) khi owner yêu cầu qua lời
    nói tự nhiên. `chat_id` dùng để lưu lịch sử hội thoại (như cũ); có thể
    khác `action_chat_id` khi owner đang chat trong DM nhưng muốn tác động
    lên nhóm đã setgroup (`/setgroup`).

    `extra_context`: khối bối cảnh nhóm (tin đang reply + lịch sử chat gần
    đây) do bot.py dựng sẵn — chỉ gắn vào system_instruction của lượt gọi
    này, KHÔNG lưu vào AI_HISTORY."""
    global _ai_key_index

    if not GEMINI_AVAILABLE:
        return _MISSING_LIB_MSG

    all_keys = _get_all_ai_keys()
    if not all_keys:
        return "⚠️ *Chưa có API key!* Dùng `/aikey KEY` để đặt ạ~"

    model_name = CONFIG.get("gemini_model")
    sys_prompt = _gemini_get_sys_prompt()
    history    = AI_HISTORY.get(chat_id, [])

    tried_keys: set = set()
    last_err   = ""
    start_idx  = _ai_key_index % len(all_keys)

    for offset in range(len(all_keys)):
        key_idx = (start_idx + offset) % len(all_keys)
        key     = all_keys[key_idx]
        if key in tried_keys:
            continue
        tried_keys.add(key)
        try:
            reply_text, tool_log = await _gemini_chat_with_key_and_tools(
                key, model_name, sys_prompt, history, user_text, bot, action_chat_id,
                extra_context=extra_context,
            )
            _ai_key_index = (key_idx + 1) % len(all_keys)
            history.append({"role": "user",  "parts": [user_text]})
            history.append({"role": "model", "parts": [reply_text]})
            if len(history) > AI_MAX_HISTORY * 2:
                history = history[-(AI_MAX_HISTORY * 2):]
            AI_HISTORY[chat_id] = history
            db = _get_db()
            if db: db.save_history(chat_id, history)
            _bump_ai_stats()
            if tool_log:
                vlog(f"agent tool_calls chat_id={action_chat_id}: "
                     f"{[(t['tool'], t['result'].get('ok')) for t in tool_log]}")
            return reply_text

        except Exception as e:
            err      = str(e).lower()
            last_err = str(e)
            if _is_quota_error(e, err):
                continue
            if _is_model_error(err):
                for fallback in GEMINI_MODELS_FREE:
                    if fallback == model_name:
                        continue
                    try:
                        reply_text, tool_log = await _gemini_chat_with_key_and_tools(
                            key, fallback, sys_prompt, history, user_text, bot, action_chat_id,
                            extra_context=extra_context,
                        )
                        _ai_key_index = (key_idx + 1) % len(all_keys)
                        history.append({"role": "user",  "parts": [user_text]})
                        history.append({"role": "model", "parts": [reply_text]})
                        if len(history) > AI_MAX_HISTORY * 2:
                            history = history[-(AI_MAX_HISTORY * 2):]
                        AI_HISTORY[chat_id] = history
                        db = _get_db()
                        if db: db.save_history(chat_id, history)
                        _bump_ai_stats()
                        return f"_(Model dự phòng: {fallback})_\n{reply_text}"
                    except Exception:
                        continue
            break

    total_keys = len(all_keys)
    if total_keys > 1 and "quota" in last_err.lower():
        return f"⚠️ *Tất cả {total_keys} API key đã hết quota!* Thêm bằng `/aiaddkey` ạ~"
    return f"❌ *Lỗi Gemini:* `{last_err[:200]}`"


# ── Media chung: ảnh / voice / video ngắn ─────────────────────────
# Một pipeline DUY NHẤT cho mọi loại media gửi trực tiếp vào Gemini (ảnh,
# voice message, video ngắn) — Gemini đa phương thức tự "nghe"/"xem" thẳng
# file gốc, không cần bước chuyển giọng nói → văn bản riêng, không cần code
# tách khung hình video riêng.
#
# Giới hạn thực tế cần biết:
#   - Audio/video gửi qua Part.from_bytes (inline) chỉ nên dùng cho file
#     nhỏ (~dưới 20MB) — đúng với voice message Telegram (luôn nhỏ) và clip
#     ngắn. Video dài/nặng sẽ cần Files API riêng (chưa làm trong bản này).
#   - Token tiêu tốn cho audio/video CAO hơn ảnh tĩnh rất nhiều (tính theo
#     từng giây), nên chỉ gọi khi người dùng thực sự gửi voice/video.

async def _gemini_chat_media(
    chat_id: int,
    media_parts: list,             # list[(bytes, mime_type)]
    prompt_text: str,
    history_label: str = "[Media]",
) -> str:
    """Pipeline chung: gửi 1 hoặc nhiều media part (ảnh/audio/video) + prompt
    text lên Gemini, có key-rotation + fallback model giống _gemini_chat()."""
    global _ai_key_index

    if not GEMINI_AVAILABLE:
        return _MISSING_LIB_MSG

    all_keys = _get_all_ai_keys()
    if not all_keys:
        return "⚠️ *Chưa có API key!* Dùng `/aikey KEY` để đặt ạ~"

    parts = [genai_types.Part.from_bytes(data=b, mime_type=m) for b, m in media_parts]
    model_name = CONFIG.get("gemini_model", "gemini-2.5-flash")
    sys_prompt = _gemini_get_sys_prompt()

    def _save_history(reply_text: str) -> None:
        history = AI_HISTORY.get(chat_id, [])
        history.append({"role": "user",  "parts": [f"{history_label} {prompt_text}"]})
        history.append({"role": "model", "parts": [reply_text]})
        if len(history) > AI_MAX_HISTORY * 2:
            history = history[-(AI_MAX_HISTORY * 2):]
        AI_HISTORY[chat_id] = history
        db = _get_db()
        if db: db.save_history(chat_id, history)

    tried_keys: set = set()
    last_err = ""
    start_idx = _ai_key_index % len(all_keys)

    async def _attempt(key: str, mdl: str):
        client = genai.Client(api_key=key)
        config = genai_types.GenerateContentConfig(
            system_instruction=sys_prompt if sys_prompt else None,
        )
        return await asyncio.to_thread(
            client.models.generate_content,
            model=mdl,
            contents=[*parts, prompt_text],
            config=config,
        )

    for offset in range(len(all_keys)):
        key_idx = (start_idx + offset) % len(all_keys)
        key = all_keys[key_idx]
        if key in tried_keys:
            continue
        tried_keys.add(key)
        try:
            response = await _attempt(key, model_name)
            _ai_key_index = (key_idx + 1) % len(all_keys)
            reply_text = response.text
            _save_history(reply_text)
            _bump_ai_stats()
            vlog(f"media OK chat_id={chat_id} kind={history_label} model={model_name}")
            return reply_text
        except Exception as e:
            err = str(e).lower()
            last_err = str(e)
            if _is_quota_error(e, err):
                continue
            is_model = any(x in err for x in ["not found", "invalid", "model", "unsupported", "404"])
            if is_model:
                for fallback in GEMINI_MODELS_FREE:
                    if fallback == model_name:
                        continue
                    try:
                        response = await _attempt(key, fallback)
                        _ai_key_index = (key_idx + 1) % len(all_keys)
                        reply_text = response.text
                        _save_history(reply_text)
                        _bump_ai_stats()
                        vlog(f"media OK (fallback) chat_id={chat_id} kind={history_label} model={fallback}")
                        return f"_(Model dự phòng: {fallback})_\n{reply_text}"
                    except Exception:
                        continue
            break

    total_keys = len(all_keys)
    vlog(f"media FAIL chat_id={chat_id} kind={history_label} err={last_err[:200]}")
    if total_keys > 1 and "quota" in last_err.lower():
        return f"⚠️ *Tất cả {total_keys} API key đã hết quota!* Thêm bằng `/aiaddkey` ạ~"
    return f"❌ *Lỗi Gemini:* `{last_err[:200]}`"


# ── Vision: phân tích hình ảnh (wrapper tương thích bản cũ, dùng pipeline
#    media chung ở trên) ───────────────────────────────────────────
async def _gemini_chat_vision(
    chat_id: int,
    image_bytes,
    mime_type: str = "image/jpeg",
    caption: str = "",
) -> str:
    """
    Gửi hình ảnh lên Gemini Vision và trả về phân tích/phản hồi.

    `image_bytes` có thể là:
      - bytes đơn (1 ảnh, dùng kèm `mime_type`)
      - list[tuple[bytes, str]]: nhiều ảnh, mỗi item là (bytes, mime_type)
    """
    if isinstance(image_bytes, (list, tuple)) and image_bytes and isinstance(image_bytes[0], (list, tuple)):
        raw_images = [(b, m or "image/jpeg") for b, m in image_bytes]
    else:
        raw_images = [(image_bytes, mime_type or "image/jpeg")]
    n_images = len(raw_images)

    _analyze_keywords = ["phân tích", "phan tich", "analyze", "analysis"]
    _wants_analysis = any(k in caption.lower() for k in _analyze_keywords)

    _question_keywords = [
        "?", "là gì", "là ai", "ai là", "cái gì", "gì vậy", "gì thế", "gì đây",
        "có gì", "ở đâu", "bao nhiêu", "vì sao", "tại sao", "như thế nào",
        "thế nào", "có phải", "đúng không", "không vậy", "có biết", "được không",
        "what", "who", "where", "why", "how", "which",
    ]
    _is_question = any(k in caption.lower() for k in _question_keywords)

    _vision_style_note = (
        " (Trả lời tự nhiên như đang trò chuyện, KHÔNG mô tả/phân tích chi tiết từng phần của ảnh — "
        "chỉ nêu suy nghĩ/cảm nhận/phản ứng của bạn về ảnh đó.)"
    )
    _question_note = (
        " (Đây là một câu hỏi — hãy quan sát ảnh và trả lời ĐẦY ĐỦ, ĐÚNG TRỌNG TÂM câu hỏi, "
        "dựa trên thông tin/nội dung thực tế có trong ảnh. Trả lời tự nhiên như đang trò chuyện, "
        "KHÔNG cần liệt kê/phân tích chi tiết toàn bộ bức ảnh — chỉ tập trung vào điều được hỏi.)"
    )
    _multi_note = f" (Có tổng cộng {n_images} ảnh được gửi cùng lúc, hãy xem xét tất cả.)" if n_images > 1 else ""

    if caption.strip():
        if _wants_analysis:
            prompt_text = caption.strip()
        elif _is_question:
            prompt_text = caption.strip() + _question_note
        else:
            prompt_text = caption.strip() + _vision_style_note
        prompt_text += _multi_note
    elif _wants_analysis:
        prompt_text = "Phân tích " + ("các hình ảnh này" if n_images > 1 else "hình ảnh này") + " chi tiết bằng tiếng Việt." + _multi_note
    else:
        subject = "những bức ảnh này" if n_images > 1 else "bức ảnh này"
        prompt_text = (
            f"Hãy xem {subject} và trả lời/nhận xét một cách tự nhiên như đang trò chuyện bình thường "
            "(theo đúng văn phong/nhân vật của bạn), KHÔNG mô tả hay phân tích chi tiết từng phần của ảnh "
            f"— chỉ nói lên suy nghĩ, cảm nhận hoặc phản ứng của bạn về {subject} bằng tiếng Việt." + _multi_note
        )

    return await _gemini_chat_media(chat_id, raw_images, prompt_text, history_label=f"[Ảnh x{n_images}]")


# ── Voice: nghe hiểu voice message trực tiếp (không cần speech-to-text
#    riêng — Gemini đa phương thức tự nghe file âm thanh gốc) ─────
async def _gemini_chat_voice(
    chat_id: int,
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
    caption: str = "",
) -> str:
    """Gửi voice message lên Gemini, AI tự nghe và trả lời theo đúng nội
    dung + ngữ điệu/ý định trong giọng nói (không phải chỉ transcript khô khan)."""
    prompt_text = caption.strip() if caption.strip() else (
        "Hãy nghe đoạn ghi âm này và trả lời/phản hồi một cách tự nhiên như đang trò chuyện "
        "bình thường (theo đúng văn phong/nhân vật của bạn), dựa trên đúng nội dung người dùng "
        "nói trong ghi âm."
    )
    return await _gemini_chat_media(chat_id, [(audio_bytes, mime_type or "audio/ogg")],
                                      prompt_text, history_label="[Voice]")


# ── Video: hiểu video ngắn gửi vào nhóm (clip, video note...) ─────
async def _gemini_chat_video(
    chat_id: int,
    video_bytes: bytes,
    mime_type: str = "video/mp4",
    caption: str = "",
) -> str:
    """Gửi video ngắn lên Gemini, AI tự xem (hình ảnh + âm thanh trong video)
    và trả lời/nhận xét. CHỈ dùng cho video NGẮN (vài chục giây tới ~1-2
    phút) gửi inline — video dài cần Files API riêng (chưa hỗ trợ)."""
    prompt_text = caption.strip() if caption.strip() else (
        "Hãy xem đoạn video này và trả lời/nhận xét một cách tự nhiên như đang trò chuyện bình "
        "thường (theo đúng văn phong/nhân vật của bạn), dựa trên đúng nội dung thực tế trong video."
    )
    return await _gemini_chat_media(chat_id, [(video_bytes, mime_type or "video/mp4")],
                                      prompt_text, history_label="[Video]")


# ── Phân tích văn bản (megumin phân tích + reply text) ────────────
async def _gemini_text_analyze(chat_id: int, target_text: str, extra_prompt: str = "") -> str:
    """
    Phân tích/tóm tắt một đoạn văn bản (vd: reply vào tin nhắn dài + 'megumin phân tích').
    Dùng lại _gemini_chat (text-only) với prompt được đóng gói sẵn.
    """
    instruction = extra_prompt.strip() if extra_prompt.strip() else (
        "Hãy phân tích/tóm tắt nội dung đoạn văn bản dưới đây một cách rõ ràng, mạch lạc bằng tiếng Việt "
        "(theo đúng văn phong/nhân vật của bạn)."
    )
    prompt = f"{instruction}\n\n---\n{target_text.strip()}\n---"
    vlog(f"text-analyze chat_id={chat_id} len={len(target_text)}")
    return await _gemini_chat(chat_id, prompt)


# ── Validate 1 API key (dùng cho /aikey) ──────────────────────────
async def _validate_api_key(key: str) -> tuple[bool, int, str]:
    """
    Thử xác thực 1 API key bằng cách list models qua SDK mới.
    Trả về (ok, số_model_khả_dụng, lỗi_nếu_có).
    """
    if not GEMINI_AVAILABLE:
        return False, 0, "Thiếu thư viện google-genai"
    try:
        client = genai.Client(api_key=key)
        models = await asyncio.to_thread(lambda: list(client.models.list()))
        return True, len(models), ""
    except Exception as e:
        return False, 0, str(e)
