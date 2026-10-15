"""
config.py — Cấu hình, mã hóa Fernet, load/save config và DB.
"""

import os
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Vietnam Timezone (UTC+7) ─────────────────────────────────────
VN_TZ = timezone(timedelta(hours=7))

def now_vn() -> datetime:
    """Trả về thời gian hiện tại theo giờ Việt Nam (UTC+7)."""
    return datetime.now(VN_TZ)

def fmt_vn(dt_str: str) -> str:
    """Chuyển ISO timestamp sang định dạng giờ VN dễ đọc."""
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_vn = dt.astimezone(VN_TZ)
        return dt_vn.strftime("%d/%m/%Y %H:%M (ICT)")
    except Exception:
        return dt_str[:16].replace("T", " ")

# ── Cryptography ─────────────────────────────────────────────────
try:
    from cryptography.fernet import Fernet, InvalidToken
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False

# ── Paths ────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
DATA_DIR    = BASE_DIR / "data"
CONFIG_FILE = DATA_DIR / "config.json"
DB_FILE     = DATA_DIR / "db.json"
KEY_FILE    = DATA_DIR / ".secret.key"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Default config ───────────────────────────────────────────────
DEFAULT_CONFIG: dict = {
    "token":            "PASTE_BOT_TOKEN_HERE",
    "owner_id":         0,
    "extra_owners":     [],     # owner phụ do owner chính thêm bằng /addowner <telegram_id>
                                # (quyền dùng bot như owner chính, trừ việc thêm/xoá owner)
    "ai_log_groups":    True,   # ghi lại tin nhắn MỌI nhóm bot có mặt để AI đọc bối cảnh/tra lịch sử
    "ai_ctx_msgs":      30,     # số tin gần nhất đính kèm làm bối cảnh mỗi lần AI trả lời trong nhóm (0 = tắt)
    "ai_ctx_chars":     6000,   # trần ký tự của khối bối cảnh nhóm
    "bot_username":     "",
    "target_group_id":  0,
    "proxy_mode":       False,
    "monitor_mode":     False,
    "monitor_keywords": [],
    "stats": {"total_messages": 0, "started_at": now_vn().isoformat(), "ai_messages": 0},
    "gemini_api_key":        "",
    "gemini_api_keys_extra": ["", ""],
    "gemini_model":          "gemini-3.5-flash",
    "ai_provider":           "gemini",   # "gemini" | "claude"
    "claude_api_key":        "",
    "claude_model":          "claude-haiku-5-5",
    "ai_mode":               True,
    "ai_persona":            "aemeth",
    "ai_custom_personas":    {},   # nhân vật do owner tự tạo: {key: {name, emoji, trigger, prompt, memory}}
    "search_debug": False,
    "ai_thinking_level":     "low",   # low|medium|high — chỉ áp dụng với Gemini 3.x
    "ai_grounding":          False,       # True = dùng Google Search Grounding gốc cho web/tin tức (Gemini 3.x), False = scrape DDG/Bing như cũ
    "msg_buffer_limit": 5000,    # số tin nhắn tối đa lưu mỗi nhóm trong msg_buffer
                                  # (tùy chỉnh qua /setbufferlimit thay vì sửa code)
    "fn_open":          False,   # /fn — True = mọi thành viên trong nhóm đều TRIGGER
                                  # được filter/note (gõ trúng từ khóa, #note); False
                                  # (mặc định) = chỉ owner trigger được. Tạo/sửa/xoá
                                  # filter/note luôn chỉ owner, không phụ thuộc cờ này.
}

# ── Fields được mã hóa ──────────────────────────────────────────
_SENSITIVE_FIELDS = [
    "token", "gemini_api_key", "gemini_api_keys_extra", "claude_api_key",
]

# ══════════════════════════════════════════════════════════════════
# ENCRYPTION
# ══════════════════════════════════════════════════════════════════

def _get_or_create_key() -> bytes:
    # Ưu tiên 1: env var do người dùng tự đặt (persistent qua mọi restart/redeploy
    # trên Render, khác với disk local bị reset) — dành cho ai muốn tự quản lý key.
    env_key = os.environ.get("CONFIG_FERNET_KEY", "").strip()
    if env_key:
        try:
            Fernet(env_key.encode())  # validate format hợp lệ
            return env_key.encode()
        except Exception:
            pass
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes().strip()
    key = Fernet.generate_key()
    KEY_FILE.write_bytes(key)
    try: KEY_FILE.chmod(0o600)
    except Exception: pass
    return key

def _get_fernet():
    if not CRYPTO_AVAILABLE: return None
    try: return Fernet(_get_or_create_key())
    except Exception: return None

def _enc(value, f):
    """Mã hóa str/list/dict → ENC:..."""
    if f is None: return value
    if isinstance(value, str):
        if not value or value.startswith("ENC:"): return value
        try: return "ENC:" + f.encrypt(value.encode()).decode()
        except Exception: return value
    if isinstance(value, list): return [_enc(v, f) for v in value]
    if isinstance(value, dict): return {k: _enc(v, f) for k, v in value.items()}
    return value

def _dec(value, f):
    """Giải mã ENC:... → plaintext"""
    if f is None: return value
    if isinstance(value, str):
        if not value.startswith("ENC:"): return value
        try: return f.decrypt(value[4:].encode()).decode()
        except Exception: return value
    if isinstance(value, list): return [_dec(v, f) for v in value]
    if isinstance(value, dict): return {k: _dec(v, f) for k, v in value.items()}
    return value

def _encrypt_config(cfg: dict, f) -> dict:
    out = cfg.copy()
    for field in _SENSITIVE_FIELDS:
        if field in out and out[field]:
            out[field] = _enc(out[field], f)
    return out

def _decrypt_config(cfg: dict, f) -> dict:
    out = cfg.copy()
    for field in _SENSITIVE_FIELDS:
        if field in out and out[field]:
            out[field] = _dec(out[field], f)
    return out

def normalize_owner_ids(raw) -> list:
    """Chuẩn hoá danh sách id owner (list/tuple/set/chuỗi 'id1,id2') → list[int]
    không trùng, bỏ giá trị không hợp lệ. Luôn trả về list MỚI."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.replace(";", ",").split(",")
    out = []
    for x in raw:
        try:
            v = int(str(x).strip())
        except (TypeError, ValueError):
            continue
        if v > 0 and v not in out:
            out.append(v)
    return out

# ── Persistence ──────────────────────────────────────────────────
def load_config() -> dict:
    f = _get_fernet()
    if CONFIG_FILE.exists():
        try:
            raw = json.loads(CONFIG_FILE.read_text("utf-8"))
            cfg = _decrypt_config(raw, f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            # setdefault ở trên không merge được vào *trong* dict con đã tồn tại
            # (vd "stats" đã có từ bản cũ nhưng thiếu field "ai_messages" mới) —
            # backfill riêng cho các dict con cần field mới.
            cfg.setdefault("stats", {}).setdefault("ai_messages", 0)
            cfg["extra_owners"] = normalize_owner_ids(cfg.get("extra_owners"))
            # Sửa giá trị bot_username="0" cũ (bug mặc định trước đây)
            if cfg.get("bot_username") == "0":
                cfg["bot_username"] = ""
            # Sửa key persona "Megumin" cũ (đổi sang "megumin_serious")
            if cfg.get("ai_persona") == "Megumin":
                cfg["ai_persona"] = "megumin_serious"
            # Dọn các field cũ (tính năng đã bị loại bỏ)
            for legacy_key in (
                "f95_username", "f95_password", "f95_cookies",   # f95 — bỏ từ trước
                "welcome_mode", "welcome_text",                    # welcome — đã xóa handler
                "antiflood_mode", "antiflood_limit", "antiflood_window",  # antiflood — đã xóa
                "report_mode",                                     # report — đã xóa handler
            ):
                cfg.pop(legacy_key, None)
            return cfg
        except Exception:
            pass
    cfg = DEFAULT_CONFIG.copy()
    save_config(cfg)

    # Override với env vars (ưu tiên cao nhất — dùng khi deploy Render)
    if os.environ.get("TELEGRAM_TOKEN"):
        cfg["token"] = os.environ["TELEGRAM_TOKEN"]
    if os.environ.get("OWNER_ID"):
        try:
            cfg["owner_id"] = int(os.environ["OWNER_ID"])
        except ValueError:
            pass
    if os.environ.get("GEMINI_KEY"):
        cfg["gemini_api_key"] = os.environ["GEMINI_KEY"]
    if os.environ.get("ANTHROPIC_KEY") or os.environ.get("CLAUDE_KEY"):
        cfg["claude_api_key"] = (os.environ.get("ANTHROPIC_KEY") or os.environ.get("CLAUDE_KEY")).strip()
    if os.environ.get("GEMINI_KEYS"):
        keys = [k.strip() for k in os.environ["GEMINI_KEYS"].split(",") if k.strip()]
        if keys:
            cfg["gemini_api_key"] = keys[0]
            cfg["gemini_api_keys_extra"] = keys[1:]

    return cfg

def save_config(cfg: dict) -> None:
    f = _get_fernet()
    CONFIG_FILE.write_text(
        json.dumps(_encrypt_config(cfg, f), ensure_ascii=False, indent=2), "utf-8"
    )
    # ── Đồng bộ lên PostgreSQL ─────────────────────────────────────
    # File config.json ở trên chỉ sống trên disk local — trên Render (free,
    # không có Persistent Disk) disk này bị reset mỗi lần restart/redeploy,
    # nên đây mới là bản lưu "thật" sống sót qua restart.
    # [BIG UPDATE] Lưu bản ĐÃ MÃ HÓA (không phải plaintext) lên Postgres — trước
    # đây field nhạy cảm (token, gemini key...) bị lưu trần trên DB dù đã có cơ
    # chế Fernet cho file local. Xem sync_encryption_key_with_db() để biết cách
    # key được giữ ổn định qua các lần restart dù không có Persistent Disk.
    try:
        import db_postgres
        if db_postgres.is_ready():
            db_postgres.config_save("settings", _encrypt_config(cfg, f))
    except Exception:
        pass

def sync_encryption_key_with_db() -> None:
    """
    Đồng bộ Fernet key với PostgreSQL để key KHÔNG bị đổi (và làm dữ liệu đã mã
    hóa cũ trở thành rác không giải mã được) sau mỗi lần restart trên Render free
    tier — disk local bị reset, nhưng Postgres thì không.

    Thứ tự ưu tiên:
      1. Env var `CONFIG_FERNET_KEY` (nếu người dùng tự đặt) → luôn dùng key này.
      2. Key đã có sẵn trong Postgres (từ lần chạy trước) → áp dụng lại key đó.
      3. Chưa có gì trong Postgres → đẩy key local hiện tại (vừa tạo hoặc đã có)
         lên Postgres để các lần restart sau khôi phục được.

    Phải gọi ngay sau khi db_postgres.init() thành công, TRƯỚC restore_config_from_db().
    An toàn để gọi nhiều lần / khi DB chưa sẵn sàng (không làm gì, bỏ qua).
    """
    if not CRYPTO_AVAILABLE:
        return
    if os.environ.get("CONFIG_FERNET_KEY", "").strip():
        return  # ưu tiên 1 đã được _get_or_create_key() xử lý, không cần đụng tới DB
    try:
        import db_postgres
        if not db_postgres.is_ready():
            return
        saved = db_postgres.config_load_all().get("_enc_key")
        saved_key = saved.get("key", "") if isinstance(saved, dict) else ""
        if saved_key:
            try:
                Fernet(saved_key.encode())  # validate trước khi ghi đè
                KEY_FILE.write_bytes(saved_key.encode())
                try: KEY_FILE.chmod(0o600)
                except Exception: pass
            except Exception:
                pass
        else:
            current_key = _get_or_create_key().decode()
            db_postgres.config_save("_enc_key", {"key": current_key})
    except Exception:
        pass

def restore_config_from_db() -> int:
    """
    Khôi phục config đã lưu trong PostgreSQL (bảng bot_config) vào CONFIG hiện tại.
    Phải gọi 1 lần ngay sau sync_encryption_key_with_db(), TRƯỚC khi đọc
    token/owner_id và build Application trong bot.py:main().
    Trả về số field đã khôi phục (0 nếu DB chưa sẵn sàng hoặc chưa có gì lưu).
    """
    try:
        import db_postgres
        if not db_postgres.is_ready():
            return 0
        saved = db_postgres.config_load_all().get("settings")
        if not saved:
            return 0
        f = _get_fernet()
        saved = _decrypt_config(saved, f)
        count = 0
        for k, v in saved.items():
            if k in DEFAULT_CONFIG:   # chỉ khôi phục field hợp lệ, tránh rác cũ
                CONFIG[k] = v
                count += 1
        CONFIG.setdefault("stats", {}).setdefault("ai_messages", 0)
        return count
    except Exception:
        return 0

def load_db() -> dict:
    default = {"notes": {}, "filters": {}, "warns": {}, "users": {},
               "rules": {}, "admins": [], "blocked": [], "audit_log": [],
               "phrase_media": {}, "thinking_media": None}
    if DB_FILE.exists():
        try:
            data = json.loads(DB_FILE.read_text("utf-8"))
            for k, v in default.items():
                data.setdefault(k, v)
            return data
        except Exception:
            pass
    return default

def save_db(data: dict) -> None:
    DB_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    # [BIG UPDATE] Đồng bộ notes/filters/warns/users/rules/admins/blocked/audit_log
    # lên PostgreSQL. TRƯỚC bản cập nhật này, dữ liệu này CHỈ sống trên disk local
    # (db.json) nên bị MẤT HẾT mỗi lần Render free tier restart/redeploy (không
    # có Persistent Disk) — trong khi CONFIG và lịch sử AI đã được Postgres bảo vệ
    # từ trước. Giờ cả hai loại dữ liệu đều sống sót qua restart như nhau.
    try:
        import db_postgres
        if db_postgres.is_ready():
            db_postgres.config_save("main_db", data)
    except Exception:
        pass

def restore_db_from_db() -> int:
    """
    Khôi phục DB (notes/filters/warns/users/rules/admins/blocked/audit_log) từ
    PostgreSQL vào biến DB hiện tại. Gọi 1 lần ngay sau restore_config_from_db()
    trong bot.py:main(), TRƯỚC khi Application bắt đầu xử lý update.
    Trả về số khóa cấp 1 đã khôi phục (0 nếu DB chưa sẵn sàng hoặc chưa có gì lưu).
    """
    try:
        import db_postgres
        if not db_postgres.is_ready():
            return 0
        saved = db_postgres.config_load_all().get("main_db")
        if not saved:
            return 0
        count = 0
        for k in ("notes", "filters", "warns", "users", "rules", "admins", "blocked", "audit_log",
                  "phrase_media", "thinking_media"):
            if k in saved:
                DB[k] = saved[k]
                count += 1
        return count
    except Exception:
        return 0

# ── Global instances (dùng chung toàn bộ project) ────────────────
CONFIG: dict = load_config()
DB:     dict = load_db()

__all__ = [
    "VN_TZ", "now_vn", "fmt_vn",
    "CRYPTO_AVAILABLE",
    "BASE_DIR", "DATA_DIR", "CONFIG_FILE", "DB_FILE", "KEY_FILE",
    "DEFAULT_CONFIG", "_SENSITIVE_FIELDS",
    "_get_fernet", "_encrypt_config", "_decrypt_config",
    "load_config", "save_config", "sync_encryption_key_with_db",
    "restore_config_from_db", "load_db", "save_db", "restore_db_from_db",
    "CONFIG", "DB",
]
