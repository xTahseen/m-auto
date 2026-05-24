import aiohttp
import asyncio
import logging
import html
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

UNSUBSCRIBE_URL = "https://api.meeff.com/chatroom/unsubscribe/v1"
CHATROOM_URL = "https://api.meeff.com/chatroom/dashboard/v1"
MORE_CHATROOMS_URL = "https://api.meeff.com/chatroom/more/v1"
HEADERS = {
    'User-Agent': "okhttp/4.12.0",
    'Accept-Encoding': "gzip",
    'content-type': "application/json; charset=utf-8"
}

# --- Helpers for API Calls ---

async def fetch_chatrooms(session, token, from_date=None):
    headers = {**HEADERS, 'meeff-access-token': token}
    params = {'locale': "en"}
    if from_date:
        params['fromDate'] = from_date
    async with session.get(CHATROOM_URL, params=params, headers=headers) as resp:
        if resp.status != 200:
            logging.error(f"Failed to fetch chatrooms: {resp.status}")
            return [], None
        data = await resp.json()
        return data.get("rooms", []), data.get("next")

async def fetch_more_chatrooms(session, token, from_date):
    headers = {**HEADERS, 'meeff-access-token': token}
    payload = {"fromDate": from_date, "locale": "en"}
    async with session.post(MORE_CHATROOMS_URL, json=payload, headers=headers) as resp:
        if resp.status != 200:
            logging.error(f"Failed to fetch more chatrooms: {resp.status}")
            return [], None
        data = await resp.json()
        return data.get("rooms", []), data.get("next")

async def unsubscribe_chatroom(session, token, chatroom_id):
    headers = {**HEADERS, 'meeff-access-token': token}
    payload = {"chatRoomId": chatroom_id, "locale": "en"}
    async with session.post(UNSUBSCRIBE_URL, json=payload, headers=headers) as resp:
        if resp.status != 200:
            logging.error(f"Failed to unsubscribe chatroom: {resp.status}")
            return None
        return await resp.json()

# --- Main Feature: Unsubscribe from all chatrooms ---

async def unsubscribe_everyone(token, status_message=None, bot=None, chat_id=None):
    total_unsubscribed, from_date = 0, None
    connector = aiohttp.TCPConnector(limit=30)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            chatrooms, next_from_date = (
                await fetch_chatrooms(session, token)
                if from_date is None else
                await fetch_more_chatrooms(session, token, from_date)
            )
            if not chatrooms:
                logging.info("No more chatrooms found.")
                break
            tasks = [unsubscribe_chatroom(session, token, c["_id"]) for c in chatrooms]
            await asyncio.gather(*tasks)
            total_unsubscribed += len(tasks)
            if bot and chat_id and status_message:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_message.message_id,
                    text=f"Total chatrooms unsubscribed: {total_unsubscribed}",
                )
            logging.info(f"Unsubscribed {len(tasks)} chatrooms.")
            if not next_from_date:
                break
            from_date = next_from_date
    logging.info(f"Finished unsubscribing. Total chatrooms unsubscribed: {total_unsubscribed}")
    if bot and chat_id and status_message:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_message.message_id,
            text=f"Finished unsubscribing. Total chatrooms unsubscribed: {total_unsubscribed}"
        )
    return total_unsubscribed

# --- Command Handler for /skip ---

async def unsubscribe_command_handler(message, has_valid_access, get_current_account, get_tokens, user_states):
    user_id = message.chat.id
    if not has_valid_access(user_id):
        await message.reply("You are not authorized to use this bot.")
        return
    tokens = await get_tokens(user_id)
    if not tokens:
        await message.reply("No accounts found. Please add an account first.")
        return
    token = await get_current_account(user_id)
    if not token:
        await message.reply("No active account found. Please set an account before unsubscribing.")
        return
    state = user_states[user_id]
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Current", callback_data="unsubscribe_current"),
            InlineKeyboardButton(text="All", callback_data="unsubscribe_all")
        ],
        [InlineKeyboardButton(text="Cancel", callback_data="unsubscribe_cancel")]
    ])
    await message.reply("How would you like to unsubscribe from chatrooms?", reply_markup=markup)

# --- Callback Handler for unsubscribe actions ---

async def handle_unsubscribe_callback(
    callback_query, state, bot, user_id,
    get_current_account, get_tokens, unsubscribe_everyone_fn
):
    data = callback_query.data
    if data == "unsubscribe_current":
        await callback_query.answer("Processing...")
        token = await get_current_account(user_id)
        tokens = await get_tokens(user_id)
        account_name = next((t["name"] for t in tokens if t["token"] == token), "Unknown")
        status_message = await callback_query.message.edit_text(
            f"<b>Account:</b> {html.escape(account_name)}\nUnsubscribing from chatrooms...", parse_mode="HTML"
        )
        unsub_count = await unsubscribe_everyone_fn(token, status_message=status_message, bot=bot, chat_id=user_id)
        await status_message.edit_text(
            f"<b>Account:</b> {html.escape(account_name)}\nUnsubscribed from {unsub_count} chatrooms.",
            parse_mode="HTML"
        )
        return True

    elif data == "unsubscribe_all":
        await callback_query.answer()
        tokens = await get_tokens(user_id)
        acc_line = "<b>Accounts:</b> " + ", ".join(html.escape(t["name"]) for t in tokens)
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Confirm", callback_data="unsubscribe_confirm")],
            [InlineKeyboardButton(text="Cancel", callback_data="unsubscribe_cancel")]
        ])
        await callback_query.message.edit_text(
            f"You chose to unsubscribe from all chatrooms in:\n{acc_line}\n\nPress Confirm to proceed.",
            reply_markup=markup,
            parse_mode="HTML"
        )
        state['unsubscribe_send_type'] = "unsubscribe_all"
        return True

    elif data == "unsubscribe_confirm":
        await callback_query.answer("Processing...")
        status_message = await callback_query.message.edit_text("Unsubscribing from all chatrooms, please wait...")
        tokens = await get_tokens(user_id)
        if not tokens:
            await status_message.edit_text("No accounts found.")
            return True
        per_account_counts, total_unsubscribed = [], 0
        for idx, token_info in enumerate(tokens):
            account_name = token_info.get('name', f"Account {idx+1}")
            await status_message.edit_text(
                f"<b>Account:</b> {html.escape(account_name)}\nUnsubscribing from chatrooms...",
                parse_mode="HTML"
            )
            unsub_count = await unsubscribe_everyone_fn(token_info["token"], status_message=status_message, bot=bot, chat_id=user_id)
            per_account_counts.append(unsub_count)
            total_unsubscribed += unsub_count
            await status_message.edit_text(
                f"<b>Account:</b> {html.escape(account_name)}\nUnsubscribed from {unsub_count} chatrooms.",
                parse_mode="HTML"
            )
        summary_text = (
            f"Total Account: {len(tokens)}\n"
            f"Unsubscribed from {total_unsubscribed} chatrooms.\n"
            f"({' | '.join(str(x) for x in per_account_counts)})"
        )
        await status_message.edit_text(summary_text, parse_mode="HTML")
        state.pop('unsubscribe_send_type', None)
        return True

    elif data == "unsubscribe_cancel":
        await callback_query.answer("Cancelled.")
        state.pop('unsubscribe_send_type', None)
        await callback_query.message.edit_text("Unsubscribe operation cancelled.")
        return True

    return False
