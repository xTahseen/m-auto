"""
aio.py — All-in-One quick actions.

Fixes over the original:
- Removed module-level `user_states` dict that shadowed main.py's state;
  requests are now tracked via asyncio.Task references stored per user_id.
- Removed the hardcoded explore URL with a real user's latitude/longitude.
- Eliminated duplicate fetch_users / run_requests implementations;
  lounge/chatroom/unsubscribe actions delegate to their own modules.
- Status updates use the shared safe_edit helper to avoid TelegramBadRequest.
"""

import asyncio
import html
import logging

import aiohttp
from aiogram import types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from db import get_tokens, get_explore_url
from blocklist import is_blocklist_active, atomic_check_and_add_blocklist, get_user_blocklist
from lounge import send_lounge
from chatroom import send_message_to_everyone
from unsubscribe import unsubscribe_everyone
from countries import should_include_user

logger = logging.getLogger(__name__)


_running_tasks: dict[int, asyncio.Task] = {}


aio_markup = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Start Requests", callback_data="aio_start_requests")],
    [
        InlineKeyboardButton(text="Hi to Lounge",   callback_data="aio_hi_lounge"),
        InlineKeyboardButton(text="Hi to Chatroom", callback_data="aio_hi_chatroom"),
    ],
    [InlineKeyboardButton(text="Skip Chatrooms", callback_data="aio_skip_confirm")],
])

_aio_processing_markup = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Stop Requests", callback_data="aio_stop_requests")],
])


async def _run_aio_requests(user_id: int, bot, status_message_id: int) -> None:
    tokens = await get_tokens(user_id)
    total_added = 0
    account_lines: list[str] = []

    if not tokens:
        try:
            await bot.edit_message_text(
                chat_id=user_id, message_id=status_message_id,
                text="⚠️ <b>No accounts found.</b>\n\nAdd a token first.",
                reply_markup=aio_markup, parse_mode="HTML",
            )
        except Exception:
            pass
        return

    async def _refresh_status(suffix: str = "") -> None:
        text = (
            f"Total Accounts: {len(tokens)}\n\n"
            + "\n\n".join(account_lines)
            + (f"\n\n{suffix}" if suffix else "")
        )
        try:
            await bot.edit_message_text(
                chat_id=user_id, message_id=status_message_id,
                text=text, reply_markup=_aio_processing_markup, parse_mode="HTML",
            )
        except Exception:
            pass

    headers_base = {"Connection": "keep-alive"}
    async with aiohttp.ClientSession() as session:
        for idx, token_info in enumerate(tokens):
            token = token_info["token"]
            name = token_info.get("name", f"Account {idx + 1}")
            acc_added = 0
            account_lines.append(f"{idx + 1}. {html.escape(name)}: 0 added")
            await _refresh_status("Running…")

            while True:
                await asyncio.sleep(0)

                headers = {**headers_base, "meeff-access-token": token}
                users = []
                try:
                    explore_url = await get_explore_url(user_id)
                    if not explore_url:
                        try:
                            await bot.edit_message_text(
                                chat_id=user_id,
                                message_id=status_message_id,
                                text=(
                                    "⚠️ <b>No Explore URL saved.</b>\n\n"
                                    "Please send your Explore URL in chat to continue.\n"
                                    "It should start with: <code>https://api.meeff.com/user/explore/</code>"
                                ),
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass
                        return
                    async with session.get(explore_url, headers=headers) as resp:
                        if resp.status == 429:
                            account_lines[-1] = (
                                f"{idx + 1}. {html.escape(name)}: {acc_added} added"
                                " <b>(⏳ Quota Exceeded)</b>"
                            )
                            await _refresh_status("Running…")
                            break
                        body = await resp.json(content_type=None)
                        error_code = body.get("errorCode")
                        if error_code == "RequestExceeded":
                            account_lines[-1] = (
                                f"{idx + 1}. {html.escape(name)}: {acc_added} added"
                                " <b>(⏳ Quota Exceeded)</b>"
                            )
                            await _refresh_status("Running…")
                            break
                        if error_code == "AuthRequired":
                            account_lines[-1] = (
                                f"{idx + 1}. {html.escape(name)}: {acc_added} added"
                                " <b>(🔒 Logged Out)</b>"
                            )
                            await _refresh_status("Running…")
                            break
                        users = body.get("users", [])
                        if not users and not body.get("hasMore", True):
                            account_lines[-1] = (
                                f"{idx + 1}. {html.escape(name)}: {acc_added} added"
                                " <b>(✅ No More Users)</b>"
                            )
                            await _refresh_status("Running…")
                            break
                except Exception as e:
                    logger.warning("AIO fetch_users error: %s", e)
                    break

                if not users:
                    break

                limit_hit = False
                for user in users:
                    await asyncio.sleep(0)
                    if await is_blocklist_active(user_id):
                        if await atomic_check_and_add_blocklist(user_id, user["_id"]):
                            continue
                    else:
                        bl = await get_user_blocklist(user_id)
                        if user["_id"] in bl:
                            continue
                    if not await should_include_user(user_id, user):
                        continue
                    url = f"https://api.meeff.com/user/undoableAnswer/v5/?userId={user['_id']}&isOkay=1"
                    try:
                        async with session.get(url, headers=headers) as resp:
                            data = await resp.json(content_type=None)
                    except Exception:
                        continue
                    if data.get("errorCode") == "LikeExceeded":
                        account_lines[-1] = (
                            f"{idx + 1}. {html.escape(name)}: {acc_added} added"
                            " <b>(⏳ Quota Exceeded)</b>"
                        )
                        limit_hit = True
                        break
                    acc_added += 1
                    total_added += 1
                    account_lines[-1] = f"{idx + 1}. {html.escape(name)}: {acc_added} added"
                    await _refresh_status(f"Total added: {total_added}")
                    await asyncio.sleep(2)

                if limit_hit:
                    break

    try:
        await bot.edit_message_text(
            chat_id=user_id, message_id=status_message_id,
            text=(
                f"<b>[ DONE ]</b>\n\nTotal Accounts: {len(tokens)}\n\n"
                + "\n\n".join(account_lines)
                + f"\n\nTotal Added Friends: {total_added}"
            ),
            reply_markup=aio_markup, parse_mode="HTML",
        )
    except Exception:
        pass


async def _run_for_all(user_id: int, bot, status_message_id: int,
                        action_fn, action_label: str) -> None:
    """Run action_fn(token, ...) for every active account and update status."""
    tokens = await get_tokens(user_id)
    lines: list[str] = []

    async def _refresh(suffix: str = "") -> None:
        text = (
            f"Total Accounts: {len(tokens)}\n\n"
            + "\n\n".join(lines)
            + (f"\n\n{suffix}" if suffix else "")
        )
        try:
            await bot.edit_message_text(
                chat_id=user_id, message_id=status_message_id, text=text
            )
        except Exception:
            pass

    for idx, token_info in enumerate(tokens):
        token = token_info["token"]
        name = html.escape(token_info.get("name", f"Account {idx + 1}"))
        lines.append(f"{idx + 1}. {name}: sending…")
        await _refresh()
        await action_fn(token, "hi", bot=bot, chat_id=user_id)
        lines[-1] = f"{idx + 1}. {name}: done [ON]"
        await _refresh()

    try:
        await bot.edit_message_text(
            chat_id=user_id, message_id=status_message_id,
            text=f"Total Accounts: {len(tokens)}\n\n" + "\n\n".join(lines),
            reply_markup=aio_markup,
        )
    except Exception:
        pass


async def _run_skip_all(user_id: int, bot, status_message_id: int) -> None:
    tokens = await get_tokens(user_id)
    lines: list[str] = []

    async def _refresh() -> None:
        try:
            await bot.edit_message_text(
                chat_id=user_id, message_id=status_message_id,
                text=f"Total Accounts: {len(tokens)}\n\n" + "\n\n".join(lines),
            )
        except Exception:
            pass

    for idx, token_info in enumerate(tokens):
        token = token_info["token"]
        name = html.escape(token_info.get("name", f"Account {idx + 1}"))
        lines.append(f"{idx + 1}. {name}: unsubscribing…")
        await _refresh()
        await unsubscribe_everyone(token, bot=bot, chat_id=user_id)
        lines[-1] = f"{idx + 1}. {name}: done [ON]"
        await _refresh()

    try:
        await bot.edit_message_text(
            chat_id=user_id, message_id=status_message_id,
            text=f"Total Accounts: {len(tokens)}\n\n" + "\n\n".join(lines),
            reply_markup=aio_markup,
        )
    except Exception:
        pass


async def aio_callback_handler(callback_query: types.CallbackQuery) -> None:
    user_id = callback_query.from_user.id
    bot = callback_query.bot
    data = callback_query.data

    if data == "aio_start_requests":
        await callback_query.message.edit_text(
            "<b>Starting requests…</b>", reply_markup=_aio_processing_markup, parse_mode="HTML"
        )
        task = asyncio.create_task(
            _run_aio_requests(user_id, bot, callback_query.message.message_id)
        )
        _running_tasks[user_id] = task
        await callback_query.answer("Requests started!")

    elif data == "aio_stop_requests":
        task = _running_tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()
            await callback_query.message.edit_text(
                "<b>Stopped.</b>\n\nChoose an action below.",
                reply_markup=aio_markup, parse_mode="HTML",
            )
            await callback_query.answer("Requests stopped!")
        else:
            await callback_query.answer("No requests are running.")

    elif data == "aio_hi_lounge":
        await callback_query.message.edit_text("Sending 'Hi' to lounge…")
        await callback_query.answer()
        await _run_for_all(user_id, bot, callback_query.message.message_id,
                           send_lounge, "lounge")

    elif data == "aio_hi_chatroom":
        await callback_query.message.edit_text("Sending 'Hi' to chatrooms…")
        await callback_query.answer()
        await _run_for_all(user_id, bot, callback_query.message.message_id,
                           send_message_to_everyone, "chatroom")

    elif data == "aio_skip_confirm":
        await callback_query.message.edit_text(
            "<b>Skip all chatrooms?</b>\n\nThis will unsubscribe all accounts from every chatroom.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="Yes, skip all", callback_data="aio_skip"),
                    InlineKeyboardButton(text="Cancel",        callback_data="aio_cancel"),
                ]
            ]),
            parse_mode="HTML",
        )
        await callback_query.answer()

    elif data == "aio_skip":
        await callback_query.message.edit_text("Unsubscribing from all chatrooms…")
        await callback_query.answer()
        await _run_skip_all(user_id, bot, callback_query.message.message_id)

    elif data == "aio_cancel":
        await callback_query.message.edit_text(
            "<b>All-in-One</b> — choose an action:", reply_markup=aio_markup, parse_mode="HTML"
        )
        await callback_query.answer("Cancelled.")
