import aiohttp
import json
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from db import set_token, set_info_card, get_info_card, set_user_filters, get_user_filters, set_device_info, get_device_info
from requests import format_user
from device_info import random_device_info

def format_user_with_nationality(user):
    def time_ago(dt_str):
        if not dt_str:
            return "N/A"
        try:
            from dateutil import parser
            from datetime import datetime, timezone
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

    card = (
        f"<b>Name:</b> {user.get('name', 'N/A')}\n"
        f"<b>ID:</b> <code>{user.get('_id', 'N/A')}</code>\n"
        f"<b>Description:</b> {user.get('description', 'N/A')}\n"
        f"<b>Birth Year:</b> {user.get('birthYear', 'N/A')}\n"
        f"<b>Nationality Code:</b> {user.get('nationalityCode', 'N/A')}\n"
        f"<b>Platform:</b> {user.get('platform', 'N/A')}\n"
        f"<b>Profile Score:</b> {user.get('profileScore', 'N/A')}\n"
        f"<b>Distance:</b> {user.get('distance', 'N/A')} km\n"
        f"<b>Language Codes:</b> {', '.join(user.get('languageCodes', []))}\n"
        f"<b>Last Active:</b> {last_active}\n"
        "Photos: " + ' '.join([f"<a href='{url}'>Photo</a>" for url in user.get('photoUrls', [])])
    )
    if "email" in user:
        card += f"\n<b>Email:</b> <code>{user['email']}</code>"
    if "password" in user:
        card += f"\n<b>Password:</b> <code>{user['password']}</code>"
    if "token" in user:
        card += f"\n<b>Token:</b> <code>{user['token']}</code>"
    return card

SIGNUP_MENU = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="Sign Up", callback_data="signup_go"),
        InlineKeyboardButton(text="Sign In", callback_data="signin_go")
    ]
])
VERIFY_BUTTON = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Verify Email", callback_data="signup_verify")]
])
BACK_TO_SIGNUP = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Back", callback_data="signup_menu")]
])
DONE_PHOTOS = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Done", callback_data="signup_photos_done")],
    [InlineKeyboardButton(text="Back", callback_data="signup_menu")]
])
RESEND_EMAIL_BUTTON = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Resend Verification Email", callback_data="resend_email_verification")],
    [InlineKeyboardButton(text="Back", callback_data="signup_menu")]
])

user_signup_states = {}

DEFAULT_PHOTOS = (
    "https://meeffus.s3.amazonaws.com/profile/2025/06/16/20250616052423006_profile-1.0-bd262b27-1916-4bd3-9f1d-0e7fdba35268.jpg|"
    "https://meeffus.s3.amazonaws.com/profile/2025/06/16/20250616052438006_profile-1.0-349bf38c-4555-40cc-a322-e61afe15aa35.jpg"
)

# Default filter for new accounts
DEFAULT_FILTER = {
    "filterGenderType": 5,
    "filterBirthYearFrom": 1995,
    "filterBirthYearTo": 2007,
    "filterDistance": 510,
    "filterLanguageCodes": "",
    "filterNationalityBlock": 0,
    "filterNationalityCode": "",
    "locale": "en"
}

async def check_email_exists(email):
    url = "https://api.meeff.com/user/checkEmail/v1"
    payload = {
        "email": email,
        "locale": "en"
    }
    headers = {
        'User-Agent': "okhttp/5.3.2",
        'Accept-Encoding': "gzip",
        'Content-Type': "application/json",
        'content-type': "application/json; charset=utf-8"
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            status = resp.status
            try:
                resp_json = await resp.json()
            except Exception:
                resp_json = {}
            if status == 406 or resp_json.get("errorCode") == "AlreadyInUse":
                return False, resp_json.get("errorMessage", "This email address is already in use.")
            return True, ""

async def signup_command(message: Message):
    user_signup_states[message.chat.id] = {"stage": "menu"}
    await message.answer("Choose an option:", reply_markup=SIGNUP_MENU)

async def signup_callback_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    state = user_signup_states.get(user_id, {"stage": "menu"})
    if callback.data == "signup_go":
        state["stage"] = "ask_email"
        state["device_info"] = random_device_info()
        user_signup_states[user_id] = state
        await callback.message.edit_text("Enter your email for registration:", reply_markup=BACK_TO_SIGNUP)
        await callback.answer()
        return True
    if callback.data == "signup_menu":
        state["stage"] = "menu"
        await callback.message.edit_text("Choose an option:", reply_markup=SIGNUP_MENU)
        await callback.answer()
        return True
    if callback.data == "signin_go":
        state["stage"] = "signin_email"
        state["device_info"] = random_device_info()
        user_signup_states[user_id] = state
        await callback.message.edit_text("Enter your email to sign in:", reply_markup=BACK_TO_SIGNUP)
        await callback.answer()
        return True
    if callback.data == "signup_verify":
        creds = state.get("creds")
        if not creds:
            await callback.answer("No signup info. Please sign up again.", show_alert=True)
            state["stage"] = "menu"
            await callback.message.edit_text("Choose an option:", reply_markup=SIGNUP_MENU)
            return True
        await callback.message.edit_text("Checking verification and logging in...", reply_markup=None)
        # Pass the previously received accessToken so Meeff recognises the session
        login_result = await try_signin(
            creds['email'], creds['password'],
            state.get("device_info"),
            access_token=state.get("pending_access_token"),
        )
        if login_result.get("accessToken") and login_result.get("errorCode") not in ("EmailVerificationRequired", "NotVerified"):
            await store_token_and_show_card(callback.message, login_result, creds, device_info=state.get("device_info"))
        elif login_result.get("errorCode") == "SecuredPendingDevice":
            await callback.message.edit_text(
                "Device verification required!\n\n"
                "Check your mobile device for a verification notification and verify it there.\n\n"
                "Once verified on your mobile, click 'Verify' again to complete login.",
                reply_markup=VERIFY_BUTTON
            )
        elif login_result.get("errorCode") in ("NotVerified", "EmailVerificationRequired"):
            # Save/refresh the accessToken for subsequent re-login attempts
            if login_result.get("accessToken"):
                state["pending_access_token"] = login_result["accessToken"]
            await callback.message.edit_text(
                "Email not verified! Please check your inbox and click the link, then try Verify again.\n"
                "Or click the button below to resend the verification email.",
                reply_markup=RESEND_EMAIL_BUTTON
            )
        else:
            await callback.message.edit_text(f"Login failed: {login_result.get('errorMessage', 'Unknown error')}", reply_markup=SIGNUP_MENU)
        return True
    if callback.data == "resend_email_verification":
        creds = state.get("creds")
        if not creds or not creds.get("email") or not creds.get("password"):
            await callback.answer("No signup info. Please sign up again.", show_alert=True)
            return True
        login_result = await try_signin(creds['email'], creds['password'], state.get("device_info"), access_token=state.get("pending_access_token"))
        access_token = login_result.get("accessToken")
        if not access_token:
            await callback.answer("Token not available. Try signing up again.", show_alert=True)
            return True
        resend_result = await resend_verification_email(access_token)
        if (resend_result.get("errorCode") == "" or resend_result.get("errorCode") is None):
            await callback.message.edit_text(
                "Verification email resent! Please check your inbox and verify your email.",
                reply_markup=VERIFY_BUTTON
            )
        else:
            await callback.message.edit_text(
                f"Failed to resend verification email: {resend_result.get('errorMessage', 'Unknown error')}",
                reply_markup=RESEND_EMAIL_BUTTON
            )
        await callback.answer()
        return True
    if callback.data == "signup_photos_done":
        state["stage"] = "signup_submit"
        processing_msg = await callback.message.edit_text("Registering your account, please wait...", reply_markup=None)
        signup_result = await try_signup(state)
        if signup_result.get("user", {}).get("_id"):
            state["creds"] = {
                "email": state["email"],
                "password": state["password"],
                "name": state["name"],
            }
            state["stage"] = "await_verify"
            # Do an initial login to get the pending accessToken for subsequent verify attempts
            initial_login = await try_signin(state["email"], state["password"], state.get("device_info"))
            if initial_login.get("accessToken"):
                state["pending_access_token"] = initial_login["accessToken"]
            await processing_msg.edit_text("Account created! Please verify your email, then click the button below.", reply_markup=VERIFY_BUTTON)
        else:
            err = signup_result.get("errorMessage", "Registration failed.")
            state["stage"] = "menu"
            await processing_msg.edit_text(f"Signup failed: {err}", reply_markup=SIGNUP_MENU)
        return True
    return False

async def signup_message_handler(message: Message):
    user_id = message.from_user.id
    if user_id not in user_signup_states:
        return False
    state = user_signup_states[user_id]
    if message.text and message.text.startswith("/"):
        return False

    # SIGNUP FLOW
    if state.get("stage") == "ask_email":
        email = message.text.strip()
        ok, msg = await check_email_exists(email)
        if not ok:
            await message.answer(f"{msg}", reply_markup=BACK_TO_SIGNUP)
            return True
        state["stage"] = "ask_password"
        state["email"] = email
        await message.answer("Enter a password for your account:", reply_markup=BACK_TO_SIGNUP)
        return True
    if state.get("stage") == "ask_password":
        state["stage"] = "ask_name"
        state["password"] = message.text.strip()
        await message.answer("Enter your display name:", reply_markup=BACK_TO_SIGNUP)
        return True
    if state.get("stage") == "ask_name":
        state["stage"] = "ask_gender"
        state["name"] = message.text.strip()
        await message.answer("Enter your gender (M/F):", reply_markup=BACK_TO_SIGNUP)
        return True
    if state.get("stage") == "ask_gender":
        gender = message.text.strip().upper()
        if gender not in ("M", "F"):
            await message.answer("Please enter M or F for gender:")
            return True
        state["stage"] = "ask_signup_country"
        state["gender"] = gender
        await message.answer("Enter your signup country code (e.g. US, RU, UK):", reply_markup=BACK_TO_SIGNUP)
        return True
    if state.get("stage") == "ask_signup_country":
        signup_country = message.text.strip().upper()
        if not (2 <= len(signup_country) <= 3):
            await message.answer("Enter a valid 2- or 3-letter country code (e.g. US, RU, UK):", reply_markup=BACK_TO_SIGNUP)
            return True
        state["signup_country"] = signup_country
        state["stage"] = "ask_desc"
        await message.answer("Enter your profile description:", reply_markup=BACK_TO_SIGNUP)
        return True
    if state.get("stage") == "ask_desc":
        state["stage"] = "ask_photos"
        state["desc"] = message.text.strip()
        state["photos"] = []
        await message.answer(
            "Now send up to 6 profile pictures one by one. Send each as a photo (not file). "
            "When done, click 'Done' or send /done.", reply_markup=DONE_PHOTOS)
        return True
    if state.get("stage") == "ask_photos":
        if message.content_type == "photo":
            if len(state["photos"]) >= 6:
                await message.answer("You have already sent 6 photos. Click Done to finish or /done.")
                return True
            photo = message.photo[-1]
            file = await message.bot.get_file(photo.file_id)
            file_url = f"https://api.telegram.org/file/bot{message.bot.token}/{file.file_path}"
            async with aiohttp.ClientSession() as session:
                async with session.get(file_url) as resp:
                    img_bytes = await resp.read()
            img_url = await meeff_upload_image(img_bytes)
            if img_url:
                state["photos"].append(img_url)
                await message.answer(f"Photo uploaded ({len(state['photos'])}/6).")
            else:
                await message.answer("Failed to upload photo. Try again.")
            if len(state["photos"]) == 6:
                await message.answer("You've uploaded 6 photos. Click Done to finish or /done.", reply_markup=DONE_PHOTOS)
            return True
        elif message.text and message.text.strip().lower() == "/done":
            state["stage"] = "signup_submit"
            processing_msg = await message.answer("Registering your account, please wait...", reply_markup=None)
            signup_result = await try_signup(state)
            if signup_result.get("user", {}).get("_id"):
                state["creds"] = {
                    "email": state["email"],
                    "password": state["password"],
                    "name": state["name"],
                }
                state["stage"] = "await_verify"
                initial_login = await try_signin(state["email"], state["password"], state.get("device_info"))
                if initial_login.get("accessToken"):
                    state["pending_access_token"] = initial_login["accessToken"]
                await processing_msg.edit_text("Account created! Please verify your email, then click the button below.", reply_markup=VERIFY_BUTTON)
            else:
                err = signup_result.get("errorMessage", "Registration failed.")
                state["stage"] = "menu"
                await processing_msg.edit_text(f"Signup failed: {err}", reply_markup=SIGNUP_MENU)
            return True
        else:
            await message.answer("Please send a photo or click Done to finish.")
            return True

    # SIGNIN FLOW
    if state.get("stage") == "signin_email":
        state["stage"] = "signin_password"
        state["signin_email"] = message.text.strip()
        await message.answer("Enter your password:", reply_markup=BACK_TO_SIGNUP)
        return True
    if state.get("stage") == "signin_password":
        email = state["signin_email"]
        password = message.text.strip()
        processing_msg = await message.answer("Signing in, please wait...", reply_markup=None)
        login_result = await try_signin(email, password, state.get("device_info"))
        error_code = login_result.get("errorCode")
        access_token = login_result.get("accessToken")

        if access_token and not error_code:
            # Fully successful login — save and show card
            state["creds"] = {"email": email, "password": password}
            await store_token_and_show_card(processing_msg, login_result, state["creds"], device_info=state.get("device_info"))
            state["stage"] = "menu"

        elif error_code in ("EmailVerificationRequired", "NotVerified"):
            # Account exists but email not verified — do NOT save to DB
            # Store creds and pending token so user can verify and resend
            state["creds"] = {"email": email, "password": password}
            if access_token:
                state["pending_access_token"] = access_token
            # Auto-send verification email like signup flow does
            if access_token:
                await resend_verification_email(access_token)
            state["stage"] = "await_verify"
            await processing_msg.edit_text(
                "⚠️ <b>Email not verified.</b>\n\n"
                "A verification email has been sent to your inbox.\n"
                "Please verify your email, then click the button below.",
                reply_markup=RESEND_EMAIL_BUTTON,
                parse_mode="HTML",
            )

        elif error_code == "SecuredPendingDevice":
            state["creds"] = {"email": email, "password": password}
            if access_token:
                state["pending_access_token"] = access_token
            state["stage"] = "await_verify"
            await processing_msg.edit_text(
                "Device verification required!\n\n"
                "Check your mobile device for a verification notification and verify it there.\n\n"
                "Once verified on your mobile, click 'Verify' again to complete login.",
                reply_markup=VERIFY_BUTTON,
            )

        else:
            err = login_result.get("errorMessage", "Sign in failed.")
            await processing_msg.edit_text(f"Sign in failed: {err}", reply_markup=SIGNUP_MENU)
            state["stage"] = "menu"
        return True

    return False

async def meeff_upload_image(img_bytes):
    url = "https://api.meeff.com/api/upload/v1"
    payload = {
        "category": "profile",
        "count": 1,
        "locale": "en"
    }
    headers = {
        'User-Agent': "okhttp/5.3.2",
        'Accept-Encoding': "gzip",
        'Content-Type': "application/json; charset=utf-8"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=json.dumps(payload), headers=headers) as resp:
                resp_json = await resp.json()
                upload_info = resp_json.get("data", {}).get("uploadImageInfoList", [{}])[0]
                data = resp_json.get("data", {})
                if not upload_info or not data:
                    print("Meeff upload: missing upload_info or data.")
                    return None
                upload_url = data.get("Host")
                fields = {
                    "key": upload_info.get("key"),
                    "acl": data.get("acl"),
                    "Content-Type": data.get("Content-Type"),
                    "x-amz-meta-uuid": data.get("x-amz-meta-uuid"),
                    "X-Amz-Algorithm": upload_info.get("X-Amz-Algorithm") or data.get("X-Amz-Algorithm"),
                    "X-Amz-Credential": upload_info.get("X-Amz-Credential") or data.get("X-Amz-Credential"),
                    "X-Amz-Date": upload_info.get("X-Amz-Date") or data.get("X-Amz-Date"),
                    "Policy": upload_info.get("Policy") or data.get("Policy"),
                    "X-Amz-Signature": upload_info.get("X-Amz-Signature") or data.get("X-Amz-Signature"),
                }
                for k, v in fields.items():
                    if v is None:
                        print(f"Meeff S3 upload missing field: {k}")
                        return None
                form = aiohttp.FormData()
                for k, v in fields.items():
                    form.add_field(k, v)
                form.add_field('file', img_bytes, filename='photo.jpg', content_type='image/jpeg')
                async with session.post(upload_url, data=form) as s3resp:
                    if s3resp.status in (200, 204):
                        return upload_info.get("uploadImagePath")
                    else:
                        print(f"S3 upload failed: {s3resp.status} {await s3resp.text()}")
        return None
    except Exception as ex:
        print(f"Photo upload failed: {ex}")
        return None

async def try_signup(state):
    device_info = state.get("device_info", {})
    if state.get("photos"):
        photos_str = "|".join(state["photos"])
    else:
        photos_str = DEFAULT_PHOTOS
    url = "https://api.meeff.com/user/register/email/v4"
    payload = {
        "providerId": state["email"],
        "providerToken": state["password"],
        **device_info,
        "name": state["name"],
        "gender": state["gender"],
        "color": "124cd1",
        "birthYear": 2004,
        "birthMonth": 1,
        "birthDay": 2,
        "nationalityCode": state.get("signup_country", "US"),
        "languages": "en,ko,ja,be,uk,ru",
        "levels": "5,1,1,1,1,1",
        "description": state["desc"],
        "photos": photos_str,
        "purpose": "PB000000,PB000002",
        "purposeEtcDetail": "",
        "interest": "IS000001,IS000002,IS000010,IE000009,IE000011,IF000001,IF000012,IF000003,IH000004,IV000001",
        "locale": "en"
    }
    headers = {
        'User-Agent': "okhttp/5.3.2",
        'Accept-Encoding': "gzip",
        'content-type': "application/json; charset=utf-8"
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            result = await resp.json(content_type=None)

    # Step 2: Accept TOS — required after registration
    user_id_meeff = result.get("user", {}).get("_id")
    if user_id_meeff:
        await accept_tos(user_id_meeff)

    return result


async def accept_tos(meeff_user_id: str):
    """POST TOS acceptance after account registration — required by Meeff."""
    url = f"https://api.meeff.com/tos/info/v1/{meeff_user_id}"
    payload = {
        "TOS": True,
        "PRIVACY": True,
        "LOCATION": True,
        "MARKETING": True,
        "NIGHT": True,
        "AGE": True,
        "locale": "en"
    }
    headers = {
        'User-Agent': "okhttp/5.3.2",
        'Accept-Encoding': "gzip",
        'content-type': "application/json; charset=utf-8"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                return await resp.json(content_type=None)
    except Exception as ex:
        print(f"TOS acceptance failed: {ex}")
        return {}

async def try_signin(email, password, device_info=None, access_token=None):
    if not device_info:
        device_info = random_device_info()
    url = "https://api.meeff.com/user/login/v4"
    payload = {
        "provider": "email",
        "providerId": email,
        "providerToken": password,
        **device_info,
        "locale": "en"
    }
    headers = {
        'User-Agent': "okhttp/5.3.2",
        'Accept-Encoding': "gzip",
        'content-type': "application/json; charset=utf-8"
    }
    # When re-logging in after email verification, Meeff expects the token
    # from the initial login response as meeff-access-token
    if access_token:
        headers['meeff-access-token'] = access_token
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            result = await resp.json(content_type=None)
            if result.get("errorCode") == "SecuredPendingDevice":
                pending_device_id = result.get("pendingDeviceId")
                tok = result.get("accessToken")
                if pending_device_id and tok:
                    await verify_pending_device(tok, pending_device_id)
            return result

async def verify_pending_device(access_token, pending_device_id):
    """Verify a pending device when SecuredPendingDevice error occurs."""
    url = f"https://api.meeff.com/user/pendingdevice/{pending_device_id}/verify/v1"
    payload = {"locale": "en"}
    headers = {
        'User-Agent': "okhttp/5.3.2",
        'Accept-Encoding': "gzip",
        'meeff-access-token': access_token,
        'content-type': "application/json; charset=utf-8"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                return await resp.json(content_type=None)
    except Exception as ex:
        print(f"Pending device verification failed: {ex}")
        return {"errorCode": "unknown", "errorMessage": "Verification request failed."}

async def resend_verification_email(access_token):
    url = "https://api.meeff.com/user/resendEmailVerification/v1"
    payload = {"locale": "en"}
    headers = {
        'User-Agent': "okhttp/5.3.2",
        'Accept-Encoding': "gzip",
        'meeff-access-token': access_token,
        'content-type': "application/json; charset=utf-8"
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            try:
                return await resp.json(content_type=None)
            except Exception:
                return {"errorCode": "unknown", "errorMessage": "No response."}

async def store_token_and_show_card(msg_obj, login_result, creds, device_info=None):
    access_token = login_result.get("accessToken")
    user_data = login_result.get("user")
    email = creds.get("email")
    if access_token:
        user_id = msg_obj.chat.id
        account_name = user_data.get("name") if user_data else creds.get("email")
        await set_token(user_id, access_token, account_name, email)
        if device_info:
            await set_device_info(user_id, access_token, device_info)
        if not await get_user_filters(user_id, access_token):
            await set_user_filters(user_id, access_token, DEFAULT_FILTER)
        if user_data:
            user_data["email"] = creds.get("email")
            user_data["password"] = creds.get("password")
            user_data["token"] = access_token
            text = format_user_with_nationality(user_data)
            await set_info_card(user_id, access_token, text, email)
            await msg_obj.edit_text("Account signed in and saved!\n" + text, parse_mode="HTML")
        else:
            await msg_obj.edit_text(
                "Account signed in, but no profile info found. Please verify your email and try again.",
                reply_markup=RESEND_EMAIL_BUTTON
            )
    else:
        await msg_obj.edit_text("Failed to sign in after registration.", reply_markup=SIGNUP_MENU)
