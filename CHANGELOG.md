# 📋 Changelog

## [Unreleased] — Không cần reply: AI tự hiểu "tin nhắn này" là tin gần nhất

### 🧠 Fix: AI lấy nhầm ngữ cảnh cũ thay vì tin mới nhất trong nhóm
- **Vấn đề phát hiện sau khi test bản fix trước:** khi owner gõ "Megumin dịch
  đi" mà KHÔNG reply cụ thể vào tin nào, AI đôi khi lấy nhầm nội dung từ một
  lượt phân tích media CŨ đã lưu trong lịch sử hội thoại riêng (`AI_HISTORY`,
  vd tin nhắn placeholder `[Video]` từ lần phân tích video trước đó), thay vì
  hiểu đúng là owner đang nói tới tin nhắn *mới nhất vừa được đăng trong
  nhóm*.
- **`bot.py` — `_build_group_ai_context()` viết lại theo 3 lớp ưu tiên, xếp
  theo thứ tự để phần quan trọng KHÔNG BAO GIỜ bị cắt nếu vượt giới hạn ký
  tự (chỉ phần lịch sử mở rộng ở cuối mới bị cắt bớt):**
  1. **Đối tượng mặc định** cho "tin nhắn này/tin đó/nó/cái đó": nếu owner có
     reply → dùng đúng tin đang reply; nếu KHÔNG reply → tự động dùng tin
     **ngay trước đó trong nhóm** (từ `MSG_BUFFER`), đúng như cách một người
     bình thường đọc nhóm sẽ hiểu.
  2. **Quy tắc ưu tiên bắt buộc**: chỉ thị rõ cho AI luôn dùng đối tượng mặc
     định ở trên, TUYỆT ĐỐI không lấy nội dung từ các lượt chat cũ giữa AI
     và owner trừ khi owner nói rõ đang tiếp tục hỏi về đúng thứ đó.
  3. Lịch sử chat mở rộng (~20 tin gần nhất) — chỉ để nắm mạch chuyện rộng,
     không dùng để suy ra đối tượng mặc định.
- Nhờ vậy: **không còn bắt buộc phải reply** — owner chỉ cần gõ "megumin
  dịch đi" ngay sau tin muốn dịch là đủ, giống hệt cách một thành viên bình
  thường đọc và phản hồi trong nhóm.

## [Unreleased] — AI đọc được ngữ cảnh nhóm (tin đang reply + chat gần đây)

### 🧠 Fix: AI không "thấy" nội dung tin nhắn đang được reply
- **Lỗi cũ:** khi owner gọi AI trong nhóm bằng cách tag bot/gõ "megumin" hoặc
  reply thẳng vào tin của bot, code chỉ gửi cho Gemini ĐÚNG phần chữ owner gõ
  sau trigger (vd "dịch tin nhắn này") — không hề kèm nội dung tin nhắn đang
  được reply, nên AI không thể biết "tin nhắn này" là tin nào.
- **Mới (`bot.py`):** `_build_group_ai_context()` — dựng khối bối cảnh gồm
  (1) nội dung tin owner đang reply (lấy trực tiếp từ `msg.reply_to_message`,
  luôn đầy đủ dù buffer có lưu hay không) và (2) tối đa 20 tin gần nhất trong
  nhóm lấy từ `MSG_BUFFER` (tính năng giám sát đã có sẵn), giúp Megumin đọc
  hiểu mạch chuyện xung quanh giống một thành viên bình thường thay vì chỉ
  thấy đúng câu lệnh trần trụi.
- **`ai_module.py`:** `_gemini_chat_with_tools()` / `_gemini_chat_with_key_and_tools()`
  nhận thêm tham số `extra_context` — khối này được gắn vào
  `system_instruction` CHỈ CHO LƯỢT GỌI HIỆN TẠI, KHÔNG lưu vào `AI_HISTORY`,
  nên lịch sử hội thoại đã lưu không bị phình to/lặp lại mỗi lượt chat.
- **Yêu cầu ngoài code:** tính năng chỉ hoạt động đầy đủ khi (1) đã tắt
  Privacy Mode cho bot qua BotFather (`/setprivacy` → *Disable*) để Telegram
  gửi mọi tin nhắn nhóm cho bot, và (2) đã chạy `/setgroup` trong nhóm đó để
  `MSG_BUFFER` được ghi nhận.

## [Unreleased] — Quản lý nhân vật AI: tạo/xóa/sửa prompt + ghi nhớ riêng

### 🎭 Tạo/xóa nhân vật AI tùy chỉnh
- **Mới (`ai_module.py`):** `CONFIG["ai_custom_personas"]` lưu nhân vật do owner tự
  tạo (`{key: {name, emoji, trigger, prompt, memory}}`), tách biệt hoàn toàn với
  `AI_PERSONA_PRESETS` built-in trong code — không bao giờ mất nhân vật gốc khi
  update code, vì custom luôn lưu trong config.json (đã được `/backup`/`/restore`
  đóng gói sẵn, không cần sửa gì thêm).
- **Mới:** `get_all_personas()` — dict gộp built-in + custom, dùng thay
  `AI_PERSONA_PRESETS` ở mọi nơi hiển thị/chọn nhân vật (`/tinhcach`, callback
  `aipersona:`, trạng thái "đang suy nghĩ...", `/aisys`, trigger từ gọi bot...).
- **Lệnh mới (`handlers_ai.py`):**
  - `/aipersonanew key | Tên | Prompt` — tạo nhân vật mới. Key chỉ gồm chữ/số,
    không trùng nhân vật built-in (`megumin`, `tro_ly`) hoặc custom đã có.
  - `/aipersonadel key` — xóa nhân vật tự tạo. Không xóa được nhân vật built-in
    (chặn ở `delete_custom_persona()`). Nếu đang dùng đúng persona bị xóa, tự
    fallback về `megumin`.
- **`/tinhcach`** giờ liệt kê cả nhân vật tự tạo (đánh dấu `_(tự tạo)_`), kèm
  hướng dẫn nhanh các lệnh quản lý nhân vật ngay trong message.

### ✏️ Sửa prompt nhân vật bất kỳ
- **Mới:** `/aipersonaedit key | Prompt mới` — sửa prompt của MỘT nhân vật bất
  kỳ, kể cả nhân vật built-in. Sửa built-in không đụng vào code gốc — tạo một
  override riêng trong `ai_custom_personas[key]` kèm cờ `_override`, kế thừa
  `name`/`emoji`/`trigger` gốc. `get_all_personas()` tự ưu tiên override khi
  trả kết quả. Sửa xong tự `AI_HISTORY.clear()` để AI áp dụng prompt mới ngay
  từ lượt chat kế tiếp, không bị lịch sử cũ "kéo" lại văn phong trước đó.

### 🧠 Ghi nhớ riêng cho từng nhân vật
- **Mới:** `/aimemory key | nội_dung` — thêm 1 dòng ghi nhớ dài hạn vào nhân
  vật (vd: tên người dùng muốn được gọi, quy tắc cố định cần nhớ...). Ghi nhớ
  được NỐI THÊM vào cuối prompt nhân vật trong mỗi lần gọi `_gemini_get_sys_prompt()`,
  dưới khối `[GHI NHỚ RIÊNG - LUÔN NHỚ NHỮNG ĐIỀU SAU]` — không ghi đè prompt
  gốc, gọi nhiều lần sẽ cộng dồn từng dòng (mỗi dòng bắt đầu bằng `- `).
- **Mới:** `/aimemory key` (không kèm nội dung) → xem toàn bộ ghi nhớ hiện tại
  của nhân vật đó.
- **Mới:** `/aimemoryclear key` — xóa sạch ghi nhớ của 1 nhân vật (không ảnh
  hưởng đến prompt gốc).
- Ghi nhớ áp dụng cho cả nhân vật built-in: gọi `/aimemory megumin | ...` sẽ tự
  tạo override (giữ nguyên prompt gốc, chỉ thêm phần ghi nhớ) giống cơ chế của
  `/aipersonaedit`.

### 🔔 Đặt từ gọi bot (trigger) riêng cho từng nhân vật
- **Mới (`ai_module.py`):** `set_persona_triggers(key, triggers)` — đặt lại field
  `trigger` (list) cho 1 nhân vật bất kỳ. Loại trùng, giữ thứ tự, validate ít
  nhất 1 từ hợp lệ. Sửa built-in tạo override giống `/aipersonaedit`/`/aimemory`
  — giữ nguyên prompt/memory hiện có, chỉ thay trigger.
- **Gộp vào `/aipersonaedit` (KHÔNG tạo lệnh riêng)** — để tránh phình số lệnh:
  - `/aipersonaedit key | Prompt mới` → sửa prompt (như cũ)
  - `/aipersonaedit key | trigger: từ1, từ2` → sửa trigger, nhận biết bằng
    tiền tố `trigger:` ở đầu phần thứ 2 (không phân biệt hoa/thường)
  - Gọi `/aipersonaedit` không kèm tham số → hiện trigger hiện tại của TẤT CẢ
    nhân vật (built-in + custom) cùng với hướng dẫn sửa prompt
  - `_get_alt_triggers()` (utils.py) đã hỗ trợ sẵn list từ trước nên không cần
    sửa gì ở phần khớp tin nhắn trong nhóm — đổi `trigger` có hiệu lực ngay,
    không cần restart bot.
- Trigger chỉ có tác dụng khi nhân vật đó đang được chọn làm persona hiện tại
  (`/aipersona key`) — đổi trigger của nhân vật không active không ảnh hưởng gì.

### 🆕 `/aipersonanew` hỗ trợ thêm emoji tùy chọn
- **Cú pháp mới:** `/aipersonanew key | Tên | emoji | Prompt` (emoji optional —
  không truyền thì mặc định `🎭`, không cần thêm lệnh riêng).
- **Fix (`handlers_ai.py`):** Phân biệt dạng 3 phần (không emoji) và 4 phần (có
  emoji) bằng ĐỘ DÀI đoạn thứ 3 (≤ 8 ký tự, không khoảng trắng → coi là emoji),
  không dựa vào đếm số dấu `|` toàn câu — tránh nhầm khi nội dung prompt người
  dùng tự chứa ký tự `|` (vd: trích lời thoại).
- **Fix (`_split_pipe_args`):** Sửa lỗi mất khoảng trắng quanh dấu `|` nằm
  TRONG phần prompt khi gộp lại (trước đây join bằng `"|"` trần làm dính chữ
  liền nhau, vd: `"A | B"` → `"A|B"`).


- `/help` (mục AI) và `/aisys` (khi gọi không kèm tham số) giờ liệt kê đầy đủ
  6 lệnh quản lý nhân vật mới ở trên.

### 🐛 Fix nút bấm nhân vật bị cắt mất chữ đầu
- **Bug:** Nút bấm chọn nhân vật trong `/tinhcach` và callback `aipersona:` dùng
  `info['name'].split(' ', 1)[-1]` — vô tình XÓA LUÔN từ đầu tiên của tên hiển
  thị. Ví dụ "Trợ lý chuyên nghiệp" bị cắt thành "lý chuyên nghiệp" trên nút.
- **Fix (`handlers_ai.py`, `bot.py`):** Bỏ `.split(' ', 1)[-1]`, hiển thị
  nguyên `info['name']` trên nút — không có lý do gì để cắt từ đầu của tên.


- **`/tinhcach`** (không kèm tham số) bỏ đoạn hướng dẫn 5 lệnh quản lý nhân vật
  in lặp lại trong message — đã có đầy đủ ở `/help` (mục AI), giờ chỉ còn gợi
  ý ngắn "nhấn nút hoặc gõ `/tinhcach key`".
- **`/aisys`** (không kèm tham số) bỏ tương tự, chỉ giữ link trỏ về `/help`.
- **Fix lỗi gọi nhầm lệnh `/aipersona`** (lệnh KHÔNG tồn tại — tên lệnh thật
  là `/tinhcach`) còn sót lại ở 4 chỗ trong text phản hồi (`cmd_ai_toggle`,
  `cmd_ai_persona_new`, nhánh trigger của `cmd_ai_persona_edit`, và dòng help
  cũ trong `/aisys`) — tất cả đã đổi đúng thành `/tinhcach`.

## [Unreleased] — Fix /aithinking + /cancel hủy AI ngay lập tức

### 🧠 `/aithinking` — Sửa không có tác dụng với Gemini 2.5
- **Bug:** `_build_thinking_config()` chỉ áp dụng `ThinkingConfig(thinking_level=...)` cho
  model Gemini 3.x. Với Gemini 2.5 (`gemini-2.5-flash`, `gemini-2.5-pro`...) lệnh này
  hoàn toàn bị bỏ qua — Gemini 2.5 dùng `thinking_budget` (số nguyên), không dùng
  `thinking_level`.
- **Fix (`ai_module.py`):** Thêm `_is_gemini25()` và mapping `_GEMINI25_BUDGET_MAP`:
  - `low` → `thinking_budget=0` (tắt thinking hoàn toàn, nhanh nhất, tiết kiệm quota)
  - `medium` → `thinking_budget=-1` (auto/dynamic, Gemini tự quyết — mặc định)
  - `high` → `thinking_budget=8192` (suy luận sâu, tốn thêm token)
- **Fix (`handlers_ai.py`):** Cập nhật text hướng dẫn `/aithinking` giải thích rõ
  cả 2 dòng model được hỗ trợ thay vì ghi sai "Gemini 2.5 bỏ qua".

### ❌ `/cancel` — Hủy AI đang generate ngay lập tức
- **Bug:** `/cancel` cũ chỉ xóa `pending_reply_to`/`pending_edit_msg` (thao tác
  reply/edit thủ công). Còn AI đang generate chạy dưới dạng `await` trực tiếp
  trong handler — không có cách nào dừng, phải đợi AI trả lời xong mới cancel được,
  khiến lệnh gần như vô dụng khi AI đang suy nghĩ.
- **Fix (`ai_cancel.py` — file mới):** Module trung gian `AI_ACTIVE_TASKS: dict[int, Task]`
  lưu task AI đang chạy theo `chat_id`. Tách riêng để tránh circular import
  `bot.py ↔ handlers_basic.py`.
- **Fix (`bot.py`):** Tất cả 5 hàm `_run_ai_chat`, `_run_vision_analysis`,
  `_run_voice_analysis`, `_run_video_analysis`, `_run_text_analysis` đều wrap phần
  AI call vào `asyncio.ensure_future()`, đăng ký vào `AI_ACTIVE_TASKS[chat.id]`,
  bắt `CancelledError` để dọn status message, tự xóa khỏi dict khi xong (`finally`).
- **Fix (`ai_module.py`):** Thêm `await asyncio.sleep(0)` checkpoint đầu mỗi vòng
  lặp tool trong `_gemini_chat_with_key_and_tools` — `asyncio.to_thread()` không bị
  ngắt giữa chừng, nhưng `CancelledError` được raise tại điểm yield tiếp theo
  (giữa các tool step) thay vì phải đợi hết toàn bộ agent loop mới nhận ra.
- **Fix (`handlers_basic.py`):** `cmd_cancel` giờ gọi `task.cancel()` ngay khi có
  task đang chạy — AI bị dừng ngay ở lần await kế tiếp, status "đang suy nghĩ"
  được xóa sạch, bot thông báo `🛑 Đã hủy!` thay vì dùng `cancel_done`/`cancel_none`.



- **Sửa toàn bộ chỗ dùng `**chữ đậm**`** (cú pháp Markdown chuẩn/GitHub) trong
  nội dung gửi qua Telegram — Telegram `ParseMode.MARKDOWN` (bản v1 đang dùng)
  chỉ hiểu **1 dấu sao** `*chữ đậm*`, không hỗ trợ 2 dấu sao. Ảnh hưởng: bảng
  hướng dẫn `/help` chi tiết, `/note`/`/filter` usage text, thông báo xác nhận
  ghi đè — tất cả giờ hiện đậm đúng thay vì hiện nguyên `**` xấu hoặc có nguy
  cơ lệch cặp `*` gây lỗi parse phần còn lại của tin nhắn.
- **`/tomtat`** (tóm tắt AI): prompt gửi cho Gemini giờ yêu cầu rõ chỉ dùng 1
  dấu sao; đồng thời kết quả AI trả về được tự động chuẩn hoá `**` → `*`
  trước khi gửi, phòng trường hợp AI không tuân thủ tuyệt đối hướng dẫn.

## [Unreleased] — Album media cho note/filter + /help theo nhóm lệnh

- **`/note` và `/filter` giờ hỗ trợ ALBUM (nhiều ảnh/video/gif) trong 1 câu:**
  reply vào 1 ảnh **bên trong 1 album nhiều ảnh** rồi gõ lệnh → bot tự gom
  **toàn bộ album** đó (không chỉ 1 ảnh bạn reply trúng), tải về và gắn hết
  vào câu đang lưu. Khi gọi lại (`/get` hoặc trigger filter nổ), bot gửi lại
  y hệt dưới dạng album thật của Telegram (gọn, lướt được).
  - Bot "nhớ" album trong vòng **24 tiếng** kể từ lúc gửi — owner có thể gửi
    album xong, vài tiếng/qua ngày sau mới quay lại reply để lưu, vẫn lấy đủ.
    Sau khi `/note`/`/filter` đã lưu xong, album được tải cố định và không
    phụ thuộc bộ nhớ tạm này nữa — dùng được vĩnh viễn, qua restart thoải mái.
  - Album thật của Telegram chỉ cho phép `caption` hiện ở ảnh/video **đầu
    tiên** (giới hạn từ chính Telegram, không phải bug) — nội dung text vẫn
    đầy đủ, chỉ là vị trí hiển thị theo đúng cách Telegram client thường thấy.
  - Animation/gif không được Telegram cho gộp chung album với ảnh/video, nên
    nếu 1 câu có cả ảnh/video lẫn gif, bot tự tách gửi: ảnh/video thành 1
    album, gif gửi nối tiếp ngay sau.
  - Mỗi câu random (`%%%`) vẫn có thể mang album riêng của nó như thiết kế
    media-riêng-từng-câu trước đó — không giới hạn chỉ 1 ảnh/câu nữa.
- **`/noformat`** (xem nội dung thô của 1 filter qua chat thường) sửa lỗi
  crash khi filter có câu random — giờ hiện đúng text + số lượng media mỗi
  câu thay vì lỗi.

## [Unreleased] — /help: bảng tổng quan gọn + chi tiết riêng từng nhóm

- **Thiết kế lại `/help` hoàn toàn:** trước đây là 1 khối text khổng lồ liệt
  kê hết mọi lệnh, dễ rối mắt và phải kéo dài. Giờ `/help` chỉ hiện **1 dòng
  đại diện mỗi nhóm lệnh** (9 nhóm: Cơ bản, Ghi chú, Bộ lọc, Quản lý nhóm,
  Media cho câu thoại, Giám sát & tóm tắt, Cơ sở dữ liệu, Phân quyền & owner,
  AI Gemini) kèm **nút bấm** mở hướng dẫn đầy đủ riêng từng nhóm — không cần
  gõ lệnh con, bấm trực tiếp trên bàn phím inline. Mỗi trang chi tiết có nút
  "◀️ Quay lại tổng quan" để điều hướng qua lại.
- Hướng dẫn chi tiết của nhóm Ghi chú/Bộ lọc giờ giải thích đầy đủ cú pháp
  `%%%` (ghi đè vs random), cờ `{}` của filter, biến cá nhân hoá, và cú pháp
  chèn link `[chữ](url)` — gộp toàn bộ kiến thức đã thêm trong các bản trước.

## [Unreleased] — Note/Filter: ghi đè vs random rõ ràng + media riêng từng câu

- **Sửa lẫn lộn ghi đè / random (%%%):**
  - Gõ lại `/note #tên` hoặc `/filter trigger` **không** kèm `%%%` → **ghi đè**
    toàn bộ nội dung cũ (trước đây luôn cộng dồn, gây phình DB và không cách
    nào sửa lại nội dung).
  - Nếu note/filter đó **đang có ≥2 câu random**, bot sẽ **hỏi xác nhận** (nút
    bấm ✅/❌) trước khi xoá, tránh mất dữ liệu do gõ nhầm.
  - Có `%%%` trong nội dung → luôn **thêm** 1 câu random mới, hành vi như cũ.
- **Mỗi câu random giờ có thể kèm ảnh/video riêng:** trước đây 1 filter/note
  chỉ có 1 media dùng chung cho mọi câu %%%. Giờ reply ảnh khi thêm 1 câu
  `%%%` mới sẽ gắn ảnh đó CHỈ cho câu đó — random sẽ ra đúng cặp text+ảnh của
  cùng 1 câu được chọn, không bị lệch ảnh/text của 2 câu khác nhau.
- **Sửa bug "gửi ảnh bị mất, chỉ ra text":**
  - Lỗi gửi media (markdown trong caption lỗi cú pháp, caption vượt quá 1024
    ký tự giới hạn Telegram, `link_preview_options` không hợp lệ khi gửi
    ảnh/video kèm flag `{preview}`...) trước đây bị nuốt im lặng → bot rớt
    xuống gửi text thường, mất hẳn ảnh. Giờ bot tự thử lại không-markdown,
    tách caption quá dài ra tin nhắn riêng (vẫn giữ ảnh), và chỉ thật sự bỏ
    ảnh khi file mất + không còn `file_id` dự phòng nào.
  - Log lỗi thật (qua `vlog`, bật bằng `/searchdebug`) thay vì nuốt exception
    hoàn toàn, dễ chẩn đoán sự cố sau này.
- **Hỗ trợ chèn link trong note/filter:** `[chữ hiển thị](https://...)` —
  cú pháp Markdown link chuẩn, Telegram tự hiển thị thành hyperlink.
- **Dọn rác media khi ghi đè:** file ảnh/video cũ trên đĩa của các câu %%% bị
  xoá khi ghi đè giờ được tự động xoá theo, tránh tồn file mồ côi.

## [Unreleased] — Media đính kèm cho câu thoại & filter

- **Bỏ trick link-preview ẩn cho status AI** (`THINKING_IMG_MD`) khi owner muốn —
  giờ có thể `/setthinkingmedia` (reply ảnh/video/gif) để gửi media thật kèm
  caption "đang suy nghĩ/phân tích", thay vì phụ thuộc link CDN ngoài + cài đặt
  preview của người xem.
- **`/setmedia <key>` / `/delmedia <key>` / `/listmedia`:** gắn ảnh/video/gif local
  cho bất kỳ câu thoại tĩnh nào trong `MAID_PHRASES` (ok, done, ban_done, kick_done,
  v.v.) — bot sẽ gửi kèm media đó mỗi khi câu thoại được dùng.
- **`/filter` hỗ trợ media:** reply vào 1 ảnh/video/gif rồi gõ `/filter từ_khóa nội_dung`
  để bộ lọc tự động trả lời kèm media, giống cách Telegram client thường thấy.
- **Lưu trữ:** media lưu local trong thư mục `media/` (không commit git). Vì nhiều
  host free (vd Render free tier) không có persistent disk, mỗi media sau khi gửi
  thành công lần đầu sẽ tự cache thêm Telegram `file_id` vào DB (đồng bộ Postgres
  như notes/filters) — nếu file local mất sau khi restart, bot tự fallback gửi
  bằng `file_id` đã cache mà không cần file vật lý nữa.

## [2.1.0] — Big Update

Bản cập nhật lớn, toàn diện: vá lỗi nghiêm trọng (SDK deprecated, mất dữ liệu khi
restart), tăng cường bảo mật & độ tin cậy, và bổ sung nhiều tính năng quản lý nhóm
còn thiếu.

### 🔴 Sửa lỗi nghiêm trọng (Critical)

- **Migrate SDK Gemini:** `google-generativeai` (Google ngừng hỗ trợ chính thức từ
  31/08/2025) → **`google-genai`**, SDK thống nhất mới (`from google import genai`,
  `client.models.generate_content(...)`, `client.chats.create(...)`). Toàn bộ
  `ai_module.py`, `search_module.py`, `handlers_ai.py`, `handlers_monitor.py` và
  `requirements.txt` đã được cập nhật. Hành vi (key rotation, fallback model, vision
  nhiều ảnh, lịch sử hội thoại) được giữ nguyên 100%.
- **Mất dữ liệu khi restart trên Render free tier:** `notes`, `filters`, `warns`,
  `users`, `rules`, `admins`, `blocked` (biến `DB` trong `config.py`) trước đây **chỉ**
  được lưu trên disk local (`data/db.json`) — bị xóa sạch mỗi lần Render free tier
  restart/redeploy (không có Persistent Disk), trong khi `CONFIG` và lịch sử AI đã
  được PostgreSQL bảo vệ từ trước. Giờ `save_db()`/`restore_db_from_db()` đồng bộ 2
  chiều với PostgreSQL giống `CONFIG`, qua bảng `bot_config` có sẵn (key `"main_db"`).
- **Bug `db_postgres.config_load_all()`:** hàm này gọi `json.loads()` trên giá trị
  **đã được psycopg2 tự decode** thành kiểu Python gốc (str/int/float/bool), không
  chỉ dict/list như code cũ giả định — gây `JSONDecodeError` âm thầm nếu có giá trị
  dạng chuỗi/số thuần được lưu qua `config_save()`. Đã sửa để xử lý đúng mọi kiểu
  JSON hợp lệ mà psycopg2 trả về.

### 🔐 Bảo mật

- **Mã hóa trước khi lưu Postgres:** trước đây `save_config()` lưu bản **plaintext**
  (chưa mã hóa) của `token`/`gemini_api_key`/... lên PostgreSQL dù file local đã được
  mã hóa Fernet — nghĩa là ai có quyền truy cập DB (vd `DATABASE_URL` bị lộ) đọc được
  thẳng các secret. Giờ bản lưu lên Postgres cũng được mã hóa giống file local.
- **Fernet key ổn định qua restart:** thêm `sync_encryption_key_with_db()` — key mã
  hóa được đồng bộ qua PostgreSQL (bảng `bot_config["_enc_key"]`) để không bị đổi
  ngẫu nhiên mỗi lần disk local bị reset (tránh trường hợp dữ liệu mã hóa cũ trở thành
  "rác" không giải mã được sau restart). Hỗ trợ thêm biến môi trường tuỳ chọn
  `CONFIG_FERNET_KEY` cho ai muốn tự quản lý key độc lập với DB.
- **`/encrekey` đồng bộ Postgres:** trước đây chỉ ghi lại key/file local — nếu dùng
  trên Render thì lần restart sau sẽ tự khôi phục NHẦM key cũ từ DB. Giờ lệnh này
  cũng đẩy key mới + config mã hóa lại lên Postgres ngay.

### 🛡️ Độ tin cậy

- **Global error handler:** `app.add_error_handler(...)` — mọi exception chưa được
  xử lý ở bất kỳ handler nào giờ được log đầy đủ traceback **và** báo trực tiếp cho
  owner qua DM (giới hạn tần suất báo mỗi 30s để tránh spam nếu lỗi lặp lại liên tục).
  Trước đây lỗi chỉ âm thầm xuất hiện trong console, owner không biết bot đang gặp vấn đề.
- **Graceful shutdown:** thêm hook `post_shutdown` — CONFIG & DB được lưu lần cuối
  ngay trước khi process dừng (kể cả khi Render gửi SIGTERM lúc redeploy), giảm rủi
  ro mất các thay đổi gần nhất chưa tới mốc lưu định kỳ.
- **`/status` endpoint (JSON):** bổ sung cạnh `/health` — xem nhanh uptime, trạng thái
  AI/monitor/proxy/welcome/antiflood, và tình trạng kết nối Postgres qua trình duyệt,
  không cần vào Telegram.

### ✨ Tính năng mới

- **`/welcome` + `/setwelcome`:** tự chào thành viên mới (mẫu tuỳ biến với
  `{name}`/`{group}`) và tự dọn tin "đã vào/ra nhóm" của Telegram cho gọn nhóm.
- **`/antiflood [n] [giây]`:** tự động mute tạm 5 phút thành viên gửi quá `n` tin
  trong vòng `giây` giây (mặc định 6 tin / 10 giây) — bỏ qua owner & admin đã biết.
- **`/lock`, `/unlock`:** khóa/mở khóa nhanh toàn nhóm (chỉ admin nhắn được) — hữu ích
  khi cần chống raid hoặc giữ trật tự tạm thời.
- **`/auditlog [n]`:** xem lại lịch sử hành động kiểm duyệt (ban/unban/kick/mute/
  unmute/warn/promote/demote/lock/unlock/report/antiflood) — ai làm, làm gì, khi nào,
  lý do gì. Lưu tối đa 200 hành động gần nhất, đồng bộ Postgres như DB chính.
  Mọi lệnh ban/kick/mute/warn/promote/demote hiện có đều đã được nối vào audit log
  này.
- **`/report` (reply vào tin):** dành cho **mọi thành viên**, không cần là owner —
  báo cáo 1 tin nhắn cho chủ nhân (forward + thông tin người báo cáo), có cooldown
  60s/người để tránh spam báo cáo.
- **`/cancel`:** hủy nhanh trạng thái "đang chờ nhập nội dung reply/sửa tin" trong DM
  (trước đây nếu bấm nút rồi đổi ý, không có cách hủy, phải nhắn gì đó để "giải toả").
- **`/backup`:** xuất toàn bộ `notes/filters/warns/users/rules/admins/blocked/
  audit_log` + config không nhạy cảm thành 1 file JSON gửi qua DM — lớp an toàn bổ
  sung dù đã tự đồng bộ Postgres.
- **`/version`:** xem phiên bản bot, thời gian đã chạy (uptime), trạng thái kết nối
  Postgres, và tóm tắt nhanh điểm nổi bật của big update gần nhất.
- **Persona AI mới — "Trợ lý chuyên nghiệp" (`tro_ly`):** lựa chọn không nhập vai
  nhân vật anime, dành cho lúc cần làm việc nghiêm túc (qua `/tinhcach tro_ly`).
- **Thống kê tin AI:** `/stats` giờ hiện thêm số tin AI đã trả lời và thời gian tiến
  trình đã chạy (uptime) bên cạnh các số liệu cũ.

### 🧹 Khác

- Banner khởi động hiện thêm trạng thái Welcome/Anti-flood và tình trạng kết nối
  PostgreSQL.
- `README.md`: sửa điểm không khớp "Webhook mode" trong log mẫu (bot thực chạy ở
  polling mode, biến `WEBHOOK_URL` chỉ phục vụ self-ping keep-alive) — tránh hiểu
  lầm khi đối chiếu log thực tế lúc deploy. Cập nhật sơ đồ kiến trúc bộ nhớ, thêm
  mục Tính năng chính, làm rõ biến `CONFIG_FERNET_KEY` mới.
- Thêm `.env.example` và `.gitignore`.

### 💡 Hướng phát triển tiếp theo (chưa làm trong bản này)

- Hỗ trợ đa nhóm (`target_group_id` hiện chỉ hỗ trợ 1 nhóm duy nhất theo thiết kế gốc).
- Lệnh khôi phục từ file `/backup` (hiện chỉ có xuất, chưa có nhập, để tránh rủi ro
  ghi đè nhầm dữ liệu).
- Tạo ảnh bằng AI (Imagen/Gemini image generation).

---

## Trước đó

Xem lịch sử Git/README cũ cho các thay đổi trước bản 2.1.0 (PostgreSQL persistence
cho AI history & monitor buffer, hệ thống persona AI, proxy/monitor mode, mã hóa
Fernet cho config local...).
