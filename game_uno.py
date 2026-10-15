"""
game_uno.py — Engine UNO (bộ bài chuẩn quốc tế 108 lá). Thuần logic, không
đụng Telegram — test độc lập được bằng `python3 game_uno.py` hoặc import vào
script test riêng, giống game_cards.py / game_tienlen.py / game_blackjack.py.

QUYẾT ĐỊNH NƯỚC ĐI CỦA AI LÀ THUẬT TOÁN (heuristic), KHÔNG gọi LLM — đúng
kiến trúc chung của cả bộ minigame. LLM chỉ được dùng ở lớp Telegram bên
trên (handlers_games.py) để sinh LỜI THOẠI khi AI đánh bài.

Luật áp dụng (chuẩn phổ biến, đơn giản hoá vài chỗ để chơi vui — xem ghi chú
tại từng chỗ, cùng tinh thần "đơn giản hoá" đã ghi trong game_blackjack.py):
- 108 lá: 4 màu (Đỏ/Vàng/Lục/Lam) x (một lá 0, hai lá mỗi số 1-9, hai lá mỗi
  Skip/Reverse/+2) = 25 lá/màu x 4 = 100 lá, cộng 4 Đổi màu + 4 Đổi màu +4 = 108.
- Đánh lá cùng MÀU, cùng SỐ/KÝ HIỆU với lá trên cùng chồng bỏ, hoặc lá Đổi
  màu (luôn đánh được, người đánh chọn màu kế tiếp).
- Skip: người kế tiếp mất lượt. Reverse: đảo chiều chơi (2 người thì coi như
  Skip luôn, theo đúng luật gốc). +2 / +4: người kế tiếp bốc bài rồi mất lượt
  (bản này KHÔNG cho "chồng bài" +2/+4 lên nhau — theo đúng luật UNO gốc,
  nhiều nhóm chơi biến thể cho chồng nhưng đó là house-rule, không phải luật
  chuẩn).
- Không có lá hợp lệ -> bốc 1 lá; nếu lá vừa bốc đánh được, được đánh luôn
  (không bắt buộc); không thì qua lượt (đơn giản hoá: chỉ bốc đúng 1 lá/lượt
  khi bí bài, không bốc-tới-khi-có-bài-đánh-được).
- Còn đúng 1 lá phải hô "UNO"; quên hô thì người khác bấm "bắt lỗi" trước
  lượt kế của người đó -> bị phạt bốc 2 lá. AI luôn tự hô đúng lúc (không bao
  giờ bị bắt) — đúng phần "AI chơi thông minh hơn".
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum


# ════════════════════════════════════════════════════════════════
# LÁ BÀI & BỘ BÀI
# ════════════════════════════════════════════════════════════════

class UnoColor(Enum):
    RED = "🔴"
    YELLOW = "🟡"
    GREEN = "🟢"
    BLUE = "🔵"
    WILD = "⚫"     # chỉ là màu "chưa xác định" của lá Đổi màu khi còn úp trong bộ

COLORS4 = (UnoColor.RED, UnoColor.YELLOW, UnoColor.GREEN, UnoColor.BLUE)


class UnoType(Enum):
    NUMBER = "number"
    SKIP = "skip"
    REVERSE = "reverse"
    DRAW_TWO = "draw_two"
    WILD = "wild"
    WILD_DRAW_FOUR = "wild4"


_ACTION_LABEL = {
    UnoType.SKIP: "🚫 Mất lượt",
    UnoType.REVERSE: "🔁 Đảo chiều",
    UnoType.DRAW_TWO: "+2",
    UnoType.WILD: "Đổi màu",
    UnoType.WILD_DRAW_FOUR: "+4 Đổi màu",
}


@dataclass(frozen=True)
class UnoCard:
    color: UnoColor
    ctype: UnoType
    number: int | None = None   # 0-9 khi ctype == NUMBER, None ở mọi loại khác

    def __str__(self) -> str:
        if self.ctype == UnoType.NUMBER:
            return f"{self.color.value}{self.number}"
        return f"{self.color.value}{_ACTION_LABEL[self.ctype]}"

    @property
    def is_wild(self) -> bool:
        return self.ctype in (UnoType.WILD, UnoType.WILD_DRAW_FOUR)

    @property
    def is_action(self) -> bool:
        return self.ctype != UnoType.NUMBER


def new_uno_deck() -> list[UnoCard]:
    deck: list[UnoCard] = []
    for color in COLORS4:
        deck.append(UnoCard(color, UnoType.NUMBER, 0))
        for n in list(range(1, 10)) * 2:
            deck.append(UnoCard(color, UnoType.NUMBER, n))
        for _ in range(2):
            deck.append(UnoCard(color, UnoType.SKIP))
            deck.append(UnoCard(color, UnoType.REVERSE))
            deck.append(UnoCard(color, UnoType.DRAW_TWO))
    for _ in range(4):
        deck.append(UnoCard(UnoColor.WILD, UnoType.WILD))
        deck.append(UnoCard(UnoColor.WILD, UnoType.WILD_DRAW_FOUR))
    return deck  # đúng 108 lá


_COLOR_ORDER = {UnoColor.RED: 0, UnoColor.YELLOW: 1, UnoColor.GREEN: 2, UnoColor.BLUE: 3, UnoColor.WILD: 4}
_TYPE_ORDER = {UnoType.NUMBER: 0, UnoType.DRAW_TWO: 1, UnoType.SKIP: 2, UnoType.REVERSE: 3,
               UnoType.WILD: 4, UnoType.WILD_DRAW_FOUR: 5}


def sort_uno_hand(cards: list[UnoCard]) -> list[UnoCard]:
    """Sắp bài theo màu rồi theo loại/số — để hiển thị dễ nhìn, cùng màu đứng
    cạnh nhau thay vì lộn xộn theo thứ tự bốc được."""
    return sorted(cards, key=lambda c: (_COLOR_ORDER[c.color], _TYPE_ORDER[c.ctype], c.number or 0))


def fmt_uno_cards(cards: list[UnoCard]) -> str:
    return " ".join(str(c) for c in cards)


def fmt_uno_cards_numbered(cards: list[UnoCard]) -> str:
    return " ".join(f"{i + 1}:{c}" for i, c in enumerate(cards))


# ════════════════════════════════════════════════════════════════
# ENGINE
# ════════════════════════════════════════════════════════════════

@dataclass
class UnoSeat:
    seat_id: str
    display_name: str
    user_id: int | None
    is_ai: bool = False
    hand: list = field(default_factory=list)      # list[UnoCard]
    finished: bool = False
    finish_rank: int | None = None
    called_uno: bool = False   # đã hô "UNO" ở lần gần nhất về còn 1 lá chưa


@dataclass
class UnoGame:
    chat_id: int
    seats: list = field(default_factory=list)         # list[UnoSeat], ghế cố định
    draw_pile: list = field(default_factory=list)      # list[UnoCard]
    discard_pile: list = field(default_factory=list)   # list[UnoCard], cuối = lá trên cùng
    current_color: UnoColor = UnoColor.RED
    direction: int = 1                 # +1 = xuôi theo thứ tự ghế, -1 = ngược
    turn_idx: int = 0
    phase: str = "playing"             # playing | done
    finished_order: list = field(default_factory=list)
    pending_uno_seat: int | None = None   # ghế vừa còn 1 lá & CHƯA hô -> có thể bị bắt lỗi
    last_action_note: str = ""            # mô tả hiệu ứng nước vừa rồi, để lớp Telegram hiển thị

    # ── Khởi tạo ────────────────────────────────────────────────
    def start(self) -> None:
        deck = new_uno_deck()
        random.shuffle(deck)
        for seat in self.seats:
            seat.hand = [deck.pop() for _ in range(7)]

        # Lá lật đầu tiên không được là +4 (luật chuẩn — +4 chỉ dùng khi bí
        # màu, không hợp lý làm lá mở màn); nếu bốc trúng, nhét lại và xáo.
        while True:
            first = deck.pop()
            if first.ctype != UnoType.WILD_DRAW_FOUR:
                break
            deck.insert(0, first)
            random.shuffle(deck)

        self.discard_pile = [first]
        self.draw_pile = deck
        self.current_color = first.color if first.color != UnoColor.WILD else random.choice(COLORS4)
        self.turn_idx = 0
        self.direction = 1

        # Hiệu ứng lá mở màn nếu là lá hành động (theo đúng luật phổ biến).
        if first.ctype == UnoType.SKIP:
            self._advance_turn(1)
        elif first.ctype == UnoType.REVERSE:
            self.direction = -1
            self._advance_turn(1)
        elif first.ctype == UnoType.DRAW_TWO:
            self._advance_turn(1)
            self._draw_n(self.turn_idx, 2)
            self._advance_turn(1)

    # ── Trạng thái chỗ ngồi ─────────────────────────────────────
    def active_indices(self) -> list[int]:
        return [i for i, s in enumerate(self.seats) if not s.finished]

    def current_seat(self) -> UnoSeat | None:
        if self.phase != "playing":
            return None
        return self.seats[self.turn_idx]

    def top_card(self) -> UnoCard:
        return self.discard_pile[-1]

    def _advance_turn(self, steps: int = 1) -> None:
        """Di chuyển lượt `steps` bước dọc vòng NGƯỜI-CÒN-CHƠI theo `direction`,
        bắt đầu đếm từ vị trí ghế hiện tại (kể cả khi ghế đó vừa hết bài/loại,
        không cần ghế xuất phát phải đang active — chỉ đích đến mới cần)."""
        active = self.active_indices()
        if not active:
            return
        if len(active) == 1:
            self.turn_idx = active[0]
            return
        n = len(self.seats)
        idx = self.turn_idx
        hops, guard = 0, 0
        while hops < steps and guard < n * 4 + 8:
            guard += 1
            idx = (idx + self.direction) % n
            if idx in active:
                hops += 1
        self.turn_idx = idx

    # ── Bốc bài (tự xáo lại chồng bỏ khi hết bài bốc) ────────────
    def _reshuffle_if_needed(self) -> None:
        if self.draw_pile or len(self.discard_pile) <= 1:
            return
        top = self.discard_pile[-1]
        rest = self.discard_pile[:-1]
        random.shuffle(rest)
        self.draw_pile = rest
        self.discard_pile = [top]

    def _draw_n(self, seat_idx: int, n: int) -> list[UnoCard]:
        seat = self.seats[seat_idx]
        drawn = []
        for _ in range(n):
            self._reshuffle_if_needed()
            if not self.draw_pile:
                break   # hết sạch bài cả 2 chồng — cực hiếm, bốc được bao nhiêu hay bấy nhiêu
            card = self.draw_pile.pop()
            seat.hand.append(card)
            drawn.append(card)
        if drawn and self.pending_uno_seat == seat_idx:
            # Vừa bốc thêm bài (vd bị dính +2/+4 của người khác) -> không còn
            # đúng 1 lá nữa, không thể bị "bắt lỗi quên hô UNO" nữa.
            self.pending_uno_seat = None
        return drawn

    # ── Luật hợp lệ ───────────────────────────────────────────────
    def is_playable(self, card: UnoCard, top: UnoCard | None = None) -> bool:
        if card.is_wild:
            return True
        top = top or self.top_card()
        if card.color == self.current_color:
            return True
        if card.ctype == UnoType.NUMBER:
            return top.ctype == UnoType.NUMBER and card.number == top.number
        return card.ctype == top.ctype   # cùng loại ký hiệu (Skip/Reverse/+2), khác màu vẫn được

    def legal_plays(self, seat: UnoSeat) -> list[UnoCard]:
        return [c for c in seat.hand if self.is_playable(c)]

    # ── Hành động: đánh bài ─────────────────────────────────────
    def play(self, seat_idx: int, card: UnoCard, chosen_color: UnoColor | None = None) -> tuple[bool, str]:
        if self.phase != "playing" or seat_idx != self.turn_idx:
            return False, "Chưa tới lượt bạn."
        seat = self.seats[seat_idx]
        if seat.finished:
            return False, "Bạn đã về đích rồi."
        if card not in seat.hand:
            return False, "Bạn không có lá này trong tay."
        if not self.is_playable(card):
            return False, "Lá này không hợp lệ (khác màu/số/ký hiệu với lá trên bàn)."
        if card.is_wild:
            if chosen_color is None or chosen_color == UnoColor.WILD:
                return False, "Cần chọn 1 trong 4 màu khi đánh lá Đổi màu."
        else:
            chosen_color = None

        active_before = self.active_indices()   # số người TRƯỚC nước này, để xử lý luật "2 người = Reverse~Skip"
        heads_up = len(active_before) == 2

        seat.hand.remove(card)
        self.discard_pile.append(card)
        self.current_color = chosen_color if card.is_wild else card.color

        just_won = not seat.hand
        if just_won:
            seat.finished = True
            seat.finish_rank = len(self.finished_order) + 1
            self.finished_order.append(seat_idx)
            self.pending_uno_seat = None
        else:
            self.pending_uno_seat = seat_idx if len(seat.hand) == 1 and not seat.called_uno else None
        seat.called_uno = False

        remaining = self.active_indices()
        if just_won and len(remaining) <= 1:
            if remaining:
                loser = self.seats[remaining[0]]
                loser.finished = True
                loser.finish_rank = len(self.finished_order) + 1
                self.finished_order.append(remaining[0])
            self.phase = "done"
            self.last_action_note = ""
            return True, "OK"

        note = ""
        if card.ctype == UnoType.SKIP:
            self._advance_turn(2)
            note = "mất lượt người kế tiếp"
        elif card.ctype == UnoType.REVERSE:
            self.direction *= -1
            if heads_up:
                if just_won:
                    self._advance_turn(1)   # người vừa đánh đã ra -> chuyển thẳng cho người còn lại
                # heads_up & không thắng: giữ nguyên turn_idx = quay lại đúng người vừa đánh (skip đối thủ)
            else:
                self._advance_turn(1)
            note = "đảo chiều"
        elif card.ctype == UnoType.DRAW_TWO:
            self._advance_turn(1)
            victim_idx = self.turn_idx
            self._draw_n(victim_idx, 2)
            note = f"{self.seats[victim_idx].display_name} bốc 2 lá và mất lượt"
            self._advance_turn(1)
        elif card.ctype == UnoType.WILD_DRAW_FOUR:
            self._advance_turn(1)
            victim_idx = self.turn_idx
            self._draw_n(victim_idx, 4)
            note = f"{self.seats[victim_idx].display_name} bốc 4 lá và mất lượt"
            self._advance_turn(1)
        else:
            self._advance_turn(1)

        self.last_action_note = note
        return True, "OK"

    # ── Hành động: bốc bài khi bí (không có lá hợp lệ) ───────────
    def draw_for_turn(self, seat_idx: int) -> UnoCard | None:
        if self.phase != "playing" or seat_idx != self.turn_idx:
            return None
        self._reshuffle_if_needed()
        if not self.draw_pile:
            return None
        card = self.draw_pile.pop()
        self.seats[seat_idx].hand.append(card)
        if self.pending_uno_seat == seat_idx:
            self.pending_uno_seat = None
        return card

    def end_turn_after_draw(self, seat_idx: int) -> None:
        """Không đánh lá vừa bốc (hoặc nó không hợp lệ) -> qua lượt kế."""
        if self.phase != "playing" or seat_idx != self.turn_idx:
            return
        self._advance_turn(1)

    # ── Hô UNO / bắt lỗi quên hô ──────────────────────────────────
    def call_uno(self, seat_idx: int) -> tuple[bool, str]:
        seat = self.seats[seat_idx]
        if len(seat.hand) != 1:
            return False, "Bạn chỉ hô UNO được khi trong tay còn đúng 1 lá."
        seat.called_uno = True
        if self.pending_uno_seat == seat_idx:
            self.pending_uno_seat = None
        return True, "OK"

    def catch_uno(self, target_idx: int) -> tuple[bool, str]:
        if self.pending_uno_seat != target_idx:
            return False, "Không có ai quên hô UNO để bắt lỗi cả."
        target = self.seats[target_idx]
        self._draw_n(target_idx, 2)
        self.pending_uno_seat = None
        return True, f"{target.display_name} quên hô UNO — bị phạt bốc 2 lá!"

    # ── AI: quyết định nước đi (thuật toán, không LLM) ────────────
    def ai_decide(self, seat: UnoSeat) -> tuple[UnoCard, UnoColor | None] | None:
        """Trả (card, màu_chọn_nếu_là_wild) để đánh, hoặc None nếu phải bốc
        bài (không có lá hợp lệ). Ưu tiên:
          1. Có lá nào đánh HẾT bài (thắng ngay) thì đánh luôn.
          2. Nếu có đối thủ sắp về đích (<=2 lá): ưu tiên bài "tấn công"
             (Skip/Reverse/+2/+4) để cản, thay vì tiết kiệm.
          3. Ngược lại: né dùng Đổi màu nếu còn lá thường đánh được (giữ lại
             Đổi màu làm "lá cứu nguy" cho lượt sau), và ưu tiên đánh theo
             màu mình đang CÒN NHIỀU nhất để giữ nhịp linh hoạt các lượt sau.
          4. Khi buộc phải đánh lá Đổi màu: chọn màu mình còn nhiều nhất sau
             khi bỏ lá đó ra, để tối đa cơ hội đánh tiếp được ở lượt sau.
        """
        options = self.legal_plays(seat)
        if not options:
            return None

        if len(seat.hand) == 1:
            # Lá cuối cùng, đánh được thì đánh luôn để thắng — không cân nhắc gì thêm.
            win_card = options[0]
            return win_card, self.best_color_for(seat, win_card)

        my_idx = self.turn_idx
        other_sizes = [len(s.hand) for i, s in enumerate(self.seats) if i != my_idx and not s.finished]
        threat = bool(other_sizes) and min(other_sizes) <= 2

        def color_count(color: UnoColor) -> int:
            return sum(1 for c in seat.hand if c.color == color)

        def score(c: UnoCard):
            offense_first = 0 if (threat and c.ctype in (
                UnoType.DRAW_TWO, UnoType.WILD_DRAW_FOUR, UnoType.SKIP, UnoType.REVERSE
            )) else 1
            save_wild = 1 if c.is_wild else 0
            keep_flow = -color_count(c.color) if not c.is_wild else 0
            return (offense_first, save_wild, keep_flow, c.is_action)

        options.sort(key=score)
        chosen = options[0]
        return chosen, self.best_color_for(seat, chosen)

    def best_color_for(self, seat: UnoSeat, card_to_play: UnoCard) -> UnoColor | None:
        if not card_to_play.is_wild:
            return None
        remaining = [c for c in seat.hand if c is not card_to_play]
        counts = {col: sum(1 for c in remaining if c.color == col) for col in COLORS4}
        best = max(COLORS4, key=lambda col: counts[col])
        if counts[best] == 0:
            return random.choice(COLORS4)
        return best
