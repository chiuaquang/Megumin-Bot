# 🌸 Maid Bot — Hướng dẫn Deploy lên Render

> 📋 Xem [CHANGELOG.md](./CHANGELOG.md) để biết chi tiết các lần cập nhật.

## Tính năng chính

Bot **cá nhân** — chỉ owner dùng được lệnh và AI. Thành viên khác bị chặn hoàn toàn.
Owner chính (`OWNER_ID`) có thể **thêm owner phụ** bằng ID Telegram: `/addowner <id>`.

- **Quản lý nhóm:** ban/kick/mute/warn (3 warn → auto ban), ghi chú (`/note`), bộ lọc
  tự trả lời (`/filter`), nội quy (`/rules`), ghim/bỏ ghim, xóa/purge tin hàng loạt.
- **Phân quyền linh hoạt:** `/adm`, `/apromote`, `/deladm` — promote/demote bất kỳ
  **thành viên hoặc bot khác** trong nhóm (không chỉ bản thân).
- **Lock/Unlock nhóm:** `/lock` khóa tức thì (chỉ admin nhắn được), `/unlock` mở lại —
  hữu ích khi cần chống raid hoặc giữ trật tự tạm thời.
- **AI Gemini:** chat đa persona (`/tinhcach`), nhận diện & phân tích ảnh (vision),
  tự xoay nhiều API key khi hết quota, tự tìm kiếm web khi cần (Google/Facebook/YouTube).
- **AI Claude (tuỳ chọn):** `/claudekey KEY` → `/aiprovider claude` để Claude trả lời thay Gemini (chat, ảnh, tool quản trị nhóm). `/claudemodel` đổi model (mặc định `claude-haiku-5-5`). Lỗi thì tự rơi về Gemini. ⚠️ API Claude tính phí. Voice/video vẫn do Gemini xử lý.
- **AI đọc bối cảnh nhóm:** trả lời dựa trên các tin phía trên (không chỉ câu hỏi), chỉ nói về
  chủ đề đang chat khi được hỏi; tra lịch sử nhóm khi được hỏi ("hôm qua ai nói gì về X").
  Thấy được tin của bot/AI khác như Mira — xem mục *Cho bot thấy bot khác* bên dưới.
- **Giám sát nhóm (monitor):** lưu lại toàn bộ tin nhắn nhóm, đẩy real-time về DM
  chủ nhân, tóm tắt bằng AI (`/tomtat`).
- **Proxy:** chat 2 chiều giữa chủ nhân và nhóm qua DM khi không tiện vào nhóm trực tiếp.
- **Minigame:** Blackjack / Tiến Lên / UNO (`/game`) và **Ma Sói** 🐺 (`/masoi`) — 5–20 người thật, ~22 vai trò (Sói Đầu Đàn, Sát Nhân, Hiệp Sĩ...) đã cân bằng bằng mô phỏng, *Vòng Mô Tả — Tìm Kẻ Giả Mạo* với kho ~1.600 từ khoá (không có bot/AI chơi Ma Sói).
- **Bảo mật:** token & API key được mã hóa (Fernet) cả khi lưu file local lẫn khi
  đồng bộ lên PostgreSQL.

---

## Cấu trúc bộ nhớ

```
RAM (trong khi bot chạy)          PostgreSQL (persistent - không mất khi restart)
─────────────────────────         ──────────────────────────────────────────────
AI_HISTORY  dict                  bảng ai_history     ← lịch sử hội thoại AI
MSG_BUFFER  deque(200)            bảng msg_buffer     ← tin nhắn nhóm (monitor)
CONFIG      dict                  bảng bot_config["settings"]  ← cấu hình (đã mã hóa)
DB          dict                  bảng bot_config["main_db"]   ← notes/filters/warns/
                                                                  users/rules/admins/
                                                                  blocked
(Fernet key)                      bảng bot_config["_enc_key"]  ← key mã hóa (ổn định
                                                                  qua các lần restart)
```

**Khi bot KHỞI ĐỘNG:** load từ PostgreSQL → RAM  
**Khi có thay đổi (AI trả lời, ban/mute/note/filter...):** lưu RAM → PostgreSQL ngay  
**Khi bot RESTART/redeploy:** toàn bộ dữ liệu trên được khôi phục từ PostgreSQL — kể
cả khi Render free tier xóa sạch disk local, **không** mất gì cả.

> ⚠️ Nếu **không** cấu hình `DATABASE_URL` (không dùng PostgreSQL), mọi thứ chỉ sống
> trong RAM + file local `data/` — trên Render free tier (disk không persistent),
> nghĩa là **mất hết mỗi lần restart/redeploy**. Khuyến nghị luôn dùng PostgreSQL
> khi deploy thật (xem Bước 1).

---

## Bước 1: Tạo PostgreSQL miễn phí trên Render

1. Vào **render.com** → **New** → **PostgreSQL**
2. Điền:
   - Name: `maid-bot-db`
   - Region: `Singapore` (gần VN nhất)
   - Plan: **Free**
3. Bấm **Create Database**
4. Sau khi tạo xong → tab **Info** → copy **External Database URL**
   (dạng: `postgresql://user:pass@host.render.com/dbname`)

---

## Bước 2: Push code lên GitHub

```bash
git init
git add .
git commit -m "Maid Bot with PostgreSQL persistence"
git remote add origin https://github.com/TEN_BAN/TEN_REPO.git
git push -u origin main
```

---

## Bước 3: Tạo Web Service trên Render

1. **New** → **Web Service**
2. Kết nối GitHub repo vừa tạo
3. Cấu hình:

| Trường | Giá trị |
|---|---|
| **Name** | `maid-bot` |
| **Region** | Singapore |
| **Runtime** | Python 3 |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `python bot.py` |
| **Instance Type** | **Free** |

---

## Bước 4: Thêm Environment Variables

Trong tab **Environment** của Web Service, thêm các biến:

| Key | Value | Bắt buộc |
|---|---|---|
| `TELEGRAM_TOKEN` | Token từ @BotFather | ✅ |
| `OWNER_ID` | Telegram User ID của bạn | ✅ |
| `GEMINI_KEY` | Gemini API key (key chính) | ✅ |
| `GEMINI_KEYS` | `key1,key2,key3` (nhiều key) | tuỳ chọn |
| `DATABASE_URL` | URL từ Render Postgres (Bước 1) | ✅ (khuyến nghị) |
| `WEBHOOK_URL` | URL của web service (xem bên dưới) | tuỳ chọn |
| `CONFIG_FERNET_KEY` | Key Fernet tự quản lý (xem ghi chú) | tuỳ chọn |
| `EXTRA_OWNER_IDS` | Owner phụ: `id1,id2` (hoặc thêm bằng `/addowner`) | tuỳ chọn |

### Lấy WEBHOOK_URL
Bot chạy ở **polling mode** (không phải webhook thật) — biến này chỉ dùng để bot
tự ping chính nó (`/health`) mỗi 10 phút, giúp Render free tier không "ngủ". Sau khi
tạo Web Service, Render cấp URL dạng `https://maid-bot.onrender.com` — copy vào
`WEBHOOK_URL`. Nếu để trống, bot vẫn chạy bình thường, chỉ là không tự ping (nên
dùng thêm UptimeRobot — xem phần Lưu ý).

### Về CONFIG_FERNET_KEY (tuỳ chọn, nâng cao)
Mặc định, bot tự tạo và tự đồng bộ key mã hóa qua PostgreSQL — không cần làm gì
thêm. Nếu muốn **tự quản lý key mã hóa** (không để key nằm trong cùng DB với dữ
liệu đã mã hóa, an toàn hơn một chút), tự tạo 1 key và set vào biến này:
```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
Khi đã set biến này, bot luôn dùng đúng key đó, không tự sinh/đồng bộ key qua DB nữa.

---

## Bước 5: Deploy

Bấm **Deploy** (hoặc tự động deploy khi push code mới lên GitHub).

Xem logs trong tab **Logs** — bot khởi động thành công khi thấy:
```
💾 PostgreSQL: OK — đã load N cuộc hội thoại
🗂️  DB: đã khôi phục N mục (notes/filters/warns/...) từ PostgreSQL
🌸🌸🌸 MAID BOT 2.2.0 - ĐANG CHẠY 🌸🌸🌸
📡 Polling mode
```

---

## Cho bot thấy bot khác (Mira…) & bối cảnh nhóm

Mặc định Telegram **không** gửi tin của bot khác cho bot của bạn. Để bot thấy Mira và các bot khác:

1. Mở **@BotFather** → chọn bot → bật **Bot-to-Bot Communication Mode**.
2. Cho bot làm **admin** của nhóm (hoặc tắt *Group Privacy* trong @BotFather).

Chưa làm vậy thì bot vẫn thấy tin của bot khác **khi ai đó reply vào tin đó**.
Bot không tự trả lời tin của bot khác nên không bị vòng lặp bot–bot.

Lịch sử nhóm lưu trong bảng `chat_history` (PostgreSQL; không có DB thì chỉ giữ ~600 tin gần nhất
trong RAM). Chỉnh bằng `/aictx` (số tin bối cảnh, trần ký tự, bật/tắt ghi, xoá lịch sử).
⚠️ Bot ghi lại tin nhắn mọi nhóm nó có mặt — hãy báo thành viên nếu cần.

---

## Lưu ý quan trọng

### Free Tier Render
- Web Service free **ngủ sau 15 phút** không có request → bot hơi chậm lần đầu
- Để bot luôn thức: dùng [UptimeRobot](https://uptimerobot.com) ping URL mỗi 10 phút (miễn phí)
  - URL để ping: `https://maid-bot.onrender.com/health` — không cần thêm gì, Render tự trả 200
  - Muốn xem nhanh trạng thái chi tiết (uptime, AI/monitor/proxy đang bật gì):
    mở `https://maid-bot.onrender.com/status` (trả về JSON)

### PostgreSQL Free
- Giới hạn 1GB storage, 97 kết nối
- Hết hạn sau 90 ngày nếu không hoạt động → Render gửi email nhắc gia hạn (miễn phí, chỉ cần click)
- Dùng `/dbstats` để xem dung lượng đang dùng, `/cleandb` để dọn dữ liệu monitor cũ nếu cần

### Biến ENV vs config.json
- **Trên Render:** bot đọc TOKEN, OWNER_ID, GEMINI_KEY từ env vars
- **Local dev:** bot đọc từ `data/config.json` như cũ
- Cả hai cùng hoạt động song song; PostgreSQL (nếu cấu hình) luôn là nguồn dữ liệu
  "thật" được khôi phục mỗi lần khởi động, ưu tiên hơn file local

### Nếu gặp lỗi liên quan tới thư viện Gemini
Bot dùng SDK **`google-genai`** (SDK thống nhất mới của Google — SDK cũ
`google-generativeai` đã bị Google chính thức ngừng hỗ trợ từ 31/08/2025). Nếu
log hiện lỗi `ModuleNotFoundError: google.generativeai` hoặc tương tự, kiểm tra lại
`requirements.txt` đã có `google-genai` chưa, sau đó **Manual Deploy → Clear build
cache & deploy** trên Render để cài lại đúng dependency.

---

## Chạy local (dev)

```bash
pip install -r requirements.txt

# Cách 1: dùng env vars
export TELEGRAM_TOKEN="your_token"
export OWNER_ID="123456789"
export GEMINI_KEY="your_gemini_key"
export DATABASE_URL="postgresql://..."  # tuỳ chọn nhưng khuyến nghị

python bot.py

# Cách 2: dùng config.json (cũ)
# Mở data/config.json và điền thông tin
python bot.py
```

Xem [.env.example](./.env.example) để có danh sách đầy đủ các biến môi trường
hỗ trợ kèm giải thích.

---

## Sách lệnh nhanh

Gõ `/help` trong chat với bot để xem đầy đủ — bảng lệnh được nhóm theo Cơ bản /
Giám sát / Quản lý nhóm / Phân quyền / Owner / AI.
