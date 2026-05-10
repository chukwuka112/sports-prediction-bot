# /// script
# requires-python = "==3.11.*"
# dependencies = [
#   "codewords-client==0.4.6",
#   "fastapi==0.116.1",
#   "httpx==0.28.1",
#   "Pillow==10.4.0",
#   "python-multipart==0.0.12",
# ]
# [tool.env-checker]
# env_vars = [
#   "PORT=8000",
#   "LOGLEVEL=INFO",
#   "CODEWORDS_API_KEY",
#   "CODEWORDS_RUNTIME_URI",
#   "TELEGRAM_BOT_TOKEN",
#   "NOWPAYMENTS_API_KEY",
#   "NOWPAYMENTS_IPN_SECRET",
#   "ADMIN_CHAT_IDS",
# ]
# ///

"""
Telegram Sports Prediction Bot

Features:
- Free & Premium predictions with photo + text + optional link
- Premium tips show pixelated/watermarked preview until unlocked
- NOWPayments crypto unlock (all wallet options)
- Admin panel: post tips, approve users, broadcast, set results (WON/LOST/VOID)
- Referral system with configurable commission %
- Tip/donate to admin via crypto
- Full tracking (views, payments) per user & prediction
- Results broadcast to all approved users
"""

import asyncio
import hashlib
import hmac
import io
import json
import os
import random
import string
import time
import uuid
from datetime import datetime
from typing import Optional

import httpx
from codewords_client import AsyncCodewordsClient, logger, redis_client, run_service
from fastapi import BackgroundTasks, FastAPI, Request, Response
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "")
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
ADMIN_IDS = {x.strip() for x in os.environ.get("ADMIN_CHAT_IDS", "").split(",") if x.strip()}

SHUFFLE_URL = "https://shuffle.com/?r=f20anQxZ3a"
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
NOWPAYMENTS_API = "https://api.nowpayments.io/v1"

CURRENCY_LABELS: dict = {
    "btc": "BTC (Bitcoin)",
    "eth": "ETH (Ethereum)",
    "usdterc20": "USDT ERC20",
    "usdttrc20": "USDT TRC20",
    "bnbbsc": "BNB (BSC)",
    "sol": "SOL (Solana)",
    "ltc": "LTC (Litecoin)",
    "xrp": "XRP (Ripple)",
    "doge": "DOGE (Dogecoin)",
}


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------

async def get_settings(redis, ns: str) -> dict:
    raw = await redis.get(f"{ns}:settings")
    if raw:
        return json.loads(raw)
    return {"default_premium_price_usd": 10.0, "commission_pct": 10.0, "shuffle_url": SHUFFLE_URL}


async def save_settings(redis, ns: str, settings: dict):
    await redis.set(f"{ns}:settings", json.dumps(settings))


async def get_user(redis, ns: str, user_id: str) -> Optional[dict]:
    raw = await redis.get(f"{ns}:user:{user_id}")
    return json.loads(raw) if raw else None


async def save_user(redis, ns: str, user: dict):
    await redis.set(f"{ns}:user:{user['user_id']}", json.dumps(user))
    users_raw = await redis.get(f"{ns}:users_list")
    users: list = json.loads(users_raw) if users_raw else []
    if user["user_id"] not in users:
        users.append(user["user_id"])
        await redis.set(f"{ns}:users_list", json.dumps(users))


async def get_all_user_ids(redis, ns: str) -> list:
    raw = await redis.get(f"{ns}:users_list")
    return json.loads(raw) if raw else []


async def get_state(redis, ns: str, user_id: str) -> dict:
    raw = await redis.get(f"{ns}:state:{user_id}")
    return json.loads(raw) if raw else {"state": "idle", "data": {}}


async def save_state(redis, ns: str, user_id: str, state: str, data: Optional[dict] = None):
    await redis.set(f"{ns}:state:{user_id}", json.dumps({"state": state, "data": data or {}}))


async def clear_state(redis, ns: str, user_id: str):
    await save_state(redis, ns, user_id, "idle", {})


async def get_prediction(redis, ns: str, pred_id: str) -> Optional[dict]:
    raw = await redis.get(f"{ns}:prediction:{pred_id}")
    return json.loads(raw) if raw else None


async def save_prediction(redis, ns: str, pred: dict):
    await redis.set(f"{ns}:prediction:{pred['id']}", json.dumps(pred))
    preds_raw = await redis.get(f"{ns}:predictions_list")
    preds: list = json.loads(preds_raw) if preds_raw else []
    if pred["id"] not in preds:
        preds.insert(0, pred["id"])
        await redis.set(f"{ns}:predictions_list", json.dumps(preds))


async def get_recent_predictions(redis, ns: str, limit: int = 20) -> list:
    raw = await redis.get(f"{ns}:predictions_list")
    pred_ids: list = json.loads(raw) if raw else []
    result = []
    for pid in pred_ids[:limit]:
        p = await get_prediction(redis, ns, pid)
        if p:
            result.append(p)
    return result


async def get_payment(redis, ns: str, payment_id: str) -> Optional[dict]:
    raw = await redis.get(f"{ns}:payment:{payment_id}")
    return json.loads(raw) if raw else None


async def save_payment(redis, ns: str, payment: dict):
    await redis.set(f"{ns}:payment:{payment['id']}", json.dumps(payment))
    if payment.get("nowpayments_id"):
        await redis.set(f"{ns}:np_map:{payment['nowpayments_id']}", payment["id"])


async def has_unlocked(redis, ns: str, user_id: str, pred_id: str) -> bool:
    raw = await redis.get(f"{ns}:unlock:{user_id}:{pred_id}")
    return raw is not None


async def mark_unlocked(redis, ns: str, user_id: str, pred_id: str, payment_id: str):
    await redis.set(
        f"{ns}:unlock:{user_id}:{pred_id}",
        json.dumps({"payment_id": payment_id, "paid_at": time.time()}),
    )


async def get_referral_owner(redis, ns: str, code: str) -> Optional[str]:
    return await redis.get(f"{ns}:referral:{code}")


async def save_referral_code(redis, ns: str, code: str, user_id: str):
    await redis.set(f"{ns}:referral:{code}", user_id)


async def get_pending_users(redis, ns: str) -> list:
    uids = await get_all_user_ids(redis, ns)
    result = []
    for uid in uids:
        u = await get_user(redis, ns, uid)
        if u and u.get("status") == "pending" and u.get("username"):
            result.append(u)
    return result


# ---------------------------------------------------------------------------
# Telegram API wrappers
# ---------------------------------------------------------------------------

async def tg_call(method: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{TG_API}/{method}", json=payload)
        result = resp.json()
        if not result.get("ok"):
            logger.warning("Telegram API error", method=method, error=result.get("description"))
        return result


async def send_message(chat_id, text: str, reply_markup: Optional[dict] = None, parse_mode: str = "HTML") -> dict:
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await tg_call("sendMessage", payload)


async def send_photo(
    chat_id, photo: str, caption: Optional[str] = None,
    reply_markup: Optional[dict] = None, parse_mode: str = "HTML"
) -> dict:
    payload: dict = {"chat_id": chat_id, "photo": photo}
    if caption:
        payload["caption"] = caption
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await tg_call("sendPhoto", payload)


async def answer_callback_query(cq_id: str, text: Optional[str] = None) -> dict:
    payload: dict = {"callback_query_id": cq_id}
    if text:
        payload["text"] = text
    return await tg_call("answerCallbackQuery", payload)


async def download_telegram_file(file_id: str) -> bytes:
    result = await tg_call("getFile", {"file_id": file_id})
    if not result.get("ok"):
        raise ValueError(f"getFile failed: {result}")
    file_path = result["result"]["file_path"]
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}")
        resp.raise_for_status()
        return resp.content


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def user_main_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "\U0001f4ca Free Tips"}, {"text": "\U0001f48e Premium Tips"}],
            [{"text": "\U0001f465 My Referral"}, {"text": "\U0001f4a1 Tip Admin"}],
            [{"text": "\U0001f464 My Profile"}],
        ],
        "resize_keyboard": True,
    }


def admin_main_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "\u2795 New Free Tip"}, {"text": "\U0001f48e New Premium Tip"}],
            [{"text": "\u2705 Approve Users"}, {"text": "\U0001f4e2 Broadcast"}],
            [{"text": "\U0001f4ca Stats"}, {"text": "\u2699\ufe0f Settings"}],
        ],
        "resize_keyboard": True,
    }


def cancel_keyboard() -> dict:
    return {"keyboard": [[{"text": "\u274c Cancel"}]], "resize_keyboard": True}


def submit_keyboard() -> dict:
    return {"keyboard": [[{"text": "\U0001f4dd Submit Username for Approval"}]], "resize_keyboard": True}


def inline_unlock_button(pred_id: str, price: float) -> dict:
    return {"inline_keyboard": [[{"text": f"\U0001f513 Unlock  ${price:.2f}", "callback_data": f"unlock:{pred_id}"}]]}


def inline_currency_buttons(pred_ref: str) -> dict:
    """pred_ref = pred_id for unlocks, or 'tip:{payment_id}' for tips."""
    buttons: list = []
    row: list = []
    for code, label in CURRENCY_LABELS.items():
        row.append({"text": label, "callback_data": f"pay:{pred_ref}:{code}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([{"text": "\u274c Cancel", "callback_data": "cancel_payment"}])
    return {"inline_keyboard": buttons}


def inline_result_buttons(pred_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "\u2705 WON", "callback_data": f"result:{pred_id}:won"},
            {"text": "\u274c LOST", "callback_data": f"result:{pred_id}:lost"},
            {"text": "\u26aa VOID", "callback_data": f"result:{pred_id}:void"},
        ]]
    }


def inline_approve_reject(user_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "\u2705 Approve", "callback_data": f"approve:{user_id}"},
            {"text": "\u274c Reject", "callback_data": f"reject:{user_id}"},
        ]]
    }


# ---------------------------------------------------------------------------
# Image processing
# ---------------------------------------------------------------------------

def pixelate_image(image_bytes: bytes, pixel_size: int = 22) -> bytes:
    """Pixelate + watermark an image for locked premium previews."""
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    small = img.resize((max(1, w // pixel_size), max(1, h // pixel_size)), Image.NEAREST)
    pixelated = small.resize((w, h), Image.NEAREST)
    draw = ImageDraw.Draw(pixelated)
    banner_h = max(70, h // 5)
    y0 = (h - banner_h) // 2
    draw.rectangle([(0, y0), (w, y0 + banner_h)], fill=(10, 10, 10))
    lines = ["PREMIUM CONTENT", "Tap Unlock to reveal the full tip"]
    gap = banner_h // (len(lines) + 1)
    for i, line in enumerate(lines):
        y = y0 + gap * (i + 1)
        try:
            bbox = draw.textbbox((0, 0), line)
            tx = max(4, (w - (bbox[2] - bbox[0])) // 2)
            draw.text((tx, y - (bbox[3] - bbox[1]) // 2), line, fill=(255, 255, 255))
        except Exception:
            draw.text((10, y), line, fill=(255, 255, 255))
    out = io.BytesIO()
    pixelated.save(out, format="JPEG", quality=80)
    return out.getvalue()


# ---------------------------------------------------------------------------
# NOWPayments API
# ---------------------------------------------------------------------------

async def create_nowpayments_payment(
    amount_usd: float, pay_currency: str, order_id: str,
    description: str, ipn_callback_url: str,
) -> dict:
    headers = {"x-api-key": NOWPAYMENTS_API_KEY, "Content-Type": "application/json"}
    payload = {
        "price_amount": amount_usd, "price_currency": "usd",
        "pay_currency": pay_currency, "order_id": order_id,
        "order_description": description, "ipn_callback_url": ipn_callback_url,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{NOWPAYMENTS_API}/payment", json=payload, headers=headers)
        data = resp.json()
        logger.info("NOWPayments payment created", order_id=order_id, status=resp.status_code)
        return data


def verify_nowpayments_signature(raw_body: bytes, signature: str) -> bool:
    try:
        payload = json.loads(raw_body)
        sorted_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        expected = hmac.new(
            NOWPAYMENTS_IPN_SECRET.encode(), sorted_payload.encode(), hashlib.sha512
        ).hexdigest()
        return hmac.compare_digest(expected, signature.lower())
    except Exception as exc:
        logger.error("IPN signature error", error=str(exc))
        return False


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def generate_referral_code(length: int = 8) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


def is_admin(user_id) -> bool:
    return str(user_id) in ADMIN_IDS


def fmt_prediction(pred: dict, show_link: bool = True) -> str:
    status_emoji = {"active": "\U0001f7e1", "won": "\u2705", "lost": "\u274c", "void": "\u26aa"}.get(
        pred["status"], "\U0001f7e1"
    )
    tier = "\U0001f48e" if pred["type"] == "premium" else "\U0001f193"
    lines = [f"{tier} <b>Prediction</b> {status_emoji}", "", pred["text"]]
    if pred.get("status") != "active":
        lines.append(f"\n<b>Result: {pred['status'].upper()}</b>")
    if show_link and pred.get("link"):
        lines.append(f'\n\U0001f517 <a href="{pred["link"]}">View More</a>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Update router
# ---------------------------------------------------------------------------

async def handle_update(update: dict, base_url: str):
    """Route incoming Telegram update."""
    if "callback_query" in update:
        await handle_callback_query(update["callback_query"], base_url)
        return
    message = update.get("message", {})
    if not message:
        return
    user_id = str(message.get("from", {}).get("id", ""))
    chat_id = message.get("chat", {}).get("id", user_id)
    text = message.get("text", "")
    photo = message.get("photo")
    if not user_id:
        return
    logger.info("Message received", user_id=user_id, has_photo=bool(photo), text=text[:40])
    async with redis_client() as (redis, ns):
        if base_url and not await redis.get(f"{ns}:settings:base_url"):
            await redis.set(f"{ns}:settings:base_url", base_url)
        if is_admin(user_id):
            await handle_admin_message(redis, ns, message, user_id, int(chat_id), text, photo, base_url)
            return
        user = await get_user(redis, ns, user_id)
        state_obj = await get_state(redis, ns, user_id)
        state = state_obj["state"]
        state_data = state_obj["data"]
        if text and text.startswith("/start"):
            await handle_start(redis, ns, user, user_id, int(chat_id))
            return
        if not user:
            await send_message(chat_id, "Please use /start to register.")
            return
        if state == "awaiting_username":
            await handle_username_submission(redis, ns, user, user_id, int(chat_id), text)
        elif state == "awaiting_referral_code":
            await handle_referral_code_input(redis, ns, user, user_id, int(chat_id), text)
        elif state == "awaiting_tip_amount":
            await handle_tip_amount_input(redis, ns, user, user_id, int(chat_id), text, base_url)
        elif text:
            await handle_menu_selection(redis, ns, user, user_id, int(chat_id), text, base_url)


# ---------------------------------------------------------------------------
# User handlers
# ---------------------------------------------------------------------------

async def handle_start(redis, ns, user, user_id: str, chat_id: int):
    if not user:
        code = generate_referral_code()
        while await get_referral_owner(redis, ns, code):
            code = generate_referral_code()
        user = {
            "user_id": user_id, "username": "", "status": "pending",
            "referral_code": code, "referred_by_code": None,
            "commission_balance": 0.0, "joined_at": time.time(), "total_spent": 0.0,
        }
        await save_referral_code(redis, ns, code, user_id)
        await save_user(redis, ns, user)
    if user["status"] == "approved":
        await send_message(chat_id, "\U0001f44b Welcome back! Use the menu below.", reply_markup=user_main_keyboard())
        return
    if user["status"] == "rejected":
        await send_message(chat_id, "\u274c Your access was declined. Contact support.")
        return
    if user["status"] == "banned":
        await send_message(chat_id, "\U0001f6ab Your account has been banned.")
        return
    settings = await get_settings(redis, ns)
    shuffle_url = settings.get("shuffle_url", SHUFFLE_URL)
    msg = (
        "\U0001f44b <b>Welcome to the Sports Prediction Bot!</b>\n\n"
        "To get access, follow these 2 steps:\n\n"
        f"1\ufe0f\u20e3 <b>Join Shuffle Casino</b> via our referral link:\n"
        f'<a href="{shuffle_url}">\U0001f449 Join Shuffle here</a>\n\n'
        "2\ufe0f\u20e3 Tap the button below to submit your Telegram username for approval.\n\n"
        "<i>You will be notified as soon as the admin approves your account.</i>"
    )
    await send_message(chat_id, msg, reply_markup=submit_keyboard())


async def handle_username_submission(redis, ns, user, user_id: str, chat_id: int, text: str):
    if text == "\u274c Cancel" or not text:
        await clear_state(redis, ns, user_id)
        await send_message(chat_id, "Cancelled.", reply_markup=submit_keyboard())
        return
    username = text.strip().lstrip("@")
    if not username or len(username) > 64:
        await send_message(chat_id, "Please send a valid Telegram username (without @).")
        return
    user["username"] = username
    await save_user(redis, ns, user)
    await clear_state(redis, ns, user_id)
    await send_message(
        chat_id,
        f"\u2705 Username <b>@{username}</b> submitted for approval!\n\nYou'll be notified once the admin reviews your request.",
        reply_markup=submit_keyboard(),
    )
    for admin_id in ADMIN_IDS:
        await send_message(
            int(admin_id),
            f"\U0001f514 <b>New Approval Request</b>\n\nUser ID: <code>{user_id}</code>\nUsername: @{username}",
            reply_markup=inline_approve_reject(user_id),
        )


async def handle_referral_code_input(redis, ns, user, user_id: str, chat_id: int, text: str):
    if text == "\u274c Cancel":
        await clear_state(redis, ns, user_id)
        await send_message(chat_id, "Cancelled.", reply_markup=user_main_keyboard())
        return
    code = text.strip().upper()
    owner_id = await get_referral_owner(redis, ns, code)
    if not owner_id:
        await send_message(chat_id, "\u274c Invalid referral code. Try again or tap Cancel.")
        return
    if owner_id == user_id:
        await send_message(chat_id, "\u274c You cannot use your own referral code.")
        return
    if user.get("referred_by_code"):
        await clear_state(redis, ns, user_id)
        await send_message(chat_id, "\u274c You have already applied a referral code.", reply_markup=user_main_keyboard())
        return
    user["referred_by_code"] = code
    await save_user(redis, ns, user)
    await clear_state(redis, ns, user_id)
    await send_message(chat_id, "\u2705 Referral code applied! You'll earn your referrer a commission on your premium unlocks.", reply_markup=user_main_keyboard())


async def handle_tip_amount_input(redis, ns, user, user_id: str, chat_id: int, text: str, base_url: str):
    if text == "\u274c Cancel":
        await clear_state(redis, ns, user_id)
        await send_message(chat_id, "Cancelled.", reply_markup=user_main_keyboard())
        return
    try:
        amount = float(text.strip().replace("$", ""))
        if amount < 1.0:
            await send_message(chat_id, "\u274c Minimum tip is $1.00.")
            return
    except ValueError:
        await send_message(chat_id, "\u274c Please enter a valid amount e.g. <code>5</code> or <code>10.50</code>.")
        return
    await clear_state(redis, ns, user_id)
    payment_id = uuid.uuid4().hex
    payment = {
        "id": payment_id, "user_id": user_id, "type": "tip", "pred_id": None,
        "amount_usd": amount, "status": "awaiting_currency",
        "nowpayments_id": None, "address": None, "crypto": None, "created_at": time.time(),
    }
    await save_payment(redis, ns, payment)
    await send_message(
        chat_id,
        f"\U0001f4a1 <b>Tip Admin ${amount:.2f}</b>\n\nChoose your preferred cryptocurrency:",
        reply_markup=inline_currency_buttons(f"tip:{payment_id}"),
    )


async def handle_menu_selection(redis, ns, user, user_id: str, chat_id: int, text: str, base_url: str):
    if text == "\U0001f4dd Submit Username for Approval":
        if user["status"] == "pending":
            await save_state(redis, ns, user_id, "awaiting_username")
            await send_message(chat_id, "Please send your Telegram username (without @):", reply_markup=cancel_keyboard())
        else:
            await send_message(chat_id, "Your application has already been processed.")
        return
    if user["status"] != "approved":
        await send_message(chat_id, "\u23f3 Your account is pending approval. You'll be notified once approved.")
        return
    if text == "\U0001f4ca Free Tips":
        await show_free_tips(redis, ns, user_id, chat_id)
    elif text == "\U0001f48e Premium Tips":
        await show_premium_tips(redis, ns, user_id, chat_id)
    elif text == "\U0001f465 My Referral":
        await show_referral_info(redis, ns, user, user_id, chat_id)
    elif text == "\U0001f4a1 Tip Admin":
        await save_state(redis, ns, user_id, "awaiting_tip_amount")
        await send_message(
            chat_id,
            "\U0001f4a1 <b>Tip the Admin</b>\n\nEnter the amount in USD (e.g. <code>5</code> or <code>10.50</code>):",
            reply_markup=cancel_keyboard(),
        )
    elif text == "\U0001f464 My Profile":
        await show_profile(redis, ns, user, chat_id)
    else:
        await send_message(chat_id, "Use the menu buttons below.", reply_markup=user_main_keyboard())


async def show_free_tips(redis, ns, user_id: str, chat_id: int):
    preds = await get_recent_predictions(redis, ns)
    free_tips = [p for p in preds if p["type"] == "free"]
    if not free_tips:
        await send_message(chat_id, "\U0001f4ca No free tips available yet. Check back soon!")
        return
    await send_message(chat_id, f"\U0001f4ca <b>Free Tips</b> ({len(free_tips)} available):")
    for pred in free_tips[:5]:
        await redis.incr(f"{ns}:views:{pred['id']}")
        await send_photo(chat_id, pred["photo_file_id"], caption=fmt_prediction(pred, show_link=True))
        await asyncio.sleep(0.15)


async def show_premium_tips(redis, ns, user_id: str, chat_id: int):
    preds = await get_recent_predictions(redis, ns)
    premium_tips = [p for p in preds if p["type"] == "premium"]
    if not premium_tips:
        await send_message(chat_id, "\U0001f48e No premium tips available yet. Check back soon!")
        return
    await send_message(chat_id, f"\U0001f48e <b>Premium Tips</b> ({len(premium_tips)} available):")
    for pred in premium_tips[:5]:
        await redis.incr(f"{ns}:views:{pred['id']}")
        unlocked = await has_unlocked(redis, ns, user_id, pred["id"])
        if unlocked:
            caption = fmt_prediction(pred, show_link=True) + "\n\n\u2705 <i>Unlocked</i>"
            await send_photo(chat_id, pred["photo_file_id"], caption=caption)
        else:
            caption = (
                "\U0001f48e <b>Premium Tip</b>\n\n"
                "\U0001f512 This tip is locked. Tap Unlock to reveal the full prediction!\n\n"
                f"\U0001f4b0 Price: <b>${pred['price_usd']:.2f}</b>"
            )
            rm = inline_unlock_button(pred["id"], pred["price_usd"])
            if pred.get("pixelated_url"):
                await send_photo(chat_id, pred["pixelated_url"], caption=caption, reply_markup=rm)
            else:
                await send_message(chat_id, caption, reply_markup=rm)
        await asyncio.sleep(0.15)


async def show_referral_info(redis, ns, user: dict, user_id: str, chat_id: int):
    code = user.get("referral_code", "")
    settings = await get_settings(redis, ns)
    commission_pct = settings.get("commission_pct", 10.0)
    all_uids = await get_all_user_ids(redis, ns)
    referral_count = 0
    for uid in all_uids:
        u = await get_user(redis, ns, uid)
        if u and u.get("referred_by_code") == code:
            referral_count += 1
    balance = user.get("commission_balance", 0.0)
    msg = (
        f"\U0001f465 <b>Your Referral Dashboard</b>\n\n"
        f"\U0001f517 Your Code: <code>{code}</code>\n"
        f"Share this code with friends. They enter it when joining.\n\n"
        f"\U0001f4ca <b>Stats</b>\n"
        f"\u2022 Referrals: <b>{referral_count}</b>\n"
        f"\u2022 Commission Rate: <b>{commission_pct:.0f}%</b>\n"
        f"\u2022 Pending Balance: <b>${balance:.2f}</b>\n\n"
        f"<i>Earn {commission_pct:.0f}% every time your referrals buy premium tips.</i>"
    )
    rm = None
    if balance >= 1.0:
        rm = {"inline_keyboard": [[{"text": f"\U0001f4b0 Withdraw ${balance:.2f}", "callback_data": f"withdraw:{user_id}"}]]}
    await send_message(chat_id, msg, reply_markup=rm)


async def show_profile(redis, ns, user: dict, chat_id: int):
    statuses = {"pending": "\u23f3 Pending", "approved": "\u2705 Approved", "rejected": "\u274c Rejected", "banned": "\U0001f6ab Banned"}
    joined = datetime.fromtimestamp(user.get("joined_at", time.time())).strftime("%Y-%m-%d")
    msg = (
        f"\U0001f464 <b>Your Profile</b>\n\n"
        f"\U0001f194 ID: <code>{user['user_id']}</code>\n"
        f"\U0001f464 Username: @{user.get('username', 'Not set')}\n"
        f"\U0001f4cc Status: {statuses.get(user['status'], user['status'])}\n"
        f"\U0001f4c5 Joined: {joined}\n"
        f"\U0001f4b0 Total Spent: ${user.get('total_spent', 0.0):.2f}\n"
        f"\U0001f381 Referral Code: <code>{user.get('referral_code', 'N/A')}</code>"
    )
    await send_message(chat_id, msg)


# ---------------------------------------------------------------------------
# Admin handlers
# ---------------------------------------------------------------------------

async def handle_admin_message(redis, ns, message: dict, user_id: str, chat_id: int, text: str, photo: list, base_url: str):
    state_obj = await get_state(redis, ns, user_id)
    state = state_obj["state"]
    state_data = state_obj["data"]
    if text == "/start":
        await clear_state(redis, ns, user_id)
        await send_message(chat_id, "\U0001f451 <b>Admin Panel</b>\nWelcome back!", reply_markup=admin_main_keyboard())
        return
    if text == "\u274c Cancel":
        await clear_state(redis, ns, user_id)
        await send_message(chat_id, "Cancelled.", reply_markup=admin_main_keyboard())
        return
    if not state.startswith(("posting_", "broadcasting", "settings")):
        await handle_admin_menu(redis, ns, user_id, chat_id, text)
        return
    if state.startswith("posting_free_tip"):
        await admin_free_tip_state(redis, ns, user_id, chat_id, state, state_data, text, photo)
    elif state.startswith("posting_premium_tip"):
        await admin_premium_tip_state(redis, ns, user_id, chat_id, state, state_data, text, photo)
    elif state == "broadcasting":
        await admin_broadcast_state(redis, ns, user_id, chat_id, text, photo)
    elif state.startswith("settings"):
        await admin_settings_state(redis, ns, user_id, chat_id, state, state_data, text)


async def handle_admin_menu(redis, ns, user_id: str, chat_id: int, text: str):
    if text == "\u2795 New Free Tip":
        await save_state(redis, ns, user_id, "posting_free_tip:photo", {})
        await send_message(chat_id, "\U0001f4f8 <b>New Free Tip</b>\n\nSend the prediction screenshot:", reply_markup=cancel_keyboard())
    elif text == "\U0001f48e New Premium Tip":
        await save_state(redis, ns, user_id, "posting_premium_tip:photo", {})
        await send_message(chat_id, "\U0001f4f8 <b>New Premium Tip</b>\n\nSend the prediction screenshot:", reply_markup=cancel_keyboard())
    elif text == "\u2705 Approve Users":
        await admin_show_pending(redis, ns, chat_id)
    elif text == "\U0001f4e2 Broadcast":
        await save_state(redis, ns, user_id, "broadcasting", {})
        await send_message(chat_id, "\U0001f4e2 <b>Broadcast</b>\n\nSend the message (text and/or photo) to broadcast to all approved users:", reply_markup=cancel_keyboard())
    elif text == "\U0001f4ca Stats":
        await admin_show_stats(redis, ns, chat_id)
    elif text == "\u2699\ufe0f Settings":
        await admin_show_settings(redis, ns, chat_id)
    else:
        await send_message(chat_id, "Use the admin menu.", reply_markup=admin_main_keyboard())


async def admin_show_pending(redis, ns, chat_id: int):
    pending = await get_pending_users(redis, ns)
    if not pending:
        await send_message(chat_id, "\u2705 No pending users.", reply_markup=admin_main_keyboard())
        return
    await send_message(chat_id, f"\u23f3 <b>{len(pending)} pending request(s):</b>")
    for u in pending[:10]:
        joined = datetime.fromtimestamp(u.get("joined_at", time.time())).strftime("%Y-%m-%d %H:%M")
        await send_message(
            chat_id,
            f"\U0001f464 <b>Approval Request</b>\nID: <code>{u['user_id']}</code>\nUsername: @{u.get('username', 'N/A')}\nApplied: {joined}",
            reply_markup=inline_approve_reject(u["user_id"]),
        )


async def admin_show_stats(redis, ns, chat_id: int):
    all_uids = await get_all_user_ids(redis, ns)
    approved = pending = 0
    total_rev = 0.0
    for uid in all_uids:
        u = await get_user(redis, ns, uid)
        if u:
            if u["status"] == "approved":
                approved += 1
            elif u["status"] == "pending":
                pending += 1
            total_rev += u.get("total_spent", 0.0)
    preds_raw = await redis.get(f"{ns}:predictions_list")
    pred_ids = json.loads(preds_raw) if preds_raw else []
    free_c = sum(1 for pid in pred_ids if (lambda p: p and p["type"] == "free")(None))
    free_c = 0
    prem_c = 0
    for pid in pred_ids:
        p = await get_prediction(redis, ns, pid)
        if p:
            if p["type"] == "free":
                free_c += 1
            else:
                prem_c += 1
    msg = (
        f"\U0001f4ca <b>Bot Statistics</b>\n\n"
        f"\U0001f465 Users: {len(all_uids)} total | \u2705 {approved} approved | \u23f3 {pending} pending\n\n"
        f"\U0001f4cb Tips: {len(pred_ids)} total | \U0001f193 {free_c} free | \U0001f48e {prem_c} premium\n\n"
        f"\U0001f4b0 Total Revenue: ${total_rev:.2f}"
    )
    await send_message(chat_id, msg, reply_markup=admin_main_keyboard())


async def admin_show_settings(redis, ns, chat_id: int):
    s = await get_settings(redis, ns)
    rm = {"inline_keyboard": [
        [{"text": "\U0001f4b0 Set Premium Price", "callback_data": "admin_set:price"}],
        [{"text": "\U0001f465 Set Commission %", "callback_data": "admin_set:commission"}],
    ]}
    await send_message(
        chat_id,
        f"\u2699\ufe0f <b>Settings</b>\n\n"
        f"\U0001f4b0 Default Premium Price: <b>${s.get('default_premium_price_usd', 10.0):.2f}</b>\n"
        f"\U0001f465 Referral Commission: <b>{s.get('commission_pct', 10.0):.0f}%</b>",
        reply_markup=rm,
    )


async def admin_free_tip_state(redis, ns, user_id: str, chat_id: int, state: str, state_data: dict, text: str, photo: list):
    if state == "posting_free_tip:photo":
        if not photo:
            await send_message(chat_id, "\U0001f4f8 Please send an image/screenshot.")
            return
        state_data["photo_file_id"] = photo[-1]["file_id"]
        await save_state(redis, ns, user_id, "posting_free_tip:details", state_data)
        await send_message(
            chat_id,
            "\u2705 Photo received!\n\n\U0001f4dd Now send the match details and tip text:\n<i>Example:\nArsenal vs Chelsea\nPrediction: Over 2.5 Goals\nOdds: 1.85</i>",
            reply_markup=cancel_keyboard(),
        )
    elif state == "posting_free_tip:details":
        state_data["text"] = text
        await save_state(redis, ns, user_id, "posting_free_tip:link", state_data)
        await send_message(
            chat_id, "\U0001f517 Send an optional link (e.g. for more analysis), or skip:",
            reply_markup={"keyboard": [[{"text": "\u23ed Skip Link"}, {"text": "\u274c Cancel"}]], "resize_keyboard": True},
        )
    elif state == "posting_free_tip:link":
        link = None if text in ("\u23ed Skip Link", "skip") else text.strip()
        if link and not link.startswith("http"):
            link = None
        pred_id = uuid.uuid4().hex[:8].upper()
        pred = {
            "id": pred_id, "type": "free", "photo_file_id": state_data["photo_file_id"],
            "pixelated_url": None, "text": state_data["text"], "link": link,
            "price_usd": 0.0, "status": "active", "created_at": time.time(),
        }
        await save_prediction(redis, ns, pred)
        await clear_state(redis, ns, user_id)
        await send_message(
            chat_id,
            f"\u2705 <b>Free Tip Posted!</b>\nID: <code>{pred_id}</code>\nBroadcasting to all users...",
            reply_markup=admin_main_keyboard(),
        )
        await send_message(chat_id, f"\U0001f4cc Mark result for tip <code>{pred_id}</code> when the match ends:", reply_markup=inline_result_buttons(pred_id))
        await broadcast_prediction(redis, ns, pred)


async def admin_premium_tip_state(redis, ns, user_id: str, chat_id: int, state: str, state_data: dict, text: str, photo: list):
    if state == "posting_premium_tip:photo":
        if not photo:
            await send_message(chat_id, "\U0001f4f8 Please send an image/screenshot.")
            return
        state_data["photo_file_id"] = photo[-1]["file_id"]
        await save_state(redis, ns, user_id, "posting_premium_tip:details", state_data)
        await send_message(chat_id, "\u2705 Photo received!\n\n\U0001f4dd Now send the match details and tip text:", reply_markup=cancel_keyboard())
    elif state == "posting_premium_tip:details":
        state_data["text"] = text
        settings = await get_settings(redis, ns)
        dp = settings.get("default_premium_price_usd", 10.0)
        await save_state(redis, ns, user_id, "posting_premium_tip:price", state_data)
        await send_message(
            chat_id, f"\U0001f4b0 Set the unlock price in USD (default: ${dp:.2f}):",
            reply_markup={"keyboard": [[{"text": f"\u2705 Use Default (${dp:.2f})"}, {"text": "\u274c Cancel"}]], "resize_keyboard": True},
        )
    elif state == "posting_premium_tip:price":
        settings = await get_settings(redis, ns)
        dp = settings.get("default_premium_price_usd", 10.0)
        if text.startswith("\u2705 Use Default"):
            price = dp
        else:
            try:
                price = float(text.strip().replace("$", ""))
            except ValueError:
                await send_message(chat_id, "\u274c Invalid price. Enter a number or use default.")
                return
        state_data["price"] = price
        await save_state(redis, ns, user_id, "posting_premium_tip:link", state_data)
        await send_message(
            chat_id, "\U0001f517 Send an optional link, or skip:",
            reply_markup={"keyboard": [[{"text": "\u23ed Skip Link"}, {"text": "\u274c Cancel"}]], "resize_keyboard": True},
        )
    elif state == "posting_premium_tip:link":
        link = None if text in ("\u23ed Skip Link", "skip") else text.strip()
        if link and not link.startswith("http"):
            link = None
        await send_message(chat_id, "\u23f3 Processing image, please wait...")
        pixelated_url = None
        try:
            img_bytes = await download_telegram_file(state_data["photo_file_id"])
            pix_bytes = pixelate_image(img_bytes)
            async with AsyncCodewordsClient() as cw:
                pixelated_url = await cw.upload_file_content(
                    filename=f"preview_{uuid.uuid4().hex[:8]}.jpg",
                    file_content=pix_bytes,
                    content_type="image/jpeg",
                )
        except Exception as exc:
            logger.error("Pixelation failed", error=str(exc))
            await send_message(chat_id, f"\u26a0\ufe0f Image processing failed ({exc}). Tip will be created without visual preview.")
        pred_id = uuid.uuid4().hex[:8].upper()
        pred = {
            "id": pred_id, "type": "premium",
            "photo_file_id": state_data["photo_file_id"], "pixelated_url": pixelated_url,
            "text": state_data["text"], "link": link,
            "price_usd": state_data["price"], "status": "active", "created_at": time.time(),
        }
        await save_prediction(redis, ns, pred)
        await clear_state(redis, ns, user_id)
        await send_message(
            chat_id,
            f"\u2705 <b>Premium Tip Posted!</b>\nID: <code>{pred_id}</code> | Price: ${pred['price_usd']:.2f}\nBroadcasting blurred preview to all users...",
            reply_markup=admin_main_keyboard(),
        )
        await send_message(chat_id, f"\U0001f4cc Mark result for tip <code>{pred_id}</code> when the match ends:", reply_markup=inline_result_buttons(pred_id))
        await broadcast_prediction(redis, ns, pred)


async def admin_broadcast_state(redis, ns, user_id: str, chat_id: int, text: str, photo: list):
    all_uids = await get_all_user_ids(redis, ns)
    sent = failed = 0
    for uid in all_uids:
        u = await get_user(redis, ns, uid)
        if not u or u.get("status") != "approved":
            continue
        try:
            if photo and text:
                await send_photo(int(uid), photo[-1]["file_id"], caption=text)
            elif photo:
                await send_photo(int(uid), photo[-1]["file_id"])
            elif text:
                await send_message(int(uid), text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as exc:
            logger.warning("Broadcast failed", uid=uid, error=str(exc))
            failed += 1
    await clear_state(redis, ns, user_id)
    await send_message(chat_id, f"\U0001f4e2 <b>Broadcast Complete!</b>\n\u2705 Sent: {sent} | \u274c Failed: {failed}", reply_markup=admin_main_keyboard())


async def admin_settings_state(redis, ns, user_id: str, chat_id: int, state: str, state_data: dict, text: str):
    settings = await get_settings(redis, ns)
    if state == "settings:price":
        try:
            price = float(text.strip().replace("$", ""))
            if price < 0.5:
                await send_message(chat_id, "\u274c Minimum $0.50.")
                return
            settings["default_premium_price_usd"] = price
            await save_settings(redis, ns, settings)
            await clear_state(redis, ns, user_id)
            await send_message(chat_id, f"\u2705 Premium price set to <b>${price:.2f}</b>", reply_markup=admin_main_keyboard())
        except ValueError:
            await send_message(chat_id, "\u274c Enter a number like 10 or 25.50")
    elif state == "settings:commission":
        try:
            pct = float(text.strip().replace("%", ""))
            if not 0 <= pct <= 100:
                await send_message(chat_id, "\u274c Must be 0-100.")
                return
            settings["commission_pct"] = pct
            await save_settings(redis, ns, settings)
            await clear_state(redis, ns, user_id)
            await send_message(chat_id, f"\u2705 Commission set to <b>{pct:.0f}%</b>", reply_markup=admin_main_keyboard())
        except ValueError:
            await send_message(chat_id, "\u274c Enter a number like 10 or 20")


# ---------------------------------------------------------------------------
# Callback query handler
# ---------------------------------------------------------------------------

async def handle_callback_query(cq: dict, base_url: str):
    cq_id = cq["id"]
    user_id = str(cq["from"]["id"])
    chat_id = int(cq["message"]["chat"]["id"])
    data = cq.get("data", "")
    logger.info("Callback", user_id=user_id, data=data)
    async with redis_client() as (redis, ns):
        if data.startswith("approve:"):
            target = data.split(":", 1)[1]
            await approve_reject_user(redis, ns, target, True)
            await answer_callback_query(cq_id, "\u2705 User approved!")
        elif data.startswith("reject:"):
            target = data.split(":", 1)[1]
            await approve_reject_user(redis, ns, target, False)
            await answer_callback_query(cq_id, "\u274c User rejected.")
        elif data.startswith("result:"):
            parts = data.split(":")
            await set_prediction_result(redis, ns, parts[1], parts[2], chat_id)
            await answer_callback_query(cq_id, f"Result: {parts[2].upper()}")
        elif data.startswith("unlock:"):
            pred_id = data.split(":", 1)[1]
            user = await get_user(redis, ns, user_id)
            if not user or user["status"] != "approved":
                await answer_callback_query(cq_id, "\u274c Access denied.")
                return
            pred = await get_prediction(redis, ns, pred_id)
            if not pred:
                await answer_callback_query(cq_id, "\u274c Tip not found.")
                return
            if await has_unlocked(redis, ns, user_id, pred_id):
                await answer_callback_query(cq_id, "\u2705 Already unlocked!")
                return
            await answer_callback_query(cq_id)
            await send_message(
                chat_id,
                f"\U0001f48e <b>Unlock Premium Tip #{pred_id}</b>\n\nPrice: <b>${pred['price_usd']:.2f}</b>\n\nSelect your preferred cryptocurrency:",
                reply_markup=inline_currency_buttons(pred_id),
            )
        elif data.startswith("pay:"):
            parts = data.split(":")
            if len(parts) >= 4 and parts[1] == "tip":
                await process_tip_payment(redis, ns, user_id, chat_id, parts[2], parts[3], base_url)
            else:
                await process_unlock_payment(redis, ns, user_id, chat_id, parts[1], parts[2], base_url)
            await answer_callback_query(cq_id)
        elif data == "cancel_payment":
            await answer_callback_query(cq_id, "Cancelled.")
            try:
                await tg_call("deleteMessage", {"chat_id": chat_id, "message_id": cq["message"]["message_id"]})
            except Exception:
                pass
        elif data.startswith("admin_set:"):
            setting = data.split(":", 1)[1]
            await answer_callback_query(cq_id)
            if setting == "price":
                await save_state(redis, ns, user_id, "settings:price", {})
                await send_message(chat_id, "\U0001f4b0 Enter new default premium price in USD:", reply_markup=cancel_keyboard())
            elif setting == "commission":
                await save_state(redis, ns, user_id, "settings:commission", {})
                await send_message(chat_id, "\U0001f465 Enter new commission % (e.g. 10 or 20):", reply_markup=cancel_keyboard())
        elif data.startswith("withdraw:"):
            target = data.split(":", 1)[1]
            if target == user_id:
                user = await get_user(redis, ns, user_id)
                balance = user.get("commission_balance", 0.0) if user else 0.0
                if balance < 1.0:
                    await answer_callback_query(cq_id, "\u274c Minimum withdrawal $1.00")
                    return
                await answer_callback_query(cq_id)
                await send_message(chat_id, f"\U0001f4b0 Withdrawal request of <b>${balance:.2f}</b> sent to admin. They'll contact you to process the payment.")
                for admin_id in ADMIN_IDS:
                    await send_message(
                        int(admin_id),
                        f"\U0001f4b0 <b>Commission Withdrawal</b>\nUser: @{user.get('username', 'N/A')} (ID: {user_id})\nAmount: ${balance:.2f}",
                    )


async def approve_reject_user(redis, ns, target_uid: str, approved: bool):
    user = await get_user(redis, ns, target_uid)
    if not user:
        return
    user["status"] = "approved" if approved else "rejected"
    await save_user(redis, ns, user)
    if approved:
        await send_message(
            int(target_uid),
            "\U0001f389 <b>Access Granted!</b>\n\nYour account is approved! You can now view free and premium predictions.",
            reply_markup=user_main_keyboard(),
        )
    else:
        await send_message(int(target_uid), "\u274c Your access request has been declined.")


async def set_prediction_result(redis, ns, pred_id: str, result: str, chat_id: int):
    pred = await get_prediction(redis, ns, pred_id)
    if not pred:
        await send_message(chat_id, f"\u274c Prediction {pred_id} not found.")
        return
    pred["status"] = result
    await save_prediction(redis, ns, pred)
    emoji = {"won": "\u2705", "lost": "\u274c", "void": "\u26aa"}.get(result, "\U0001f7e1")
    await send_message(chat_id, f"{emoji} Result <b>{result.upper()}</b> set for tip <code>{pred_id}</code>. Broadcasting...")
    await broadcast_result_update(redis, ns, pred, result)


async def broadcast_result_update(redis, ns, pred: dict, result: str):
    emoji = {"won": "\u2705", "lost": "\u274c", "void": "\u26aa"}.get(result, "\U0001f7e1")
    tier = "Free" if pred["type"] == "free" else "Premium"
    preview = pred.get("text", "")[:80]
    msg = (
        f"{emoji} <b>Tip Result!</b>\n\n"
        f"ID: <code>{pred['id']}</code> | Type: {tier}\n"
        f"Result: <b>{result.upper()}</b>\n\n"
        f"<i>{preview}{'...' if len(pred.get('text','')) > 80 else ''}</i>"
    )
    all_uids = await get_all_user_ids(redis, ns)
    sent = 0
    for uid in all_uids:
        u = await get_user(redis, ns, uid)
        if u and u.get("status") == "approved":
            try:
                await send_message(int(uid), msg)
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                pass
    logger.info("Result broadcast done", pred_id=pred["id"], result=result, sent=sent)


async def broadcast_prediction(redis, ns, pred: dict):
    all_uids = await get_all_user_ids(redis, ns)
    sent = 0
    for uid in all_uids:
        u = await get_user(redis, ns, uid)
        if not u or u.get("status") != "approved":
            continue
        try:
            if pred["type"] == "free":
                await send_photo(int(uid), pred["photo_file_id"], caption=fmt_prediction(pred, show_link=True))
            else:
                caption = (
                    "\U0001f48e <b>New Premium Tip Available!</b>\n\n"
                    "\U0001f512 Tap Unlock to see the full prediction.\n\n"
                    f"\U0001f4b0 Price: <b>${pred['price_usd']:.2f}</b>"
                )
                rm = inline_unlock_button(pred["id"], pred["price_usd"])
                if pred.get("pixelated_url"):
                    await send_photo(int(uid), pred["pixelated_url"], caption=caption, reply_markup=rm)
                else:
                    await send_message(int(uid), caption, reply_markup=rm)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as exc:
            logger.warning("Prediction broadcast failed", uid=uid, error=str(exc))
    logger.info("Prediction broadcast done", pred_id=pred["id"], sent=sent)


# ---------------------------------------------------------------------------
# Payment processing
# ---------------------------------------------------------------------------

async def process_unlock_payment(redis, ns, user_id: str, chat_id: int, pred_id: str, currency: str, base_url: str):
    pred = await get_prediction(redis, ns, pred_id)
    if not pred:
        await send_message(chat_id, "\u274c Tip not found.")
        return
    if await has_unlocked(redis, ns, user_id, pred_id):
        await send_message(chat_id, "\u2705 You've already unlocked this tip!")
        return
    payment_id = uuid.uuid4().hex
    ipn_url = f"{base_url}/nowpayments-ipn"
    try:
        res = await create_nowpayments_payment(
            amount_usd=pred["price_usd"], pay_currency=currency,
            order_id=payment_id, description=f"Unlock tip #{pred_id}",
            ipn_callback_url=ipn_url,
        )
        if res.get("payment_id") or res.get("payment_status") == "waiting":
            payment = {
                "id": payment_id, "user_id": user_id, "type": "unlock", "pred_id": pred_id,
                "amount_usd": pred["price_usd"], "status": "pending",
                "nowpayments_id": str(res.get("payment_id", "")),
                "address": res.get("pay_address", ""), "crypto": currency,
                "pay_amount": res.get("pay_amount", 0), "created_at": time.time(),
            }
            await save_payment(redis, ns, payment)
            msg = (
                f"\U0001f4b3 <b>Payment Details</b>\n\n"
                f"Tip: <code>{pred_id}</code> | Amount: <b>${pred['price_usd']:.2f}</b>\n"
                f"Pay: <b>{res.get('pay_amount', '?')} {currency.upper()}</b>\n\n"
                f"\U0001f4e4 Send to:\n<code>{res.get('pay_address', 'N/A')}</code>\n\n"
                f"\u23f0 Expires in ~60 mins. Tip unlocks automatically after confirmation."
            )
            await send_message(chat_id, msg)
        else:
            err = res.get("message", res.get("error", "Unknown"))
            await send_message(chat_id, f"\u274c Payment creation failed: <i>{err}</i>")
    except Exception as exc:
        logger.error("Unlock payment error", error=str(exc))
        await send_message(chat_id, "\u274c Payment service unavailable. Try again later.")


async def process_tip_payment(redis, ns, user_id: str, chat_id: int, payment_id: str, currency: str, base_url: str):
    payment = await get_payment(redis, ns, payment_id)
    if not payment:
        await send_message(chat_id, "\u274c Payment session expired. Please try again.")
        return
    amount = payment["amount_usd"]
    ipn_url = f"{base_url}/nowpayments-ipn"
    try:
        res = await create_nowpayments_payment(
            amount_usd=amount, pay_currency=currency,
            order_id=payment_id, description=f"Tip from {user_id}",
            ipn_callback_url=ipn_url,
        )
        if res.get("payment_id") or res.get("payment_status") == "waiting":
            payment.update({
                "nowpayments_id": str(res.get("payment_id", "")),
                "address": res.get("pay_address", ""),
                "crypto": currency, "status": "pending",
            })
            await save_payment(redis, ns, payment)
            msg = (
                f"\U0001f4a1 <b>Tip Admin</b>\n\n"
                f"Amount: <b>${amount:.2f}</b> | Pay: <b>{res.get('pay_amount', '?')} {currency.upper()}</b>\n\n"
                f"\U0001f4e4 Send to:\n<code>{res.get('pay_address', 'N/A')}</code>\n\n"
                f"\u23f0 Expires in ~60 mins. Thank you! \U0001f64f"
            )
            await send_message(chat_id, msg)
        else:
            err = res.get("message", res.get("error", "Unknown"))
            await send_message(chat_id, f"\u274c Payment failed: <i>{err}</i>")
    except Exception as exc:
        logger.error("Tip payment error", error=str(exc))
        await send_message(chat_id, "\u274c Payment service unavailable. Try again later.")


async def handle_confirmed_payment(redis, ns, payment_id: str):
    payment = await get_payment(redis, ns, payment_id)
    if not payment:
        logger.error("IPN: payment not found", payment_id=payment_id)
        return
    if payment.get("status") == "confirmed":
        logger.info("IPN: already confirmed, skip", payment_id=payment_id)
        return
    payment["status"] = "confirmed"
    await save_payment(redis, ns, payment)
    user_id = payment["user_id"]
    amount = payment["amount_usd"]
    if payment["type"] == "unlock":
        pred_id = payment["pred_id"]
        await mark_unlocked(redis, ns, user_id, pred_id, payment_id)
        user = await get_user(redis, ns, user_id)
        if user:
            user["total_spent"] = round(user.get("total_spent", 0.0) + amount, 2)
            await save_user(redis, ns, user)
            if user.get("referred_by_code"):
                settings = await get_settings(redis, ns)
                commission_pct = settings.get("commission_pct", 10.0)
                commission_amt = round(amount * commission_pct / 100, 2)
                referrer_id = await get_referral_owner(redis, ns, user["referred_by_code"])
                if referrer_id:
                    referrer = await get_user(redis, ns, referrer_id)
                    if referrer:
                        referrer["commission_balance"] = round(referrer.get("commission_balance", 0.0) + commission_amt, 2)
                        await save_user(redis, ns, referrer)
                        await send_message(
                            int(referrer_id),
                            f"\U0001f389 <b>Commission Earned!</b>\nReferral just unlocked a premium tip!\n\U0001f4b0 Earned: <b>${commission_amt:.2f}</b> | Balance: <b>${referrer['commission_balance']:.2f}</b>",
                        )
        pred = await get_prediction(redis, ns, pred_id)
        if pred:
            caption = "\u2705 <b>TIP UNLOCKED!</b>\n\n" + fmt_prediction(pred, show_link=True)
            await send_photo(int(user_id), pred["photo_file_id"], caption=caption)
        logger.info("Premium tip revealed", user_id=user_id, pred_id=pred_id)
    elif payment["type"] == "tip":
        await send_message(int(user_id), f"\u2705 <b>Tip Sent!</b>\nThank you for your ${amount:.2f} tip! \U0001f64f")
        for admin_id in ADMIN_IDS:
            await send_message(int(admin_id), f"\U0001f4a1 <b>Tip Received!</b>\nFrom: User {user_id}\nAmount: ${amount:.2f}")


async def process_ipn_background(order_id: str):
    async with redis_client() as (redis, ns):
        await handle_confirmed_payment(redis, ns, order_id)


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Sports Prediction Bot",
    description="Telegram sports prediction bot with free/premium tips, NOWPayments crypto unlocks, referral commissions, and result broadcasting.",
    version="1.0.0",
)


class HealthResponse(BaseModel):
    status: str = Field(..., description="Health status")
    message: str = Field(..., description="Status message")


@app.post("/")
async def root_health():
    """Root health/status check."""
    return {"status": "ok", "message": "Sports Prediction Bot running"}


@app.post("/webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    """Handle Telegram webhook updates."""
    try:
        base_url = str(request.base_url).rstrip("/")
        body = await request.json()
        logger.info("Telegram update", update_id=body.get("update_id"))
        background_tasks.add_task(handle_update, body, base_url)
        return Response(content="OK", status_code=200)
    except Exception as exc:
        logger.error("Webhook handler error", error=str(exc))
        return Response(content="OK", status_code=200)


@app.post("/nowpayments-ipn")
async def nowpayments_ipn(request: Request, background_tasks: BackgroundTasks):
    """Handle NOWPayments IPN payment confirmation webhook."""
    try:
        raw_body = await request.body()
        signature = request.headers.get("x-nowpayments-sig", "")
        if NOWPAYMENTS_IPN_SECRET and not verify_nowpayments_signature(raw_body, signature):
            logger.warning("Invalid IPN signature")
            return Response(content="Invalid signature", status_code=400)
        data = json.loads(raw_body)
        logger.info("NOWPayments IPN", np_id=data.get("payment_id"), status=data.get("payment_status"))
        if data.get("payment_status") in ("finished", "confirmed"):
            order_id = data.get("order_id", "")
            if order_id:
                background_tasks.add_task(process_ipn_background, order_id)
        return Response(content="OK", status_code=200)
    except Exception as exc:
        logger.error("IPN handler error", error=str(exc))
        return Response(content="Error", status_code=500)


@app.get("/set-webhook")
async def set_webhook(request: Request):
    """
    Register the Telegram webhook. Visit this URL ONCE after deployment.
    Example: https://your-service-url/set-webhook
    """
    base_url = str(request.base_url).rstrip("/")
    webhook_url = f"{base_url}/webhook"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{TG_API}/setWebhook",
            json={"url": webhook_url, "allowed_updates": ["message", "callback_query"], "drop_pending_updates": True},
        )
        result = resp.json()
    async with redis_client() as (redis, ns):
        await redis.set(f"{ns}:settings:base_url", base_url)
    logger.info("Webhook registered", webhook_url=webhook_url, ok=result.get("ok"))
    return {"status": "ok" if result.get("ok") else "error", "webhook_url": webhook_url, "telegram_response": result}


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "Sports Prediction Bot", "version": "1.0.0"}


if __name__ == "__main__":
    run_service(app)
