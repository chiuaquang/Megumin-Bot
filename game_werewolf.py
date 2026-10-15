"""
game_werewolf.py — Engine MA SÓI (thuần logic, KHÔNG phụ thuộc Telegram, KHÔNG có AI).

Chỉ có NGƯỜI CHƠI THẬT với nhau. Lớp Telegram (phòng chờ, DM, nút bấm, hẹn giờ) nằm ở
handlers_werewolf.py; file này chỉ giữ luật chơi nên test được độc lập.

Điểm đặc biệt: "VÒNG MÔ TẢ — TÌM KẺ GIẢ MẠO"
  Mỗi ngày bot bốc 1 cặp từ na ná nhau (xem ww_words.py). Số đông nhận từ A, phe "giả mạo"
  (Sói, Kẻ Ngốc, Người Lạc Lối...) nhận từ B. Mỗi người mô tả từ của mình bằng 1 câu (không được
  nói thẳng). Ai mô tả "lệch" số đông sẽ bị nghi ngờ và bị vote — nhưng Sói cũng có thể đoán từ thật
  qua lời người khác để ngụy trang, còn Sói Nằm Vùng thì cầm luôn từ của Dân Làng.

Hệ vai trò: mỗi vai là 1 class kế thừa `Role` (tên, phe, mô tả, hành động đêm, nhóm từ khoá...).
"""
from __future__ import annotations

import math
import random
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ww_words import pick_word_pair, contains_forbidden, TOPICS

NONE = -1                # "bỏ qua / không chọn ai"
MIN_PLAYERS = 5
MAX_PLAYERS = 20

# ── Cân bằng (chỉnh ở đây) ──────────────────────────────────
SK_MIN_PLAYERS = 12          # Sát Nhân Hàng Loạt chỉ xuất hiện khi ván có ≥ ngần này người
DETECTIVE_MIN_PLAYERS = 13   # Thám Tử chỉ xuất hiện khi ván có TRÊN 12 người
LAST_STAND_MIN_OTHERS = 3    # "Đường Cùng" của Sói: còn 1 sói mà người khác ≥ ngần này
DETECTIVE_CHANCE = 0.45      # xác suất Thám Tử điều tra ra manh mối về hung thủ
SK_NO_LETTER_MSG = ("Sát Nhân biết thân phận của mọi người — gửi thư nặc danh sẽ phá game, "
                   "nên bạn không được gửi thư.")
FORGERY_FLAW = 0.30          # xác suất Thám Tử nhận ra chứng cứ bị Sát Nhân làm giả
BLOOD_SCENT = 0.25           # xác suất Sói ngửi thấy mùi máu mỗi khi Sát Nhân ra tay
SPY_BASE, SPY_STEP, SPY_MAX = 0.03, 0.04, 0.60   # xác suất Sói phát giác Sát Nhân mạo danh (tăng theo số tin đã nói)
ALPHA_MIN_PLAYERS = 13       # Sói Đầu Đàn: ván có TRÊN 12 người (cùng ngưỡng Thám Tử)
ALPHA_PURGE_MAX = 2          # số lần Sói Đầu Đàn được thanh trừng sói trong đàn / ván
# Sát Nhân: đêm N lập kế hoạch (chọn mục tiêu) → đêm N+1 ra tay + làm giả chứng cứ. Mục tiêu chết trước đó = kế hoạch thất bại.
KNIGHT_SWORD = 2             # số nhát kiếm của Hiệp Sĩ (mỗi ngày tối đa 1 nhát)
LS_FRENZY_OTHERS = 5          # Đường Cùng: còn >= ngần này người khác thì Sói điên cuồng, cắn thêm 1 nạn nhân ngẫu nhiên (99 = tắt)
LS_PIERCE_HEAL = True        # Đường Cùng: nanh xuyên luôn cả bình CỨU của Phù Thuỷ
LS_BLIND_SEER = True         # Đường Cùng: Tiên Tri không soi ra con sói cuối cùng
LS_ALPHA_NEEDS_REVIVE_USED = True   # có Sói Đầu Đàn: con sói cuối cùng trong bầy chỉ được buff Đường Cùng khi Đầu Đàn ĐÃ dùng hết lượt hồi sinh
PLAIN_FRAC_STEPS = ((5, 0.45), (6, 0.60), (8, 0.45), (11, 0.55), (15, 0.45), (16, 0.60), (18, 0.35), (20, 0.45))   # (số người tối đa, tỉ lệ Dân Làng thường tối thiểu)
DREAM_MSGS = 3               # số tin Già Làng được báo mộng (1 đêm duy nhất)


class Team(str, Enum):
    VILLAGE = "village"
    WOLF = "wolf"
    NEUTRAL = "neutral"


TEAM_LABEL = {Team.VILLAGE: "🟢 Phe Dân Làng", Team.WOLF: "🔴 Phe Ma Sói", Team.NEUTRAL: "🟣 Phe Thứ Ba"}


# ════════════════════════════════════════════════════════════════
# CÀI ĐẶT VÁN
# ════════════════════════════════════════════════════════════════

@dataclass
class WWSettings:
    word_round: bool = True      # Vòng Mô Tả — tìm kẻ giả mạo
    secret_vote: bool = False    # phiếu kín (mặc định công khai để "đủ drama")
    reveal_roles: bool = True    # lộ vai khi chết
    letters: bool = True         # thư nặc danh
    silence: bool = True         # xoá tin nhắn của người đã chết (tin ban đêm LUÔN bị xoá để bảo vệ kênh chat bí mật)
    speed: str = "normal"        # fast | normal | slow
    topic: str = "all"           # chủ đề từ khoá
    mode: str = "balanced"       # classic | balanced | drama

    def scale(self) -> float:
        return {"fast": 0.6, "normal": 1.0, "slow": 1.7}.get(self.speed, 1.0)


# ════════════════════════════════════════════════════════════════
# CÁC LỚP VAI TRÒ
# ════════════════════════════════════════════════════════════════

LAST_STAND_TEXT = ("🩸 *ĐƯỜNG CÙNG:* khi bạn là Sói DUY NHẤT còn sống trong bầy (Sói Đầu Đàn không tính là 1 con trong bầy) mà làng còn ≥ %d người khác, bạn được buff: "
                   "① nanh xuyên Bảo Vệ (Phù Thuỷ vẫn cứu được) ② nhận TỪ KHOÁ CỦA DÂN LÀNG ở Vòng Mô Tả "
                   "③ mỗi đêm tự dò 1 người xem có chức năng đặc biệt không." % LAST_STAND_MIN_OTHERS)


class Role:
    key = "villager"
    name = "Dân Làng"
    emoji = "🧑‍🌾"
    team = Team.VILLAGE
    detail = ""
    win_text = "Tiêu diệt toàn bộ Ma Sói và mọi kẻ giết người."
    word_group = "A"            # "A" = từ số đông · "B" = từ kẻ giả mạo
    seer_reads_wolf = False     # Tiên Tri soi ra "Sói"?
    detect_team: Optional[str] = None   # Thám Tử coi vai này thuộc phe nào (None = theo team)
    has_power = False           # Sói Trinh Sát dò ra "có chức năng"?
    village_power = False       # (không còn dùng — Già Làng bị treo không còn làm Dân Làng mất năng lực)
    public = False              # True = cả làng biết ai giữ vai này ngay từ đầu (chỉ Già Làng)
    night_kinds: tuple = ()     # các hành động đêm (xem KIND_INFO)
    night_chat: Optional[str] = None   # kênh chat bí mật ban đêm qua DM bot ("wolf" = cả bầy sói)

    @property
    def is_wolf(self) -> bool:
        return self.team == Team.WOLF

    def align_key(self) -> str:
        """Khoá phe dùng cho Thám Tử: 2 vai cùng khoá => 'cùng phe'."""
        if self.detect_team:
            return self.detect_team
        return self.team.value if self.team != Team.NEUTRAL else self.key


class Villager(Role):
    detail = "Bạn không có năng lực đặc biệt. Vũ khí của bạn là trí nhớ, cái miệng và lá phiếu — hãy soi kỹ từng lời mô tả!"


class Seer(Role):
    key, name, emoji = "seer", "Tiên Tri", "🔮"
    detail = "Mỗi đêm soi 1 người để biết họ CÓ phải Ma Sói hay không. (Coi chừng Sói Nằm Vùng — hắn qua mặt được bạn!)"
    village_power = has_power = True
    night_kinds = ("seer",)


class Guardian(Role):
    key, name, emoji = "guardian", "Bảo Vệ", "🛡️"
    detail = ("Mỗi đêm bảo vệ 1 người (kể cả bản thân) khỏi cú cắn của Sói. Không chặn được Sát Nhân Hàng Loạt. "
              "Không được bảo vệ cùng 1 người 2 đêm liên tiếp. (Sói đơn độc ở 'Đường Cùng' có nanh xuyên được lá chắn!)")
    village_power = has_power = True
    night_kinds = ("guard",)

    def __init__(self):
        self.last_guard: Optional[int] = None


class Witch(Role):
    key, name, emoji = "witch", "Phù Thuỷ", "🧙‍♀️"
    detail = ("Có 1 bình CỨU và 1 bình ĐỘC — mỗi loại dùng 1 lần cả ván. Mỗi đêm bạn được thấy Sói định cắn ai "
              "rồi quyết định cứu, đầu độc hay làm ngơ.")
    village_power = has_power = True

    def __init__(self):
        self.heal = True
        self.poison = True


class Hunter(Role):
    key, name, emoji = "hunter", "Thợ Săn", "🏹"
    detail = ("Chỉ khi bạn bị SÓI CẮN hoặc bị SÁT NHÂN giết, bạn mới được bắn chết 1 người kéo theo. "
              "Bị treo cổ, đầu độc, trúng đạn hay chết theo người yêu thì mũi tên vô dụng.")
    village_power = has_power = True


class Cupid(Role):
    key, name, emoji = "cupid", "Thần Tình Yêu", "💘"
    detail = ("Đêm đầu tiên ghép đôi 2 người thành cặp tình nhân (có thể gồm cả bạn). Một người chết, người kia chết theo. "
              "Nếu cặp đôi khác phe, họ chỉ thắng khi là 2 người cuối cùng còn sống!")
    village_power = has_power = True
    night_kinds = ("cupid",)


class Elder(Role):
    key, name, emoji = "elder", "Già Làng", "👴"
    public = True
    detail = ("Vai DUY NHẤT không che đậy: cả 3 phe đều biết bạn là Già Làng ngay từ đầu. Bạn sống sót sau lần đầu bị Sói cắn. "
              "Bạn biết ai là Thị Trưởng và Phù Thuỷ, và mỗi đêm được chọn 1 trong 2 người đó để thì thầm riêng. "
              "Khi bạn chết (kể cả bị treo cổ — Dân Làng KHÔNG mất năng lực), linh hồn bạn nhìn thấu Sói và Sát Nhân; "
              f"đêm hôm sau bạn có thể BÁO MỘNG vào nhóm chính (tối đa {DREAM_MSGS} tin, chỉ 1 đêm).")
    has_power = True
    night_kinds = ("elderlink",)

    def __init__(self):
        self.lives = 2
        self.death_night: Optional[int] = None
        self.dream_left = DREAM_MSGS


class Detective(Role):
    key, name, emoji = "detective", "Thám Tử", "🕵️"
    detail = (f"Chỉ xuất hiện khi ván có trên {DETECTIVE_MIN_PLAYERS - 1} người. Sau đêm có người bị GIẾT (Sói/Sát Nhân), đêm kế tiếp bạn chọn 1 nạn nhân để điều tra: "
              f"~{int(DETECTIVE_CHANCE * 100)}% tìm ra manh mối chỉ về hung thủ. Coi chừng — Sát Nhân có thể làm giả chứng cứ để đổ tội cho người vô tội!")
    village_power = has_power = True
    night_kinds = ("investigate",)


class LittleGirl(Role):
    key, name, emoji = "littlegirl", "Cô Bé Tò Mò", "👧"
    detail = ("Mỗi đêm có thể hé mắt nhìn trộm bầy Sói: có thể thấy mặt 1 con sói… nhưng cũng có thể bị bắt gặp — "
              "khi đó Sói biết bạn là ai!")
    village_power = has_power = True
    night_kinds = ("peek",)


class Gunner(Role):
    key, name, emoji = "gunner", "Xạ Thủ", "🔫"
    detail = "Có 2 viên đạn. Ban ngày có thể công khai bắn chết 1 người. Bắn là LỘ DIỆN: cả làng biết bạn là Xạ Thủ."
    village_power = has_power = True

    def __init__(self):
        self.bullets = 2


class Mayor(Role):
    key, name, emoji = "mayor", "Thị Trưởng", "🎖️"
    detail = ("Ban ngày có thể lộ diện. Từ đó: 🗳️ phiếu ×2 · ⚖️ PHÁ HOÀ — phiếu hoà thì bạn chọn người bị treo · "
              "🛡️ KHIÊN CHỨC VỤ — sống sót 1 lần bị Sói cắn (không chống được Sát Nhân hay treo cổ) · "
              "👑 KẾ NHIỆM — khi chết chọn 1 người thừa hưởng phiếu ×2. Sói sẽ để mắt tới bạn đấy.")
    has_power = True

    def __init__(self):
        self.revealed = False
        self.shield = False


class Judge(Role):
    key, name, emoji = "judge", "Quan Toà", "⚖️"
    detail = "1 lần duy nhất, ngay trước giờ hành quyết, bạn có thể HUỶ án treo cổ. Bạn sẽ bị lộ danh tính."
    village_power = has_power = True

    def __init__(self):
        self.used = False


class LostSoul(Role):
    key, name, emoji = "lostsoul", "Người Lạc Lối", "🫥"
    word_group = "B"
    detail = ("Bạn thuộc phe Dân Làng — nhưng từ khoá mỗi ngày của bạn lại là từ của KẺ GIẢ MẠO, và bạn KHÔNG hề biết. "
              "Nếu mô tả của bạn bị chỉ điểm, hãy thuyết phục cả làng rằng bạn vô tội!")


class Mage(Role):
    key, name, emoji = "mage", "Pháp Sư", "🧿"
    detail = ("1 LẦN duy nhất cả ván, vào đêm bạn có thể HỒI SINH 1 người khác đã chết (giữ nguyên vai). "
              "Người đó trở lại lúc trời sáng. Không hồi sinh được người vừa chết trong đêm nay.")
    has_power = True
    night_kinds = ("revive",)

    def __init__(self):
        self.used = False


class Werewolf(Role):
    key, name, emoji = "werewolf", "Ma Sói", "🐺"
    team = Team.WOLF
    word_group = "B"
    win_text = "Giết sạch mọi người không phải Sói (và không còn Sát Nhân Hàng Loạt) — không thắng theo số đông."
    detail = ("Mỗi đêm cả bầy thống nhất cắn 1 người. Từ khoá của bạn KHÁC số đông — hãy đoán từ thật qua lời mô tả của "
              "mọi người rồi ngụy trang cho khéo!")
    night_kinds = ("wolf",)
    night_chat = "wolf"
    seer_reads_wolf = True


class CursedWolf(Werewolf):
    key, name, emoji = "cursedwolf", "Sói Nguyền", "🧬"
    detail = ("Như Ma Sói, nhưng 1 LẦN trong ván có thể dùng lời nguyền: nạn nhân của đêm đó không chết mà biến thành "
              "Sói. (Bị Bảo Vệ/Phù Thuỷ chặn thì lời nguyền chưa bị tiêu hao.)")
    has_power = True
    night_kinds = ("wolf", "curse")

    def __init__(self):
        self.curse_left = True


class ScoutWolf(Werewolf):
    key, name, emoji = "scoutwolf", "Sói Trinh Sát", "🔭"
    detail = "Như Ma Sói, mỗi đêm còn dò 1 người xem họ có phải vai ĐẶC BIỆT (Tiên Tri, Phù Thuỷ...) hay chỉ là dân thường."
    has_power = True
    night_kinds = ("wolf", "scout")


class SleeperWolf(Werewolf):
    key, name, emoji = "sleeperwolf", "Sói Nằm Vùng", "🎭"
    word_group = "A"
    seer_reads_wolf = False
    detect_team = "village"
    has_power = True
    detail = ("Sói cài cắm: Tiên Tri và Thám Tử thấy bạn là dân lành. Đặc biệt bạn nhận ĐÚNG TỪ KHOÁ CỦA DÂN LÀNG — hãy "
              "dẫn dắt lá phiếu để hại chính họ.")


class AlphaWolf(Werewolf):
    key, name, emoji = "alphawolf", "Sói Đầu Đàn", "👑"
    public = True
    has_power = True
    detail = (f"Chỉ xuất hiện khi ván có trên {ALPHA_MIN_PLAYERS - 1} người và CÔNG KHAI như Già Làng: cả làng biết bạn là Sói Đầu Đàn.\n"
              "🛡️ BẤT KHẢ XÂM PHẠM: không ai nhắm được bạn (cắn, độc, đạn, mũi tên, dao, kiếm, treo cổ) — CHỪNG NÀO còn sói khác trong đàn. "
              "⚖️ MẤT ĐÀN LÀ CHẾT: hết sói trong đàn thì bạn không thể tồn tại — sáng ngày hôm sau bạn bị Dân Làng treo cổ NGAY (không qua bỏ phiếu). Thần Tình Yêu cũng không ghép được bạn.\n"
              "✨ Hồi sinh 1 con sói đã chết (1 lần/ván). 🗡️ Thanh trừng: ban đêm giết 1 sói trong đàn (kể cả Sát Nhân đã bị nguyền vào bầy) "
              f"(tối đa {ALPHA_PURGE_MAX} lần/ván — dọn sói bị lộ, nhưng đàn yếu đi).\n"
              "🚫 Bạn KHÔNG được bỏ phiếu ban ngày. 💬 Ban đêm bạn được chat thoải mái ở nhóm chính (tin không bị xoá).\n"
              "🩸 Khi đàn chỉ còn 1 con sói (và bạn đã dùng hết lượt hồi sinh), con sói đó — không phải bạn — nhận buff Đường Cùng.")
    night_kinds = ("wolf", "purge", "packrevive")

    def __init__(self):
        self.curse_left = False
        self.purge_left = ALPHA_PURGE_MAX
        self.revive_left = 1


class Knight(Role):
    key, name, emoji = "knight", "Hiệp Sĩ", "⚔️"
    detail = ("Bạn là người DUY NHẤT còn sống của phe Dân Làng — và là Dân Làng thường — nên được PHONG HIỆP SĨ (cả làng biết):\n"
              f"⚔️ Kiếm: ban ngày chém 1 người bất kỳ ({KNIGHT_SWORD} nhát, mỗi ngày tối đa 1 nhát). Chém nhầm người phe Dân Làng thì GIÁP VỠ.\n"
              "🛡️ Giáp: đỡ được 1 lần Sói cắn (không đỡ được Sát Nhân). 🚫 Miễn nhiễm phiếu treo cổ.")
    has_power = True

    def __init__(self):
        self.armor = True
        self.sword = KNIGHT_SWORD
        self.last_swing = -1


class SerialKiller(Role):
    key, name, emoji = "serialkiller", "Sát Nhân Hàng Loạt", "🔪"
    team = Team.NEUTRAL
    word_group = "A"             # nhận TỪ KHOÁ CỦA DÂN LÀNG ở Vòng Mô Tả
    win_text = "Tiêu diệt cả 3 phe cho tới khi chỉ còn MỘT MÌNH bạn."
    detail = (f"Chỉ xuất hiện khi ván có từ {SK_MIN_PLAYERS} người. Bạn sống lẫn trong Dân Làng nên được tin tưởng. "
              "Nhát dao BỎ QUA MỌI phòng thủ (Bảo Vệ, khiên, mạng phụ...). Tiên Tri không soi ra bạn. "
              "Bạn có thể tự nhận là bất cứ vai nào, nhưng không có kỹ năng của vai đó.\n"
              "🧠 Bạn biết THÂN PHẬN (vai trò) của mọi người: bầy sói, Dân Làng lẫn phe thứ ba. "
              "Ở Vòng Mô Tả bạn nhận TỪ KHOÁ CỦA DÂN LÀNG.\n"
              "📝 Kế hoạch giết người: đêm này chọn mục tiêu, ĐÊM HÔM SAU mới ra tay (giết + dựng chứng cứ cần thời gian). "
              "Nếu mục tiêu chết trước khi bạn ra tay (bất kể ai giết) thì kế hoạch thất bại — phải chọn lại mục tiêu, lập kế hoạch từ đầu. "
              "Khi chỉ còn 1 người sống sót cùng bạn (người yêu của bạn không tính), bạn ra tay NGAY, ngày hay đêm, không cần kế hoạch.\n"
              "🎭 Làm giả chứng cứ: vào đêm ra tay, chọn 1 người để đổ tội trước Thám Tử.\n"
              "👂 Nghe lén kênh chat đêm của Sói và có thể mạo danh sói: TỰ CHỌN đội tên con sói nào trong bầy — "
              "càng nói nhiều càng dễ bị phát giác. Mỗi lần ra tay cũng có nguy cơ Sói ngửi thấy mùi máu.\n"
              "🥷 Né được 1 lần bị Sói cắn. Nếu bị ghép đôi tình nhân: người yêu chết bạn KHÔNG chết (nhưng sẽ bị lộ). "
              "Bị Sói nguyền, bạn vào bầy sói và được VOTE CẮN ban đêm như các sói khác, nhưng vẫn giữ mục tiêu: giết tất cả mới thắng.")
    has_power = True
    night_kinds = ("serial", "frame")

    def __init__(self):
        self.dodge_left = 1
        self.plan: Optional[int] = None   # mục tiêu đã lên kế hoạch (ra tay vào đêm kế tiếp)
        self.plan_night = -9              # đêm đã lập kế hoạch
        self.spy_as: Optional[int] = None # đang đội tên con sói nào khi mạo danh
        self.converted = False       # bị Sói Nguyền: vào bầy sói (vote cắn được) nhưng vẫn là Sát Nhân
        self.exposed = False         # đã bị Sói phát giác khi mạo danh
        self.talk_count = 0
        self.decoy: Optional[int] = None   # người 'chung nghi vấn' khi Sói ngửi mùi máu


class Executioner(Role):
    key, name, emoji = "executioner", "Đao Phủ", "🪓"
    team = Team.NEUTRAL
    win_text = "Dân Làng treo cổ đúng MỤC TIÊU của bạn (ván vẫn tiếp tục — chiến thắng của bạn được công bố khi ván kết thúc)."
    detail = ("Bạn được giao 1 mục tiêu. Hãy thuyết phục cả làng treo cổ người đó! Nếu mục tiêu chết bằng cách khác, "
              "bạn trở thành Dân Làng.")

    def __init__(self):
        self.target: Optional[int] = None


ROLE_CLASSES: dict[str, type] = {c.key: c for c in (
    Villager, Seer, Guardian, Witch, Hunter, Cupid, Elder, Detective, LittleGirl, Gunner, Mayor, Judge, Mage,
    LostSoul, Werewolf, CursedWolf, ScoutWolf, SleeperWolf, AlphaWolf, SerialKiller, Executioner, Knight,
)}


def make_role(key: str) -> Role:
    return ROLE_CLASSES[key]()


# Nhãn hiển thị + luật cho từng loại hành động đêm
KIND_INFO = {
    #        need: số mục tiêu cần chọn (0 = có/không) · locked: chọn xong là chốt · required: phải xong mới kết thúc sớm
    "wolf":   dict(need=1, locked=False, required=True),
    "curse":  dict(need=0, locked=True,  required=False),
    "scout":  dict(need=1, locked=True,  required=True),
    "guard":  dict(need=1, locked=False, required=True),
    "seer":   dict(need=1, locked=True,  required=True),
    "investigate": dict(need=1, locked=True, required=True),
    "cupid":  dict(need=2, locked=True,  required=True),
    "serial": dict(need=1, locked=False, required=True),
    "frame":  dict(need=1, locked=False, required=False),
    "revive": dict(need=1, locked=True,  required=False),
    "elderlink": dict(need=1, locked=True, required=False),
    "purge":  dict(need=1, locked=True,  required=False),
    "packrevive": dict(need=1, locked=True, required=False),
    "peek":   dict(need=0, locked=True,  required=True),
}

CAUSE_TEXT = {
    "wolf": "bị Sói cắn", "lynch": "bị treo cổ", "poison": "bị Phù Thuỷ đầu độc", "hunter": "trúng đạn Thợ Săn",
    "gunner": "trúng đạn Xạ Thủ", "serial": "bị Sát Nhân Hàng Loạt giết", "heartbreak": "chết theo người yêu",
    "purge": "bị Sói Đầu Đàn thanh trừng", "sword": "bị Hiệp Sĩ chém",
}


# ════════════════════════════════════════════════════════════════
# CHIA VAI
# ════════════════════════════════════════════════════════════════

WOLF_STEPS = ((6, 1), (11, 2), (16, 3), (20, 4))     # (số người tối đa, số Sói) — chỉnh theo mô phỏng cân bằng


def wolf_count(n: int) -> int:
    for cap, w in WOLF_STEPS:
        if n <= cap:
            return w
    return WOLF_STEPS[-1][1]


def build_roles(n: int, mode: str, word_round: bool, rng: random.Random) -> list[str]:
    """Trả danh sách khoá vai (đã xáo) cho n người. Cân bằng theo số người + chế độ."""
    w = wolf_count(n)
    wolves = ["werewolf"] * w
    if w == 1 and mode != "classic":
        wolves[0] = "cursedwolf"                 # Sói đơn độc (ván 5-6 người) mang Lời Nguyền: có đường lật kèo bằng cách biến nạn nhân thành Sói
    if w >= 2 and (n >= 8 or mode == "drama"):
        wolves[1] = "cursedwolf"
    if w >= 2 and mode != "classic" and n >= 9:
        wolves[-1] = "sleeperwolf"
    if w >= 3 and mode != "classic":
        wolves[2 if wolves[2] == "werewolf" else 0] = "scoutwolf"

    # Phe thứ ba: Đao Phủ (từ 8 người; drama từ 6) · Sát Nhân Hàng Loạt CHỈ khi ván >= SK_MIN_PLAYERS
    neutrals: list[str] = []
    if mode != "classic":
        if n >= (6 if mode == "drama" else 8):
            neutrals.append("executioner")
        if n >= SK_MIN_PLAYERS:
            neutrals.append("serialkiller")
    neutral_n = len(neutrals)

    # Vai đặc biệt: nhóm "lõi" luôn có trước, phần còn lại bốc ngẫu nhiên cho mỗi ván đa dạng
    if n >= ALPHA_MIN_PLAYERS:                # Sói Đầu Đàn thế chỗ 1 sói (không làm phe Sói đông thêm)
        for tgt in ("werewolf", "scoutwolf", "sleeperwolf"):
            if tgt in wolves:
                wolves[wolves.index(tgt)] = "alphawolf"
                break

    if mode == "classic":
        core, extra = ["seer", "guardian", "witch"], ["hunter", "cupid", "elder", "littlegirl"]
    elif mode == "drama":
        core = ["seer", "witch", "hunter"]
        extra = ["cupid", "lostsoul", "gunner", "judge", "mayor", "elder", "littlegirl", "guardian", "mage"]
    else:
        core = ["seer", "witch", "guardian"]
        extra = ["hunter", "mayor", "elder", "mage", "cupid", "gunner", "judge", "littlegirl", "lostsoul"]
    if not word_round:
        extra = [x for x in extra if x != "lostsoul"]
    rng.shuffle(extra)
    if n >= DETECTIVE_MIN_PLAYERS:
        extra.insert(0, "detective")          # đủ người thì Thám Tử luôn có mặt
    specials = core + extra

    rem = n - w - neutral_n
    frac = next((f for cap, f in PLAIN_FRAC_STEPS if n <= cap), PLAIN_FRAC_STEPS[-1][1])
    plain_min = max(1, int(rem * frac + 1e-9))
    k = min(len(specials), max(1, rem - plain_min))
    roles = wolves + neutrals + specials[:k]
    roles += ["villager"] * (n - len(roles))
    rng.shuffle(roles)
    return roles


def roles_summary(keys: list[str]) -> str:
    """'Sói ×2 · Tiên Tri · ...' — dùng công bố thành phần vai của ván."""
    c = Counter(keys)
    order = list(ROLE_CLASSES)
    parts = []
    for k in sorted(c, key=order.index):
        cls = ROLE_CLASSES[k]
        parts.append(f"{cls.emoji} {cls.name}" + (f" ×{c[k]}" if c[k] > 1 else ""))
    return " · ".join(parts)


# ════════════════════════════════════════════════════════════════
# TRẠNG THÁI
# ════════════════════════════════════════════════════════════════

@dataclass
class Player:
    idx: int
    user_id: int
    name: str
    role: Role
    alive: bool = True
    lover: Optional[int] = None
    death_cause: str = ""
    death_stamp: str = ""
    word: Optional[str] = None
    letters_sent: int = 0
    desc_count: int = 0
    vote_bonus: bool = False        # được Thị Trưởng chỉ định kế nhiệm: phiếu x2


@dataclass
class NightState:
    no: int
    wolf_votes: dict = field(default_factory=dict)      # wolf_idx -> target | NONE
    curse_armed: bool = False
    guard: Optional[int] = None
    serial: Optional[int] = None
    done: dict = field(default_factory=dict)            # (actor, kind) -> list[int]
    partial: dict = field(default_factory=dict)         # (actor, kind) -> list[int] đang chọn dở
    victim: Optional[int] = None
    witch_heal: bool = False
    witch_poison: Optional[int] = None
    frame: Optional[int] = None                         # Sát Nhân đổ tội cho ai
    revive: Optional[tuple] = None                      # (pháp sư, người được hồi sinh)
    elder_link: Optional[int] = None                    # Già Làng thì thầm với ai đêm nay
    purge: Optional[int] = None                         # Sói Đầu Đàn thanh trừng ai
    pack_revive: Optional[int] = None                   # Sói Đầu Đàn hồi sinh sói nào


@dataclass
class VoteState:
    round: int
    candidates: list
    ballots: dict = field(default_factory=dict)         # voter_idx -> target_idx | None(bỏ qua)


@dataclass
class Desc:
    idx: int
    text: str
    order: int
    secs: float


@dataclass
class PickResult:
    ok: bool
    msg: str = ""
    committed: bool = False
    selected: list = field(default_factory=list)


@dataclass
class Outcome:
    kind: str                  # village | wolf | lovers | serial | draw
    title: str
    winners: list


class WerewolfGame:
    def __init__(self, entries: list[tuple[int, str]], settings: Optional[WWSettings] = None,
                 rng: Optional[random.Random] = None):
        if not (MIN_PLAYERS <= len(entries) <= MAX_PLAYERS):
            raise ValueError(f"Ma Sói cần {MIN_PLAYERS}-{MAX_PLAYERS} người chơi")
        self.rng = rng or random.Random()
        self.settings = settings or WWSettings()
        self.players: list[Player] = [Player(i, uid, name, Villager()) for i, (uid, name) in enumerate(entries)]
        self.night_no = 0
        self.day_no = 0
        self.phase = "setup"
        self.night: Optional[NightState] = None
        self.vote: Optional[VoteState] = None
        self.over = False
        self.pending_successors: list[int] = []   # Thị Trưởng đã chết cần chọn người kế nhiệm
        self.last_cases: dict = {}                # nạn nhân bị giết đêm trước -> hồ sơ vụ án (cho Thám Tử)
        self.revived: list[int] = []              # những người được Pháp Sư hồi sinh sáng nay
        self.knighted = False                     # đã phong Hiệp Sĩ chưa
        self.plain_peak = 0                       # số Dân Làng thường nhiều nhất từng đồng thời còn sống
        self.fallen: list[int] = []               # Sói Đầu Đàn bị treo vì mất đàn ở lần begin_day gần nhất
        self.ls_announced = False                 # đã báo 'Đường Cùng' cho Sói chưa
        self.ls_scouted: set = set()
        self.outcome: Optional[Outcome] = None
        self.personal_wins: list[int] = []
        self.pending_shots: list[int] = []
        self.judge_vetoed = False
        self.out_public: list[str] = []
        self.out_dm: list[tuple[int, str]] = []
        self.death_log: list[tuple[int, str, str]] = []
        self.role_keys: list[str] = []
        # vòng mô tả
        self.used_words: set = set()
        self.word_pair: Optional[tuple] = None
        self.word_hist: list[tuple] = []          # (ngày, từ A, từ B, nhãn)
        self.descs: list[Desc] = []
        self.desc_all: list[tuple] = []           # (ngày, idx, text)
        self.round_start = 0.0
        # thư nặc danh
        self.letters: list[tuple] = []            # (ngày, idx, text)
        self.vote_history: list[tuple] = []       # (ngày, {target: [voters]})

    # ── Sói Đầu Đàn: bất khả xâm phạm khi đàn còn sói ───────────
    def alpha_shielded(self, idx: int) -> bool:
        p = self.players[idx]
        return p.alive and p.role.key == "alphawolf" and any(w.idx != idx for w in self.wolves_alive())

    def attackable_idx(self) -> list[int]:
        """Người còn sống có thể bị giết (loại Sói Đầu Đàn đang được đàn che chắn)."""
        return [i for i in self.alive_idx() if not self.alpha_shielded(i)]

    def lynchable_idx(self) -> list[int]:
        """Người có thể bị bỏ phiếu treo: loại Đầu Đàn được che chắn và Hiệp Sĩ (miễn nhiễm)."""
        return [i for i in self.attackable_idx() if self.players[i].role.key != "knight"]

    def voter_idx(self) -> list[int]:
        """Người được bỏ phiếu: mọi người sống trừ Sói Đầu Đàn."""
        return [i for i in self.alive_idx() if self.players[i].role.key != "alphawolf"]

    # ── tiện ích ────────────────────────────────────────────────
    def say(self, text: str) -> None:
        self.out_public.append(text)

    def tell(self, idx: int, text: str) -> None:
        self.out_dm.append((idx, text))

    def pop_out(self) -> tuple[list, list]:
        pub, dm = self.out_public, self.out_dm
        self.out_public, self.out_dm = [], []
        return pub, dm

    def nm(self, idx: int) -> str:
        return self.players[idx].name

    def alive_idx(self) -> list[int]:
        return [p.idx for p in self.players if p.alive]

    def alive_players(self) -> list[Player]:
        return [p for p in self.players if p.alive]

    def wolves_alive(self) -> list[Player]:
        return [p for p in self.players if p.alive and p.role.is_wolf]

    def pack_wolves(self) -> list[Player]:
        """Sói TRONG ĐÀN còn sống: mọi sói trừ Sói Đầu Đàn (Đầu Đàn không tính là 1 đơn vị của đàn)."""
        return [p for p in self.wolves_alive() if p.role.key != "alphawolf"]

    @staticmethod
    def _purgeable(p: Player) -> bool:
        """Sói Đầu Đàn thanh trừng được: sói trong đàn + Sát Nhân đã bị nguyền vào bầy (không phải chính Đầu Đàn)."""
        r = p.role
        return p.alive and ((r.is_wolf and r.key != "alphawolf") or bool(getattr(r, "converted", False)))

    def by_key(self, key: str, alive_only: bool = True) -> list[Player]:
        return [p for p in self.players if p.role.key == key and (p.alive or not alive_only)]

    def find_user(self, user_id: int) -> Optional[Player]:
        for p in self.players:
            if p.user_id == user_id:
                return p
        return None

    def stamp(self) -> str:
        return f"Đêm {self.night_no}" if self.phase.startswith("night") else f"Ngày {self.day_no}"

    def can_use_power(self, p: Player) -> bool:
        return p.alive

    # ── chia vai ────────────────────────────────────────────────
    def assign_roles(self) -> None:
        keys = build_roles(len(self.players), self.settings.mode, self.settings.word_round, self.rng)
        self.role_keys = list(keys)
        for p, k in zip(self.players, keys):
            p.role = make_role(k)
        self.plain_peak = sum(1 for p in self.players if p.role.key == "villager")
        for p in self.by_key("executioner"):
            pool = [q.idx for q in self.players if q.idx != p.idx and q.role.team == Team.VILLAGE]
            pool = pool or [q.idx for q in self.players if q.idx != p.idx]
            p.role.target = self.rng.choice(pool)
        self.phase = "ready"

    def composition(self) -> str:
        return roles_summary(self.role_keys)

    def role_card(self, idx: int) -> str:
        p = self.players[idx]
        r = p.role
        lines = [f"{r.emoji} *BẠN LÀ {r.name.upper()}*", f"_{TEAM_LABEL[r.team]}_", "", r.detail, "",
                 f"🎯 *Mục tiêu:* {r.win_text}"]
        if r.is_wolf:
            mates = [q.name + (" 👑Đầu Đàn" if q.role.key == "alphawolf" else "")
                     for q in self.players if q.role.is_wolf and q.idx != idx]
            lines.append("")
            lines.append("🐺 *Đồng bọn:* " + (", ".join(mates) if mates else "không có — bạn đơn độc!"))
            lines.append("🌙 _Ban đêm ĐỪNG chat ở nhóm (tin sẽ bị xoá) — hãy nhắn cho bot ở DM này, bot sẽ chuyển kín cho cả bầy._")
            lines.append("")
            lines.append(LAST_STAND_TEXT)
        if r.key == "serialkiller":
            lines.append("")
            lines.append(self.sk_intel(idx))
            lines.append("")
            lines.append("🗣️ _Từ khoá Vòng Mô Tả của bạn là từ của DÂN LÀNG._")
        if isinstance(r, Executioner) and r.target is not None:
            lines.append("")
            lines.append(f"🪓 *Mục tiêu của bạn:* {self.nm(r.target)}")
        if isinstance(r, Elder):
            ms = [q for q in self.players if q.role.key in ("mayor", "witch")]
            lines.append("")
            lines.append("🔍 *Bạn biết:* " + (", ".join(f"{q.role.emoji} {q.role.name} = {q.name}" for q in ms)
                                              if ms else "_ván này không có Thị Trưởng / Phù Thuỷ_"))
        if p.lover is not None:
            lines.append("")
            lines.append(f"💞 *Người yêu của bạn:* {self.nm(p.lover)} — ai chết trước, người kia chết theo!")
            lines.append("🌙 _Ban đêm muốn thì thầm với người yêu: nhắn cho bot ở DM này._")
        return "\n".join(lines)

    # ════════════════════════════════════════════════════════════
    # ĐÊM
    # ════════════════════════════════════════════════════════════
    def begin_night(self) -> None:
        self.night_no += 1
        self.phase = "night_a"
        self.night = NightState(no=self.night_no)
        self._last_stand_night()
        self._sk_night_brief()

    def _sk_night_brief(self) -> None:
        """Đầu đêm: báo Sát Nhân thân phận mọi người (cập nhật theo vai hiện tại) + việc cần làm đêm nay."""
        for sk in self.by_key("serialkiller"):
            self.tell(sk.idx, self.sk_intel(sk.idx))
            r = sk.role
            if self.sk_rush_target(sk) is not None:
                continue
            if r.plan is None:
                self.tell(sk.idx, "📝 *Đêm nay bạn LẬP KẾ HOẠCH:* chọn mục tiêu — đêm mai bạn mới ra tay.")
            else:
                self.tell(sk.idx, f"🔪 *Đêm nay bạn THỰC HIỆN kế hoạch* giết *{self.nm(r.plan)}* — "
                                  "có thể chọn 1 người để làm giả chứng cứ đổ tội.")

    def sk_intel(self, idx: int) -> str:
        """Bảng thân phận của mọi người còn sống, dành riêng cho Sát Nhân."""
        rows = {Team.WOLF: [], Team.VILLAGE: [], Team.NEUTRAL: []}
        for q in self.alive_players():
            if q.idx == idx:
                continue
            extra = " (đã bị nguyền vào bầy)" if getattr(q.role, "converted", False) else ""
            rows[q.role.team].append(f"{q.role.emoji} {q.name} — {q.role.name}{extra}")
        lines = ["🧠 *THÂN PHẬN MỌI NGƯỜI* (chỉ mình bạn biết):"]
        for t in (Team.WOLF, Team.VILLAGE, Team.NEUTRAL):
            lines.append(f"\n{TEAM_LABEL[t]}")
            lines += ["• " + x for x in rows[t]] or ["• _không còn ai_"]
        return "\n".join(lines)

    def sk_rush_target(self, sk: Player) -> Optional[int]:
        """Sát Nhân giết thẳng (không cần kế hoạch) khi chỉ còn 1 người sống sót cùng hắn.
        Người yêu của Sát Nhân không tính: SK + người yêu + 1 người khác => giết người khác ngay."""
        if not sk.alive or self.over:
            return None
        others = [q.idx for q in self.alive_players() if q.idx != sk.idx]
        if len(others) == 1:
            return others[0]
        if len(others) == 2 and sk.lover in others:
            return next(i for i in others if i != sk.lover)
        return None

    def _sk_rush(self) -> list[int]:
        """Kiểm tra sau mỗi cái chết: Sát Nhân ra tay ngay (ngày hay đêm) nếu chỉ còn 1 đối thủ."""
        if getattr(self, "_rushing", False) or self.over:
            return []
        self._rushing = True
        died: list[int] = []
        try:
            while True:
                acted = False
                for sk in self.by_key("serialkiller"):
                    t = self.sk_rush_target(sk)
                    if t is None:
                        continue
                    sk.role.plan = None
                    self.say(f"🔪 *{sk.name}* không cần giấu mình nữa — giữa lúc chỉ còn hai người, "
                             "Sát Nhân Hàng Loạt ra tay NGAY!")
                    self.tell(sk.idx, f"🔪 Chỉ còn *{self.nm(t)}* — bạn ra tay ngay, không cần kế hoạch!")
                    died += self._die(t, "serial")
                    acted = True
                if not acted:
                    break
        finally:
            self._rushing = False
        return died

    # ── ĐƯỜNG CÙNG của Sói ─────────────────────────────────
    def ls_others(self) -> int:
        """Số người 'khác' quanh con sói cuối cùng (không tính chính nó và Sói Đầu Đàn)."""
        return len(self.alive_idx()) - 1 - len([w for w in self.wolves_alive() if w.role.key == "alphawolf"])

    def last_stand_wolf(self) -> Optional[Player]:
        """Con sói DUY NHẤT còn lại TRONG ĐÀN (Sói Đầu Đàn không tính) khi làng còn đông — được buff.
        Có Sói Đầu Đàn: chỉ buff khi Đầu Đàn đã dùng hết lượt hồi sinh (hết đường cứu đàn)."""
        pack = self.pack_wolves()
        if len(pack) != 1:
            return None
        alphas = [w for w in self.wolves_alive() if w.role.key == "alphawolf"]
        if alphas and LS_ALPHA_NEEDS_REVIVE_USED and any(a.role.revive_left > 0 for a in alphas):
            return None
        if self.ls_others() < LAST_STAND_MIN_OTHERS:
            return None
        return pack[0]

    @staticmethod
    def _power_label(t: Player) -> str:
        return "có CHỨC NĂNG ĐẶC BIỆT ⚡" if t.role.has_power or t.role.key == "hunter" else "chỉ là người thường 🙂"

    def _last_stand_night(self) -> None:
        w = self.last_stand_wolf()
        if w is None:
            return
        if not self.ls_announced:
            self.ls_announced = True
            self.tell(w.idx, "🩸 *ĐƯỜNG CÙNG!* Bạn là con sói cuối cùng mà làng vẫn còn đông — máu điên bùng lên:\n"
                             "① Nanh của bạn *xuyên qua lá chắn Bảo Vệ* (Phù Thuỷ vẫn cứu được).\n"
                             "② Từ giờ bạn nhận *từ khoá của DÂN LÀNG* ở Vòng Mô Tả — hãy ngụy trang thật khéo.\n"
                             "③ Mỗi đêm khứu giác săn mồi tự dò 1 người xem có chức năng đặc biệt không.")
        pool = [q.idx for q in self.alive_players() if not q.role.is_wolf and q.idx not in self.ls_scouted]
        if pool:
            t = self.players[self.rng.choice(pool)]
            self.ls_scouted.add(t.idx)
            self.tell(w.idx, f"👃 Khứu giác săn mồi: *{t.name}* {self._power_label(t)}")

    def kinds_tonight(self, p: Player) -> list[str]:
        if not p.alive or self.night is None:
            return []
        out = []
        for k in p.role.night_kinds:
            if k == "cupid" and self.night_no != 1:
                continue
            if k == "curse" and not getattr(p.role, "curse_left", False):
                continue
            if k == "revive" and (p.role.used or not self.targets_for(p.idx, "revive")):
                continue
            if k in ("investigate", "elderlink") and not self.targets_for(p.idx, k):
                continue
            if k == "serial" and (p.role.plan is not None or self.sk_rush_target(p) is not None):
                continue                      # đã có kế hoạch (đang chờ ra tay) hoặc sắp giết thẳng
            if k == "frame" and (p.role.plan is None or self.sk_rush_target(p) is not None):
                continue                      # chỉ dựng chứng cứ vào đêm ra tay
            if k == "purge" and (p.role.purge_left <= 0 or not self.targets_for(p.idx, k)):
                continue
            if k == "packrevive" and (p.role.revive_left <= 0 or not self.targets_for(p.idx, k)):
                continue
            out.append(k)
        if getattr(p.role, "converted", False):      # Sát Nhân đã bị nguyền vào bầy: vote cắn như các sói khác
            out.insert(0, "wolf")
        return out

    # ── chat bí mật ban đêm (qua DM bot) ────────────────────────
    def pack_alive(self) -> list[Player]:
        """Thành viên bầy sói còn sống: sói thật + Sát Nhân bị nguyền (đã 'nhập bầy')."""
        return [p for p in self.players if p.alive and (p.role.is_wolf or getattr(p.role, "converted", False))]

    def wolf_voters(self) -> list[Player]:
        """Những người được vote cắn ban đêm: mọi sói còn sống + Sát Nhân đã bị nguyền vào bầy."""
        return [p for p in self.pack_alive()]

    def eavesdroppers(self) -> list[int]:
        """Sát Nhân (chưa bị nguyền) nghe lén chat đêm của bầy sói."""
        return [p.idx for p in self.players if p.alive and p.role.key == "serialkiller" and not p.role.converted]

    def night_channels(self, idx: int) -> dict[str, list[int]]:
        """Các kênh chat đêm mà người chơi `idx` đang dùng được -> {kênh: [idx người nhận]}.
        'wolf'  : cả bầy sói còn sống (trừ mình) — gồm Sát Nhân đã bị nguyền.
        'spy'   : Sát Nhân mạo danh sói (nghe lén được xử lý ở handler).
        'elder' : Già Làng <-> Thị Trưởng/Phù Thuỷ mà Già Làng chọn đêm nay.
        'lover' : người yêu còn sống.
        Rỗng = không có kênh chat đêm. Người nhận có thể rỗng (VD sói đơn độc)."""
        p = self.players[idx]
        out: dict[str, list[int]] = {}
        if not p.alive:
            return out
        r = p.role
        if r.night_chat == "wolf" or getattr(r, "converted", False):
            out["wolf"] = [q.idx for q in self.pack_alive() if q.idx != idx]
        elif r.key == "serialkiller" and self.wolves_alive():
            out["spy"] = [w.idx for w in self.wolves_alive()]
        n = self.night
        if n is not None and n.elder_link is not None:
            el = self.by_key("elder")
            if el and idx == el[0].idx:
                out["elder"] = [n.elder_link] if self.players[n.elder_link].alive else []
            elif el and idx == n.elder_link:
                out["elder"] = [el[0].idx]
        if p.lover is not None:
            out["lover"] = [p.lover] if self.players[p.lover].alive else []
        return out

    def spy_alias_lookup(self, token: str) -> Optional[int]:
        """Tìm con sói (còn sống) theo tên gõ vào: khớp đúng → khớp đầu tên → khớp một phần (chỉ nhận nếu duy nhất)."""
        t = token.strip().lstrip("@").lower()
        if not t:
            return None
        ws = self.wolves_alive()
        for test in (lambda n: n == t, lambda n: n.startswith(t), lambda n: t in n):
            hit = [w.idx for w in ws if test(w.name.lower())]
            if len(hit) == 1:
                return hit[0]
        return None

    def set_spy_alias(self, idx: int, wolf_idx: Optional[int]) -> tuple[bool, str]:
        p = self.players[idx]
        r = p.role
        if r.key != "serialkiller" or not p.alive or r.converted:
            return False, "Bạn không thể mạo danh sói."
        if wolf_idx is None or wolf_idx == NONE:
            r.spy_as = None
            return True, "🎲 Bạn sẽ đội tên sói NGẪU NHIÊN mỗi lần nói."
        w = self.players[wolf_idx]
        if not (w.alive and w.role.is_wolf):
            return False, "Con sói đó không còn trong bầy."
        r.spy_as = wolf_idx
        return True, f"🎭 Từ giờ tin của bạn sẽ hiện dưới tên *{w.name}*."

    def spy_speak(self, idx: int, alias: Optional[int] = None) -> tuple[bool, str, bool]:
        """Sát Nhân nói vào kênh sói. -> (được nói?, tên hiển thị / lý do từ chối, bị phát giác?)
        Bạn TỰ CHỌN đội tên con sói nào (alias / spy_as); không chọn thì ngẫu nhiên.
        Mỗi tin có xác suất bị phát giác, tăng theo số tin đã nói. Bị phát giác: cả bầy biết hắn là Sát Nhân."""
        p = self.players[idx]
        r = p.role
        wolves = self.wolves_alive()
        if r.key != "serialkiller" or not p.alive or not wolves:
            return False, "Không có ai để nghe bạn.", False
        if r.exposed:
            return False, "Bạn đã bị bầy sói phát giác — chúng không còn tin lời bạn nữa (bạn vẫn nghe lén được).", False
        if alias is not None:
            r.spy_as = alias
        r.talk_count += 1
        chance = min(SPY_MAX, SPY_BASE + SPY_STEP * r.talk_count)
        if self.rng.random() < chance:
            r.exposed = True
            for w in wolves:
                self.tell(w.idx, f"🚨 *Có kẻ mạo danh sói trong kênh chat!* Hắn là *{p.name}* — SÁT NHÂN HÀNG LOẠT đang nghe lén bầy!")
            self.tell(idx, "🚨 *Bạn bị phát giác!* Bầy sói đã biết bạn là Sát Nhân — coi chừng những cú cắn!")
            return True, p.name, True
        chosen = self.players[r.spy_as] if r.spy_as is not None else None
        if chosen is None or not (chosen.alive and chosen.role.is_wolf):
            chosen = self.rng.choice(wolves)
        return True, chosen.name, False

    # ── Già Làng báo mộng ───────────────────────────────────
    def dream_ready(self, idx: int) -> bool:
        """Già Làng đã chết, đang là ĐÊM HÔM SAU cái chết và còn lượt báo mộng."""
        p = self.players[idx]
        r = p.role
        return (not p.alive and r.key == "elder" and r.death_night is not None
                and self.night_no == r.death_night + 1 and r.dream_left > 0 and self.phase == "night_a")

    def use_dream(self, idx: int) -> int:
        r = self.players[idx].role
        r.dream_left -= 1
        return r.dream_left

    def public_elder(self) -> Optional[Player]:
        for p in self.players:
            if p.role.public:
                return p
        return None

    def can_night_chat(self, idx: int) -> bool:
        return bool(self.night_channels(idx))

    def night_actors(self) -> list[tuple[int, str]]:
        res = []
        for p in self.players:
            for k in self.kinds_tonight(p):
                res.append((p.idx, k))
        return res

    def targets_for(self, actor: int, kind: str) -> list[int]:
        p = self.players[actor]
        alive = self.alive_idx()
        if kind in ("wolf", "scout"):
            return [i for i in alive if not self.players[i].role.is_wolf
                    and not getattr(self.players[i].role, "converted", False)]
        if kind == "guard":
            last = getattr(p.role, "last_guard", None)
            return [i for i in alive if i != last]
        if kind == "serial":
            return [i for i in self.attackable_idx() if i != actor]
        if kind in ("seer", "frame"):
            return [i for i in alive if i != actor]
        if kind == "purge":
            return [q.idx for q in self.alive_players() if q.idx != actor and self._purgeable(q)]
        if kind == "packrevive":
            return [q.idx for q in self.players if not q.alive and q.role.is_wolf and q.role.key != "alphawolf"]
        if kind == "investigate":
            return list(self.last_cases)
        if kind == "revive":
            return [q.idx for q in self.players if not q.alive and q.idx != actor]
        if kind == "elderlink":
            return [q.idx for q in self.players if q.alive and q.role.key in ("mayor", "witch")]
        if kind == "cupid":   # Sói Đầu Đàn công khai nên không bị ghép đôi
            return [i for i in alive if self.players[i].role.key != "alphawolf"]
        return alive

    def night_pick(self, actor: int, kind: str, value: int) -> PickResult:
        n = self.night
        if self.phase != "night_a" or n is None:
            return PickResult(False, "Không phải lúc hành động.")
        p = self.players[actor]
        if kind not in self.kinds_tonight(p):
            return PickResult(False, "Bạn không thể làm điều này đêm nay.")
        info = KIND_INFO[kind]
        key = (actor, kind)
        if info["locked"] and key in n.done:
            return PickResult(False, "Bạn đã chọn rồi — không đổi được.")

        if info["need"] == 0:                       # có / không
            v = 1 if value else 0
            n.done[key] = [v]
            self._commit(actor, kind, [v])
            return PickResult(True, "Đã ghi nhận.", True, [v])

        if value == NONE:                            # bỏ qua
            n.done[key] = []
            n.partial.pop(key, None)
            self._commit(actor, kind, [])
            return PickResult(True, "Bạn chọn: bỏ qua.", True, [])

        if value not in self.targets_for(actor, kind):
            return PickResult(False, "Mục tiêu không hợp lệ.")

        if info["need"] == 2:                        # chọn 2 (toggle)
            cur = n.partial.setdefault(key, [])
            if value in cur:
                cur.remove(value)
                return PickResult(True, "Đã bỏ chọn.", False, list(cur))
            cur.append(value)
            if len(cur) < 2:
                return PickResult(True, f"Đã chọn {self.nm(value)} — chọn thêm 1 người nữa.", False, list(cur))
            pair = list(cur)
            n.done[key] = pair
            n.partial.pop(key, None)
            self._commit(actor, kind, pair)
            return PickResult(True, "Đã chốt.", True, pair)

        n.done[key] = [value]
        self._commit(actor, kind, [value])
        return PickResult(True, f"Bạn chọn: {self.nm(value)}", True, [value])

    def _commit(self, actor: int, kind: str, sel: list) -> None:
        n = self.night
        p = self.players[actor]
        me = p.name
        if kind == "wolf":
            t = sel[0] if sel else NONE
            n.wolf_votes[actor] = t
            txt = f"🐺 *{me}* chọn cắn *{self.nm(t)}*" if t != NONE else f"🐺 *{me}* muốn tha đêm nay."
            for w in self.pack_alive():
                if w.idx != actor:
                    self.tell(w.idx, txt)
        elif kind == "curse":
            n.curse_armed = bool(sel and sel[0])
            if n.curse_armed:
                for w in self.wolves_alive():
                    if w.idx != actor:
                        self.tell(w.idx, f"🧬 *{me}* sẽ dùng LỜI NGUYỀN lên nạn nhân đêm nay!")
        elif kind == "scout":
            if sel:
                t = self.players[sel[0]]
                res = self._power_label(t)
                for w in self.wolves_alive():
                    self.tell(w.idx, f"🔭 Trinh sát: *{t.name}* {res}")
        elif kind == "guard":
            n.guard = sel[0] if sel else None
        elif kind == "seer":
            if sel:
                t = self.players[sel[0]]
                lsw = self.last_stand_wolf()
                hidden = LS_BLIND_SEER and lsw is not None and lsw.idx == t.idx
                res = "🐺 là *MA SÓI*!" if (t.role.seer_reads_wolf and not hidden) else "🌿 *KHÔNG* phải Ma Sói."
                self.tell(actor, f"🔮 Tiên Tri soi *{t.name}*: {res}")
        elif kind == "investigate":
            if sel:
                self._investigate(actor, sel[0])
            else:
                self.tell(actor, "🕵️ Bạn quyết định không điều tra đêm nay.")
        elif kind == "cupid":
            if len(sel) == 2:
                a, b = sel
                self.players[a].lover, self.players[b].lover = b, a
                for x, y in ((a, b), (b, a)):
                    self.tell(x, f"💞 Mũi tên của Thần Tình Yêu đã bắn trúng bạn! Người yêu của bạn là *{self.nm(y)}*. "
                                 f"Ai chết trước, người kia chết theo — hãy bảo vệ nhau (hoặc phản bội nhau 😈). "
                                 f"Ban đêm hai người có thể thì thầm kín bằng cách nhắn cho bot ở DM.")
                self.tell(actor, f"💘 Bạn đã ghép đôi *{self.nm(a)}* và *{self.nm(b)}*.")
        elif kind == "serial":
            n.serial = sel[0] if sel else None
        elif kind == "frame":
            n.frame = sel[0] if sel else None
            if sel:
                self.tell(actor, f"🎭 Bạn sẽ đổ tội cho *{self.nm(sel[0])}*: nếu Thám Tử điều tra vụ án đêm nay, manh mối sẽ chỉ về hắn.")
        elif kind == "purge":
            n.purge = sel[0] if sel else None
            if sel:
                self.tell(actor, f"🗡️ Bạn quyết định THANH TRỪNG *{self.nm(sel[0])}* — hắn sẽ chết trước khi trời sáng.")
        elif kind == "packrevive":
            n.pack_revive = sel[0] if sel else None
            if sel:
                self.tell(actor, f"✨ Bạn hú lên gọi *{self.nm(sel[0])}* trở về từ cõi chết — hắn sẽ sống lại lúc trời sáng.")
        elif kind == "revive":
            n.revive = (actor, sel[0]) if sel else None
            if sel:
                self.tell(actor, f"🧿 Bạn niệm chú hồi sinh *{self.nm(sel[0])}* — họ sẽ trở lại lúc trời sáng.")
        elif kind == "elderlink":
            if sel:
                n.elder_link = sel[0]
                t = self.players[sel[0]]
                self.tell(actor, f"👴 Bạn đã nối giấc mơ với *{t.name}* ({t.role.emoji} {t.role.name}). "
                                 f"Gõ tin nhắn ở đây — bot chuyển kín cho họ (mỗi đêm chỉ nói với 1 người).")
                self.tell(sel[0], "👴 *Già Làng* muốn thì thầm với bạn đêm nay! Nhắn tin ở DM này để trả lời — bot chuyển kín, không ai khác nghe.")
        elif kind == "peek":
            if sel and sel[0]:
                roll = self.rng.random()
                wolves = [w for w in self.wolves_alive()]
                if roll < 0.30 and wolves:
                    w = self.rng.choice(wolves)
                    self.tell(actor, f"👀 Trong bóng tối bạn thấy dáng của *{w.name}* lảng vảng cùng bầy sói…")
                elif roll < 0.50:
                    self.tell(actor, "👀 Bạn hé mắt… và một cặp mắt đỏ quay phắt lại nhìn bạn! Bạn vội nhắm chặt mắt.")
                    for w in wolves:
                        self.tell(w.idx, f"🚨 *{p.name}* vừa bị bắt gặp NHÌN TRỘM bầy sói! Cô bé Tò Mò chính là {p.name}.")
                else:
                    self.tell(actor, "👀 Bạn hé mắt nhưng chỉ thấy một màn đêm đen kịt.")
            else:
                self.tell(actor, "😴 Bạn ngủ ngoan tới sáng.")

    def stage_a_done(self) -> bool:
        n = self.night
        if n is None:
            return True
        for actor, kind in self.night_actors():
            if KIND_INFO[kind]["required"] and (actor, kind) not in n.done:
                return False
        wolves = [w.idx for w in self.wolf_voters()]
        if wolves:
            votes = {n.wolf_votes.get(w) for w in wolves}
            if None in votes or len(votes) > 1:      # chưa đủ phiếu hoặc bầy chưa thống nhất
                return False
        return True

    def pending_actors(self) -> list[tuple[int, str]]:
        n = self.night
        return [(a, k) for a, k in self.night_actors()
                if KIND_INFO[k]["required"] and (a, k) not in n.done]

    def close_stage_a(self) -> Optional[int]:
        """Chốt nạn nhân của bầy sói (đa số; hoà thì ngẫu nhiên trong nhóm hoà)."""
        n = self.night
        votes = [t for w, t in n.wolf_votes.items() if self.players[w].alive and t != NONE]
        if not votes:
            n.victim = None
            return None
        c = Counter(votes)
        top = max(c.values())
        n.victim = self.rng.choice([t for t, v in c.items() if v == top])
        return n.victim

    # ── Phù Thuỷ (giai đoạn B) ─────────────────────────────────
    def witch_active(self) -> Optional[Player]:
        for p in self.by_key("witch"):
            if self.can_use_power(p) and (p.role.heal or p.role.poison):
                return p
        return None

    def witch_set(self, actor: int, action: str, value: int = NONE) -> PickResult:
        n = self.night
        p = self.players[actor]
        if p.role.key != "witch" or not self.can_use_power(p) or n is None or self.phase != "night_b":
            return PickResult(False, "Không hợp lệ.")
        if action == "heal":
            if not p.role.heal or n.victim is None:
                return PickResult(False, "Không có gì để cứu.")
            n.witch_heal = not n.witch_heal
            return PickResult(True, "Đã chọn CỨU." if n.witch_heal else "Đã bỏ cứu.", False, [])
        if action == "poison":
            if not p.role.poison:
                return PickResult(False, "Bạn hết bình độc.")
            if value == NONE:
                n.witch_poison = None
                return PickResult(True, "Đã bỏ bình độc.")
            if value not in self.attackable_idx() or value == actor:
                return PickResult(False, "Mục tiêu không hợp lệ.")
            n.witch_poison = value
            return PickResult(True, f"Sẽ đầu độc {self.nm(value)}.")
        return PickResult(False, "Không hợp lệ.")

    # ── Thám Tử điều tra vụ án ──────────────────────────────
    def _investigate(self, actor: int, victim: int) -> None:
        """Thám Tử điều tra 1 nạn nhân bị giết đêm trước: có xác suất ra manh mối về hung thủ.
        Nếu Sát Nhân đã đổ tội, manh mối (nếu có) chỉ về người vô tội — trừ khi Thám Tử nhận ra chứng cứ giả."""
        case = self.last_cases.get(victim)
        vn = self.nm(victim)
        if not case:
            self.tell(actor, f"🕵️ Không tìm thấy dấu vết gì ở hiện trường của *{vn}*.")
            return
        if self.rng.random() >= DETECTIVE_CHANCE:
            self.tell(actor, f"🕵️ Bạn lục tung hiện trường của *{vn}* nhưng không tìm ra manh mối nào đáng tin.")
            return
        if case["kind"] == "wolf":
            culprit = self.rng.choice(case["culprits"]) if case["culprits"] else None
        else:
            framed = case.get("framed")
            if framed is not None:
                if self.rng.random() < FORGERY_FLAW:
                    self.tell(actor, f"🕵️ Hiện trường của *{vn}* có dấu hiệu bị *DÀN DỰNG* — ai đó đã cố đổ tội cho người khác! "
                                     "Đừng tin vào bằng chứng có sẵn.")
                    return
                culprit = framed
            else:
                culprit = case["culprit"]
        if culprit is None:
            self.tell(actor, f"🕵️ Hiện trường của *{vn}* quá sạch sẽ — không có manh mối.")
            return
        self.tell(actor, f"🕵️ *Manh mối:* mọi dấu vết ở hiện trường của *{vn}* đều chỉ về *{self.nm(culprit)}*! "
                         f"(Có thể là hung thủ thật… hoặc bị Sát Nhân đổ tội.)")

    # ── Sói ngửi mùi máu ────────────────────────────────────
    def _blood_scent(self, sk: Player) -> None:
        r = sk.role
        wolves = self.wolves_alive()
        if not wolves or r.exposed or r.converted or self.rng.random() >= BLOOD_SCENT:
            return
        if r.decoy is None or not self.players[r.decoy].alive or self.players[r.decoy].role.is_wolf or r.decoy == sk.idx:
            pool = [q.idx for q in self.alive_players() if not q.role.is_wolf and q.idx != sk.idx]
            r.decoy = self.rng.choice(pool) if pool else None
        if r.decoy is None:
            txt = f"🩸 Bầy sói ngửi thấy MÙI MÁU của kẻ ngoài bầy đang săn người… đó là *{sk.name}*!"
        else:
            pair = [sk.idx, r.decoy]
            self.rng.shuffle(pair)
            txt = (f"🩸 Bầy sói ngửi thấy MÙI MÁU của một kẻ ngoài bầy đang săn người… "
                   f"hắn là *{self.nm(pair[0])}* hoặc *{self.nm(pair[1])}*!")
        for w in wolves:
            self.tell(w.idx, txt)

    # ── Pháp Sư hồi sinh ────────────────────────────────────
    def _revive(self, idx: int) -> None:
        p = self.players[idx]
        p.alive = True
        p.death_cause, p.death_stamp = "", ""
        self.revived.append(idx)
        self.tell(idx, "✨ *Bạn được Pháp Sư gọi từ cõi chết trở về!* Bạn sống lại với nguyên vai trò cũ.")
        if p.lover is not None and not self.players[p.lover].alive:
            self.players[p.lover].lover = None       # người yêu đã chết theo — mối duyên đứt
            p.lover = None

    # ── Kết toán đêm ────────────────────────────────────────
    def resolve_night(self) -> list[int]:
        n = self.night
        prot = n.guard
        kills: list[tuple[int, str]] = []
        cases: dict = {}
        self.revived = []

        # 0. Hồi sinh (chỉ người đã chết TRƯỚC đêm nay): Pháp Sư & Sói Đầu Đàn
        if n.revive:
            actor, t = n.revive
            mg = self.players[actor]
            if mg.alive and mg.role.key == "mage" and not mg.role.used and not self.players[t].alive:
                mg.role.used = True
                self._revive(t)
        if n.pack_revive is not None:
            t = n.pack_revive
            for a in self.by_key("alphawolf"):
                tp = self.players[t]
                if a.role.revive_left > 0 and not tp.alive and tp.role.is_wolf and tp.role.key != "alphawolf":
                    a.role.revive_left -= 1
                    self._revive(t)
                    for w in self.wolves_alive():
                        if w.idx != t:
                            self.tell(w.idx, f"👑✨ Sói Đầu Đàn đã hồi sinh *{tp.name}* — đàn sói đông đủ trở lại!")
                    break

        wolves = self.pack_alive()          # cả bầy (gồm Sát Nhân đã bị nguyền) — để báo tin
        witch = self.witch_active()
        pierce = self.last_stand_wolf() is not None        # Đường Cùng: nanh xuyên Bảo Vệ

        def heal_note() -> None:
            if n.witch_heal and witch:
                self.tell(witch.idx, "💊 Nạn nhân đã an toàn sẵn nên bình cứu CHƯA bị tiêu hao.")

        # 1. Bầy sói cắn
        v = n.victim
        if v is not None and self.players[v].alive:
            vp = self.players[v]
            voters = [w for w, t in n.wolf_votes.items() if t == v and self.players[w].alive]
            curse_wolf = next((w for w in wolves if getattr(w.role, "curse_left", False)), None)
            curse_try = n.curse_armed and curse_wolf is not None
            if prot == v and pierce:
                for g in self.by_key("guardian"):
                    self.tell(g.idx, "🛡️💥 Cú cắn của con sói cuối cùng quá hung tợn — lá chắn của bạn bị xuyên thủng!")
            if prot == v and not pierce:
                for w in wolves:
                    self.tell(w.idx, f"🛡️ Cú cắn vào *{vp.name}* bị chặn lại bởi một thế lực bảo vệ!")
                for g in self.by_key("guardian"):
                    self.tell(g.idx, "🛡️ Đêm nay bạn đã chặn được một cuộc tấn công!")
                heal_note()
            elif vp.role.key == "serialkiller" and vp.role.dodge_left > 0:
                vp.role.dodge_left -= 1
                for w in wolves:
                    self.tell(w.idx, f"😨 Cú cắn vào *{vp.name}* trượt! Hắn né như một sát thủ dày dạn — hắn không phải người thường!")
                self.tell(v, "🥷 Bạn nghe tiếng nanh sói rình rập và kịp né được! (Lần sau sẽ không may mắn thế đâu.)")
                heal_note()
            elif vp.role.key == "elder" and vp.role.lives > 1:
                vp.role.lives -= 1
                self.tell(v, "👴 Bạn bị sói tấn công nhưng nhờ tuổi đời dày dạn nên sống sót! (Lần sau sẽ không may mắn thế đâu.)")
                for w in wolves:
                    self.tell(w.idx, f"🐺 *{vp.name}* dường như có một sức sống kỳ lạ… hắn không chết!")
                heal_note()
            elif vp.role.key == "mayor" and vp.role.revealed and vp.role.shield:
                vp.role.shield = False
                self.tell(v, "🛡️ *Khiên chức vụ* vỡ vụn thay bạn chịu cú cắn của Sói! Bạn sống sót (khiên chỉ dùng được 1 lần).")
                for w in wolves:
                    self.tell(w.idx, f"🐺 Nanh sói găm vào *{vp.name}* nhưng bị một luồng sáng chức vụ chặn lại — hắn vẫn sống!")
                heal_note()
            elif vp.role.key == "knight" and vp.role.armor:
                vp.role.armor = False
                self.tell(v, "🛡️ *Giáp Hiệp Sĩ* vỡ tan thay bạn chịu cú cắn của Sói! Bạn sống sót (giáp chỉ đỡ 1 lần).")
                for w in wolves:
                    self.tell(w.idx, f"🐺 Nanh sói găm vào *{vp.name}* nhưng gãy trên lớp giáp Hiệp Sĩ — hắn vẫn sống!")
                heal_note()
            elif n.witch_heal and witch and pierce and LS_PIERCE_HEAL:
                self.tell(witch.idx, "💊🩸 Nanh của con sói cuối cùng quá độc — bình cứu KHÔNG cứu nổi nạn nhân (bình chưa bị tiêu hao)!")
                kills.append((v, "wolf"))
                cases[v] = dict(kind="wolf", culprits=voters)
            elif n.witch_heal and witch:
                witch.role.heal = False
                self.tell(witch.idx, f"💊 Bạn đã cứu *{vp.name}* khỏi cái chết.")
                self.tell(v, "💊 Đêm qua bạn bị tấn công nhưng có ai đó đã cứu bạn!")
                for w in wolves:
                    self.tell(w.idx, f"🐺 Nạn nhân *{vp.name}* vẫn sống — có kẻ đã cứu hắn!")
            elif curse_try:
                curse_wolf.role.curse_left = False
                self._convert_to_wolf(v)
            else:
                kills.append((v, "wolf"))
                cases[v] = dict(kind="wolf", culprits=voters)

        # 1b. Đường Cùng — bị bao vây quá đông thì con sói cuối cùng điên cuồng cắn thêm 1 nạn nhân ngẫu nhiên
        lsw = self.last_stand_wolf()
        if lsw is not None and v is not None and self.ls_others() >= LS_FRENZY_OTHERS:
            pool = [i for i in self.attackable_idx()
                    if not self.players[i].role.is_wolf and i != v and i not in [k for k, _ in kills]]
            if pool:
                t2 = self.rng.choice(pool)
                kills.append((t2, "wolf"))
                cases[t2] = dict(kind="wolf", culprits=[lsw.idx])
                self.tell(lsw.idx, f"🩸 Bị bao vây giữa đám đông, bạn hoá điên cuồng và cắn thêm cả *{self.nm(t2)}*!")

        # 2. Sát Nhân Hàng Loạt — đêm N lập kế hoạch, đêm N+1 ra tay (BỎ QUA mọi phòng thủ)
        for sk in self.by_key("serialkiller"):
            r = sk.role
            if r.plan is not None and r.plan_night < self.night_no:          # ĐÊM RA TAY
                t, r.plan = r.plan, None
                if (not self.players[t].alive or self.alpha_shielded(t) or t in [k for k, _ in kills]):
                    self.tell(sk.idx, f"❌ Kế hoạch giết *{self.nm(t)}* thất bại — mục tiêu đã chết/không thể chạm tới trước khi bạn ra tay. "
                                      "Đêm sau hãy chọn mục tiêu khác và lập kế hoạch lại từ đầu.")
                    continue
                framed = n.frame if (n.frame is not None and n.frame != sk.idx and self.players[n.frame].alive) else None
                kills.append((t, "serial"))
                cases.setdefault(t, dict(kind="serial", culprit=sk.idx, framed=framed))
                self.tell(sk.idx, f"🔪 Kế hoạch hoàn tất: *{self.nm(t)}* sẽ không thấy được bình minh.")
                self._blood_scent(sk)
            elif r.plan is None and n.serial is not None:                   # ĐÊM LẬP KẾ HOẠCH
                t = n.serial
                if t == sk.idx or not self.players[t].alive or self.alpha_shielded(t):
                    continue
                r.plan, r.plan_night = t, self.night_no
                self.tell(sk.idx, f"📝 Kế hoạch đã lập: giết *{self.nm(t)}*. Đêm mai bạn sẽ ra tay (và dựng chứng cứ). "
                                  "Nếu hắn chết trước đó, kế hoạch sẽ đổ bể.")

        # 2b. Sói Đầu Đàn thanh trừng 1 sói trong đàn
        if n.purge is not None:
            for a in self.by_key("alphawolf"):
                tp = self.players[n.purge]
                if a.role.purge_left > 0 and self._purgeable(tp):
                    a.role.purge_left -= 1
                    kills.append((n.purge, "purge"))
                    self.tell(tp.idx, "🗡️ *Sói Đầu Đàn đã thanh trừng bạn!* Nanh của chính đồng loại xé toạc cổ họng bạn…")
                    for w in self.wolves_alive():
                        if w.idx not in (tp.idx, a.idx):
                            self.tell(w.idx, f"🗡️ Sói Đầu Đàn thanh trừng *{tp.name}* — kẻ phản bội sẽ không sống qua đêm nay!")
                    break

        # 3. Phù Thuỷ đầu độc
        if (witch and witch.role.key == "witch" and n.witch_poison is not None and witch.role.poison
                and self.players[n.witch_poison].alive):
            witch.role.poison = False
            kills.append((n.witch_poison, "poison"))
            self.tell(witch.idx, f"☠️ Bạn đã đầu độc *{self.nm(n.witch_poison)}*.")

        for g in self.by_key("guardian"):
            g.role.last_guard = prot

        died: list[int] = []
        seen = set()
        for idx, cause in kills:
            if idx in seen:
                continue
            seen.add(idx)
            died += self._die(idx, cause)
        # hồ sơ vụ án cho Thám Tử ở đêm kế tiếp: chỉ những nạn nhân thực sự đã chết
        self.last_cases = {i: c for i, c in cases.items() if not self.players[i].alive}
        self.phase = "dawn"
        return died

    def _convert_to_wolf(self, idx: int) -> None:
        p = self.players[idx]
        if p.role.key == "serialkiller":
            # Sát Nhân bị nguyền: vào bầy sói (nói chuyện như đồng bọn thật) nhưng VẪN giữ mục tiêu ban đầu
            p.role.converted = True
            mates = [w.name for w in self.wolves_alive()]
            self.tell(idx, "🧬 *Lời nguyền đã ám lên bạn!* Bầy sói tưởng bạn là đồng bọn mới: bạn vào được kênh chat của bầy "
                           f"({', '.join(mates)}). Nhưng bạn VẪN là Sát Nhân — mục tiêu không đổi: giết tất cả, chỉ còn mình bạn mới thắng.")
            for w in self.wolves_alive():
                self.tell(w.idx, f"🧬 *{p.name}* đã bị nguyền và gia nhập bầy sói!")
            return
        p.role = Werewolf()
        mates = [w.name for w in self.wolves_alive() if w.idx != idx]
        self.tell(idx, "🧬 *Lời nguyền đã ứng!* Đêm qua bạn bị sói nhiễm nguyền: từ giờ bạn là *Ma Sói* 🐺.\n"
                       f"Đồng bọn: {', '.join(mates)}. Hãy giữ bí mật — và phản bội người từng là đồng đội!")
        for w in self.wolves_alive():
            if w.idx != idx:
                self.tell(w.idx, f"🧬 *{p.name}* đã bị nguyền và gia nhập bầy sói!")

    # ── Cái chết & hệ quả ───────────────────────────────────────
    def _die(self, idx: int, cause: str) -> list[int]:
        died: list[int] = []
        queue = [(idx, cause)]
        while queue:
            i, c = queue.pop(0)
            q = self.players[i]
            if not q.alive or self.alpha_shielded(i):
                continue
            q.alive = False
            q.vote_bonus = False
            q.death_cause, q.death_stamp = c, self.stamp()
            died.append(i)
            self.death_log.append((i, c, q.death_stamp))
            for sk in self.by_key("serialkiller"):          # mục tiêu của kế hoạch chết trước khi bị ra tay -> đổ bể
                if sk.role.plan == i:
                    sk.role.plan = None
                    self.tell(sk.idx, f"❌ *{q.name}* đã chết trước khi bạn kịp ra tay — kế hoạch THẤT BẠI. "
                                      "Hãy chọn mục tiêu khác và lập kế hoạch lại từ đầu.")
            # Thợ Săn chỉ bắn được khi bị SÓI CẮN hoặc SÁT NHÂN giết
            if q.role.key == "hunter" and c in ("wolf", "serial"):
                self.pending_shots.append(i)
            # Thị Trưởng đã lộ diện: được chọn người kế nhiệm phiếu x2
            if q.role.key == "mayor" and q.role.revealed:
                self.pending_successors.append(i)
            if q.role.key == "elder":
                q.role.death_night = self.night_no
            if q.lover is not None and self.players[q.lover].alive:
                lv = self.players[q.lover]
                if lv.role.key == "serialkiller":
                    # Sát Nhân KHÔNG chết theo người yêu — và chính điều đó làm hắn lộ diện
                    self.say(f"💞 *{q.name}* đã chết, nhưng người yêu của họ là *{lv.name}* lại thản nhiên đứng vững, "
                             f"chẳng hề đau lòng… *{lv.name}* KHÔNG phải người thường!")
                    self.tell(lv.idx, "💞 Người yêu của bạn đã chết nhưng bạn không chết theo — và cả làng đã nhận ra điều đó!")
                else:
                    queue.append((q.lover, "heartbreak"))
        self._after_deaths(died, cause)
        died += self._sk_rush()                              # chỉ còn 1 đối thủ -> Sát Nhân giết thẳng
        return died

    def _after_deaths(self, died: list[int], cause: str) -> None:
        dset = set(died)
        if self.phase in ("day", "vote") and not self.pack_wolves():
            for a in self.by_key("alphawolf"):
                self.say(f"👑⚖️ Đàn sói đã tan — *{a.name}* (Sói Đầu Đàn) mất hết đàn nên không thể tồn tại: "
                         f"*sáng mai sẽ bị treo cổ!* (Chỉ còn đêm nay để hồi sinh 1 sói nếu còn lượt.)")
        for i in died:
            q = self.players[i]
            if q.role.key == "elder":
                pool = [w for w in self.players if w.alive and w.role.is_wolf and w.role.key != "alphawolf"]
                txt = ["👴💀 *Linh hồn bạn thoát khỏi xác…* bạn nhìn thấu một góc bóng tối:"]
                if pool:
                    w = self.rng.choice(pool)
                    txt.append(f"🐺 Bạn thấy rõ MỘT con sói trong đàn: *{w.name}*. (Sói Đầu Đàn đã công khai nên không tính.)")
                else:
                    txt.append("🐺 Bạn tìm khắp bóng tối nhưng không còn con sói nào ngoài Sói Đầu Đàn.")
                if cause == "serial":
                    sks = [k.name for k in self.by_key("serialkiller")]
                    if sks:
                        txt.append("🔪 Kẻ vừa giết bạn là Sát Nhân Hàng Loạt: *" + ", ".join(sks) + "*!")
                txt.append(f"🌙 Đêm hôm sau bạn có thể *BÁO MỘNG*: nhắn cho bot ở DM này, bot sẽ đăng vào nhóm chính "
                           f"(tối đa {DREAM_MSGS} tin, chỉ 1 đêm duy nhất, nội dung do bạn tự viết). Hãy dùng thật khôn ngoan!")
                self.tell(i, "\n".join(txt))
        for p in self.players:
            if not p.alive:
                continue
            if p.role.key == "executioner" and p.role.target in dset and cause != "lynch":
                p.role = Villager()
                self.tell(p.idx, "🪓 Mục tiêu của bạn đã chết không do treo cổ… bạn trở thành *Dân Làng* thường.")

    # ════════════════════════════════════════════════════════════
    # NGÀY
    # ════════════════════════════════════════════════════════════
    def begin_day(self) -> None:
        self.day_no += 1
        self.phase = "day"
        self.judge_vetoed = False
        self.fallen = self.alpha_fall()      # Sói Đầu Đàn mất đàn -> bị treo cổ ngay lúc bình minh

    def alpha_fall(self) -> list[int]:
        """Sói Đầu Đàn không thể sống nếu mất đàn: còn sống mà hết sói trong đàn (Đầu Đàn không tính) thì
        bị Dân Làng treo cổ NGAY sáng hôm sau — không qua bỏ phiếu, Quan Toà không huỷ được."""
        died: list[int] = []
        if self.pack_wolves():
            return died
        for a in self.by_key("alphawolf"):
            died += self.lynch(a.idx)
        return died

    # ── Vòng Mô Tả ─────────────────────────────────────────────
    def begin_word_round(self) -> dict[int, str]:
        a, b, label = pick_word_pair(self.rng, self.used_words, self.settings.topic)
        self.word_pair = (a, b, label)
        self.word_hist.append((self.day_no, a, b, label))
        self.descs = []
        self.round_start = time.time()
        for p in self.players:
            p.word = None
        out = {}
        lsw = self.last_stand_wolf()
        for p in self.alive_players():
            grp = "A" if (lsw is not None and p.idx == lsw.idx) else p.role.word_group   # Đường Cùng: từ của Dân Làng
            p.word = b if grp == "B" else a
            out[p.idx] = p.word
        return out

    def word_hint(self, idx: int) -> str:
        r = self.players[idx].role
        lsw = self.last_stand_wolf()
        if lsw is not None and lsw.idx == idx:
            return "🩸 _ĐƯỜNG CÙNG: từ này là từ của DÂN LÀNG — hãy nói thật giống họ rồi dẫn phiếu bầu!_"
        if r.key == "sleeperwolf":
            return "🎭 _Từ này là từ của DÂN LÀNG (bạn được cài cắm) — hãy nói sao cho thật giống họ, rồi dẫn phiếu bầu!_"
        if r.is_wolf:
            return "⚠️ _Từ của bạn KHÁC số đông — bạn là kẻ giả mạo! Đọc lời người khác để đoán từ thật, đừng nói quá lệch._"
        return ""

    def submit_desc(self, idx: int, text: str) -> tuple[bool, str]:
        p = self.players[idx]
        text = " ".join((text or "").split())
        if not p.alive or self.word_pair is None or p.word is None:
            return False, "Bạn không tham gia vòng mô tả."
        if any(d.idx == idx for d in self.descs):
            return False, "Bạn đã gửi mô tả rồi."
        if len(text) < 2:
            return False, "Mô tả quá ngắn."
        if len(text) > 80:
            return False, "Mô tả quá dài (tối đa 80 ký tự)."
        if contains_forbidden(text, p.word):
            return False, "❌ Không được nói thẳng từ khoá! Hãy mô tả gián tiếp hơn."
        d = Desc(idx, text, len(self.descs) + 1, time.time() - self.round_start)
        self.descs.append(d)
        p.desc_count += 1
        self.desc_all.append((self.day_no, idx, text))
        return True, ""

    def desc_missing(self) -> list[int]:
        done = {d.idx for d in self.descs}
        return [i for i in self.alive_idx() if i not in done]

    # ── Mayor / Gunner / Judge / Thư ───────────────────────────
    def mayor_reveal(self, idx: int) -> bool:
        p = self.players[idx]
        if p.role.key != "mayor" or not p.alive or p.role.revealed:
            return False
        p.role.revealed = True
        p.role.shield = True
        self.say(f"🎖️ *{p.name}* LỘ DIỆN là *Thị Trưởng*! Phiếu ×2 · phá hoà khi hoà phiếu · có khiên chức vụ chặn 1 cú cắn của Sói "
                 f"· khi chết được chọn người kế nhiệm.")
        return True

    def mayor_tiebreaker(self) -> Optional[Player]:
        """Thị Trưởng đã lộ diện & còn sống — được chọn người bị treo khi hoà phiếu."""
        for p in self.by_key("mayor"):
            if p.role.revealed:
                return p
        return None

    def mayor_successor(self, mayor: int, target: Optional[int]) -> bool:
        if mayor in self.pending_successors:
            self.pending_successors.remove(mayor)
        if target is None or target == mayor or not self.players[target].alive:
            return False
        self.players[target].vote_bonus = True
        self.say(f"👑 Thị Trưởng *{self.nm(mayor)}* trước khi chết đã chỉ định *{self.nm(target)}* kế nhiệm — "
                 f"từ giờ phiếu của người này tính ×2!")
        return True

    def gunner_shoot(self, idx: int, target: int) -> list[int]:
        p = self.players[idx]
        if p.role.key != "gunner" or not self.can_use_power(p) or p.role.bullets <= 0:
            return []
        if target == idx or target not in self.attackable_idx():
            return []
        p.role.bullets -= 1
        self.say(f"🔫 *PằNG!* *{p.name}* lộ diện là *Xạ Thủ* và bắn *{self.nm(target)}*! (còn {p.role.bullets} viên)")
        return self._die(target, "gunner")

    def check_knight(self) -> Optional[Player]:
        """Cả PHE Dân Làng chỉ còn đúng 1 người sống và người đó là Dân Làng thường (vai chỉ có lá phiếu)
        -> phong Hiệp Sĩ (1 lần/ván). Còn Tiên Tri, Phù Thuỷ... (bất kỳ vai Dân Làng nào khác) thì chưa phong."""
        if self.knighted or self.over:
            return None
        village = [p for p in self.alive_players() if p.role.team == Team.VILLAGE]
        plain = [p for p in village if p.role.key == "villager"]
        self.plain_peak = max(self.plain_peak, len(plain))
        if len(village) != 1 or len(plain) != 1:
            return None
        p = plain[0]
        p.role = Knight()
        self.knighted = True
        for e in self.by_key("executioner"):          # Hiệp Sĩ miễn nhiễm treo cổ -> Đao Phủ cần mục tiêu mới
            if e.role.target == p.idx:
                pool = [q.idx for q in self.alive_players() if q.idx != e.idx and q.idx != p.idx and q.role.team == Team.VILLAGE]
                pool = pool or [q.idx for q in self.alive_players() if q.idx not in (e.idx, p.idx)]
                if pool:
                    e.role.target = self.rng.choice(pool)
                    self.tell(e.idx, f"🪓 Mục tiêu cũ được phong Hiệp Sĩ (miễn nhiễm treo cổ)! Mục tiêu mới của bạn: *{self.nm(e.role.target)}*.")
        self.say(f"⚔️ *{p.name}* là Dân Làng duy nhất còn đứng vững của cả phe — được PHONG *HIỆP SĨ*! "
                 f"Có kiếm chém người ban ngày, giáp đỡ 1 nhát cắn của Sói, và miễn nhiễm phiếu treo cổ.")
        self.tell(p.idx, "⚔️ *BẠN ĐƯỢC PHONG HIỆP SĨ!*\n\n" + self.role_card(p.idx))
        return p

    def knight_strike(self, idx: int, target: int) -> list[int]:
        p = self.players[idx]
        r = p.role
        if r.key != "knight" or not p.alive or r.sword <= 0 or r.last_swing == self.day_no:
            return []
        if target == idx or target not in self.attackable_idx():
            return []
        r.sword -= 1
        r.last_swing = self.day_no
        innocent = self.players[target].role.team == Team.VILLAGE
        self.say(f"⚔️ *KENG!* Hiệp Sĩ *{p.name}* rút kiếm chém *{self.nm(target)}*! (còn {r.sword} nhát)")
        died = self._die(target, "sword")
        if died and innocent and r.armor:
            r.armor = False
            self.tell(idx, "💔 Bạn chém nhầm một người phe Dân Làng — *GIÁP của bạn vỡ vụn* vì hổ thẹn!")
        return died

    def judge_ready(self) -> Optional[Player]:
        for p in self.by_key("judge"):
            if self.can_use_power(p) and not p.role.used:
                return p
        return None

    def judge_veto(self, idx: int) -> bool:
        p = self.players[idx]
        if p.role.key != "judge" or not self.can_use_power(p) or p.role.used or self.judge_vetoed:
            return False
        p.role.used = True
        self.judge_vetoed = True
        self.say(f"⚖️ *{p.name}* lộ diện là *Quan Toà* và HUỶ án treo cổ hôm nay! Không ai bị hành quyết.")
        return True

    def add_letter(self, idx: int, text: str) -> tuple[bool, str]:
        p = self.players[idx]
        text = " ".join((text or "").split())
        if not self.settings.letters:
            return False, "Thư nặc danh đang tắt."
        if not p.alive:
            return False, "Người chết không gửi thư được."
        if p.role.key == "serialkiller":
            return False, SK_NO_LETTER_MSG
        if p.letters_sent >= 1 + 0 * self.day_no and any(d == self.day_no and i == idx for d, i, _ in self.letters):
            return False, "Mỗi ngày chỉ được 1 thư nặc danh."
        if len(text) < 3 or len(text) > 160:
            return False, "Thư phải dài 3-160 ký tự."
        self.letters.append((self.day_no, idx, text))
        p.letters_sent += 1
        return True, ""

    # ── Bỏ phiếu ───────────────────────────────────────────────
    def begin_vote(self, candidates: Optional[list] = None, round_no: int = 1) -> None:
        lynchable = set(self.lynchable_idx())
        cands = [c for c in (candidates if candidates is not None else self.alive_idx())
                 if self.players[c].alive and c in lynchable]
        self.vote = VoteState(round=round_no, candidates=cands)
        self.phase = "vote"

    def cast_vote(self, voter: int, target: Optional[int]) -> tuple[bool, str]:
        v = self.vote
        if v is None or self.phase != "vote":
            return False, "Chưa tới giờ bỏ phiếu."
        if not self.players[voter].alive:
            return False, "Người chết không được bỏ phiếu."
        if self.players[voter].role.key == "alphawolf":
            return False, "Sói Đầu Đàn đứng ngoài lá phiếu của làng."
        if target is not None and (target not in v.candidates or not self.players[target].alive):
            return False, "Không thể bỏ phiếu cho người này."
        v.ballots[voter] = target
        return True, ""

    def vote_weight(self, voter: int) -> int:
        p = self.players[voter]
        return 2 if ((p.role.key == "mayor" and p.role.revealed) or p.vote_bonus) else 1

    def tally(self) -> dict:
        v = self.vote
        res: dict = {}
        for voter, t in v.ballots.items():
            if not self.players[voter].alive or self.players[voter].role.key == "alphawolf":
                continue
            if t is not None and not self.players[t].alive:
                continue
            res[t] = res.get(t, 0) + self.vote_weight(voter)
        return res

    def voters_done(self) -> bool:
        return all(i in self.vote.ballots for i in self.voter_idx())

    def vote_outcome(self) -> tuple[str, list]:
        """('lynch',[i]) · ('tie',[i,j..]) · ('none',[]) — có ngưỡng tối thiểu chống 1 phiếu 'chơi khăm'."""
        t = self.tally()
        players_votes = {k: w for k, w in t.items() if k is not None}
        if not players_votes:
            return "none", []
        top = max(players_votes.values())
        skip = t.get(None, 0)
        alive = len(self.voter_idx())
        need = 1 if alive <= 2 else max(2, math.ceil(alive * 0.3))
        if top < need or skip >= top:
            return "none", []
        winners = [k for k, w in players_votes.items() if w == top]
        return ("lynch", winners) if len(winners) == 1 else ("tie", winners)

    def close_vote(self) -> dict:
        """Lưu lịch sử phiếu (ai vote ai) và trả về {target: [voters]}."""
        roll: dict = {}
        for voter, t in self.vote.ballots.items():
            roll.setdefault(t, []).append(voter)
        self.vote_history.append((self.day_no, roll))
        return roll

    def lynch(self, idx: int) -> list[int]:
        p = self.players[idx]
        if not p.alive or idx not in self.lynchable_idx():
            return []
        # Đao Phủ: mục tiêu bị treo cổ = hoàn thành nhiệm vụ. Ván VẪN TIẾP TỤC, chiến thắng chỉ được công bố khi ván kết thúc.
        for e in self.by_key("executioner"):
            if e.role.target == idx and e.idx not in self.personal_wins:
                self.personal_wins.append(e.idx)
                self.tell(e.idx, f"🪓 Mục tiêu *{p.name}* đã bị treo cổ — *nhiệm vụ hoàn thành!* "
                                 "Ván vẫn tiếp tục; chiến thắng của bạn sẽ được công bố khi ván kết thúc.")
                self.say("🪓 _Có kẻ đang mỉm cười trong bóng tối…_")
        return self._die(idx, "lynch")

    def hunter_shoot(self, hunter: int, target: Optional[int]) -> list[int]:
        if hunter in self.pending_shots:
            self.pending_shots.remove(hunter)
        if target is None or target == hunter or target not in self.attackable_idx():
            return []
        return self._die(target, "hunter")

    # ════════════════════════════════════════════════════════════
    # ĐIỀU KIỆN THẮNG
    # ════════════════════════════════════════════════════════════
    def check_win(self) -> Optional[Outcome]:
        if self.over:
            return self.outcome
        alive = self.alive_players()
        n = len(alive)
        res: Optional[Outcome] = None

        if n == 0:
            res = Outcome("draw", "💀 KHÔNG AI SỐNG SÓT — HOÀ!", [])
        else:
            sk = [p for p in alive if p.role.key == "serialkiller"]
            wolves = [p for p in alive if p.role.is_wolf]
            lovers = [p for p in alive if p.lover is not None and self.players[p.lover].alive]
            if sk and n == 1:
                res = Outcome("serial", "🔪 SÁT NHÂN HÀNG LOẠT THẮNG!", [sk[0].idx])
            elif (n == 2 and len(lovers) == 2 and not sk
                  and lovers[0].role.align_key() != lovers[1].role.align_key()):
                res = Outcome("lovers", "💞 CẶP ĐÔI TÌNH YÊU THẮNG!", [p.idx for p in lovers])
            elif not wolves and not sk:
                res = Outcome("village", "🟢 DÂN LÀNG CHIẾN THẮNG!",
                              [p.idx for p in self.players if p.role.team == Team.VILLAGE])
            elif not sk and len(wolves) == n:
                # Sói phải GIẾT HẾT mọi người khác mới thắng (không thắng theo số đông) — để Dân Làng cuối cùng còn kịp
                # được phong Hiệp Sĩ, hoặc Sát Nhân Hàng Loạt còn cơ hội solo tới cùng đàn sói.
                res = Outcome("wolf", "🔴 MA SÓI CHIẾN THẮNG!",
                              [p.idx for p in self.players if p.role.team == Team.WOLF])
        if res:
            self.outcome, self.over = res, True
        return res

    # ════════════════════════════════════════════════════════════
    # BÁO CÁO
    # ════════════════════════════════════════════════════════════
    def death_line(self, idx: int, reveal: bool = True) -> str:
        p = self.players[idx]
        if p.death_cause == "heartbreak":
            base = f"💔 *{p.name}* đau đớn tới mức chết theo người yêu *{self.nm(p.lover)}*"
        elif p.death_cause == "lynch":
            base = f"⚖️ *{p.name}* đã bị treo cổ"
        elif p.death_cause == "hunter":
            base = f"🏹 *{p.name}* trúng mũi tên của Thợ Săn"
        elif p.death_cause == "gunner":
            base = f"🔫 *{p.name}* trúng đạn Xạ Thủ"
        elif p.death_cause == "sword":
            base = f"⚔️ *{p.name}* bị Hiệp Sĩ chém gục"
        else:
            base = f"💀 *{p.name}* được phát hiện đã chết"
        if reveal:
            base += f" — là {p.role.emoji} *{p.role.name}*"
        return base

    def final_report(self) -> str:
        lines = []
        for p in self.players:
            status = "🟢 sống" if p.alive else f"☠️ {p.death_stamp}: {CAUSE_TEXT.get(p.death_cause, '?')}"
            extra = ""
            if p.lover is not None:
                extra += f" 💞{self.nm(p.lover)}"
            if p.idx in self.personal_wins:
                extra += " 🏅"
            lines.append(f"{p.role.emoji} *{p.name}* — {p.role.name} ({status}){extra}")
        return "\n".join(lines)
