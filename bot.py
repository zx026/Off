#!/usr/bin/env python3
"""
Telegram phone-lookup bot with Mongo persistence, forced channel membership,
admin/approval, rate-limiting, inline buttons, and formatted API output.

Environment variables (required):
  TELEGRAM_BOT_TOKEN   - Telegram bot token
  MONGODB_URI          - MongoDB connection string
  ADMIN_IDS            - comma-separated numeric Telegram user ids (admins)
  FORCE_CHANNEL        - channel username (e.g. @datacheak)
  API_BASE             - external API base URL (e.g. https://.../tg2phone/api)
  API_KEY              - external API key
Optional env:
  RATE_LIMIT_COUNT     - default 10
  RATE_LIMIT_WINDOW    - seconds, default 3600
  CALLBACK_TTL         - seconds for callback docs TTL, default 3600
  BROADCAST_DELAY      - float seconds, default 0.06
"""
import os
import uuid
import asyncio
import json
from datetime import datetime, timezone
from typing import Optional, Tuple, Any, Dict

import aiohttp
from motor.motor_asyncio import AsyncIOMotorClient
from urllib.parse import quote_plus

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# ----------------- Config (from env) -----------------
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.environ.get("MONGODB_URI")
ADMIN_IDS = set(int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip())
FORCE_CHANNEL = os.environ.get("FORCE_CHANNEL", "@datacheak")
API_BASE = os.environ.get("API_BASE", "https://project-fawn-eight-95.vercel.app/tg2phone/api")
API_KEY = os.environ.get("API_KEY", "Smoke")

RATE_LIMIT_COUNT = int(os.environ.get("RATE_LIMIT_COUNT", "10"))
RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "3600"))
CALLBACK_TTL = int(os.environ.get("CALLBACK_TTL", "3600"))
BROADCAST_DELAY = float(os.environ.get("BROADCAST_DELAY", "0.06"))

if not TOKEN:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN environment variable.")
if not MONGO_URI:
    raise SystemExit("Set MONGODB_URI environment variable.")
# ----------------------------------------------------

# Mongo setup
mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo.get_default_database()
users_col = db["users"]
callbacks_col = db["callbacks"]
usage_col = db["usage"]

async def ensure_indexes():
    # TTL indexes for callback and usage docs (expire docs automatically)
    try:
        await callbacks_col.create_index("created_at", expireAfterSeconds=CALLBACK_TTL)
        await usage_col.create_index("ts", expireAfterSeconds=RATE_LIMIT_WINDOW)
        await users_col.create_index("user_id", unique=True)
        await users_col.create_index("username")
        await users_col.create_index("approved")
    except Exception as e:
        print("Index creation error:", e)

# ----------------- External API -----------------
async def fetch_phone_for(target: str) -> Tuple[Optional[Dict[str, Any]], str]:
    url = f"{API_BASE}?key={quote_plus(API_KEY)}&q={quote_plus(target)}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=15) as resp:
                text = await resp.text()
                content_type = resp.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    try:
                        data = await resp.json()
                        return data, text
                    except Exception:
                        return None, text or ""
                return None, text or ""
    except asyncio.TimeoutError:
        return None, "Request timed out."
    except Exception as e:
        return None, f"Request failed: {e}"

# ----------------- Formatting -----------------
def _get_bool(d, *keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, str)) and str(v).lower() in ("1", "true", "yes", "y", "on"):
            return True
    return False

def format_result(target: str, data: Optional[Dict[str,Any]], raw: str) -> str:
    if data:
        username = data.get("username") or data.get("user") or data.get("name") or "—"
        uid = data.get("id") or data.get("user_id") or data.get("uid") or (target if str(target).isdigit() else "—")
        status = data.get("status") or data.get("presence") or "—"
        dc = data.get("dc") or data.get("data_center") or data.get("server") or "—"

        is_bot = _get_bool(data, "is_bot", "bot", "bot_account")
        verified = _get_bool(data, "verified", "is_verified", "verified_account")
        premium = _get_bool(data, "premium", "is_premium")

        phone_field = data.get("phone") or data.get("number") or data.get("phone_number") or None
        phone_number = "—"
        phone_country = "—"
        if isinstance(phone_field, dict):
            phone_number = phone_field.get("number") or phone_field.get("value") or phone_field.get("phone") or "—"
            phone_country = phone_field.get("country") or phone_field.get("country_name") or phone_field.get("country_code") or "—"
            if phone_country and str(phone_country).isdigit():
                phone_country = f"+{phone_country}"
        elif isinstance(phone_field, str):
            phone_number = phone_field
            if phone_number.startswith("+"):
                prefix = phone_number[1:4]
                phone_country = "+" + prefix
            else:
                phone_country = "—"
        else:
            phone_number = data.get("phone_number") or data.get("mobile") or "—"
            country = data.get("country") or data.get("country_name") or data.get("country_code")
            if country:
                phone_country = (f"+{country}" if str(country).isdigit() else country)

        lines = []
        lines.append(f"👤 {username}")
        lines.append(f"🆔 {uid}")
        lines.append(f"👁 Status  : {status}")
        lines.append(f"🖥 DC      : {dc}")
        lines.append("")
        lines.append(f"🤖 Bot     : {'✅' if is_bot else '❌'}")
        lines.append(f"✅ Verified: {'✅' if verified else '❌'}")
        lines.append(f"⭐ Premium : {'✅' if premium else '❌'}")
        lines.append("")
        lines.append("📞 Phone")
        lines.append(f"├ Number  : {phone_number}")
        lines.append(f"└ Country : {phone_country}")
        return "\n".join(lines)

    raw_short = raw.strip()
    if len(raw_short) > 3500:
        raw_short = raw_short[:3500] + "\n... (truncated)"
    return raw_short or "(no data returned)"

# ----------------- Helpers & DB -----------------
def normalize_input(raw: str) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip()
    if s.isdigit():
        return s
    if not s.startswith("@"):
        s = "@" + s
    if " " in s or len(s) > 64:
        return None
    return s

async def record_user(tg_user):
    if not tg_user:
        return
    now = datetime.now(timezone.utc)
    await users_col.update_one(
        {"user_id": tg_user.id},
        {"$set": {"username": getattr(tg_user, "username", None), "last_seen": now, "is_bot": getattr(tg_user, "is_bot", False)},
         "$setOnInsert": {"first_seen": now, "approved": False}},
        upsert=True,
    )

async def is_admin_or_approved(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    u = await users_col.find_one({"user_id": user_id}, {"approved": 1})
    return bool(u and u.get("approved"))

async def is_allowed_and_record(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    u = await users_col.find_one({"user_id": user_id}, {"approved": 1})
    if u and u.get("approved"):
        return True
    cnt = await usage_col.count_documents({"user_id": user_id})
    if cnt >= RATE_LIMIT_COUNT:
        return False
    await usage_col.insert_one({"user_id": user_id, "ts": datetime.now(timezone.utc)})
    return True

async def check_channel_membership_or_denied(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return False, "Unable to identify you."
    if await is_admin_or_approved(user.id):
        return True, None
    try:
        member = await context.bot.get_chat_member(FORCE_CHANNEL, user.id)
    except Exception as e:
        return False, f"Bot cannot verify membership. Make sure the bot is added to {FORCE_CHANNEL} and has permission. ({e})"
    status = getattr(member, "status", None)
    if status in ("creator", "administrator", "member"):
        return True, None
    return False, f"Please join the channel {FORCE_CHANNEL} to use this bot."

# ----------------- Command Handlers -----------------
async def phone_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await record_user(update.effective_user)
    ok, deny_msg = await check_channel_membership_or_denied(update, context)
    if not ok:
        await update.message.reply_text(deny_msg)
        return

    args = context.args or []
    targets = []
    if args:
        for a in args:
            n = normalize_input(a)
            if n:
                targets.append(n)
    else:
        if update.message and update.message.reply_to_message:
            r = update.message.reply_to_message.from_user
            if r:
                if getattr(r, "username", None):
                    targets.append("@" + r.username)
                else:
                    targets.append(str(r.id))

    if not targets:
        await update.message.reply_text("Usage: /phone @username OR /phone username OR /phone <numeric_user_id> (or reply)")
        return

    keyboard = []
    for t in targets:
        key = uuid.uuid4().hex
        doc = {"key": key, "target": t, "created_at": datetime.now(timezone.utc)}
        try:
            await callbacks_col.insert_one(doc)
        except Exception:
            pass
        keyboard.append([InlineKeyboardButton(f"Get number for {t}", callback_data=key)])

    await update.message.reply_text("Ready to fetch phone numbers for:\n" + "\n".join(f"• {t}" for t in targets),
                                    reply_markup=InlineKeyboardMarkup(keyboard))

async def callback_get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    await record_user(query.from_user)
    ok, deny_msg = await check_channel_membership_or_denied(update, context)
    if not ok:
        try:
            await query.answer(deny_msg, show_alert=True)
        except Exception:
            await query.message.reply_text(deny_msg)
        return

    key = query.data
    cb = await callbacks_col.find_one({"key": key})
    if not cb:
        try:
            await query.answer("This button expired or is invalid. Please issue the command again.", show_alert=True)
        except Exception:
            pass
        return

    requester_id = query.from_user.id
    allowed = await is_allowed_and_record(requester_id)
    if not allowed:
        await query.answer(f"Rate limit: max {RATE_LIMIT_COUNT} requests per {RATE_LIMIT_WINDOW//3600} hour(s).", show_alert=True)
        return

    target = cb["target"]
    try:
        await query.edit_message_text(f"Fetching number for {target}...")
    except Exception:
        await query.message.reply_text(f"Fetching number for {target}...")

    data, raw = await fetch_phone_for(target)

    # store API response into callbacks_col for audit (non-blocking)
    try:
        await callbacks_col.update_one({"key": key}, {"$set": {"api_raw": raw, "api_data": data, "fetched_at": datetime.now(timezone.utc)}})
    except Exception:
        pass

    formatted = format_result(target, data, raw)
    try:
        await query.edit_message_text(formatted)
    except Exception:
        await query.message.reply_text(formatted)

# Admin commands
async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender = update.effective_user
    if not sender or sender.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized: only admins can approve users.")
        return
    await record_user(sender)
    target_arg = None
    args = context.args or []
    if args:
        target_arg = args[0]
    elif update.message and update.message.reply_to_message:
        r = update.message.reply_to_message.from_user
        if r:
            target_arg = ("@" + r.username) if getattr(r, "username", None) else str(r.id)
    if not target_arg:
        await update.message.reply_text("Usage: /approve <@username|user_id> or reply to a user's message with /approve")
        return
    norm = normalize_input(target_arg) or target_arg
    now = datetime.now(timezone.utc)
    if norm.startswith("@"):
        uname = norm[1:]
        await users_col.update_one({"username": uname}, {"$set": {"approved": True, "approved_by": sender.id, "approved_at": now, "username": uname}}, upsert=True)
        await update.message.reply_text(f"Approved {norm}. They have unlimited usage.")
    else:
        try:
            uid = int(norm)
        except ValueError:
            await update.message.reply_text("Invalid target. Provide @username or numeric user id.")
            return
        await users_col.update_one({"user_id": uid}, {"$set": {"approved": True, "approved_by": sender.id, "approved_at": now, "user_id": uid}}, upsert=True)
        await update.message.reply_text(f"Approved {uid}. They have unlimited usage.")

async def unapprove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender = update.effective_user
    if not sender or sender.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized: only admins can unapprove users.")
        return
    await record_user(sender)
    target_arg = None
    args = context.args or []
    if args:
        target_arg = args[0]
    elif update.message and update.message.reply_to_message:
        r = update.message.reply_to_message.from_user
        if r:
            target_arg = ("@" + r.username) if getattr(r, "username", None) else str(r.id)
    if not target_arg:
        await update.message.reply_text("Usage: /unapprove <@username|user_id> or reply to a user's message with /unapprove")
        return
    norm = normalize_input(target_arg) or target_arg
    if norm.startswith("@"):
        uname = norm[1:]
        res = await users_col.find_one_and_update({"username": uname}, {"$set": {"approved": False}})
        if res:
            await update.message.reply_text(f"Unapproved {norm}.")
        else:
            await update.message.reply_text(f"No record for {norm}; treated as unapproved.")
    else:
        try:
            uid = int(norm)
        except ValueError:
            await update.message.reply_text("Invalid target. Provide @username or numeric user id.")
            return
        res = await users_col.find_one_and_update({"user_id": uid}, {"$set": {"approved": False}})
        if res:
            await update.message.reply_text(f"Unapproved {uid}.")
        else:
            await update.message.reply_text(f"No record for {uid}; treated as unapproved.")

async def approveds_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender = update.effective_user
    if not sender or sender.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized: only admins can list approved users.")
        return
    await record_user(sender)
    cursor = users_col.find({"approved": True}, {"user_id": 1, "username": 1, "approved_at": 1}).sort("approved_at", -1).limit(500)
    lines = []
    async for u in cursor:
        uname = ("@" + u["username"]) if u.get("username") else str(u.get("user_id"))
        at = u.get("approved_at")
        tstr = at.isoformat() if at else "?"
        lines.append(f"{uname} — approved_at: {tstr}")
    if not lines:
        await update.message.reply_text("No approved users.")
        return
    chunk = []
    msg = ""
    for l in lines:
        if len(msg) + len(l) + 1 > 3500:
            chunk.append(msg)
            msg = ""
        msg += l + "\n"
    if msg:
        chunk.append(msg)
    for c in chunk:
        await update.message.reply_text(c)

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await record_user(update.effective_user)
    total_users = await users_col.count_documents({})
    approved_count = await users_col.count_documents({"approved": True})
    current_usages = await usage_col.count_documents({})
    text = (f"Stats:\n• Total distinct users: {total_users}\n• Approved users (unlimited): {approved_count}\n"
            f"• API requests recorded (in TTL window): {current_usages}\n• Rate limit for normal users: {RATE_LIMIT_COUNT} requests per {RATE_LIMIT_WINDOW//3600} hour(s)\n"
            f"• Admins (unlimited): {len(ADMIN_IDS)}")
    await update.message.reply_text(text)

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender = update.effective_user
    if not sender or sender.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized: only admins can broadcast.")
        return
    await record_user(sender)
    args = context.args or []
    if not args and update.message.reply_to_message:
        msg_text = update.message.reply_to_message.text or ""
    else:
        msg_text = " ".join(args).strip()
    if not msg_text:
        await update.message.reply_text("Usage: /broadcast <message>\nOr reply to a message and run /broadcast")
        return
    cursor = users_col.find({}, {"user_id": 1})
    total = sent = failed = 0
    async for u in cursor:
        total += 1
        uid = u["user_id"]
        try:
            await context.bot.send_message(chat_id=uid, text=msg_text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(BROADCAST_DELAY)
    await update.message.reply_text(f"Broadcast done. Total users: {total}, sent: {sent}, failed: {failed}")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await record_user(update.effective_user)
    await update.message.reply_text(f"Hello — use /phone @username or reply to a user with /phone. Must be member of {FORCE_CHANNEL} (admins/approved bypass).")

# ----------------- Startup -----------------
async def main():
    await ensure_indexes()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("phone", phone_cmd))
    app.add_handler(CallbackQueryHandler(callback_get_phone))
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("unapprove", unapprove_cmd))
    app.add_handler(CommandHandler("approveds", approveds_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    print("Bot started.")
    app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
