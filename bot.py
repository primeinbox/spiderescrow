import os
import re
import time
import html
import asyncio
import threading
import logging
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv
from pymongo import MongoClient, ReturnDocument
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import filters
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    MessageHandler,
    ContextTypes,
)

load_dotenv()


# ===========================
# TERMINAL LOGGING
# ===========================
class CompactFormatter(logging.Formatter):
    def format(self, record):
        return super().format(record)


def setup_logging():
    """Readable production-friendly terminal logs; avoids noisy library logs."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Prevent duplicate handlers when reloaded by a process manager.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setFormatter(
        CompactFormatter(
            "[%(asctime)s] %(levelname)-8s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


setup_logging()
logger = logging.getLogger("spiderxescrow")


def log_event(scope, message, level=logging.INFO, *args):
    """Safe structured terminal logging. Never lets a bad format string crash logging."""
    try:
        rendered = message % args if args else message
    except (TypeError, ValueError):
        rendered = f"{message} | args={args!r}"
    logger.log(level, "[%-8s] %s", scope, rendered)


# ===========================
# Config (.env se aata hai)
# ===========================
# SPIDER_BOT_TOKEN=xxxx
# MONGO_URI=xxxx
# ADMIN_IDS=123,456   -> ye "OWNERS" hai, sirf ye naye bot-admin add/remove kar sakte hai

BOT_TOKEN = os.getenv("SPIDER_BOT_TOKEN")
BRAND = "@SPIDERXESCROWSERVICE"
PROVIDER = "@SPIDERXESCROWSERVICE"

MONGO_URI = os.getenv("MONGO_URI")
OWNER_IDS = set(
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
)

# In "limited" secondary accounts se /add ya /close chale to "Escrowed By" me
# inka username nahi, mapped MAIN username dikhega.
ADMIN_ALIASES = {
    8258334055: "primaxog",
    8651783270: "gareeb_jimmy",
    8940820946: "A813ss",
}

mongo_client = MongoClient(MONGO_URI) if MONGO_URI else None
mongo_db = mongo_client["spider_escrow_bots"] if mongo_client else None
coll = mongo_db["deals_spiderxescrow"] if mongo_db is not None else None
meta_coll = mongo_db["meta_spiderxescrow"] if mongo_db is not None else None
admins_coll = mongo_db["bot_admins_spiderxescrow"] if mongo_db is not None else None
users_coll = mongo_db["broadcast_users_spiderxescrow"] if mongo_db is not None else None
groups_coll = mongo_db["groups_spiderxescrow"] if mongo_db is not None else None
automod_coll = mongo_db["group_automod_spiderxescrow"] if mongo_db is not None else None

DEALS = {}

if coll is not None:
    for doc in coll.find({}):
        tid = doc.pop("_id")
        DEALS[tid] = doc
    log_event("MONGO", "%d deal(s) loaded", logging.INFO, len(DEALS))


# ---- Bot-admin set (owners + dynamically added admins) ----
BOT_ADMINS = set(OWNER_IDS)
if admins_coll is not None:
    for doc in admins_coll.find({}):
        BOT_ADMINS.add(doc["_id"])
    log_event("AUTH", "%d bot admin(s) loaded", logging.INFO, len(BOT_ADMINS))



def save_deal(tid):
    if coll is not None:
        coll.update_one({"_id": tid}, {"$set": dict(DEALS[tid])}, upsert=True)


def is_owner(uid):
    return uid in OWNER_IDS


def is_admin(uid):
    return uid in BOT_ADMINS or is_owner(uid)


def admin_only_allowed(update: Update):
    """Admin commands sirf private chat me, aur sirf admin/owner ke liye."""
    if update.effective_chat.type != "private":
        return False
    return is_admin(update.effective_user.id)



# ===========================
# GROUP AUTH HELPERS
# ===========================

async def group_admin_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow only Owner or Telegram group admins in authorized groups."""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ("group", "supergroup"):
        return False, "❌ Ye command group me use karo."

    if is_owner(user.id):
        return True, None

    if not group_is_authorized(chat.id):
        return False, "🔒 Ye group Owner ne authorize nahi kiya."

    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        return False, "❌ Aapka group admin status check nahi ho paaya."

    if member.status not in ("administrator", "creator"):
        return False, "❌ Sirf group admin/Owner ye command use kar sakta hai."

    return True, None


async def group_control_allowed(update: Update):
    """Basic gate for commands that work in groups."""
    chat = update.effective_chat

    if chat.type not in ("group", "supergroup"):
        return True, None

    if not group_is_authorized(chat.id):
        return False, "🔒 Ye group Owner ne authorize nahi kiya."

    return True, None


async def add_close_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gate /add and /close: Owner or Telegram group admin, authorized group only."""
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ("group", "supergroup"):
        return True, None

    if is_owner(user.id):
        if not group_is_authorized(chat.id):
            return False, "🔒 Ye group Owner ne authorize nahi kiya."
        return True, None

    if not group_is_authorized(chat.id):
        return False, "🔒 Ye group Owner ne authorize nahi kiya."

    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        return False, "❌ Aapka group admin status check nahi ho paaya."

    if member.status not in ("administrator", "creator"):
        return False, "❌ Sirf group admin/Owner /add aur /close use kar sakta hai."

    return True, None


def find_user_target(context: ContextTypes.DEFAULT_TYPE, update: Update):
    """Resolve target for the existing status renderer."""
    user = update.effective_user

    if update.message and update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        if target:
            username = (target.username or "").lstrip("@").lower()
            return target.id, username, target.first_name or "User"

    return user.id, (user.username or "").lstrip("@").lower(), user.first_name or "User"


def status_for_target(user_id, username, first_name):
    """Build stats for an explicit target without changing the normal /stats UI."""
    username = (username or "").lstrip("@").lower()

    # Existing deals store escrowed_by as a username.
    mine = []
    for d in DEALS.values():
        escrowed_by = str(d.get("escrowed_by") or "").lstrip("@").lower()
        if username and escrowed_by == username:
            mine.append(d)

    completed = [d for d in mine if d.get("status") == "COMPLETED"]
    active = [d for d in mine if d.get("status") == "ACTIVE"]

    totals = {"TON": 0.0, "USDT": 0.0, "INR": 0.0}
    for d in completed:
        cur = d.get("currency", "INR")
        totals[cur] = totals.get(cur, 0.0) + float(d.get("amount", 0.0) or 0.0)

    board = build_leaderboard(today_only=False)
    rank_key = username or str(user_id)
    rank = get_rank(rank_key, board, by="deals")

    return (
        f"{pe('📈')} <b>{esc(first_name)} Deal status !</b>\n"
        "──────────────────\n"
        f"{pe('🚀')} Rank ➤ #{rank}\n\n"
        f"{pe('🔥')} Active deals ➤ {len(active)}\n\n"
        f"{pe('✅')} Total Escrow's ➤ {len(completed)}\n\n"
        f"{pe('⚡')} Total Volume :\n"
        f"  {pe('🪙')} ➤ {totals.get('TON', 0.0):g} TON\n"
        f"  {pe('💰')} ➤ {totals.get('USDT', 0.0):g} USDT\n"
        f"  {pe('🤑')} ➤ {totals.get('INR', 0.0):g} ₹\n"
        "──────────────────\n"
        f"{pe('📱')} Escrow Bot for @SPIDERXESCROWSERVICE\n"
        f"{pe('💤')} Provided by @SPIDERXESCROWSERVICE !"
    )


# ===========================
# GROUP CONTROL / AUTHORIZATION
# ===========================

def register_group(chat, bot_status=None):
    """Bot jis group/supergroup me add hua hai uska record Mongo me rakho."""
    if groups_coll is None or chat is None:
        return

    now = datetime.now(timezone.utc).isoformat()
    existing = groups_coll.find_one({"_id": chat.id})

    data = {
        "title": chat.title or f"Chat {chat.id}",
        "type": chat.type,
        "username": getattr(chat, "username", None),
        "updated_at": now,
    }

    if bot_status is not None:
        data["bot_status"] = bot_status

    if existing is None:
        data["authorized"] = False
        data["added_at"] = now

    ensure_group_runtime(chat.id)

    groups_coll.update_one(
        {"_id": chat.id},
        {"$set": data},
        upsert=True,
    )


def group_is_authorized(chat_id):
    if groups_coll is None:
        return False
    try:
        doc = groups_coll.find_one({"_id": int(chat_id)})
    except Exception:
        doc = groups_coll.find_one({"_id": chat_id})
    return bool(doc and doc.get("authorized") is True)


# ===========================
# Sequential Trade ID: DL-SPIDER-1, DL-SPIDER-2, ...
# ===========================

def next_trade_id():
    if meta_coll is not None:
        doc = meta_coll.find_one_and_update(
            {"_id": "trade_counter"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        seq = doc["seq"]
    else:
        seq = len(DEALS) + 1

    tid = f"DL-SPIDER-{seq}"
    while tid in DEALS:  # safety, collision na ho
        seq += 1
        tid = f"DL-SPIDER-{seq}"
    return tid


# ===========================
# Helpers
# ===========================

def esc(text):
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


def fmt(amount, currency="INR"):
    if currency in ("USDT", "TON"):
        return f"{amount:,.2f} {currency}"
    symbol = {"INR": "₹", "USD": "$"}.get(currency, "")
    return f"{symbol}{amount:,.2f}"


def extract_amount(text):
    match = re.search(r"[\d,]+(?:\.\d+)?", text or "")
    if not match:
        return 0.0
    value = match.group(0).replace(",", "")
    try:
        return float(value)
    except ValueError:
        return 0.0


def resolve_username(update: Update):
    user_id = update.effective_user.id
    if user_id in ADMIN_ALIASES:
        return "@" + ADMIN_ALIASES[user_id]
    return (
        "@" + update.effective_user.username
        if update.effective_user.username
        else update.effective_user.first_name
    )


# Bold unicode (Mathematical Sans-Bold) helpers
_UP = ord('𝗔') - ord('A')
_LOW = ord('𝗮') - ord('a')
_DIG = ord('𝟬') - ord('0')


def normalize_bold(text):
    out = []
    for ch in text:
        code = ord(ch)
        if ord('𝗔') <= code <= ord('𝗭'):
            out.append(chr(code - _UP))
        elif ord('𝗮') <= code <= ord('𝘇'):
            out.append(chr(code - _LOW))
        elif ord('𝟬') <= code <= ord('𝟵'):
            out.append(chr(code - _DIG))
        else:
            out.append(ch)
    return "".join(out)


# ===========================
# Premium Emoji IDs
# ===========================
# Ye IDs Telegram Premium custom-emoji document IDs hain. Har entry me "character"
# (jaise ⭐, ❤️) sirf fallback hai un clients ke liye jinke paas Premium nahi hai —
# actual visual wahi custom emoji hoga jiska ID diya gaya hai.
#
# Agar koi ID galat / expired ho jaaye to Telegram sirf fallback character dikha
# dega (crash nahi hoga). Apni khud ki custom emoji IDs nikalne ke liye:
#   1) Us emoji ko kisi message me bhejo jisme HTML/entities dikhne wala export ho
#      (ya koi "emoji id finder" utility bot use karo jo message forward karke
#      custom_emoji entities se ID nikaalta hai).
#   2) Wahan se mile document_id ko yaha neeche waali dict me daal do.
#
# Neeche di gayi IDs me se check / trade / escrow verify ho chuki hain (working).
PE = {
    "⭐️": "6307695271346184221",
    "❤️": "5260535596941582167",
    "💬": "5258330865674494479",
    "🍑": "5323761960829862762",
    "⚡️": "5938539885907415367",
    "🌐": "6041705726206808304",
    "🔥": "5420315771991497307",
    "📈": "5774022692642492953",
    "🪙": "5884428842780594914",
    "💰": "6039802097916974085",
    "🤑": "5893473283696759404",
    "📱": "6152069549442208798",
    "💤": "5895266423952904371",
    "✅": "5197474765387864959",
    "🆔": "5936017305585586269",
    "🛡": "5920052658743283381",
    "📤": "6030822047150512346",
    "⭐": "5879785854284599288",
    "👤": "5258011929993026890",
    "📝": "5879841310902324730",
    "⏱️": "5936170807716745162",
    "📌": "5796440171364749940",
    "🛡️": "5920052658743283381",
    "🚀": "5780773956030043338",
    "🏆": "6194737030165959506",
    "👑": "5807868868886009920",
    "📖": "5258328383183396223",
    "ℹ️": "5994473545650934240",
}


def pe(emoji):
    """Return a Telegram custom emoji tag only for verified IDs."""
    emoji_id = PE.get(emoji)
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>'
    return emoji


# ===========================
# CHARGES (amount ke hisaab se slabs)
# ===========================

def calculate_fee(amount, is_exchange=False):
    if is_exchange:
        return amount * 0.025

    if amount <= 50:
        return 3.0
    elif amount <= 100:
        return 5.0
    elif amount <= 500:
        return 10.0
    elif amount <= 1000:
        return 15.0
    elif amount <= 2000:
        return 20.0
    else:
        return amount * 0.03

# ===========================
# Dashboard views
# ===========================

def main_menu_kb():
    rows = [
        [InlineKeyboardButton("✦ My status", callback_data="menu:my_status")],
        [InlineKeyboardButton("★ My Deals Info", callback_data="menu:my_deals")],
        [InlineKeyboardButton("➤ My Pending Deals", callback_data="menu:pending")],
        [InlineKeyboardButton("✓ Escrow Global status", callback_data="menu:global")],
    ]
    return InlineKeyboardMarkup(rows)


def status_kb():
    """Keyboard jo /status ke saath jaata hai — private aur group dono me kaam karta hai."""
    rows = [
        [InlineKeyboardButton("★ My Deals Info", callback_data="menu:my_deals")],
        [InlineKeyboardButton("➤ My Pending Deals", callback_data="menu:pending")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="refresh:my_status")],
    ]
    return InlineKeyboardMarkup(rows)


def back_refresh_kb(refresh_target):
    rows = [
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{refresh_target}")],
        [InlineKeyboardButton("➤ Back", callback_data="menu:back")],
    ]
    return InlineKeyboardMarkup(rows)


def welcome_text(first_name):
    return (
        f"{pe('⭐️')} <b>Welcome {esc(first_name)}!</b>\n"
        "╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍\n"
        f"{pe('❤️')} Escrow Bot for {BRAND}\n"
        f"{pe('💬')} Provided by @SPIDERXESCROWSERVICE\n\n"
        f"{pe('🍑')} <b>This is Your Personal Dashboard:</b>\n"
        "╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍\n"
        f"Select the option below {pe('⚡️')}\n"
        "╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍"
    )


def global_status_text():
    completed = [d for d in DEALS.values() if d.get("status") == "COMPLETED"]
    totals = {"TON": 0.0, "USDT": 0.0, "INR": 0.0}
    for d in completed:
        cur = d.get("currency", "INR")
        totals[cur] = totals.get(cur, 0.0) + d.get("amount", 0.0)

    lines = [
        f"{pe('🌐')} <b>Escrow Global Statistics</b>",
        "──────────────────",
        f"{pe('🔥')} Total Deals: {len(completed)}\n",
        f"{pe('⚡️')} <b>Total Volume:</b>",
        f"  {pe('🪙')} - {totals['TON']:g} TON",
        f"  {pe('💰')} - {totals['USDT']:g} USDT",
        f"  {pe('🤑')} - {totals['INR']:g} ₹",
        "──────────────────",
        f"{pe('📱')} Escrow Bot for {BRAND}",
        f"{pe('💤')} Provided by {PROVIDER}",
    ]
    return "\n".join(lines)


# ---- Leaderboard / rank ----

def _is_today(iso_ts):
    if not iso_ts:
        return False
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return False
    return ts.date() == datetime.now(timezone.utc).date()


def build_leaderboard(today_only=False):
    board = {}
    for d in DEALS.values():
        if d.get("status") != "COMPLETED":
            continue
        if today_only and not _is_today(d.get("completed_at")):
            continue
        user = d.get("escrowed_by", "-")
        entry = board.setdefault(user, {"deals": 0, "volume": 0.0})
        entry["deals"] += 1
        entry["volume"] += d.get("amount", 0.0)
    return board


def get_rank(username, board, by="deals"):
    ranked = sorted(board.items(), key=lambda kv: kv[1][by], reverse=True)
    for i, (user, _) in enumerate(ranked, start=1):
        if user == username:
            return i
    return len(ranked) + 1


def my_status_text(update: Update):
    username = resolve_username(update)
    first_name = update.effective_user.first_name

    mine = [d for d in DEALS.values() if d.get("escrowed_by") == username]
    completed = [d for d in mine if d.get("status") == "COMPLETED"]
    active = [d for d in mine if d.get("status") == "ACTIVE"]

    totals = {"TON": 0.0, "USDT": 0.0, "INR": 0.0}
    for d in completed:
        cur = d.get("currency", "INR")
        totals[cur] = totals.get(cur, 0.0) + d.get("amount", 0.0)

    board = build_leaderboard(today_only=False)
    rank = get_rank(username, board, by="deals")

    return (
        f"{pe('📈')} <b>{esc(first_name)} Deal status !</b>\n"
        "──────────────────\n"
        f"{pe('🚀')} Rank ➤ #{rank}\n\n"
        f"{pe('🔥')} Active deals ➤ {len(active)}\n\n"
        f"{pe('✅')} Total Escrow's ➤ {len(completed)}\n\n"
        f"{pe('⚡')} Total Volume :\n"
        f"  {pe('🪙')} ➤ {totals['TON']:g} TON\n"
        f"  {pe('💰')} ➤ {totals['USDT']:g} USDT\n"
        f"  {pe('🤑')} ➤ {totals['INR']:g} ₹\n"
        "──────────────────\n"
        f"{pe('📱')} Escrow Bot for {BRAND}\n"
        f"{pe('💤')} Provided by {PROVIDER} !"
    )


# ---- My Deals Info: paginated list + detail view ----

PAGE_SIZE = 6


def deal_status_display(status):
    return {
        "ACTIVE": "🟡 PENDING",
        "HOLD": "⏸️ HOLD",
        "COMPLETED": "✅ DONE",
        "CANCELLED": "❌ CANCELLED",
    }.get(status, status)


def deal_detail_text(tid, deal):
    lines = [
        f"Your Deal-{esc(tid)} Info !",
        "──────────────────",
        f"➥ status: {deal_status_display(deal.get('status', '-'))}",
        f"➥ Buyer: {esc(deal.get('buyer', '-'))}",
        f"➥ Seller: {esc(deal.get('seller', '-'))}",
        f"➥ Amount: {fmt(deal.get('amount', 0), deal.get('currency', 'INR'))}",
        f"➥ Fees: {deal.get('fee_percent', 0):.1f}%",
        f"➥ Escrowed by: {esc(deal.get('escrowed_by', '-'))}",
    ]

    if deal.get("created_at"):
        dt = datetime.fromisoformat(deal["created_at"])
        lines.append(f"➥ Start Time: {dt.strftime('%H:%M:%S')}")
        lines.append(f"     [ {dt.strftime('%d %B %Y')} ]")

    if deal.get("completed_at"):
        dt2 = datetime.fromisoformat(deal["completed_at"])
        lines.append(f"➥ End Time: {dt2.strftime('%H:%M:%S')}")
        lines.append(f"     [ {dt2.strftime('%d %B %Y')} ]")

    lines += [
        "──────────────────",
        f"{pe('📱')} Escrow Bot for {BRAND}",
        f"{pe('💤')} Provided by {PROVIDER}",
    ]
    return "\n".join(lines)


def my_deals_header_text(update: Update):
    first_name = update.effective_user.first_name
    return (
        f"{pe('♡')} <b>{esc(first_name)} All deals info !</b>\n"
        "──────────────────\n"
        "Select the deal below for info :\n"
        "──────────────────"
    )


def my_deals_ids(update: Update):
    username = resolve_username(update)
    ids = [tid for tid, d in DEALS.items() if d.get("escrowed_by") == username]
    return list(reversed(ids))  # naye deals upar


def my_deals_kb(update: Update, page=0):
    ids = my_deals_ids(update)
    start = page * PAGE_SIZE
    chunk = ids[start:start + PAGE_SIZE]

    rows = [[InlineKeyboardButton(tid, callback_data=f"dealview:{tid}:{page}")] for tid in chunk]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"dealspage:{page-1}"))
    if start + PAGE_SIZE < len(ids):
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"dealspage:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("➤ Back", callback_data="menu:back")])
    return InlineKeyboardMarkup(rows), len(ids)


def deal_view_kb(page):
    rows = [
        [InlineKeyboardButton("◀ Back to My Deals", callback_data=f"dealspage:{page}")],
        [InlineKeyboardButton("➤ Main Menu", callback_data="menu:back")],
    ]
    return InlineKeyboardMarkup(rows)


def pending_deals_text(update: Update):
    username = resolve_username(update)
    pending = [
        (tid, d)
        for tid, d in DEALS.items()
        if d.get("escrowed_by") == username and d.get("status") == "ACTIVE"
    ]
    if not pending:
        return f"{pe('➤')} Koi pending deal nahi hai."

    lines = [f"{pe('➤')} <b>My Pending Deals</b>", "──────────────────"]
    for tid, d in pending:
        lines.append(
            f"<code>{esc(tid)}</code> — "
            f"{esc(d.get('buyer','-'))} ↔ {esc(d.get('seller','-'))} — "
            f"{fmt(d.get('amount',0), d.get('currency','INR'))}"
        )
    return "\n".join(lines)


# ===========================
# Broadcast subscribers
# ===========================

def remember_user(update: Update):
    if users_coll is None or not update.effective_user:
        return
    u = update.effective_user
    users_coll.update_one(
        {"_id": u.id},
        {"$set": {
            "username": u.username,
            "first_name": u.first_name,
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    if users_coll is None:
        await update.message.reply_text("❌ MongoDB required for /broadcast.")
        return

    message = update.message.text.partition(" ")[2].strip()
    if not message:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    sent = failed = 0
    for doc in users_coll.find({}, {"_id": 1}):
        try:
            await context.bot.send_message(chat_id=doc["_id"], text=message)
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"📢 Broadcast finished.\nSent: {sent}\nFailed: {failed}"
    )


# ===========================
# /start
# ===========================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
    if update.effective_chat.type != "private":
        return  # group me /start kaam nahi karega

    await update.message.reply_text(
        welcome_text(update.effective_user.first_name),
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(),
    )


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    log_event(
        "CALLBACK",
        "generic router | data=%s | user=%s",
        logging.INFO,
        data,
        query.from_user.id if query.from_user else None,
    )

    if data == "menu:back":
        # Private me full dashboard, group me wapas apne status pe.
        if update.effective_chat.type == "private":
            await query.edit_message_text(
                welcome_text(update.effective_user.first_name),
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_kb(),
            )
        else:
            await query.edit_message_text(
                my_status_text(update),
                parse_mode=ParseMode.HTML,
                reply_markup=status_kb(),
            )
        return

    if data in ("menu:my_deals",) or data.startswith("dealspage:"):
        page = 0
        if data.startswith("dealspage:"):
            page = int(data.split(":", 1)[1])
        kb, total = my_deals_kb(update, page)
        if total == 0:
            text = my_deals_header_text(update) + "\n\n📭 Koi deal nahi mili."
        else:
            text = my_deals_header_text(update)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if data.startswith("dealview:"):
        _, tid, page = data.split(":", 2)
        deal = DEALS.get(tid)
        if not deal:
            await query.edit_message_text("❌ Deal not found.", reply_markup=deal_view_kb(int(page)))
            return
        await query.edit_message_text(
            deal_detail_text(tid, deal),
            parse_mode=ParseMode.HTML,
            reply_markup=deal_view_kb(int(page)),
        )
        return

    target = None
    if data in ("menu:my_status", "refresh:my_status"):
        target = "my_status"
        text = my_status_text(update)
    elif data in ("menu:pending", "refresh:pending"):
        target = "pending"
        text = pending_deals_text(update)
    elif data in ("menu:global", "refresh:global"):
        target = "global"
        text = global_status_text()
    else:
        log_event(
            "CALLBACK",
            "Unhandled callback | user=%s | data=%s",
            logging.WARNING,
            update.effective_user.id if update.effective_user else "?",
            data,
        )
        await query.answer("⚠️ This button is outdated. Please reopen the menu.", show_alert=True)
        return

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=back_refresh_kb(target),
    )


# ===========================
# /status  — HAR USER, PRIVATE + GROUP dono me kaam karega
# ===========================

async def _legacy_mystatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)

    target = update.effective_user
    if not target:
        return

    # Proxy target is already resolved by mystatus_cmd.
    username = (getattr(target, "username", None) or "").lstrip("@").lower()
    first_name = getattr(target, "first_name", None) or "User"

    # For the command sender, preserve the original UI exactly.
    if not (update.message and update.message.reply_to_message):
        text = my_status_text(update)
    else:
        text = status_for_target(
            getattr(target, "id", None),
            username,
            first_name,
        )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=status_kb(),
    )


# ===========================
# /add
# ===========================

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    allowed, reason = await add_close_allowed(update, context)
    if not allowed:
        if reason and update.message:
            await update.message.reply_text(reason)
        return
    
    raw_text = (
        update.message.reply_to_message.text
        if update.message.reply_to_message
        else ""
    )
    text = normalize_bold(raw_text)

    # Deal template uses a bullet before every field:
    # "• SELLER : @username". Allow that bullet (and whitespace) explicitly.
    field_prefix = r"(?:^|\n)\s*(?:[•·▪▫●○‣➜➤-]\s*)?"
    seller = re.search(field_prefix + r"SELLER\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)
    buyer = re.search(field_prefix + r"BUYER\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)
    detail = re.search(field_prefix + r"DEAL\s+DETAIL\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)
    amount = re.search(field_prefix + r"DEAL\s+AMOUNT\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)
    exp_time = re.search(field_prefix + r"EXPECTED\s+TIME\s+TO\s+COMPLETE\s+DEAL\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)
    tc = re.search(field_prefix + r"T\s*/\s*C\s*(?:\(\s*IF\s+ANY\s*\))?\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)
    currency = re.search(field_prefix + r"CURRENCY\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)

    seller_val = seller.group(1).strip() if seller else "-"
    buyer_val = buyer.group(1).strip() if buyer else "-"
    detail_val = detail.group(1).strip() if detail else "-"
    # Normal form se amount
    form_amount_val = extract_amount(amount.group(1)) if amount else 0.0

    exp_time_val = exp_time.group(1).strip() if exp_time else "-"
    tc_val = tc.group(1).strip() if tc else "-"
    currency_val = currency.group(1).strip().upper() if currency else "INR"

    # ==================================================
    # /add 500 -> ₹500 amount use hoga
    # /add      -> form wala DEAL AMOUNT use hoga
    # /add exchange -> form amount + exchange fee
    # ==================================================

    is_exchange = False
    amount_val = form_amount_val

    if context.args:
        arg = context.args[0].strip()

        if arg.lower() == "exchange":
            is_exchange = True
        else:
        # /add 500, /add 3000, /add 1,500 etc.
            custom_amount = extract_amount(arg)

            if custom_amount > 0:
                amount_val = custom_amount

    tid = next_trade_id()
    creator_username = resolve_username(update)

    fee_amount = calculate_fee(amount_val, is_exchange)
    release_val = amount_val - fee_amount
    fee_percent = (fee_amount / amount_val * 100) if amount_val else 0.0

    DEALS[tid] = {
        "seller": seller_val,
        "buyer": buyer_val,
        "detail": detail_val,
        "amount": amount_val,
        "release": release_val,
        "fee_percent": fee_percent,
        "exp_time": exp_time_val,
        "tc": tc_val,
        "currency": currency_val,
        "status": "ACTIVE",
        "escrowed_by": creator_username,
        "escrowed_by_id": update.effective_user.id,
        "created_by_id": update.effective_user.id,
        "chat_id": update.effective_chat.id,
        "exchange": is_exchange,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_deal(tid)

    msg = (
        f"{pe('💰')} <b>Deal Amount:</b> {fmt(amount_val, currency_val)}\n"
        f"{pe('📤')} <b>Fee:</b> {fee_percent:.2f}% — {fmt(fee_amount, currency_val)}\n"
        f"{pe('📤')} <b>Net Release:</b> {fmt(release_val, currency_val)}\n"
        # f"{pe('📤')} <b>Net Release:</b> {fmt(amount_val, currency_val)}\n"
        f"{pe('🆔')} <b>Trade ID:</b> <code>{esc(tid)}</code>\n\n"
        f"{pe('👤')} <b>Buyer:</b> {esc(buyer_val)}\n"
        f"{pe('👤')} <b>Seller:</b> {esc(seller_val)}\n"
        f"{pe('📝')} <b>Detail:</b> {esc(detail_val)}\n"
        f"{pe('⏱️')} <b>Expected Time:</b> {esc(exp_time_val)}\n"
        f"{pe('📌')} <b>T/C:</b> {esc(tc_val)}\n\n"
        f"{pe('🛡')} <b>Escrowed By:</b> {esc(creator_username)}"
    )

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    try:
        await update.message.delete()
    except Exception:
        pass


# ===========================
# /hold — owner-only admin hold report
# ===========================

HOLD_ADMIN_EMOJI_ID = "5258011929993026890"


def _hold_admin_emoji():
    return pe('🛡️')


def _is_owner(user_id):
    return user_id in OWNER_IDS


async def hold_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Owner-only admin hold report.

    Shows every bot admin's currently open (ACTIVE) deal amount.
    /close removes the deal from this report automatically because its
    status changes to COMPLETED/CANCELLED.
    Non-owner users get no response.
    """
    if not update.effective_user or not is_admin(update.effective_user.id):
        return

    if update.effective_chat.type in ("group", "supergroup"):
        group_ok, reason = await group_control_allowed(update)
        if not group_ok:
            if reason and update.message:
                await update.message.reply_text(reason)
            return

    # Only the owner can use this command, regardless of chat type.
    open_deals = [
        (tid, deal) for tid, deal in DEALS.items()
        if deal.get("status") == "ACTIVE"
    ]

    # Group ACTIVE deals by the admin/escrower who created them.
    grouped = {}
    for tid, deal in open_deals:
        admin = deal.get("escrowed_by") or deal.get("created_by") or "-"
        grouped.setdefault(admin, []).append((tid, deal))

    lines = [
        f"{_hold_admin_emoji()} <b>ADMIN HOLD</b>",
        "",
    ]

    if not grouped:
        lines.append("No active deals are currently on hold.")
    else:
        grand_total = 0.0

        for admin in sorted(grouped, key=lambda x: x.lower()):
            deals = grouped[admin]
            admin_total = sum(float(d.get("amount", 0) or 0) for _, d in deals)
            grand_total += admin_total

            lines.append(
                f"{_hold_admin_emoji()} <b>{esc(admin)}</b> — "
                f"<b>Total Hold: {fmt(admin_total, 'INR')}</b>"
            )

            for tid, deal in deals:
                amount = float(deal.get("amount", 0) or 0)
                currency = deal.get("currency", "INR")
                buyer = esc(deal.get("buyer", "-"))
                seller = esc(deal.get("seller", "-"))
                detail = esc(deal.get("detail", "-"))
                fee = float(deal.get("fee_percent", 0) or 0)
                release = float(deal.get("release", 0) or 0)

                lines.extend([
                    f"  • <code>{esc(tid)}</code> — <b>{fmt(amount, currency)}</b>",
                    f"    Buyer: {buyer}",
                    f"    Seller: {seller}",
                    f"    Fee: {fee:.2f}% — Net: {fmt(release, currency)}",
                    f"    Detail: {detail}",
                ])
            lines.append("")

        lines.append("──────────────────")
        lines.append(
            f"{_hold_admin_emoji()} <b>ALL ADMINS TOTAL HOLD: "
            f"{fmt(grand_total, 'INR')}</b>"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ===========================
# /close
# ===========================

async def close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    allowed, reason = await add_close_allowed(update, context)

    if not allowed:
        if reason and update.message:
            await update.message.reply_text(reason)
        return

    tid = None
    released_amount_arg = None

    # ==========================================
    # CASE 1: Direct ID
    #
    # /close DL-SPIDER-4
    # /close DL-SPIDER-4 300
    # /close DL-SPIDER-4 cancel
    # ==========================================
    if context.args and re.fullmatch(
        r"DL-SPIDER-\d+",
        context.args[0],
        re.IGNORECASE
    ):
        tid = context.args[0].upper()

        if len(context.args) > 1:
            released_amount_arg = context.args[1]

    # ==========================================
    # CASE 2: Reply karke
    #
    # /close
    # /close 300
    # /close cancel
    # ==========================================
    elif update.message.reply_to_message:
        reply_text = update.message.reply_to_message.text or ""

        match = re.search(
            r"Trade ID:\s*(DL-SPIDER-\d+)",
            reply_text,
            re.IGNORECASE
        )

        if not match:
            await update.message.reply_text(
                "❌ Reply kiye gaye message me Trade ID nahi mila."
            )
            return

        tid = match.group(1).upper()

        if context.args:
            released_amount_arg = context.args[0]

    # ==========================================
    # Invalid usage
    # ==========================================
    else:
        await update.message.reply_text(
            "❌ Deal close karne ke liye:\n\n"
            "<b>Reply karke:</b>\n"
            "<code>/close</code>\n"
            "<code>/close 300</code>\n"
            "<code>/close cancel</code>\n\n"
            "<b>Ya direct ID se:</b>\n"
            "<code>/close DL-SPIDER-4</code>\n"
            "<code>/close DL-SPIDER-4 300</code>\n"
            "<code>/close DL-SPIDER-4 cancel</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # ==========================================
    # Deal lookup
    # ==========================================
    deal = DEALS.get(tid)

    if not deal:
        await update.message.reply_text(
            f"❌ Deal <code>{esc(tid)}</code> not found.",
            parse_mode=ParseMode.HTML,
        )
        return

    # ==========================================
    # CLOSE PERMISSION CHECK
    #
    # Owner -> kisi ki bhi deal close kar sakta hai
    # Admin -> sirf apni create ki hui deal close kar sakta hai
    # ==========================================
    closer_id = update.effective_user.id
    deal_creator_id = deal.get("created_by_id")

    if not is_owner(closer_id):

        # New deals:
        # Telegram user ID exact match
        if deal_creator_id is not None:

            if closer_id != deal_creator_id:
                await update.message.reply_text(
                    "❌ Tum sirf apni create ki hui deal close kar sakte ho."
                )
                return

        # Old deals:
        # created_by_id nahi hai to username fallback
        else:

            if resolve_username(update) != deal.get("escrowed_by"):
                await update.message.reply_text(
                    "❌ Tum sirf apni create ki hui deal close kar sakte ho."
                )
                return

    # ==========================================
    # Status check
    # ==========================================
    if deal.get("status") == "HOLD":
        await update.message.reply_text(
            "⏸️ Yeh deal HOLD par hai. Pehle /unhold karo."
        )
        return

    if deal.get("status") != "ACTIVE":
        await update.message.reply_text(
            f"❌ Yeh deal already {deal.get('status', 'closed')} hai."
        )
        return

    # ==========================================
    # Cancel / Complete
    # ==========================================
    is_cancel = (
        released_amount_arg
        and released_amount_arg.lower() == "cancel"
    )

    currency_val = deal.get("currency", "INR")

    if is_cancel:
        released_val = 0.0

    elif released_amount_arg:
        released_val = extract_amount(released_amount_arg)

    else:
        released_val = deal.get("release", 0.0)

    # ==========================================
    # Update deal
    # ==========================================
    deal["status"] = "CANCELLED" if is_cancel else "COMPLETED"
    deal["released"] = released_val
    deal["completed_at"] = datetime.now(timezone.utc).isoformat()

    # Optional: actual kisne close kiya record karo
    deal["closed_by_id"] = closer_id
    deal["closed_by"] = resolve_username(update)

    save_deal(tid)

    closer = resolve_username(update)

    # ==========================================
    # Cancel message
    # ==========================================
    if is_cancel:

        msg = (
            f"❌ <b>Deal Cancelled</b>\n"
            f"{pe('🆔')} Trade ID: <code>{esc(tid)}</code>\n"
            f"{pe('ℹ️')} 100% of the charge has been deducted.\n"
            f"{pe('🛡️')} Escrowed By: {esc(deal.get('escrowed_by', '-'))}"
        )

    # ==========================================
    # Completed message
    # ==========================================
    else:

        msg = (
            f"{pe('✅')} <b>Deal Completed</b>\n"
            f"{pe('🆔')} Trade ID: <code>{esc(tid)}</code>\n"
            f"{pe('📤')} Released: {fmt(released_val, currency_val)}\n"
            f"{pe('🛡️')} Escrowed By: {esc(deal.get('escrowed_by', '-'))}\n\n"
            f"~ {esc(deal['buyer'])} and {esc(deal['seller'])} are requested to "
            f"drop the vouch before leaving👇🏻\n\n"
            f"<code>Vouch {esc(deal.get('escrowed_by', '-'))} for "
            f"{fmt(released_val, currency_val)} smooth escrow deal</code>\n"
        )

    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML
    )

    # Command message delete
    try:
        await update.message.delete()
    except Exception:
        pass


# ===========================
# /alldeals, /leaderboard, /deal — admin only, private chat only, silent skip warna
# ===========================

async def alldeals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Purana '/status' — ab admin ke liye saari deals ki poori list, private-only."""
    if not admin_only_allowed(update):
        return

    if not DEALS:
        await update.message.reply_text("📭 Koi deal record nahi hai.")
        return

    lines = [f"📊 <b>Total Deals:</b> {len(DEALS)}\n"]
    for tid, d in DEALS.items():
        lines.append(
            f"<code>{esc(tid)}</code> — {d['status']} — "
            f"{esc(d.get('buyer','-'))} ↔ {esc(d.get('seller','-'))} — "
            f"{fmt(d.get('amount',0), d.get('currency','INR'))}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only_allowed(update):
        return

    today_board = build_leaderboard(today_only=True)
    all_board = build_leaderboard(today_only=False)

    def top_line(board, by):
        if not board:
            return "  koi data nahi"
        top_user, status = max(board.items(), key=lambda kv: kv[1][by])
        return f"  {esc(top_user)} — {status['deals']} deals, ₹{status['volume']:,.2f}"

    msg = (
        f"{pe('🏆')} <b>Leaderboard</b>\n"
        "──────────────────\n"
        f"<b>📅 Today</b>\n"
        f"🔥 Top Dealer (most deals):\n{top_line(today_board, 'deals')}\n"
        f"💰 Top Earner (most volume):\n{top_line(today_board, 'volume')}\n\n"
        f"<b>♾ All-Time</b>\n"
        f"🔥 Top Dealer (most deals):\n{top_line(all_board, 'deals')}\n"
        f"💰 Top Earner (most volume):\n{top_line(all_board, 'volume')}"
    )

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def deal_lookup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/deal DL-SPIDER-5 -> admin kisi bhi deal ki full detail (escrowed_by samet) dekh sakta hai."""
    if not admin_only_allowed(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: <code>/deal DL-SPIDER-5</code>", parse_mode=ParseMode.HTML)
        return

    tid = context.args[0].upper()
    deal = DEALS.get(tid)
    if not deal:
        await update.message.reply_text("❌ Deal not found.")
        return

    await update.message.reply_text(deal_detail_text(tid, deal))


# ===========================
# Bot-admin management — sirf OWNER (.env ADMIN_IDS) add/remove kar sakta hai
# ===========================

async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_owner(update.effective_user.id):
        return

    target_user = None

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        target_id = target_user.id

    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])

    else:
        await update.message.reply_text(
            "Usage: kisi user ke message pe reply karke /addadmin bhejo, "
            "ya <code>/addadmin &lt;user_id&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    BOT_ADMINS.add(target_id)

    admin_data = {
        "added_by": update.effective_user.id,
    }

    # Reply se add karne par user's real Telegram details save hongi
    if target_user:
        admin_data.update({
            "username": target_user.username,
            "first_name": target_user.first_name,
            "last_name": target_user.last_name,
        })

    if admins_coll is not None:
        admins_coll.update_one(
            {"_id": target_id},
            {"$set": admin_data},
            upsert=True,
        )

    await update.message.reply_text(
        f"✅ <code>{target_id}</code> ab bot admin hai.",
        parse_mode=ParseMode.HTML,
    )


async def removeadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_owner(update.effective_user.id):
        return

    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])
    else:
        await update.message.reply_text(
            "Usage: <code>/removeadmin &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML
        )
        return

    if target_id in OWNER_IDS:
        await update.message.reply_text("❌ Owner ko remove nahi kar sakte.")
        return

    BOT_ADMINS.discard(target_id)
    if admins_coll is not None:
        admins_coll.delete_one({"_id": target_id})
    await update.message.reply_text(f"✅ <code>{target_id}</code> ab admin nahi raha.", parse_mode=ParseMode.HTML)


async def admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only_allowed(update):
        return

    lines = [f"{pe('👑')} <b>Owners</b>"]

    # ==========================
    # OWNERS
    # ==========================
    if not OWNER_IDS:
        lines.append("  (koi owner set nahi hai)")
    else:
        for uid in sorted(OWNER_IDS):
            lines.append(
                f'  • <a href="tg://user?id={uid}">Owner</a> '
                f'<code>({uid})</code>'
            )

    # Extra admins
    extra_admins = BOT_ADMINS - OWNER_IDS

    lines.append(f"\n{pe('🛡')} <b>Bot Admins</b>")

    if not extra_admins:
        lines.append("  (koi extra admin nahi hai)")
    else:
        for uid in sorted(extra_admins):

            # Default values
            username = None
            first_name = None
            last_name = None

            # ==========================
            # 1. MongoDB se saved details
            # ==========================
            if admins_coll is not None:
                admin_doc = admins_coll.find_one(
                    {"_id": uid}
                )

                if admin_doc:
                    username = admin_doc.get("username")
                    first_name = admin_doc.get("first_name")
                    last_name = admin_doc.get("last_name")

            # ==========================
            # 2. Alias fallback
            # ==========================
            if not username and uid in ADMIN_ALIASES:
                username = ADMIN_ALIASES[uid]

            # ==========================
            # 3. Display name banao
            # ==========================
            display_name = ""

            if first_name:
                display_name = first_name

                if last_name:
                    display_name += f" {last_name}"

            elif username:
                display_name = username.replace("_", " ").title()

            else:
                display_name = "Admin"

            # ==========================
            # Clickable display
            # ==========================

            if username:
                # Username hai -> clickable public Telegram link
                lines.append(
                    f'  • <a href="https://t.me/{esc(username)}">'
                    f'{esc(display_name)}</a> '
                    f'<code>({uid})</code>'
                )

            else:
                # Username nahi hai -> ID based clickable mention
                lines.append(
                    f'  • <a href="tg://user?id={uid}">'
                    f'{esc(display_name)}</a> '
                    f'<code>({uid})</code>'
                )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

# ===========================
# OWNER GROUP MODERATION PANEL
# ===========================

GROUP_ACCESS = [
    ("can_delete_messages", "🗑 Delete Messages"),
    ("can_restrict_members", "🔇 Mute / Unmute Users"),
    ("can_ban_members", "🚫 Ban / Unban Users"),
    ("can_invite_users", "➕ Invite Users"),
    ("can_pin_messages", "📌 Pin Messages"),
    ("can_manage_topics", "🧵 Manage Topics"),
    ("can_change_info", "✏️ Change Group Info"),
    ("can_manage_chat", "⚙️ Manage Chat"),
]




def group_access_panel_kb(group_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Permissions", callback_data=f"groupaccess:{group_id}")],
        [InlineKeyboardButton("🛡 Moderation Commands", callback_data=f"groupcontrol:{group_id}")],
        [InlineKeyboardButton("◀ Back to Groups", callback_data="groups:back")],
    ])










async def groups_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_owner(query.from_user.id):
        await query.answer("❌ Sirf Owner.", show_alert=True)
        return
    if groups_coll is None:
        await query.answer("MongoDB required.", show_alert=True)
        return
    docs = list(groups_coll.find({}).sort("title", 1))
    lines = [
        f"{pe('👑')} <b>Bot Groups</b>",
        "──────────────────",
        "🟢 Authorized = bot group commands work karenge",
        "🔴 Unauthorized = group commands blocked",
        "",
    ]
    for i, g in enumerate(docs, start=1):
        title = esc(g.get("title", f"Group {g['_id']}"))
        gid = g["_id"]
        status = "🟢 AUTHORIZED" if g.get("authorized") is True else "🔴 NOT AUTHORIZED"
        bot_status = esc(g.get("bot_status", "unknown"))
        lines.append(f"<b>{i}. {title}</b>\n   ID: <code>{gid}</code>\n   Status: {status}\n   Bot: <code>{bot_status}</code>")
        lines.append("")
    await query.answer()
    await query.edit_message_text(
        "\n".join(lines), parse_mode=ParseMode.HTML,
        reply_markup=groups_kb(), disable_web_page_preview=True,
    )


# ===========================
# OWNER-ONLY GROUP MODERATION COMMANDS
# ===========================

async def _owner_authorized_group(update, context, permission):
    if not update.effective_user or not is_owner(update.effective_user.id):
        return False
    if update.effective_chat.type not in ("group", "supergroup"):
        return False
    ok, reason = await group_control_allowed(update)
    if not ok:
        if reason and update.message:
            await update.message.reply_text(reason)
        return False
    perms, error = await get_bot_group_permissions(context, update.effective_chat.id)
    if error:
        if update.message:
            await update.message.reply_text(error)
        return False
    if not perms.get(permission, False):
        await update.message.reply_text("❌ Bot ke paas is action ki Telegram permission nahi hai.")
        return False
    return True


async def _resolve_moderation_user(update, context):
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        return update.message.reply_to_message.from_user
    if context.args:
        raw = context.args[0].lstrip("@")
        if raw.isdigit():
            try:
                return await context.bot.get_chat_member(update.effective_chat.id, int(raw))
            except Exception:
                return None
        try:
            member = await context.bot.get_chat_member(update.effective_chat.id, "@" + raw)
            return member.user
        except Exception:
            return None
    return None














# ===========================
# BOT CHAT MEMBER UPDATE
# ===========================

async def bot_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track when the bot is added/removed from a group.

    This is intentionally lightweight: group authorization is still controlled
    by the Owner through /groups, so simply adding the bot never grants access.
    """
    try:
        cmu = update.my_chat_member
        if not cmu:
            return

        chat = cmu.chat
        new_member = cmu.new_chat_member

        if chat.type not in ("group", "supergroup"):
            return

        if groups_coll is None:
            return

        is_present = new_member.status in ("member", "administrator", "creator")
        is_admin = new_member.status in ("administrator", "creator")

        groups_coll.update_one(
            {"_id": chat.id},
            {
                "$set": {
                    "title": chat.title or str(chat.id),
                    "username": getattr(chat, "username", None),
                    "bot_status": new_member.status,
                    "bot_is_admin": is_admin,
                    "bot_present": is_present,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                "$setOnInsert": {
                    "authorized": False,
                    "added_at": datetime.now(timezone.utc).isoformat(),
                },
            },
            upsert=True,
        )

        ensure_group_runtime(chat.id)

    except Exception as exc:
        log_event("GROUP", "bot membership update failed: %s", logging.ERROR, exc)

# ===========================
# OWNER GROUP CONTROL PANEL
# ===========================

GROUP_ACCESS = [
    ("can_delete_messages", "🗑 Delete Messages"),
    ("can_restrict_members", "🔇 Mute / Unmute"),
    ("can_ban_members", "🚫 Ban / Unban Users"),
    ("can_invite_users", "➕ Invite Users"),
    ("can_pin_messages", "📌 Pin Messages"),
    ("can_manage_topics", "🧵 Manage Topics"),
    ("can_change_info", "✏️ Change Group Info"),
]

DEFAULT_AUTOMOD = {
    "auto_ban": False,
    "auto_mute": False,
    "anti_spam": False,
    "anti_link": False,
    "spam_limit": 5,
    "spam_window": 10,
    "mute_minutes": 10,
}


def get_automod(gid):
    if automod_coll is None:
        return dict(DEFAULT_AUTOMOD)
    doc = automod_coll.find_one({"_id": gid})
    data = dict(DEFAULT_AUTOMOD)
    if doc:
        for k in data:
            if k in doc:
                data[k] = doc[k]
    return data


def save_automod(gid, data, owner_id):
    if automod_coll is None:
        return
    data = dict(data)
    data["_id"] = gid
    data["updated_by"] = owner_id
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    automod_coll.update_one({"_id": gid}, {"$set": data}, upsert=True)


def ensure_group_runtime(gid):
    if automod_coll is not None and automod_coll.find_one({"_id": gid}) is None:
        automod_coll.insert_one({"_id": gid, **DEFAULT_AUTOMOD})


async def get_bot_group_permissions(context, chat_id):
    """Fetch the bot's current Telegram permissions for a group.

    Telegram is the source of truth here; MongoDB only stores the group registry.
    Errors are returned with the real Telegram exception so the owner can diagnose
    missing membership/admin rights instead of receiving a generic failure.
    """
    try:
        me = await context.bot.get_me()
        log_event(
            "ACCESS",
            "permission check | group=%s | bot=%s",
            logging.INFO,
            chat_id,
            me.id,
        )

        member = await context.bot.get_chat_member(chat_id=chat_id, user_id=me.id)

    except Forbidden as exc:
        log_event(
            "ACCESS",
            "Telegram Forbidden | group=%s | %s",
            logging.ERROR,
            chat_id,
            exc,
        )
        return None, "❌ Telegram ne permission check deny kar diya. Bot ko group me Admin rakho."

    except BadRequest as exc:
        log_event(
            "ACCESS",
            "Telegram BadRequest | group=%s | %s",
            logging.ERROR,
            chat_id,
            exc,
        )
        return None, f"❌ Telegram error: {exc}"

    except TelegramError as exc:
        log_event(
            "ACCESS",
            "TelegramError | group=%s | %s",
            logging.ERROR,
            chat_id,
            exc,
        )
        return None, f"❌ Telegram error: {exc}"

    except Exception as exc:
        log_event(
            "ACCESS",
            "unexpected permission error | group=%s | %r",
            logging.ERROR,
            chat_id,
            exc,
        )
        return None, "❌ Permission check failed. Render logs me exact error dekho."

    status = getattr(member, "status", None)
    log_event(
        "ACCESS",
        "Telegram member status | group=%s | status=%s",
        logging.INFO,
        chat_id,
        status,
    )

    if status not in ("administrator", "creator"):
        return None, f"❌ Bot is group me Admin nahi hai. Current status: {status}"

    permissions = {"status": status}
    for key, _label in GROUP_ACCESS:  # keep one canonical permission list
        permissions[key] = bool(getattr(member, key, False))

    return permissions, None


def group_access_kb(gid):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=f"groupaccess:{gid}"),
            InlineKeyboardButton("⚙️ Controls", callback_data=f"groupcontrol:{gid}"),
        ],
        [InlineKeyboardButton("🤖 Auto-Mod", callback_data=f"automod:{gid}")],
        [InlineKeyboardButton("◀ Groups", callback_data="groups:back")],
    ])


def group_control_kb(gid):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚫 Ban User", callback_data=f"modhelp:ban:{gid}"),
            InlineKeyboardButton("🔇 Mute User", callback_data=f"modhelp:mute:{gid}"),
        ],
        [
            InlineKeyboardButton("♻️ Unban User", callback_data=f"modhelp:unban:{gid}"),
            InlineKeyboardButton("🔊 Unmute User", callback_data=f"modhelp:unmute:{gid}"),
        ],
        [InlineKeyboardButton("🗑 Delete Message", callback_data=f"modhelp:del:{gid}")],
        [
            InlineKeyboardButton("🤖 Auto-Mod", callback_data=f"automod:{gid}"),
            InlineKeyboardButton("🛡 Permissions", callback_data=f"groupaccess:{gid}"),
        ],
        [InlineKeyboardButton("◀ Groups", callback_data="groups:back")],
    ])


def automod_kb(gid, data):
    def mark(key):
        return "🟢" if data.get(key) else "🔴"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{mark('auto_ban')} Auto-Ban", callback_data=f"automodtoggle:auto_ban:{gid}")],
        [InlineKeyboardButton(f"{mark('auto_mute')} Auto-Mute", callback_data=f"automodtoggle:auto_mute:{gid}")],
        [InlineKeyboardButton(f"{mark('anti_spam')} Anti-Spam", callback_data=f"automodtoggle:anti_spam:{gid}")],
        [InlineKeyboardButton(f"{mark('anti_link')} Anti-Link", callback_data=f"automodtoggle:anti_link:{gid}")],
        [
            InlineKeyboardButton("➖ Spam Limit", callback_data=f"automodnum:spam_limit:-1:{gid}"),
            InlineKeyboardButton(str(data.get("spam_limit", 5)), callback_data=f"automodnum:spam_limit:0:{gid}"),
            InlineKeyboardButton("➕", callback_data=f"automodnum:spam_limit:1:{gid}"),
        ],
        [
            InlineKeyboardButton("➖ Window", callback_data=f"automodnum:spam_window:-1:{gid}"),
            InlineKeyboardButton(f"{data.get('spam_window', 10)}s", callback_data=f"automodnum:spam_window:0:{gid}"),
            InlineKeyboardButton("➕", callback_data=f"automodnum:spam_window:1:{gid}"),
        ],
        [
            InlineKeyboardButton("➖ Mute", callback_data=f"automodnum:mute_minutes:-1:{gid}"),
            InlineKeyboardButton(f"{data.get('mute_minutes', 10)}m", callback_data=f"automodnum:mute_minutes:0:{gid}"),
            InlineKeyboardButton("➕", callback_data=f"automodnum:mute_minutes:1:{gid}"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=f"automod:{gid}"),
            InlineKeyboardButton("◀ Controls", callback_data=f"groupcontrol:{gid}"),
        ],
    ])


def automod_text(group, data):
    return (
        f"{pe('🤖')} <b>AUTO-MODERATION</b>\n"
        "──────────────────\n"
        f"<b>Group:</b> {esc(group.get('title', 'Group'))}\n"
        f"<b>ID:</b> <code>{group.get('_id')}</code>\n\n"
        f"{'🟢' if data.get('auto_ban') else '🔴'} Auto-Ban\n"
        f"{'🟢' if data.get('auto_mute') else '🔴'} Auto-Mute\n"
        f"{'🟢' if data.get('anti_spam') else '🔴'} Anti-Spam\n"
        f"{'🟢' if data.get('anti_link') else '🔴'} Anti-Link\n\n"
        f"Spam: <b>{data.get('spam_limit', 5)} messages / "
        f"{data.get('spam_window', 10)} sec</b>\n"
        f"Mute: <b>{data.get('mute_minutes', 10)} min</b>\n\n"
        "⚠️ Admins, Owner aur bots ko Auto-Mod ignore karega."
    )


def group_access_text(group, permissions):
    lines = [
        f"{pe('🛡')} <b>BOT ACCESS PANEL</b>",
        "──────────────────",
        f"<b>Group:</b> {esc(group.get('title', 'Group'))}",
        f"<b>ID:</b> <code>{group.get('_id')}</code>",
        "",
    ]
    if not permissions:
        lines.append("🔴 Permission data unavailable.")
        return "\n".join(lines)

    lines.append(f"<b>Bot status:</b> <code>{permissions.get('status')}</code>")
    lines.append("")
    for key, label in GROUP_ACCESS:
        lines.append(f"{'🟢' if permissions.get(key) else '🔴'} {label}")
    lines += ["", "🟢 Available    🔴 Missing"]
    return "\n".join(lines)


async def group_access_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_event("COMMAND", "/groupaccess | user=%s", logging.INFO, update.effective_user.id)
    if update.effective_chat.type != "private" or not is_owner(update.effective_user.id):
        return
    if groups_coll is None:
        await update.message.reply_text("❌ MongoDB required.")
        return
    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /groupaccess <group_id>")
        return

    gid = int(context.args[0])
    group = groups_coll.find_one({"_id": gid})
    if not group:
        await update.message.reply_text("❌ Group record nahi mila.")
        return

    permissions, error = await get_bot_group_permissions(context, gid)
    if error:
        await update.message.reply_text(error)
        return

    await update.message.reply_text(
        group_access_text(group, permissions),
        parse_mode=ParseMode.HTML,
        reply_markup=group_access_kb(gid),
    )


async def group_access_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only Access button.

    Follows PTB's documented callback pattern: answer the callback first, then
    perform the Telegram/Mongo work and edit the message. Every failure is logged
    with enough context to diagnose it from Render logs.
    """
    query = update.callback_query
    if query is None:
        return

    data = query.data or ""
    user_id = query.from_user.id if query.from_user else None

    log_event(
        "CALLBACK",
        "received | data=%s | user=%s | chat=%s",
        logging.INFO,
        data,
        user_id,
        getattr(getattr(query, "message", None), "chat_id", None),
    )

    # Telegram expects every callback query to be answered. Do this immediately.
    try:
        await query.answer()
    except TelegramError as exc:
        log_event("CALLBACK", "answer failed | %s", logging.WARNING, exc)
        # Continue: an expired callback should not prevent diagnostics.

    try:
        if not query.message:
            log_event("CALLBACK", "missing callback message", logging.ERROR)
            return

        if query.message.chat.type != "private":
            await query.answer(
                "Owner panel private chat me hai.",
                show_alert=True,
            )
            return

        if not is_owner(user_id):
            await query.answer("❌ Sirf Owner.", show_alert=True)
            return

        if not data.startswith("groupaccess:"):
            log_event("CALLBACK", "invalid Access data=%s", logging.ERROR, data)
            await query.answer("❌ Invalid Access callback.", show_alert=True)
            return

        try:
            gid = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            log_event("CALLBACK", "invalid group id | data=%s", logging.ERROR, data)
            await query.answer("❌ Invalid group ID.", show_alert=True)
            return

        if groups_coll is None:
            await query.answer("❌ MongoDB unavailable.", show_alert=True)
            return

        group = groups_coll.find_one({"_id": gid})
        if not group:
            log_event("ACCESS", "group not found in Mongo | group=%s", logging.ERROR, gid)
            await query.answer("❌ Group record nahi mila.", show_alert=True)
            return

        permissions, error = await get_bot_group_permissions(context, gid)
        if error:
            log_event(
                "ACCESS",
                "permission check failed | group=%s | %s",
                logging.ERROR,
                gid,
                error,
            )
            await query.answer(error, show_alert=True)
            return

        text = group_access_text(group, permissions)
        markup = group_access_kb(gid)

        log_event("ACCESS", "rendering panel | group=%s", logging.INFO, gid)

        try:
            await query.edit_message_text(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        except BadRequest as exc:
            # Most useful case: message was already edited / markup is invalid.
            log_event(
                "ACCESS",
                "edit_message_text BadRequest | group=%s | %s",
                logging.ERROR,
                gid,
                exc,
            )
            await query.answer(f"❌ Telegram: {exc}", show_alert=True)
            return

        log_event("ACCESS", "panel opened successfully | group=%s", logging.INFO, gid)

    except Exception as exc:
        logger.exception(
            "[ACCESS  ] callback crashed | user=%s | data=%s | error=%r",
            user_id,
            data,
            exc,
        )
        try:
            await query.answer(
                "❌ Access panel error. Render logs me exact reason check karo.",
                show_alert=True,
            )
        except Exception:
            pass


async def group_control_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query.message or query.message.chat.type != "private":
        await query.answer("Owner panel private chat me hai.", show_alert=True)
        return
    if not is_owner(query.from_user.id):
        await query.answer("❌ Sirf Owner.", show_alert=True)
        return

    gid = int(query.data.split(":", 1)[1])
    group = groups_coll.find_one({"_id": gid}) if groups_coll else None
    if not group:
        await query.answer("Group record nahi mila.", show_alert=True)
        return

    await query.answer()
    await query.edit_message_text(
        f"{pe('⚙️')} <b>GROUP CONTROL</b>\n"
        "──────────────────\n"
        f"<b>{esc(group.get('title', 'Group'))}</b>\n\n"
        "Owner-only moderation controls.",
        parse_mode=ParseMode.HTML,
        reply_markup=group_control_kb(gid),
    )


async def modhelp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_owner(query.from_user.id):
        await query.answer("❌ Sirf Owner.", show_alert=True)
        return

    _, action, _gid = query.data.split(":", 2)
    command_map = {
        "ban": "/ban — target user ke message par reply karo",
        "unban": "/unban <user_id>",
        "mute": "/mute <minutes> — target message par reply karo",
        "unmute": "/unmute — target message par reply karo",
        "del": "/del — target message par reply karo",
    }
    await query.answer(command_map[action], show_alert=True)


async def automod_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_owner(query.from_user.id):
        await query.answer("❌ Sirf Owner.", show_alert=True)
        return

    gid = int(query.data.split(":", 1)[1])
    group = groups_coll.find_one({"_id": gid}) if groups_coll else None
    if not group:
        await query.answer("Group record nahi mila.", show_alert=True)
        return

    ensure_group_runtime(gid)
    data = get_automod(gid)
    await query.answer()
    await query.edit_message_text(
        automod_text(group, data),
        parse_mode=ParseMode.HTML,
        reply_markup=automod_kb(gid, data),
    )


async def automod_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_owner(query.from_user.id):
        await query.answer("❌ Sirf Owner.", show_alert=True)
        return

    _, key, gid_text = query.data.split(":", 2)
    gid = int(gid_text)
    data = get_automod(gid)
    data[key] = not bool(data.get(key))
    save_automod(gid, data, query.from_user.id)

    group = groups_coll.find_one({"_id": gid})
    await query.answer("Updated.")
    await query.edit_message_text(
        automod_text(group, data),
        parse_mode=ParseMode.HTML,
        reply_markup=automod_kb(gid, data),
    )


async def automod_num_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_owner(query.from_user.id):
        await query.answer("❌ Sirf Owner.", show_alert=True)
        return

    _, key, delta_text, gid_text = query.data.split(":", 3)
    gid = int(gid_text)
    delta = int(delta_text)

    data = get_automod(gid)
    if delta:
        limits = {
            "spam_limit": (1, 30),
            "spam_window": (3, 60),
            "mute_minutes": (1, 1440),
        }
        lo, hi = limits[key]
        data[key] = max(lo, min(hi, int(data.get(key, DEFAULT_AUTOMOD[key])) + delta))
        save_automod(gid, data, query.from_user.id)

    group = groups_coll.find_one({"_id": gid})
    await query.answer()
    await query.edit_message_text(
        automod_text(group, data),
        parse_mode=ParseMode.HTML,
        reply_markup=automod_kb(gid, data),
    )



# ===========================
# /stats ROUTER
# ===========================

async def mystatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exact behavior:
    - /stats => command sender's stats
    - reply + /stats => replied user's stats
    - group target by username/user_id is intentionally not used
    """
    if not update.message:
        return

    chat = update.effective_chat
    command_user = update.effective_user

    if chat.type in ("group", "supergroup"):
        ok, reason = await group_control_allowed(update)
        if not ok:
            await update.message.reply_text(reason)
            return

    # If /stats is a reply, show the replied user's stats.
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target = update.message.reply_to_message.from_user
    else:
        # Plain /stats always means the command sender.
        target = command_user

    # Preserve the existing stats renderer.
    context.user_data["_stats_target_user"] = target

    class _StatsProxy:
        def __init__(self, original, target_user):
            self._original = original
            self.effective_user = target_user
            self.effective_chat = original.effective_chat
            self.message = original.message
            self.callback_query = original.callback_query

    try:
        await _legacy_mystatus_cmd(_StatsProxy(update, target), context)
    finally:
        context.user_data.pop("_stats_target_user", None)


# ===========================
# /groups — OWNER ONLY
# ===========================

def groups_kb():
    rows = []
    if groups_coll is None:
        return InlineKeyboardMarkup(rows)

    docs = list(groups_coll.find({}).sort("title", 1))
    for g in docs:
        gid = g["_id"]
        authorized = g.get("authorized") is True
        title = g.get("title", f"Group {gid}")
        label = "🟢" if authorized else "🔴"
        action = "groupauth:off:" if authorized else "groupauth:on:"
        rows.append([
            InlineKeyboardButton(
                f"{label} {title[:28]}",
                callback_data=f"{action}{gid}",
            ),
            InlineKeyboardButton("🛡 Access", callback_data=f"groupaccess:{gid}"),
        ])
    return InlineKeyboardMarkup(rows)


async def groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_event("COMMAND", "/groups | user=%s", logging.INFO, update.effective_user.id)
    if update.effective_chat.type != "private" or not is_owner(update.effective_user.id):
        return

    if groups_coll is None:
        await update.message.reply_text("❌ MongoDB required for /groups.")
        return

    docs = list(groups_coll.find({}).sort("title", 1))
    if not docs:
        await update.message.reply_text(
            "📭 Abhi bot ka koi group record nahi hai.\n\n"
            "Bot ko kisi group me add karne ke baad /groups dobara check karo."
        )
        return

    lines = [
        f"{pe('👑')} <b>Bot Groups</b>",
        "──────────────────",
        "🟢 Authorized = bot group commands work karenge",
        "🔴 Unauthorized = /add /close /stats target etc. blocked",
        "",
    ]

    for i, g in enumerate(docs, start=1):
        title = esc(g.get("title", f"Group {g['_id']}"))
        gid = g["_id"]
        status = "🟢 AUTHORIZED" if g.get("authorized") is True else "🔴 NOT AUTHORIZED"
        bot_status = esc(g.get("bot_status", "unknown"))
        username = g.get("username")
        public = f" @{esc(username)}" if username else ""
        lines.append(
            f"<b>{i}. {title}</b>{public}\n"
            f"   ID: <code>{gid}</code>\n"
            f"   Status: {status}\n"
            f"   Bot: <code>{bot_status}</code>"
        )
        lines.append("")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=groups_kb(),
        disable_web_page_preview=True,
    )


async def group_auth_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not query.message or query.message.chat.type != "private":
        await query.answer("Owner private chat me hi group control kar sakta hai.", show_alert=True)
        return

    if not is_owner(query.from_user.id):
        await query.answer("❌ Sirf Owner.", show_alert=True)
        return

    if groups_coll is None:
        await query.answer("MongoDB required.", show_alert=True)
        return

    try:
        _, action, gid_text = query.data.split(":", 2)
        gid = int(gid_text)
    except Exception:
        await query.answer("Invalid group.", show_alert=True)
        return

    group = groups_coll.find_one({"_id": gid})
    if not group:
        await query.answer("Group record nahi mila.", show_alert=True)
        return

    authorized = action == "on"
    log_event("GROUP", "authorization change | group=%s | authorized=%s | by=%s", logging.INFO, gid, authorized, query.from_user.id)
    groups_coll.update_one(
        {"_id": gid},
        {"$set": {
            "authorized": authorized,
            "authorized_by": query.from_user.id,
            "authorized_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "reauthorized_required": False,
        }},
    )

    await query.answer(
        "✅ Group authorized." if authorized else "🔴 Group unauthorized.",
        show_alert=False,
    )

    # /groups screen refresh
    docs = list(groups_coll.find({}).sort("title", 1))
    lines = [
        f"{pe('👑')} <b>Bot Groups</b>",
        "──────────────────",
        "🟢 Authorized = bot group commands work karenge",
        "🔴 Unauthorized = group commands blocked",
        "",
    ]

    for i, g in enumerate(docs, start=1):
        title = esc(g.get("title", f"Group {g['_id']}"))
        gid2 = g["_id"]
        status = "🟢 AUTHORIZED" if g.get("authorized") is True else "🔴 NOT AUTHORIZED"
        bot_status = esc(g.get("bot_status", "unknown"))
        username = g.get("username")
        public = f" @{esc(username)}" if username else ""
        lines.append(
            f"<b>{i}. {title}</b>{public}\n"
            f"   ID: <code>{gid2}</code>\n"
            f"   Status: {status}\n"
            f"   Bot: <code>{bot_status}</code>"
        )
        lines.append("")

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=groups_kb(),
        disable_web_page_preview=True,
    )



# ===========================
# OWNER GROUP MODERATION
# ===========================

async def owner_group_mod_allowed(update, context, permission):
    if not is_owner(update.effective_user.id):
        return False, "❌ Sirf Owner."
    if update.effective_chat.type not in ("group", "supergroup"):
        return False, "❌ Ye command group me use karo."

    ok, reason = await group_control_allowed(update)
    if not ok:
        return False, reason

    try:
        bot_member = await context.bot.get_chat_member(
            update.effective_chat.id, context.bot.id
        )
    except Exception:
        return False, "❌ Bot permission check fail hua."

    if bot_member.status != "administrator" or not getattr(bot_member, permission, False):
        return False, "❌ Bot ke paas required Telegram permission nahi hai."

    return True, None


async def ban_cmd(update, context):
    ok, reason = await owner_group_mod_allowed(update, context, "can_ban_members")
    if not ok:
        await update.message.reply_text(reason)
        return

    target = update.message.reply_to_message.from_user if update.message.reply_to_message else None
    if not target:
        await update.message.reply_text("❌ User ke message par reply karke /ban karo.")
        return
    if target.id == context.bot.id or target.is_bot:
        await update.message.reply_text("❌ Bot ko ban nahi kar raha.")
        return

    await context.bot.ban_chat_member(update.effective_chat.id, target.id)
    await update.message.reply_text(
        f"🚫 <b>{esc(target.first_name)}</b> banned.",
        parse_mode=ParseMode.HTML,
    )


async def unban_cmd(update, context):
    ok, reason = await owner_group_mod_allowed(update, context, "can_ban_members")
    if not ok:
        await update.message.reply_text(reason)
        return

    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /unban <user_id>")
        return

    uid = int(context.args[0])
    await context.bot.unban_chat_member(
        update.effective_chat.id, uid, only_if_banned=True
    )
    await update.message.reply_text(f"♻️ <code>{uid}</code> unbanned.", parse_mode=ParseMode.HTML)


async def mute_cmd(update, context):
    ok, reason = await owner_group_mod_allowed(update, context, "can_restrict_members")
    if not ok:
        await update.message.reply_text(reason)
        return

    target = update.message.reply_to_message.from_user if update.message.reply_to_message else None
    if not target:
        await update.message.reply_text("❌ User ke message par reply karke /mute <minutes> karo.")
        return

    minutes = 10
    if context.args and context.args[0].isdigit():
        minutes = max(1, min(int(context.args[0]), 1440))

    until_date = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    await context.bot.restrict_chat_member(
        update.effective_chat.id,
        target.id,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=until_date,
    )
    await update.message.reply_text(
        f"🔇 <b>{esc(target.first_name)}</b> muted for {minutes} min.",
        parse_mode=ParseMode.HTML,
    )


async def unmute_cmd(update, context):
    ok, reason = await owner_group_mod_allowed(update, context, "can_restrict_members")
    if not ok:
        await update.message.reply_text(reason)
        return

    target = update.message.reply_to_message.from_user if update.message.reply_to_message else None
    if not target:
        await update.message.reply_text("❌ User ke message par reply karke /unmute karo.")
        return

    await context.bot.restrict_chat_member(
        update.effective_chat.id,
        target.id,
        permissions=ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
        ),
    )
    await update.message.reply_text(
        f"🔊 <b>{esc(target.first_name)}</b> unmuted.",
        parse_mode=ParseMode.HTML,
    )


async def del_cmd(update, context):
    ok, reason = await owner_group_mod_allowed(update, context, "can_delete_messages")
    if not ok:
        await update.message.reply_text(reason)
        return

    target_message = update.message.reply_to_message
    if not target_message:
        await update.message.reply_text("❌ Message par reply karke /del karo.")
        return

    await context.bot.delete_message(update.effective_chat.id, target_message.message_id)
    await context.bot.delete_message(update.effective_chat.id, update.message.message_id)


# Per-group in-memory spam counters.
AUTOMOD_STATE = {}


def has_link(text):
    if not text:
        return False
    return bool(re.search(r"(https?://|www\.|t\.me/|telegram\.me/)", text, re.I))


async def auto_moderate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_chat.type not in ("group", "supergroup"):
        return

    chat = update.effective_chat
    user = update.effective_user
    if not user or user.is_bot:
        return

    if groups_coll is None or not group_is_authorized(chat.id):
        return

    data = get_automod(chat.id)
    if not any(data.get(k) for k in ("auto_ban", "auto_mute", "anti_spam", "anti_link")):
        return

    # Never auto-moderate Telegram admins/creator.
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status in ("administrator", "creator"):
            return
    except Exception:
        return

    text = update.message.text or update.message.caption or ""

    # Anti-link.
    if data.get("anti_link") and has_link(text):
        try:
            if data.get("can_delete_messages", True):
                await context.bot.delete_message(chat.id, update.message.message_id)
        except Exception:
            pass

        if data.get("auto_ban"):
            try:
                await context.bot.ban_chat_member(chat.id, user.id)
                return
            except Exception:
                pass

        if data.get("auto_mute"):
            try:
                await context.bot.restrict_chat_member(
                    chat.id, user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=datetime.now(timezone.utc) + timedelta(
                        minutes=int(data.get("mute_minutes", 10))
                    ),
                )
            except Exception:
                pass
        return

    # Anti-spam.
    if data.get("anti_spam"):
        now = time.monotonic()
        key = (chat.id, user.id)
        bucket = AUTOMOD_STATE.setdefault(key, [])
        window = int(data.get("spam_window", 10))
        bucket[:] = [t for t in bucket if now - t <= window]
        bucket.append(now)

        if len(bucket) >= int(data.get("spam_limit", 5)):
            bucket.clear()

            if data.get("auto_ban"):
                try:
                    await context.bot.ban_chat_member(chat.id, user.id)
                    return
                except Exception:
                    pass

            if data.get("auto_mute"):
                try:
                    await context.bot.restrict_chat_member(
                        chat.id, user.id,
                        permissions=ChatPermissions(can_send_messages=False),
                        until_date=datetime.now(timezone.utc) + timedelta(
                            minutes=int(data.get("mute_minutes", 10))
                        ),
                    )
                except Exception:
                    pass


# ===========================
# /help — admin/owner ko sab commands, normal user ko sirf user commands
# ===========================

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lines = [
        f"{pe('📖')} <b>Commands</b>",
        "──────────────────",
        "<b>👤 User Commands</b>",
        "/start — Dashboard kholo (private chat)",
        "/stats — Apna deal status dekho (private ya group, kahin bhi)",
        "/help — Ye list dikhata hai",
    ]

    if is_admin(uid):
        lines += [
            "",
            "<b>🛡 Admin Commands</b> (private chat me hi kaam karenge)",
            "/add — Deal create karo (deal message pe reply karke)",
            "/close — Deal complete karo (deal message pe reply karke)",
            "/alldeals — Saari deals ki poori list",
            "/leaderboard — Today + All-time top dealer/earner",
            "/deal &lt;DL-SPIDER-N&gt; — Kisi bhi deal ki full detail dekho",
            "/admins — Bot admins ki list dekho",
            "/groups — Bot kin groups me added hai + authorization control",
            "/groupaccess &lt;group_id&gt; — Bot ke Telegram permissions dekho",
            "/broadcast &lt;message&gt; — Private subscribers ko broadcast",
            "/ban /unban /mute /unmute /del — Owner-only group moderation",
        ]

    if is_owner(uid):
        lines += [
            "",
            "<b>👑 Owner Commands</b>",
            "/addadmin — Reply karke (ya ID de ke) naya bot admin banao",
            "/removeadmin — Reply karke (ya ID de ke) admin hatao",
        ]

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ===========================
# Keep-alive server (Render port check ke liye)
# ===========================

def start_dummy_server():
    port = int(os.getenv("PORT", "10000"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Spider Escrow</title>

    <style>
        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
            font-family: Arial, Helvetica, sans-serif;
            color: #ffffff;
            background:
                radial-gradient(circle at top, #20243a 0%, #0d0f17 45%, #07080d 100%);
        }

        .card {
            width: 100%;
            max-width: 430px;
            padding: 38px 28px;
            text-align: center;
            border-radius: 24px;
            background: rgba(20, 22, 34, 0.82);
            border: 1px solid rgba(255, 255, 255, 0.08);
            box-shadow:
                0 20px 60px rgba(0, 0, 0, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.05);
            backdrop-filter: blur(15px);
        }

        .logo {
            width: 70px;
            height: 70px;
            margin: 0 auto 20px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 20px;
            font-size: 32px;
            background: linear-gradient(135deg, #7c3aed, #4f46e5);
            box-shadow: 0 12px 30px rgba(99, 102, 241, 0.35);
        }

        h1 {
            margin: 0;
            font-size: 27px;
            letter-spacing: -0.6px;
        }

        .status {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            margin: 14px 0 10px;
            padding: 8px 14px;
            border-radius: 999px;
            font-size: 13px;
            color: #b9fbc0;
            background: rgba(34, 197, 94, 0.10);
            border: 1px solid rgba(34, 197, 94, 0.20);
        }

        .dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #22c55e;
            box-shadow: 0 0 12px #22c55e;
        }

        p {
            margin: 10px 0 24px;
            color: #9699a8;
            font-size: 14px;
            line-height: 1.5;
        }

        .telegram {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 9px;
            width: 100%;
            padding: 13px 20px;
            border-radius: 13px;
            color: #ffffff;
            text-decoration: none;
            font-size: 14px;
            font-weight: 600;
            background: linear-gradient(135deg, #229ed9, #168acd);
            box-shadow: 0 10px 25px rgba(34, 158, 217, 0.22);
            transition: 0.2s ease;
        }

        .telegram:hover {
            transform: translateY(-2px);
            box-shadow: 0 14px 30px rgba(34, 158, 217, 0.30);
        }

        .footer {
            margin-top: 22px;
            color: #666978;
            font-size: 12px;
        }
    </style>
</head>

<body>
    <div class="card">
        <div class="logo">&#128蜘蛛;</div>

        <h1>Spider Escrow</h1>

        <div class="status">
            <span class="dot"></span>
            Service Online
        </div>

        <p>
            Secure escrow service is online and ready.
        </p>

        <a
            class="telegram"
            href="https://t.me/SPIDERXESCROWSERVICE"
            target="_blank"
        >
            &#9992; @SPIDERXESCROWSERVICE
        </a>

        <div class="footer">
            Spider Escrow Service
        </div>
    </div>
</body>
</html>"""

            body = html.encode("utf-8")

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8"
            )
            self.send_header(
                "Content-Length",
                str(len(body))
            )
            self.end_headers()

            self.wfile.write(body)

        def do_HEAD(self):
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8"
            )
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(
        ("0.0.0.0", port),
        Handler
    )

    threading.Thread(
        target=server.serve_forever,
        daemon=True
    ).start()

    log_event(
        "HTTP",
        "health server listening on port %d",
        logging.INFO,
        port
    )
    
# ===========================
# GLOBAL ERROR HANDLER
# ===========================

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    logger.error(
        "[ERROR] unhandled exception | update=%s | error=%s",
        type(update).__name__,
        error,
        exc_info=(type(error), error, error.__traceback__) if error else None,
    )

    query = getattr(update, "callback_query", None)
    if query:
        try:
            await query.answer(
                "❌ Internal error. Render logs me exact error check karo.",
                show_alert=True,
            )
        except Exception:
            pass


# ===========================
# Main
# ===========================

def main():
    log_event("BOOT", "initializing | mongo=%s | owners=%d", logging.INFO, bool(mongo_client), len(OWNER_IDS))
    start_dummy_server()

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    if not BOT_TOKEN:
        raise RuntimeError("SPIDER_BOT_TOKEN is missing from Render environment variables")

    app = Application.builder().token(BOT_TOKEN).build()
    log_event("BOOT", "python-telegram-bot application created")

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("stats", mystatus_cmd))
    app.add_handler(CommandHandler("groups", groups_cmd))
    app.add_handler(CommandHandler("groupaccess", group_access_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("close", close))
    app.add_handler(CommandHandler("hold", hold_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("alldeals", alldeals_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(CommandHandler("deal", deal_lookup_cmd))
    app.add_handler(CommandHandler("addadmin", addadmin_cmd))
    app.add_handler(CommandHandler("removeadmin", removeadmin_cmd))
    app.add_handler(CommandHandler("admins", admins_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

    # Telegram bot add/remove/promote events -> group registry.
    app.add_handler(
        ChatMemberHandler(bot_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER)
    )

    # Owner's /groups authorize/revoke buttons.
    app.add_handler(
        CallbackQueryHandler(group_auth_callback, pattern=r"^groupauth:(on|off):"), group=-10
    )
    # Existing callbacks remain unchanged.
    app.add_handler(MessageHandler(filters.ALL, auto_moderate_message), group=1)

    # Owner group-control callbacks MUST be registered before the generic router.
    app.add_handler(
        CallbackQueryHandler(group_access_callback, pattern=r"^groupaccess:-?\d+$"), group=-10
    )
    app.add_handler(
        CallbackQueryHandler(group_control_callback, pattern=r"^groupcontrol:-?\d+$"), group=-10
    )
    app.add_handler(
        CallbackQueryHandler(automod_callback, pattern=r"^automod:-?\d+$"), group=-10
    )
    app.add_handler(
        CallbackQueryHandler(
            automod_toggle_callback,
            pattern=r"^automodtoggle:(auto_ban|auto_mute|anti_spam|anti_link):-?\d+$",
        ), group=-10
    )
    app.add_handler(
        CallbackQueryHandler(
            automod_num_callback,
            pattern=r"^automodnum:(spam_limit|spam_window|mute_minutes):-?\d+:-?\d+$",
        ), group=-10
    )
    app.add_handler(
        CallbackQueryHandler(
            modhelp_callback,
            pattern=r"^modhelp:(ban|mute|unban|unmute|del):-?\d+$",
        ), group=-10
    )
    app.add_handler(
        CallbackQueryHandler(groups_back_callback, pattern=r"^groups:back$"), group=-10
    )

    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_error_handler(global_error_handler)

    log_event("BOOT", "RizzlerXEscrow Bot starting polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
