"""
ai_cancel.py — Module trung gian lưu trữ AI_ACTIVE_TASKS.

Tách riêng để tránh circular import giữa bot.py ↔ handlers_basic.py.
Cả bot.py và handlers_basic.py đều import từ đây.
"""

import asyncio

# {chat_id: asyncio.Task} — task AI đang chạy cho mỗi chat.
# Được đặt bởi _run_ai_chat / _run_vision_analysis / v.v. trong bot.py.
# Được cancel bởi cmd_cancel trong handlers_basic.py.
AI_ACTIVE_TASKS: dict[int, "asyncio.Task"] = {}
