"""
filters.py — Meeff filter management (gender, age, nationality).

The keyboard builders that existed identically in common.py have been
consolidated here. common.py can be removed entirely.
"""

import json
import logging
from datetime import datetime

import aiohttp
from aiogram import types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from db import get_current_account, get_user_filters, set_user_filters

logger = logging.getLogger(__name__)

MEEFF_FILTER_URL = "https://api.meeff.com/user/updateFilter/v1"
FILTER_HEADERS = {
    "User-Agent": "okhttp/4.12.0",
    "Accept-Encoding": "gzip",
    "content-type": "application/json; charset=utf-8",
}

NATIONALITY_COUNTRIES = [
    ("RU", "🇷🇺"), ("UA", "🇺🇦"), ("BY", "🇧🇾"), ("IR", "🇮🇷"), ("PH", "🇵🇭"),
    ("PK", "🇵🇰"), ("US", "🇺🇸"), ("IN", "🇮🇳"), ("DE", "🇩🇪"), ("FR", "🇫🇷"),
    ("BR", "🇧🇷"), ("CN", "🇨🇳"), ("JP", "🇯🇵"), ("KR", "🇰🇷"), ("CA", "🇨🇦"),
    ("AU", "🇦🇺"), ("IT", "🇮🇹"), ("ES", "🇪🇸"), ("ZA", "🇿🇦"), ("TR", "🇹🇷"),
]

GENDER_MAP = {"male": 6, "female": 5, "all": 7}


# ─── Keyboard builders ────────────────────────────────────────────────────────

def get_filter_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Gender", callback_data="filter_gender"),
            InlineKeyboardButton(text="Age", callback_data="filter_age"),
        ],
        [InlineKeyboardButton(text="Nationality", callback_data="filter_nationality")],
        [InlineKeyboardButton(text="Back", callback_data="back_to_menu")],
    ])


def get_gender_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="All Gender", callback_data="filter_gender_all")],
        [InlineKeyboardButton(text="Male", callback_data="filter_gender_male")],
        [InlineKeyboardButton(text="Female", callback_data="filter_gender_female")],
        [InlineKeyboardButton(text="Back", callback_data="filter_back")],
    ])


def get_age_keyboard() -> InlineKeyboardMarkup:
    ages = list(range(18, 50))
    max_per_row = 8
    rows = [
        [InlineKeyboardButton(text=str(a), callback_data=f"filter_age_{a}")
         for a in ages[i:i + max_per_row]]
        for i in range(0, len(ages), max_per_row)
    ]
    rows.append([InlineKeyboardButton(text="Back", callback_data="filter_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_nationality_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="All Countries", callback_data="filter_nationality_all")],
        *[
            [InlineKeyboardButton(text=f"{flag} {code}", callback_data=f"filter_nationality_{code}")]
            for code, flag in NATIONALITY_COUNTRIES
        ],
        [InlineKeyboardButton(text="Back", callback_data="filter_back")],
    ])


# ─── Default filter builder ───────────────────────────────────────────────────

def _build_filter_data(existing: dict) -> dict:
    """Merge stored values with safe defaults."""
    return {
        "filterGenderType": existing.get("filterGenderType", 5),
        "filterBirthYearFrom": existing.get("filterBirthYearFrom", 1995),
        "filterBirthYearTo": datetime.now().year - 18,
        "filterDistance": 510,
        "filterLanguageCodes": existing.get("filterLanguageCodes", ""),
        "filterNationalityBlock": existing.get("filterNationalityBlock", 0),
        "filterNationalityCode": existing.get("filterNationalityCode", ""),
        "filterMinProfileImageCount": existing.get("filterMinProfileImageCount", 2),
        "filterFaceVerifiedOnly": existing.get("filterFaceVerifiedOnly", False),
        "filterRecentlyActiveOnly": existing.get("filterRecentlyActiveOnly", False),
        "locale": "en",
    }


# ─── Commands & handlers ──────────────────────────────────────────────────────

async def filter_command(msg, edit: bool = False) -> None:
    user_id = getattr(msg, "chat", msg).id
    token = await get_current_account(user_id)
    text = (
        "<b>Filters</b> — set your preferences:"
        if token
        else "No active account selected.\nPlease go to Accounts and select one first."
    )
    if edit and hasattr(msg, "edit_text"):
        await msg.edit_text(text, reply_markup=get_filter_keyboard(), parse_mode="HTML")
    else:
        await msg.answer(text, reply_markup=get_filter_keyboard(), parse_mode="HTML")


async def set_filter(callback_query: types.CallbackQuery) -> None:
    user_id = callback_query.from_user.id
    token = await get_current_account(user_id)
    if not token:
        await callback_query.message.edit_text(
            "No active account selected.\nPlease go to Accounts and select one first.",
            reply_markup=get_filter_keyboard(), parse_mode="HTML",
        )
        await callback_query.answer()
        return

    d = callback_query.data
    existing = (await get_user_filters(user_id, token)) or {}
    filter_data = _build_filter_data(existing)

    # ── Navigation callbacks (no API call needed) ──────────────────────────
    if d == "filter_gender":
        await callback_query.message.edit_text(
            "<b>Gender</b> — select a filter:", reply_markup=get_gender_keyboard(), parse_mode="HTML"
        )
        await callback_query.answer()
        return

    if d == "filter_age":
        await callback_query.message.edit_text(
            "<b>Age</b> — select a minimum age:", reply_markup=get_age_keyboard(), parse_mode="HTML"
        )
        await callback_query.answer()
        return

    if d == "filter_nationality":
        await callback_query.message.edit_text(
            "<b>Nationality</b> — select a country:", reply_markup=get_nationality_keyboard(), parse_mode="HTML"
        )
        await callback_query.answer()
        return

    if d == "filter_back":
        await filter_command(callback_query.message, edit=True)
        await callback_query.answer()
        return

    # ── Value-setting callbacks ────────────────────────────────────────────
    if d.startswith("filter_gender_"):
        gender = d.split("_")[-1]
        filter_data["filterGenderType"] = GENDER_MAP.get(gender, 5)
        confirm_msg = f"Filter updated: Gender set to {gender.capitalize()}"

    elif d.startswith("filter_age_"):
        age = int(d.split("_")[-1])
        filter_data["filterBirthYearFrom"] = datetime.now().year - age
        confirm_msg = f"Filter updated: Age set to {age}"

    elif d.startswith("filter_nationality_"):
        nationality = d.split("_")[-1]
        filter_data["filterNationalityCode"] = "" if nationality == "all" else nationality
        confirm_msg = f"Filter updated: Nationality set to {'All' if nationality == 'all' else nationality.upper()}"

    else:
        return  # unknown callback prefix — ignore

    await set_user_filters(user_id, token, filter_data)

    headers = {**FILTER_HEADERS, "meeff-access-token": token}
    async with aiohttp.ClientSession() as session:
        async with session.post(MEEFF_FILTER_URL, data=json.dumps(filter_data), headers=headers) as resp:
            if resp.status == 200:
                await callback_query.message.edit_text(
                    f"{confirm_msg}\n\n<b>Filters</b> — set your preferences:",
                    reply_markup=get_filter_keyboard(), parse_mode="HTML",
                )
            else:
                resp_text = await resp.text()
                logger.warning("Filter update failed (%s): %s", resp.status, resp_text)
                await callback_query.message.edit_text(
                    f"Failed to update filter.\n<code>{resp_text}</code>",
                    reply_markup=get_filter_keyboard(), parse_mode="HTML",
                )

    await callback_query.answer()
