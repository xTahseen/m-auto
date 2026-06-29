import aiohttp
import asyncio
import logging
import html
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

CHATROOM_URL = "https://api.meeff.com/chatroom/dashboard/v1"
MORE_CHATROOMS_URL = "https://api.meeff.com/chatroom/more/v1"
SEND_MESSAGE_URL = "https://api.meeff.com/chat/send/v2"
HEADERS = {
    'User-Agent': "okhttp/4.12.0",
    'Accept-Encoding': "gzip",
    'content-type': "application/json; charset=utf-8"
}


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

async def send_message(session, token, chatroom_id, message):
    headers = {**HEADERS, 'meeff-access-token': token}
    payload = {"chatRoomId": chatroom_id, "message": message, "locale": "en"}
    async with session.post(SEND_MESSAGE_URL, json=payload, headers=headers) as resp:
        if resp.status != 200:
            logging.error(f"Failed to send message: {resp.status}")
            return None
        return await resp.json()


async def send_message_to_everyone(token, messages, status_message=None, bot=None, chat_id=None):
    if isinstance(messages, str):
        messages = [msg.strip() for msg in messages.split(",") if msg.strip()]
    sent_count, total_chatrooms, from_date = 0, 0, None
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
            total_chatrooms += len(chatrooms)
            send_tasks = [
                send_message(session, token, chatroom["_id"], msg)
                for chatroom in chatrooms for msg in messages
            ]
            await asyncio.gather(*send_tasks)
            sent_count += len(send_tasks)
            if bot and chat_id and status_message:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_message.message_id,
                    text=f"Chatrooms: {total_chatrooms} Messages sent: {sent_count}",
                )
            logging.info(f"Sent messages to {len(chatrooms)} chatrooms.")
            if not next_from_date:
                break
            from_date = next_from_date
    logging.info(f"Finished sending messages. Total Chatrooms: {total_chatrooms}, Messages sent: {sent_count}")
    if bot and chat_id and status_message:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_message.message_id,
            text=f"Finished sending messages. Total Chatrooms: {total_chatrooms}, Messages sent: {sent_count}"
        )
    return sent_count


async def chatroom_command_handler(message, has_valid_access, get_current_account, get_tokens, user_states):
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
        await message.reply("No active account found. Please set an account before sending messages.")
        return
    command_text = message.text.strip()
    if len(command_text.split()) < 2:
        await message.reply("Please provide a message to send. Usage: /chatroom <message>")
        return
    messages = [msg.strip() for msg in " ".join(command_text.split()[1:]).split(",") if msg.strip()]
    state = user_states[user_id]
    state['pending_chatroom_message'] = messages
    msg_lines = "\n".join(f"<b>Message:</b> {html.escape(m)}" for m in messages)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Current", callback_data="chatroom_current"),
            InlineKeyboardButton(text="All", callback_data="chatroom_all")
        ],
        [InlineKeyboardButton(text="Cancel", callback_data="chatroom_cancel")]
    ])
    await message.reply(
        f"How would you like to send your chatroom message?\n\n{msg_lines}",
        reply_markup=markup,
        parse_mode="HTML"
    )


async def handle_chatroom_callback(
    callback_query, state, bot, user_id,
    get_current_account, get_tokens, send_message_to_everyone_fn
):
    data = callback_query.data
    if data == "chatroom_current":
        await callback_query.answer("Processing...")
        token = await get_current_account(user_id)
        tokens = await get_tokens(user_id)
        account_name = next((t["name"] for t in tokens if t["token"] == token), "Unknown")
        status_message = await callback_query.message.edit_text(
            f"<b>Account:</b> {html.escape(account_name)}\nMessages sending...", parse_mode="HTML"
        )
        messages = state.get('pending_chatroom_message', [])
        sent_count = await send_message_to_everyone_fn(token, messages, status_message=status_message, bot=bot, chat_id=user_id)
        await status_message.edit_text(
            f"<b>Account:</b> {html.escape(account_name)}\nMessages sent successfully ({sent_count}).",
            parse_mode="HTML"
        )
        state.pop('pending_chatroom_message', None)
        return True

    elif data == "chatroom_all":
        await callback_query.answer()
        tokens = await get_tokens(user_id)
        acc_line = "<b>Accounts:</b> " + ", ".join(html.escape(t["name"]) for t in tokens)
        confirm_markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Confirm", callback_data="chatroom_confirm")],
            [InlineKeyboardButton(text="Cancel", callback_data="chatroom_cancel")]
        ])
        await callback_query.message.edit_text(
            f"You chose to send the chatroom message to:\n{acc_line}\n\nPress Confirm to proceed.",
            reply_markup=confirm_markup,
            parse_mode="HTML"
        )
        state['chatroom_send_type'] = "chatroom_all"
        return True

    elif data == "chatroom_confirm":
        await callback_query.answer("Processing...")
        messages = state.get('pending_chatroom_message', [])
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
            sent_count = await send_message_to_everyone_fn(
                token_info["token"], messages, status_message=status_message, bot=bot, chat_id=user_id
            )
            per_account_counts.append(sent_count)
            total_sent += sent_count
            await status_message.edit_text(
                f"<b>Account:</b> {html.escape(account_name)}\nMessages sent successfully ({sent_count}).", parse_mode="HTML"
            )
        summary_text = (
            f"Total Account: {len(tokens)}\n"
            f"Messages sent successfully ({total_sent}).\n"
            f"({' | '.join(str(x) for x in per_account_counts)})"
        )
        await status_message.edit_text(summary_text, parse_mode="HTML")
        state.pop('chatroom_send_type', None)
        state.pop('pending_chatroom_message', None)
        return True

    elif data == "chatroom_cancel":
        await callback_query.answer("Cancelled.")
        state.pop('chatroom_send_type', None)
        state.pop('pending_chatroom_message', None)
        await callback_query.message.edit_text("Chatroom message sending cancelled.")
        return True

    return False
