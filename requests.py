"""
requests.py — Friend-request runner (single account and parallel all-accounts modes).

Key improvements over the original:
- Eliminated duplicated blocklist / country-filter logic between the two
  worker paths by extracting a _should_skip() helper.
- Safe JSON parsing on API responses (content_type=None everywhere).
- Shared aiohttp session per worker run (was creating a new session per user).
- Rate-limit (LikeExceeded) stops cleanly without busy-looping.
"""

import asyncio
import html
import json
import logging
import time
from datetime import datetime, timezone

import aiohttp
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from dateutil import parser as dateutil_parser

from blocklist import is_blocklist_active, atomic_check_and_add_blocklist, get_user_blocklist
from countries import should_include_user
from db import get_user_filters, get_explore_url

logger = logging.getLogger(__name__)


class MissingExploreUrlError(Exception):
    """Raised when no explore URL has been saved for the user."""

class RequestExceededError(Exception):
    """Raised when the API returns HTTP 429 / errorCode RequestExceeded."""

class NoMoreUsersError(Exception):
    """Raised when the API returns an empty user list with hasMore=false."""

# ─── Constants ─────────────────────────────────────────────────────────────────

_MEEFF_ANSWER_URL = "https://api.meeff.com/user/undoableAnswer/v5/?userId={user_id}&isOkay=1"
_MEEFF_FILTER_URL = "https://api.meeff.com/user/updateFilter/v1"
_FILTER_PUSH_INTERVAL = 7   # push filters every N successful sends

SPEED_LEVELS: dict[str, tuple[str, float]] = {
    "default": ("Default (3s)", 3.0),
    "turbo":   ("Turbo (0.02s)", 0.02),
}

UPDATE_INTERVAL = 2  # seconds between status-message edits

# ─── Markups ───────────────────────────────────────────────────────────────────

REQUESTS_CHOICE_MARKUP = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="Current Account", callback_data="requests_current"),
        InlineKeyboardButton(text="All Accounts",    callback_data="requests_all"),
    ],
    [InlineKeyboardButton(text="Cancel", callback_data="requests_cancel")],
])

REQUESTS_ALL_CONFIRM_MARKUP = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Confirm", callback_data="requests_confirm")],
    [InlineKeyboardButton(text="Cancel",  callback_data="requests_cancel")],
])

STOP_MARKUP = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Stop Requests", callback_data="stop")],
])


def get_speed_markup(current_speed: str | None = None) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=f"{title} [on]" if key == current_speed else title,
            callback_data=f"speed_{key}",
        )
        for key, (title, _) in SPEED_LEVELS.items()
    ]
    buttons.append(InlineKeyboardButton(text="Custom", callback_data="speed_custom"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


# ─── Formatters ────────────────────────────────────────────────────────────────

def _time_ago(dt_str: str | None) -> str:
    if not dt_str:
        return "N/A"
    try:
        diff = datetime.now(timezone.utc) - dateutil_parser.isoparse(dt_str)
        minutes = int(diff.total_seconds() // 60)
        if minutes < 1:
            return "just now"
        if minutes < 60:
            return f"{minutes} min ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} hr ago"
        return f"{hours // 24} day(s) ago"
    except Exception:
        return "unknown"


def format_user(user: dict) -> str:
    last_active = _time_ago(user.get("recentAt"))
    height = html.escape(str(user.get("height", "N/A")))
    if "|" in height:
        h_val, h_unit = height.split("|", 1)
        height = f"{h_val.strip()} {h_unit.strip()}"
    photos = " ".join(
        f"<a href='{html.escape(url)}'>Photo</a>" for url in user.get("photoUrls", [])
    )
    return (
        f"<b>Name:</b> {html.escape(user.get('name', 'N/A'))}\n"
        f"<b>ID:</b> <code>{html.escape(user.get('_id', 'N/A'))}</code>\n"
        f"<b>Nationality:</b> {html.escape(user.get('nationalityCode', 'N/A'))}\n"
        f"<b>Height:</b> {height}\n"
        f"<b>Description:</b> {html.escape(user.get('description', 'N/A'))}\n"
        f"<b>Birth Year:</b> {html.escape(str(user.get('birthYear', 'N/A')))}\n"
        f"<b>Platform:</b> {html.escape(user.get('platform', 'N/A'))}\n"
        f"<b>Profile Score:</b> {html.escape(str(user.get('profileScore', 'N/A')))}\n"
        f"<b>Distance:</b> {html.escape(str(user.get('distance', 'N/A')))} km\n"
        f"<b>Language Codes:</b> {html.escape(', '.join(user.get('languageCodes', [])))}\n"
        f"<b>Last Active:</b> {last_active}\n"
        f"Photos: {photos}"
    )


def _format_time(start: datetime, end: datetime) -> str:
    total = int((end - start).total_seconds())
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def format_progress_single(account_name: str, added: int, skipped: int) -> str:
    return (
        f"<b>[ RUNNING ]</b> — {html.escape(account_name)}\n"
        f"├ Sent: {added}\n"
        f"└ Skipped: {skipped}\n\n"
        "Processing… tap Stop to interrupt."
    )


def _format_result_single(account_name: str, added: int, skipped: int,
                           start: datetime, end: datetime,
                           like_exceeded: bool = False,
                           no_more_users: bool = False,
                           stopped_by_user: bool = False) -> str:
    header = "<b>[ STOPPED ]</b>" if stopped_by_user else "<b>[ DONE ]</b>"
    if like_exceeded:
        extra = "\n<b>⏳ Daily request quota reached. Please wait until tomorrow.</b>"
    elif no_more_users:
        extra = "\n<b>✅ No more users available right now.</b>"
    else:
        extra = ""
    return (
        f"{header}\n"
        f"Account: {html.escape(account_name)}{extra}\n\n"
        f"• Sent: {added}\n"
        f"• Skipped: {skipped}\n"
        f"Time: {_format_time(start, end)}"
    )


def format_progress(accounts: list[dict], names: list[str]) -> str:
    lines = ["<b>[ ALL ACCOUNTS ]</b>"]
    for i, acc in enumerate(accounts):
        s = f"{i+1}. {html.escape(names[i])}: {acc['added']} sent, {acc['skipped']} skipped"
        if acc.get("exceeded"):
            s += " <b>(⏳ Quota Exceeded)</b>"
        elif acc.get("no_more"):
            s += " <b>(✅ No More Users)</b>"
        lines.append(s)
    lines.append("\nProcessing… tap Stop to interrupt.")
    return "\n".join(lines)


def _format_result(accounts: list[dict], names: list[str],
                   start: datetime, end: datetime,
                   stopped_by_user: bool = False) -> str:
    header = "<b>[ STOPPED ]</b>" if stopped_by_user else "<b>[ DONE ]</b>"
    lines = [header]
    for i, acc in enumerate(accounts):
        s = f"{i+1}. {html.escape(names[i])}: {acc['added']} sent, {acc['skipped']} skipped"
        if acc.get("exceeded"):
            s += " <b>(⏳ Quota Exceeded)</b>"
        elif acc.get("no_more"):
            s += " <b>(✅ No More Users)</b>"
        lines.append(s)
    lines.append(f"Time: {_format_time(start, end)}")
    return "\n".join(lines)


# ─── Telegram helpers ─────────────────────────────────────────────────────────

async def safe_edit(bot, chat_id, msg_id, text, markup=None) -> None:
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id, text=text,
            reply_markup=markup, parse_mode="HTML",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            logger.warning("safe_edit: %s", e)
    except Exception as e:
        logger.warning("safe_edit unexpected: %s", e)


# ─── Meeff API helpers ────────────────────────────────────────────────────────

async def _fetch_users(session: aiohttp.ClientSession, token: str, user_id=None) -> list:
    url = await get_explore_url(user_id) if user_id is not None else None
    if not url:
        raise MissingExploreUrlError()
    headers = {"meeff-access-token": token, "Connection": "keep-alive"}
    try:
        async with session.get(url, headers=headers) as resp:
            # 429 → quota exceeded for today
            if resp.status == 429:
                raise RequestExceededError()
            body = await resp.json(content_type=None)
            # Quota exceeded returned as 200 with errorCode
            if body.get("errorCode") == "RequestExceeded":
                raise RequestExceededError()
            users = body.get("users", [])
            # Empty list with hasMore=false means no more users available
            if not users and not body.get("hasMore", True):
                raise NoMoreUsersError()
            return users
    except (MissingExploreUrlError, RequestExceededError, NoMoreUsersError):
        raise
    except Exception as e:
        logger.warning("fetch_users error: %s", e)
        return []


async def _push_filters(session: aiohttp.ClientSession, user_id, token: str) -> None:
    filters = await get_user_filters(user_id, token)
    if not filters:
        return
    headers = {
        "User-Agent": "okhttp/4.12.0",
        "Accept-Encoding": "gzip",
        "meeff-access-token": token,
        "content-type": "application/json; charset=utf-8",
    }
    try:
        async with session.post(_MEEFF_FILTER_URL, data=json.dumps(filters), headers=headers) as resp:
            if resp.status != 200:
                logger.warning("Filter push failed: %s", resp.status)
    except Exception as e:
        logger.warning("Filter push error: %s", e)


async def _should_skip(user_id, user_data: dict) -> bool:
    """
    Return True if this user should be skipped.
    Checks blocklist (active mode adds to temporary list) and country filter.
    """
    if await is_blocklist_active(user_id):
        if await atomic_check_and_add_blocklist(user_id, user_data["_id"]):
            return True
    else:
        blocklist = await get_user_blocklist(user_id)
        if user_data["_id"] in blocklist:
            return True

    if not await should_include_user(user_id, user_data):
        return True

    return False


# ─── Core runners ──────────────────────────────────────────────────────────────

async def run_requests_single(user_id, state: dict, bot, token: str,
                               account_name: str, speed: float) -> None:
    start_time = datetime.now()
    added = 0
    skipped = 0
    last_text: str | None = None
    last_update_time = 0
    like_exceeded = False
    no_more_users = False
    sent_since_filter = 0

    async def _update(force: bool = False) -> None:
        nonlocal last_text, last_update_time
        if state.get("finalized"):
            return
        now = time.time()
        text = format_progress_single(account_name, added, skipped)
        if force or (text != last_text and now - last_update_time > UPDATE_INTERVAL):
            last_text = text
            last_update_time = now
            await safe_edit(bot, user_id, state["status_message_id"], text, STOP_MARKUP)

    headers = {"meeff-access-token": token, "Connection": "keep-alive"}
    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        while state.get("running", True):
            try:
                users = await _fetch_users(session, token, user_id)
            except MissingExploreUrlError:
                state["running"] = False
                state["finalized"] = True
                await safe_edit(
                    bot, user_id, state["status_message_id"],
                    "⚠️ <b>No Explore URL saved.</b>\n\n"
                    "Please send your Explore URL in chat to continue.\n"
                    "It should start with: <code>https://api.meeff.com/user/explore/</code>",
                )
                return
            except RequestExceededError:
                state["running"] = False
                like_exceeded = True
                break
            except NoMoreUsersError:
                state["running"] = False
                no_more_users = True
                break
            if not users:
                await asyncio.sleep(2)
                continue

            for user in users:
                if not state.get("running", True):
                    break

                if await _should_skip(user_id, user):
                    skipped += 1
                    await _update()
                    continue

                url = _MEEFF_ANSWER_URL.format(user_id=user["_id"])
                async with session.get(url, headers=headers) as resp:
                    data = await resp.json(content_type=None)

                if data.get("errorCode") == "LikeExceeded":
                    like_exceeded = True
                    state["running"] = False
                    break

                added += 1
                sent_since_filter += 1
                if sent_since_filter >= _FILTER_PUSH_INTERVAL:
                    sent_since_filter = 0
                    await _push_filters(session, user_id, token)

                try:
                    await bot.send_message(user_id, format_user(user), parse_mode="HTML")
                except Exception:
                    pass

                await _update()
                if speed > 0:
                    await asyncio.sleep(speed)

            if speed > 0:
                await asyncio.sleep(speed)

    end_time = datetime.now()
    state["finalized"] = True
    await safe_edit(
        bot, user_id, state["status_message_id"],
        _format_result_single(
            account_name, added, skipped, start_time, end_time,
            like_exceeded=like_exceeded,
            no_more_users=no_more_users,
            stopped_by_user=state.get("stopped_by_user", False),
        ),
    )


async def run_requests_parallel(user_id, bot, tokens: list[dict],
                                 status_message_id: int, state: dict, speed: float) -> None:
    start_time = datetime.now()
    accounts = [{"added": 0, "skipped": 0, "exceeded": False, "no_more": False, "running": True} for _ in tokens]
    names = [tok.get("name", f"Account {i+1}") for i, tok in enumerate(tokens)]
    state["per_account"] = accounts
    state["account_names"] = names
    last_text: str | None = None
    last_update_time = 0

    async def _update(force: bool = False) -> None:
        nonlocal last_text, last_update_time
        if state.get("finalized"):
            return
        now = time.time()
        text = format_progress(accounts, names)
        if force or (text != last_text and now - last_update_time > UPDATE_INTERVAL):
            last_text = text
            last_update_time = now
            await safe_edit(bot, user_id, status_message_id, text, STOP_MARKUP)

    async def _worker(idx: int, token_obj: dict) -> None:
        acc = accounts[idx]
        token = token_obj["token"]
        sent_since_filter = 0
        headers = {"meeff-access-token": token, "Connection": "keep-alive"}
        connector = aiohttp.TCPConnector(limit=10)
        async with aiohttp.ClientSession(connector=connector) as session:
            while acc["running"] and state.get("running", True):
                try:
                    users = await _fetch_users(session, token, user_id)
                except MissingExploreUrlError:
                    acc["running"] = False
                    state["running"] = False
                    await safe_edit(
                        bot, user_id, status_message_id,
                        "⚠️ <b>No Explore URL saved.</b>\n\n"
                        "Please send your Explore URL in chat to continue.\n"
                        "It should start with: <code>https://api.meeff.com/user/explore/</code>",
                    )
                    return
                except RequestExceededError:
                    acc["exceeded"] = True
                    acc["running"] = False
                    await _update(force=True)
                    return
                except NoMoreUsersError:
                    acc["no_more"] = True
                    acc["running"] = False
                    await _update(force=True)
                    return
                if not users:
                    await asyncio.sleep(2)
                    continue

                for user in users:
                    if not acc["running"] or not state.get("running", True):
                        break

                    if await _should_skip(user_id, user):
                        acc["skipped"] += 1
                        await _update()
                        continue

                    url = _MEEFF_ANSWER_URL.format(user_id=user["_id"])
                    async with session.get(url, headers=headers) as resp:
                        data = await resp.json(content_type=None)

                    if data.get("errorCode") == "LikeExceeded":
                        acc["exceeded"] = True
                        acc["running"] = False
                        await _update(force=True)
                        return

                    acc["added"] += 1
                    sent_since_filter += 1
                    if sent_since_filter >= _FILTER_PUSH_INTERVAL:
                        sent_since_filter = 0
                        await _push_filters(session, user_id, token)

                    try:
                        await bot.send_message(user_id, format_user(user), parse_mode="HTML")
                    except Exception:
                        pass

                    await _update()
                    if speed > 0:
                        await asyncio.sleep(speed)

                if speed > 0:
                    await asyncio.sleep(speed)

        await _update(force=True)

    state["finalized"] = False
    await safe_edit(bot, user_id, status_message_id, format_progress(accounts, names), STOP_MARKUP)
    await asyncio.gather(*(_worker(idx, tok) for idx, tok in enumerate(tokens)))
    end_time = datetime.now()
    state["finalized"] = True
    await safe_edit(
        bot, user_id, status_message_id,
        _format_result(accounts, names, start_time, end_time,
                       stopped_by_user=state.get("stopped_by_user", False)),
    )


# ─── Shared start helpers ─────────────────────────────────────────────────────

async def _start_current_run(user_id, state: dict, bot, current_token: str,
                              account_name: str, speed: float, reply_target) -> None:
    state.update({"running": True, "finalized": False, "mode": "current", "skipped_count": 0})
    if hasattr(reply_target, "edit_text"):
        status_msg = await reply_target.edit_text(
            format_progress_single(account_name, 0, 0), reply_markup=STOP_MARKUP, parse_mode="HTML"
        )
    else:
        status_msg = await reply_target.answer(
            format_progress_single(account_name, 0, 0), reply_markup=STOP_MARKUP, parse_mode="HTML"
        )
    state["status_message_id"] = status_msg.message_id
    try:
        await bot.pin_chat_message(chat_id=user_id, message_id=status_msg.message_id)
        state["pinned_message_id"] = status_msg.message_id
    except Exception:
        pass

    await run_requests_single(user_id, state, bot, current_token, account_name, speed)

    pin_id = state.pop("pinned_message_id", None)
    if pin_id:
        try:
            await bot.unpin_chat_message(chat_id=user_id, message_id=pin_id)
        except Exception:
            pass
    _cleanup_state(state)


async def _start_all_run(user_id, state: dict, bot, tokens: list[dict],
                          speed: float, reply_target) -> None:
    state.update({"running": True, "finalized": False, "mode": "all"})
    init_names = [tok.get("name", f"Account {i+1}") for i, tok in enumerate(tokens)]
    init_accounts = [{"added": 0, "skipped": 0, "exceeded": False, "no_more": False} for _ in tokens]
    if hasattr(reply_target, "edit_text"):
        status_msg = await reply_target.edit_text(
            format_progress(init_accounts, init_names), reply_markup=STOP_MARKUP, parse_mode="HTML"
        )
    else:
        status_msg = await reply_target.answer(
            format_progress(init_accounts, init_names), reply_markup=STOP_MARKUP, parse_mode="HTML"
        )
    try:
        await bot.pin_chat_message(chat_id=user_id, message_id=status_msg.message_id)
        state["pinned_message_id"] = status_msg.message_id
    except Exception:
        pass

    await run_requests_parallel(user_id, bot, tokens, status_msg.message_id, state, speed)

    pin_id = state.pop("pinned_message_id", None)
    if pin_id:
        try:
            await bot.unpin_chat_message(chat_id=user_id, message_id=pin_id)
        except Exception:
            pass
    _cleanup_state(state)


def _cleanup_state(state: dict) -> None:
    state["running"] = False
    for key in ("mode", "stopped_by_user", "per_account", "account_names"):
        state.pop(key, None)


# ─── Custom speed message handler ────────────────────────────────────────────

async def handle_custom_speed_message(message, state: dict, bot, get_tokens, get_current_account) -> None:
    user_id = message.from_user.id
    try:
        speed = float(message.text.strip())
        if not (0.01 <= speed <= 30):
            await message.reply("Please enter a value between 0.01 and 30 seconds. Send /cancel to abort.")
            return
    except (ValueError, TypeError):
        await message.reply("Invalid speed. Send a number like 1.5. Send /cancel to abort.")
        return

    state.pop("awaiting_custom_speed", None)
    mode = state.pop("pending_speed_mode", None)

    if mode == "current":
        current_token = await get_current_account(user_id)
        account_name = state.pop("pending_account_name", "Current")
        await _start_current_run(user_id, state, bot, current_token, account_name, speed, message)

    elif mode == "all":
        tokens = await get_tokens(user_id)
        if not tokens:
            await message.reply("No accounts found.\nAdd a token first.")
            return
        await _start_all_run(user_id, state, bot, tokens, speed, message)

    else:
        await message.reply("Speed selection is not valid here.")


# ─── Callback handler ─────────────────────────────────────────────────────────

async def handle_requests_callback(
    callback_query, state: dict, bot, user_id,
    get_current_account, get_tokens, set_current_account, start_markup
) -> bool:
    data = callback_query.data

    async def edit(text: str, markup=None) -> None:
        await callback_query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await callback_query.answer()

    if data == "start":
        await edit("<b>Start Requests</b>\n\nChoose which accounts to run:", REQUESTS_CHOICE_MARKUP)
        return True

    if data == "requests_all":
        tokens = await get_tokens(user_id)
        if not tokens:
            await edit("No accounts found.\nAdd a token first.", start_markup)
            return True
        account_list = "\n".join(
            f"{i+1}. {html.escape(tok.get('name', f'Account {i+1}'))}"
            for i, tok in enumerate(tokens)
        )
        await edit(
            f"<b>Run on All Accounts</b>\n\n"
            f"Runs simultaneously on every active account.\n\n<b>Accounts:</b>\n{account_list}",
            REQUESTS_ALL_CONFIRM_MARKUP,
        )
        state["pending_requests_all"] = True
        return True

    if data == "requests_confirm":
        if not state.get("pending_requests_all"):
            await callback_query.answer("Nothing to confirm.")
            return True
        state["pending_speed_mode"] = "all"
        state.pop("pending_requests_all", None)
        await edit("<b>Select Speed</b>\n\nHow fast should requests be sent?", get_speed_markup())
        return True

    if data == "requests_current":
        if state.get("running"):
            await callback_query.answer("Requests are already running!")
            return True
        tokens = await get_tokens(user_id)
        if not tokens:
            await edit("No accounts found.\nAdd a token first.", start_markup)
            return True
        current_token = await get_current_account(user_id)
        if not current_token:
            await edit("No active account selected.\nGo to Tools → Accounts and pick one.", start_markup)
            return True
        account_name = next(
            (tok.get("name", "Current") for tok in tokens if tok["token"] == current_token), "Current"
        )
        state["pending_speed_mode"] = "current"
        state["pending_account_name"] = account_name
        await edit("<b>Select Speed</b>\n\nHow fast should requests be sent?", get_speed_markup())
        return True

    if data == "speed_custom":
        state["awaiting_custom_speed"] = True
        await edit(
            "<b>Custom Speed</b>\n\nSend your delay in seconds (e.g. <code>2.0</code>).\n"
            "Range: 0.01–30\n\nSend /cancel to abort.",
        )
        return True

    if data == "requests_cancel":
        for key in ("pending_requests_all", "pending_speed_mode", "pending_account_name", "awaiting_custom_speed"):
            state.pop(key, None)
        await edit("Welcome! Choose an action below.", start_markup)
        return True

    if data == "stop":
        if not state.get("running"):
            await callback_query.answer("No requests are running.")
            return True
        if state.get("finalized"):
            await callback_query.answer("Already stopped.")
            return True
        state["finalized"] = True
        state["running"] = False
        state["stopped_by_user"] = True
        pin_id = state.pop("pinned_message_id", None)
        if pin_id:
            try:
                await bot.unpin_chat_message(chat_id=user_id, message_id=pin_id)
            except Exception:
                pass
        await callback_query.answer("Stopping…")
        return True

    if data.startswith("speed_"):
        selected = data.split("_", 1)[1]
        if selected not in SPEED_LEVELS:
            await edit("Unknown speed selected.")
            return True
        speed_value = SPEED_LEVELS[selected][1]
        mode = state.pop("pending_speed_mode", None)

        if mode == "current":
            current_token = await get_current_account(user_id)
            account_name = state.pop("pending_account_name", "Current")
            tokens = await get_tokens(user_id)
            if not tokens:
                await edit("No accounts found.\nAdd a token first.", start_markup)
                return True
            if not current_token:
                await edit("No active account selected.\nGo to Tools → Accounts and pick one.", start_markup)
                return True
            await callback_query.answer()
            await _start_current_run(user_id, state, bot, current_token, account_name, speed_value,
                                      callback_query.message)
            return True

        if mode == "all":
            tokens = await get_tokens(user_id)
            if not tokens:
                await edit("No accounts found.\nAdd a token first.")
                return True
            await callback_query.answer()
            await _start_all_run(user_id, state, bot, tokens, speed_value, callback_query.message)
            return True

        await edit("Speed selection is not valid here.")
        return True

    return False
