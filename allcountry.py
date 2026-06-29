import asyncio
import aiohttp
import html
import logging
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest
from blocklist import is_blocklist_active, add_to_temporary_blocklist, get_user_blocklist
from db import get_explore_url
from dateutil import parser
from datetime import datetime, timezone

COUNTRIES = [
    "AF", "AX", "AL", "DZ", "AS", "AD", "AO", "AI", "AQ", "AG", "AR", "AM", "AW", "AU", "AT", "AZ",
    "BS", "BH", "BD", "BB", "BY", "BE", "BZ", "BJ", "BM", "BT", "BO", "BQ", "BA", "BW", "BV", "BR",
    "IO", "BN", "BG", "BF", "BI", "CV", "KH", "CM", "CA", "KY", "CF", "TD", "CL", "CN", "CX", "CC",
    "CO", "KM", "CG", "CD", "CK", "CR", "CI", "HR", "CU", "CW", "CY", "CZ", "DK", "DJ", "DM", "DO",
    "EC", "EG", "SV", "GQ", "ER", "EE", "SZ", "ET", "FK", "FO", "FJ", "FI", "FR", "GF", "PF", "TF",
    "GA", "GM", "GE", "DE", "GH", "GI", "GR", "GL", "GD", "GP", "GU", "GT", "GG", "GN", "GW", "GY",
    "HT", "HM", "VA", "HN", "HK", "HU", "IS", "IN", "ID", "IR", "IQ", "IE", "IM", "IL", "IT", "JM",
    "JP", "JE", "JO", "KZ", "KE", "KI", "KP", "KR", "KW", "KG", "LA", "LV", "LB", "LS", "LR", "LY",
    "LI", "LT", "LU", "MO", "MG", "MW", "MY", "MV", "ML", "MT", "MH", "MQ", "MR", "MU", "YT", "MX",
    "FM", "MD", "MC", "MN", "ME", "MS", "MA", "MZ", "MM", "NA", "NR", "NP", "NL", "NC", "NZ", "NI",
    "NE", "NG", "NU", "NF", "MK", "MP", "NO", "OM", "PK", "PW", "PS", "PA", "PG", "PY", "PE", "PH",
    "PN", "PL", "PT", "PR", "QA", "RE", "RO", "RU", "RW", "BL", "SH", "KN", "LC", "MF", "PM", "VC",
    "WS", "SM", "ST", "SA", "SN", "RS", "SC", "SL", "SG", "SX", "SK", "SI", "SB", "SO", "ZA", "GS",
    "SS", "ES", "LK", "SD", "SR", "SJ", "SE", "CH", "SY", "TW", "TJ", "TZ", "TH", "TL", "TG", "TK",
    "TO", "TT", "TN", "TR", "TM", "TC", "TV", "UG", "UA", "AE", "GB", "US", "UM", "UY", "UZ", "VU",
    "VE", "VN", "VG", "VI", "WF", "EH", "YE", "ZM", "ZW"
]
REQUESTS_PER_COUNTRY = 2
BASE_HEADERS = {
    "User-Agent": "okhttp/4.12.0",
    "Accept-Encoding": "gzip",
    "Content-Type": "application/json; charset=utf-8"
}

ALL_COUNTRIES_CHOICE_MARKUP = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="Current", callback_data="allcountries_current"),
        InlineKeyboardButton(text="All", callback_data="allcountries_all")
    ],
    [InlineKeyboardButton(text="Cancel", callback_data="allcountries_cancel")]
])
ALL_COUNTRIES_ALL_CONFIRM_MARKUP = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Confirm", callback_data="allcountries_confirm")],
    [InlineKeyboardButton(text="Cancel", callback_data="allcountries_cancel")]
])
ALL_COUNTRIES_STOP_MARKUP = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Stop Requests", callback_data="stop")]
])

def format_time_used(start_time, end_time):
    delta = end_time - start_time
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    else:
        return f"{seconds}s"

def format_user(user):
    def time_ago(dt_str):
        if not dt_str:
            return "N/A"
        try:
            dt = parser.isoparse(dt_str)
            now = datetime.now(timezone.utc)
            diff = now - dt
            minutes = int(diff.total_seconds() // 60)
            if minutes < 1:
                return "just now"
            elif minutes < 60:
                return f"{minutes} min ago"
            hours = minutes // 60
            if hours < 24:
                return f"{hours} hr ago"
            days = hours // 24
            return f"{days} day(s) ago"
        except Exception:
            return "unknown"
    last_active = time_ago(user.get("recentAt"))
    return (
        f"<b>Name:</b> {html.escape(user.get('name', 'N/A'))}\n"
        f"<b>ID:</b> <code>{html.escape(user.get('_id', 'N/A'))}</code>\n"
        f"<b>Description:</b> {html.escape(user.get('description', 'N/A'))}\n"
        f"<b>Birth Year:</b> {html.escape(str(user.get('birthYear', 'N/A')))}\n"
        f"<b>Platform:</b> {html.escape(user.get('platform', 'N/A'))}\n"
        f"<b>Profile Score:</b> {html.escape(str(user.get('profileScore', 'N/A')))}\n"
        f"<b>Distance:</b> {html.escape(str(user.get('distance', 'N/A')))} km\n"
        f"<b>Language Codes:</b> {html.escape(', '.join(user.get('languageCodes', [])))}\n"
        f"<b>Last Active:</b> {last_active}\n"
        "Photos: " + ' '.join([f"<a href='{html.escape(url)}'>Photo</a>" for url in user.get('photoUrls', [])])
    )

def format_progress_single(account_name, country_code, added, countries_processed):
    return (
        f"<b>[ ALL COUNTRIES ]</b>\n"
        f"Account: {account_name}\n"
        f"Country: {country_code}\n"
        f"├ Sent: {added}\n"
        f"├ Countries processed: {countries_processed}\n"
        "\nProcessing... (Press Stop to interrupt)"
    )

def format_result_single(account_name, total_added, countries_processed, start_time, end_time, like_exceeded=False, finished_by_user=False):
    status = "<b>[ DONE ]</b>" if not finished_by_user else "<b>[ STOPPED ]</b>"
    extra = "\n<b>Like limit exceeded!</b>" if like_exceeded else ""
    return (
        f"{status}\n"
        f"Account: {account_name}{extra}\n"
        f"\n• Total Requests Sent: {total_added}"
        f"\n• Countries Processed: {countries_processed}"
        f"\nTime used: {format_time_used(start_time, end_time)}"
    )

async def safe_edit(bot, chat_id, msg_id, text, markup=None):
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" in str(e): pass
        else: logging.warning(f"edit_message_text error: {e}")
    except Exception as e:
        logging.warning(f"edit_message_text unknown error: {e}")

async def update_country_filter(session, headers, country_code):
    url = "https://api.meeff.com/user/updateFilter/v1"
    data = {
        "filterGenderType": 5,
        "filterBirthYearFrom": 1981,
        "filterBirthYearTo": 2007,
        "filterDistance": 510,
        "filterLanguageCodes": "",
        "filterNationalityBlock": 0,
        "filterNationalityCode": country_code,
        "filterMinProfileImageCount": 2,
        "filterFaceVerifiedOnly": False,
        "filterRecentlyActiveOnly": False,
        "locale": "en"
    }
    try:
        async with session.post(url, json=data, headers=headers) as resp:
            if resp.status != 200:
                logging.error(f"Failed to update country: {country_code}, status: {resp.status}")
    except Exception as e:
        logging.error(f"Error updating country filter for {country_code}: {e}")

async def fetch_users(session, headers, user_id):
    url = await get_explore_url(user_id)
    if not url:
        return None
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return (await resp.json(content_type=None)).get("users", [])
    except Exception as e:
        logging.error(f"Error fetching users: {e}")
    return []

async def like_user(session, headers, user_id):
    url = f"https://api.meeff.com/user/undoableAnswer/v5/?userId={user_id}&isOkay=1"
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 429:
                return False
    except Exception as e:
        logging.error(f"Error liking user {user_id}: {e}")
    return True

async def run_all_countries_token(user_id, state, bot, token, account_name):
    start_time = datetime.now()
    if not token:
        await bot.edit_message_text(
            chat_id=user_id, message_id=state["status_message_id"],
            text="No active account found. Please set an account before starting All Countries feature."
        )
        return 0, 0, False

    headers = dict(BASE_HEADERS)
    headers["meeff-access-token"] = token
    requests_sent = countries_processed = 0
    like_limit_exceeded = False

    async with aiohttp.ClientSession() as session:
        for country_code in COUNTRIES:
            if not state.get("running"):
                break
            await update_country_filter(session, headers, country_code)
            users = await fetch_users(session, headers, user_id)
            if users is None:

                await safe_edit(
                    bot, user_id, state["status_message_id"],
                    "⚠️ <b>No Explore URL saved.</b>\n\n"
                    "Please send your Explore URL in chat to continue.\n"
                    "It should start with: <code>https://api.meeff.com/user/explore/</code>",
                )
                state["running"] = False
                return 0, 0, False
            country_requests = 0
            countries_processed += 1
            state["countries_processed"] = countries_processed
            state["total_added_friends"] = requests_sent
            for user in users:
                if country_requests >= REQUESTS_PER_COUNTRY or not state.get("running"):
                    break
                if await is_blocklist_active(user_id):
                    blocklist = await get_user_blocklist(user_id)
                    if user["_id"] in blocklist:
                        continue

                if not await like_user(session, headers, user["_id"]):
                    like_limit_exceeded = True
                    break


                if await is_blocklist_active(user_id):
                    await add_to_temporary_blocklist(user_id, user["_id"])

                requests_sent += 1
                country_requests += 1
                state["total_added_friends"] = requests_sent
                await safe_edit(
                    bot, user_id, state["status_message_id"],
                    format_progress_single(account_name, country_code, requests_sent, countries_processed),
                    ALL_COUNTRIES_STOP_MARKUP
                )
                try:
                    await bot.send_message(user_id, format_user(user), parse_mode="HTML")
                except Exception:
                    pass
                await asyncio.sleep(1)
            if like_limit_exceeded:
                break
            await asyncio.sleep(1)
        state["countries_processed"] = countries_processed
        state["total_added_friends"] = requests_sent
    end_time = datetime.now()
    state["finalized"] = True
    await safe_edit(
        bot, user_id, state["status_message_id"],
        format_result_single(
            account_name, requests_sent, countries_processed,
            start_time, end_time,
            like_exceeded=like_limit_exceeded,
            finished_by_user=not state.get("running", True)
        ),
        None
    )
    return requests_sent, countries_processed, like_limit_exceeded

async def run_all_countries(user_id, state, bot, get_current_account, account_name=None):
    token = await get_current_account(user_id)
    return await run_all_countries_token(user_id, state, bot, token, account_name or "Current")

async def handle_all_countries_callback(
    callback_query, state, bot, user_id,
    get_current_account, get_tokens, set_current_account,
    run_all_countries, start_markup
):
    data = callback_query.data

    if data == "all_countries":
        await callback_query.message.edit_text(
            "How would you like to run All Countries feature?",
            reply_markup=ALL_COUNTRIES_CHOICE_MARKUP
        )
        await callback_query.answer()
        return True

    if data == "allcountries_all":
        tokens = await get_tokens(user_id)
        if not tokens:
            await callback_query.message.edit_text("No accounts found.", reply_markup=start_markup)
            await callback_query.answer()
            return True
        account_lines = "\n".join(f"{idx+1}. {t.get('name', f'Account {idx+1}')}" for idx, t in enumerate(tokens))
        await callback_query.message.edit_text(
            f"You chose to run All Countries for ALL accounts. Press Confirm to proceed.\n\nAccounts to process:\n{account_lines}",
            reply_markup=ALL_COUNTRIES_ALL_CONFIRM_MARKUP
        )
        state["pending_allcountries_all"] = True
        await callback_query.answer()
        return True

    if data == "allcountries_confirm":
        if not state.get("pending_allcountries_all"):
            await callback_query.answer("Nothing to confirm.")
            return True
        tokens = await get_tokens(user_id)
        if not tokens:
            await callback_query.message.edit_text("No accounts found.", reply_markup=start_markup)
            await callback_query.answer()
            state.pop("pending_allcountries_all", None)
            return True
        per_account_requests_sent = []
        total_countries = total_requests = 0
        like_limit_exceeded = False
        for idx, token_info in enumerate(tokens):
            account_name = token_info.get('name', f"Account {idx+1}")
            token = token_info["token"]
            state["running"] = True
            state["finalized"] = False
            await callback_query.message.edit_text(
                f"Account {idx+1}: {html.escape(account_name)}\nStarting All Countries feature...",
                reply_markup=ALL_COUNTRIES_STOP_MARKUP, parse_mode="HTML"
            )
            state["status_message_id"] = callback_query.message.message_id
            state["pinned_message_id"] = callback_query.message.message_id
            await bot.pin_chat_message(chat_id=user_id, message_id=state["pinned_message_id"])
            requests_sent, countries_processed, like_limit = await run_all_countries_token(
                user_id, state, bot, token, account_name
            )
            per_account_requests_sent.append(requests_sent)
            total_countries = max(total_countries, countries_processed)
            total_requests += requests_sent
            if like_limit: like_limit_exceeded = True
            try:
                await bot.unpin_chat_message(chat_id=user_id, message_id=state["pinned_message_id"])
            except Exception:
                pass
            state["pinned_message_id"] = None
            if not state.get("running"):
                break
        summary_text = (
            f"Total Accounts: {len(per_account_requests_sent)}\n"
            f"Countries: {total_countries}\n"
            f"Requests sent successfully ({total_requests})\n"
            f"({' | '.join(str(x) for x in per_account_requests_sent)})"
        )
        if like_limit_exceeded: summary_text += "\nLike limit exceeded."
        await callback_query.message.edit_text(summary_text, reply_markup=start_markup)
        await callback_query.answer()
        state.pop("pending_allcountries_all", None)
        return True

    if data == "allcountries_cancel":
        state.pop("pending_allcountries_all", None)
        await callback_query.message.edit_text("All Countries operation cancelled.", reply_markup=start_markup)
        await callback_query.answer("Cancelled.")
        return True

    if data == "allcountries_current":
        if state.get("running"):
            await callback_query.answer("Another process is already running!")
            return True
        tokens = await get_tokens(user_id)
        current_token = await get_current_account(user_id)
        account_name = next((t.get("name", "Current") for t in tokens if t["token"] == current_token), "Current")
        state.update({"running": True, "finalized": False})
        await callback_query.message.edit_text(
            f"Account: {html.escape(account_name)}\nStarting All Countries feature...",
            reply_markup=ALL_COUNTRIES_STOP_MARKUP, parse_mode="HTML"
        )
        state["status_message_id"] = callback_query.message.message_id
        state["pinned_message_id"] = callback_query.message.message_id
        await bot.pin_chat_message(chat_id=user_id, message_id=state["pinned_message_id"])
        await run_all_countries_token(user_id, state, bot, current_token, account_name)
        pin_id = state.get("pinned_message_id")
        if pin_id:
            try: await bot.unpin_chat_message(chat_id=user_id, message_id=pin_id)
            except Exception: pass
            state["pinned_message_id"] = None
        state["running"] = False
        state["finalized"] = True
        return True

    if data == "stop":
        if not state.get("running"):
            await callback_query.answer("All Countries is not running!")
            return True
        if state.get("finalized"):
            await callback_query.answer("Stopped.")
            return True
        state["finalized"] = True
        state["running"] = False
        pin_id = state.get("pinned_message_id")
        if pin_id:
            try: await bot.unpin_chat_message(chat_id=user_id, message_id=pin_id)
            except Exception: pass
            state["pinned_message_id"] = None
        await callback_query.answer("Stopped.")
        return True

    return False
