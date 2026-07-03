#!/usr/bin/env python3
# Telegram bot with forced channel membership (@datacheak) for non-admin/non-approved users.
# Requirements: pip install python-telegram-bot aiohttp motor

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from motor.motor_asyncio import AsyncIOMotorClient
from urllib.parse import quote_plus

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

# ----------------- CONFIG (from your input) -----------------
# Provided by you:
TOKEN = "8794125671:AAEJltnbzbA9ITaN09wuZ0byV0QDqVZXAAY"
MONGO_URI = "mongodb+srv://yb131567_db_user:R8zxuvc9Qn999Arg@cluster0.drjaxl8.mongodb.net/telegram_bot?retryWrites=true&w=majority"
OWNER_ID = 7302427268  # owner/admin
FORCE_CHANNEL = "@datacheak"  # channel from your link https://t.me/datacheak

# API info
API_BASE = "https://project-fawn-eight-95.vercel.app/tg2phone/api"
API_KEY = "Smoke"

# Rate limiting for non-approved users
RATE_LIMIT_COUNT = 10
RATE_LIMIT_WINDOW = 3600  # seconds

# Callback TTL in Mongo (seconds). Mongo TTL index will expire callback docs after this many seconds.
CALLBACK_TTL = 3600

# Broadcast send delay to avoid flood
BROADCAST_DELAY = 0.06
# -----------------------------------------------------------

# Admin IDs set (owner included). You can add more comma-separated if you like.
ADMIN_IDS = {OWNER_ID}

# Mongo setup
mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo.get_default_database()
users_col = db["users"]
callbacks_col = db["callbacks"]
usage_col = db["usage"]


async def ensure_indexes():
    try:
        # callbacks TTL index on created_at
        await callbacks_col.create_index("created_at", expireAfterSeconds=CALLBACK_TTL)
        # usage TTL index on ts
        await usage_col.create_index("ts", expireAfterSeconds=RATE_LIMIT_WINDOW)
        # users unique index
        await users_col.create_index("user_id", unique=True)
        await users_col.create_index("username")
        await users_col.create_index("approved")
    except Exception as e:
        print("Index creation error:", e)


# ---- External API call ----
async def fetch_phone_for(target: str) -> str:
    url = f"{API_BASE}?key={quote_plus(API_KEY)}&q={quote_plus(target)}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=15) as resp:
                content_type = resp.headers.get("Content-Type", "")
                text = await resp.text()
                if "application/json" in content_type:
                    try:
                        data = await resp.json()
                        import json

                        return json.dumps(data, ensure_ascii=False, indent=2)
                    except Exception:
                        return text or "(no data)"
                return text or "(no data)"
    except asyncio.TimeoutError:
        return "Request timed out."
    except Exception as e:
        return f"Request failed: {e}"


# ---- Helpers ----
def normalize_input(raw: str) -> Optional[str]:
    if not raw:
        return None
    raw = raw.strip()
    if raw == "":
        return None
    if raw.isdigit():
        return raw
    if not raw.startswith("@"):
        raw = "@" + raw
    if " " in raw or len(raw) > 64:
        return None
    return raw


async def record_user(tg_user):
    if not tg_user:
        return
    now = datetime.now(timezone.utc)
    await users_col.update_one(
        {"user_id": tg_user.id},
        {
            "$set": {
                "username": getattr(tg_user, "username", None),
                "last_seen": now,
                "is_bot": getattr(tg_user, "is_bot", False),
            },
            "$setOnInsert": {"first_seen": now, "approved": False},
        },
        upsert=True,
    )


async def is_admin_or_approved(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    u = await users_col.find_one({"user_id": user_id}, {"approved": 1})
    return bool(u and u.get("approved"))


# Rate limit check for unapproved users; admins/approved bypass
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


# Check channel membership: admins and approved users bypass check.
# Returns (True, None) if allowed; (False, message) if not allowed (message is user-facing).
async def check_channel_membership_or_denied(update: Update, context: ContextTypes.DEFAULT_TYPE) -> (bool, Optional[str]):
    user = update.effective_user
    if not user:
        return False, "Unable to identify you."
    # Admins and approved users bypass
    if await is_admin_or_approved(user.id):
        return True, None

    # Try to get chat member status
    try:
        member = await context.bot.get_chat_member(FORCE_CHANNEL, user.id)
    except Exception as e:
        # Could be: bot not in channel, private channel, or other error
        # Inform admin to add bot to channel as admin or make channel public
        msg = (
            "Bot cannot verify channel membership right now. Make sure the bot is added to the channel "
            f"{FORCE_CHANNEL} and has permission to access members, or make the channel public. Error: {e}"
        )
        return False, msg

    status = getattr(member, "status", None)
    # allowed statuses: 'creator', 'administrator', 'member'
    if status in ("creator", "administrator", "member"):
        return True, None
    else:
        return False, f"Please join the channel {FORCE_CHANNEL} to use this bot."


# ---- Command handlers ----
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
            norm = normalize_input(a)
            if norm:
                targets.append(norm)
    else:
        if update.message and update.message.reply_to_message:
            r = update.message.reply_to_message.from_user
            if r:
                if getattr(r, "username", None):
                    targets.append("@" + r.username)
                else:
                    targets.append(str(r.id))
    if not targets:
        await update.message.reply_text(
            "Usage: /phone @username OR /phone username OR /phone <numeric_user_id>\nOr reply to a user's message with /phone"
        )
        return

    # create callback entries in Mongo
    keyboard = []
    for t in targets:
        key = uuid.uuid4().hex
        doc = {"key": key, "target": t, "created_at": datetime.now(timezone.utc)}
        try:
            await callbacks_col.insert_one(doc)
        except Exception:
            pass
        keyboard.append([InlineKeyboardButton(f"Get number for {t}", callback_data=key)])

    reply_markup = InlineKeyboardMarkup(keyboard)
    warning = ""
    if len(targets) > 30:
        warning = "\n\nNote: many targets — UI may be large; Telegram limits apply."
    text = "Ready to fetch phone numbers for:\n" + "\n".join(f"• {t}" for t in targets) + warning
    await update.message.reply_text(text, reply_markup=reply_markup)


async def callback_get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    await record_user(query.from_user)

    # Check channel membership for the pressing user
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
        await query.edit_message_text(f"Fetching number for {target}...\n(asked by @{query.from_user.username or query.from_user.id})")
    except Exception:
        await query.message.reply_text(f"Fetching number for {target}...")

    result = await fetch_phone_for(target)
    final_text = f"Result for {target}:\n{result}\n\nDone."

    try:
        await query.edit_message_text(final_text)
    except Exception:
        await query.message.reply_text(final_text)


# Admin approve/unapprove/list commands (admins only)
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
        await users_col.update_one(
            {"username": uname},
            {"$set": {"approved": True, "approved_by": sender.id, "approved_at": now, "username": uname}},
            upsert=True,
        )
        await update.message.reply_text(f"Approved {norm}. They have unlimited usage.")
    else:
        try:
            uid = int(norm)
        except ValueError:
            await update.message.reply_text("Invalid target. Provide @username or numeric user id.")
            return
        await users_col.update_one(
            {"user_id": uid},
            {"$set": {"approved": True, "approved_by": sender.id, "approved_at": now, "user_id": uid}},
            upsert=True,
        )
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
    # chunk messages
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
    text = (
        f"Stats:\n"
        f"• Total distinct users: {total_users}\n"
        f"• Approved users (unlimited): {approved_count}\n"
        f"• API requests recorded (in TTL window): {current_usages}\n"
        f"• Rate limit for normal users: {RATE_LIMIT_COUNT} requests per {RATE_LIMIT_WINDOW//3600} hour(s)\n"
        f"• Admins (unlimited): {len(ADMIN_IDS)}\n"
    )
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
    total = 0
    sent = 0
    failed = 0
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
    await update.message.reply_text(
        f"Hello — send /phone @username or reply to a user with /phone.\nYou must be a member of {FORCE_CHANNEL} to use the bot (admins/approved users bypass)."
    )


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
