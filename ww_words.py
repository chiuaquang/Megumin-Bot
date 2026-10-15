"""
ww_words.py — Kho từ khoá cho "Vòng Mô Tả" của Ma Sói (tìm kẻ giả mạo).

Cách hoạt động: mỗi ngày bot bốc 1 CẶP từ na ná nhau (vd "Phở bò" / "Hủ tiếu").
Số đông nhận từ A, kẻ giả mạo nhận từ B — ai mô tả "lệch" so với số đông sẽ bị nghi.

Kho từ gồm 2 nguồn (càng nhiều càng tốt, ghép tự động):
- CLUSTERS: các NHÓM từ cùng chủ đề (mỗi nhóm 8-14 từ). Bất kỳ 2 từ nào trong cùng nhóm đều
  ghép được thành 1 cặp hợp lệ -> hàng nghìn cặp mà không phải gõ tay từng cặp.
- CURATED: cặp "rất dễ nhầm" do người chọn tay (khó hơn, mô tả dễ trùng nhau).
"""
from __future__ import annotations

import random
import re
import unicodedata

# ── Chủ đề (dùng cho tuỳ chọn "Chủ đề từ" trong phòng chờ) ──────────────────
TOPICS = {
    "all":  "🎲 Tất cả",
    "food": "🍜 Ăn uống",
    "animal": "🐾 Động vật",
    "thing": "🧰 Đồ vật",
    "place": "🗺️ Địa điểm & giao thông",
    "people": "👥 Con người & nghề",
    "fun": "🎮 Giải trí & học đường",
    "nature": "🌿 Thiên nhiên & vũ trụ",
    "life": "🎎 Văn hoá & đời sống",
}

# ── NHÓM TỪ: topic -> {tên nhóm: "từ 1, từ 2, ..."} ──────────────────────────
_RAW: dict[str, dict[str, str]] = {
    "food": {
        "món nước": "Phở bò, Bún bò Huế, Hủ tiếu, Mì Quảng, Bún riêu, Bánh canh, Miến gà, Cháo lòng, Bún mắm, Mì vịt tiềm, Bún thang, Lẩu thái",
        "món cơm": "Cơm tấm, Cơm gà, Cơm chiên, Cơm cháy, Cơm niêu, Cơm hến, Cơm lam, Xôi gà, Cơm bình dân, Cơm hộp",
        "bánh Việt": "Bánh chưng, Bánh tét, Bánh giò, Bánh bao, Bánh bèo, Bánh cuốn, Bánh xèo, Bánh khọt, Bánh mì, Bánh đúc, Bánh gai, Bánh tráng trộn",
        "bánh ngọt": "Bánh bông lan, Bánh flan, Bánh su kem, Donut, Cupcake, Crepe, Tiramisu, Macaron, Bánh mochi, Bánh kem, Bánh quy, Croissant",
        "đồ uống": "Cà phê sữa, Trà sữa, Trà đào, Sinh tố bơ, Nước cam, Nước mía, Sữa đậu nành, Trà chanh, Soda chanh, Nước dừa, Sữa tươi, Bia",
        "tráng miệng": "Chè, Kem, Sữa chua, Rau câu, Tào phớ, Sương sa hạt lựu, Chè khúc bạch, Kem que, Kem ốc quế, Bánh plan, Trái cây dĩa, Yaourt đá",
        "nướng chiên": "Xúc xích, Chả giò, Nem nướng, Gà rán, Khoai tây chiên, Thịt nướng, Sườn nướng, Bò lá lốt, Cá viên chiên, Bắp xào, Đậu hũ chiên, Nem chua rán",
        "đồ ăn nhanh": "Pizza, Hamburger, Hotdog, Sandwich, Taco, Kebab, Mì Ý, Salad, Bánh tráng nướng, Khoai lắc, Gà popcorn, Bánh mì kẹp",
        "món Nhật Hàn": "Sushi, Sashimi, Ramen, Tempura, Kimbap, Tokbokki, Kim chi, Bibimbap, Udon, Takoyaki, Lẩu Hàn, Cơm cuộn",
        "trái cây nhiệt đới": "Xoài, Mít, Sầu riêng, Chôm chôm, Nhãn, Vải, Măng cụt, Thanh long, Ổi, Mận, Bơ, Dừa, Đu đủ, Chuối",
        "trái cây ôn đới": "Táo, Lê, Nho, Đào, Cherry, Dâu tây, Kiwi, Việt quất, Cam, Quýt, Bưởi, Chanh, Dưa hấu, Dưa lưới",
        "rau củ": "Cà rốt, Khoai tây, Khoai lang, Củ cải, Bắp cải, Su hào, Bí đỏ, Mướp, Dưa leo, Cà chua, Bông cải, Cải thìa",
        "rau thơm": "Rau mùi, Húng quế, Tía tô, Ngò gai, Lá lốt, Diếp cá, Hành lá, Sả, Ớt, Gừng, Tỏi, Hẹ",
        "gia vị": "Muối, Đường, Tiêu, Nước mắm, Tương ớt, Bột ngọt, Mắm tôm, Dầu hào, Nước tương, Giấm, Mù tạt, Tương cà",
        "hải sản": "Tôm, Cua, Ghẹ, Mực, Ốc, Sò điệp, Hàu, Cá hồi, Cá thu, Lươn, Tôm hùm, Nghêu",
        "ngũ cốc & hạt": "Gạo, Ngô, Lúa mì, Đậu xanh, Đậu đen, Đậu phộng, Hạt điều, Hạt dẻ, Hạt sen, Mè, Hạt hướng dương, Yến mạch",
        "kẹo & snack": "Kẹo cao su, Kẹo mút, Kẹo dẻo, Socola, Snack, Bim bim, Bỏng ngô, Kẹo bông gòn, Kẹo dừa, Hạt dưa, Mứt, Bánh gạo",
        "món thịt": "Thịt kho, Thịt luộc, Sườn xào chua ngọt, Bò xào, Giò chả, Nem chua, Thịt đông, Lạp xưởng, Chả lụa, Bò bít tết, Bò né, Thịt quay",
        "món canh": "Canh chua, Canh khổ qua, Canh bí, Canh rau muống, Súp cua, Canh cải, Súp gà, Canh kim chi, Canh mồng tơi, Canh xương, Súp bí đỏ, Canh rong biển",
        "bữa ăn": "Bữa sáng, Bữa trưa, Bữa tối, Ăn vặt, Ăn khuya, Tiệc buffet, Tiệc nướng, Picnic, Mâm cỗ, Cơm nhà, Ăn tiệc, Ăn chay",
        "dụng cụ ăn": "Đũa, Muỗng, Nĩa, Dao ăn, Tô, Chén, Đĩa, Ly, Ống hút, Khay cơm, Tăm, Khăn ăn",
    },
    "animal": {
        "thú cưng": "Chó, Mèo, Thỏ, Hamster, Chuột lang, Vẹt, Cá vàng, Rùa cảnh, Chim yến, Sóc cảnh",
        "vật nuôi": "Heo, Bò, Trâu, Dê, Cừu, Ngựa, Gà, Vịt, Ngan, Ngỗng, Lừa, Bồ câu",
        "thú săn mồi": "Sư tử, Hổ, Báo hoa mai, Báo đốm, Sói, Cáo, Linh cẩu, Gấu, Sói xám, Mèo rừng, Chó rừng, Báo săn",
        "thú lớn": "Voi, Tê giác, Hà mã, Hươu cao cổ, Ngựa vằn, Lạc đà, Bò rừng, Gấu trúc, Kangaroo, Gấu bắc cực, Tuần lộc, Bò tót",
        "linh trưởng": "Khỉ, Đười ươi, Tinh tinh, Khỉ đột, Vượn, Culi, Khỉ đầu chó, Voọc, Khỉ mặt đỏ, Vượn tay dài",
        "loài chim": "Chim sẻ, Bồ câu, Đại bàng, Diều hâu, Cú mèo, Công, Hồng hạc, Chim cánh cụt, Đà điểu, Vẹt, Quạ, Chim én, Cò, Chim bói cá",
        "bò sát": "Rắn, Thằn lằn, Cá sấu, Tắc kè, Kỳ nhông, Rùa, Ba ba, Trăn, Cắc ké, Thạch sùng, Cự đà, Rắn hổ mang",
        "côn trùng": "Muỗi, Ruồi, Ong, Kiến, Gián, Châu chấu, Bướm, Chuồn chuồn, Dế, Bọ cánh cứng, Ve sầu, Đom đóm, Bọ rùa, Nhện, Mối, Bọ ngựa",
        "sinh vật biển": "Cá mập, Cá voi, Cá heo, Hải cẩu, Sứa, Bạch tuộc, Cá ngựa, Sao biển, Cá đuối, Rùa biển, Hải mã, Cá ngừ",
        "gặm nhấm": "Chuột, Sóc, Nhím, Hải ly, Chuột chũi, Chuột túi, Dơi, Thỏ rừng, Chuột đồng, Sóc bay",
        "cá nước ngọt": "Cá chép, Cá rô, Cá lóc, Cá trê, Cá basa, Cá diêu hồng, Cá tra, Cá koi, Cá thác lác, Cá chạch, Cá cờ, Cá betta",
        "bộ phận động vật": "Sừng, Vuốt, Cánh, Đuôi, Móng, Vảy, Râu, Nanh, Lông vũ, Mai rùa, Ngà, Bờm",
    },
    "thing": {
        "đồ học tập": "Bút chì, Bút bi, Thước kẻ, Cục tẩy, Compa, Cặp sách, Tập vở, Bút dạ quang, Kéo, Hồ dán, Bút mực, Bảng đen, Phấn, Máy tính cầm tay",
        "đồ nhà bếp": "Nồi, Chảo, Dao, Thớt, Vá, Rổ, Xoong, Ấm đun nước, Nồi áp suất, Máy xay sinh tố, Nồi chiên không dầu, Cái rây",
        "điện gia dụng": "Tủ lạnh, Máy giặt, Lò vi sóng, Nồi cơm điện, Quạt điện, Máy lạnh, Tivi, Máy sấy tóc, Bàn ủi, Máy hút bụi, Máy lọc nước, Bếp từ",
        "nội thất": "Ghế, Bàn, Tủ quần áo, Giường, Sofa, Kệ sách, Gương, Rèm cửa, Thảm, Đèn ngủ, Võng, Chiếu",
        "đồ phòng tắm": "Xà phòng, Dầu gội, Bàn chải đánh răng, Kem đánh răng, Khăn tắm, Vòi sen, Bồn tắm, Lược, Dao cạo, Chỉ nha khoa, Nước súc miệng, Bông tắm",
        "đồ điện tử": "Điện thoại, Máy tính bảng, Laptop, Tai nghe, Loa Bluetooth, Sạc dự phòng, Chuột máy tính, Bàn phím, Màn hình, Máy ảnh, Đồng hồ thông minh, Ổ cứng",
        "quần áo": "Áo thun, Áo sơ mi, Áo khoác, Áo len, Áo dài, Váy, Quần jean, Quần short, Quần tây, Đồ bơi, Áo hoodie, Áo ba lỗ, Pijama, Đầm dạ hội",
        "giày dép": "Dép lê, Giày thể thao, Giày cao gót, Sandal, Bốt, Dép tông, Giày lười, Giày bata, Guốc, Ủng",
        "phụ kiện": "Nón lá, Mũ lưỡi trai, Khăn quàng, Găng tay, Thắt lưng, Kính mát, Đồng hồ đeo tay, Nhẫn, Dây chuyền, Túi xách, Cà vạt, Khẩu trang",
        "đồ nghề": "Búa, Tua vít, Cờ lê, Kìm, Cưa, Máy khoan, Thước dây, Đinh, Ốc vít, Sơn, Thang, Keo dán",
        "đồ dọn dẹp": "Chổi, Cây lau nhà, Thùng rác, Xô, Bột giặt, Nước rửa chén, Giẻ lau, Khăn giấy, Nước lau sàn, Bình xịt muỗi, Chổi lông gà, Cây phất trần",
        "đồ văn phòng": "Máy in, Máy photocopy, Ghim bấm, Kẹp giấy, Bút xóa, Bảng trắng, Sổ tay, Lịch bàn, Băng keo, Con dấu, Bìa hồ sơ, Máy chiếu",
        "đồ chơi": "Búp bê, Lego, Xe điều khiển, Gấu bông, Diều, Bóng bay, Xếp hình, Cờ vua, Bài tây, Con quay, Súng nước, Đất nặn",
        "nhạc cụ": "Guitar, Piano, Violin, Trống, Sáo, Đàn tranh, Đàn bầu, Kèn, Harmonica, Ukulele, Đàn nhị, Organ",
        "dụng cụ y tế": "Ống nghe, Nhiệt kế, Kim tiêm, Băng gạc, Khẩu trang y tế, Máy đo huyết áp, Xe lăn, Nạng, Bông y tế, Thuốc, Cồn, Băng cá nhân",
        "thuốc & sức khoẻ": "Paracetamol, Vitamin C, Thuốc ho, Dầu gió, Cao xoa, Thuốc nhỏ mắt, Kháng sinh, Cồn sát khuẩn, Miếng dán hạ sốt, Thuốc cảm, Oresol, Thuốc ngủ",
        "đồ đi phượt": "Lều, Túi ngủ, Đèn pin, Bếp gas mini, Ba lô, Bản đồ, La bàn, Bình nước, Dao đa năng, Áo mưa, Gậy leo núi, Bật lửa",
        "vật liệu": "Gỗ, Sắt, Thép, Nhựa, Thuỷ tinh, Gốm, Vải, Giấy, Cao su, Đá, Xi măng, Gạch",
        "tiền bạc": "Tiền mặt, Thẻ ATM, Ví, Chuyển khoản, Séc, Ví điện tử, Mã QR, Thẻ tín dụng, Heo đất, Vàng, Cổ phiếu, Tiền xu",
        "tâm linh": "Nhang, Nến, Bàn thờ, Tràng hạt, Lì xì, Bùa, Vàng mã, Chuông, Tượng Phật, Quẻ xăm, Bát hương, Cúng rằm",
        "mỹ phẩm": "Son môi, Phấn, Kem chống nắng, Mascara, Kẹp tóc, Nước hoa, Sữa rửa mặt, Mặt nạ, Dầu xả, Tẩy trang, Kem dưỡng da, Sơn móng tay",
        "ánh sáng": "Đèn pin, Nến, Đèn dầu, Bóng đèn, Đèn neon, Đèn lồng, Đèn ông sao, Pháo sáng, Đuốc, Đèn flash, Đèn LED, Đèn bàn",
        "đồ mưa nắng": "Ô, Áo mưa, Nón bảo hiểm, Quạt tay, Áo ấm, Máy sưởi, Khăn choàng, Bình giữ nhiệt, Găng tay len, Kính râm, Dép đi mưa, Kem chống nắng",
        "đồ cho bé": "Bình sữa, Tã, Núm vú, Xe đẩy, Nôi, Yếm, Khăn sữa, Xe tập đi, Sữa bột, Lục lạc, Địu em bé, Chậu tắm",
        "túi & hành lý": "Vali, Ba lô, Túi tote, Túi đeo chéo, Túi du lịch, Cặp táp, Túi rút, Ví cầm tay, Túi giữ nhiệt, Balo laptop",
        "đồ chơi game": "Tay cầm, Bàn phím cơ, Ghế gaming, Tai nghe gaming, Máy console, Kính VR, Vô lăng game, Card đồ hoạ, Router wifi, Webcam",
        "dụng cụ đo": "Thước, Cân, Đồng hồ treo tường, Phong vũ biểu, Máy đo đường huyết, Đồng hồ bấm giờ, Ống nhòm, Kính hiển vi, Kính viễn vọng, Kính lúp",
    },
    "place": {
        "xe đường bộ": "Xe máy, Xe đạp, Xe buýt, Taxi, Xe tải, Xe container, Xe ba gác, Xích lô, Xe ôm, Xe điện, Xe khách, Xe limousine",
        "trên không & dưới nước": "Máy bay, Tàu hoả, Tàu thuỷ, Ca nô, Thuyền, Phà, Khinh khí cầu, Trực thăng, Tàu điện ngầm, Cáp treo, Thuyền thúng, Ghe",
        "xe chuyên dụng": "Xe cứu thương, Xe cứu hoả, Xe cảnh sát, Xe rác, Xe cẩu, Máy xúc, Xe lu, Xe bồn, Xe tang, Xe bọc thép, Xe đưa đón học sinh, Xe ủi",
        "nơi công cộng": "Bệnh viện, Trường học, Chợ, Siêu thị, Thư viện, Bưu điện, Ngân hàng, Đồn công an, Nhà thờ, Chùa, Nhà văn hoá, Toà án",
        "nơi giải trí": "Rạp phim, Công viên, Sở thú, Bảo tàng, Khu vui chơi, Quán karaoke, Bãi biển, Thuỷ cung, Quán net, Sân vận động, Hồ bơi, Khu trượt tuyết",
        "cửa hàng": "Tiệm tạp hoá, Tiệm bánh, Quán cà phê, Quán nhậu, Nhà hàng, Tiệm vàng, Hiệu thuốc, Tiệm hoa, Cửa hàng tiện lợi, Tiệm cắt tóc, Tiệm giặt ủi, Tiệm sửa xe",
        "cảnh thiên nhiên": "Núi, Sông, Hồ, Biển, Suối, Thác nước, Hang động, Đồi, Rừng, Sa mạc, Đảo, Đồng lúa",
        "thành phố VN": "Hà Nội, Sài Gòn, Đà Nẵng, Huế, Nha Trang, Đà Lạt, Cần Thơ, Hải Phòng, Vũng Tàu, Phú Quốc, Quy Nhơn, Buôn Ma Thuột",
        "danh thắng VN": "Vịnh Hạ Long, Sa Pa, Phong Nha, Hội An, Mũi Né, Tràng An, Côn Đảo, Mộc Châu, Hà Giang, Bà Nà Hills, Chợ nổi Cái Răng, Thánh địa Mỹ Sơn",
        "quốc gia châu Á": "Nhật Bản, Hàn Quốc, Trung Quốc, Thái Lan, Singapore, Ấn Độ, Malaysia, Indonesia, Lào, Campuchia, Philippines, Đài Loan",
        "quốc gia Âu Mỹ": "Pháp, Ý, Đức, Anh, Tây Ban Nha, Nga, Mỹ, Canada, Brazil, Mexico, Úc, Hà Lan",
        "công trình thế giới": "Tháp Eiffel, Tượng Nữ thần Tự do, Vạn Lý Trường Thành, Kim tự tháp, Angkor Wat, Taj Mahal, Đấu trường Colosseum, Tháp nghiêng Pisa, Big Ben, Cầu Cổng Vàng, Nhà hát Opera Sydney, Núi Phú Sĩ",
        "phòng trong nhà": "Phòng khách, Phòng ngủ, Phòng bếp, Phòng tắm, Ban công, Sân thượng, Tầng hầm, Gara, Sân vườn, Gác mái, Nhà kho, Hành lang",
        "nơi ở": "Chung cư, Biệt thự, Nhà cấp 4, Nhà sàn, Nhà ống, Lều trại, Khách sạn, Nhà trọ, Ký túc xá, Nhà nghỉ, Resort, Homestay",
        "trong trường": "Nhà ăn, Sân trường, Phòng học, Căn tin, Phòng thí nghiệm, Nhà thể chất, Phòng hiệu trưởng, Bảng tin, Cổng trường, Phòng y tế, Thư viện trường, Sân bóng",
        "chốn ăn chơi": "Quán bar, Quán pub, Quán trà sữa, Phố đi bộ, Chợ đêm, Sân khấu, Vũ trường, Quán lẩu, Quán cóc, Quán nướng, Quán cà phê sách, Quán game",
    },
    "people": {
        "ngành y": "Bác sĩ, Y tá, Dược sĩ, Nha sĩ, Hộ lý, Bác sĩ thú y, Điều dưỡng, Kỹ thuật viên xét nghiệm, Bác sĩ phẫu thuật, Nhà tâm lý, Bác sĩ nhi, Nữ hộ sinh",
        "ngành giáo": "Giáo viên, Giảng viên, Hiệu trưởng, Gia sư, Trợ giảng, Sinh viên, Học sinh, Nghiên cứu sinh, Giáo sư, Thủ khoa, Lớp trưởng, Bảo mẫu",
        "dịch vụ": "Đầu bếp, Nhân viên phục vụ, Thợ cắt tóc, Thợ may, Thợ xăm, Nhiếp ảnh gia, Nhân viên bán hàng, Tài xế, Shipper, Bảo vệ, Thu ngân, Lễ tân",
        "pháp luật & an ninh": "Công an, Cảnh sát giao thông, Bộ đội, Lính cứu hoả, Thám tử, Luật sư, Thẩm phán, Công tố viên, Thư ký toà, Trọng tài, Kiểm lâm, Hải quan",
        "nghệ sĩ": "Ca sĩ, Diễn viên, Hoạ sĩ, Nhà văn, Nhạc sĩ, Đạo diễn, MC, Vũ công, Ảo thuật gia, Nhà thiết kế, Nhà thơ, Diễn viên hài",
        "ngành công nghệ": "Lập trình viên, Kỹ sư, Hacker, Nhà khoa học, Streamer, YouTuber, TikToker, Game thủ, Designer, Kế toán, Data analyst, Tester",
        "lao động chân tay": "Nông dân, Ngư dân, Thợ mỏ, Thợ xây, Thợ điện, Thợ mộc, Thợ hàn, Thợ sửa xe, Tiều phu, Người chăn cừu, Công nhân, Lao công",
        "họ hàng": "Bố, Mẹ, Ông nội, Bà ngoại, Anh trai, Chị gái, Em út, Cô, Dì, Chú, Bác, Cậu",
        "quan hệ": "Người yêu, Vợ, Chồng, Bạn thân, Người yêu cũ, Crush, Tình địch, Đồng nghiệp, Hàng xóm, Bạn cùng phòng, Sếp, Bạn học",
        "nhân vật kỳ ảo": "Ma, Quỷ, Tiên, Phù thuỷ, Rồng, Ma cà rồng, Người sói, Thần đèn, Người tuyết, Người ngoài hành tinh, Robot, Zombie",
        "cổ tích Việt": "Thạch Sanh, Tấm, Cám, Sơn Tinh, Thuỷ Tinh, Thánh Gióng, Sọ Dừa, Chử Đồng Tử, Chú Cuội, Ông Bụt, Mỵ Châu, Trương Chi",
        "hoạt hình & truyện": "Doraemon, Conan, Pikachu, Naruto, Luffy, Goku, Shin, Nobita, Tom và Jerry, Mickey, Elsa, Spider-Man",
        "tính cách": "Keo kiệt, Hào phóng, Lười biếng, Chăm chỉ, Nhút nhát, Dũng cảm, Kiêu ngạo, Khiêm tốn, Nóng tính, Điềm tĩnh, Hay quên, Hậu đậu",
        "kiểu người": "Mọt sách, Cú đêm, Dân văn phòng, Fan cứng, Anti-fan, Người hướng nội, Người hướng ngoại, Kẻ nghiện việc, Con nghiện game, Tín đồ cà phê, Tín đồ thời trang, Thánh soi",
    },
    "fun": {
        "môn bóng": "Bóng đá, Bóng rổ, Bóng chuyền, Bóng bàn, Cầu lông, Tennis, Bóng ném, Bóng chày, Golf, Bi-a, Bóng bầu dục, Pickleball",
        "thể thao khác": "Bơi lội, Chạy bộ, Đạp xe, Yoga, Võ thuật, Quyền anh, Cử tạ, Leo núi, Trượt ván, Lặn biển, Gym, Nhảy dù",
        "trò chơi dân gian": "Nhảy dây, Đá cầu, Bịt mắt bắt dê, Trốn tìm, Ô ăn quan, Kéo co, Đánh đáo, Chơi chuyền, Cờ cá ngựa, Đập niêu, Bắn bi, Rồng rắn lên mây",
        "bài & cờ": "Bài tây, Cờ tướng, Cờ vua, Cờ caro, Cờ tỷ phú, Domino, Mạt chược, Uno, Poker, Sudoku, Ma sói, Xì dách",
        "game": "Liên Quân, Free Fire, PUBG, Minecraft, Among Us, Genshin Impact, Liên Minh Huyền Thoại, Roblox, Fortnite, Valorant, Candy Crush, Pokémon Go",
        "ứng dụng": "Facebook, TikTok, Instagram, Zalo, Telegram, YouTube, Twitter, Shopee, Grab, Discord, Messenger, Spotify",
        "hãng công nghệ": "Apple, Samsung, Xiaomi, Sony, Google, Microsoft, Nokia, Huawei, Oppo, Vinfast, Tesla, Nvidia",
        "thể loại nhạc": "Pop, Rap, Rock, Ballad, EDM, Cải lương, Hát chèo, Bolero, Jazz, Hip hop, Nhạc không lời, Nhạc trữ tình",
        "thể loại phim": "Phim kinh dị, Phim hài, Phim tình cảm, Phim hành động, Phim hoạt hình, Phim viễn tưởng, Phim tài liệu, Phim trinh thám, Phim cổ trang, Phim bộ, Phim chiếu rạp, Anime",
        "nghệ thuật biểu diễn": "Múa rối nước, Hát tuồng, Xiếc, Kịch nói, Opera, Nhạc kịch, Ballet, Hát quan họ, Ca trù, Đờn ca tài tử, Hát xẩm, Ảo thuật",
        "sở thích": "Đọc sách, Vẽ tranh, Chụp ảnh, Câu cá, Làm vườn, Nấu ăn, Sưu tầm tem, Xem phim, Nghe nhạc, Du lịch, Nhảy múa, Đan len",
        "môn học": "Toán, Ngữ văn, Vật lý, Hoá học, Sinh học, Lịch sử, Địa lý, Tiếng Anh, Thể dục, Tin học, Mỹ thuật, Âm nhạc",
        "cảm xúc": "Vui, Buồn, Giận, Sợ hãi, Ngạc nhiên, Ghen tị, Xấu hổ, Tự hào, Lo lắng, Hạnh phúc, Cô đơn, Hối hận",
        "hành động": "Chạy, Nhảy, Bơi, Bò, Leo, Trượt, Lăn, Ném, Đá, Đẩy, Kéo, Bắt",
        "việc hằng ngày": "Ăn cơm, Đánh răng, Rửa mặt, Tắm, Ngủ trưa, Nấu ăn, Giặt đồ, Quét nhà, Đi chợ, Tập thể dục, Tưới cây, Đọc báo",
        "cách giải trí": "Xem phim, Nghe nhạc, Hát karaoke, Chơi game, Đi dạo, Cắm trại, Đi bar, Livestream, Xem hài, Xem bóng đá, Đọc truyện tranh, Chơi cờ",
        "việc ở trường": "Kiểm tra, Thi cuối kỳ, Bài tập về nhà, Học bạ, Chào cờ, Giờ ra chơi, Học nhóm, Lớp học thêm, Đi trễ, Nghỉ hè, Tốt nghiệp, Khai giảng",
    },
    "nature": {
        "thời tiết": "Mưa, Nắng, Bão, Sương mù, Tuyết, Sấm sét, Cầu vồng, Gió, Mưa đá, Lũ lụt, Mưa phùn, Nồm",
        "thiên thể": "Mặt trời, Mặt trăng, Sao chổi, Sao Hoả, Sao Kim, Thiên hà, Nhật thực, Thiên thạch, Sao băng, Trái đất, Sao Mộc, Dải Ngân Hà",
        "các loài hoa": "Hoa hồng, Hoa cúc, Hoa sen, Hoa mai, Hoa đào, Hoa lan, Hoa hướng dương, Hoa ly, Hoa giấy, Hoa tulip, Hoa loa kèn, Hoa oải hương",
        "các loài cây": "Tre, Dừa, Cau, Phượng vĩ, Bàng, Đa, Thông, Liễu, Bạch đàn, Xương rồng, Lúa, Cây quất",
        "thảm hoạ": "Động đất, Sóng thần, Núi lửa, Lũ quét, Hạn hán, Cháy rừng, Sạt lở, Bão cát, Lốc xoáy, Dịch bệnh, Mất điện, Ngập lụt",
        "mùa & thời gian": "Mùa xuân, Mùa hạ, Mùa thu, Mùa đông, Bình minh, Hoàng hôn, Nửa đêm, Buổi trưa, Cuối tuần, Sinh nhật, Năm mới, Ngày mai",
        "địa hình": "Hẻm núi, Cao nguyên, Đồng bằng, Đảo san hô, Vịnh, Bán đảo, Đầm lầy, Cồn cát, Thung lũng, Vách đá, Sông băng, Hồ nước",
        "khoáng vật": "Vàng, Bạc, Kim cương, Ngọc trai, Đá quý, Than đá, Dầu mỏ, Thạch anh, Đồng, Sắt, Muối mỏ, Ngọc bích",
        "vũ trụ": "Tên lửa, Vệ tinh, Trạm vũ trụ, Phi hành gia, Tàu con thoi, UFO, Hố đen, Kính thiên văn, Robot thám hiểm, Sao Thuỷ, Vành đai tiểu hành tinh, Trạm Mặt Trăng",
        "nguồn nước": "Sông, Suối, Ao, Hồ, Kênh, Rạch, Giếng, Thác, Vòi phun, Biển, Đại dương, Cống",
    },
    "life": {
        "lễ hội": "Tết Nguyên Đán, Trung Thu, Giáng sinh, Halloween, Valentine, Ngày Phụ nữ, Giỗ Tổ, Tết Đoan Ngọ, Vu Lan, Rằm tháng Giêng, Lễ Phục sinh, Black Friday",
        "đồ Tết": "Bánh chưng, Mứt, Lì xì, Hoa đào, Câu đối, Pháo hoa, Mâm ngũ quả, Dưa hấu, Hạt dưa, Cây quất, Bao lì xì, Áo dài Tết",
        "đám cưới": "Nhẫn cưới, Váy cưới, Hoa cưới, Rước dâu, Tiệc cưới, Phong bì, Thiệp mời, Xe hoa, Chụp ảnh cưới, Lễ ăn hỏi, Tuần trăng mật, Phù dâu",
        "nơi thờ tự": "Chùa, Nhà thờ, Đền, Miếu, Đình, Thánh đường, Am, Nhà nguyện, Tháp Chàm, Chùa Một Cột, Thiền viện, Đàn tế",
        "bệnh thường gặp": "Cảm cúm, Sốt, Đau đầu, Ho, Đau bụng, Dị ứng, Tiêu chảy, Đau răng, Mất ngủ, Say xe, Chóng mặt, Nghẹt mũi",
        "bộ phận cơ thể": "Tay, Chân, Đầu, Mắt, Mũi, Miệng, Tai, Cổ, Vai, Đầu gối, Bụng, Lưng, Tóc, Răng",
        "màu sắc": "Màu đỏ, Màu cam, Màu vàng, Màu xanh lá, Màu xanh dương, Màu tím, Màu hồng, Màu nâu, Màu đen, Màu trắng, Màu xám, Màu bạc",
        "hình dạng": "Hình tròn, Hình vuông, Tam giác, Hình chữ nhật, Ngôi sao, Trái tim, Hình thoi, Hình elip, Xoắn ốc, Hình trụ, Hình nón, Hình bầu dục",
        "internet": "Wifi, 4G, Bluetooth, Email, Website, Ứng dụng, Mật khẩu, Tin nhắn, Đám mây, Virus, Bình luận, Tài khoản",
        "hành vi mạng xã hội": "Like, Share, Follow, Inbox, Story, Livestream, Trend, Meme, Hashtag, Check-in, Block, Comment",
        "công sở": "Cuộc họp, Deadline, Tăng lương, Sa thải, Tăng ca, Phỏng vấn, Thử việc, Nghỉ phép, Đồng phục, Báo cáo, Hợp đồng, Team building",
        "chi phí": "Tiền thuê nhà, Tiền điện, Tiền nước, Học phí, Viện phí, Tiền lãi, Tiền lương, Tiền thưởng, Thuế, Học bổng, Tiền mừng tuổi, Tiền boa",
        "nơi mua bán": "Chợ truyền thống, Siêu thị mini, Chợ đêm, Chợ nổi, Sạp hàng, Xe đẩy, Gánh hàng rong, Trung tâm thương mại, Hội chợ, Phiên chợ quê, Cửa hàng tạp hoá, Cửa hàng online",
        "kỹ năng": "Bơi, Lái xe, Nấu ăn, Vẽ, Hát, Múa, Đánh đàn, Nói tiếng Anh, Chụp ảnh, Lập trình, Đánh máy, Diễn thuyết",
        "thói quen xấu": "Cắn móng tay, Thức khuya, Nói dối, Trì hoãn, Hút thuốc, Uống rượu, Nghiện game, Ăn vặt đêm, Xả rác, Nói tục, Đi trễ, Nghiện mạng xã hội",
        "đơn vị": "Mét, Lít, Ki-lô-gam, Giờ, Phút, Giây, Ngày, Tuần, Tháng, Năm, Độ C, Ki-lô-mét",
    },
}

# ── CẶP DỄ NHẦM (chọn tay): topic -> [(từ A, từ B), ...] ─────────────────────
CURATED: dict[str, list[tuple[str, str]]] = {
    "food": [
        ("Phở", "Hủ tiếu"), ("Bánh chưng", "Bánh tét"), ("Chả giò", "Nem nướng"), ("Trà sữa", "Cà phê sữa"),
        ("Bún bò", "Bún riêu"), ("Bánh mì", "Bánh bao"), ("Kem", "Sữa chua"), ("Sinh tố", "Nước ép"),
        ("Coca", "Pepsi"), ("Pizza", "Bánh mì kẹp"), ("Sushi", "Kimbap"), ("Mì gói", "Mì Ý"),
        ("Cơm tấm", "Cơm gà"), ("Xôi", "Cháo"), ("Bánh flan", "Rau câu"), ("Kẹo", "Socola"),
        ("Bia", "Rượu vang"), ("Nước lọc", "Nước khoáng"), ("Muối", "Đường"), ("Tiêu", "Ớt"),
        ("Nước mắm", "Nước tương"), ("Cam", "Quýt"), ("Bưởi", "Cam sành"), ("Xoài", "Đu đủ"),
        ("Chuối", "Dứa"), ("Dưa hấu", "Dưa lưới"), ("Cà chua", "Ớt chuông"), ("Khoai tây", "Khoai lang"),
        ("Tôm", "Cua"), ("Gà rán", "Vịt quay"), ("Trà đá", "Trà nóng"), ("Bánh quy", "Bánh gạo"),
        ("Đũa", "Nĩa"), ("Bữa sáng", "Bữa tối"), ("Lẩu", "Nướng"), ("Cà phê", "Ca cao"),
    ],
    "animal": [
        ("Chó", "Sói"), ("Mèo", "Hổ"), ("Thỏ", "Chuột"), ("Cá heo", "Cá voi"), ("Cá sấu", "Thằn lằn"),
        ("Ngựa", "Lừa"), ("Bò", "Trâu"), ("Gà", "Vịt"), ("Bướm", "Ong"), ("Muỗi", "Ruồi"),
        ("Sư tử", "Hổ"), ("Voi", "Tê giác"), ("Cá mập", "Cá heo"), ("Đại bàng", "Diều hâu"),
        ("Rắn", "Lươn"), ("Nhện", "Bọ cạp"), ("Ếch", "Cóc"), ("Khỉ", "Đười ươi"), ("Cừu", "Dê"),
        ("Bồ câu", "Chim sẻ"), ("Cú mèo", "Dơi"), ("Cua", "Ghẹ"), ("Sứa", "Bạch tuộc"), ("Rùa", "Ba ba"),
    ],
    "thing": [
        ("Nhẫn", "Vòng tay"), ("Dây chuyền", "Vòng cổ"), ("Kính cận", "Kính râm"), ("Ba lô", "Vali"),
        ("Ví", "Túi xách"), ("Dao", "Kéo"), ("Bút chì", "Bút bi"), ("Vở", "Sách"), ("Thước kẻ", "Compa"),
        ("Bàn", "Ghế"), ("Gối", "Chăn"), ("Giường", "Võng"), ("Nồi cơm điện", "Nồi áp suất"),
        ("Tủ lạnh", "Tủ đông"), ("Quạt", "Máy lạnh"), ("Tivi", "Máy chiếu"), ("Điện thoại", "Máy tính bảng"),
        ("Laptop", "Máy tính bàn"), ("Tai nghe", "Loa"), ("Áo dài", "Váy"), ("Quần jean", "Quần kaki"),
        ("Áo thun", "Áo sơ mi"), ("Dép", "Giày"), ("Mũ bảo hiểm", "Nón lá"), ("Son", "Phấn"),
        ("Nước hoa", "Xịt phòng"), ("Xà phòng", "Sữa tắm"), ("Bàn chải", "Lược"), ("Bếp gas", "Bếp từ"),
        ("Búa", "Cưa"), ("Cầu thang", "Thang máy"), ("Cửa sổ", "Cửa chính"), ("Gương", "Kính"),
        ("Đồng hồ", "Lịch"), ("Bản đồ", "La bàn"), ("Đèn pin", "Nến"), ("Pin", "Ắc quy"),
        ("Guitar", "Ukulele"), ("Piano", "Organ"), ("Violin", "Cello"), ("Máy ảnh", "Máy quay phim"),
        ("Kính lúp", "Kính hiển vi"), ("Ô", "Áo mưa"), ("Khăn tắm", "Khăn mặt"), ("Chổi", "Cây lau nhà"),
    ],
    "place": [
        ("Xe máy", "Xe đạp điện"), ("Xe buýt", "Xe khách"), ("Taxi", "Xe ôm công nghệ"), ("Máy bay", "Trực thăng"),
        ("Tàu hoả", "Tàu điện"), ("Sông", "Suối"), ("Hồ", "Biển"), ("Núi", "Đồi"), ("Chùa", "Nhà thờ"),
        ("Trường học", "Nhà trẻ"), ("Bệnh viện", "Phòng khám"), ("Siêu thị", "Chợ"), ("Rạp phim", "Nhà hát"),
        ("Quán cà phê", "Quán trà sữa"), ("Khách sạn", "Nhà nghỉ"), ("Sân bay", "Bến xe"), ("Ga tàu", "Bến phà"),
        ("Hà Nội", "Huế"), ("Đà Lạt", "Sa Pa"), ("Nhật Bản", "Hàn Quốc"), ("Thư viện", "Hiệu sách"),
        ("Công viên", "Sở thú"), ("Bãi biển", "Hồ bơi"), ("Chung cư", "Nhà phố"), ("Phòng khách", "Phòng ngủ"),
    ],
    "people": [
        ("Bác sĩ", "Y tá"), ("Cô giáo", "Cô bảo mẫu"), ("Công an", "Bảo vệ"), ("Ca sĩ", "Rapper"),
        ("Diễn viên", "Người mẫu"), ("Đầu bếp", "Thợ làm bánh"), ("Luật sư", "Thẩm phán"), ("Thợ điện", "Thợ nước"),
        ("Shipper", "Tài xế taxi"), ("Streamer", "YouTuber"), ("Lập trình viên", "Hacker"), ("Kế toán", "Thu ngân"),
        ("Ông", "Bà"), ("Anh", "Chị"), ("Bạn thân", "Người yêu"), ("Sếp", "Đồng nghiệp"),
        ("Ma", "Quỷ"), ("Tiên", "Phù thuỷ"), ("Robot", "Người ngoài hành tinh"), ("Thám tử", "Cảnh sát"),
    ],
    "fun": [
        ("Facebook", "Instagram"), ("Zalo", "Messenger"), ("YouTube", "TikTok"), ("Shopee", "Lazada"),
        ("Grab", "Be"), ("Bóng đá", "Futsal"), ("Cầu lông", "Tennis"), ("Bóng bàn", "Bi-a"),
        ("Bơi lội", "Lặn biển"), ("Yoga", "Gym"), ("Karaoke", "Hát nhóm"), ("Cờ vua", "Cờ tướng"),
        ("Liên Quân", "Liên Minh Huyền Thoại"), ("Free Fire", "PUBG"), ("Toán", "Vật lý"), ("Văn", "Sử"),
        ("Phim kinh dị", "Phim hành động"), ("Anime", "Phim hoạt hình"), ("Đọc sách", "Đọc truyện tranh"),
    ],
    "nature": [
        ("Mưa", "Sương mù"), ("Bão", "Lốc xoáy"), ("Mặt trời", "Mặt trăng"), ("Sao", "Đom đóm"),
        ("Hoa hồng", "Hoa cúc"), ("Hoa đào", "Hoa mai"), ("Tre", "Trúc"), ("Sấm", "Sét"),
        ("Động đất", "Sóng thần"), ("Núi lửa", "Suối nước nóng"), ("Sa mạc", "Bãi biển"), ("Tuyết", "Mưa đá"),
        ("Vàng", "Bạc"), ("Kim cương", "Thuỷ tinh"), ("Cầu vồng", "Cực quang"),
    ],
    "life": [
        ("Tết", "Giáng sinh"), ("Sinh nhật", "Đám cưới"), ("Đám cưới", "Lễ ăn hỏi"), ("Lì xì", "Phong bì"),
        ("Đi học", "Đi làm"), ("Thi cử", "Kiểm tra"), ("Cảm cúm", "Sốt xuất huyết"), ("Đau đầu", "Chóng mặt"),
        ("Wifi", "4G"), ("Email", "Tin nhắn"), ("Mật khẩu", "Mã OTP"), ("Livestream", "Video"),
        ("Học bổng", "Học phí"), ("Tiền lương", "Tiền thưởng"), ("Nghỉ phép", "Nghỉ việc"),
    ],
}

# ── Dựng chỉ mục ─────────────────────────────────────────────────────────────
CLUSTERS: list[tuple[str, str, list[str]]] = []      # (topic, tên nhóm, [từ])
for _topic, _groups in _RAW.items():
    for _gname, _words in _groups.items():
        _lst = [w.strip() for w in _words.split(",") if w.strip()]
        CLUSTERS.append((_topic, _gname, list(dict.fromkeys(_lst))))


def normalize(text: str) -> str:
    """Bỏ dấu + hạ chữ thường + gọn khoảng trắng — dùng để so khớp 'nói thẳng từ khoá'."""
    t = unicodedata.normalize("NFD", text or "")
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = t.replace("đ", "d").replace("Đ", "d").lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# Âm tiết quá phổ thông — nói ra không coi là "nói thẳng từ khoá"
_GENERIC = {
    "con", "cai", "qua", "trai", "nguoi", "xe", "nha", "cay", "hoa", "banh", "mon", "bai", "bo",
    "ca", "an", "nuoc", "phong", "may", "quan", "ao", "thu", "tiem", "cua", "tho", "nhan", "bua",
    "khu", "sao", "mat", "tien", "cho", "mua", "lam", "dong", "tay", "chan", "dau", "mau", "hinh",
}


def contains_forbidden(desc: str, word: str) -> bool:
    """True nếu lời mô tả 'nói thẳng' từ khoá: chứa nguyên cụm, hoặc chứa 1 âm tiết đặc trưng
    (>=3 ký tự, không phổ thông) của từ nhiều âm tiết."""
    d = normalize(desc)
    w = normalize(word)
    if not w:
        return False
    if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", d):
        return True
    toks = w.split()
    if len(toks) >= 2:
        d_toks = set(d.split())
        for s in toks:
            if len(s) >= 3 and s not in _GENERIC and s in d_toks:
                return True
    return False


def vocab_stats() -> dict:
    words = {w for _, _, ws in CLUSTERS for w in ws}
    for lst in CURATED.values():
        for a, b in lst:
            words.add(a); words.add(b)
    pairs = sum(len(ws) * (len(ws) - 1) // 2 for _, _, ws in CLUSTERS) + sum(len(v) for v in CURATED.values())
    return {"words": len(words), "clusters": len(CLUSTERS), "pairs": pairs}


def pick_word_pair(rng: random.Random, used: set[str] | None = None, topic: str = "all"):
    """Bốc 1 cặp từ (A, B, nhãn nhóm). A = từ của số đông, B = từ của kẻ giả mạo (đã xáo ngẫu nhiên).
    `used` = tập từ (đã normalize) đã dùng trong ván -> tránh lặp."""
    used = used if used is not None else set()
    topics = list(TOPICS) if topic in (None, "all") else [topic]
    topics = [t for t in topics if t != "all"]
    clusters = [c for c in CLUSTERS if c[0] in topics]
    curated = [(t, p) for t in topics for p in CURATED.get(t, [])]

    def fresh(a: str, b: str) -> bool:
        return normalize(a) not in used and normalize(b) not in used and normalize(a) != normalize(b)

    for _ in range(60):
        if curated and (not clusters or rng.random() < 0.4):
            t, (a, b) = rng.choice(curated)
            label = TOPICS.get(t, "")
        else:
            t, gname, words = rng.choice(clusters)
            if len(words) < 2:
                continue
            a, b = rng.sample(words, 2)
            label = f"{TOPICS.get(t, '')} · {gname}"
        if fresh(a, b):
            break
    else:                               # kho chủ đề nhỏ hoặc đã dùng hết -> cho phép lặp
        t, gname, words = rng.choice(clusters or CLUSTERS)
        a, b = rng.sample(words, 2)
        label = f"{TOPICS.get(t, '')} · {gname}"
    if rng.random() < 0.5:
        a, b = b, a
    used.add(normalize(a)); used.add(normalize(b))
    return a, b, label
