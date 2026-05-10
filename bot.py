#!/usr/bin/env python3
"""
Sports Prediction Bot - Standalone

Usage:
    pip install -r requirements.txt
    export TELEGRAM_BOT_TOKEN=your_token
    export NOWPAYMENTS_API_KEY=your_key
    export NOWPAYMENTS_IPN_SECRET=your_secret
    export ADMIN_CHAT_IDS=your_telegram_id
    python bot.py
"""

import asyncio
import io
import logging
import os
import random
import sqlite3
import string
import time
import uuid
from datetime import datetime
from typing import Optional

import httpx
from PIL import Image, ImageDraw
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN          = os.environ.get("TELEGRAM_BOT_TOKEN", "")
NOWPAYMENTS_KEY    = os.environ.get("NOWPAYMENTS_API_KEY", "")
ADMIN_IDS          = {x.strip() for x in os.environ.get("ADMIN_CHAT_IDS", "").split(",") if x.strip()}
SHUFFLE_URL        = os.environ.get("SHUFFLE_REFERRAL_URL", "https://shuffle.com/?r=f20anQxZ3a")
DB_PATH            = "bot_data.db"
NP_API             = "https://api.nowpayments.io/v1"

CURRENCY_LABELS = {
    "btc":       "Bitcoin (BTC)",
    "eth":       "Ethereum (ETH)",
    "usdterc20": "USDT ERC-20",
    "usdttrc20": "USDT TRC-20",
    "bnbbsc":    "BNB (BSC)",
    "sol":       "Solana (SOL)",
    "ltc":       "Litecoin (LTC)",
    "xrp":       "XRP (Ripple)",
    "doge":      "Dogecoin (DOGE)",
}

# Conversation states
(
    AWAIT_USERNAME, AWAIT_TIP_AMT,
    ADM_FREE_PHOTO, ADM_FREE_DET, ADM_FREE_LINK,
    ADM_PREM_PHOTO, ADM_PREM_DET, ADM_PREM_PRICE, ADM_PREM_LINK,
    ADM_BROADCAST,
) = range(10)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY, username TEXT DEFAULT '',
                status TEXT DEFAULT 'pending', referral_code TEXT UNIQUE,
                referred_by_code TEXT, commission_balance REAL DEFAULT 0,
                joined_at REAL, total_spent REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS predictions (
                id TEXT PRIMARY KEY, type TEXT, photo_file_id TEXT,
                pixelated_file_id TEXT, text TEXT, link TEXT,
                price_usd REAL DEFAULT 0, status TEXT DEFAULT 'active', created_at REAL
            );
            CREATE TABLE IF NOT EXISTS payments (
                id TEXT PRIMARY KEY, user_id TEXT, type TEXT, pred_id TEXT,
                amount_usd REAL, status TEXT DEFAULT 'pending',
                nowpayments_id TEXT, address TEXT, crypto TEXT,
                pay_amount REAL, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS unlocks (
                user_id TEXT, pred_id TEXT, payment_id TEXT, paid_at REAL,
                PRIMARY KEY (user_id, pred_id)
            );
            CREATE TABLE IF NOT EXISTS pred_messages (
                pred_id TEXT, user_id TEXT, message_id INTEGER,
                PRIMARY KEY (pred_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS ref_codes (
                code TEXT PRIMARY KEY, user_id TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT
            );
            INSERT OR IGNORE INTO settings VALUES ('price', '10.0');
            INSERT OR IGNORE INTO settings VALUES ('commission', '10.0');
        """)


def save_pred_msg(pred_id: str, user_id: str, message_id: int):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pred_messages VALUES(?,?,?)",
            (pred_id, str(user_id), message_id),
        )


def get_pred_msgs(pred_id: str) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT user_id, message_id FROM pred_messages WHERE pred_id=?", (pred_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_pred_msgs_db(pred_id: str):
    with get_db() as conn:
        conn.execute("DELETE FROM pred_messages WHERE pred_id=?", (pred_id,))



    with get_db() as conn:
        r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_cfg(key, val):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, str(val)))


def get_user(uid) -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute("SELECT * FROM users WHERE user_id=?", (str(uid),)).fetchone()
    return dict(r) if r else None


def save_user(u: dict):
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO users
               (user_id,username,status,referral_code,referred_by_code,
                commission_balance,joined_at,total_spent)
               VALUES(?,?,?,?,?,?,?,?)""",
            (u["user_id"], u.get("username", ""), u.get("status", "pending"),
             u.get("referral_code"), u.get("referred_by_code"),
             u.get("commission_balance", 0), u.get("joined_at", time.time()),
             u.get("total_spent", 0)),
        )


def get_users(status=None):
    with get_db() as conn:
        if status:
            rows = conn.execute("SELECT * FROM users WHERE status=?", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM users").fetchall()
    return [dict(r) for r in rows]


def get_pred(pid) -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute("SELECT * FROM predictions WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def save_pred(p: dict):
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO predictions
               (id,type,photo_file_id,pixelated_file_id,text,link,price_usd,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (p["id"], p["type"], p["photo_file_id"], p.get("pixelated_file_id"),
             p["text"], p.get("link"), p.get("price_usd", 0),
             p.get("status", "active"), p.get("created_at", time.time())),
        )


def recent_preds(ptype=None, limit=5):
    with get_db() as conn:
        if ptype:
            rows = conn.execute(
                "SELECT * FROM predictions WHERE type=? ORDER BY created_at DESC LIMIT ?",
                (ptype, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM predictions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def save_payment(p: dict):
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO payments
               (id,user_id,type,pred_id,amount_usd,status,
                nowpayments_id,address,crypto,pay_amount,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (p["id"], p["user_id"], p["type"], p.get("pred_id"),
             p["amount_usd"], p.get("status", "pending"),
             p.get("nowpayments_id"), p.get("address"),
             p.get("crypto"), p.get("pay_amount", 0), p.get("created_at", time.time())),
        )


def get_payment(pid) -> Optional[dict]:
    with get_db() as conn:
        r = conn.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def pending_payments():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM payments WHERE status='pending' AND nowpayments_id IS NOT NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def confirm_payment(pid):
    with get_db() as conn:
        conn.execute("UPDATE payments SET status='confirmed' WHERE id=?", (pid,))


def unlocked(uid, pid) -> bool:
    with get_db() as conn:
        r = conn.execute(
            "SELECT 1 FROM unlocks WHERE user_id=? AND pred_id=?", (uid, pid)
        ).fetchone()
    return r is not None


def mark_unlock(uid, pid, pay_id):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO unlocks VALUES(?,?,?,?)", (uid, pid, pay_id, time.time())
        )


def get_ref_owner(code) -> Optional[str]:
    with get_db() as conn:
        r = conn.execute("SELECT user_id FROM ref_codes WHERE code=?", (code,)).fetchone()
    return r["user_id"] if r else None


def save_ref(code, uid):
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO ref_codes VALUES(?,?)", (code, uid))


def ref_count(code) -> int:
    with get_db() as conn:
        r = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE referred_by_code=?", (code,)
        ).fetchone()
    return r["c"] if r else 0


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def rand_code(n=8) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))


def is_admin(uid) -> bool:
    return str(uid) in ADMIN_IDS


def fmt_pred(p: dict, show_link: bool = True) -> str:
    em = {"active": "\U0001f7e1", "won": "\u2705", "lost": "\u274c", "void": "\u26aa"}.get(
        p["status"], "\U0001f7e1"
    )
    tier = "\U0001f48e" if p["type"] == "premium" else "\U0001f193"
    lines = [f"{tier} <b>Prediction</b> {em}", "", p["text"]]
    if p.get("status") != "active":
        lines.append(f"\n<b>Result: {p['status'].upper()}</b>")
    if show_link and p.get("link"):
        lines.append(f'\n\U0001f517 <a href="{p["link"]}">View More</a>')
    return "\n".join(lines)


def pixelate(img_bytes: bytes, px: int = 22) -> bytes:
    img = Image.open(io.BytesIO(img_bytes))
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    s = img.resize((max(1, w // px), max(1, h // px)), Image.NEAREST)
    pix = s.resize((w, h), Image.NEAREST)
    draw = ImageDraw.Draw(pix)
    bh = max(70, h // 5)
    y0 = (h - bh) // 2
    draw.rectangle([(0, y0), (w, y0 + bh)], fill=(10, 10, 10))
    for i, line in enumerate(["PREMIUM CONTENT", "Tap Unlock to reveal"]):
        y = y0 + bh // 3 * (i + 1)
        try:
            bb = draw.textbbox((0, 0), line)
            draw.text(
                (max(4, (w - (bb[2] - bb[0])) // 2), y - (bb[3] - bb[1]) // 2),
                line, fill=(255, 255, 255),
            )
        except Exception:
            draw.text((10, y), line, fill=(255, 255, 255))
    out = io.BytesIO()
    pix.save(out, format="JPEG", quality=80)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def kb_user():
    return ReplyKeyboardMarkup(
        [["\U0001f4ca Free Tips", "\U0001f48e Premium Tips"],
         ["\U0001f465 My Referral", "\U0001f4a1 Tip Admin"],
         ["\U0001f464 My Profile"]],
        resize_keyboard=True,
    )


def kb_admin():
    return ReplyKeyboardMarkup(
        [["\u2795 New Free Tip", "\U0001f48e New Premium Tip"],
         ["\u2705 Approve Users", "\U0001f4e2 Broadcast"],
         ["\U0001f4ca Stats", "\u2699\ufe0f Settings"]],
        resize_keyboard=True,
    )


def kb_cancel():
    return ReplyKeyboardMarkup([["\u274c Cancel"]], resize_keyboard=True)


def kb_submit():
    return ReplyKeyboardMarkup([["\U0001f4dd Submit Username for Approval"]], resize_keyboard=True)


def ik_unlock(pid: str, price: float):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"\U0001f513 Unlock  ${price:.2f}", callback_data=f"unlock:{pid}")]]
    )


def ik_currencies(ref: str):
    buttons: list = []
    row: list = []
    for code, label in CURRENCY_LABELS.items():
        row.append(InlineKeyboardButton(label, callback_data=f"pay:{ref}:{code}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("\u274c Cancel", callback_data="cancel_pay")])
    return InlineKeyboardMarkup(buttons)


def ik_result(pid: str):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("\u2705 WON",  callback_data=f"result:{pid}:won"),
            InlineKeyboardButton("\u274c LOST", callback_data=f"result:{pid}:lost"),
            InlineKeyboardButton("\u26aa VOID", callback_data=f"result:{pid}:void"),
        ]]
    )


def ik_approve(uid: str):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("\u2705 Approve", callback_data=f"approve:{uid}"),
            InlineKeyboardButton("\u274c Reject",  callback_data=f"reject:{uid}"),
        ]]
    )


# ---------------------------------------------------------------------------
# NOWPayments
# ---------------------------------------------------------------------------

async def np_create(amount: float, currency: str, order_id: str, desc: str) -> dict:
    headers = {"x-api-key": NOWPAYMENTS_KEY}
    data = {
        "price_amount": amount, "price_currency": "usd",
        "pay_currency": currency, "order_id": order_id, "order_description": desc,
    }
    async with httpx.AsyncClient(timeout=30) as c:
        return (await c.post(f"{NP_API}/payment", json=data, headers=headers)).json()


async def np_status(np_id: str) -> Optional[str]:
    headers = {"x-api-key": NOWPAYMENTS_KEY}
    async with httpx.AsyncClient(timeout=30) as c:
        r = (await c.get(f"{NP_API}/payment/{np_id}", headers=headers)).json()
    return r.get("payment_status")


# ---------------------------------------------------------------------------
# Broadcast helpers
# ---------------------------------------------------------------------------

async def bcast_pred(bot, pred: dict):
    for u in get_users("approved"):
        try:
            uid = int(u["user_id"])
            msg = None
            if pred["type"] == "free":
                msg = await bot.send_photo(
                    uid, pred["photo_file_id"],
                    caption=fmt_pred(pred, True), parse_mode="HTML",
                )
            else:
                cap = (
                    "\U0001f48e <b>New Premium Tip!</b>\n\U0001f512 Locked.\n\n"
                    f"\U0001f4b0 Price: <b>${pred['price_usd']:.2f}</b>"
                )
                rm = ik_unlock(pred["id"], pred["price_usd"])
                fid = pred.get("pixelated_file_id")
                if fid:
                    msg = await bot.send_photo(uid, fid, caption=cap, reply_markup=rm, parse_mode="HTML")
                else:
                    msg = await bot.send_message(uid, cap, reply_markup=rm, parse_mode="HTML")
            if msg:
                save_pred_msg(pred["id"], str(uid), msg.message_id)
            await asyncio.sleep(0.05)
        except Exception as exc:
            logger.warning(f"bcast_pred {u['user_id']}: {exc}")


async def bcast_result(bot, pred: dict, result: str):
    em = {"won": "\u2705", "lost": "\u274c", "void": "\u26aa"}.get(result, "\U0001f7e1")
    msg = (
        f"{em} <b>Tip Result!</b>\n"
        f"ID: <code>{pred['id']}</code> | {'Free' if pred['type']=='free' else 'Premium'}\n"
        f"Result: <b>{result.upper()}</b>\n\n"
        f"<i>{pred.get('text', '')[:80]}</i>"
    )
    # First delete the original prediction messages from all users
    stored_msgs = get_pred_msgs(pred["id"])
    for entry in stored_msgs:
        try:
            await bot.delete_message(
                chat_id=int(entry["user_id"]),
                message_id=entry["message_id"]
            )
        except Exception:
            pass  # Message may already be deleted or too old
        await asyncio.sleep(0.03)
    delete_pred_msgs_db(pred["id"])
    # Then send the result notification to all approved users
    for u in get_users("approved"):
        try:
            await bot.send_message(int(u["user_id"]), msg, parse_mode="HTML")
            await asyncio.sleep(0.05)
        except Exception as exc:
            logger.warning(f"bcast_result {u['user_id']}: {exc}")


# ---------------------------------------------------------------------------
# Payment confirmed handler
# ---------------------------------------------------------------------------

async def on_payment_confirmed(bot, pay_id: str):
    p = get_payment(pay_id)
    if not p or p["status"] == "confirmed":
        return
    confirm_payment(pay_id)
    uid = p["user_id"]
    amount = p["amount_usd"]

    if p["type"] == "unlock":
        pid = p["pred_id"]
        mark_unlock(uid, pid, pay_id)
        user = get_user(uid)
        if user:
            user["total_spent"] = round(user.get("total_spent", 0) + amount, 2)
            referred_code = user.get("referred_by_code")
            if referred_code:
                pct = float(cfg("commission", "10.0"))
                comm = round(amount * pct / 100, 2)
                rid = get_ref_owner(referred_code)
                if rid:
                    referrer = get_user(rid)
                    if referrer:
                        referrer["commission_balance"] = round(
                            referrer.get("commission_balance", 0) + comm, 2
                        )
                        save_user(referrer)
                        await bot.send_message(
                            int(rid),
                            f"\U0001f389 <b>Commission!</b> Referral unlocked a tip!\n"
                            f"\U0001f4b0 +${comm:.2f} | Balance: ${referrer['commission_balance']:.2f}",
                            parse_mode="HTML",
                        )
            save_user(user)
        pred = get_pred(pid)
        if pred:
            cap = "\u2705 <b>TIP UNLOCKED!</b>\n\n" + fmt_pred(pred, True)
            await bot.send_photo(int(uid), pred["photo_file_id"], caption=cap, parse_mode="HTML")

    elif p["type"] == "tip":
        await bot.send_message(
            int(uid),
            f"\u2705 <b>Tip sent!</b> Thank you for your ${amount:.2f}! \U0001f64f",
            parse_mode="HTML",
        )
        for aid in ADMIN_IDS:
            await bot.send_message(
                int(aid),
                f"\U0001f4a1 <b>Tip received!</b>\nFrom: {uid}\nAmount: ${amount:.2f}",
                parse_mode="HTML",
            )


# ---------------------------------------------------------------------------
# Payment poller (background task)
# ---------------------------------------------------------------------------

async def payment_poller(bot):
    """Check pending NOWPayments every 30 seconds and reveal content on confirmation."""
    while True:
        try:
            for p in pending_payments():
                status = await np_status(p["nowpayments_id"])
                if status in ("finished", "confirmed"):
                    logger.info(f"Payment confirmed: {p['id']}")
                    await on_payment_confirmed(bot, p["id"])
        except Exception as exc:
            logger.error(f"Poller error: {exc}")
        await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# /start command
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if is_admin(uid):
        await update.message.reply_text(
            "\U0001f451 <b>Admin Panel</b>\nWelcome back!",
            reply_markup=kb_admin(), parse_mode="HTML",
        )
        return ConversationHandler.END

    user = get_user(uid)
    if not user:
        code = rand_code()
        while get_ref_owner(code):
            code = rand_code()
        user = {
            "user_id": uid, "username": "", "status": "pending",
            "referral_code": code, "referred_by_code": None,
            "commission_balance": 0.0, "joined_at": time.time(), "total_spent": 0.0,
        }
        save_ref(code, uid)
        save_user(user)

    if user["status"] == "approved":
        await update.message.reply_text("\U0001f44b Welcome back!", reply_markup=kb_user())
        return ConversationHandler.END
    if user["status"] == "rejected":
        await update.message.reply_text("\u274c Access was declined. Contact support.")
        return ConversationHandler.END
    if user["status"] == "banned":
        await update.message.reply_text("\U0001f6ab Your account is banned.")
        return ConversationHandler.END

    await update.message.reply_text(
        "\U0001f44b <b>Welcome to the Sports Prediction Bot!</b>\n\n"
        "Follow 2 steps to get access:\n\n"
        f"1\ufe0f\u20e3 <b>Join Shuffle Casino</b>:\n"
        f'<a href="{SHUFFLE_URL}">\U0001f449 Join here</a>\n\n'
        "2\ufe0f\u20e3 Tap below to submit your username for admin approval.",
        reply_markup=kb_submit(), parse_mode="HTML",
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Main menu text handler
# ---------------------------------------------------------------------------

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text or ""

    # --- Admin path ---
    if is_admin(uid):
        # Check if admin was prompted for a settings value
        mode = ctx.user_data.pop("setting_mode", None)
        if mode == "price":
            try:
                p = float(text.replace("$", ""))
                set_cfg("price", p)
                await update.message.reply_text(f"\u2705 Price set to ${p:.2f}", reply_markup=kb_admin())
            except ValueError:
                await update.message.reply_text("\u274c Enter a number (e.g. 15).", reply_markup=kb_admin())
            return ConversationHandler.END
        if mode == "commission":
            try:
                p = float(text.replace("%", ""))
                set_cfg("commission", p)
                await update.message.reply_text(f"\u2705 Commission set to {p:.0f}%", reply_markup=kb_admin())
            except ValueError:
                await update.message.reply_text("\u274c Enter a number (e.g. 10).", reply_markup=kb_admin())
            return ConversationHandler.END

        if text == "\u2795 New Free Tip":
            await update.message.reply_text("\U0001f4f8 Send prediction screenshot:", reply_markup=kb_cancel())
            return ADM_FREE_PHOTO
        if text == "\U0001f48e New Premium Tip":
            await update.message.reply_text("\U0001f4f8 Send prediction screenshot:", reply_markup=kb_cancel())
            return ADM_PREM_PHOTO
        if text == "\u2705 Approve Users":
            pending = [u for u in get_users("pending") if u.get("username")]
            if not pending:
                await update.message.reply_text("\u2705 No pending users.", reply_markup=kb_admin())
            else:
                for pu in pending[:10]:
                    j = datetime.fromtimestamp(pu.get("joined_at", time.time())).strftime("%Y-%m-%d")
                    await update.message.reply_text(
                        f"\U0001f464 ID: <code>{pu['user_id']}</code>\n"
                        f"@{pu.get('username', 'N/A')} — {j}",
                        reply_markup=ik_approve(pu["user_id"]), parse_mode="HTML",
                    )
            return ConversationHandler.END
        if text == "\U0001f4e2 Broadcast":
            await update.message.reply_text("\U0001f4e2 Send message (text or photo):", reply_markup=kb_cancel())
            return ADM_BROADCAST
        if text == "\U0001f4ca Stats":
            all_u = get_users()
            appr = sum(1 for x in all_u if x["status"] == "approved")
            rev = sum(x.get("total_spent", 0) for x in all_u)
            fc = len(recent_preds("free", 1000))
            pc = len(recent_preds("premium", 1000))
            await update.message.reply_text(
                f"\U0001f4ca <b>Stats</b>\n\n"
                f"\U0001f465 {len(all_u)} users | \u2705 {appr} approved\n"
                f"\U0001f4cb {fc} free | {pc} premium tips\n"
                f"\U0001f4b0 Revenue: ${rev:.2f}",
                reply_markup=kb_admin(), parse_mode="HTML",
            )
            return ConversationHandler.END
        if text == "\u2699\ufe0f Settings":
            pr = cfg("price", "10.0")
            co = cfg("commission", "10.0")
            rm = InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001f4b0 Set Premium Price", callback_data="aset:price")],
                [InlineKeyboardButton("\U0001f465 Set Commission %",  callback_data="aset:commission")],
            ])
            await update.message.reply_text(
                f"\u2699\ufe0f <b>Settings</b>\n\n"
                f"\U0001f4b0 Price: ${float(pr):.2f}\n"
                f"\U0001f465 Commission: {float(co):.0f}%",
                reply_markup=rm, parse_mode="HTML",
            )
            return ConversationHandler.END
        await update.message.reply_text("Use the admin menu.", reply_markup=kb_admin())
        return ConversationHandler.END

    # --- User path ---
    user = get_user(uid)
    if text == "\U0001f4dd Submit Username for Approval":
        if not user or user["status"] == "pending":
            await update.message.reply_text("Enter your Telegram username (no @):", reply_markup=kb_cancel())
            return AWAIT_USERNAME
        await update.message.reply_text("Your application has already been processed.")
        return ConversationHandler.END

    if not user or user["status"] != "approved":
        await update.message.reply_text(
            "\u23f3 Pending approval. You'll be notified when approved.",
            reply_markup=kb_submit(),
        )
        return ConversationHandler.END

    if text == "\U0001f4ca Free Tips":
        preds = recent_preds("free")
        if not preds:
            await update.message.reply_text("No free tips yet. Check back soon!")
        else:
            for p in preds:
                await update.message.reply_photo(
                    p["photo_file_id"], caption=fmt_pred(p, True), parse_mode="HTML"
                )
                await asyncio.sleep(0.2)

    elif text == "\U0001f48e Premium Tips":
        preds = recent_preds("premium")
        if not preds:
            await update.message.reply_text("No premium tips yet. Check back soon!")
        else:
            for p in preds:
                if unlocked(uid, p["id"]):
                    await update.message.reply_photo(
                        p["photo_file_id"],
                        caption=fmt_pred(p, True) + "\n\n\u2705 <i>Unlocked</i>",
                        parse_mode="HTML",
                    )
                else:
                    cap = (
                        "\U0001f48e <b>Premium Tip</b>\n"
                        "\U0001f512 Locked! Tap Unlock to reveal.\n\n"
                        f"\U0001f4b0 Price: <b>${p['price_usd']:.2f}</b>"
                    )
                    rm = ik_unlock(p["id"], p["price_usd"])
                    fid = p.get("pixelated_file_id")
                    if fid:
                        await update.message.reply_photo(fid, caption=cap, reply_markup=rm, parse_mode="HTML")
                    else:
                        await update.message.reply_text(cap, reply_markup=rm, parse_mode="HTML")
                await asyncio.sleep(0.2)

    elif text == "\U0001f465 My Referral":
        code = user.get("referral_code", "")
        pct = float(cfg("commission", "10.0"))
        bal = user.get("commission_balance", 0.0)
        rm = None
        if bal >= 1.0:
            rm = InlineKeyboardMarkup([[InlineKeyboardButton(
                f"\U0001f4b0 Withdraw ${bal:.2f}", callback_data=f"withdraw:{uid}"
            )]])
        await update.message.reply_text(
            f"\U0001f465 <b>Referral</b>\n\n"
            f"\U0001f517 Code: <code>{code}</code>\n"
            f"Referrals: <b>{ref_count(code)}</b> | Commission: <b>{pct:.0f}%</b>\n"
            f"Balance: <b>${bal:.2f}</b>",
            reply_markup=rm, parse_mode="HTML",
        )

    elif text == "\U0001f4a1 Tip Admin":
        await update.message.reply_text(
            "\U0001f4a1 Enter tip amount in USD (e.g. <code>5</code>):",
            reply_markup=kb_cancel(), parse_mode="HTML",
        )
        return AWAIT_TIP_AMT

    elif text == "\U0001f464 My Profile":
        st = {"pending": "\u23f3", "approved": "\u2705", "rejected": "\u274c", "banned": "\U0001f6ab"}.get(
            user["status"], "?"
        )
        j = datetime.fromtimestamp(user.get("joined_at", time.time())).strftime("%Y-%m-%d")
        await update.message.reply_text(
            f"\U0001f464 <b>Profile</b>\n\n"
            f"ID: <code>{uid}</code>\n"
            f"@{user.get('username', 'N/A')}\n"
            f"Status: {st} | Joined: {j}\n"
            f"Spent: ${user.get('total_spent', 0):.2f} | Code: <code>{user.get('referral_code', '')}</code>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("Use the menu buttons.", reply_markup=kb_user())

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# User conversation states
# ---------------------------------------------------------------------------

async def await_username(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text or ""
    if text == "\u274c Cancel":
        await update.message.reply_text("Cancelled.", reply_markup=kb_submit())
        return ConversationHandler.END
    uname = text.strip().lstrip("@")
    if not uname:
        await update.message.reply_text("Send a valid username.")
        return AWAIT_USERNAME
    user = get_user(uid)
    if not user:
        await cmd_start(update, ctx)
        return ConversationHandler.END
    user["username"] = uname
    save_user(user)
    await update.message.reply_text(
        f"\u2705 @{uname} submitted! Waiting for admin approval.", reply_markup=kb_submit()
    )
    for aid in ADMIN_IDS:
        try:
            await ctx.bot.send_message(
                int(aid),
                f"\U0001f514 <b>New Request</b>\nID: <code>{uid}</code>\nUsername: @{uname}",
                reply_markup=ik_approve(uid), parse_mode="HTML",
            )
        except Exception as exc:
            logger.error(f"Notify admin failed: {exc}")
    return ConversationHandler.END


async def await_tip_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    text = update.message.text or ""
    if text == "\u274c Cancel":
        await update.message.reply_text("Cancelled.", reply_markup=kb_user())
        return ConversationHandler.END
    try:
        amt = float(text.replace("$", "").strip())
        if amt <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("\u274c Please enter a valid amount (e.g. <code>5</code> or <code>0.50</code>).", parse_mode="HTML")
        return AWAIT_TIP_AMT
    pid = uuid.uuid4().hex
    save_payment({
        "id": pid, "user_id": uid, "type": "tip", "pred_id": None,
        "amount_usd": amt, "status": "awaiting_currency",
    })
    await update.message.reply_text(
        f"\U0001f4a1 Tip ${amt:.2f} — choose crypto:",
        reply_markup=ik_currencies(f"tip:{pid}"), parse_mode="HTML",
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Admin conversation states: Free Tip
# ---------------------------------------------------------------------------

async def adm_free_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if (update.message.text or "") == "\u274c Cancel":
        await update.message.reply_text("Cancelled.", reply_markup=kb_admin())
        return ConversationHandler.END
    if not update.message.photo:
        await update.message.reply_text("\U0001f4f8 Send a photo.")
        return ADM_FREE_PHOTO
    ctx.user_data["fp"] = update.message.photo[-1].file_id
    await update.message.reply_text(
        "\u2705 Photo received!\n\n\U0001f4dd Send match details + tip text:",
        reply_markup=kb_cancel(),
    )
    return ADM_FREE_DET


async def adm_free_det(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if text == "\u274c Cancel":
        await update.message.reply_text("Cancelled.", reply_markup=kb_admin())
        return ConversationHandler.END
    ctx.user_data["ft"] = text
    await update.message.reply_text(
        "\U0001f517 Send an optional link, or skip:",
        reply_markup=ReplyKeyboardMarkup([["\u23ed Skip", "\u274c Cancel"]], resize_keyboard=True),
    )
    return ADM_FREE_LINK


async def adm_free_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if text == "\u274c Cancel":
        await update.message.reply_text("Cancelled.", reply_markup=kb_admin())
        return ConversationHandler.END
    link = None if text == "\u23ed Skip" else (text if text.startswith("http") else None)
    pid = uuid.uuid4().hex[:8].upper()
    pred = {
        "id": pid, "type": "free", "photo_file_id": ctx.user_data["fp"],
        "pixelated_file_id": None, "text": ctx.user_data["ft"], "link": link,
        "price_usd": 0, "status": "active", "created_at": time.time(),
    }
    save_pred(pred)
    await update.message.reply_text(
        f"\u2705 Free tip <code>{pid}</code> posted! Broadcasting...",
        reply_markup=kb_admin(), parse_mode="HTML",
    )
    await update.message.reply_text(
        f"\U0001f4cc Mark result for <code>{pid}</code> when game ends:",
        reply_markup=ik_result(pid), parse_mode="HTML",
    )
    await bcast_pred(ctx.bot, pred)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Admin conversation states: Premium Tip
# ---------------------------------------------------------------------------

async def adm_prem_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if (update.message.text or "") == "\u274c Cancel":
        await update.message.reply_text("Cancelled.", reply_markup=kb_admin())
        return ConversationHandler.END
    if not update.message.photo:
        await update.message.reply_text("\U0001f4f8 Send a photo.")
        return ADM_PREM_PHOTO
    ctx.user_data["pp"] = update.message.photo[-1].file_id
    await update.message.reply_text(
        "\u2705 Photo received!\n\n\U0001f4dd Send match details + tip text:",
        reply_markup=kb_cancel(),
    )
    return ADM_PREM_DET


async def adm_prem_det(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if text == "\u274c Cancel":
        await update.message.reply_text("Cancelled.", reply_markup=kb_admin())
        return ConversationHandler.END
    ctx.user_data["pt"] = text
    dp = float(cfg("price", "10.0"))
    await update.message.reply_text(
        f"\U0001f4b0 Set unlock price in USD (default: ${dp:.2f}):",
        reply_markup=ReplyKeyboardMarkup(
            [[f"\u2705 Use Default (${dp:.2f})", "\u274c Cancel"]], resize_keyboard=True
        ),
    )
    return ADM_PREM_PRICE


async def adm_prem_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    dp = float(cfg("price", "10.0"))
    if text == "\u274c Cancel":
        await update.message.reply_text("Cancelled.", reply_markup=kb_admin())
        return ConversationHandler.END
    if text.startswith("\u2705 Use Default"):
        price = dp
    else:
        try:
            price = float(text.replace("$", ""))
        except ValueError:
            await update.message.reply_text("\u274c Enter a valid number.")
            return ADM_PREM_PRICE
    ctx.user_data["ppr"] = price
    await update.message.reply_text(
        "\U0001f517 Send an optional link, or skip:",
        reply_markup=ReplyKeyboardMarkup([["\u23ed Skip", "\u274c Cancel"]], resize_keyboard=True),
    )
    return ADM_PREM_LINK


async def adm_prem_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if text == "\u274c Cancel":
        await update.message.reply_text("Cancelled.", reply_markup=kb_admin())
        return ConversationHandler.END
    link = None if text == "\u23ed Skip" else (text if text.startswith("http") else None)
    await update.message.reply_text("\u23f3 Processing image...")

    pix_fid = None
    try:
        photo_file = await ctx.bot.get_file(ctx.user_data["pp"])
        photo_bytes = bytes(await photo_file.download_as_bytearray())
        pix_bytes = pixelate(photo_bytes)
        # Send pixelated to first admin chat to get a Telegram file_id
        admin_id = int(list(ADMIN_IDS)[0])
        pix_msg = await ctx.bot.send_photo(
            admin_id, io.BytesIO(pix_bytes), caption="[Auto: pixelated preview]"
        )
        pix_fid = pix_msg.photo[-1].file_id
    except Exception as exc:
        logger.error(f"Pixelate error: {exc}")
        await update.message.reply_text("\u26a0\ufe0f Image processing failed. Tip created without preview.")

    pid = uuid.uuid4().hex[:8].upper()
    pred = {
        "id": pid, "type": "premium", "photo_file_id": ctx.user_data["pp"],
        "pixelated_file_id": pix_fid, "text": ctx.user_data["pt"], "link": link,
        "price_usd": ctx.user_data["ppr"], "status": "active", "created_at": time.time(),
    }
    save_pred(pred)
    await update.message.reply_text(
        f"\u2705 Premium tip <code>{pid}</code> | ${pred['price_usd']:.2f} posted!",
        reply_markup=kb_admin(), parse_mode="HTML",
    )
    await update.message.reply_text(
        f"\U0001f4cc Mark result for <code>{pid}</code> when game ends:",
        reply_markup=ik_result(pid), parse_mode="HTML",
    )
    await bcast_pred(ctx.bot, pred)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Admin: Broadcast
# ---------------------------------------------------------------------------

async def adm_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    photo = update.message.photo
    if text == "\u274c Cancel":
        await update.message.reply_text("Cancelled.", reply_markup=kb_admin())
        return ConversationHandler.END
    sent = failed = 0
    for u in get_users("approved"):
        try:
            uid_int = int(u["user_id"])
            if photo and text:
                await ctx.bot.send_photo(uid_int, photo[-1].file_id, caption=text, parse_mode="HTML")
            elif photo:
                await ctx.bot.send_photo(uid_int, photo[-1].file_id)
            elif text:
                await ctx.bot.send_message(uid_int, text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await update.message.reply_text(
        f"\U0001f4e2 Broadcast done! \u2705 {sent} | \u274c {failed}",
        reply_markup=kb_admin(),
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Callback query handler
# ---------------------------------------------------------------------------

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = str(q.from_user.id)
    data = q.data
    chat_id = q.message.chat_id

    if data.startswith("approve:"):
        tgt = data.split(":", 1)[1]
        user = get_user(tgt)
        if user:
            user["status"] = "approved"
            save_user(user)
            await ctx.bot.send_message(
                int(tgt),
                "\U0001f389 <b>Access Granted!</b> You can now use the bot!",
                reply_markup=kb_user(), parse_mode="HTML",
            )
        await q.edit_message_text(q.message.text + "\n\n\u2705 Approved", parse_mode="HTML")

    elif data.startswith("reject:"):
        tgt = data.split(":", 1)[1]
        user = get_user(tgt)
        if user:
            user["status"] = "rejected"
            save_user(user)
            await ctx.bot.send_message(int(tgt), "\u274c Your access was declined.")
        await q.edit_message_text(q.message.text + "\n\n\u274c Rejected", parse_mode="HTML")

    elif data.startswith("result:"):
        parts = data.split(":")
        pred = get_pred(parts[1])
        result = parts[2]
        if pred:
            pred["status"] = result
            save_pred(pred)
            em = {"won": "\u2705", "lost": "\u274c", "void": "\u26aa"}.get(result, "")
            await q.edit_message_text(
                f"{em} Result <b>{result.upper()}</b> set for <code>{parts[1]}</code>",
                parse_mode="HTML",
            )
            await bcast_result(ctx.bot, pred, result)

    elif data.startswith("unlock:"):
        pid = data.split(":", 1)[1]
        pred = get_pred(pid)
        user = get_user(uid)
        if not user or user["status"] != "approved":
            await q.answer("\u274c Access denied.", show_alert=True)
            return
        if not pred:
            await q.answer("\u274c Tip not found.", show_alert=True)
            return
        if unlocked(uid, pid):
            await q.answer("\u2705 Already unlocked!", show_alert=True)
            return
        await ctx.bot.send_message(
            chat_id,
            f"\U0001f48e <b>Unlock Tip #{pid}</b>\nPrice: <b>${pred['price_usd']:.2f}</b>\n\nChoose crypto:",
            reply_markup=ik_currencies(pid), parse_mode="HTML",
        )

    elif data.startswith("pay:"):
        parts = data.split(":")
        if len(parts) >= 4 and parts[1] == "tip":
            pay_id = parts[2]
            cur = parts[3]
            p = get_payment(pay_id)
            if not p:
                await ctx.bot.send_message(chat_id, "\u274c Payment expired. Try again.")
                return
            result = await np_create(p["amount_usd"], cur, pay_id, f"Tip from {uid}")
            if result.get("payment_id"):
                p.update({
                    "nowpayments_id": str(result["payment_id"]),
                    "address": result.get("pay_address", ""),
                    "crypto": cur,
                    "pay_amount": result.get("pay_amount", 0),
                    "status": "pending",
                })
                save_payment(p)
                await ctx.bot.send_message(
                    chat_id,
                    f"\U0001f4a1 <b>Tip Admin</b>\n\n"
                    f"\U0001f4b5 Amount: <b>${p['amount_usd']:.2f} USD</b>\n"
                    f"\U0001f4b3 Pay with: <b>{cur.upper()}</b>\n\n"
                    f"\U0001f4e4 Send exactly <b>{result.get('pay_amount', '?')} {cur.upper()}</b> to:\n"
                    f"<code>{result.get('pay_address', 'N/A')}</code>\n\n"
                    f"\u23f0 Expires ~60 mins. Thank you! \U0001f64f",
                    parse_mode="HTML",
                )
            else:
                await ctx.bot.send_message(
                    chat_id, f"\u274c Payment failed: {result.get('message', 'Unknown')}"
                )
        else:
            pid = parts[1]
            cur = parts[2]
            pred = get_pred(pid)
            if not pred or unlocked(uid, pid):
                return
            pay_id = uuid.uuid4().hex
            result = await np_create(pred["price_usd"], cur, pay_id, f"Unlock #{pid}")
            if result.get("payment_id"):
                save_payment({
                    "id": pay_id, "user_id": uid, "type": "unlock", "pred_id": pid,
                    "amount_usd": pred["price_usd"], "status": "pending",
                    "nowpayments_id": str(result["payment_id"]),
                    "address": result.get("pay_address", ""),
                    "crypto": cur, "pay_amount": result.get("pay_amount", 0),
                    "created_at": time.time(),
                })
                await ctx.bot.send_message(
                    chat_id,
                    f"\U0001f4b3 <b>Payment Details</b>\n\n"
                    f"\U0001f4b5 Tip Price: <b>${pred['price_usd']:.2f} USD</b>\n"
                    f"\U0001f4b3 Pay with: <b>{cur.upper()}</b>\n\n"
                    f"\U0001f4e4 Send exactly <b>{result.get('pay_amount', '?')} {cur.upper()}</b> to:\n"
                    f"<code>{result.get('pay_address', 'N/A')}</code>\n\n"
                    f"\u23f0 Expires ~60 mins. Tip auto-unlocks after confirmation.",
                    parse_mode="HTML",
                )
            else:
                await ctx.bot.send_message(
                    chat_id, f"\u274c Payment failed: {result.get('message', 'Unknown')}"
                )

    elif data == "cancel_pay":
        try:
            await q.delete_message()
        except Exception:
            pass

    elif data.startswith("aset:"):
        setting = data.split(":", 1)[1]
        ctx.user_data["setting_mode"] = setting
        prompt = {
            "price": "\U0001f4b0 Enter new premium price in USD:",
            "commission": "\U0001f465 Enter commission % (e.g. 10):",
        }.get(setting, "Enter value:")
        await ctx.bot.send_message(chat_id, prompt, reply_markup=kb_cancel())

    elif data.startswith("withdraw:"):
        tgt = data.split(":", 1)[1]
        if tgt == uid:
            user = get_user(uid)
            bal = user.get("commission_balance", 0) if user else 0
            if bal < 1.0:
                await q.answer("\u274c Minimum $1.00", show_alert=True)
                return
            await ctx.bot.send_message(
                chat_id,
                f"\U0001f4b0 Withdrawal request of ${bal:.2f} sent to admin.",
            )
            for aid in ADMIN_IDS:
                await ctx.bot.send_message(
                    int(aid),
                    f"\U0001f4b0 <b>Withdrawal Request</b>\n"
                    f"User: @{user.get('username', '?')} (ID: {uid})\n"
                    f"Amount: ${bal:.2f}",
                    parse_mode="HTML",
                )


# ---------------------------------------------------------------------------
# /cancel command
# ---------------------------------------------------------------------------

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    kb = kb_admin() if is_admin(uid) else kb_user()
    await update.message.reply_text("Cancelled.", reply_markup=kb)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        print("ERROR: Set the TELEGRAM_BOT_TOKEN environment variable")
        return

    init_db()
    logger.info("Database initialised")

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            MessageHandler(filters.TEXT & ~filters.COMMAND, on_text),
            MessageHandler(filters.PHOTO, on_text),
        ],
        states={
            AWAIT_USERNAME: [MessageHandler(filters.TEXT, await_username)],
            AWAIT_TIP_AMT:  [MessageHandler(filters.TEXT, await_tip_amt)],
            ADM_FREE_PHOTO: [MessageHandler(filters.PHOTO | filters.TEXT, adm_free_photo)],
            ADM_FREE_DET:   [MessageHandler(filters.TEXT, adm_free_det)],
            ADM_FREE_LINK:  [MessageHandler(filters.TEXT, adm_free_link)],
            ADM_PREM_PHOTO: [MessageHandler(filters.PHOTO | filters.TEXT, adm_prem_photo)],
            ADM_PREM_DET:   [MessageHandler(filters.TEXT, adm_prem_det)],
            ADM_PREM_PRICE: [MessageHandler(filters.TEXT, adm_prem_price)],
            ADM_PREM_LINK:  [MessageHandler(filters.TEXT, adm_prem_link)],
            ADM_BROADCAST:  [MessageHandler(filters.TEXT | filters.PHOTO, adm_broadcast)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_user=True,
        per_chat=True,
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(on_callback))

    async def post_init(application: Application) -> None:
        asyncio.create_task(payment_poller(application.bot))
        logger.info("Bot started in polling mode")

    app.post_init = post_init
    logger.info("Starting @Obsidiancirclebot...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
