"""
db_postgres.py — Lưu trữ lịch sử AI và config vào PostgreSQL.

Cơ chế hoạt động:
  - Khi bot KHỞI ĐỘNG: load toàn bộ AI_HISTORY từ DB vào RAM
  - Khi AI TRẢ LỜI: lưu history mới nhất vào DB (fire-and-forget)
  - Khi bot RESTART: history được khôi phục từ DB, không mất dữ liệu

Bảng dữ liệu:
  ai_history  — Lịch sử hội thoại AI (chat_id, role, content)
  bot_config  — Key-value JSONB dùng chung cho nhiều mục đích:
                  "settings" → CONFIG (đã mã hóa field nhạy cảm, xem config.py)
                  "main_db"  → DB (notes/filters/warns/users/rules/admins/blocked/
                               audit_log — [BIG UPDATE] trước đây CHỈ sống trên
                               disk local nên mất hết khi Render restart)
                  "_enc_key" → Fernet key dùng để mã hóa "settings", giữ ổn định
                               qua các lần restart (xem sync_encryption_key_with_db
                               trong config.py)
                  "last_tomtat" → mốc thời gian /tomtat moi gần nhất mỗi nhóm
  db_messages — Toàn bộ tin nhắn nhóm (monitor buffer, persistent)

Quản lý dung lượng (free tier Render chỉ có 1GB):
  - msg_buffer_delete_older_than(days) / msg_buffer_clear_all() để dọn dữ liệu
    cũ khi DB gần đầy — dùng qua lệnh /cleandb trong bot.
  - get_db_size_info() để xem dung lượng hiện tại.
"""

import json
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_conn = None
_lock = threading.Lock()   # thread-safe cho psycopg2

# ── Kết nối ──────────────────────────────────────────────────────

def init() -> bool:
    """
    Khởi tạo kết nối PostgreSQL.
    Đọc DATABASE_URL từ biến môi trường.
    Trả về True nếu thành công, False nếu thất bại.
    """
    global _conn
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        logger.warning("[db] DATABASE_URL chưa set → chạy in-memory only (lịch sử mất khi restart)")
        return False
    try:
        import psycopg2
        import psycopg2.extras
        # sslmode=require bắt buộc với Render/Aiven/Neon
        _conn = psycopg2.connect(db_url, sslmode="require")
        _conn.autocommit = True
        _create_schema()
        logger.info("[db] ✅ PostgreSQL kết nối thành công")
        return True
    except Exception as e:
        logger.error("[db] ❌ Kết nối thất bại: %s", e)
        _conn = None
        return False

def is_ready() -> bool:
    """Kiểm tra DB có sẵn sàng không."""
    return _conn is not None

def _create_schema():
    """Tạo bảng nếu chưa tồn tại (idempotent — an toàn gọi nhiều lần)."""
    with _lock:
        with _conn.cursor() as cur:
            # Bảng 1: Lịch sử hội thoại AI
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_history (
                    chat_id  BIGINT  NOT NULL,
                    idx      INTEGER NOT NULL,
                    role     TEXT    NOT NULL,
                    content  TEXT    NOT NULL,
                    PRIMARY KEY (chat_id, idx)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_history_chat ON ai_history(chat_id)")

            # Bảng 2: Config bot (thay thế 1 phần config.json)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_config (
                    key        TEXT PRIMARY KEY,
                    value      JSONB NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            # Bảng 3: Tin nhắn nhóm (monitor buffer persistent)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS msg_buffer (
                    id         BIGSERIAL PRIMARY KEY,
                    chat_id    BIGINT NOT NULL,
                    msg_id     BIGINT NOT NULL,
                    user_id    BIGINT,
                    name       TEXT,
                    username   TEXT,
                    text       TEXT,
                    msg_type   TEXT DEFAULT 'text',
                    ts         TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_buffer_chat ON msg_buffer(chat_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_buffer_ts   ON msg_buffer(ts DESC)")

    logger.info("[db] Schema OK (3 bảng: ai_history, bot_config, msg_buffer)")

# ── AI History CRUD ───────────────────────────────────────────────

def save_history(chat_id: int, history: list) -> None:
    """
    Lưu toàn bộ history của 1 chat vào PostgreSQL.
    Xóa các row cũ rồi insert lại toàn bộ (đơn giản, đủ nhanh với MAX_HISTORY=20).
    Được gọi sau mỗi lượt AI trả lời.
    """
    if not _conn:
        return
    try:
        with _lock:
            with _conn.cursor() as cur:
                cur.execute("DELETE FROM ai_history WHERE chat_id = %s", (chat_id,))
                for i, msg in enumerate(history):
                    parts = msg.get("parts", [])
                    content = parts[0] if parts and isinstance(parts[0], str) else json.dumps(parts, ensure_ascii=False)
                    cur.execute(
                        "INSERT INTO ai_history (chat_id, idx, role, content) VALUES (%s, %s, %s, %s)",
                        (chat_id, i, msg["role"], content)
                    )
    except Exception as e:
        logger.error("[db] save_history(chat_id=%s): %s", chat_id, e)

def load_all_history(max_per_conv: int = 40) -> dict:
    """
    Load toàn bộ history từ DB khi bot khởi động.
    Trả về dict: {chat_id: [{role, parts}, ...]}
    """
    if not _conn:
        return {}
    try:
        with _lock:
            with _conn.cursor() as cur:
                cur.execute("SELECT DISTINCT chat_id FROM ai_history")
                chat_ids = [r[0] for r in cur.fetchall()]

                result = {}
                for cid in chat_ids:
                    cur.execute("""
                        SELECT role, content FROM (
                            SELECT idx, role, content FROM ai_history
                            WHERE chat_id = %s ORDER BY idx DESC LIMIT %s
                        ) sub ORDER BY idx ASC
                    """, (cid, max_per_conv))
                    rows = cur.fetchall()
                    result[cid] = [
                        {"role": r[0], "parts": [r[1]]}
                        for r in rows
                    ]
        logger.info("[db] Loaded AI history: %d cuộc hội thoại", len(result))
        return result
    except Exception as e:
        logger.error("[db] load_all_history: %s", e)
        return {}

def delete_history(chat_id: int) -> None:
    """Xóa history của 1 chat (khi user dùng /aiclear)."""
    if not _conn:
        return
    try:
        with _lock:
            with _conn.cursor() as cur:
                cur.execute("DELETE FROM ai_history WHERE chat_id = %s", (chat_id,))
    except Exception as e:
        logger.error("[db] delete_history(%s): %s", chat_id, e)

def delete_all_history() -> None:
    """Xóa toàn bộ history (khi đổi nhân vật AI)."""
    if not _conn:
        return
    try:
        with _lock:
            with _conn.cursor() as cur:
                cur.execute("TRUNCATE ai_history")
        logger.info("[db] Đã xóa toàn bộ AI history")
    except Exception as e:
        logger.error("[db] delete_all_history: %s", e)

# ── Bot Config CRUD ───────────────────────────────────────────────

def config_save(key: str, value) -> None:
    """Lưu 1 config key vào DB (upsert)."""
    if not _conn:
        return
    try:
        with _lock:
            with _conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO bot_config (key, value, updated_at)
                    VALUES (%s, %s::jsonb, NOW())
                    ON CONFLICT (key) DO UPDATE
                        SET value = EXCLUDED.value, updated_at = NOW()
                """, (key, json.dumps(value, ensure_ascii=False)))
    except Exception as e:
        logger.error("[db] config_save(%s): %s", key, e)

def config_load_all() -> dict:
    """Load toàn bộ config từ DB khi khởi động."""
    if not _conn:
        return {}
    try:
        with _lock:
            with _conn.cursor() as cur:
                cur.execute("SELECT key, value FROM bot_config")
                rows = cur.fetchall()
        # psycopg2 tự động decode cột jsonb thành kiểu Python tương ứng (dict,
        # list, str, int, float, bool, None) — KHÔNG chỉ dict/list như giả định
        # trước đây. Gọi json.loads() lại trên 1 giá trị đã-được-decode (vd 1
        # chuỗi thuần "abc123") sẽ ném JSONDecodeError vì đó không phải JSON hợp
        # lệ nữa. Chỉ fallback sang json.loads() cho trường hợp lạ ngoài dự kiến.
        out = {}
        for key, value in rows:
            if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
                out[key] = value
            else:
                try:
                    out[key] = json.loads(value)
                except Exception:
                    out[key] = value
        return out
    except Exception as e:
        logger.error("[db] config_load_all: %s", e)
        return {}

# ── Msg Buffer (Monitor) CRUD ─────────────────────────────────────

def msg_buffer_push(entry: dict, max_keep: int = 5000) -> None:
    """
    Lưu 1 tin nhắn nhóm vào DB (monitor buffer persistent).
    entry là dict từ _buf_entry() trong handlers_monitor.py

    max_keep: số tin nhắn tối đa giữ lại mỗi nhóm (tin cũ hơn sẽ bị xóa tự
    động). Mặc định 5000, có thể tùy chỉnh qua CONFIG["msg_buffer_limit"]
    (xem cmd_setbufferlimit trong handlers_basic.py) — không cần sửa code.
    """
    if not _conn:
        return
    try:
        with _lock:
            with _conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO msg_buffer (chat_id, msg_id, user_id, name, username, text, msg_type, ts)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                """, (
                    entry.get("chat_id", 0),
                    entry.get("msg_id", 0),
                    entry.get("user_id", 0),
                    entry.get("name", ""),
                    entry.get("username", ""),
                    entry.get("text", "")[:2000],   # giới hạn 2000 ký tự
                    entry.get("type", "text"),
                ))
                # Giữ tối đa max_keep tin nhắn mới nhất mỗi nhóm — số này giờ
                # tùy chỉnh được qua /setbufferlimit thay vì sửa cứng trong code.
                cur.execute("""
                    DELETE FROM msg_buffer WHERE id IN (
                        SELECT id FROM msg_buffer WHERE chat_id = %s
                        ORDER BY id DESC OFFSET %s
                    )
                """, (entry.get("chat_id", 0), max_keep))
    except Exception as e:
        logger.error("[db] msg_buffer_push: %s", e)

def msg_buffer_load(chat_id: int, limit: int = 200) -> list:
    """Load tin nhắn gần nhất của 1 nhóm từ DB."""
    if not _conn:
        return []
    try:
        with _lock:
            with _conn.cursor() as cur:
                cur.execute("""
                    SELECT msg_id, user_id, name, username, text, msg_type, ts
                    FROM msg_buffer WHERE chat_id = %s
                    ORDER BY id DESC LIMIT %s
                """, (chat_id, limit))
                rows = cur.fetchall()
        result = []
        for r in reversed(rows):
            result.append({
                "msg_id":   r[0],
                "chat_id":  chat_id,
                "user_id":  r[1],
                "name":     r[2],
                "username": r[3],
                "text":     r[4] or "",
                "type":     r[5],
                "ts":       r[6].isoformat() if r[6] else "",
                "msg":      None,   # object gốc không lưu được vào DB
            })
        return result
    except Exception as e:
        logger.error("[db] msg_buffer_load: %s", e)
        return []

def msg_buffer_load_since(chat_id: int, since_iso: str, limit: int = 1000) -> list:
    """
    Load tin nhắn của 1 nhóm từ DB, tính từ thời điểm `since_iso` (ISO 8601,
    có thể có hoặc không có timezone — nếu không có sẽ coi là UTC) cho đến hiện tại.
    Dùng cho /tomtat ngay (từ 00:00 hôm nay) và /tomtat moi (từ lần tóm tắt cuối).
    """
    if not _conn:
        return []
    try:
        with _lock:
            with _conn.cursor() as cur:
                cur.execute("""
                    SELECT msg_id, user_id, name, username, text, msg_type, ts
                    FROM msg_buffer
                    WHERE chat_id = %s AND ts >= %s::timestamptz
                    ORDER BY id ASC LIMIT %s
                """, (chat_id, since_iso, limit))
                rows = cur.fetchall()
        result = []
        for r in rows:
            result.append({
                "msg_id":   r[0],
                "chat_id":  chat_id,
                "user_id":  r[1],
                "name":     r[2],
                "username": r[3],
                "text":     r[4] or "",
                "type":     r[5],
                "ts":       r[6].isoformat() if r[6] else "",
                "msg":      None,
            })
        return result
    except Exception as e:
        logger.error("[db] msg_buffer_load_since: %s", e)
        return []

def msg_buffer_delete_older_than(days: int, chat_id: Optional[int] = None) -> int:
    """
    Xóa tin nhắn trong msg_buffer cũ hơn `days` ngày (dùng khi DB gần đầy —
    free tier Render PostgreSQL chỉ có 1GB storage).
    Nếu `chat_id` được truyền, chỉ xóa tin của nhóm đó; mặc định xóa tất cả nhóm.
    Trả về số dòng đã xóa (-1 nếu lỗi).
    """
    if not _conn:
        return -1
    try:
        with _lock:
            with _conn.cursor() as cur:
                if chat_id is not None:
                    cur.execute("""
                        DELETE FROM msg_buffer
                        WHERE chat_id = %s AND ts < NOW() - (%s * INTERVAL '1 day')
                    """, (chat_id, days))
                else:
                    cur.execute("""
                        DELETE FROM msg_buffer
                        WHERE ts < NOW() - (%s * INTERVAL '1 day')
                    """, (days,))
                deleted = cur.rowcount
        logger.info("[db] Đã xóa %d tin msg_buffer cũ hơn %d ngày", deleted, days)
        return deleted
    except Exception as e:
        logger.error("[db] msg_buffer_delete_older_than: %s", e)
        return -1

def msg_buffer_clear_all(chat_id: Optional[int] = None) -> int:
    """Xóa TOÀN BỘ msg_buffer (hoặc của 1 nhóm cụ thể). Trả về số dòng đã xóa (-1 nếu lỗi)."""
    if not _conn:
        return -1
    try:
        with _lock:
            with _conn.cursor() as cur:
                if chat_id is not None:
                    cur.execute("DELETE FROM msg_buffer WHERE chat_id = %s", (chat_id,))
                else:
                    cur.execute("DELETE FROM msg_buffer")
                deleted = cur.rowcount
        logger.info("[db] Đã xóa toàn bộ msg_buffer (%d dòng)", deleted)
        return deleted
    except Exception as e:
        logger.error("[db] msg_buffer_clear_all: %s", e)
        return -1

def get_db_size_info() -> dict:
    """Trả về dung lượng DB hiện tại (ước lượng) — dùng để cảnh báo khi gần đầy 1GB free tier."""
    if not _conn:
        return {}
    try:
        with _lock:
            with _conn.cursor() as cur:
                cur.execute("SELECT pg_database_size(current_database())")
                size_bytes = cur.fetchone()[0]
                cur.execute("SELECT pg_total_relation_size('msg_buffer')")
                msg_buffer_bytes = cur.fetchone()[0]
        return {
            "total_bytes":       size_bytes,
            "total_mb":          round(size_bytes / (1024 * 1024), 1),
            "msg_buffer_bytes":  msg_buffer_bytes,
            "msg_buffer_mb":     round(msg_buffer_bytes / (1024 * 1024), 1),
        }
    except Exception as e:
        logger.error("[db] get_db_size_info: %s", e)
        return {}

# ── Stats ─────────────────────────────────────────────────────────

def get_stats() -> dict:
    """Trả về thống kê DB (dùng cho /stats)."""
    if not _conn:
        return {"db_ready": False}
    try:
        with _lock:
            with _conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM ai_history")
                hist_rows = cur.fetchone()[0]
                cur.execute("SELECT COUNT(DISTINCT chat_id) FROM ai_history")
                hist_chats = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM msg_buffer")
                msg_rows = cur.fetchone()[0]
        return {
            "db_ready":    True,
            "hist_rows":   hist_rows,
            "hist_chats":  hist_chats,
            "msg_rows":    msg_rows,
        }
    except Exception as e:
        return {"db_ready": False, "error": str(e)}
