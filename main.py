import os, json, hmac, hashlib, time, asyncio
import urllib.request
from urllib.parse import parse_qsl, urlencode
from contextlib import asynccontextmanager
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
CARD_INFO = os.environ.get("CARD_INFO", "Karta ma'lumoti kiritilmagan, admin bilan bog'laning")
COIN_PRICE = int(os.environ.get("COIN_PRICE", "1000"))  # 1 coin necha so'm
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]


def db():
    # "with db() as c:" blok oxirida avtomatik saqlaydi (xato bo'lsa bekor qiladi)
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None)


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            id BIGINT PRIMARY KEY, name TEXT, balance BIGINT DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS auctions(
            id BIGSERIAL PRIMARY KEY, title TEXT, start_price BIGINT,
            ends_at BIGINT, top_bid BIGINT DEFAULT 0, top_user BIGINT,
            status TEXT DEFAULT 'active')""")
        c.execute("""CREATE TABLE IF NOT EXISTS bids(
            id BIGSERIAL PRIMARY KEY, auction_id BIGINT,
            user_id BIGINT, amount BIGINT, ts BIGINT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS topups(
            id BIGSERIAL PRIMARY KEY, user_id BIGINT, coins BIGINT,
            status TEXT DEFAULT 'pending', ts BIGINT)""")


def tg(method, **params):
    try:
        data = urlencode(params).encode()
        with urllib.request.urlopen(
                f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", data, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print("Telegram xato:", method, e)
        return None


def notify(chat_id, text):
    tg("sendMessage", chat_id=chat_id, text=text)


def set_webhook():
    base = os.environ.get("BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if base:
        tg("setWebhook", url=base.rstrip("/") + "/webhook", secret_token=WEBHOOK_SECRET,
           allowed_updates=json.dumps(["message", "callback_query"]))


def close_expired():
    with db() as c:
        rows = c.execute("UPDATE auctions SET status='ended' "
                         "WHERE status='active' AND ends_at<=%s "
                         "RETURNING title, top_bid, top_user",
                         (int(time.time()),)).fetchall()
    for r in rows:
        if r["top_user"] is None:
            continue
        notify(r["top_user"],
               f"Tabriklaymiz! Siz {r['title']} auksionida {r['top_bid']} coin bilan yutdingiz.")
        for admin in ADMINS:
            notify(admin,
                   f"Auksion tugadi: {r['title']}. G'olib ID: {r['top_user']}, stavka: {r['top_bid']} coin.")


async def closer():
    while True:
        try:
            await asyncio.to_thread(close_expired)
        except Exception as e:
            print("closer xato:", e)
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app):
    init_db()
    asyncio.create_task(asyncio.to_thread(set_webhook))
    task = asyncio.create_task(closer())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


def auth(init_data: str) -> int:
    """Telegram initData imzosini tekshiradi va user id qaytaradi."""
    try:
        d = dict(parse_qsl(init_data, strict_parsing=True))
        h = d.pop("hash")
        s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(key, s.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, h):
            raise ValueError
        if time.time() - int(d["auth_date"]) > 86400:
            raise ValueError
        u = json.loads(d["user"])
    except Exception:
        raise HTTPException(401, "Avtorizatsiya xato")
    with db() as c:
        c.execute("INSERT INTO users(id,name) VALUES(%s,%s) "
                  "ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name",
                  (u["id"], u.get("first_name", "")))
    return u["id"]


@app.get("/")
def index():
    return FileResponse("index.html")


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"ok": True}


@app.get("/api/me")
def me(x_init_data: str = Header()):
    uid = auth(x_init_data)
    with db() as c:
        row = c.execute("SELECT balance FROM users WHERE id=%s", (uid,)).fetchone()
    return {"id": uid, "balance": row["balance"], "is_admin": uid in ADMINS}


@app.get("/api/auctions")
def auctions(x_init_data: str = Header()):
    uid = auth(x_init_data)
    with db() as c:
        rows = c.execute("SELECT * FROM auctions WHERE status='active' "
                         "ORDER BY ends_at").fetchall()
    return [{
        "id": r["id"], "title": r["title"],
        "price": max(r["start_price"], r["top_bid"]),
        "ends_at": r["ends_at"],
        "is_top": r["top_user"] == uid,
        "has_bids": r["top_user"] is not None,
    } for r in rows]


@app.get("/api/history")
def history(x_init_data: str = Header()):
    uid = auth(x_init_data)
    with db() as c:
        rows = c.execute(
            "SELECT a.title, a.start_price, a.top_bid, a.top_user, u.name "
            "FROM auctions a LEFT JOIN users u ON u.id = a.top_user "
            "WHERE a.status='ended' ORDER BY a.ends_at DESC LIMIT 20").fetchall()
    return [{
        "title": r["title"],
        "price": max(r["start_price"], r["top_bid"]),
        "winner": r["name"] or "",
        "has_winner": r["top_user"] is not None,
        "is_me": r["top_user"] == uid,
    } for r in rows]


class Bid(BaseModel):
    auction_id: int
    amount: int


@app.post("/api/bid")
def bid(b: Bid, x_init_data: str = Header()):
    uid = auth(x_init_data)
    if b.amount <= 0:
        raise HTTPException(400, "Miqdor noto'g'ri")
    with db() as c:
        a = c.execute("SELECT * FROM auctions WHERE id=%s FOR UPDATE",
                      (b.auction_id,)).fetchone()
        if not a or a["status"] != "active" or a["ends_at"] <= time.time():
            raise HTTPException(400, "Auksion tugagan")
        min_bid = a["start_price"] if a["top_user"] is None else a["top_bid"] + 1
        if b.amount < min_bid:
            raise HTTPException(400, f"Kamida {min_bid} coin")
        ids = sorted({uid} | ({a["top_user"]} if a["top_user"] is not None else set()))
        rows = c.execute("SELECT id, balance FROM users WHERE id = ANY(%s) "
                         "ORDER BY id FOR UPDATE", (ids,)).fetchall()
        bal = next(r["balance"] for r in rows if r["id"] == uid)
        # Har bir stavka alohida to'lanadi: oldingi stavka qaytarilmaydi
        # va yangi stavka uchun qayta ishlatilmaydi.
        if bal < b.amount:
            raise HTTPException(400, "Coin yetarli emas")
        c.execute("UPDATE users SET balance=balance-%s WHERE id=%s", (b.amount, uid))
        c.execute("UPDATE auctions SET top_bid=%s, top_user=%s WHERE id=%s",
                  (b.amount, uid, a["id"]))
        c.execute("INSERT INTO bids(auction_id,user_id,amount,ts) VALUES(%s,%s,%s,%s)",
                  (a["id"], uid, b.amount, int(time.time())))
    return {"ok": True}


class TopUp(BaseModel):
    coins: int


@app.post("/api/topup")
def topup(t: TopUp, x_init_data: str = Header()):
    uid = auth(x_init_data)
    if t.coins < 1 or t.coins > 10_000_000:
        raise HTTPException(400, "Miqdor noto'g'ri")
    with db() as c:
        c.execute("UPDATE topups SET status='cancelled' "
                  "WHERE user_id=%s AND status='pending'", (uid,))
        row = c.execute("INSERT INTO topups(user_id,coins,ts) VALUES(%s,%s,%s) RETURNING id",
                        (uid, t.coins, int(time.time()))).fetchone()
    return {"id": row["id"], "coins": t.coins,
            "sum": t.coins * COIN_PRICE, "card": CARD_INFO}


def handle_message(m):
    if m["chat"]["type"] != "private":
        return
    uid = m["from"]["id"]
    if m.get("photo") or m.get("document"):
        with db() as c:
            t = c.execute("SELECT id, coins FROM topups WHERE user_id=%s AND status='pending' "
                          "ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        if not t:
            notify(uid, "Avval Mini App'da To'ldirish bo'limida so'rov yarating, keyin chekni yuboring.")
            return
        cap = (f"So'rov #{t['id']}\nFoydalanuvchi: {m['from'].get('first_name', '')} (ID {uid})\n"
               f"Miqdor: {t['coins']} coin ({t['coins'] * COIN_PRICE} so'm)")
        kb = json.dumps({"inline_keyboard": [[
            {"text": "Tasdiqlash", "callback_data": f"ok:{t['id']}"},
            {"text": "Rad etish", "callback_data": f"no:{t['id']}"}]]})
        for admin in ADMINS:
            tg("copyMessage", chat_id=admin, from_chat_id=uid,
               message_id=m["message_id"], caption=cap, reply_markup=kb)
        notify(uid, f"Chek qabul qilindi. So'rov #{t['id']} admin tomonidan tekshirilmoqda.")
    else:
        notify(uid, "Auksionga kirish uchun chat menyusidagi Auksion tugmasini bosing.")


def handle_callback(cb):
    adm = cb["from"]["id"]
    if adm not in ADMINS:
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Faqat admin", show_alert="true")
        return
    action, tid = cb["data"].split(":")
    tid = int(tid)
    new = "approved" if action == "ok" else "rejected"
    with db() as c:
        t = c.execute("UPDATE topups SET status=%s WHERE id=%s AND status='pending' "
                      "RETURNING user_id, coins", (new, tid)).fetchone()
        if t and new == "approved":
            c.execute("UPDATE users SET balance=balance+%s WHERE id=%s",
                      (t["coins"], t["user_id"]))
    if not t:
        tg("answerCallbackQuery", callback_query_id=cb["id"],
           text="Bu so'rov allaqachon ko'rib chiqilgan", show_alert="true")
        return
    tg("answerCallbackQuery", callback_query_id=cb["id"], text="Bajarildi")
    if new == "approved":
        notify(t["user_id"], f"So'rov #{tid} tasdiqlandi. Hisobingizga {t['coins']} coin qo'shildi.")
        status = "TASDIQLANDI"
    else:
        notify(t["user_id"], f"So'rov #{tid} rad etildi. Savol bo'lsa admin bilan bog'laning.")
        status = "RAD ETILDI"
    msg = cb.get("message")
    if msg:
        tg("editMessageCaption", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
           caption=f"So'rov #{tid}: {status} ({t['coins']} coin)")


@app.post("/webhook")
def webhook(update: dict, x_telegram_bot_api_secret_token: str = Header(None)):
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(403, "Ruxsat yo'q")
    try:
        if "callback_query" in update:
            handle_callback(update["callback_query"])
        elif "message" in update:
            handle_message(update["message"])
    except Exception as e:
        print("webhook xato:", e)
    return {"ok": True}


class NewAuction(BaseModel):
    title: str
    start_price: int
    minutes: int


class Give(BaseModel):
    user_id: int
    amount: int


def require_admin(uid):
    if uid not in ADMINS:
        raise HTTPException(403, "Faqat admin")


@app.post("/api/admin/auction")
def new_auction(a: NewAuction, x_init_data: str = Header()):
    require_admin(auth(x_init_data))
    with db() as c:
        c.execute("INSERT INTO auctions(title,start_price,ends_at) VALUES(%s,%s,%s)",
                  (a.title, a.start_price, int(time.time()) + a.minutes * 60))
    return {"ok": True}


@app.post("/api/admin/give")
def give(g: Give, x_init_data: str = Header()):
    require_admin(auth(x_init_data))
    with db() as c:
        c.execute("INSERT INTO users(id,name,balance) VALUES(%s,%s,%s) "
                  "ON CONFLICT(id) DO UPDATE SET balance=users.balance+%s",
                  (g.user_id, "", g.amount, g.amount))
    return {"ok": True}
