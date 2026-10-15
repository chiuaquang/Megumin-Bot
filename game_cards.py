"""
game_cards.py — Bộ bài Tây 52 lá dùng chung cho mọi minigame (Blackjack, Tiến Lên...).

Chỉ chứa logic thuần Python (không đụng Telegram) để có thể test độc lập
bằng `python3 game_cards.py` hoặc import vào script test riêng.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum


# ════════════════════════════════════════════════════════════════
# CARD & DECK CƠ BẢN
# ════════════════════════════════════════════════════════════════

SUITS = ["♠", "♣", "♦", "♥"]          # Bích, Chuồn, Rô, Cơ
RANKS = ["3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A", "2"]

# Thứ tự SỨC MẠNH quân bài trong Tiến Lên (3 nhỏ nhất, 2 to nhất)
_TL_RANK_VALUE = {r: i for i, r in enumerate(RANKS)}
# Thứ tự chất khi 2 lá cùng hàng (vd 2 lá 2 khác chất) — quy ước phổ biến
# miền Nam: Bích < Chuồn < Rô < Cơ. Có thể chỉnh lại nếu nhóm bạn chơi
# luật khác, không ảnh hưởng phần còn lại của engine.
_TL_SUIT_VALUE = {"♠": 0, "♣": 1, "♦": 2, "♥": 3}

# Giá trị lá bài kiểu Tây (Blackjack) — A tính riêng (1 hoặc 11) trong game_blackjack.py
_BJ_PIP_VALUE = {**{str(n): n for n in range(2, 11)}, "J": 10, "Q": 10, "K": 10, "A": 11}


@dataclass(frozen=True)
class Card:
    rank: str   # "3".."10","J","Q","K","A","2"
    suit: str   # "♠","♣","♦","♥"

    def __str__(self) -> str:
        return f"{self.rank}{self.suit}"

    @property
    def tl_value(self) -> int:
        """Giá trị sức mạnh trong Tiến Lên: hàng*4 + chất — dùng để so sánh
        2 lá bất kỳ (kể cả khác hàng) chỉ bằng 1 phép so sánh số."""
        return _TL_RANK_VALUE[self.rank] * 4 + _TL_SUIT_VALUE[self.suit]

    @property
    def tl_rank_value(self) -> int:
        return _TL_RANK_VALUE[self.rank]

    @property
    def bj_value(self) -> int:
        return _BJ_PIP_VALUE[self.rank]

    @property
    def is_two(self) -> bool:
        return self.rank == "2"


def new_deck() -> list[Card]:
    return [Card(r, s) for s in SUITS for r in RANKS]


def shuffle_deck(deck: list[Card]) -> None:
    random.shuffle(deck)


def sort_hand(cards: list[Card]) -> list[Card]:
    """Sắp bài từ nhỏ đến lớn theo thứ tự Tiến Lên — cũng hợp lý để hiển thị
    trong Blackjack (không quan trọng thứ tự ở đó)."""
    return sorted(cards, key=lambda c: c.tl_value)


def fmt_cards(cards: list[Card]) -> str:
    return " ".join(str(c) for c in cards)


def fmt_cards_numbered(cards: list[Card]) -> str:
    """Hiện bài kèm số thứ tự (1-based) để người chơi gõ/chọn — vd trong DM."""
    return " ".join(f"{i+1}:{c}" for i, c in enumerate(cards))


# ════════════════════════════════════════════════════════════════
# TIẾN LÊN — NHẬN DIỆN & SO SÁNH BỘ BÀI (COMBO)
# ════════════════════════════════════════════════════════════════

class ComboType(Enum):
    INVALID          = "invalid"
    SINGLE           = "single"
    PAIR             = "pair"
    TRIPLE           = "triple"
    STRAIGHT         = "straight"           # sảnh, >=3 lá liên tiếp, không có 2
    THREE_PAIR_RUN   = "three_pair_run"     # 3 đôi thông liên tiếp — bomb
    FOUR_PAIR_RUN    = "four_pair_run"      # 4 đôi thông liên tiếp — bomb mạnh hơn
    QUAD             = "quad"               # tứ quý — bomb mạnh nhất


# Các loại được coi là "bomb" (chặt được heo/2, và chặt được bomb yếu hơn)
BOMB_TYPES = {ComboType.THREE_PAIR_RUN, ComboType.FOUR_PAIR_RUN, ComboType.QUAD}
# Bomb càng lớn số càng mạnh — dùng để so 2 bomb khác loại với nhau.
# (Quy ước phổ biến: tứ quý > 4 đôi thông > 3 đôi thông; có nhóm chơi
# ngược lại — chỉnh BOMB_TIER nếu cần khác.)
BOMB_TIER = {ComboType.THREE_PAIR_RUN: 0, ComboType.FOUR_PAIR_RUN: 1, ComboType.QUAD: 2}


@dataclass
class Combo:
    ctype: int
    cards: list  # list[Card], đã sort tăng dần
    # "sức mạnh" đại diện để so sánh 2 combo CÙNG loại & CÙNG độ dài
    power: int

    def __repr__(self):
        return f"<Combo {self.ctype.value}: {fmt_cards(self.cards)}>"


def _is_consecutive_no_two(ranks_sorted_vals: list[int]) -> bool:
    if any(v == _TL_RANK_VALUE["2"] for v in ranks_sorted_vals):
        return False
    return all(b - a == 1 for a, b in zip(ranks_sorted_vals, ranks_sorted_vals[1:]))


def detect_combo(cards: list[Card]) -> Combo | None:
    """Nhận diện 1 nước ra bài hợp lệ. Trả None nếu không khớp bất kỳ loại
    nào (bài rác không theo luật)."""
    if not cards:
        return None
    n = len(cards)
    cs = sort_hand(cards)
    ranks = [c.rank for c in cs]
    rank_vals = [c.tl_rank_value for c in cs]
    top = cs[-1]

    if n == 1:
        return Combo(ComboType.SINGLE, cs, top.tl_value)

    if n == 2:
        if ranks[0] == ranks[1]:
            return Combo(ComboType.PAIR, cs, top.tl_value)
        return None

    if n == 3:
        if len(set(ranks)) == 1:
            return Combo(ComboType.TRIPLE, cs, top.tl_value)
        if _is_consecutive_no_two(sorted(set(rank_vals))) and len(set(rank_vals)) == 3:
            return Combo(ComboType.STRAIGHT, cs, max(rank_vals))
        return None

    if n == 4:
        if len(set(ranks)) == 1:
            return Combo(ComboType.QUAD, cs, rank_vals[0])
        if _is_consecutive_no_two(sorted(set(rank_vals))) and len(set(rank_vals)) == 4:
            return Combo(ComboType.STRAIGHT, cs, max(rank_vals))
        return None

    # n >= 5
    uniq_vals = sorted(set(rank_vals))
    if len(uniq_vals) == len(rank_vals) and _is_consecutive_no_two(uniq_vals):
        return Combo(ComboType.STRAIGHT, cs, max(rank_vals))

    # Đôi thông (3 hoặc 4 đôi liên tiếp) — n phải chẵn (6 hoặc 8), mỗi hàng
    # đúng 2 lá, các hàng liên tiếp không chứa 2.
    if n in (6, 8):
        from collections import Counter
        cnt = Counter(rank_vals)
        if all(v == 2 for v in cnt.values()):
            distinct = sorted(cnt.keys())
            if len(distinct) == n // 2 and _is_consecutive_no_two(distinct):
                ctype = ComboType.THREE_PAIR_RUN if n == 6 else ComboType.FOUR_PAIR_RUN
                return Combo(ctype, cs, max(distinct))

    return None


def combo_beats(new: Combo, current: Combo | None) -> bool:
    """True nếu `new` được phép đè lên `current` (current=None nghĩa là
    đang ra bài tự do, đầu vòng)."""
    if current is None:
        return True

    # Bomb chặt bomb yếu hơn, hoặc chặt single/pair/triple 2 (heo)
    if new.ctype in BOMB_TYPES:
        if current.ctype in BOMB_TYPES:
            if BOMB_TIER[new.ctype] != BOMB_TIER[current.ctype]:
                return BOMB_TIER[new.ctype] > BOMB_TIER[current.ctype]
            return new.power > current.power
        # current không phải bomb — bomb chỉ được chặt khi current là bài
        # chứa quân 2 (heo): single 2 / pair 2 / triple 2.
        if current.cards and current.cards[-1].rank == "2":
            return True
        return False

    # new không phải bomb: current là bomb thì không thể đè (chỉ bomb mạnh
    # hơn mới đè được bomb).
    if current.ctype in BOMB_TYPES:
        return False

    # Bài thường: phải cùng loại & cùng số lá mới so được.
    if new.ctype != current.ctype or len(new.cards) != len(current.cards):
        return False
    return new.power > current.power


def is_dragon_straight(hand: list[Card]) -> bool:
    """Sảnh rồng — 12 lá liên tiếp từ 3 đến A (không có quân 2 nào) nằm
    trọn trong 1 bàn tay 13 lá => tới trắng (auto-win) ngay lúc chia bài."""
    vals = sorted({c.tl_rank_value for c in hand if c.rank != "2"})
    if len(vals) < 12:
        return False
    # Cần đúng dải liên tiếp 3..A (giá trị 0..11)
    return set(range(0, 12)).issubset(set(vals))


def deal_tienlen(num_players: int) -> list[list[Card]]:
    """Chia bài Tiến Lên: luôn chia đúng 13 lá/người (chuẩn miền Nam), phần
    dư (khi chơi 2-3 người) để riêng không dùng — theo đúng luật nhà phổ
    biến khi bàn không đủ 4 người."""
    assert 2 <= num_players <= 4, "Tiến Lên chỉ chơi được 2-4 người"
    deck = new_deck()
    shuffle_deck(deck)
    hands = [sort_hand(deck[i * 13:(i + 1) * 13]) for i in range(num_players)]
    return hands


def find_starting_player(hands: list[list[Card]]) -> int:
    """Người có 3♠ (Bích) đi trước, theo đúng luật chuẩn. Khi chơi 2-3 người
    (chia dư bài, có thể 3♠ nằm trong phần bài KHÔNG được chia), fallback:
    ai cầm lá nhỏ nhất trong số bài thực sự đang chơi sẽ đi trước."""
    for i, hand in enumerate(hands):
        if any(c.rank == "3" and c.suit == "♠" for c in hand):
            return i
    best_i, best_val = 0, None
    for i, hand in enumerate(hands):
        for c in hand:
            if best_val is None or c.tl_value < best_val:
                best_val, best_i = c.tl_value, i
    return best_i


def three_spades_in_play(hands: list[list[Card]]) -> bool:
    return any(c.rank == "3" and c.suit == "♠" for hand in hands for c in hand)
