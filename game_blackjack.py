"""
game_blackjack.py — Engine Blackjack thuần logic (không đụng Telegram).

Luật (bản KHÔNG CÓ NHÀ CÁI — các người chơi đấu xếp hạng với nhau):
- A = 11 hoặc 1 (tự động chọn giá trị không làm quá 21 — "soft/hard hand"). J/Q/K = 10.
- Ai cũng rút (hit) / dừng (stand) tới khi xong lượt. Quá 21 = "quắc" (bust) → rớt xuống BẢNG LOSER.
- Người không quắc được xếp hạng theo điểm: CÀNG GẦN 21 XẾP HẠNG CÀNG CAO (21 = hạng đầu).
  Bằng điểm thì đồng hạng; riêng Blackjack tự nhiên (2 lá đầu = 21) được xếp trên 21 thường.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from game_cards import Card, new_deck, shuffle_deck, fmt_cards


def hand_value(cards: list[Card]) -> tuple[int, bool]:
    """Trả về (điểm tối ưu, is_soft). is_soft=True nếu đang tính 1 con A=11."""
    total = sum(c.bj_value for c in cards)
    aces = sum(1 for c in cards if c.rank == "A")
    is_soft = False
    while total > 21 and aces > 0:
        total -= 10  # đổi 1 con A từ 11 -> 1
        aces -= 1
    else:
        is_soft = aces > 0 and total <= 21
    return total, is_soft


def is_bust(cards: list[Card]) -> bool:
    return hand_value(cards)[0] > 21


def is_natural_blackjack(cards: list[Card]) -> bool:
    return len(cards) == 2 and hand_value(cards)[0] == 21


@dataclass
class BJSeat:
    """1 chỗ ngồi — có thể là người thật (user_id>0) hoặc AI (is_ai=True)."""
    seat_id: str          # "u<user_id>" hoặc "ai<n>" — khoá định danh trong session
    display_name: str
    user_id: int | None   # None nếu là AI
    is_ai: bool = False
    hand: list = field(default_factory=list)
    standing: bool = False
    busted: bool = False
    result: str | None = None   # "win" (hạng 1) / "rank" (có hạng) / "lose" (quắc) / None (chưa xong)
    rank: int | None = None     # hạng (1 = cao nhất); None nếu quắc hoặc chưa xong


@dataclass
class BlackjackGame:
    chat_id: int
    seats: list = field(default_factory=list)
    deck: list = field(default_factory=list)
    turn_index: int = 0
    phase: str = "playing"   # playing -> done

    def start(self) -> None:
        self.deck = new_deck()
        shuffle_deck(self.deck)
        for s in self.seats:
            s.hand = [self.deck.pop(), self.deck.pop()]
        self.turn_index = 0
        self.phase = "playing"
        self._skip_finished_seats()

    def current_seat(self) -> BJSeat | None:
        if self.phase != "playing":
            return None
        if self.turn_index >= len(self.seats):
            return None
        return self.seats[self.turn_index]

    def _skip_finished_seats(self) -> None:
        while (self.turn_index < len(self.seats)
               and (self.seats[self.turn_index].standing or self.seats[self.turn_index].busted)):
            self.turn_index += 1
        if self.turn_index >= len(self.seats) and self.phase == "playing":
            self.phase = "done"
            self._settle()

    def hit(self, seat: BJSeat) -> Card:
        card = self.deck.pop()
        seat.hand.append(card)
        if is_bust(seat.hand):
            seat.busted = True
            self._skip_finished_seats()
        return card

    def stand(self, seat: BJSeat) -> None:
        seat.standing = True
        self._skip_finished_seats()

    def ai_decide(self, seat: BJSeat) -> str:
        """AI quyết định hit/stand. Không còn nhà cái nên chỉ nhìn bài mình: mục tiêu là
        điểm cao nhưng KHÔNG quắc (quắc = xuống bảng Loser). Thuật toán cứng, KHÔNG hỏi LLM."""
        total, soft = hand_value(seat.hand)
        if total >= 21:
            return "stand"
        if soft:
            return "hit" if total <= 17 else "stand"   # tay mềm: rút thoải mái tới soft 17
        return "hit" if total <= 16 else "stand"       # tay cứng: từ 17 trở lên dừng kẻo quắc

    def _rank_key(self, seat: BJSeat) -> tuple:
        return (hand_value(seat.hand)[0], is_natural_blackjack(seat.hand))

    def _settle(self) -> None:
        """Xếp hạng: điểm cao → hạng cao (đồng điểm = đồng hạng, kiểu 1-1-3). Quắc → 'lose'."""
        alive = sorted((s for s in self.seats if not s.busted), key=self._rank_key, reverse=True)
        prev_key, prev_rank = None, 0
        for i, s in enumerate(alive, start=1):
            k = self._rank_key(s)
            if k != prev_key:
                prev_rank, prev_key = i, k
            s.rank = prev_rank
            s.result = "win" if prev_rank == 1 else "rank"
        for s in self.seats:
            if s.busted:
                s.rank, s.result = None, "lose"

    def ranked(self) -> list:
        return sorted((s for s in self.seats if not s.busted), key=lambda s: (s.rank or 99, self.seats.index(s)))

    def losers(self) -> list:
        return [s for s in self.seats if s.busted]

    def board_text(self) -> str:
        """Bảng xếp hạng cuối ván + bảng Loser (quắc)."""
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = ["🏆 *BẢNG XẾP HẠNG*"]
        for s in self.ranked():
            total, _ = hand_value(s.hand)
            bj = " — BLACKJACK! 🎉" if is_natural_blackjack(s.hand) else ""
            lines.append(f"{medals.get(s.rank, f'#{s.rank}')} {s.display_name}: {fmt_cards(s.hand)} = {total}{bj}")
        if not self.ranked():
            lines.append("_(Không ai có hạng — tất cả đều quắc!)_")
        lines.append("")
        lines.append("💀 *BẢNG LOSER*")
        los = self.losers()
        if los:
            for s in los:
                lines.append(f"❌ {s.display_name}: {fmt_cards(s.hand)} = {hand_value(s.hand)[0]}")
        else:
            lines.append("_(Trống — không ai quắc 🎉)_")
        return "\n".join(lines)

    def summary_line(self, seat: BJSeat) -> str:
        total, _ = hand_value(seat.hand)
        if seat.busted:
            tag = "💀 Quắc → bảng Loser"
        elif seat.rank == 1:
            tag = "🥇 Hạng 1"
        else:
            tag = f"Hạng {seat.rank}" if seat.rank else "…"
        return f"{seat.display_name}: {fmt_cards(seat.hand)} = {total} — {tag}"
