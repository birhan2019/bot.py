
"""
HabeshaBingo - Telegram bot + Mini App backend (one process).
 
Run:
    pip install -r requirements.txt
    BOT_TOKEN=123:ABC ADMIN_IDS=111,222 WEBAPP_URL=https://your-domain.com python bot.py
 
The mini app (webapp/index.html, style.css, app.js) is served by this same process
on PORT (default 8080). Telegram needs WEBAPP_URL to be a public HTTPS address
(use a reverse proxy such as Caddy/nginx, or a tunnel such as cloudflared while testing).
 
Rules: 600 cards (1-600), 5x5, free centre, max 2 cards per player per round.
Winning patterns: any row, column, diagonal, four corners, or full card.
Prize = pool - house commission (default 10%, /setcut to change), split between winning cards.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl
 
from aiohttp import web
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
    BotCommand,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("habesha")
 
# ------------------------------------------------------------------ config
TOKEN = "8271535831:AAGUQ69TsTmyqMzM7kpBzYI1fhVW5Prx4YQ"
ADMIN_IDS = {12345678}  # 12345678 የሚለውን በእርስዎ ትክክለኛ የቴሌግራም ID ይቀይሩት
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://birhan2019.github.io/habeshabingo/").rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", "habesha.db")
CARD_SALT = os.environ.get("CARD_SALT", "habesha")
# DEV_MODE=1 lets you test the mini app in a normal browser with ?dev_uid=123.
# NEVER enable it in production: anyone could impersonate any user.
DEV_MODE = os.environ.get("DEV_MODE") == "1"
WEBAPP_DIR = Path(__file__).parent / "webapp"
 
TOTAL_CARDS = 600
MAX_CARDS = 2           # cards per player per round
BUY_SECONDS = 45        # card buying window
MAX_EXTENSIONS = 2      # extra buying windows if fewer than MIN_PLAYERS joined
MIN_PLAYERS = 2
CALL_INTERVAL = 4       # seconds between called numbers
RESULT_SECONDS = 10
CANCEL_SECONDS = 6
MIN_DEPOSIT = 1000      # cents -> 10 ETB
MIN_WITHDRAW = 5000     # cents -> 50 ETB
 
# (room id, button label, price in cents)
ROOM_DEFS = [
    ("play", "🎮 PLAY", 1000),
    ("super", "👑 SuperBingo", 5000),
]
 
DEFAULTS = {
    "cut": "10",
    "acct_cbe": "1000XXXXXXXXX",
    "acct_tele": "000000",
    "support": "@YourSupport",
}
 
# ------------------------------------------------------------------ database
db = sqlite3.connect(DB_PATH, isolation_level=None)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, name TEXT, phone TEXT,
    balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
    created INTEGER);
CREATE TABLE IF NOT EXISTS tx(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT,
    amount INTEGER, status TEXT, info TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sms(hash TEXT PRIMARY KEY, tx_id INTEGER);
CREATE TABLE IF NOT EXISTS rounds(
    id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT, status TEXT,
    price INTEGER, pool INTEGER DEFAULT 0, commission INTEGER DEFAULT 0, ts INTEGER);
CREATE TABLE IF NOT EXISTS entries(
    round_id INTEGER, user_id INTEGER, card_no INTEGER);
"""
)
 
 
def get_setting(key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULTS[key]
 
 
def set_setting(key, value):
    db.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
 
 
def ensure_user(user):
    db.execute(
        "INSERT INTO users(id,name,created) VALUES(?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
        (user.id, user.first_name or str(user.id), int(time.time())),
    )
 
 
def reg_user(uid):
    return db.execute(
        "SELECT * FROM users WHERE id=? AND phone IS NOT NULL", (uid,)
    ).fetchone()
 
 
def balance(uid):
    row = db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()
    return row["balance"] if row else 0
 
 
def user_name(uid):
    row = db.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
    return row["name"] if row else str(uid)
 
 
def move(uid, delta, kind, info="", status="done"):
    """Atomically change a balance and log it. Returns tx id or None if funds are insufficient."""
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE users SET balance = balance + ? WHERE id=?", (delta, uid))
        txid = db.execute(
            "INSERT INTO tx(user_id,kind,amount,status,info,ts) VALUES(?,?,?,?,?,?)",
            (uid, kind, abs(delta), status, info, int(time.time())),
        ).lastrowid
        db.execute("COMMIT")
        return txid
    except sqlite3.IntegrityError:
        db.execute("ROLLBACK")
        return None
 
 
def log_tx(uid, kind, amount, status="done", info=""):
    return db.execute(
        "INSERT INTO tx(user_id,kind,amount,status,info,ts) VALUES(?,?,?,?,?,?)",
        (uid, kind, amount, status, info, int(time.time())),
    ).lastrowid
 
 
def fmt(cents):
    return f"{cents / 100:,.2f} ETB"
 
 
def parse_amount(text):
    try:
        cents = int(round(float(str(text).replace(",", "")) * 100))
    except (TypeError, ValueError):
        return None
    return cents if cents > 0 else None
 
 
def recover_rounds():
    """After a crash/restart, refund every unfinished round."""
    for r in db.execute("SELECT * FROM rounds WHERE status IN ('open','playing')").fetchall():
        for e in db.execute("SELECT * FROM entries WHERE round_id=?", (r["id"],)).fetchall():
            move(e["user_id"], r["price"], "refund", f"restart round {r['id']}")
        db.execute("UPDATE rounds SET status='cancelled' WHERE id=?", (r["id"],))
        log.info("refunded unfinished round %s", r["id"])
 
 
# ------------------------------------------------------------------ bingo logic
def card_for(card_no):
    """Fixed 5x5 card for a card number. Column c holds numbers c*15+1..c*15+15. Centre = 0 (FREE)."""
    rng = random.Random(f"{CARD_SALT}-{card_no}")
    cols = [rng.sample(range(c * 15 + 1, c * 15 + 16), 5) for c in range(5)]
    card = [[cols[c][r] for c in range(5)] for r in range(5)]
    card[2][2] = 0
    return card
 
 
ROWS = [[(r, c) for c in range(5)] for r in range(5)]
COLS = [[(r, c) for r in range(5)] for c in range(5)]
DIAGS = [[(i, i) for i in range(5)], [(i, 4 - i) for i in range(5)]]
CORNERS = [[(0, 0), (0, 4), (4, 0), (4, 4)]]
FULL = [[(r, c) for r in range(5) for c in range(5)]]
PATTERNS = (
    ("መስመር", ROWS),
    ("መስመር", COLS),
    ("ሰያፍ", DIAGS),
    ("አራት ጥግ", CORNERS),
    ("ሙሉ ካርድ", FULL),
)
 
 
def find_win(card, called):
    """Return (pattern name, cells) if the card has a winning pattern within `called`, else None."""
    for name, group in PATTERNS:
        for cells in group:
            if all(card[r][c] == 0 or card[r][c] in called for r, c in cells):
                return name, cells
    return None
 
 
class GameError(Exception):
    pass
 
 
NOTIFY = None  # async callable(uid, text), set in main()
 
 
class Room:
    def __init__(self, rid, name, price):
        self.id, self.name, self.price = rid, name, price
        self.lock = asyncio.Lock()
        self.epoch = 0
        self.phase = "buying"
        self.phase_end = time.time() + BUY_SECONDS
        self.phase_len = BUY_SECONDS
        self.reset_round()
 
    # ---- helpers
    def reset_round(self):
        self.epoch += 1
        self.round_id = None
        self.entries = {}       # card_no -> uid
        self.called = []
        self.pool = []
        self.dq = set()         # disqualified card numbers
        self.result = None
        self.cancel_reason = ""
        self.done = asyncio.Event()
 
    def cards_of(self, uid):
        return sorted(no for no, u in self.entries.items() if u == uid)
 
    def cut(self):
        return float(get_setting("cut"))
 
    def current_prize(self):
        return int(self.price * len(self.entries) * (100 - self.cut()) / 100)
 
    def set_phase(self, phase, seconds=0):
        self.phase = phase
        self.phase_len = seconds
        self.phase_end = time.time() + seconds if seconds else 0
 
    # ---- player actions
    async def buy(self, uid, card_no):
        async with self.lock:
            if self.phase != "buying":
                raise GameError("የካርድ መግዣ ጊዜ አብቅቷል። ቀጣዩን ዙር ይጠብቁ።")
            if not 1 <= card_no <= TOTAL_CARDS:
                raise GameError("ልክ ያልሆነ የካርድ ቁጥር።")
            if card_no in self.entries:
                raise GameError("ይህ ካርድ ተሽጧል።")
            if len(self.cards_of(uid)) >= MAX_CARDS:
                raise GameError(f"በአንድ ዙር እስከ {MAX_CARDS} ካርድ ብቻ መግዛት ይቻላል።")
            if self.round_id is None:
                self.round_id = db.execute(
                    "INSERT INTO rounds(room,status,price,ts) VALUES(?,?,?,?)",
                    (self.id, "open", self.price, int(time.time())),
                ).lastrowid
            if move(uid, -self.price, "bet", f"{self.id}:{self.round_id}:{card_no}") is None:
                raise GameError("በቂ ሂሳብ የለዎትም። /deposit ብለው ሂሳብ ይሙሉ።")
            db.execute(
                "INSERT INTO entries(round_id,user_id,card_no) VALUES(?,?,?)",
                (self.round_id, uid, card_no),
            )
            self.entries[card_no] = uid
 
    async def claim(self, uid):
        async with self.lock:
            if self.phase != "playing":
                raise GameError("ጨዋታው በሂደት ላይ አይደለም።")
            mine = [no for no in self.cards_of(uid) if no not in self.dq]
            if not mine:
                raise GameError("በዚህ ዙር ንቁ ካርድ የለዎትም።")
            called = set(self.called)
            if not any(find_win(card_for(no), called) for no in mine):
                self.dq.update(mine)
                raise GameError("❌ ሀሰተኛ ቢንጎ! ከዚህ ዙር ተሰናብተዋል።")
            self.finalize(called)
 
    # ---- round lifecycle
    def finalize(self, called):
        winners = []
        for no, uid in sorted(self.entries.items()):
            if no in self.dq:
                continue
            w = find_win(card_for(no), called)
            if w:
                winners.append((no, uid, w))
        pool = self.price * len(self.entries)
        prize = self.current_prize()
        each = prize // len(winners)
        commission = pool - each * len(winners)
        for no, uid, _ in winners:
            move(uid, each, "win", f"{self.id}:{self.round_id}:{no}")
            if NOTIFY:
                asyncio.create_task(
                    NOTIFY(uid, f"🏆 እንኳን ደስ አለዎት! በካርድ #{no} አሸንፈዋል: +{fmt(each)}")
                )
        log_tx(0, "commission", commission, "done", f"{self.id}:{self.round_id}")
        db.execute(
            "UPDATE rounds SET status='done', pool=?, commission=? WHERE id=?",
            (pool, commission, self.round_id),
        )
        self.result = {
            "cancelled": False,
            "pool": pool / 100,
            "prize": prize / 100,
            "prize_each": each / 100,
            "winners": [
                {
                    "name": user_name(uid),
                    "card": no,
                    "grid": card_for(no),
                    "cells": [list(c) for c in w[1]],
                    "pattern": w[0],
                }
                for no, uid, w in winners
            ],
        }
        self.set_phase("result", RESULT_SECONDS)
        self.done.set()
 
    def cancel_round(self, reason):
        if self.round_id:
            for no, uid in self.entries.items():
                move(uid, self.price, "refund", f"{self.id}:{self.round_id}")
            db.execute("UPDATE rounds SET status='cancelled' WHERE id=?", (self.round_id,))
        self.cancel_reason = reason
        self.set_phase("cancelled", CANCEL_SECONDS)
        self.done.set()
 
    async def buying(self):
        """Returns True when the round should start."""
        for _ in range(MAX_EXTENSIONS + 1):
            self.set_phase("buying", BUY_SECONDS)
            await asyncio.sleep(BUY_SECONDS)
            async with self.lock:
                players = len(set(self.entries.values()))
                if players >= MIN_PLAYERS:
                    self.pool = list(range(1, 76))
                    secrets.SystemRandom().shuffle(self.pool)  # unpredictable draw order
                    db.execute("UPDATE rounds SET status='playing' WHERE id=?", (self.round_id,))
                    self.set_phase("playing")
                    return True
                if players == 0:
                    return False
        async with self.lock:
            self.cancel_round("በቂ ተጫዋች አልተገኘም። ገንዘብዎ ተመልሷል።")
        return False
 
    async def play_loop(self):
        idle = 0
        while self.phase == "playing":
            try:
                await asyncio.wait_for(self.done.wait(), CALL_INTERVAL)
            except asyncio.TimeoutError:
                pass
            async with self.lock:
                if self.phase != "playing":
                    return
                if self.pool:
                    self.called.append(self.pool.pop())
                else:
                    idle += 1
                    if idle >= 3:  # all 75 called and nobody claimed
                        self.cancel_round("ማንም አላሸነፈም። ገንዘብዎ ተመልሷል።")
                        return
 
    async def run(self):
        while True:
            try:
                self.reset_round()
                if await self.buying():
                    await self.play_loop()
                if self.phase == "result":
                    await asyncio.sleep(RESULT_SECONDS)
                elif self.phase == "cancelled":
                    await asyncio.sleep(CANCEL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("room %s crashed - refunding round", self.id)
                try:
                    async with self.lock:
                        self.cancel_round("ስህተት ተፈጥሯል። ገንዘብዎ ተመልሷል።")
                except Exception:
                    log.exception("refund failed")
                await asyncio.sleep(3)
 
    # ---- view for the mini app
    def state_for(self, uid):
        d = {
            "room": self.id,
            "name": self.name,
            "price": self.price / 100,
            "phase": self.phase,
            "phase_end": self.phase_end,
            "phase_len": self.phase_len,
            "server_time": time.time(),
            "epoch": self.epoch,
            "sold": sorted(self.entries),
            "players": len(set(self.entries.values())),
            "prize": self.current_prize() / 100,
            "called": self.called,
            "mine": [
                {"no": no, "grid": card_for(no), "dq": no in self.dq}
                for no in self.cards_of(uid)
            ],
            "balance": balance(uid) / 100,
            "total_cards": TOTAL_CARDS,
            "max_cards": MAX_CARDS,
        }
        if self.phase == "result":
            d["result"] = self.result
        if self.phase == "cancelled":
            d["reason"] = self.cancel_reason
        return d
 
 
ROOMS = {rid: Room(rid, name, price) for rid, name, price in ROOM_DEFS}
 
 
# ------------------------------------------------------------------ web api
def verify_init_data(init_data):
    """Validate Telegram WebApp initData and return the user dict, or None."""
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        got_hash = parsed.pop("hash", None)
        if not got_hash:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got_hash):
            return None
        if time.time() - int(parsed.get("auth_date", "0")) > 86400:
            return None
        return json.loads(parsed["user"])
    except Exception:
        return None
 
 
@web.middleware
async def auth_mw(request, handler):
    if request.path.startswith("/api/"):
        user = None
        init = request.headers.get("X-Init-Data", "")
        if init:
            user = verify_init_data(init)
        elif DEV_MODE and request.query.get("dev_uid", "").isdigit():
            user = {"id": int(request.query["dev_uid"])}
        if not user:
            return web.json_response({"error": "unauthorized"}, status=401)
        if not reg_user(user["id"]):
            return web.json_response({"error": "not_registered"}, status=403)
        request["uid"] = user["id"]
    return await handler(request)
 
 
def get_room(request, data=None):
    rid = (data or {}).get("room") if data else request.query.get("room")
    room = ROOMS.get(rid or "")
    if not room:
        raise web.HTTPNotFound(
            text=json.dumps({"error": "ክፍሉ አልተገኘም"}), content_type="application/json"
        )
    return room
 
 
async def api_state(request):
    return web.json_response(get_room(request).state_for(request["uid"]))
 
 
async def api_card(request):
    try:
        no = int(request.query.get("no", "0"))
        assert 1 <= no <= TOTAL_CARDS
    except Exception:
        return web.json_response({"error": "ልክ ያልሆነ ቁጥር"}, status=400)
    return web.json_response({"no": no, "grid": card_for(no)})
 
 
async def api_buy(request):
    data = await request.json()
    room = get_room(request, data)
    try:
        await room.buy(request["uid"], int(data.get("card", 0)))
    except GameError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"ok": True})
 
 
async def api_bingo(request):
    data = await request.json()
    room = get_room(request, data)
    try:
        await room.claim(request["uid"])
    except GameError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"ok": True})
 
 
async def index(request):
    return web.FileResponse(WEBAPP_DIR / "index.html")
 
 
def build_web_app():
    app = web.Application(middlewares=[auth_mw])
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/card", api_card)
    app.router.add_post("/api/buy", api_buy)
    app.router.add_post("/api/bingo", api_bingo)
    app.router.add_get("/", index)
    app.router.add_static("/", WEBAPP_DIR, show_index=False)
    return app
 
 
# ------------------------------------------------------------------ telegram bot
MENU = ReplyKeyboardMarkup(
    [["🎮 ተጫወት", "💰 ቀሪ ሂሳብ"], ["➕ ሂሳብ ሙላ", "➖ ገንዘብ አውጣ"], ["📜 ታሪክ", "ℹ️ መመሪያ"]],
    resize_keyboard=True,
)
MENU_REGEX = r"^(🎮 ተጫወት|💰 ቀሪ ሂሳብ|➕ ሂሳብ ሙላ|➖ ገንዘብ አውጣ|📜 ታሪክ|ℹ️ መመሪያ)$"
 
INSTRUCTIONS = (
    "ℹ️ <b>የጨዋታ ህጎች (Game Rules)</b>\n\n"
    "1️⃣ ካርድ ከ1 እስከ 600 ይምረጡ (በአንድ ዙር እስከ 2 ካርድ)።\n"
    "2️⃣ ቁጥሮች ሲጠሩ በካርድዎ ላይ ያለውን ቁጥር ይንኩ።\n"
    "3️⃣ ከታች ካሉት አንዱን ሲያሟሉ <b>BINGO!</b> ይጫኑ።\n\n"
    "<pre>  B  I  N  G  O\n"
    "✅ ✅ ✅ ✅ ✅   ← መስመር\n\n"
    "  B  I  N  G  O\n"
    "✅ ⬜ ⬜ ⬜ ✅   ← አራት ጥግ\n"
    "⬜ ⬜ ⬜ ⬜ ⬜\n"
    "⬜ ⬜ ⭐ ⬜ ⬜\n"
    "⬜ ⬜ ⬜ ⬜ ⬜\n"
    "✅ ⬜ ⬜ ⬜ ✅\n\n"
    "✅ ⬜ ⬜ ⬜ ⬜   ← ሰያፍ\n"
    "⬜ ✅ ⬜ ⬜ ⬜\n"
    "⬜ ⬜ ⭐ ⬜ ⬜\n"
    "⬜ ⬜ ⬜ ✅ ⬜\n"
    "⬜ ⬜ ⬜ ⬜ ✅\n\n"
    "ሙሉ ካርድም ያሸንፋል።</pre>\n"
    "💰 ከአንድ በላይ አሸናፊ ካለ ደራሹ እኩል ይከፋፈላል።\n"
    "⚠️ ሀሰተኛ BINGO የጠየቀ ተጫዋች ከዙሩ ይሰናበታል።"
)
 
 
def instructions_for(method):
    if method == "cbe":
        title, acct, label = "🏦 የ CBE-Birr አካውንት", get_setting("acct_cbe"), "CBE-Birr"
    else:
        title, acct, label = "📱 የ TELE-Birr አካውንት", get_setting("acct_tele"), "TELE-Birr"
    return (
        f"{title}\n\n{label} Merchant ID: <code>{acct}</code>\n\n"
        "<b>መመሪያ</b>\n"
        f"1. በ{label} ከላይ ባለው አካውንት ገንዘቡን ይላኩ (Pay for Merchant)።\n"
        f"2. ክፍያው ሲሳካ ከ{label} አጭር የጽሁፍ መልእክት (SMS) ይደርስዎታል።\n"
        "3. ያንን መልእክት ሙሉውን ኮፒ (copy) አድርገው እዚህ ፔስት (paste) አድርገው ይላኩ።\n\n"
        f"ችግር ካጋጠመዎ {get_setting('support')} ያነጋግሩ።"
    )
 
 
async def notify_user(uid, text):
    try:
        await APP.bot.send_message(uid, text)
    except Exception as e:
        log.warning("notify %s failed: %s", uid, e)
 
 
async def notify_admins(text, kind, txid, can_approve=True):
    rows = []
    if can_approve:
        rows.append(
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"a:{kind}:{txid}:ok"),
                InlineKeyboardButton("❌ Reject", callback_data=f"a:{kind}:{txid}:no"),
            ]
        )
    for admin in ADMIN_IDS:
        try:
            await APP.bot.send_message(admin, text, reply_markup=InlineKeyboardMarkup(rows) if rows else None)
        except Exception as e:
            log.warning("admin notify failed: %s", e)
 
 
def ask_phone_markup():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 ስልክ አጋራ (Share Phone)", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
 
 
async def guard(update: Update):
    """True if the user is registered; otherwise asks them to share their phone."""
    ensure_user(update.effective_user)
    if reg_user(update.effective_user.id):
        return True
    await update.effective_message.reply_text(
        "🛡 <b>ምዝገባ ያስፈልጋል (Registration Required)</b>\n\n"
        "HabeshaBingoን ለመጠቀም ከታች «ስልክ አጋራ» የሚለውን ይጫኑ።",
        parse_mode="HTML",
        reply_markup=ask_phone_markup(),
    )
    return False
 
 
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if await guard(update):
        await update.message.reply_text(
            "🎱 እንኳን ወደ <b>HabeshaBingo</b> በደህና መጡ!\nለመጫወት «🎮 ተጫወት» ይጫኑ።",
            parse_mode="HTML",
            reply_markup=MENU,
        )
 
 
async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    if reg_user(update.effective_user.id):
        await update.message.reply_text("✅ ቀደም ብለው ተመዝግበዋል።", reply_markup=MENU)
    else:
        await guard(update)
 
 
async def on_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact
    user = update.effective_user
    if contact.user_id != user.id:
        await update.message.reply_text("❌ እባክዎ የራስዎን ስልክ ቁጥር ብቻ ያጋሩ።")
        return
    ensure_user(user)
    db.execute("UPDATE users SET phone=? WHERE id=?", (contact.phone_number, user.id))
    await update.message.reply_text(
        "✅ በተሳካ ሁኔታ ተመዝግበዋል! እንኳን ደህና መጡ 🎉\nለመጫወት «🎮 ተጫወት» ይጫኑ።",
        reply_markup=MENU,
    )
 
 
async def play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    context.user_data.pop("state", None)
    rows = [
        [
            InlineKeyboardButton(
                f"{name} | {price // 100} ብር",
                web_app=WebAppInfo(url=f"{WEBAPP_URL}/?room={rid}"),
            )
        ]
        for rid, name, price in ROOM_DEFS
    ]
    await update.message.reply_text(
        "🕹 <b>ለመጫወት ክፍል ይምረጡ:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )
 
 
async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    await update.message.reply_text(
        f"💰 ቀሪ ሂሳብ (Available): <code>{balance(update.effective_user.id) / 100:.2f} ETB</code>",
        parse_mode="HTML",
    )
 
 
async def instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(INSTRUCTIONS, parse_mode="HTML")
 
 
KIND_LABEL = {
    "deposit": "➕ ገቢ",
    "withdraw": "➖ ወጪ",
    "bet": "🎟 ካርድ ግዢ",
    "win": "🏆 ሽልማት",
    "refund": "↩️ ተመላሽ",
    "admin_credit": "🎁 ጭማሪ",
}
STATUS_LABEL = {"pending": "⏳", "done": "✅", "rejected": "❌"}
 
 
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    rows = db.execute(
        "SELECT * FROM tx WHERE user_id=? ORDER BY id DESC LIMIT 15", (update.effective_user.id,)
    ).fetchall()
    head = f"📜 <b>የክፍያ ታሪክ ({datetime.now():%d/%m/%Y})</b>\n━━━━━━━━━━━━\n"
    if not rows:
        await update.message.reply_text(head + "😔 ምንም ታሪክ የለም።", parse_mode="HTML")
        return
    lines = [
        f"{STATUS_LABEL.get(r['status'], '')} {KIND_LABEL.get(r['kind'], r['kind'])}: "
        f"{r['amount'] / 100:.2f} ETB · {datetime.fromtimestamp(r['ts']):%d/%m %H:%M}"
        for r in rows
    ]
    await update.message.reply_text(head + "\n".join(lines), parse_mode="HTML")
 
 
async def deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    context.user_data.pop("state", None)
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("CBE BIRR", callback_data="dep:cbe"),
            InlineKeyboardButton("TELE BIRR", callback_data="dep:tele"),
        ]]
    )
    await update.message.reply_text(
        "💳 <b>የማስገቢያ መንገድ ይምረጡ (Select Deposit Method)</b>\n\n"
        "እባክዎ ሂሳብ ለመሙላት የሚጠቀሙበትን መንገድ ይምረጡ።",
        parse_mode="HTML",
        reply_markup=kb,
    )
 
 
async def on_deposit_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    method = q.data.split(":")[1]
    context.user_data["state"] = f"dep:{method}"
    await q.answer()
    await q.message.reply_text(instructions_for(method), parse_mode="HTML")
 
 
async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    context.user_data["state"] = "wd_amount"
    await update.message.reply_text(
        "📤 <b>ገንዘብ ያውጡ (Withdraw Funds)</b>\n"
        "እባክዎ የሚያወጡትን የገንዘብ መጠን ያስገቡ (Enter amount to withdraw):",
        parse_mode="HTML",
    )
 
 
AMOUNT_RE = re.compile(r"(?:ETB|Birr|birr|BIRR|ብር)\s*([\d,]+(?:\.\d+)?)|([\d,]+(?:\.\d+)?)\s*(?:ETB|Birr|birr|BIRR|ብር)")
 
 
def guess_amount(sms):
    m = AMOUNT_RE.search(sms)
    if not m:
        return 0
    return parse_amount(m.group(1) or m.group(2)) or 0
 
 
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles free text for the deposit / withdraw conversations."""
    if not await guard(update):
        return
    user = update.effective_user
    text = update.message.text.strip()
    state = context.user_data.get("state", "")
 
    if state.startswith("dep:"):
        method = state.split(":")[1]
        if len(text) < 20:
            await update.message.reply_text("❌ እባክዎ የደረስዎትን SMS ሙሉውን ኮፒ አድርገው ይላኩ።")
            return
        h = hashlib.sha256(re.sub(r"\s+", "", text.lower()).encode()).hexdigest()
        if db.execute("SELECT 1 FROM sms WHERE hash=?", (h,)).fetchone():
            await update.message.reply_text("❌ ይህ መልእክት ከዚህ በፊት ገብቷል።")
            return
        amount = guess_amount(text)
        txid = log_tx(user.id, "deposit", amount, "pending", json.dumps({"m": method, "sms": text[:600]}))
        db.execute("INSERT INTO sms(hash,tx_id) VALUES(?,?)", (h, txid))
        context.user_data.pop("state", None)
        await update.message.reply_text(
            "⏳ ጥያቄዎ ደርሷል። አስተዳዳሪው ካረጋገጠ በኋላ ሂሳብዎ ይሞላል።", reply_markup=MENU
        )
        note = (
            f"➕ Deposit #{txid} ({method.upper()})\nUser: {user.first_name} ({user.id})\n"
            f"Detected amount: {fmt(amount) if amount else 'NOT FOUND'}\n\n{text[:600]}"
        )
        if not amount:
            note += f"\n\n⚠️ Amount not detected. Use /approve {txid} <amount>"
        note += "\n\n⚠️ Verify the payment in your own CBE/Telebirr account before approving."
        await notify_admins(note, "deposit", txid, can_approve=bool(amount))
        return
 
    if state == "wd_amount":
        amount = parse_amount(text)
        if not amount:
            await update.message.reply_text("❌ እባክዎ ቁጥር ብቻ ያስገቡ።")
            return
        if amount > balance(user.id):
            context.user_data.pop("state", None)
            await update.message.reply_text("❌ በቂ ሂሳብ የለዎትም (Insufficient balance).")
            return
        if amount < MIN_WITHDRAW:
            await update.message.reply_text(f"❌ ዝቅተኛው የማውጫ መጠን {fmt(MIN_WITHDRAW)} ነው።")
            return
        context.user_data["state"] = f"wd_acct:{amount}"
        await update.message.reply_text("ገንዘቡ የሚላክበትን የ Telebirr / CBE-Birr ስልክ ቁጥር ወይም አካውንት ያስገቡ:")
        return
 
    if state.startswith("wd_acct:"):
        amount = int(state.split(":")[1])
        account = text[:60]
        context.user_data.pop("state", None)
        txid = move(user.id, -amount, "withdraw", account, "pending")  # funds are held right away
        if txid is None:
            await update.message.reply_text("❌ በቂ ሂሳብ የለዎትም (Insufficient balance).")
            return
        await update.message.reply_text(
            "⏳ የማውጣት ጥያቄዎ ደርሷል። ገንዘቡ ተይዟል፤ አስተዳዳሪው ከከፈለ በኋላ ያሳውቅዎታል።", reply_markup=MENU
        )
        await notify_admins(
            f"➖ Withdrawal #{txid}\nUser: {user.first_name} ({user.id})\n"
            f"Amount: {fmt(amount)}\nPay to: {account}",
            "withdraw",
            txid,
        )
        return
 
    await update.message.reply_text("ለመጫወት «🎮 ተጫወት» ወይም /play ይጫኑ።", reply_markup=MENU)
 
 
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text
    context.user_data.pop("state", None)
    if t == "🎮 ተጫወት":
        await play(update, context)
    elif t == "💰 ቀሪ ሂሳብ":
        await balance_cmd(update, context)
    elif t == "➕ ሂሳብ ሙላ":
        await deposit(update, context)
    elif t == "➖ ገንዘብ አውጣ":
        await withdraw(update, context)
    elif t == "📜 ታሪክ":
        await history(update, context)
    else:
        await instructions(update, context)
 
 
# ---- admin
def is_admin(update):
    return update.effective_user.id in ADMIN_IDS
 
 
def apply_decision(txid, kind, ok):
    """Atomically approve/reject a pending tx. Returns (tx row, message for user) or None if already handled."""
    row = db.execute("SELECT * FROM tx WHERE id=?", (txid,)).fetchone()
    if not row or row["kind"] != kind:
        return None
    cur = db.execute(
        "UPDATE tx SET status=? WHERE id=? AND status='pending'", ("done" if ok else "rejected", txid)
    )
    if cur.rowcount == 0:
        return None
    uid, amount = row["user_id"], row["amount"]
    if kind == "deposit" and ok:
        db.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amount, uid))
        msg = f"✅ የ{fmt(amount)} ገቢዎ ተረጋግጧል። ቀሪ ሂሳብ: {fmt(balance(uid))}"
    elif kind == "deposit":
        msg = "❌ የሂሳብ መሙላት ጥያቄዎ ውድቅ ሆኗል። እባክዎ ሳፖርት ያነጋግሩ።"
    elif ok:
        msg = f"✅ የ{fmt(amount)} ወጪ ጥያቄዎ ተከፍሏል።"
    else:
        db.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amount, uid))
        msg = f"❌ የማውጣት ጥያቄዎ ውድቅ ሆኗል። {fmt(amount)} ወደ ሂሳብዎ ተመልሷል።"
    return row, msg
 
 
async def on_admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("Admins only.", show_alert=True)
        return
    _, kind, txid, act = q.data.split(":")
    res = apply_decision(int(txid), kind, act == "ok")
    if not res:
        await q.answer("Already handled.", show_alert=True)
        return
    row, msg = res
    await q.answer("Done.")
    label = "✅ Approved" if act == "ok" else "❌ Rejected"
    await q.edit_message_text(f"{q.message.text}\n\n{label} by {q.from_user.first_name}")
    await notify_user(row["user_id"], msg)
 
 
async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/approve <deposit id> <amount>  - approve a deposit with a manually typed amount."""
    if not is_admin(update):
        return
    try:
        txid, amount = int(context.args[0]), parse_amount(context.args[1])
        assert amount
    except Exception:
        await update.message.reply_text("Usage: /approve <deposit id> <amount>")
        return
    db.execute("UPDATE tx SET amount=? WHERE id=? AND kind='deposit' AND status='pending'", (amount, txid))
    res = apply_decision(txid, "deposit", True)
    if not res:
        await update.message.reply_text("Not found or already handled.")
        return
    await update.message.reply_text(f"✅ Deposit #{txid} approved: {fmt(amount)}")
    await notify_user(res[0]["user_id"], res[1])
 
 
async def setcut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    try:
        v = float(context.args[0])
        assert 0 <= v < 100
    except Exception:
        await update.message.reply_text("Usage: /setcut <percent>   e.g. /setcut 10")
        return
    set_setting("cut", v)
    await update.message.reply_text(f"House commission set to {v}%.")
 
 
async def setacct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setacct cbe 1000123456   |  /setacct tele 715516  |  /setacct support @name"""
    if not is_admin(update):
        return
    if len(context.args) < 2 or context.args[0] not in ("cbe", "tele", "support"):
        await update.message.reply_text("Usage: /setacct cbe|tele|support <value>")
        return
    key = {"cbe": "acct_cbe", "tele": "acct_tele", "support": "support"}[context.args[0]]
    set_setting(key, " ".join(context.args[1:]))
    await update.message.reply_text("Updated.")
 
 
async def credit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    try:
        uid, amt = int(context.args[0]), parse_amount(context.args[1])
        assert amt
    except Exception:
        await update.message.reply_text("Usage: /credit <user_id> <amount>")
        return
    if not db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
        await update.message.reply_text("Unknown user.")
        return
    move(uid, amt, "admin_credit", f"by {update.effective_user.id}")
    await update.message.reply_text(f"Added {fmt(amt)} to {uid}.")
    await notify_user(uid, f"💰 {fmt(amt)} ወደ ሂሳብዎ ተጨምሯል።")
 
 
def total(kind, since=0):
    return db.execute(
        "SELECT COALESCE(SUM(amount),0) s FROM tx WHERE kind=? AND status='done' AND ts>=?", (kind, since)
    ).fetchone()["s"]
 
 
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    day = int(time.time()) - 86400
    pend = lambda k: db.execute(
        "SELECT COUNT(*) c FROM tx WHERE kind=? AND status='pending'", (k,)
    ).fetchone()["c"]
    wallets = db.execute("SELECT COALESCE(SUM(balance),0) s FROM users").fetchone()["s"]
    users = db.execute("SELECT COUNT(*) c FROM users WHERE phone IS NOT NULL").fetchone()["c"]
    await update.message.reply_text(
        f"📊 Stats\nUsers: {users}\n"
        f"Commission (24h): {fmt(total('commission', day))}\n"
        f"Commission (all): {fmt(total('commission'))}\n"
        f"Deposits approved: {fmt(total('deposit'))}\n"
        f"Withdrawals paid: {fmt(total('withdraw'))}\n"
        f"Players' wallets total: {fmt(wallets)}\n"
        f"Pending: {pend('deposit')} deposits, {pend('withdraw')} withdrawals\n"
        f"Commission rate: {get_setting('cut')}%"
    )
 
 
# ------------------------------------------------------------------ main
APP = None
 
 
async def main():
    global APP, NOTIFY
    if not TOKEN:
        raise SystemExit("Set BOT_TOKEN first.")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS is empty: nobody can approve deposits/withdrawals!")
    if DEV_MODE:
        log.warning("DEV_MODE is ON - do not use in production!")
 
    recover_rounds()
    APP = Application.builder().token(TOKEN).build()
    NOTIFY = notify_user
 
    private = filters.ChatType.PRIVATE
    for name, fn in [
        ("start", start), ("register", register), ("play", play), ("balance", balance_cmd),
        ("deposit", deposit), ("withdraw", withdraw), ("history", history),
        ("instructions", instructions), ("approve", approve_cmd), ("setcut", setcut),
        ("setacct", setacct), ("credit", credit), ("stats", stats),
    ]:
        APP.add_handler(CommandHandler(name, fn, filters=private))
    APP.add_handler(MessageHandler(private & filters.CONTACT, on_contact))
    APP.add_handler(MessageHandler(private & filters.Regex(MENU_REGEX), menu_router))
    APP.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, text_router))
    APP.add_handler(CallbackQueryHandler(on_deposit_method, pattern=r"^dep:"))
    APP.add_handler(CallbackQueryHandler(on_admin_decision, pattern=r"^a:"))
 
    runner = web.AppRunner(build_web_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Mini app server on port %s, WEBAPP_URL=%s", PORT, WEBAPP_URL)
 
    await APP.initialize()
    await APP.bot.set_my_commands(
        [
            BotCommand("play", "Play"),
            BotCommand("balance", "Balance"),
            BotCommand("deposit", "Deposit"),
            BotCommand("withdraw", "Withdraw"),
            BotCommand("history", "Transaction history"),
            BotCommand("instructions", "Instructions"),
            BotCommand("register", "Register"),
        ]
    )
    await APP.start()
    await APP.updater.start_polling()
    tasks = [asyncio.create_task(room.run()) for room in ROOMS.values()]
    try:
        await asyncio.Event().wait()
    finally:
        for t in tasks:
            t.cancel()
        await APP.updater.stop()
        await APP.stop()
        await APP.shutdown()
        await runner.cleanup()
 
 
if __name__ == "__main__":
    asyncio.run(main())