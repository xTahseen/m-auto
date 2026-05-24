"""
countries.py — Country filter management (include / exclude mode).

Previously used a separate MongoDB collection. Now uses the country_filters
table in the shared SQLite database via db.py helper functions.
"""

import html
import logging

from aiogram import types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from db import get_country_filter, save_country_filter

logger = logging.getLogger(__name__)


# ─── Core filter logic ────────────────────────────────────────────────────────

def _extract_nat_code(user: dict) -> str | None:
    """Extract an ISO-2 nationality code from a Meeff user object."""
    nat = user.get("nationalityCode") or user.get("locale")
    if not nat:
        return None
    nat = nat.strip().upper()
    # "EN-US" → "US", "KR" → "KR"
    if "-" in nat:
        nat = nat.split("-")[-1]
    return nat if len(nat) == 2 else None


async def should_include_user(user_id: int, user: dict) -> bool:
    """
    Return True if this user should be processed (not filtered out).

    - Filter disabled  → always include
    - mode == "exclude" → include unless nat_code is in the list
    - mode == "include" → include only if nat_code is in the list
                          (users with unknown country are skipped)
    """
    doc = await get_country_filter(user_id)

    if not doc.get("enabled", True):
        return True

    codes = set(doc.get("codes", []))
    if not codes:
        return True

    nat_code = _extract_nat_code(user)

    if doc.get("mode", "exclude") == "exclude":
        return not (nat_code and nat_code in codes)
    else:  # include mode
        return bool(nat_code and nat_code in codes)


async def toggle_country_codes(user_id: int, codes: list) -> tuple[list, list]:
    """Toggle each code: add if absent, remove if present. Returns (added, removed)."""
    doc = await get_country_filter(user_id)
    existing = set(doc.get("codes", []))
    added, removed = [], []

    for raw in codes:
        code = raw.strip().upper()
        if not code:
            continue
        if code in existing:
            existing.discard(code)
            removed.append(code)
        else:
            existing.add(code)
            added.append(code)

    doc["codes"] = sorted(existing)
    await save_country_filter(user_id, doc)
    return added, removed


# ─── UI helpers ───────────────────────────────────────────────────────────────

def _build_keyboard(user_id: int, doc: dict) -> InlineKeyboardMarkup:
    enabled = doc.get("enabled", True)
    mode = doc.get("mode", "exclude")
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="ON" if enabled else "OFF",
                callback_data=f"ctry_toggle_enabled:{user_id}",
            ),
            InlineKeyboardButton(
                text="Include" if mode == "include" else "Exclude",
                callback_data=f"ctry_toggle_mode:{user_id}",
            ),
        ],
        [
            InlineKeyboardButton(text="Clear All", callback_data=f"ctry_clear:{user_id}"),
        ],
    ])


def _format_status(doc: dict) -> str:
    enabled = doc.get("enabled", True)
    mode = doc.get("mode", "exclude")
    codes = doc.get("codes", [])
    mode_word = "INCLUDE (add only these)" if mode == "include" else "EXCLUDE (skip these)"
    code_list = ", ".join(codes) if codes else "_(none)_"
    return "\n".join([
        f"<b>Country Filter</b>  [{'ON' if enabled else 'OFF'}]",
        f"<b>Mode:</b> {mode_word}",
        f"<b>Countries ({len(codes)}):</b> {html.escape(code_list)}",
        "",
        "Use <code>/countries PK US GB</code> to add/remove countries.",
    ])


def _format_toggle_result(added: list, removed: list) -> str:
    lines = []
    if added:
        lines.append(f"Added: {', '.join(added)}")
    if removed:
        lines.append(f"Removed: {', '.join(removed)}")
    return "\n".join(lines) if lines else "No changes made."


# ─── Command handler ──────────────────────────────────────────────────────────

async def countries_command_handler(message: types.Message) -> None:
    """
    /countries              → show status panel
    /countries PK RU US     → toggle each code
    """
    user_id = message.chat.id
    parts = message.text.strip().split()
    codes = parts[1:]

    if not codes:
        doc = await get_country_filter(user_id)
        await message.answer(
            _format_status(doc),
            reply_markup=_build_keyboard(user_id, doc),
            parse_mode="HTML",
        )
        return

    added, removed = await toggle_country_codes(user_id, codes)
    doc = await get_country_filter(user_id)
    result_text = _format_toggle_result(added, removed)
    await message.answer(
        f"{result_text}\n\n{_format_status(doc)}",
        reply_markup=_build_keyboard(user_id, doc),
        parse_mode="HTML",
    )


# ─── Callback handler ─────────────────────────────────────────────────────────

async def handle_countries_callback(callback_query: types.CallbackQuery) -> bool:
    """Return True if the callback was handled."""
    data = callback_query.data or ""
    prefixes = ("ctry_toggle_enabled:", "ctry_toggle_mode:", "ctry_clear:")
    if not any(data.startswith(p) for p in prefixes):
        return False

    try:
        owner_id = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback_query.answer("Invalid data.")
        return True

    if callback_query.from_user.id != owner_id:
        await callback_query.answer("Not your filter.", show_alert=True)
        return True

    doc = await get_country_filter(owner_id)

    if data.startswith("ctry_toggle_enabled:"):
        doc["enabled"] = not doc.get("enabled", True)
        await save_country_filter(owner_id, doc)
        await callback_query.answer(f"Filter turned {'ON' if doc['enabled'] else 'OFF'}.")

    elif data.startswith("ctry_toggle_mode:"):
        doc["mode"] = "include" if doc.get("mode", "exclude") == "exclude" else "exclude"
        await save_country_filter(owner_id, doc)
        await callback_query.answer(f"Mode set to {doc['mode'].upper()}.")

    elif data.startswith("ctry_clear:"):
        doc["codes"] = []
        await save_country_filter(owner_id, doc)
        await callback_query.answer("Country list cleared.")

    try:
        await callback_query.message.edit_text(
            _format_status(doc),
            reply_markup=_build_keyboard(owner_id, doc),
            parse_mode="HTML",
        )
    except Exception as e:
        if "message is not modified" not in str(e):
            logger.warning("countries edit error: %s", e)

    return True
