"""
game_tienlen.py — Engine Tiến Lên Miền Nam (luật đầy đủ: sảnh, tứ quý, 3/4 đôi
thông chặt heo, sảnh rồng tới trắng). Thuần logic, không đụng Telegram.

QUYẾT ĐỊNH NƯỚC ĐI CỦA AI LÀ THUẬT TOÁN (heuristic cứng), KHÔNG gọi LLM —
đúng yêu cầu "không phải AI điều khiển toàn bộ như prompt". LLM chỉ được
dùng ở lớp Telegram bên trên để sinh LỜI THOẠI khi AI đánh bài, không tham
gia quyết định luật chơi.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from game_cards import (
    Card, Combo, ComboType, BOMB_TYPES, BOMB_TIER, detect_combo, combo_beats,
    deal_tienlen, find_starting_player, three_spades_in_play,
    is_dragon_straight, sort_hand, fmt_cards,
)


@dataclass
class TLSeat:
    seat_id: str
    display_name: str
    user_id: int | None
    is_ai: bool = False
    hand: list = field(default_factory=list)
    finished: bool = False
    finish_rank: int | None = None


@dataclass
class TienLenGame:
    chat_id: int
    seats: list = field(default_factory=list)   # list[TLSeat], thứ tự chỗ ngồi cố định
    current_combo: object = None                # Combo | None
    last_play_seat_idx: int | None = None
    consecutive_passes: int = 0
    turn_idx: int = 0
    first_play_done: bool = False
    three_spades_live: bool = True   # False nếu chơi 2-3 người và 3♠ bị chia dư ra ngoài
    phase: str = "playing"                      # playing | done
    finished_order: list = field(default_factory=list)
    dragon_winner: int | None = None             # idx nếu có ai tới trắng ngay lúc chia
    log: list = field(default_factory=list)      # log ngắn cho hiển thị (tuỳ chọn)

    # ── Khởi tạo ────────────────────────────────────────────────
    def start(self) -> None:
        hands = deal_tienlen(len(self.seats))
        for seat, hand in zip(self.seats, hands):
            seat.hand = hand
        for i, seat in enumerate(self.seats):
            if is_dragon_straight(seat.hand):
                self.dragon_winner = i
                self.phase = "done"
                return
        self.three_spades_live = three_spades_in_play(hands)
        self.turn_idx = find_starting_player(hands)

    # ── Trạng thái chỗ ngồi ─────────────────────────────────────
    def active_indices(self) -> list[int]:
        return [i for i, s in enumerate(self.seats) if not s.finished]

    def current_seat(self) -> TLSeat | None:
        if self.phase != "playing":
            return None
        return self.seats[self.turn_idx]

    def _advance_turn(self) -> None:
        active = self.active_indices()
        if not active:
            return
        n = len(self.seats)
        nxt = (self.turn_idx + 1) % n
        while nxt not in active:
            nxt = (nxt + 1) % n
        self.turn_idx = nxt

    def must_include_3s(self) -> bool:
        return self.three_spades_live and not self.first_play_done

    # ── Liệt kê nước đi hợp lệ (dùng cho cả kiểm tra người & AI) ──
    def legal_plays(self, seat: TLSeat) -> list[Combo]:
        combos = enumerate_legal_plays(seat.hand)
        if self.must_include_3s():
            combos = [c for c in combos if any(cc.rank == "3" and cc.suit == "♠" for cc in c.cards)]
        if self.current_combo is not None:
            combos = [c for c in combos if combo_beats(c, self.current_combo)]
        return combos

    # ── Hành động: đánh bài ─────────────────────────────────────
    def play(self, seat_idx: int, cards: list[Card]) -> tuple[bool, str]:
        seat = self.seats[seat_idx]
        if self.phase != "playing" or seat_idx != self.turn_idx:
            return False, "Chưa tới lượt bạn."
        if seat.finished:
            return False, "Bạn đã đánh hết bài rồi."
        if not cards or any(c not in seat.hand for c in cards):
            return False, "Bài không hợp lệ hoặc không có trong tay bạn."
        combo = detect_combo(cards)
        if combo is None:
            return False, "Bộ bài này không đúng luật (không phải đôi/sảnh/tứ quý...)."
        if self.must_include_3s() and not any(c.rank == "3" and c.suit == "♠" for c in cards):
            return False, "Nước đi đầu tiên của ván phải có quân 3 Bích."
        if self.current_combo is not None and not combo_beats(combo, self.current_combo):
            return False, "Bộ bài này không đè được bài hiện tại trên bàn."

        for c in cards:
            seat.hand.remove(c)
        self.current_combo = combo
        self.last_play_seat_idx = seat_idx
        self.consecutive_passes = 0
        self.first_play_done = True

        if not seat.hand:
            seat.finished = True
            seat.finish_rank = len(self.finished_order) + 1
            self.finished_order.append(seat_idx)
            remaining = self.active_indices()
            if len(remaining) <= 1:
                if remaining:
                    last = self.seats[remaining[0]]
                    last.finished = True
                    last.finish_rank = len(self.finished_order) + 1
                    self.finished_order.append(remaining[0])
                self.phase = "done"
                return True, "OK"

        self._advance_turn()
        return True, "OK"

    # ── Hành động: bỏ lượt ──────────────────────────────────────
    def pass_turn(self, seat_idx: int) -> tuple[bool, str]:
        seat = self.seats[seat_idx]
        if self.phase != "playing" or seat_idx != self.turn_idx:
            return False, "Chưa tới lượt bạn."
        if self.current_combo is None:
            return False, "Đang ra bài tự do — không thể bỏ lượt, phải đánh 1 bộ bài."
        self.consecutive_passes += 1
        self._advance_turn()

        leader_active = (self.last_play_seat_idx is not None
                          and self.last_play_seat_idx in self.active_indices())
        reset = (self.turn_idx == self.last_play_seat_idx) if leader_active else (
            self.consecutive_passes >= len(self.active_indices())
        )
        if reset:
            self.current_combo = None
            self.consecutive_passes = 0
        return True, "OK"

    # ── AI: quyết định nước đi (thuật toán "thông minh" hơn, vẫn không LLM) ──
    #
    # So với bản cũ (chỉ đánh bừa combo nhỏ nhất theo power), bản này:
    #   1. LUÔN kiểm tra nước đi THẮNG NGAY trước tiên — kể cả khi đang phải
    #      đè bài (bản cũ chỉ check khi được ra bài tự do, bỏ lỡ nhiều nước
    #      thắng khi đang ở thế đè).
    #   2. Ưu tiên xả các lá RỜI (không nằm trong đôi/sảnh nào) trước, hạn chế
    #      phá vỡ đôi/sảnh của chính mình một cách vô ích — đo bằng hàm
    #      `waste()` (phí tổn: phá mất bao nhiêu lá "ăn theo" cùng hàng).
    #   3. Theo dõi số lá của đối thủ: nếu có ai sắp về đích (<=3 lá), AI sẽ
    #      chuyển sang đánh nhanh — ưu tiên combo dài (sảnh/đôi thông) để rút
    #      ngắn số lượt của chính mình thay vì tiết kiệm dần.
    #   4. Không còn phí bomb chặt heo vô tội vạ: chỉ chặt khi thật cần (sắp
    #      thắng, đối thủ vừa chặt đang nguy hiểm, hoặc đang dư bomb khác).
    def ai_decide(self, seat: TLSeat) -> list[Card] | None:
        """Trả về list Card để đánh, hoặc None nếu bỏ lượt."""
        options = self.legal_plays(seat)
        if not options:
            return None  # bỏ lượt (hoặc không có nước nào nếu đang lead — không thể xảy ra)

        # (1) Thắng ngay nếu có nước đi dùng hết sạch bài — check trước tiên,
        # áp dụng cho CẢ hai tình huống (dẫn bài lẫn đang phải đè).
        for c in options:
            if len(c.cards) == len(seat.hand):
                return list(c.cards)

        rank_counts: dict[int, int] = {}
        for c in seat.hand:
            rank_counts[c.tl_rank_value] = rank_counts.get(c.tl_rank_value, 0) + 1

        def waste(combo: Combo) -> int:
            """Phí tổn ước lượng khi đánh `combo`: còn lại bao nhiêu lá CÙNG
            HÀNG bị "mồ côi" hoá sau nước này (vd rút 1 lá khỏi đôi -> phí 1;
            rút nguyên đôi trọn vẹn -> phí 0). Càng thấp càng đáng ưu tiên."""
            used_per_rank: dict[int, int] = {}
            for cc in combo.cards:
                used_per_rank[cc.tl_rank_value] = used_per_rank.get(cc.tl_rank_value, 0) + 1
            return sum(max(0, rank_counts[v] - n) for v, n in used_per_rank.items())

        opponents = [s for i, s in enumerate(self.seats) if i != self.turn_idx and not s.finished]
        danger = bool(opponents) and min(len(s.hand) for s in opponents) <= 3

        # ── (2)+(3) Đang được ra bài tự do (dẫn ván) ─────────────────
        if self.current_combo is None:
            non_bomb = [c for c in options if c.ctype not in BOMB_TYPES]
            pool = non_bomb if non_bomb else options   # chỉ còn bomb thì đành phải đánh bomb
            if danger:
                # Đối thủ sắp về đích -> ưu tiên xả NHIỀU lá/lượt để chạy đua.
                pool.sort(key=lambda c: (waste(c), -len(c.cards), c.power))
            else:
                # Bình thường -> ưu tiên giữ đôi/sảnh nguyên vẹn, xả lá rời nhỏ trước.
                pool.sort(key=lambda c: (waste(c), c.power, -len(c.cards)))
            return list(pool[0].cards)

        # ── (2) Đang phải đè bài trên bàn ─────────────────────────────
        non_bomb = [c for c in options if c.ctype not in BOMB_TYPES]
        if non_bomb:
            # Không phải chọn bomb -> luôn đè được thì đè, ưu tiên phí thấp
            # nhất rồi mới tới sức mạnh nhỏ nhất (giữ lá to dành lượt sau).
            non_bomb.sort(key=lambda c: (waste(c), c.power))
            return list(non_bomb[0].cards)

        # ── (4) Chỉ còn bomb mới đè được (bàn đang có heo/bomb yếu hơn) ─
        leader = (self.seats[self.last_play_seat_idx]
                  if self.last_play_seat_idx is not None else None)
        leader_threat = leader is not None and len(leader.hand) <= 3
        if leader_threat or _bomb_resource_count(seat.hand) > 1:
            bombs = sorted(options, key=lambda c: (BOMB_TIER[c.ctype], c.power))
            return list(bombs[0].cards)
        return None  # tiếc bomb, bỏ lượt cho qua ván heo này thay vì phí phạm


# ════════════════════════════════════════════════════════════════
# LIỆT KÊ MỌI NƯỚC ĐI HỢP LỆ TỪ 1 BÀN TAY (dùng cho AI + validate)
# ════════════════════════════════════════════════════════════════

def enumerate_legal_plays(hand: list[Card]) -> list[Combo]:
    hand = sort_hand(hand)
    by_rank: dict[int, list[Card]] = {}
    for c in hand:
        by_rank.setdefault(c.tl_rank_value, []).append(c)
    for v in by_rank:
        by_rank[v].sort(key=lambda c: c.tl_value)  # suit tăng dần trong cùng hàng

    combos: list[Combo] = []

    # Singles
    for c in hand:
        combos.append(Combo(ComboType.SINGLE, [c], c.tl_value))

    # Pairs / Triples / Quads theo từng hàng
    for v, cards in by_rank.items():
        n = len(cards)
        if n >= 2:
            for combo_cards in itertools.combinations(cards, 2):
                combos.append(Combo(ComboType.PAIR, list(combo_cards), combo_cards[-1].tl_value))
        if n >= 3:
            for combo_cards in itertools.combinations(cards, 3):
                combos.append(Combo(ComboType.TRIPLE, list(combo_cards), combo_cards[-1].tl_value))
        if n == 4:
            combos.append(Combo(ComboType.QUAD, list(cards), v))

    # Sảnh (straight, >=3 lá liên tiếp, không có 2) — lấy 1 đại diện/mỗi hàng
    avail_ranks = sorted(v for v in by_rank if v != 12)  # 12 = giá trị của "2"
    runs = _consecutive_runs(avail_ranks)
    for run in runs:
        for length in range(3, len(run) + 1):
            for start in range(0, len(run) - length + 1):
                sub = run[start:start + length]
                rep_cards = [by_rank[v][0] for v in sub]  # suit thấp nhất mỗi hàng
                combos.append(Combo(ComboType.STRAIGHT, rep_cards, max(sub)))

    # 3/4 đôi thông — hàng phải có >=2 lá, liên tiếp, không có 2
    pair_ranks = sorted(v for v in by_rank if v != 12 and len(by_rank[v]) >= 2)
    pruns = _consecutive_runs(pair_ranks)
    for run in pruns:
        for length in (3, 4):
            if len(run) < length:
                continue
            for start in range(0, len(run) - length + 1):
                sub = run[start:start + length]
                rep_cards = []
                for v in sub:
                    rep_cards.extend(by_rank[v][:2])
                ctype = ComboType.THREE_PAIR_RUN if length == 3 else ComboType.FOUR_PAIR_RUN
                combos.append(Combo(ctype, rep_cards, max(sub)))

    return combos


def _bomb_resource_count(hand: list[Card]) -> int:
    """Đếm số 'bomb' ĐỘC LẬP đang có trong tay — dùng để AI cân nhắc có đang
    dư dả bomb hay không trước khi quyết định chặt heo. Tứ quý đếm riêng
    từng hàng; mỗi ĐOẠN đôi thông liên tiếp (>=3 đôi) chỉ tính 1 bomb duy
    nhất dù có thể cắt ra thành nhiều bomb con lồng nhau (tránh đếm trùng)."""
    by_rank: dict[int, int] = {}
    for c in hand:
        by_rank[c.tl_rank_value] = by_rank.get(c.tl_rank_value, 0) + 1
    quads = sum(1 for n in by_rank.values() if n == 4)
    pair_ranks = sorted(v for v, n in by_rank.items() if v != 12 and n >= 2)  # 12 = "2"
    pair_run_bombs = sum(1 for run in _consecutive_runs(pair_ranks) if len(run) >= 3)
    return quads + pair_run_bombs


def _consecutive_runs(sorted_vals: list[int]) -> list[list[int]]:
    """Tách 1 danh sách giá trị hàng (đã sort, không trùng) thành các đoạn
    liên tiếp — vd [0,1,2,4,5] -> [[0,1,2],[4,5]]."""
    if not sorted_vals:
        return []
    runs, cur = [], [sorted_vals[0]]
    for v in sorted_vals[1:]:
        if v == cur[-1] + 1:
            cur.append(v)
        else:
            runs.append(cur)
            cur = [v]
    runs.append(cur)
    return runs
