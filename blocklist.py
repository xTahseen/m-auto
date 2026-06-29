"""
blocklist.py — Per-user blocklist management (permanent + temporary).

Uses db.py's native functions directly. The MongoDB-shim (_BlocklistsProxy)
has been removed; all state lives in the SQLite blocklists table.
"""

import asyncio
import logging

from aiogram import types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import db

logger = logging.getLogger(__name__)

BLOCKLIST_MARKUP = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="Enable", callback_data="blocklist_on"),
        InlineKeyboardButton(text="Disable", callback_data="blocklist_off"),
        InlineKeyboardButton(text="Clear Temp", callback_data="blocklist_clear"),
    ],
    [InlineKeyboardButton(text="Back", callback_data="back_to_menu")],
])


_blocklist_lock = asyncio.Lock()


async def get_user_blocklist(user_id) -> set:
    """Return the union of permanent + temporary blocklists."""
    doc = await db.get_blocklist_doc(user_id)
    if not doc:
        return set()
    return set(doc.get("permanent", [])) | set(doc.get("temporary", []))


async def get_permanent_blocklist(user_id) -> set:
    doc = await db.get_blocklist_doc(user_id)
    return set(doc.get("permanent", [])) if doc else set()


async def get_temporary_blocklist(user_id) -> set:
    doc = await db.get_blocklist_doc(user_id)
    return set(doc.get("temporary", [])) if doc else set()


async def is_blocklist_active(user_id) -> bool:
    doc = await db.get_blocklist_doc(user_id)
    return bool(doc and doc.get("active", False))


async def add_to_permanent_blocklist(user_id, user_to_block: str) -> None:
    await db.add_to_blocklist(user_id, user_to_block, "permanent")


async def add_to_temporary_blocklist(user_id, user_to_block: str) -> None:
    await db.add_to_blocklist(user_id, user_to_block, "temporary")


async def clear_temporary_blocklist(user_id) -> None:
    await db.upsert_blocklist(user_id, temporary=[])


async def set_blocklist_active(user_id, active: bool) -> None:
    await db.upsert_blocklist(user_id, active=active)


async def atomic_check_and_add_blocklist(user_id, user_to_block: str) -> bool:
    """
    Thread-safe check-and-add. Returns True if the ID was already present
    (caller should skip the user), False if it was freshly added.
    """
    async with _blocklist_lock:
        blocklist = await get_user_blocklist(user_id)
        if user_to_block in blocklist:
            return True
        await add_to_temporary_blocklist(user_id, user_to_block)
        return False


def _blocklist_text(active: bool, permanent: set, temporary: set) -> str:
    status_label = "ON" if active else "OFF"
    return (
        f"<b>Blocklist</b>\n\n"
        f"Status: <b>{status_label}</b>\n"
        f"Permanent blocks: <b>{len(permanent)}</b>\n"
        f"Temporary blocks: <b>{len(temporary)}</b>\n\n"
        f"<i>Temporary blocks are cleared between sessions.\n"
        f"Permanent blocks persist forever.</i>"
    )


async def _send_blocklist_ui(target, edit: bool = True) -> None:
    """Render the blocklist panel to a message or callback."""
    if isinstance(target, types.CallbackQuery):
        user_id = target.from_user.id
    else:
        user_id = target.chat.id

    active = await is_blocklist_active(user_id)
    permanent = await get_permanent_blocklist(user_id)
    temporary = await get_temporary_blocklist(user_id)
    text = _blocklist_text(active, permanent, temporary)

    if isinstance(target, types.CallbackQuery):
        await target.message.edit_text(text, reply_markup=BLOCKLIST_MARKUP, parse_mode="HTML")
        await target.answer()
    elif edit and hasattr(target, "edit_text"):
        await target.edit_text(text, reply_markup=BLOCKLIST_MARKUP, parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=BLOCKLIST_MARKUP, parse_mode="HTML")


async def blocklist_command(message_or_callback, edit: bool = True) -> None:
    await _send_blocklist_ui(message_or_callback, edit=edit)


async def handle_blocklist_callback(callback_query: types.CallbackQuery) -> bool:
    """Return True if handled."""
    data = callback_query.data
    user_id = callback_query.from_user.id

    if data == "blocklist_on":
        await set_blocklist_active(user_id, True)
        await callback_query.answer("Blocklist is now ON.")
    elif data == "blocklist_off":
        await set_blocklist_active(user_id, False)
        await callback_query.answer("Blocklist is now OFF.")
    elif data == "blocklist_clear":
        await clear_temporary_blocklist(user_id)
        await callback_query.answer("Temporary blocklist cleared!")
    else:
        return False

    active = await is_blocklist_active(user_id)
    permanent = await get_permanent_blocklist(user_id)
    temporary = await get_temporary_blocklist(user_id)
    await callback_query.message.edit_text(
        _blocklist_text(active, permanent, temporary),
        reply_markup=BLOCKLIST_MARKUP,
        parse_mode="HTML",
    )
    return True
