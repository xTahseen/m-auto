import aiohttp
import asyncio
import logging
import html
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

LOUNGE_URL = "https://api.meeff.com/lounge/dashboard/v1"
CHATROOM_URL = "https://api.meeff.com/chatroom/open/v2"
SEND_MESSAGE_URL = "https://api.meeff.com/chat/send/v2"
HEADERS = {
    'User-Agent': "okhttp/4.12.0",
    'Accept-Encoding': "gzip",
    'content-type': "application/json; charset=utf-8"
}

# --- Helpers for API Calls ---

async def fetch_lounge_users(token):
    headers = {**HEADERS, 'meeff-access-token': token}
    params = {'locale': "en"}
    async with aiohttp.ClientSession() as session:
        async with session.get(LOUNGE_URL, params=params, headers=headers) as response:
            if response.status != 200:
                logging.error(f"Failed to fetch lounge users: {response.status}")
                return []
            return (await response.json()).get("both", [])

async def open_chatroom(token, user_id):
    headers = {**HEADERS, 'meeff-access-token': token}
    payload = {"waitingRoomId": user_id, "locale": "en"}
    async with aiohttp.ClientSession() as session:
        async with session.post(CHATROOM_URL, json=payload, headers=headers) as response:
            if response.status in (412, 401):
                logging.error(f"Failed to open chatroom: {response.status}")
                return None
            if response.status != 200:
                logging.error(f"Failed to open chatroom: {response.status}")
                return None
            return (await response.json()).get("chatRoom", {}).get("_id")

async def send_message(token, chatroom_id, message):
    headers = {**HEADERS, 'meeff-access-token': token}
    payload = {"chatRoomId": chatroom_id, "message": message, "locale": "en"}
    async with aiohttp.ClientSession() as session:
        async with session.post(SEND_MESSAGE_URL, json=payload, headers=headers) as response:
            if response.status != 200:
                logging.error(f"Failed to send message: {response.status}")
                return None
            return await response.json()

# --- Main Feature: Send message to all lounge users ---

async def handle_user(token, user, messages, bot, chat_id, status_message):
    user_id = user["user"]["_id"]
    chatroom_id = await open_chatroom(token, user_id)
    if chatroom_id:
        for message in messages:
            await send_message(token, chatroom_id, message.strip())
        logging.info(f"Sent messages to {user['user'].get('name', 'Unknown User')} in chatroom {chatroom_id}.")
        return True
    return False

async def send_lounge(token, messages="hi", status_message=None, bot=None, chat_id=None):
    if isinstance(messages, str):
        messages = [msg.strip() for msg in messages.split(",")]
    sent_count, total_users = 0, 0
    while True:
        users = await fetch_lounge_users(token)
        if not users:
            logging.info("No users found in the lounge.")
            break
        total_users += len(users)
        results = await asyncio.gather(*[
            handle_user(token, user, messages, bot, chat_id, status_message)
            for user in users
        ])
        sent_count += sum(results)
        disabled_users = len(users) - sum(results)
        if bot and chat_id and status_message:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_message.message_id,
                text=f"Lounge Users: {total_users} Messages sent: {sent_count}",
            )
        if disabled_users == len(users):
            logging.info("All users in the lounge are disabled.")
            break
    logging.info(f"Finished sending messages. Total Lounge Users: {total_users}, Messages sent: {sent_count}")
    return sent_count

# --- Command Handler for /lounge ---

async def lounge_command_handler(message, has_valid_access, get_current_account, user_states):
    user_id = message.chat.id
    if not has_valid_access(user_id):
        await message.reply("You are not authorized to use this bot.")
        return
    token = await get_current_account(user_id)
    if not token:
        await message.reply("No active account found. Please set an account before sending messages.")
        return
    command_text = message.text.strip()
    if len(command_text.split()) < 2:
        await message.reply("Please provide a message to send. Usage: /lounge <message>")
        return
    messages = [msg.strip() for msg in " ".join(command_text.split()[1:]).split(",") if msg.strip()]
    state = user_states[user_id]
    state['pending_lounge_message'] = messages
    msg_lines = "\n".join(f"<b>Message:</b> {html.escape(m)}" for m in messages)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Current", callback_data="lounge_current"),
            InlineKeyboardButton(text="All", callback_data="lounge_all")
        ],
        [InlineKeyboardButton(text="Cancel", callback_data="lounge_cancel")]
    ])
    await message.reply(
        f"How would you like to send your lounge message?\n\n{msg_lines}",
        reply_markup=markup,
        parse_mode="HTML"
    )

# --- Callback Handler for lounge actions ---

async def handle_lounge_callback(
    callback_query, state, bot, user_id,
    get_current_account, get_tokens, send_lounge_fn
):
    data = callback_query.data
    if data == "lounge_current":
        token = await get_current_account(user_id)
        tokens = await get_tokens(user_id)
        account_name = next((t["name"] for t in tokens if t["token"] == token), "Unknown")
        status_message = await callback_query.message.edit_text(
            f"<b>Account:</b> {html.escape(account_name)}\nMessages sending...", parse_mode="HTML"
        )
        messages = state.get('pending_lounge_message', [])
        sent_count = await send_lounge_fn(token, messages, status_message=status_message, bot=bot, chat_id=user_id) or 0
        await status_message.edit_text(
            f"<b>Account:</b> {html.escape(account_name)}\nMessages sent successfully ({sent_count}).",
            parse_mode="HTML"
        )
        state.pop('pending_lounge_message', None)
        await callback_query.answer("Messages sent.")
        return True

    elif data == "lounge_all":
        tokens = await get_tokens(user_id)
        acc_line = "<b>Accounts:</b> " + ", ".join(html.escape(t["name"]) for t in tokens)
        confirm_markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Confirm", callback_data="lounge_confirm")],
            [InlineKeyboardButton(text="Cancel", callback_data="lounge_cancel")]
        ])
        await callback_query.message.edit_text(
            f"You chose to send the lounge message to:\n{acc_line}\n\nPress Confirm to proceed.",
            reply_markup=confirm_markup,
            parse_mode="HTML"
        )
        state['lounge_send_type'] = "lounge_all"
        await callback_query.answer()
        return True

    elif data == "lounge_confirm":
        messages = state.get('pending_lounge_message', [])
        status_message = await callback_query.message.edit_text("Sending messages, please wait...")
        tokens = await get_tokens(user_id)
        if not tokens:
            await status_message.edit_text("No accounts found.")
            return True
        per_account_counts, total_sent = [], 0
        for idx, token_info in enumerate(tokens):
            account_name = token_info.get('name', f"Account {idx+1}")
            await status_message.edit_text(
                f"<b>Account:</b> {html.escape(account_name)}\nMessages sending...", parse_mode="HTML"
            )
            sent_count = await send_lounge_fn(
                token_info["token"], messages, status_message=status_message, bot=bot, chat_id=user_id
            ) or 0
            per_account_counts.append(sent_count)
            total_sent += sent_count
            await status_message.edit_text(
                f"<b>Account:</b> {html.escape(account_name)}\nMessages sent successfully ({sent_count}).",
                parse_mode="HTML"
            )
        summary_text = (
            f"Total Account: {len(tokens)}\n"
            f"Messages sent successfully ({total_sent}).\n"
            f"({' | '.join(str(x) for x in per_account_counts)})"
        )
        await status_message.edit_text(summary_text, parse_mode="HTML")
        state.pop('lounge_send_type', None)
        state.pop('pending_lounge_message', None)
        await callback_query.answer("Messages sent.")
        return True

    elif data == "lounge_cancel":
        state.pop('lounge_send_type', None)
        state.pop('pending_lounge_message', None)
        await callback_query.message.edit_text("Lounge message sending cancelled.")
        await callback_query.answer("Cancelled.")
        return True

    return False
