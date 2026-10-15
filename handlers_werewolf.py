

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from telegram import Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb
from telegram.constants import ParseMode
from telegram.error import TelegramError

from config import CONFIG
from utils import is_owner, safe_reply, to_markdown_v2

import handlers_games as hg
from game_werewolf import (
    WerewolfGame, WWSettings, KIND_INFO, MIN_PLAYERS, MAX_PLAYERS, NONE, TEAM_LABEL, Team, Outcome, SK_NO_LETTER_MSG,
)
from ww_words import TOPICS

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════
# TRẠNG THÁI RUNTIME (RAM only)
# ════════════════════════════════════════════════════════════════

WW_LOBBIES: dict[int, "WWRoom"] = {}     # chat_id -> phòng chờ
WW_GAMES: dict[int, "WWSession"] = {}    # chat_id -> ván đang chạy
WW_USER: dict[int, int] = {}             # user_id -> chat_id (ván/phòng đang tham gia)

# Thời lượng gốc (giây) — nhân với WWSettings.scale() (fast 0.6 · normal 1 · slow 1.7)
T_NIGHT = 70
T_WITCH = 30
T_TALK = 100
T_TALK_NOWORD = 130
T_VOTE = 60
T_RUNOFF = 35
T_HUNTER = 30
T_MAYOR = 25                             # Thị Trưởng phá hoà / chọn người kế nhiệm
MIN_TALK_SKIP = 0.5                      # > 50% người sống bấm "bỏ phiếu sớm" thì qua vote
_MD_BAD = re.compile(r"[*_\[\]`~]")
_BG: set = set()

MODES = {"classic": "📜 Cổ điển", "balanced": "⚖️ Cân bằng", "drama": "🎭 Drama"}
SPEEDS = {"fast": "⚡ Nhanh", "normal": "⏱️ Vừa", "slow": "🐢 Chậm"}
MODE_BLURB = {
    "classic": "Ít vai lạ, không phe thứ ba — nhẹ nhàng, dễ chơi.",
    "balanced": "Vai đặc biệt + phe thứ ba theo số người — chuẩn nhất.",
    "drama": "Tối đa vai gây biến: Người Lạc Lối, Sói Nằm Vùng, Đao Phủ, Pháp Sư, Sát Nhân (≥12), Sói Đầu Đàn (≥13)...",
}


@dataclass
class WWRoom:
    chat_id: int
    host_id: int
    host_name: str
    players: list = field(default_factory=list)        # [(user_id, name)]
    settings: WWSettings = field(default_factory=WWSettings)
    msg_id: Optional[int] = None
    panel: bool = False                                 # đang mở bảng cài đặt?
    created: float = field(default_factory=time.time)


@dataclass
class WWSession:
    chat_id: int
    game: WerewolfGame
    host_id: int
    task: Optional[asyncio.Task] = None
    aborted: bool = False
    ev_night: asyncio.Event = field(default_factory=asyncio.Event)
    ev_witch: asyncio.Event = field(default_factory=asyncio.Event)
    ev_words: asyncio.Event = field(default_factory=asyncio.Event)
    ev_talk: asyncio.Event = field(default_factory=asyncio.Event)
    ev_vote: asyncio.Event = field(default_factory=asyncio.Event)
    ev_hunter: asyncio.Event = field(default_factory=asyncio.Event)
    ev_mayor: asyncio.Event = field(default_factory=asyncio.Event)
    menus: dict = field(default_factory=dict)           # (idx, kind) -> message_id trong DM
    witch_msg: Optional[int] = None
    pending: dict = field(default_factory=dict)         # user_id -> "letter"
    skip_votes: set = field(default_factory=set)
    vote_live: Optional["Live"] = None
    hunter_wait: Optional[int] = None                   # idx thợ săn đang chọn
    hunter_choice: Optional[int] = None
    hunter_done: bool = False
    mayor_wait: Optional[int] = None                    # idx Thị Trưởng đang phá hoà
    mayor_choice: Optional[int] = None
    succ_wait: Optional[int] = None                     # idx Thị Trưởng đã chết đang chọn người kế nhiệm
    succ_choice: Optional[int] = None
    gun_pick: set = field(default_factory=set)          # idx xạ thủ đang mở bảng chọn mục tiêu
    dm_fail: set = field(default_factory=set)
    night_warned: set = field(default_factory=set)      # user_id đã được nhắc "chat ở DM" trong đêm nay
    stage: str = "idle"                                 # idle | night | words | talk | vote | hunter
    hunter_task: Optional[asyncio.Task] = None


def _spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _BG.add(t)
    t.add_done_callback(_BG.discard)
    return t


def chat_busy(chat_id: int) -> bool:
    return chat_id in WW_LOBBIES or chat_id in WW_GAMES


def is_ww_participant(user_id: int) -> bool:
    return user_id in WW_USER


def abort_chat(chat_id: int) -> bool:
    """Gọi từ /endgame: huỷ phòng chờ / ván Ma Sói trong chat. Trả True nếu có gì đó bị huỷ."""
    had = chat_id in WW_LOBBIES or chat_id in WW_GAMES
    _cleanup(chat_id)
    return had


def _cleanup(chat_id: int) -> None:
    room = WW_LOBBIES.pop(chat_id, None)
    if room:
        for uid, _ in room.players:
            if WW_USER.get(uid) == chat_id:
                WW_USER.pop(uid, None)
    s = WW_GAMES.pop(chat_id, None)
    if s:
        s.aborted = True
        for p in s.game.players:
            if WW_USER.get(p.user_id) == chat_id:
                WW_USER.pop(p.user_id, None)
        for ev in (s.ev_night, s.ev_witch, s.ev_words, s.ev_talk, s.ev_vote, s.ev_hunter, s.ev_mayor):
            ev.set()
        cur = asyncio.current_task()
        if s.task and not s.task.done() and s.task is not cur:
            s.task.cancel()


# ════════════════════════════════════════════════════════════════
# GỬI TIN (MarkdownV2 an toàn, giống hg._send)
# ════════════════════════════════════════════════════════════════

def _clean_name(name: str, taken: set) -> str:
    n = _MD_BAD.sub("", name or "").strip() or "Người chơi"
    n = n[:22]
    base, i = n, 2
    while n.lower() in taken:
        n = f"{base[:19]} {i}"
        i += 1
    taken.add(n.lower())
    return n


async def _send(context, chat_id: int, text: str, kb=None):
    kw = {"reply_markup": kb} if kb else {}
    return await hg._send(context, chat_id, text, **kw)


async def _edit(context, chat_id: int, mid: Optional[int], text: str, kb=None) -> bool:
    if mid is None:
        return False
    for md in (True, False):
        try:
            await context.bot.edit_message_text(
                to_markdown_v2(text) if md else text, chat_id=chat_id, message_id=mid,
                parse_mode=ParseMode.MARKDOWN_V2 if md else None, reply_markup=kb)
            return True
        except TelegramError as e:
            if "not modified" in str(e).lower():
                return True
            if not md:
                return False
    return False


async def _strip_kb(context, chat_id: int, mid: Optional[int]) -> None:
    if mid is None:
        return
    try:
        await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=mid, reply_markup=None)
    except TelegramError:
        pass


class Live:
    """1 tin nhắn cập nhật liên tục nhưng có giãn cách (tránh flood-limit của Telegram)."""

    def __init__(self, context, chat_id: int, mid: Optional[int], fn, min_gap: float = 1.6):
        self.context, self.chat_id, self.mid, self.fn, self.gap = context, chat_id, mid, fn, min_gap
        self.last = 0.0
        self.task: Optional[asyncio.Task] = None

    def refresh(self) -> None:
        if self.task and not self.task.done():
            return
        self.task = _spawn(self._go())

    async def _go(self) -> None:
        wait = self.gap - (time.time() - self.last)
        if wait > 0:
            await asyncio.sleep(wait)
        self.last = time.time()
        text, kb = self.fn()
        await _edit(self.context, self.chat_id, self.mid, text, kb)

    async def final(self, text: str) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
        await _edit(self.context, self.chat_id, self.mid, text, None)


async def _dm(context, s: WWSession, idx: int, text: str, kb=None):
    """DM cho người chơi theo idx. Lỗi (chặn bot...) -> báo nhóm 1 lần."""
    p = s.game.players[idx]
    m = await _send(context, p.user_id, text, kb)
    if m is None and p.user_id not in s.dm_fail:
        s.dm_fail.add(p.user_id)
        await _send(context, s.chat_id, f"⚠️ Bot không nhắn riêng được cho *{p.name}* — bạn ấy sẽ bị lỡ thông tin/hành động!")
    return m


async def _flush(context, s: WWSession) -> None:
    """Đẩy hàng đợi tin của engine ra: công khai -> nhóm, riêng -> DM."""
    pub, dms = s.game.pop_out()
    for t in pub:
        await _send(context, s.chat_id, t)
        await asyncio.sleep(0.3)
    for idx, t in dms:
        await _dm(context, s, idx, t)
        await asyncio.sleep(0.06)


def _T(s: WWSession, base: int) -> int:
    return max(12, int(base * s.game.settings.scale()))


async def _wait(s: WWSession, ev: asyncio.Event, secs: int, context=None, warn: str = "") -> bool:
    """Chờ ev tối đa secs giây. True nếu ev được set (xong sớm). Có nhắc còn 15s."""
    ev.clear()
    try:
        if warn and context is not None and secs > 35:
            try:
                await asyncio.wait_for(ev.wait(), secs - 15)
                return True
            except asyncio.TimeoutError:
                await _send(context, s.chat_id, warn)
                await asyncio.wait_for(ev.wait(), 15)
                return True
        await asyncio.wait_for(ev.wait(), secs)
        return True
    except asyncio.TimeoutError:
        return False


# ════════════════════════════════════════════════════════════════
# LỆNH & MENU
# ════════════════════════════════════════════════════════════════

RULES_TEXT = (
    "🐺 *MA SÓI — bản Drama*\n\n"
    "• Đêm: mỗi vai nhận *nút bấm riêng trong DM* để hành động.\n"
    "• Sáng: *VÒNG MÔ TẢ — TÌM KẺ GIẢ MẠO* 🕵️ — bot nhắn riêng mỗi người 1 từ khoá; ai cũng mô tả từ của mình "
    "bằng 1 câu (cấm nói thẳng), *không giới hạn thời gian nhưng ai cũng phải nộp*; nộp xong là lời hiện ngay ở nhóm. Số đông cầm *từ A*, kẻ giả mạo cầm *từ B* na ná — mô tả \"lệch\" là bị nghi!\n"
    "• Sau đó thảo luận, thư nặc danh ✉️, rồi bỏ phiếu treo cổ.\n"
    "• 🩸 *Đường Cùng:* Sói còn 1 mình trong bầy (Sói Đầu Đàn không tính) mà làng còn ≥3 người khác → nanh xuyên Bảo Vệ, cầm từ khoá của Dân Làng, mỗi đêm dò 1 người.\n"
    "• 🔪 *Sát Nhân Hàng Loạt* (≥12 người) giết cả 3 phe, bỏ qua mọi phòng thủ, thắng khi chỉ còn 1 mình. "
    "🕵️ *Thám Tử* (≥13 người) điều tra nạn nhân bị giết. 👴 *Già Làng* là vai công khai — chết đi vẫn báo mộng được 1 đêm.\n"
    "• 👑 *Sói Đầu Đàn* (≥13 người, công khai): không ai nhắm được khi đàn còn sói, mất đàn thì sáng hôm sau bị treo cổ ngay, không được vote, hồi sinh 1 sói, thanh trừng sói (cả Sát Nhân bị nguyền), chat tự do ban đêm.\n"
    "• ⚔️ Khi cả *phe Dân Làng* chỉ còn 1 người và đó là Dân Làng thường → được *phong Hiệp Sĩ*: kiếm chém ban ngày, giáp đỡ 1 nhát cắn, miễn nhiễm treo cổ.\n"
    "• 🔪 Sát Nhân: đêm này lập kế hoạch, đêm sau mới ra tay; mục tiêu chết trước thì kế hoạch đổ bể. Chỉ còn 1 đối thủ (người yêu không tính) thì giết thẳng. Biết thân phận mọi người, nhận từ khoá Dân Làng, mạo danh sói bằng tên tự chọn.\n"
    "• 🏹 Thợ Săn chỉ bắn khi bị Sói/Sát Nhân giết · 🧿 Pháp Sư hồi sinh 1 người/ván · 🎖️ Thị Trưởng: phá hoà, khiên chức vụ, kế nhiệm.\n"
    "• Cần /start bot ở chat riêng trước để nhận vai."
)


def _lobby_text(room: WWRoom) -> str:
    st = room.settings
    on = lambda b: "✅" if b else "⬜"
    lines = [
        f"🐺 *PHÒNG MA SÓI* — chủ phòng: {room.host_name}", "",
        f"👥 *Người chơi ({len(room.players)}/{MAX_PLAYERS}, cần ≥ {MIN_PLAYERS}):*",
    ]
    for i, (_, n) in enumerate(room.players, 1):
        lines.append(f"{i}. {n}")
    lines += [
        "",
        f"🎭 Chế độ: *{MODES[st.mode]}* — _{MODE_BLURB[st.mode]}_",
        f"⏱️ Tốc độ: *{SPEEDS[st.speed]}* · 🏷️ Chủ đề từ: *{TOPICS.get(st.topic, st.topic)}*",
        f"{on(st.word_round)} Vòng Mô Tả (Tìm Kẻ Giả Mạo) · {on(st.letters)} Thư nặc danh · "
        f"{on(st.reveal_roles)} Lộ vai khi chết · {on(st.secret_vote)} Phiếu kín · {on(st.silence)} Xoá tin người chết",
        "",
    ]
    return "\n".join(lines)


def _lobby_kb(room: WWRoom) -> Kb:
    c = room.chat_id
    if room.panel:
        st = room.settings

        def tog(label, key, val):
            return Btn(f"{'✅' if val else '⬜'} {label}", callback_data=f"ww:opt:{c}:{key}")
        return Kb([
            [Btn(f"🎭 Chế độ: {MODES[st.mode]}", callback_data=f"ww:opt:{c}:mode")],
            [Btn(f"⏱️ Tốc độ: {SPEEDS[st.speed]}", callback_data=f"ww:opt:{c}:speed")],
            [Btn(f"🏷️ Chủ đề: {TOPICS.get(st.topic, st.topic)}", callback_data=f"ww:opt:{c}:topic")],
            [tog("Vòng Mô Tả", "word_round", st.word_round), tog("Thư nặc danh", "letters", st.letters)],
            [tog("Lộ vai khi chết", "reveal_roles", st.reveal_roles), tog("Phiếu kín", "secret_vote", st.secret_vote)],
            [tog("Xoá tin người chết", "silence", st.silence)],
            [Btn("⬅️ Xong", callback_data=f"ww:back:{c}")],
        ])
    return Kb([
        [Btn("🙋 Tham gia", callback_data=f"ww:join:{c}"), Btn("🚪 Rời", callback_data=f"ww:leave:{c}")],
        [Btn("⚙️ Cài đặt", callback_data=f"ww:cfg:{c}"), Btn("📖 Luật", callback_data=f"ww:rules:{c}")],
        [Btn("▶️ Bắt đầu", callback_data=f"ww:start:{c}"), Btn("❌ Huỷ", callback_data=f"ww:cancel:{c}")],
    ])


async def cmd_masoi(update: Update, context) -> None:
    user, chat = update.effective_user, update.effective_chat
    if not is_owner(user.id):
        return
    if chat.type == "private":
        await safe_reply(update, "🐺 *Ma Sói cần chơi trong nhóm* — hãy gõ /masoi trong nhóm nhé~")
        return
    if chat_busy(chat.id) or chat.id in hg.LOBBIES or chat.id in hg.GAMES:
        await safe_reply(update, "⚠️ *Chat này đang có ván/phòng chờ rồi!* Gõ /endgame để huỷ trước.")
        return
    await _open_room(update, context, chat.id, user, via_menu=None)


async def _open_room(update, context, chat_id: int, user, via_menu) -> None:
    if user.id in WW_USER or hg.is_game_participant(user.id):
        msg = "Bạn đang ở trong một ván khác rồi ạ~"
        if via_menu:
            await via_menu.answer(msg, show_alert=True)
        else:
            await safe_reply(update, f"⚠️ {msg}")
        return
    taken: set = set()
    room = WWRoom(chat_id=chat_id, host_id=user.id, host_name=_clean_name(user.full_name, set()))
    ok = await hg._dm_ok(context, user.id, "✅ Bạn đã mở phòng *Ma Sói*! Vai trò và nút hành động sẽ gửi cho bạn ở đây.")
    if not ok:
        note = "Hãy mở chat riêng với bot, bấm /start rồi mở lại phòng nhé."
        if via_menu:
            await via_menu.answer("❌ Bot chưa nhắn riêng được cho bạn! " + note, show_alert=True)
        else:
            await safe_reply(update, "❌ *Bot chưa nhắn riêng được cho bạn.* " + note)
        return
    room.players.append((user.id, _clean_name(user.full_name, taken)))
    room.host_name = room.players[0][1]
    WW_LOBBIES[chat_id] = room
    WW_USER[user.id] = chat_id
    if via_menu:
        await via_menu.answer()
        try:
            await via_menu.message.edit_text(to_markdown_v2(_lobby_text(room)), parse_mode=ParseMode.MARKDOWN_V2,
                                             reply_markup=_lobby_kb(room))
            room.msg_id = via_menu.message.message_id
        except TelegramError:
            m = await _send(context, chat_id, _lobby_text(room), _lobby_kb(room))
            room.msg_id = m.message_id if m else None
    else:
        m = await _send(context, chat_id, _lobby_text(room), _lobby_kb(room))
        room.msg_id = m.message_id if m else None


async def _refresh_lobby(context, room: WWRoom) -> None:
    await _edit(context, room.chat_id, room.msg_id, _lobby_text(room), _lobby_kb(room))


# ════════════════════════════════════════════════════════════════
# CALLBACK ROUTER — điểm vào duy nhất từ bot.py cho mọi "ww:..."
# ════════════════════════════════════════════════════════════════

async def handle_ww_callback(update: Update, context) -> None:
    q = update.callback_query
    parts = (q.data or "").split(":")
    try:
        act = parts[1]
        if act == "new":
            await _cb_new(update, context)
            return
        chat_id = int(parts[2])
        rest = parts[3:]
        if act in ("join", "leave", "cfg", "opt", "back", "start", "cancel", "rules"):
            await _cb_lobby(update, context, act, chat_id, rest)
        else:
            s = WW_GAMES.get(chat_id)
            if not s or s.aborted:
                await q.answer("Ván này đã kết thúc rồi ạ~", show_alert=True)
                return
            if act == "n":
                await _cb_night(update, context, s, rest)
            elif act == "w":
                await _cb_witch(update, context, s, rest)
            elif act == "v":
                await _cb_vote(update, context, s, rest)
            elif act == "skip":
                await _cb_skip(update, context, s)
            elif act == "p":
                await _cb_power(update, context, s, rest)
            elif act == "h":
                await _cb_hunter(update, context, s, rest)
            elif act == "m":
                await _cb_mayor(update, context, s, rest)
            elif act == "s":
                await _cb_successor(update, context, s, rest)
            elif act == "spy":
                await _cb_spy(update, context, s, rest)
            elif act == "l":
                await _cb_letter(update, context, s)
            elif act == "role":
                await _cb_role(update, context, s)
            elif act == "alive":
                await _cb_alive(update, context, s)
            else:
                await q.answer()
    except Exception as e:  # noqa: BLE001
        logger.exception("[ww] callback lỗi")
        try:
            await q.answer(f"❌ Lỗi: {e}", show_alert=True)
        except Exception:
            pass


async def _cb_new(update, context) -> None:
    """Nút 🐺 Ma Sói trong menu /game."""
    q = update.callback_query
    user, chat_id = q.from_user, q.message.chat_id
    if not is_owner(user.id):
        await q.answer("Chỉ chủ nhân bot mới mở ván được ạ~", show_alert=True)
        return
    if q.message.chat.type == "private":
        await q.answer("Ma Sói cần chơi trong nhóm ạ~", show_alert=True)
        return
    if chat_busy(chat_id) or chat_id in hg.LOBBIES or chat_id in hg.GAMES:
        await q.answer("Chat này đang có ván/phòng khác rồi!", show_alert=True)
        return
    await _open_room(update, context, chat_id, user, via_menu=q)


async def _cb_lobby(update, context, act: str, chat_id: int, rest: list) -> None:
    q = update.callback_query
    user = q.from_user
    room = WW_LOBBIES.get(chat_id)
    if not room:
        await q.answer("Phòng đã đóng hoặc bắt đầu rồi ạ~", show_alert=True)
        return
    is_host = user.id == room.host_id

    if act == "rules":
        await q.answer()
        await _send(context, chat_id, RULES_TEXT)
        return

    if act == "join":
        if any(u == user.id for u, _ in room.players):
            await q.answer("Bạn đã ở trong phòng rồi ạ~")
            return
        if len(room.players) >= MAX_PLAYERS:
            await q.answer("Phòng đã đầy!", show_alert=True)
            return
        if user.id in WW_USER or hg.is_game_participant(user.id):
            await q.answer("Bạn đang ở trong một ván khác rồi!", show_alert=True)
            return
        ok = await hg._dm_ok(context, user.id,
                             "✅ Bạn đã vào phòng *Ma Sói*! Vai trò, từ khoá và nút hành động sẽ gửi cho bạn ở đây — "
                             "đừng chặn bot nhé.")
        if not ok:
            uname = str(CONFIG.get("bot_username", "")).lstrip("@")
            hint = f" (t.me/{uname})" if uname else ""
            await q.answer("❌ Bot chưa nhắn riêng được cho bạn! Mở chat riêng với bot" + hint +
                           ", bấm /start rồi quay lại bấm Tham gia nhé.", show_alert=True)
            return
        taken = {n.lower() for _, n in room.players}
        room.players.append((user.id, _clean_name(user.full_name, taken)))
        WW_USER[user.id] = chat_id
        await q.answer("✅ Đã vào phòng!")
        await _refresh_lobby(context, room)
        return

    if act == "leave":
        if user.id == room.host_id:
            await q.answer("Chủ phòng không rời được — hãy bấm Huỷ nếu muốn đóng phòng.", show_alert=True)
            return
        before = len(room.players)
        room.players = [(u, n) for u, n in room.players if u != user.id]
        if len(room.players) == before:
            await q.answer("Bạn chưa ở trong phòng.")
            return
        WW_USER.pop(user.id, None)
        await q.answer("Đã rời phòng.")
        await _refresh_lobby(context, room)
        return

    # các nút còn lại: chỉ chủ phòng
    if not is_host:
        await q.answer("Chỉ chủ phòng mới bấm được ạ~", show_alert=True)
        return

    if act == "cancel":
        await q.answer("Đã huỷ phòng.")
        _cleanup(chat_id)
        await _edit(context, chat_id, room.msg_id, _lobby_text(room) + "\n\n🛑 *Phòng đã bị huỷ.*")
        return

    if act == "cfg":
        room.panel = True
        await q.answer()
        await _refresh_lobby(context, room)
        return
    if act == "back":
        room.panel = False
        await q.answer()
        await _refresh_lobby(context, room)
        return

    if act == "opt":
        key = rest[0] if rest else ""
        st = room.settings
        if key == "mode":
            ks = list(MODES)
            st.mode = ks[(ks.index(st.mode) + 1) % len(ks)]
        elif key == "speed":
            ks = list(SPEEDS)
            st.speed = ks[(ks.index(st.speed) + 1) % len(ks)]
        elif key == "topic":
            ks = list(TOPICS)
            st.topic = ks[(ks.index(st.topic) + 1) % len(ks)]
        elif key in ("word_round", "letters", "reveal_roles", "secret_vote", "silence"):
            setattr(st, key, not getattr(st, key))
        await q.answer()
        await _refresh_lobby(context, room)
        return

    if act == "start":
        if len(room.players) < MIN_PLAYERS:
            await q.answer(f"Cần ít nhất {MIN_PLAYERS} người chơi (hiện {len(room.players)})!", show_alert=True)
            return
        await q.answer("🚀 Bắt đầu!")
        WW_LOBBIES.pop(chat_id, None)
        await _edit(context, chat_id, room.msg_id, _lobby_text(room) + "\n\n🚀 *Ván đã bắt đầu!*")
        await _start_game(context, room)
        return


# ════════════════════════════════════════════════════════════════
# KHỞI ĐỘNG VÁN & VÒNG LẶP CHÍNH
# ════════════════════════════════════════════════════════════════

async def _start_game(context, room: WWRoom) -> None:
    g = WerewolfGame(list(room.players), room.settings)
    g.assign_roles()
    s = WWSession(chat_id=room.chat_id, game=g, host_id=room.host_id)
    WW_GAMES[room.chat_id] = s
    s.task = _spawn(_run_game(context, s))


async def _run_game(context, s: WWSession) -> None:
    g = s.game
    try:
        await _intro(context, s)
        while not g.over and not s.aborted:      # ván chỉ kết thúc khi có phe thắng hoặc chết hết (check_win)
            await _night(context, s)
            if await _check_end(context, s):
                break
            await _day(context, s)
            if await _check_end(context, s):
                break
        if not s.aborted:
            await _finale(context, s)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("[ww] ván bị lỗi")
        await _send(context, s.chat_id, "⚠️ *Ván Ma Sói gặp lỗi nội bộ và phải dừng.* Xin lỗi cả làng~ (chủ phòng mở lại bằng /masoi)")
    finally:
        if WW_GAMES.get(s.chat_id) is s:
            _cleanup(s.chat_id)


def _game_kb(s: WWSession, extra: Optional[list] = None) -> Kb:
    c = s.chat_id
    rows = list(extra or [])
    rows.append([Btn("🃏 Vai của tôi", callback_data=f"ww:role:{c}"), Btn("👥 Còn sống", callback_data=f"ww:alive:{c}")])
    return Kb(rows)


async def _intro(context, s: WWSession) -> None:
    g = s.game
    st = g.settings
    lines = [
        f"🐺 *VÁN MA SÓI BẮT ĐẦU!* — {len(g.players)} người chơi", "",
        f"🎭 *Thành phần vai:* {g.composition()}", "",
        f"⚙️ Chế độ *{MODES[st.mode]}* · {SPEEDS[st.speed]}"
        + (f" · Chủ đề từ: {TOPICS.get(st.topic, st.topic)}" if st.word_round else ""),
        "",
        "📩 Bot đã *nhắn riêng vai trò* cho từng người — vào DM xem ngay nhé!",
    ]
    for pub in g.players:
        if pub.role.public:
            lines.append(f"{pub.role.emoji} *{pub.role.name}* là *{pub.name}* — vai CÔNG KHAI, cả làng đều biết!")
    if st.word_round:
        lines.append("🕵️ Mỗi sáng có *VÒNG MÔ TẢ*: bot nhắn từ khoá, bạn mô tả trong DM. Ai lệch số đông là đáng ngờ!")
    lines.append("🌙 *Ban đêm cấm chat ở nhóm* — bot xoá mọi tin của người chơi. Sói (và ai có kênh chat đêm, VD cặp đôi) "
                 "hãy nhắn cho bot ở *DM*, bot sẽ chuyển kín cho đồng đội. _Bot cần quyền Xoá tin nhắn!_")
    if st.silence:
        lines.append("🤫 Người đã chết cũng *không được nhắn* trong nhóm (bot sẽ xoá tin).")
    lines.append("")
    lines.append("👥 *Người chơi:* " + ", ".join(p.name for p in g.players))
    await _send(context, s.chat_id, "\n".join(lines), _game_kb(s))
    try:
        me = await context.bot.get_chat_member(s.chat_id, context.bot.id)
        can_del = getattr(me, "can_delete_messages", False) or me.status == "creator"
    except TelegramError:
        can_del = True                      # không kiểm tra được thì thôi, đừng báo nhầm
    if not can_del:
        await _send(context, s.chat_id,
                    "⚠️ *Bot chưa có quyền Xoá tin nhắn!* Ban đêm tin của Sói sẽ KHÔNG bị xoá và lộ danh tính. "
                    "Hãy cấp quyền admin (Xoá tin nhắn) cho bot, hoặc cả làng tự giữ im lặng ban đêm.")
    for p in g.players:
        card = g.role_card(p.idx)
        card += "\n\n_Mỗi đêm bot sẽ nhắn nút bấm cho bạn ở đây (nếu vai của bạn có hành động đêm)._"
        await _dm(context, s, p.idx, card)
        await asyncio.sleep(0.06)
    await asyncio.sleep(6)


async def _check_end(context, s: WWSession) -> bool:
    g = s.game
    g.check_knight()                      # Dân Làng thường cuối cùng được phong Hiệp Sĩ (thông báo qua _flush)
    await _flush(context, s)
    return bool(g.check_win()) or g.over


def _wake_all(s: WWSession) -> None:
    for ev in (s.ev_night, s.ev_witch, s.ev_words, s.ev_talk, s.ev_vote, s.ev_hunter, s.ev_mayor):
        ev.set()


async def _sync_hunters(s: WWSession) -> None:
    t = s.hunter_task
    if t and not t.done():
        try:
            await t
        except Exception:  # noqa: BLE001
            pass


# ════════════════════════════════════════════════════════════════
# ĐÊM
# ════════════════════════════════════════════════════════════════

_NIGHT_TITLE = {
    "wolf": "🐺 *Bầy sói thức giấc!* Chọn nạn nhân đêm nay. Cả bầy chọn *CÙNG 1 người* thì đêm kết thúc sớm — "
            "cứ nhắn tin ở đây để bàn bạc bí mật với đồng bọn.",
    "curse": "🧬 *Lời Nguyền (chỉ 1 lần cả ván):* nạn nhân của bầy đêm nay sẽ bị *biến thành sói* thay vì chết. Dùng không?",
    "scout": "🔭 *Trinh sát:* chọn 1 người để dò xem họ có chức năng đặc biệt không (cả bầy sẽ biết kết quả).",
    "guard": "🛡️ *Bảo Vệ:* chọn 1 người để che chở khỏi Sói đêm nay (không chặn được Sát Nhân). Không được chọn lại người đêm trước.",
    "seer": "🔮 *Tiên Tri:* chọn 1 người để soi xem có phải Ma Sói không.",
    "investigate": "🕵️ *Thám Tử:* chọn 1 nạn nhân bị giết đêm trước để điều tra hung thủ (có thể ra manh mối, cũng có thể bị Sát Nhân đánh lừa).",
    "cupid": "💘 *Thần Tình Yêu:* chọn *2 người* để ghép đôi (được chọn cả chính mình). Ai chết trước, người kia chết theo!",
    "serial": "📝 *Sát Nhân — LẬP KẾ HOẠCH:* chọn mục tiêu, ĐÊM MAI bạn mới ra tay (nhát dao bỏ qua MỌI phòng thủ). "
              "Nếu hắn chết trước đó, kế hoạch đổ bể và phải lập lại từ đầu.",
    "frame": "🔪🎭 *Đêm RA TAY — làm giả chứng cứ:* kế hoạch của bạn sẽ hoàn tất đêm nay. Chọn 1 người để đổ tội nếu Thám Tử điều tra (không bắt buộc).",
    "revive": "🧿 *Pháp Sư:* hồi sinh 1 người đã chết (chỉ 1 LẦN cả ván). Bỏ qua thì giữ phép cho đêm sau.",
    "purge": "🗡️ *Thanh trừng:* chọn 1 sói trong đàn để giết đêm nay (hạn chế số lần cả ván). Bỏ qua nếu không cần.",
    "packrevive": "✨ *Hồi sinh sói (1 lần cả ván):* chọn 1 con sói đã chết để gọi trở về lúc trời sáng.",
    "elderlink": "👴 *Già Làng:* chọn Thị Trưởng hoặc Phù Thuỷ để thì thầm riêng đêm nay (mỗi đêm 1 người).",
    "peek": "👀 *Cô Bé Tò Mò:* hé mắt nhìn trộm bầy sói? (Rủi ro: có thể bị sói bắt gặp và lộ danh tính!)",
}


def _sel_names(s: WWSession, actor: int, kind: str) -> list:
    n = s.game.night
    key = (actor, kind)
    return list(n.done.get(key) or n.partial.get(key) or [])


def _night_text(s: WWSession, actor: int, kind: str) -> str:
    g = s.game
    n = g.night
    txt = f"🌙 *ĐÊM {g.night_no}*\n{_NIGHT_TITLE[kind]}\n"
    info = KIND_INFO[kind]
    key = (actor, kind)
    if info["need"] == 0:
        if key in n.done:
            txt += "\n✅ Bạn chọn: *" + ("CÓ" if n.done[key] and n.done[key][0] else "KHÔNG") + "*"
        return txt
    sel = _sel_names(s, actor, kind)
    if key in n.done and not n.done[key]:
        txt += "\n⏭ Bạn chọn: *bỏ qua*"
    elif sel:
        txt += "\n✅ Đang chọn: *" + " & ".join(g.nm(i) for i in sel) + "*"
        if info["locked"] and key in n.done:
            txt += " 🔒"
    else:
        txt += "\n_Chưa chọn._"
    if kind == "wolf":
        rows = []
        for w in g.wolf_voters():
            v = n.wolf_votes.get(w.idx)
            rows.append(f"• {w.name} → " + ("_chưa chọn_" if v is None else ("_tha đêm nay_" if v == NONE else f"*{g.nm(v)}*")))
        txt += "\n\n🗳️ *Bầy đang chọn:*\n" + "\n".join(rows)
    return txt


def _night_kb(s: WWSession, actor: int, kind: str) -> Optional[Kb]:
    g = s.game
    c = s.chat_id
    info = KIND_INFO[kind]
    key = (actor, kind)
    if info["locked"] and key in g.night.done:
        return None
    if info["need"] == 0:
        return Kb([[Btn("✅ Có", callback_data=f"ww:n:{c}:{kind}:1"), Btn("❌ Không", callback_data=f"ww:n:{c}:{kind}:0")]])
    sel = set(_sel_names(s, actor, kind))
    rows, row = [], []
    for i in g.targets_for(actor, kind):
        row.append(Btn(("✅ " if i in sel else "") + g.players[i].name[:16], callback_data=f"ww:n:{c}:{kind}:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([Btn("⏭ Bỏ qua", callback_data=f"ww:n:{c}:{kind}:{NONE}")])
    return Kb(rows)


async def _send_night_menu(context, s: WWSession, actor: int, kind: str) -> None:
    m = await _dm(context, s, actor, _night_text(s, actor, kind), _night_kb(s, actor, kind))
    if m is not None:
        s.menus[(actor, kind)] = m.message_id


async def _close_menus(context, s: WWSession, note: str) -> None:
    for (actor, kind), mid in list(s.menus.items()):
        uid = s.game.players[actor].user_id
        try:
            await _edit(context, uid, mid, _night_text(s, actor, kind) + f"\n\n{note}", None)
        except Exception:  # noqa: BLE001
            pass
    s.menus.clear()


async def _night(context, s: WWSession) -> None:
    g = s.game
    g.begin_night()
    s.stage = "night"
    s.menus.clear()
    s.ev_night.clear()
    await _send(context, s.chat_id,
                f"🌙 *ĐÊM {g.night_no}* — Làng chìm vào giấc ngủ… ({len(g.alive_idx())} người còn sống)\n"
                "_Ai có vai hoạt động ban đêm hãy kiểm tra DM với bot._", _game_kb(s))
    s.night_warned.clear()
    for actor, kind in g.night_actors():
        await _send_night_menu(context, s, actor, kind)
        await asyncio.sleep(0.05)
    await _open_night_chat(context, s)
    await _dream_prompt(context, s)
    await _flush(context, s)

    secs = _T(s, T_NIGHT + (20 if g.night_no == 1 else 0))
    if not g.stage_a_done():
        await _wait(s, s.ev_night, secs)
    else:
        await asyncio.sleep(3)
    if s.aborted:
        return
    await asyncio.sleep(1.5)                      # tránh lộ ai hành động nhanh/chậm
    await _close_menus(context, s, "⏰ _Hết đêm._")
    g.close_stage_a()

    g.phase = "night_b"
    w = g.witch_active()
    if w:
        await _witch_stage(context, s, w)
    died = g.resolve_night()
    s.stage = "idle"
    await _flush(context, s)
    await _dawn(context, s, died)


async def _open_night_chat(context, s: WWSession) -> None:
    """Đầu đêm: nhắn cho mọi người có kênh chat đêm biết cách chat kín qua DM bot."""
    g = s.game
    for p in g.alive_players():
        ch = g.night_channels(p.idx)
        if not ch:
            continue
        lines = ["💬 *KÊNH CHAT ĐÊM* — chat ở nhóm sẽ bị xoá, hãy gõ thẳng vào đây:"]
        if "wolf" in ch:
            mates = [g.nm(i) for i in ch["wolf"]]
            lines.append("🐺 Bầy sói: " + (", ".join(mates) if mates else "_bạn đơn độc, không còn ai nghe_"))
        kb = None
        if "spy" in ch:
            lines.append("🔪 *Nghe lén bầy sói:* mọi tin của bầy sẽ được chuyển tới bạn. Bạn cũng có thể nhắn để MẠO DANH sói "
                         "— nhưng càng nói nhiều càng dễ bị phát giác (bị lộ là cả bầy biết bạn là Sát Nhân!).")
            lines.append("🎭 *Chọn tên để đội:* bấm nút bên dưới, hoặc mở đầu tin bằng `@Tên:` "
                         "(VD: `@" + g.nm(ch["spy"][0]) + ": tối nay cắn thằng đó đi`).")
            c = s.chat_id
            rows = [[Btn("🎭 " + g.nm(i)[:20], callback_data=f"ww:spy:{c}:{i}")] for i in ch["spy"]]
            rows.append([Btn("🎲 Ngẫu nhiên", callback_data=f"ww:spy:{c}:{NONE}")])
            kb = Kb(rows)
        if "lover" in ch and ch["lover"]:
            lines.append(f"💞 Người yêu: {g.nm(ch['lover'][0])}")
            if "wolf" in ch or "spy" in ch:
                lines.append("_Tin mặc định gửi cho BẦY SÓI. Muốn thì thầm cho người yêu, mở đầu bằng_ `y:` _(VD:_ `y: yêu anh`_)._")
        if p.role.key == "alphawolf":
            lines.append("👑 Là Sói Đầu Đàn, bạn được chat TỰ DO ở nhóm chính ban đêm — tin của bạn KHÔNG bị xoá.")
        lines.append("_Bot chỉ chuyển tin chữ. Tin ở đây không ai ngoài đồng đội thấy được._")
        await _dm(context, s, p.idx, "\n".join(lines), kb)
        await asyncio.sleep(0.05)


async def _dream_prompt(context, s: WWSession) -> None:
    """Già Làng đã chết: đêm hôm sau được báo mộng vào nhóm chính."""
    g = s.game
    for p in g.players:
        if g.dream_ready(p.idx):
            left = p.role.dream_left
            await _dm(context, s, p.idx,
                      f"💭 *ĐÊM BÁO MỘNG* — đêm nay là cơ hội duy nhất của bạn! Nhắn tin ở đây, bot sẽ đăng vào nhóm chính "
                      f"dưới dạng _giấc mộng của Già Làng_ (còn {left} tin, tối đa 140 ký tự/tin). Bạn biết Sói và Sát Nhân là ai — "
                      f"hãy dẫn dắt làng thật khéo (và đừng để lộ quá lộ liễu kẻo bị Sói trả thù).")


async def _night_relay(context, s: WWSession, msg, p, text: str) -> bool:
    """Chuyển tin DM ban đêm của người có kênh chat đêm tới đồng đội. True = đã xử lý."""
    g = s.game
    ch = g.night_channels(p.idx)
    if not ch:
        return False
    chan = next(c for c in ("wolf", "spy", "elder", "lover") if c in ch)
    if chan != "lover" and "lover" in ch and re.match(r"(?i)^y\s*:", text):
        chan = "lover"
        text = re.sub(r"(?i)^y\s*:\s*", "", text, count=1)
        if not text:
            return True
    targets = ch[chan]
    if not targets:
        await _send(context, p.user_id, "🌑 _Không còn ai trong kênh này để nghe bạn._")
        return True
    if chan == "wolf":
        head = f"🐺 *{p.name}:* "
    elif chan == "lover":
        head = f"💞 *{p.name}* thì thầm: "
    elif chan == "elder":
        head = "👴 *Già Làng:* " if p.role.key == "elder" else f"🗣️ *{p.name}:* "
    else:                                             # spy: Sát Nhân mạo danh sói, tự chọn tên con sói để đội
        alias = None
        m = re.match(r"(?s)^@\s*([^:\n]{1,40}?)\s*:\s*(.*)$", text)
        if m:
            alias = g.spy_alias_lookup(m.group(1))
            if alias is None:
                names = ", ".join(g.nm(i) for i in targets)
                await _send(context, p.user_id, f"❌ Không tìm thấy con sói tên đó. Trong bầy có: {names}")
                return True
            text = m.group(2).strip()
            if not text:
                okk, note = g.set_spy_alias(p.idx, alias)
                await _send(context, p.user_id, note)
                return True
        ok, shown, detected = g.spy_speak(p.idx, alias)
        if not ok:
            await _send(context, p.user_id, f"🔪 _{shown}_")
            return True
        head = f"🚨🐺 *{p.name}* (KẺ MẠO DANH!): " if detected else f"🐺 *{shown}:* "
    for i in targets:
        await _dm(context, s, i, head + text)
    if chan == "wolf":                                # Sát Nhân nghe lén bầy sói
        for e in g.eavesdroppers():
            await _dm(context, s, e, f"🐺👂 *{p.name}:* {text}")
    await _flush(context, s)                          # thông báo phát giác (nếu có)
    try:
        await msg.set_reaction("👌")
    except TelegramError:
        await _send(context, p.user_id, {"wolf": "🐺 _Đã chuyển cho bầy._", "lover": "💞 _Đã thì thầm cho người yêu._"}.get(chan, "🌙 _Đã chuyển._"))
    return True


def _witch_view(s: WWSession, w) -> tuple:
    g = s.game
    n = g.night
    c = s.chat_id
    v = n.victim
    lines = [f"🧙‍♀️ *PHÙ THUỶ — ĐÊM {g.night_no}*", ""]
    lines.append(f"🩸 Đêm nay bầy sói cắn *{g.nm(v)}*." if v is not None else "🕊️ Đêm nay bầy sói *không cắn ai*.")
    lines.append(f"💊 Bình cứu: {'còn' if w.role.heal else 'đã dùng'} · ☠️ Bình độc: {'còn' if w.role.poison else 'đã dùng'}")
    lines.append("")
    if n.witch_heal:
        lines.append("✅ Bạn định *CỨU* nạn nhân.")
    if n.witch_poison is not None:
        lines.append(f"✅ Bạn định *ĐẦU ĐỘC* {g.nm(n.witch_poison)}.")
    if not n.witch_heal and n.witch_poison is None:
        lines.append("_Chưa chọn gì — bạn có thể làm ngơ._")
    lines.append("Bấm ✅ Xong khi quyết định.")
    rows = []
    if w.role.heal and v is not None:
        rows.append([Btn(("✅ " if n.witch_heal else "💊 ") + f"Cứu {g.nm(v)[:14]}", callback_data=f"ww:w:{c}:heal")])
    if w.role.poison:
        row = []
        for i in g.attackable_idx():
            if i == w.idx:
                continue
            row.append(Btn(("☠️✅ " if n.witch_poison == i else "☠️ ") + g.players[i].name[:13], callback_data=f"ww:w:{c}:p:{i}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([Btn("🚫 Không đầu độc", callback_data=f"ww:w:{c}:p:{NONE}")])
    rows.append([Btn("✅ Xong", callback_data=f"ww:w:{c}:ok")])
    return "\n".join(lines), Kb(rows)


async def _witch_stage(context, s: WWSession, w) -> None:
    g = s.game
    s.ev_witch.clear()
    text, kb = _witch_view(s, w)
    m = await _dm(context, s, w.idx, text, kb)
    s.witch_msg = m.message_id if m else None
    await _wait(s, s.ev_witch, _T(s, T_WITCH))
    uid = w.user_id
    text, _ = _witch_view(s, w)
    await _edit(context, uid, s.witch_msg, text.replace("Bấm ✅ Xong khi quyết định.", "🔒 _Đã chốt._"), None)
    s.witch_msg = None


async def _dawn(context, s: WWSession, died: list) -> None:
    g = s.game
    g.begin_day()
    lines = [f"☀️ *NGÀY {g.day_no}* — Trời sáng rồi!", ""]
    for i in g.revived:
        lines.append(f"✨ Một phép màu xảy ra — *{g.nm(i)}* được gọi từ cõi chết trở về!")
    if g.revived:
        lines.append("")
    if died:
        for i in died:
            lines.append(g.death_line(i, g.settings.reveal_roles))
    elif not g.fallen:
        lines.append("🕊️ Đêm qua thật bình yên — *không ai chết*.")
    if g.fallen:
        lines.append("")
        lines.append("👑⚖️ *Sói Đầu Đàn đã mất hết đàn — không thể tồn tại!* Dân Làng treo cổ hắn ngay lúc bình minh:")
        for i in g.fallen:
            lines.append(g.death_line(i, g.settings.reveal_roles))
    lines.append("")
    lines.append(f"👥 Còn sống: *{len(g.alive_idx())}/{len(g.players)}*")
    await _send(context, s.chat_id, "\n".join(lines), _game_kb(s))
    await _hunters(context, s)


async def _cb_night(update, context, s: WWSession, rest: list) -> None:
    q = update.callback_query
    g = s.game
    p = g.find_user(q.from_user.id)
    if not p:
        await q.answer("Bạn không ở trong ván này.", show_alert=True)
        return
    if s.stage != "night":
        await q.answer("Hết giờ hành động rồi ạ~", show_alert=True)
        return
    kind, val = rest[0], int(rest[1])
    res = g.night_pick(p.idx, kind, val)
    if not res.ok:
        await q.answer(res.msg[:190], show_alert=True)
        return
    await q.answer(res.msg[:190])
    await _edit(context, q.message.chat_id, q.message.message_id, _night_text(s, p.idx, kind), _night_kb(s, p.idx, kind))
    await _flush(context, s)
    if g.stage_a_done():
        s.ev_night.set()


async def _cb_spy(update, context, s: WWSession, rest: list) -> None:
    """Sát Nhân chọn tên con sói để đội khi mạo danh trong kênh chat của bầy."""
    q = update.callback_query
    g = s.game
    p = g.find_user(q.from_user.id)
    if not p or s.stage != "night":
        await q.answer("Không hợp lệ hoặc đã hết giờ.", show_alert=True)
        return
    ok, note = g.set_spy_alias(p.idx, int(rest[0]))
    await q.answer(note.replace("*", "")[:190], show_alert=not ok)
    if ok:
        await _send(context, p.user_id, note)


async def _cb_witch(update, context, s: WWSession, rest: list) -> None:
    q = update.callback_query
    g = s.game
    p = g.find_user(q.from_user.id)
    w = g.witch_active()
    if not p or not w or w.idx != p.idx or g.phase != "night_b":
        await q.answer("Không hợp lệ hoặc đã hết giờ.", show_alert=True)
        return
    act = rest[0]
    if act == "ok":
        await q.answer("Đã chốt!")
        s.ev_witch.set()
        return
    if act == "heal":
        res = g.witch_set(p.idx, "heal")
    else:
        res = g.witch_set(p.idx, "poison", int(rest[1]))
    if not res.ok:
        await q.answer(res.msg[:190], show_alert=True)
        return
    await q.answer(res.msg[:190])
    text, kb = _witch_view(s, w)
    await _edit(context, q.message.chat_id, q.message.message_id, text, kb)


# ════════════════════════════════════════════════════════════════
# THỢ SĂN (bắn theo khi chết)
# ════════════════════════════════════════════════════════════════

async def _hunters(context, s: WWSession) -> None:
    g = s.game
    while g.pending_shots and not s.aborted:
        h = g.pending_shots[0]
        targets = [i for i in g.attackable_idx() if i != h]
        if g.over or not targets:
            g.pending_shots.remove(h)
            continue
        c = s.chat_id
        rows, row = [], []
        for i in targets:
            row.append(Btn(g.players[i].name[:16], callback_data=f"ww:h:{c}:{i}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([Btn("🚫 Không bắn ai", callback_data=f"ww:h:{c}:{NONE}")])
        prev = s.stage
        s.stage = "hunter"
        s.hunter_wait, s.hunter_choice = h, None
        s.ev_hunter.clear()
        m = await _dm(context, s, h, f"🏹 *BẠN ĐÃ CHẾT — nhưng còn 1 mũi tên cuối!*\nChọn 1 người để bắn theo ({_T(s, T_HUNTER)}s):", Kb(rows))
        await _send(context, s.chat_id, "🏹 _Một tiếng dây cung căng lên trong bóng tối… Thợ Săn đang ngắm bắn!_")
        await _wait(s, s.ev_hunter, _T(s, T_HUNTER))
        s.hunter_wait = None
        s.stage = prev
        if m is not None:
            await _edit(context, g.players[h].user_id, m.message_id, "🏹 _Mũi tên cuối đã rời cung._", None)
        died = g.hunter_shoot(h, s.hunter_choice)
        if died:
            for i in died:
                await _send(context, s.chat_id, g.death_line(i, g.settings.reveal_roles))
        else:
            await _send(context, s.chat_id, "🏹 _Mũi tên bay lạc vào đêm tối — không ai trúng._")
        await _flush(context, s)
    await _successors(context, s)


def _pick_rows(prefix: str, c: int, idxs: list, g, none_label: str) -> Kb:
    rows, row = [], []
    for i in idxs:
        row.append(Btn(g.players[i].name[:16], callback_data=f"ww:{prefix}:{c}:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([Btn(none_label, callback_data=f"ww:{prefix}:{c}:{NONE}")])
    return Kb(rows)


async def _successors(context, s: WWSession) -> None:
    """Thị Trưởng (đã lộ diện) vừa chết: chọn 1 người thừa hưởng phiếu ×2."""
    g = s.game
    while g.pending_successors and not s.aborted:
        m = g.pending_successors[0]
        cands = [i for i in g.voter_idx() if i != m]
        if g.over or not cands:
            g.pending_successors.remove(m)
            continue
        prev = s.stage
        s.stage = "hunter"
        s.succ_wait, s.succ_choice = m, None
        s.ev_hunter.clear()
        msg = await _dm(context, s, m, f"👑 *BẠN ĐÃ CHẾT — nhưng còn quyền KẾ NHIỆM!*\nChọn 1 người thừa hưởng phiếu ×2 ({_T(s, T_MAYOR)}s):",
                        _pick_rows("s", s.chat_id, cands, g, "🚫 Không chỉ định ai"))
        await _send(context, s.chat_id, "👑 _Thị Trưởng đang thì thầm chọn người kế nhiệm…_")
        await _wait(s, s.ev_hunter, _T(s, T_MAYOR))
        s.succ_wait = None
        s.stage = prev
        if msg is not None:
            await _edit(context, g.players[m].user_id, msg.message_id, "👑 _Đã chốt người kế nhiệm._", None)
        if not g.mayor_successor(m, s.succ_choice):
            await _send(context, s.chat_id, "👑 _Thị Trưởng ra đi mà không chỉ định người kế nhiệm._")
        await _flush(context, s)


async def _cb_successor(update, context, s: WWSession, rest: list) -> None:
    q = update.callback_query
    g = s.game
    p = g.find_user(q.from_user.id)
    if not p or s.succ_wait != p.idx:
        await q.answer("Không phải lúc của bạn.", show_alert=True)
        return
    val = int(rest[0])
    s.succ_choice = None if val == NONE else val
    await q.answer("Đã chọn!")
    s.ev_hunter.set()


async def _mayor_tiebreak(context, s: WWSession, mayor, cands: list) -> Optional[int]:
    """Hoà phiếu: Thị Trưởng đã lộ diện chọn người bị treo. None = không chọn kịp -> bỏ phiếu lại."""
    g = s.game
    prev = s.stage
    s.stage = "mayor"
    s.mayor_wait, s.mayor_choice = mayor.idx, None
    s.ev_mayor.clear()
    msg = await _dm(context, s, mayor.idx, f"🎖️ *HOÀ PHIẾU — bạn là Thị Trưởng!*\nChọn người bị treo cổ ({_T(s, T_MAYOR)}s):",
                    _pick_rows("m", s.chat_id, cands, g, "🚫 Không chọn (bỏ phiếu lại)"))
    await _send(context, s.chat_id, f"🎖️ *Hoà phiếu!* Thị Trưởng *{mayor.name}* đang cân nhắc người bị treo…")
    await _wait(s, s.ev_mayor, _T(s, T_MAYOR))
    s.mayor_wait = None
    s.stage = prev
    pick = s.mayor_choice
    if msg is not None:
        await _edit(context, mayor.user_id, msg.message_id, "🎖️ _Đã chốt quyết định._", None)
    return pick if pick in cands and g.players[pick].alive else None


async def _cb_mayor(update, context, s: WWSession, rest: list) -> None:
    q = update.callback_query
    g = s.game
    p = g.find_user(q.from_user.id)
    if not p or s.mayor_wait != p.idx:
        await q.answer("Không phải lúc của bạn.", show_alert=True)
        return
    val = int(rest[0])
    s.mayor_choice = None if val == NONE else val
    await q.answer("Đã chọn!")
    s.ev_mayor.set()


async def _cb_hunter(update, context, s: WWSession, rest: list) -> None:
    q = update.callback_query
    g = s.game
    p = g.find_user(q.from_user.id)
    if not p or s.hunter_wait != p.idx:
        await q.answer("Không phải lúc của bạn.", show_alert=True)
        return
    val = int(rest[0])
    s.hunter_choice = None if val == NONE else val
    await q.answer("Đã chọn!")
    s.ev_hunter.set()


async def _after_shot(context, s: WWSession) -> None:
    """Chạy nền sau khi Xạ Thủ bắn (callback không được chờ — bot.py xử lý update tuần tự)."""
    try:
        await _hunters(context, s)
        await _flush(context, s)
        if s.game.check_win():
            _wake_all(s)
    except Exception:  # noqa: BLE001
        logger.exception("[ww] _after_shot")


# ════════════════════════════════════════════════════════════════
# NGÀY — VÒNG MÔ TẢ → THẢO LUẬN → BỎ PHIẾU
# ════════════════════════════════════════════════════════════════

async def _day(context, s: WWSession) -> None:
    g = s.game
    s.skip_votes.clear()
    await _power_panels(context, s)
    if g.settings.word_round:
        await _word_round(context, s)
        await _sync_hunters(s)
        if await _check_end(context, s):
            return
    await _talk(context, s)
    await _sync_hunters(s)
    if await _check_end(context, s):
        return
    await _vote_phase(context, s)
    await _sync_hunters(s)


async def _power_panels(context, s: WWSession) -> None:
    """Bảng năng lực ban ngày gửi riêng trong DM (Thị Trưởng / Xạ Thủ / Quan Toà)."""
    g = s.game
    c = s.chat_id
    for p in g.alive_players():
        r = p.role
        if not g.can_use_power(p):
            continue
        rows = []
        if r.key == "mayor" and not r.revealed:
            rows.append([Btn("🎖️ Lộ diện Thị Trưởng (phiếu ×2 · phá hoà · khiên)", callback_data=f"ww:p:{c}:mayor")])
        if r.key == "gunner" and r.bullets > 0:
            rows.append([Btn(f"🔫 Bắn (còn {r.bullets} viên)", callback_data=f"ww:p:{c}:gun")])
        if r.key == "judge" and not r.used:
            rows.append([Btn("⚖️ Huỷ án treo cổ hôm nay", callback_data=f"ww:p:{c}:judge")])
        if r.key == "knight" and r.sword > 0 and r.last_swing != g.day_no:
            rows.append([Btn(f"⚔️ Vung kiếm (còn {r.sword} nhát)", callback_data=f"ww:p:{c}:sword")])
        if rows:
            await _dm(context, s, p.idx, f"☀️ *Ngày {g.day_no}* — năng lực {r.emoji} *{r.name}* của bạn:", Kb(rows))


async def _cb_power(update, context, s: WWSession, rest: list) -> None:
    q = update.callback_query
    g = s.game
    p = g.find_user(q.from_user.id)
    if not p or not p.alive:
        await q.answer("Bạn không dùng được năng lực này.", show_alert=True)
        return
    if g.phase not in ("day", "vote") or s.stage not in ("words", "talk", "vote"):
        await q.answer("Chỉ dùng được vào ban ngày ạ~", show_alert=True)
        return
    act = rest[0]
    c = s.chat_id
    if act == "mayor":
        if g.mayor_reveal(p.idx):
            await q.answer("🎖️ Bạn đã lộ diện!")
            await _strip_kb(context, q.message.chat_id, q.message.message_id)
            await _flush(context, s)
        else:
            await q.answer("Không dùng được.", show_alert=True)
        return
    if act == "judge":
        if s.stage == "vote" or s.stage in ("words", "talk"):
            if g.judge_veto(p.idx):
                await q.answer("⚖️ Đã huỷ án hôm nay!")
                await _strip_kb(context, q.message.chat_id, q.message.message_id)
                await _flush(context, s)
                if s.stage == "vote":
                    s.ev_vote.set()
            else:
                await q.answer("Không dùng được (đã dùng hoặc đã huỷ rồi).", show_alert=True)
        return
    if act == "gun":
        if p.role.key != "gunner" or p.role.bullets <= 0:
            await q.answer("Bạn hết đạn.", show_alert=True)
            return
        rows, row = [], []
        for i in g.attackable_idx():
            if i == p.idx:
                continue
            row.append(Btn(g.players[i].name[:16], callback_data=f"ww:p:{c}:gt:{i}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([Btn("⬅️ Thôi", callback_data=f"ww:p:{c}:gx")])
        await q.answer()
        await _edit(context, q.message.chat_id, q.message.message_id,
                    "🔫 *Bắn ai?* (Bắn là LỘ DIỆN Xạ Thủ trước cả làng!)", Kb(rows))
        return
    if act == "gx":
        await q.answer()
        await _strip_kb(context, q.message.chat_id, q.message.message_id)
        return
    if act == "gt":
        target = int(rest[1])
        died = g.gunner_shoot(p.idx, target)
        if not died:
            await q.answer("Không bắn được.", show_alert=True)
            return
        await q.answer("🔫 PằNG!")
        await _strip_kb(context, q.message.chat_id, q.message.message_id)
        await _flush(context, s)
        for i in died:
            await _send(context, s.chat_id, g.death_line(i, g.settings.reveal_roles))
        if (g.pending_shots or g.pending_successors) and not (s.hunter_task and not s.hunter_task.done()):
            s.hunter_task = _spawn(_after_shot(context, s))
        elif g.check_win():
            _wake_all(s)
        return
    if act == "sword":
        r = p.role
        if r.key != "knight" or r.sword <= 0 or r.last_swing == g.day_no:
            await q.answer("Hôm nay bạn không vung kiếm được nữa.", show_alert=True)
            return
        rows, row = [], []
        for i in g.attackable_idx():
            if i == p.idx:
                continue
            row.append(Btn(g.players[i].name[:16], callback_data=f"ww:p:{c}:st:{i}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([Btn("⬅️ Thôi", callback_data=f"ww:p:{c}:gx")])
        await q.answer()
        await _edit(context, q.message.chat_id, q.message.message_id,
                    "⚔️ *Chém ai?* (Chém nhầm người phe Dân Làng thì GIÁP của bạn sẽ vỡ!)", Kb(rows))
        return
    if act == "st":
        died = g.knight_strike(p.idx, int(rest[1]))
        if not died:
            await q.answer("Không chém được.", show_alert=True)
            return
        await q.answer("⚔️ KENG!")
        await _strip_kb(context, q.message.chat_id, q.message.message_id)
        await _flush(context, s)
        for i in died:
            await _send(context, s.chat_id, g.death_line(i, g.settings.reveal_roles))
        if (g.pending_shots or g.pending_successors) and not (s.hunter_task and not s.hunter_task.done()):
            s.hunter_task = _spawn(_after_shot(context, s))
        elif g.check_win():
            _wake_all(s)
        return


# ── Vòng Mô Tả ────────────────────────────────────────────────

def _words_view(s: WWSession) -> tuple:
    g = s.game
    total = len(g.alive_idx())
    miss = g.desc_missing()
    txt = f"✍️ *Đã nộp mô tả: {total - len(miss)}/{total}*"
    if miss:
        txt += "\n⏳ Chưa nộp: " + ", ".join(g.nm(i) for i in miss)
    return txt, None


async def _word_round(context, s: WWSession) -> None:
    """Vòng Mô Tả: KHÔNG có đồng hồ. Mọi người sống bắt buộc nộp mô tả xong mới sang tổng kết + thảo luận + bỏ phiếu.
    Ai nộp xong thì lời mô tả hiện NGAY ở chat chính (ghi rõ tên người nộp, xem ww_dm_text)."""
    g = s.game
    s.stage = "words"
    s.ev_words.clear()
    words = g.begin_word_round()
    a, b, label = g.word_pair
    await _send(context, s.chat_id,
                f"📝 *VÒNG MÔ TẢ — TÌM KẺ GIẢ MẠO* (Ngày {g.day_no})\n"
                f"🏷️ Chủ đề chung: *{label}*\n\n"
                f"Bot vừa *nhắn riêng 1 từ khoá* cho mỗi người. Hãy nhắn cho bot *1 câu mô tả* từ của mình "
                f"— *cấm nói thẳng từ khoá*!\n"
                f"⏳ *Không giới hạn thời gian*, nhưng *bắt buộc ai cũng phải nộp* — đủ mọi người mới tới tổng kết & bỏ phiếu. "
                f"Ai nộp xong, lời mô tả hiện ngay tại đây 👇\n"
                "Số đông cầm cùng 1 từ, còn kẻ giả mạo cầm từ khác na ná. Lời mô tả nào lệch số đông là đáng ngờ 👀")
    prog = await _send(context, s.chat_id, _words_view(s)[0])
    live = Live(context, s.chat_id, prog.message_id if prog else None, lambda: _words_view(s))
    s.vote_live = live
    for idx, w in words.items():
        hint = g.word_hint(idx)
        text = (f"📝 *VÒNG MÔ TẢ — Ngày {g.day_no}*\n🏷️ Chủ đề: {label}\n\n"
                f"🔑 Từ khoá của bạn: *{w}*\n{hint}\n\n"
                "👉 Hãy *nhắn 1 câu mô tả* từ này ngay tại đây (2–80 ký tự, không nói thẳng từ khoá). "
                "Nộp xong lời bạn sẽ hiện ở nhóm ngay, và cả làng phải chờ đủ người mới sang bước tiếp theo.")
        await _dm(context, s, idx, text)
        await asyncio.sleep(0.05)
    # Chờ tới khi TẤT CẢ người sống đã nộp (không timeout). /endgame sẽ set ev để thoát.
    while g.desc_missing() and not s.aborted:
        s.ev_words.clear()
        if not g.desc_missing():
            break
        await s.ev_words.wait()
    if s.aborted:
        return
    s.stage = "idle"
    live.mid = None
    if prog:
        await _edit(context, s.chat_id, prog.message_id, _words_view(s)[0].split("\n")[0] + " ✅", None)
    lines = [f"📜 *TỔNG KẾT LỜI MÔ TẢ NGÀY {g.day_no}* (theo thứ tự nộp):", ""]
    for d in g.descs:
        lines.append(f"{d.order}. *{g.nm(d.idx)}:* “{d.text}”")
    lines.append("")
    lines.append("🔎 _Ai mô tả khác số đông? Ai nói mơ hồ để né? Hãy soi kỹ rồi thảo luận!_")
    await _send(context, s.chat_id, "\n".join(lines))


# ── Thảo luận ────────────────────────────────────────────────

def _talk_kb(s: WWSession) -> Kb:
    c = s.chat_id
    g = s.game
    need = int(len(g.voter_idx()) * MIN_TALK_SKIP) + 1
    rows = []
    if g.settings.letters:
        rows.append([Btn("✉️ Gửi thư nặc danh", callback_data=f"ww:l:{c}")])
    rows.append([Btn(f"⏩ Bỏ phiếu sớm ({len(s.skip_votes)}/{need})", callback_data=f"ww:skip:{c}")])
    return _game_kb(s, rows)


async def _talk(context, s: WWSession) -> None:
    g = s.game
    s.stage = "talk"
    s.ev_talk.clear()
    s.skip_votes.clear()
    secs = _T(s, T_TALK if g.settings.word_round else T_TALK_NOWORD)
    m = await _send(context, s.chat_id,
                    f"💬 *THẢO LUẬN* — {secs} giây\n"
                    "Cãi nhau đi nào! Ai đáng nghi? Bạn cũng có thể gửi ✉️ *thư nặc danh* (mỗi người 1 thư/ngày) "
                    "để tố cáo mà không lộ mặt 😈\n"
                    "_Khi hơn nửa số người sống bấm ⏩ thì vào bỏ phiếu sớm._", _talk_kb(s))
    await _wait(s, s.ev_talk, secs, context, "⏳ *Còn 15 giây* trước khi bỏ phiếu!")
    s.stage = "idle"
    if m:
        await _strip_kb(context, s.chat_id, m.message_id)


async def _cb_skip(update, context, s: WWSession) -> None:
    q = update.callback_query
    g = s.game
    p = g.find_user(q.from_user.id)
    if not p or not p.alive or s.stage != "talk" or p.role.key == "alphawolf":
        await q.answer("Không dùng được.", show_alert=True)
        return
    s.skip_votes.add(p.idx)
    need = int(len(g.voter_idx()) * MIN_TALK_SKIP) + 1
    await q.answer("Đã bấm ⏩")
    if len(s.skip_votes) >= need:
        s.ev_talk.set()
        return
    try:
        await q.message.edit_reply_markup(reply_markup=_talk_kb(s))
    except TelegramError:
        pass


async def _cb_letter(update, context, s: WWSession) -> None:
    q = update.callback_query
    g = s.game
    p = g.find_user(q.from_user.id)
    if not g.settings.letters:
        await q.answer("Thư nặc danh đang tắt.", show_alert=True)
        return
    if not p or not p.alive:
        await q.answer("Bạn không gửi thư được.", show_alert=True)
        return
    if p.role.key == "serialkiller":                 # Sát Nhân biết hết thân phận -> gửi thư nặc danh là phá game
        s.pending.pop(p.user_id, None)
        await q.answer(SK_NO_LETTER_MSG, show_alert=True)
        return
    if g.phase not in ("day", "vote"):
        await q.answer("Chỉ gửi thư ban ngày ạ~", show_alert=True)
        return
    if any(d == g.day_no and i == p.idx for d, i, _ in g.letters):
        await q.answer("Hôm nay bạn đã gửi 1 thư rồi!", show_alert=True)
        return
    s.pending[p.user_id] = "letter"
    await q.answer("Bot đã nhắn hướng dẫn qua DM ✉️")
    await _dm(context, s, p.idx,
              "✉️ *THƯ NẶC DANH* — gõ nội dung thư (3–160 ký tự) rồi gửi ngay tại đây. "
              "Thư sẽ được đăng ẩn danh vào nhóm. Muốn huỷ: gõ `huỷ`.")


# ── Bỏ phiếu ──────────────────────────────────────────────────

def _vote_view(s: WWSession) -> tuple:
    g = s.game
    v = g.vote
    c = s.chat_id
    alive = len(g.voter_idx())
    lines = [f"🗳️ *BỎ PHIẾU TREO CỔ* — vòng {v.round}", "Bấm tên người bạn muốn treo cổ (đổi ý được tới khi hết giờ).", ""]
    roll: dict = {}
    for voter, t in v.ballots.items():
        roll.setdefault(t, []).append(voter)
    if g.settings.secret_vote:
        lines.append(f"🤐 _Phiếu kín_ — đã bỏ phiếu: *{len(v.ballots)}/{alive}*")
    else:
        for t, voters in roll.items():
            w = sum(g.vote_weight(x) for x in voters)
            who = ", ".join(g.nm(x) for x in voters)
            lines.append(("⏭ *Bỏ qua*" if t is None else f"🎯 *{g.nm(t)}*") + f" — {w} phiếu ({who})")
        lines.append(f"\n📊 Đã bỏ phiếu: {len(v.ballots)}/{alive}")
    tally = g.tally()
    rows, row = [], []
    for i in v.candidates:
        if not g.players[i].alive:
            continue
        cnt = tally.get(i, 0)
        label = g.players[i].name[:14] + (f" ({cnt})" if cnt and not g.settings.secret_vote else "")
        row.append(Btn(label, callback_data=f"ww:v:{c}:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([Btn("⏭ Bỏ qua (không treo ai)", callback_data=f"ww:v:{c}:s")])
    return "\n".join(lines), Kb(rows)


async def _run_vote(context, s: WWSession, secs: int) -> tuple:
    g = s.game
    s.stage = "vote"
    s.ev_vote.clear()
    text, kb = _vote_view(s)
    m = await _send(context, s.chat_id, text, kb)
    live = Live(context, s.chat_id, m.message_id if m else None, lambda: _vote_view(s))
    s.vote_live = live
    if not g.voters_done():
        await _wait(s, s.ev_vote, secs, context, "⏳ *Còn 15 giây* để bỏ phiếu!")
    s.stage = "idle"
    if live.task and not live.task.done():
        live.task.cancel()
    roll = g.close_vote()
    text, _ = _vote_view(s)
    await _edit(context, s.chat_id, m.message_id if m else None, text.split("\n\n")[0] + "\n\n🔒 _Đã đóng phiếu._", None)
    return roll, g.vote_outcome()


def _roll_lines(s: WWSession, roll: dict) -> list:
    g = s.game
    lines = []
    if g.settings.secret_vote:
        tally = g.tally()
        for t, w in sorted(tally.items(), key=lambda kv: -kv[1]):
            lines.append(("⏭ Bỏ qua" if t is None else f"🎯 {g.nm(t)}") + f": {w} phiếu")
        return lines
    for t, voters in sorted(roll.items(), key=lambda kv: -len(kv[1])):
        w = sum(g.vote_weight(x) for x in voters)
        lines.append(("⏭ Bỏ qua" if t is None else f"🎯 {g.nm(t)}") + f": {w} phiếu ← " + ", ".join(g.nm(x) for x in voters))
    nov = [g.nm(i) for i in g.voter_idx() if i not in g.vote.ballots]
    if nov:
        lines.append("😶 Không bỏ phiếu: " + ", ".join(nov))
    return lines


async def _vote_phase(context, s: WWSession) -> None:
    g = s.game
    if g.judge_vetoed:
        await _send(context, s.chat_id, "⚖️ _Án treo cổ hôm nay đã bị Quan Toà huỷ — không bỏ phiếu._")
        return
    g.begin_vote(round_no=1)
    roll, (kind, who) = await _run_vote(context, s, _T(s, T_VOTE))
    if s.aborted or g.over:
        return
    lines = ["📊 *KẾT QUẢ BỎ PHIẾU*", ""] + _roll_lines(s, roll)
    await _send(context, s.chat_id, "\n".join(lines))
    if g.judge_vetoed:
        await _send(context, s.chat_id, "⚖️ _Án treo cổ đã bị huỷ — không ai bị hành quyết._")
        return
    tried_mayor = False
    if kind == "tie":
        mayor = g.mayor_tiebreaker()
        if mayor is not None:
            tried_mayor = True
            pick = await _mayor_tiebreak(context, s, mayor, who)
            if pick is not None:
                await _send(context, s.chat_id, f"🎖️ *Thị Trưởng {mayor.name} phá hoà* — chọn treo cổ *{g.nm(pick)}*!")
                kind, who = "lynch", [pick]
    if kind == "tie":
        names = " & ".join(g.nm(i) for i in who)
        await _send(context, s.chat_id, f"⚖️ *Hoà phiếu giữa {names}!* Bỏ phiếu lại chỉ giữa những người này…")
        g.begin_vote(candidates=who, round_no=2)
        roll, (kind, who) = await _run_vote(context, s, _T(s, T_RUNOFF))
        if s.aborted or g.over:
            return
        await _send(context, s.chat_id, "📊 *KẾT QUẢ VÒNG 2*\n\n" + "\n".join(_roll_lines(s, roll)))
        if g.judge_vetoed:
            await _send(context, s.chat_id, "⚖️ _Án treo cổ đã bị huỷ — không ai bị hành quyết._")
            return
        if kind == "tie" and not tried_mayor:
            mayor = g.mayor_tiebreaker()
            if mayor is not None:
                pick = await _mayor_tiebreak(context, s, mayor, who)
                if pick is not None:
                    await _send(context, s.chat_id, f"🎖️ *Thị Trưởng {mayor.name} phá hoà* — chọn treo cổ *{g.nm(pick)}*!")
                    kind, who = "lynch", [pick]
        if kind == "tie":
            await _send(context, s.chat_id, "🤝 *Lại hoà!* Làng không thống nhất — hôm nay không ai bị treo cổ.")
            return
    if kind != "lynch":
        await _send(context, s.chat_id, "🤷 *Không đủ phiếu đồng thuận* — hôm nay không ai bị treo cổ.")
        return
    idx = who[0]
    await _send(context, s.chat_id, f"⚖️ *Làng quyết định treo cổ {g.nm(idx)}!*")
    died = g.lynch(idx)
    await _flush(context, s)
    for i in died:
        await _send(context, s.chat_id, g.death_line(i, g.settings.reveal_roles))
    await _hunters(context, s)


async def _cb_vote(update, context, s: WWSession, rest: list) -> None:
    q = update.callback_query
    g = s.game
    p = g.find_user(q.from_user.id)
    if not p:
        await q.answer("Bạn không ở trong ván này.", show_alert=True)
        return
    if not p.alive:
        await q.answer("Người chết không được bỏ phiếu 👻", show_alert=True)
        return
    if p.role.key == "alphawolf":
        await q.answer("Sói Đầu Đàn đứng ngoài lá phiếu của làng 👑", show_alert=True)
        return
    if s.stage != "vote":
        await q.answer("Chưa/đã hết giờ bỏ phiếu.", show_alert=True)
        return
    target = None if rest[0] == "s" else int(rest[0])
    ok, err = g.cast_vote(p.idx, target)
    if not ok:
        await q.answer(err, show_alert=True)
        return
    await q.answer("Đã bỏ qua." if target is None else f"Đã bỏ phiếu: {g.nm(target)}")
    if s.vote_live:
        s.vote_live.refresh()
    if g.voters_done():
        s.ev_vote.set()


# ── Nút phụ ───────────────────────────────────────────────────

async def _cb_role(update, context, s: WWSession) -> None:
    q = update.callback_query
    g = s.game
    p = g.find_user(q.from_user.id)
    if not p:
        await q.answer("Bạn không ở trong ván này.", show_alert=True)
        return
    text = g.role_card(p.idx)
    if p.word and s.stage in ("words", "talk", "vote") and g.settings.word_round:
        text += f"\n\n🔑 *Từ khoá hôm nay:* {p.word}"
    if not p.alive:
        text += "\n\n👻 _Bạn đã chết — hãy im lặng và xem tiếp nhé._"
    await q.answer("Đã gửi lại vai qua DM.")
    await _dm(context, s, p.idx, text)


async def _cb_alive(update, context, s: WWSession) -> None:
    q = update.callback_query
    g = s.game
    al = [g.nm(i) for i in g.alive_idx()]
    dead = [g.nm(p.idx) for p in g.players if not p.alive]
    txt = f"🟢 Sống ({len(al)}): " + ", ".join(al)
    if dead:
        txt += f"\n☠️ Chết ({len(dead)}): " + ", ".join(dead)
    await q.answer(txt[:195], show_alert=True)


# ════════════════════════════════════════════════════════════════
# KẾT THÚC
# ════════════════════════════════════════════════════════════════

async def _finale(context, s: WWSession) -> None:
    g = s.game
    o = g.outcome
    lines = ["🏁 *VÁN MA SÓI KẾT THÚC!*", "", f"*{o.title}*" if o else "*Hoà*"]
    if o and o.winners:
        lines.append("🏆 *Người thắng:* " + ", ".join(g.nm(i) for i in o.winners))
    extra = [i for i in g.personal_wins if not o or i not in o.winners]
    if extra:
        lines.append("🪓🏆 *Đao Phủ cũng thắng* (đã treo cổ đúng mục tiêu): " + ", ".join(g.nm(i) for i in extra))
    lines += ["", "📋 *Bảng vai trò:*", g.final_report()]
    await _send(context, s.chat_id, "\n".join(lines))
    if g.settings.word_round and g.word_hist:
        wl = ["🔑 *Từ khoá các ngày* (số đông · kẻ giả mạo):"]
        for d, a, b, label in g.word_hist:
            wl.append(f"• Ngày {d} ({label}): *{a}* · *{b}*")
        await _send(context, s.chat_id, "\n".join(wl))
    if g.letters:
        ll = ["✉️ *Ai đã viết thư nặc danh?*"]
        for d, i, text in g.letters:
            ll.append(f"• Ngày {d} — *{g.nm(i)}*: “{text}”")
        await _send(context, s.chat_id, "\n".join(ll)[:3800])
    await _send(context, s.chat_id, "🎉 _Cảm ơn cả làng đã chơi! Chủ phòng gõ /masoi để mở ván mới._")


# ════════════════════════════════════════════════════════════════
# HOOK TIN NHẮN (bot.py gọi)
# ════════════════════════════════════════════════════════════════

async def ww_dm_text(update: Update, context) -> bool:
    """Tin nhắn văn bản trong DM của người đang chơi Ma Sói. True = đã xử lý (chặn không cho rơi sang chat AI)."""
    msg, user = update.effective_message, update.effective_user
    if not msg or not msg.text or not user:
        return False
    cid = WW_USER.get(user.id)
    s = WW_GAMES.get(cid) if cid else None
    if not s:
        return False
    g = s.game
    p = g.find_user(user.id)
    text = msg.text.strip()
    if not p or text.startswith("/"):
        return False

    if s.pending.get(user.id) == "letter":
        s.pending.pop(user.id, None)
        if text.lower() in ("huỷ", "huy", "hủy", "cancel"):
            await _send(context, user.id, "✉️ Đã huỷ thư.")
            return True
        ok, err = g.add_letter(p.idx, text)
        if not ok:
            await _send(context, user.id, f"❌ {err}")
            return True
        k = sum(1 for d, _, _ in g.letters if d == g.day_no)
        await _send(context, s.chat_id, f"✉️ *Thư nặc danh #{k} (Ngày {g.day_no}):*\n“{text}”")
        await _send(context, user.id, "✅ Thư đã gửi ẩn danh vào nhóm.")
        return True

    if not p.alive:
        # Già Làng đã chết: đêm hôm sau được BÁO MỘNG vào nhóm chính
        if s.stage == "night" and g.dream_ready(p.idx):
            if len(text) > 140:
                await _send(context, user.id, "❌ Tin báo mộng tối đa 140 ký tự.")
                return True
            left = g.use_dream(p.idx)
            await _send(context, s.chat_id, f"💭 *Giấc mộng của Già Làng ({p.name}):* “{text}”")
            await _send(context, user.id, f"✅ Đã báo mộng vào nhóm. Còn {left} tin." if left else "✅ Đã báo mộng — bạn hết lượt, giấc mơ tan biến.")
            return True
        return False

    if s.stage == "words" and g.word_pair is not None and not any(d.idx == p.idx for d in g.descs):
        ok, err = g.submit_desc(p.idx, text)
        if not ok:
            await _send(context, user.id, err if err.startswith("❌") else f"❌ {err}")
            return True
        # Hiện NGAY ở chat chính (bot đăng đúng tên người nộp → không ai mạo danh được)
        d = g.descs[-1]
        await _send(context, s.chat_id, f"💬 *{d.order}. {p.name}:* “{d.text}”")
        left = len(g.desc_missing())
        await _send(context, user.id,
                    "✅ Đã ghi nhận & đăng mô tả của bạn lên nhóm!" +
                    (f" Còn {left} người chưa nộp." if left else " Mọi người đã nộp đủ!"))
        if s.vote_live:
            s.vote_live.refresh()
        if not g.desc_missing():
            s.ev_words.set()
        return True

    if s.stage == "night":
        return await _night_relay(context, s, msg, p, text)
    return False


async def ww_group_message(update: Update, context) -> bool:
    """Xoá tin trong nhóm:
    • BAN ĐÊM: xoá MỌI tin của người chơi (bất kể cài đặt) — nếu chỉ xoá tin của Sói thì tự lộ Sói.
      Người có kênh chat đêm (Sói, cặp đôi...) được nhắc sang DM bot để chat kín.
    • Người đã chết: xoá nếu bật 'Xoá tin người chết'.
    True = đã xoá (đừng xử lý tiếp)."""
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    s = WW_GAMES.get(chat.id) if chat else None
    if not s or not msg or not user:
        return False
    p = s.game.find_user(user.id)
    if not p:
        return False
    night = s.stage == "night"
    if night and p.alive and p.role.key == "alphawolf":
        return False                         # Sói Đầu Đàn công khai: được chat tự do ở nhóm chính ban đêm
    if not (night or ((not p.alive) and s.game.settings.silence)):
        return False
    try:
        await msg.delete()
    except TelegramError:
        pass
    if night and p.alive and user.id not in s.night_warned and s.game.can_night_chat(p.idx):
        s.night_warned.add(user.id)
        await _send(context, user.id,
                    "🌙 Tin bạn vừa gửi ở nhóm đã bị *xoá* để không lộ danh tính. "
                    "Ban đêm hãy nhắn cho bot ở *đây* — bot sẽ chuyển kín cho đồng đội.")
    return True
