import asyncio
import io
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta

import aiohttp
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command
from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types.callback_query import CallbackQuery
from dotenv import load_dotenv

from db import (
    backup_db, close_db, delete_token, get_all_tokens, get_current_account,
    get_device_info, get_info_card, get_tokens, init_db, replace_token,
    restore_db, set_account_active, set_current_account, set_device_info,
    set_explore_url, set_info_card, set_token, set_user_filters,
    transfer_user_data, update_token_metadata,
)
from lounge import lounge_command_handler, handle_lounge_callback, send_lounge
from chatroom import chatroom_command_handler, handle_chatroom_callback, send_message_to_everyone
from unsubscribe import unsubscribe_command_handler, handle_unsubscribe_callback, unsubscribe_everyone
from filters import filter_command, set_filter
from aio import aio_markup, aio_callback_handler
from allcountry import run_all_countries, handle_all_countries_callback
from countries import countries_command_handler, handle_countries_callback
from requests import (
    handle_requests_callback, REQUESTS_CHOICE_MARKUP,
    run_requests_single, run_requests_parallel,
    STOP_MARKUP, format_progress_single, format_progress,
    handle_custom_speed_message,
)
from blocklist import (
    blocklist_command, handle_blocklist_callback,
    is_blocklist_active, add_to_permanent_blocklist, get_user_blocklist,
)
from signup import signup_command, signup_callback_handler, signup_message_handler
from spammer import spammer_command, spammer_message_handler, spammer_callback_handler
from device_info import random_device_info

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────

API_TOKEN = os.getenv("API_TOKEN")
TEMP_PASSWORD = os.getenv("TEMP_PASSWORD")
ADMIN_USER_IDS: list[int] = [
    int(uid) for uid in os.getenv("ADMIN_USER_IDS", "").split(",") if uid.strip()
]

if not API_TOKEN:
    raise RuntimeError("API_TOKEN is not set in environment.")
if not TEMP_PASSWORD:
    raise RuntimeError("TEMP_PASSWORD is not set in environment.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Bot / dispatcher setup ───────────────────────────────────────────────────

bot = Bot(token=API_TOKEN)
router = Router()
dp = Dispatcher()

# Per-user mutable state (non-persistent, reset on restart)
user_states: dict[int, dict] = defaultdict(lambda: {
    "running": False,
    "status_message_id": None,
    "pinned_message_id": None,
    "total_added_friends": 0,
})

# user_ids awaiting a .db file upload for restore
restore_pending: set[int] = set()

# Temporary password sessions: user_id → expiry datetime
password_access: dict[int, datetime] = {}


# ─── Access helpers ────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def has_valid_access(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    exp = password_access.get(user_id)
    return exp is not None and exp > datetime.now()


# ─── Markups ──────────────────────────────────────────────────────────────────

def get_tools_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Accounts",  callback_data="manage_accounts"),
            InlineKeyboardButton(text="Filters",   callback_data="settings_filters"),
            InlineKeyboardButton(text="Blocklist", callback_data="settings_blocklist"),
        ],
        [
            InlineKeyboardButton(text="Sign Up", callback_data="signup_go"),
            InlineKeyboardButton(text="Sign In", callback_data="signin_go"),
        ],
        [
            InlineKeyboardButton(text="Backup DB",  callback_data="db_backup"),
            InlineKeyboardButton(text="Restore DB", callback_data="db_restore"),
        ],
        [InlineKeyboardButton(text="Back", callback_data="back_to_start")],
    ])


start_markup = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="Start Requests", callback_data="start"),
        InlineKeyboardButton(text="All Countries",  callback_data="all_countries"),
    ],
    [InlineKeyboardButton(text="Tools", callback_data="open_tools")],
])

back_markup = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Back", callback_data="back_to_menu")],
])


# ─── Account keyboard builder ──────────────────────────────────────────────────

def build_accounts_buttons(tokens: list[dict], current_token: str | None) -> InlineKeyboardMarkup:
    buttons = []
    for i, token in enumerate(tokens):
        is_current = token["token"] == current_token
        filters = token.get("filters") or {}
        cc = filters.get("filterNationalityCode", "")
        label = token["name"]
        if cc:
            label = f"{label} | {cc}"
        if is_current:
            label = f"[>] {label}"
        active = token.get("active", True)
        buttons.append([
            InlineKeyboardButton(text=label,                       callback_data=f"set_account_{i}"),
            InlineKeyboardButton(text="On" if active else "Off", callback_data=f"toggle_account_{i}"),
            InlineKeyboardButton(text="View",                        callback_data=f"view_account_{i}"),
            InlineKeyboardButton(text="Delete",                        callback_data=f"delete_account_{i}"),
        ])
    buttons.append([InlineKeyboardButton(text="Back", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─── Shared account UI refresh ────────────────────────────────────────────────

async def _show_accounts(message, user_id: int) -> None:
    tokens = await get_all_tokens(user_id)
    current_token = await get_current_account(user_id)
    if not tokens:
        await message.edit_text(
            "<b>Manage Accounts</b>\n\nNo accounts saved yet.\nSend a token to add one.",
            reply_markup=back_markup, parse_mode="HTML",
        )
    else:
        await message.edit_text(
            "<b>Manage Accounts</b>\n\nTap an account to select it as active.",
            reply_markup=build_accounts_buttons(tokens, current_token), parse_mode="HTML",
        )


# ─── Commands ──────────────────────────────────────────────────────────────────

@router.message(Command("password"))
async def password_command(message: types.Message) -> None:
    user_id = message.chat.id
    args = message.text.strip().split()
    if len(args) < 2:
        await message.reply("Usage: /password <password>")
        return
    if args[1] == TEMP_PASSWORD:
        password_access[user_id] = datetime.now() + timedelta(hours=1)
        await message.reply("Access granted for one hour.")
        try:
            await bot.delete_message(chat_id=user_id, message_id=message.message_id)
        except Exception:
            pass
    else:
        await message.reply("Incorrect password.")


@router.message(Command("start"))
async def start_command(message: types.Message) -> None:
    if not has_valid_access(message.chat.id):
        await message.reply("You are not authorized to use this bot.")
        return
    state = user_states[message.chat.id]
    status = await message.answer("Welcome! Choose an action below.", reply_markup=start_markup)
    state["status_message_id"] = status.message_id
    state["pinned_message_id"] = None


@router.message(Command("tools"))
async def tools_command(message: types.Message) -> None:
    if not has_valid_access(message.chat.id):
        await message.reply("You are not authorized to use this bot.")
        return
    await message.answer("<b>Tools</b> — choose an option:", reply_markup=get_tools_markup(), parse_mode="HTML")


@router.message(Command("chatroom"))
async def chatroom_command(message: types.Message) -> None:
    await chatroom_command_handler(message, has_valid_access, get_current_account, get_tokens, user_states)


@router.message(Command("skip"))
async def unsubscribe_command(message: types.Message) -> None:
    await unsubscribe_command_handler(message, has_valid_access, get_current_account, get_tokens, user_states)


@router.message(Command("lounge"))
async def lounge_command(message: types.Message) -> None:
    await lounge_command_handler(message, has_valid_access, get_current_account, user_states)


@router.message(Command("invoke"))
async def invoke_command(message: types.Message) -> None:
    user_id = message.chat.id
    if not has_valid_access(user_id):
        await message.reply("You are not authorized to use this bot.")
        return
    tokens = await get_tokens(user_id)
    if not tokens:
        await message.reply("No tokens found.")
        return

    url = "https://api.meeff.com/facetalk/vibemeet/history/count/v1"
    headers_base = {"User-Agent": "okhttp/5.0.0-alpha.14", "Accept-Encoding": "gzip"}
    disabled: list[dict] = []

    async with aiohttp.ClientSession() as session:
        for token_obj in tokens:
            headers = {**headers_base, "meeff-access-token": token_obj["token"]}
            try:
                async with session.get(url, params={"locale": "en"}, headers=headers) as resp:
                    result = await resp.json(content_type=None)
                    if result.get("errorCode") == "AuthRequired":
                        disabled.append(token_obj)
            except Exception as e:
                logger.error("Error checking token %s: %s", token_obj.get("name"), e)
                disabled.append(token_obj)

    if disabled:
        for token_obj in disabled:
            await delete_token(user_id, token_obj["token"])
        names = "\n\n".join(
            f"{t['name']} ({t['email']})" if t.get("email") else t["name"]
            for t in disabled
        )
        await message.reply(f"Deleted {len(disabled)} disabled account(s):\n{names}")
    else:
        await message.reply("All accounts are working.")


@router.message(Command("add"))
async def add_person_command(message: types.Message) -> None:
    user_id = message.chat.id
    if not has_valid_access(user_id):
        await message.reply("You are not authorized to use this bot.")
        return
    args = message.text.strip().split()
    if len(args) < 2:
        await message.reply("Usage: /add <person_id>")
        return
    person_id = args[1]
    token = await get_current_account(user_id)
    if not token:
        await message.reply("No active account found.")
        return
    url = f"https://api.meeff.com/user/undoableAnswer/v5/?userId={person_id}&isOkay=1"
    headers = {"meeff-access-token": token, "Connection": "keep-alive"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                data = await resp.json(content_type=None)
        if data.get("errorCode") == "LikeExceeded":
            await message.reply("Daily like limit reached.")
        elif data.get("errorCode"):
            await message.reply(f"Failed: {data.get('errorMessage', 'Unknown error')}")
        else:
            await message.reply(f"Added person: {person_id}")
    except Exception as e:
        logger.error("Error adding person: %s", e)
        await message.reply("An error occurred.")


@router.message(Command("block"))
async def blockadd_command(message: types.Message) -> None:
    user_id = message.chat.id
    if not has_valid_access(user_id):
        await message.reply("You are not authorized to use this bot.")
        return
    args = message.text.strip().split()
    if len(args) < 2:
        await message.reply("Usage: /block <user_id>")
        return
    block_id = args[1]
    blocklist = await get_user_blocklist(user_id)
    if block_id in blocklist:
        await message.reply(f"User {block_id} is already blocked.")
        return
    await add_to_permanent_blocklist(user_id, block_id)
    await message.reply(f"User {block_id} has been permanently blocked.")


@router.message(Command("aio"))
async def aio_command(message: types.Message) -> None:
    if not has_valid_access(message.chat.id):
        await message.reply("You are not authorized to use this bot.")
        return
    await message.answer("<b>All-in-One</b> — choose an action:", reply_markup=aio_markup, parse_mode="HTML")


@router.message(Command("countries"))
async def countries_cmd(message: types.Message) -> None:
    if not has_valid_access(message.chat.id):
        await message.reply("You are not authorized to use this bot.")
        return
    await countries_command_handler(message)


@router.message(Command("spam"))
async def spam_command(message: types.Message) -> None:
    await spammer_command(message)


@router.message(Command("transfer"))
async def transfer_command(message: types.Message) -> None:
    if not is_admin(message.chat.id):
        await message.reply("Only admins can transfer data.")
        return
    args = message.text.strip().split()
    if len(args) < 2:
        await message.reply("Usage: /transfer <destination_user_id>")
        return
    try:
        to_user_id = int(args[1])
    except ValueError:
        await message.reply("Invalid user ID format.")
        return
    from_user_id = message.chat.id
    if to_user_id == from_user_id:
        await message.reply("Cannot transfer to yourself.")
        return
    await transfer_user_data(from_user_id, to_user_id)
    await message.reply(f"All tokens and settings transferred to user {to_user_id}.")


@router.message(Command("assign"))
async def assign_command(message: types.Message) -> None:
    user_id = message.chat.id
    if not has_valid_access(user_id):
        await message.reply("You are not authorized to use this bot.")
        return
    tokens = await get_tokens(user_id)
    if not tokens:
        await message.reply("No active accounts found. Enable accounts in Tools → Accounts first.")
        return

    status = await message.reply(f"Refreshing tokens for {len(tokens)} active account(s)…")
    async with aiohttp.ClientSession() as session:
        for idx, token_info in enumerate(tokens):
            old_token = token_info["token"]
            account_name = token_info.get("name", f"Account {idx + 1}")
            await status.edit_text(f"[{idx+1}/{len(tokens)}] Processing: {account_name}")

            headers = {
                "User-Agent": "okhttp/5.3.2",
                "Accept-Encoding": "gzip",
                "meeff-access-token": old_token,
                "content-type": "application/json; charset=utf-8",
            }

            # Warm-up call
            try:
                async with session.post(
                    "https://api.meeff.com/api/init/v2",
                    json={"platform": "android", "version": "7.0.5", "locale": "en"},
                    headers=headers,
                ) as resp:
                    await resp.text()
            except Exception as e:
                logger.warning("init call failed for %s: %s", account_name, e)

            # Fetch or generate device fingerprint
            stored_device = await get_device_info(user_id, old_token)
            device = stored_device or random_device_info()
            device_payload = {**device, "appVersion": "7.0.5", "locale": "en"}

            j: dict = {}
            try:
                async with session.post(
                    "https://api.meeff.com/user/login/v4",
                    json=device_payload, headers=headers,
                ) as resp:
                    j = await resp.json(content_type=None)
            except Exception as e:
                logger.error("login failed for %s: %s", account_name, e)
                j = {"errorMessage": str(e)}

            new_token = j.get("accessToken")
            if new_token:
                try:
                    await replace_token(user_id, old_token, new_token)
                    await update_token_metadata(
                        user_id, new_token,
                        name=account_name,
                        email=token_info.get("email"),
                    )
                    await set_device_info(user_id, new_token, device)
                    await status.edit_text(f"[{idx+1}/{len(tokens)}] Updated: {account_name}")
                except Exception as e:
                    logger.error("Failed to save new token for %s: %s", account_name, e)
                    await status.edit_text(f"[{idx+1}/{len(tokens)}] Error saving: {account_name}")
            else:
                err = j.get("errorCode") or j.get("errorMessage") or "Unknown error"
                await status.edit_text(f"[{idx+1}/{len(tokens)}] Failed for {account_name}: {err}")

            await asyncio.sleep(1)

    await status.edit_text("Token refresh completed.")


# ─── Catch-all message handler ────────────────────────────────────────────────

@router.message()
async def handle_main_message(message: types.Message) -> None:
    user_id = message.from_user.id
    state = user_states[user_id]

    # DB restore: admin sent a .db file after pressing Restore DB
    if user_id in restore_pending and message.document:
    restore_pending.discard(user_id)
    doc = message.document
    if not (doc.file_name or "").endswith(".db"):
        await message.reply("That doesn't look like a .db file. Restore cancelled.")
        return

    # Send initial "downloading..." message
    msg = await message.reply("Downloading and restoring database…")

    try:
        file_info = await bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, buf)
        await restore_db(buf.getvalue())

        # Edit the previous message instead of sending a new one
        await bot.edit_message_text(
            "Database restored successfully!",
            chat_id=msg.chat.id,
            message_id=msg.message_id
        )

    except ValueError as e:
        await bot.edit_message_text(
            f"Invalid file: {e}",
            chat_id=msg.chat.id,
            message_id=msg.message_id
        )
    except Exception as e:
        await bot.edit_message_text(
            f"Restore failed: {e}",
            chat_id=msg.chat.id,
            message_id=msg.message_id
        )
    return

    if await spammer_message_handler(message):
        return
    if await signup_message_handler(message):
        return

    if state.get("awaiting_custom_speed"):
        if message.text and message.text.strip().lower() == "/cancel":
            state.pop("awaiting_custom_speed", None)
            state.pop("pending_speed_mode", None)
            await message.reply("Custom speed cancelled.")
            return
        await handle_custom_speed_message(message, state, bot, get_tokens, get_current_account)
        return

    # Ignore bot commands we didn't explicitly handle
    if message.text and message.text.startswith("/"):
        return

    if not has_valid_access(user_id):
        return

    if not message.text:
        await message.reply("Please provide a valid token.")
        return

    text = message.text.strip()

    # Explore URL shortcut
    if text.startswith("https://api.meeff.com/user/explore/"):
        await set_explore_url(user_id, text)
        await message.reply("Explore URL saved!")
        return

    # Token input
    parts = text.split(" ")
    token = parts[0]
    if len(token) < 10:
        await message.reply("Invalid token. Please try again.")
        return

    # Verify the token against Meeff
    headers = {
        "User-Agent": "okhttp/5.0.0-alpha.14",
        "Accept-Encoding": "gzip",
        "meeff-access-token": token,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.meeff.com/facetalk/vibemeet/history/count/v1",
                params={"locale": "en"}, headers=headers,
            ) as resp:
                result = await resp.json(content_type=None)
        if result.get("errorCode") == "AuthRequired":
            await message.reply("Token is invalid or disabled.")
            return
    except Exception as e:
        logger.error("Error verifying token: %s", e)
        await message.reply("Error verifying token. Please try again.")
        return

    existing_tokens = await get_tokens(user_id)
    account_name = " ".join(parts[1:]) if len(parts) > 1 else f"Account {len(existing_tokens) + 1}"
    await set_token(user_id, token, account_name)
    await message.reply(f"Token saved as <b>{account_name}</b>.", parse_mode="HTML")


# ─── Callback dispatcher ──────────────────────────────────────────────────────

@router.callback_query()
async def callback_handler(callback_query: CallbackQuery) -> None:
    user_id = callback_query.from_user.id
    state = user_states[user_id]

    if not has_valid_access(user_id):
        await callback_query.answer("You are not authorized.")
        return

    # Delegate to sub-handlers in priority order
    if await spammer_callback_handler(callback_query):
        return
    if await signup_callback_handler(callback_query):
        return
    if await handle_blocklist_callback(callback_query):
        return
    if await handle_unsubscribe_callback(callback_query, state, bot, user_id, get_current_account, get_tokens, unsubscribe_everyone):
        return
    if await handle_chatroom_callback(callback_query, state, bot, user_id, get_current_account, get_tokens, send_message_to_everyone):
        return
    if await handle_lounge_callback(callback_query, state, bot, user_id, get_current_account, get_tokens, send_lounge):
        return
    if callback_query.data.startswith("aio_"):
        await aio_callback_handler(callback_query)
        return
    if await handle_countries_callback(callback_query):
        return
    if await handle_all_countries_callback(
        callback_query, state, bot, user_id, get_current_account, get_tokens,
        set_current_account, run_all_countries, start_markup,
    ):
        return
    if await handle_requests_callback(
        callback_query, state, bot, user_id, get_current_account, get_tokens,
        set_current_account, start_markup,
    ):
        return
    # Route filter_ callbacks BEFORE the main elif ladder
    if callback_query.data.startswith("filter_"):
        await set_filter(callback_query)
        return

    data = callback_query.data

    # ── Account management ───────────────────────────────────────────────────
    if data == "manage_accounts":
        await _show_accounts(callback_query.message, user_id)
        await callback_query.answer()

    elif data.startswith("set_account_"):
        index = int(data.split("_")[-1])
        tokens = await get_all_tokens(user_id)
        if index >= len(tokens):
            await callback_query.answer("Invalid account.")
            return
        if not tokens[index].get("active", True):
            await callback_query.answer("This account is off. Turn it on first.")
            return
        await set_current_account(user_id, tokens[index]["token"])
        await _show_accounts(callback_query.message, user_id)
        await callback_query.answer()

    elif data.startswith("delete_account_"):
        index = int(data.split("_")[-1])
        tokens = await get_all_tokens(user_id)
        if index >= len(tokens):
            await callback_query.answer("Invalid account.")
            return
        await delete_token(user_id, tokens[index]["token"])
        await callback_query.answer("Account deleted.")
        await _show_accounts(callback_query.message, user_id)

    elif data.startswith("toggle_account_"):
        index = int(data.split("_")[-1])
        tokens = await get_all_tokens(user_id)
        if index >= len(tokens):
            await callback_query.answer("Invalid account.")
            return
        current_status = tokens[index].get("active", True)
        await set_account_active(user_id, tokens[index]["token"], not current_status)
        await _show_accounts(callback_query.message, user_id)
        await callback_query.answer()

    elif data.startswith("view_account_"):
        index = int(data.split("_")[-1])
        tokens = await get_all_tokens(user_id)
        if index >= len(tokens):
            await callback_query.answer("Invalid account.")
            return
        info_card = await get_info_card(user_id, tokens[index]["token"])
        if info_card:
            await callback_query.message.answer(info_card, parse_mode="HTML", disable_web_page_preview=False)
            await callback_query.answer()
        else:
            await callback_query.answer("No info card for this account.")

    # ── Navigation ───────────────────────────────────────────────────────────
    elif data in ("back_to_menu", "open_tools"):
        try:
            await callback_query.message.edit_text(
                "<b>Tools</b> — choose an option:", reply_markup=get_tools_markup(), parse_mode="HTML"
            )
        except Exception as e:
            if "message is not modified" not in str(e):
                raise
        await callback_query.answer()

    elif data == "back_to_start":
        try:
            await callback_query.message.edit_text(
                "Welcome! Choose an action below.", reply_markup=start_markup
            )
        except Exception as e:
            if "message is not modified" not in str(e):
                raise
        await callback_query.answer()

    # ── Settings shortcuts ───────────────────────────────────────────────────
    elif data == "settings_filters":
        await filter_command(callback_query.message, edit=True)
        await callback_query.answer()

    elif data == "settings_blocklist":
        await blocklist_command(callback_query, edit=True)

    # ── DB backup / restore ──────────────────────────────────────────────────
    elif data == "db_backup":
        if not is_admin(user_id):
            await callback_query.answer("Admins only.", show_alert=True)
            return
        await callback_query.answer("Creating backup…")
        try:
            from aiogram.types import BufferedInputFile
            db_bytes = await backup_db()
            filename = f"meeff_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            await callback_query.message.answer_document(
                BufferedInputFile(db_bytes, filename=filename),
                caption=f"DB backup ({len(db_bytes) // 1024} KB). Send back via Restore DB to restore.",
            )
        except Exception as e:
            await callback_query.message.answer(f"Backup failed: {e}")

    elif data == "db_restore":
        if not is_admin(user_id):
            await callback_query.answer("Admins only.", show_alert=True)
            return
        restore_pending.add(user_id)
        await callback_query.answer()
        await callback_query.message.answer(
            "<b>Restore mode active.</b>\n\nSend your <code>.db</code> backup file now.\n"
            "This will <b>completely replace</b> the current database!",
            parse_mode="HTML",
        )


# ─── Bot setup ────────────────────────────────────────────────────────────────

async def set_bot_commands() -> None:
    commands = [
        BotCommand(command="start",    description="Start the bot"),
        BotCommand(command="lounge",   description="Send message to everyone in the lounge"),
        BotCommand(command="chatroom", description="Send a message to everyone in all chatrooms"),
        BotCommand(command="add",      description="Manually add a person by ID"),
        BotCommand(command="block",    description="Permanently block a user by ID"),
        BotCommand(command="aio",      description="All-in-one quick actions"),
        BotCommand(command="countries",description="Manage country include/exclude filter"),
        BotCommand(command="invoke",   description="Verify and remove disabled accounts"),
        BotCommand(command="skip",     description="Unsubscribe from all chatrooms"),
        BotCommand(command="tools",    description="Accounts & Tools menu"),
        BotCommand(command="spam",     description="Create multiple accounts"),
        BotCommand(command="password", description="Enter password for temporary access"),
        BotCommand(command="transfer", description="Transfer tokens/settings to another user (admin)"),
        BotCommand(command="assign",   description="Refresh Meeff tokens for all accounts"),
    ]
    await bot.set_my_commands(commands)


async def main() -> None:
    await init_db()
    await set_bot_commands()
    dp.include_router(router)
    try:
        logger.info("Bot starting…")
        await dp.start_polling(bot)
    finally:
        await close_db()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
