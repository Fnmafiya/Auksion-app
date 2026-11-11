import os, re, json, hmac, hashlib, time, asyncio, base64
import urllib.request
from urllib.parse import parse_qsl, urlencode
from contextlib import asynccontextmanager
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
CARD_INFO = os.environ.get("CARD_INFO", "Karta ma'lumoti kiritilmagan, admin bilan bog'laning")
COIN_PRICE = int(os.environ.get("COIN_PRICE", "1000"))  # 1 coin necha so'm
REFERRAL_BONUS = int(os.environ.get("REFERRAL_BONUS", "50"))  # taklif qilganga bonus (coin)
WELCOME_BONUS = int(os.environ.get("WELCOME_BONUS", "100"))  # yangi o'yinchiga (coin)
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").lstrip("@")  # to'lov uchun admin lichkasi
FRIENDS_MODE = os.environ.get("FRIENDS_MODE", "0") == "1"  # do'stlar rejimi: pulsiz
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]


def db():
    # "with db() as c:" blok oxirida avtomatik saqlaydi (xato bo'lsa bekor qiladi)
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None)


def log_coin(c, uid, amount, kind):
    """Coin tushumlarini yozib boradi (kunlik/haftalik/oylik reyting uchun)."""
    c.execute("INSERT INTO coin_log(user_id,amount,kind,ts) VALUES(%s,%s,%s,%s)",
              (uid, amount, kind, int(time.time())))


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
        c.execute("ALTER TABLE auctions ADD COLUMN IF NOT EXISTS image TEXT")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referrer BIGINT")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS wins BIGINT DEFAULT 0")
        c.execute("""CREATE TABLE IF NOT EXISTS tasks(
            id BIGSERIAL PRIMARY KEY, title TEXT, descr TEXT, reward BIGINT,
            url TEXT, active BOOLEAN DEFAULT TRUE)""")
        c.execute("""CREATE TABLE IF NOT EXISTS task_claims(
            id BIGSERIAL PRIMARY KEY, task_id BIGINT, user_id BIGINT,
            status TEXT DEFAULT 'pending', ts BIGINT, UNIQUE(task_id, user_id))""")
        c.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS chat TEXT")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_paid BOOLEAN DEFAULT FALSE")
        c.execute("""CREATE TABLE IF NOT EXISTS topups(
            id BIGSERIAL PRIMARY KEY, user_id BIGINT, coins BIGINT,
            status TEXT DEFAULT 'pending', ts BIGINT)""")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS points BIGINT DEFAULT 0")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS photo TEXT")
        # eski foydalanuvchilar "kutib olingan" hisoblanadi, bonus faqat yangilarga beriladi
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS welcomed BOOLEAN")
        c.execute("UPDATE users SET welcomed=TRUE WHERE welcomed IS NULL")
        c.execute("ALTER TABLE users ALTER COLUMN welcomed SET DEFAULT FALSE")
        c.execute("""CREATE TABLE IF NOT EXISTS coin_log(
            id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount BIGINT, kind TEXT, ts BIGINT)""")
        c.execute("CREATE INDEX IF NOT EXISTS coin_log_ts ON coin_log(ts)")
        c.execute("""CREATE TABLE IF NOT EXISTS duels(
            id BIGSERIAL PRIMARY KEY, p1 BIGINT, p2 BIGINT, m1 TEXT, m2 TEXT,
            status TEXT DEFAULT 'waiting', winner BIGINT, ts BIGINT)""")


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
        with db() as c:
            c.execute("UPDATE users SET wins=COALESCE(wins,0)+1 WHERE id=%s", (r["top_user"],))
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
        c.execute("INSERT INTO users(id,name,photo) VALUES(%s,%s,%s) "
                  "ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name, "
                  "photo=COALESCE(EXCLUDED.photo, users.photo)",
                  (u["id"], u.get("first_name", ""), u.get("photo_url")))
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
    welcome = 0
    with db() as c:
        g = c.execute("UPDATE users SET balance=balance+%s, welcomed=TRUE "
                      "WHERE id=%s AND NOT COALESCE(welcomed, FALSE) RETURNING id",
                      (WELCOME_BONUS, uid)).fetchone()
        if g and WELCOME_BONUS > 0:
            log_coin(c, uid, WELCOME_BONUS, "welcome")
            welcome = WELCOME_BONUS
        row = c.execute("SELECT balance, COALESCE(wins,0) AS wins FROM users WHERE id=%s",
                        (uid,)).fetchone()
    return {"id": uid, "balance": row["balance"], "wins": row["wins"], "welcome": welcome,
            "is_admin": uid in ADMINS, "friends_mode": FRIENDS_MODE}


@app.get("/api/auctions")
def auctions(x_init_data: str = Header()):
    uid = auth(x_init_data)
    with db() as c:
        rows = c.execute("SELECT id, title, start_price, ends_at, top_bid, top_user, "
                         "(image IS NOT NULL) AS has_image FROM auctions "
                         "WHERE status='active' ORDER BY ends_at").fetchall()
    return [{
        "id": r["id"], "title": r["title"],
        "price": max(r["start_price"], r["top_bid"]),
        "ends_at": r["ends_at"],
        "is_top": r["top_user"] == uid,
        "has_bids": r["top_user"] is not None,
        "image": bool(r["has_image"]),
    } for r in rows]


@app.get("/api/auction/{aid}/image")
def auction_image(aid: int):
    with db() as c:
        r = c.execute("SELECT image FROM auctions WHERE id=%s", (aid,)).fetchone()
    if not r or not r["image"]:
        raise HTTPException(404, "Rasm yo'q")
    return Response(base64.b64decode(r["image"]), media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


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


def pay_referral(uid):
    if REFERRAL_BONUS <= 0:
        return
    with db() as c:
        r = c.execute("UPDATE users SET ref_paid=TRUE WHERE id=%s AND referrer IS NOT NULL "
                      "AND NOT COALESCE(ref_paid, FALSE) RETURNING referrer", (uid,)).fetchone()
        if r:
            c.execute("UPDATE users SET balance=balance+%s WHERE id=%s",
                      (REFERRAL_BONUS, r["referrer"]))
            log_coin(c, r["referrer"], REFERRAL_BONUS, "referral")
    if r:
        notify(r["referrer"], f"Taklif qilgan do'stingiz birinchi stavkasini qo'ydi. "
                              f"Sizga {REFERRAL_BONUS} coin bonus berildi.")


_BOT_NAME = None


def bot_username():
    global _BOT_NAME
    if not _BOT_NAME:
        r = tg("getMe")
        if r and r.get("ok"):
            _BOT_NAME = r["result"]["username"]
    return _BOT_NAME


@app.get("/api/referral")
def referral(x_init_data: str = Header()):
    uid = auth(x_init_data)
    with db() as c:
        r = c.execute("SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE ref_paid) AS p "
                      "FROM users WHERE referrer=%s", (uid,)).fetchone()
    name = bot_username()
    return {"link": f"https://t.me/{name}?start=ref_{uid}" if name else "",
            "invited": r["n"], "rewarded": r["p"], "bonus": REFERRAL_BONUS}


@app.post("/api/bid")
def bid(b: Bid, x_init_data: str = Header()):
    uid = auth(x_init_data)
    res = place_bid(b, uid)
    pay_referral(uid)
    return res


def place_bid(b: Bid, uid: int):
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
        if FRIENDS_MODE:
            # Do'stlar rejimi: pul aralashmaydi, har kim o'zining eng katta stavkasini sarflaydi
            prev = c.execute("SELECT COALESCE(MAX(amount),0) AS m FROM bids "
                             "WHERE auction_id=%s AND user_id=%s", (a["id"], uid)).fetchone()["m"]
            u = c.execute("SELECT balance FROM users WHERE id=%s FOR UPDATE", (uid,)).fetchone()
            cost = b.amount - prev
            if u["balance"] < cost:
                raise HTTPException(400, "Coin yetarli emas")
            c.execute("UPDATE users SET balance=balance-%s WHERE id=%s", (cost, uid))
            c.execute("UPDATE auctions SET top_bid=%s, top_user=%s WHERE id=%s",
                      (b.amount, uid, a["id"]))
            c.execute("INSERT INTO bids(auction_id,user_id,amount,ts) VALUES(%s,%s,%s,%s)",
                      (a["id"], uid, b.amount, int(time.time())))
            return {"ok": True}
        ids = sorted({uid} | ({a["top_user"]} if a["top_user"] is not None else set()))
        rows = c.execute("SELECT id, balance FROM users WHERE id = ANY(%s) "
                         "ORDER BY id FOR UPDATE", (ids,)).fetchall()
        bal = next(r["balance"] for r in rows if r["id"] == uid)
        own = a["top_bid"] if a["top_user"] == uid else 0
        if bal + own < b.amount:
            raise HTTPException(400, "Coin yetarli emas")
        if a["top_user"] is not None:  # oldingi yetakchiga qaytarish
            c.execute("UPDATE users SET balance=balance+%s WHERE id=%s",
                      (a["top_bid"], a["top_user"]))
        c.execute("UPDATE users SET balance=balance-%s WHERE id=%s", (b.amount, uid))
        c.execute("UPDATE auctions SET top_bid=%s, top_user=%s WHERE id=%s",
                  (b.amount, uid, a["id"]))
        c.execute("INSERT INTO bids(auction_id,user_id,amount,ts) VALUES(%s,%s,%s,%s)",
                  (a["id"], uid, b.amount, int(time.time())))
    return {"ok": True}


MOVES = {"rock": "scissors", "scissors": "paper", "paper": "rock"}


def duel_view(d, uid):
    mine = d["p1"] == uid
    my = d["m1"] if mine else d["m2"]
    opp = d["m2"] if mine else d["m1"]
    res = None
    if d["status"] == "done":
        res = "draw" if d["winner"] is None else ("win" if d["winner"] == uid else "lose")
    return {"id": d["id"], "status": d["status"], "my": my,
            "opp": opp if d["status"] == "done" else None, "result": res}


def finish(c, d):
    a, b = d["m1"], d["m2"]
    if a and b:
        w = None if a == b else (d["p1"] if MOVES[a] == b else d["p2"])
    elif a or b:
        w = d["p1"] if a else d["p2"]  # raqib javob bermadi
    else:
        w = None
    c.execute("UPDATE duels SET status='done', winner=%s WHERE id=%s", (w, d["id"]))
    if w:
        c.execute("UPDATE users SET points=points+10 WHERE id=%s", (w,))
    elif a and b:
        c.execute("UPDATE users SET points=points+3 WHERE id = ANY(%s)", ([d["p1"], d["p2"]],))
    d["status"], d["winner"] = "done", w


@app.post("/api/duel/join")
def duel_join(x_init_data: str = Header()):
    uid = auth(x_init_data)
    now = int(time.time())
    with db() as c:
        d = c.execute("SELECT * FROM duels WHERE (p1=%s OR p2=%s) AND status IN ('waiting','playing') "
                      "AND ts>%s ORDER BY id DESC LIMIT 1", (uid, uid, now - 60)).fetchone()
        if d:
            return duel_view(d, uid)
        w = c.execute("SELECT * FROM duels WHERE status='waiting' AND p1<>%s AND ts>%s "
                      "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED", (uid, now - 60)).fetchone()
        if w:
            d = c.execute("UPDATE duels SET p2=%s, status='playing', ts=%s WHERE id=%s RETURNING *",
                          (uid, now, w["id"])).fetchone()
        else:
            d = c.execute("INSERT INTO duels(p1,ts) VALUES(%s,%s) RETURNING *", (uid, now)).fetchone()
    return duel_view(d, uid)


@app.get("/api/duel/{duel_id}")
def duel_state(duel_id: int, x_init_data: str = Header()):
    uid = auth(x_init_data)
    now = int(time.time())
    with db() as c:
        d = c.execute("SELECT * FROM duels WHERE id=%s FOR UPDATE", (duel_id,)).fetchone()
        if not d or uid not in (d["p1"], d["p2"]):
            raise HTTPException(404, "Topilmadi")
        if d["status"] == "waiting" and d["ts"] < now - 60:
            c.execute("UPDATE duels SET status='expired' WHERE id=%s", (duel_id,))
            d["status"] = "expired"
        elif d["status"] == "playing" and d["ts"] < now - 60:
            finish(c, d)
    return duel_view(d, uid)


class Move(BaseModel):
    id: int
    move: str


@app.post("/api/duel/move")
def duel_move(m: Move, x_init_data: str = Header()):
    uid = auth(x_init_data)
    if m.move not in MOVES:
        raise HTTPException(400, "Noto'g'ri tanlov")
    with db() as c:
        d = c.execute("SELECT * FROM duels WHERE id=%s FOR UPDATE", (m.id,)).fetchone()
        if not d or d["status"] != "playing" or uid not in (d["p1"], d["p2"]):
            raise HTTPException(400, "O'yin faol emas")
        col = "m1" if d["p1"] == uid else "m2"
        if d[col]:
            raise HTTPException(400, "Siz allaqachon tanlagansiz")
        c.execute(f"UPDATE duels SET {col}=%s WHERE id=%s", (m.move, d["id"]))
        d[col] = m.move
        if d["m1"] and d["m2"]:
            finish(c, d)
    return duel_view(d, uid)


@app.get("/api/leaderboard")
def leaderboard(x_init_data: str = Header()):
    uid = auth(x_init_data)
    with db() as c:
        rows = c.execute("SELECT id, name, points FROM users WHERE points>0 "
                         "ORDER BY points DESC LIMIT 10").fetchall()
        me = c.execute("SELECT points FROM users WHERE id=%s", (uid,)).fetchone()
    return {"my_points": me["points"],
            "top": [{"name": r["name"] or "O'yinchi", "points": r["points"],
                     "me": r["id"] == uid} for r in rows]}


_ADMIN_NAME = None


def admin_username():
    global _ADMIN_NAME
    if ADMIN_USERNAME:
        return ADMIN_USERNAME
    if not _ADMIN_NAME and ADMINS:
        r = tg("getChat", chat_id=sorted(ADMINS)[0])
        if r and r.get("ok"):
            _ADMIN_NAME = r["result"].get("username")
    return _ADMIN_NAME


class TopUp(BaseModel):
    coins: int


@app.post("/api/topup")
def topup(t: TopUp, x_init_data: str = Header()):
    uid = auth(x_init_data)
    if FRIENDS_MODE:
        raise HTTPException(403, "Do'stlar rejimida to'ldirish o'chirilgan")
    if t.coins < 1 or t.coins > 10_000_000:
        raise HTTPException(400, "Miqdor noto'g'ri")
    with db() as c:
        c.execute("UPDATE topups SET status='cancelled' "
                  "WHERE user_id=%s AND status='pending'", (uid,))
        row = c.execute("INSERT INTO topups(user_id,coins,ts) VALUES(%s,%s,%s) RETURNING id",
                        (uid, t.coins, int(time.time()))).fetchone()
        u = c.execute("SELECT name FROM users WHERE id=%s", (uid,)).fetchone()
    kb = json.dumps({"inline_keyboard": [[
        {"text": "Tasdiqlash", "callback_data": f"ok:{row['id']}"},
        {"text": "Rad etish", "callback_data": f"no:{row['id']}"}]]})
    for admin in ADMINS:
        tg("sendMessage", chat_id=admin, reply_markup=kb,
           text=f"Coin so'rovi #{row['id']}\nFoydalanuvchi: {u['name']} (ID {uid})\n"
                f"Miqdor: {t.coins} coin ({t.coins * COIN_PRICE} so'm)\n"
                f"To'lovni lichkada olgach, Tasdiqlash ni bosing.")
    name = admin_username()
    link = ""
    if name:
        link = f"https://t.me/{name}?" + urlencode(
            {"text": f"Salom! {t.coins} coin olmoqchiman (so'rov #{row['id']})."})
    return {"id": row["id"], "coins": t.coins, "sum": t.coins * COIN_PRICE, "admin_link": link}


def handle_start(m, param):
    uid = m["from"]["id"]
    ref = int(param[4:]) if param.startswith("ref_") and param[4:].isdigit() else None
    if ref == uid:
        ref = None
    with db() as c:
        if ref is not None and not c.execute("SELECT 1 FROM users WHERE id=%s", (ref,)).fetchone():
            ref = None
        # faqat yangi foydalanuvchi taklif hisobiga o'tadi
        c.execute("INSERT INTO users(id,name,referrer) VALUES(%s,%s,%s) ON CONFLICT(id) DO NOTHING",
                  (uid, m["from"].get("first_name", ""), ref))
    base = os.environ.get("BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    params = {"chat_id": uid, "text": "Auksionga xush kelibsiz! Pastdagi tugma orqali kiring."}
    if base:
        params["reply_markup"] = json.dumps({"inline_keyboard": [[
            {"text": "🔨 Auksionni ochish", "web_app": {"url": base}}]]})
    tg("sendMessage", **params)


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
        text = m.get("text") or ""
        if text.startswith("/start"):
            handle_start(m, text[6:].strip())
        else:
            notify(uid, "Auksionga kirish uchun chat menyusidagi Auksion tugmasini bosing.")


def handle_task_cb(cb):
    action, kid = cb["data"][3:].split(":")
    kid = int(kid)
    new = "approved" if action == "ok" else "rejected"
    with db() as c:
        k = c.execute("UPDATE task_claims SET status=%s WHERE id=%s AND status='pending' "
                      "RETURNING task_id, user_id", (new, kid)).fetchone()
        t = None
        if k:
            t = c.execute("SELECT title, reward FROM tasks WHERE id=%s", (k["task_id"],)).fetchone()
            if new == "approved" and t:
                c.execute("UPDATE users SET balance=balance+%s WHERE id=%s",
                          (t["reward"], k["user_id"]))
                log_coin(c, k["user_id"], t["reward"], "task")
    if not k:
        tg("answerCallbackQuery", callback_query_id=cb["id"],
           text="Bu so'rov allaqachon ko'rib chiqilgan", show_alert="true")
        return
    tg("answerCallbackQuery", callback_query_id=cb["id"], text="Bajarildi")
    title = t["title"] if t else "vazifa"
    if new == "approved":
        notify(k["user_id"], f"Vazifa tasdiqlandi: {title}. Hisobingizga {t['reward']} coin qo'shildi.")
        status = "TASDIQLANDI"
    else:
        notify(k["user_id"], f"Vazifa rad etildi: {title}.")
        status = "RAD ETILDI"
    msg = cb.get("message")
    if msg:
        tg("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
           text=f"Vazifa: {title}. {status}")


def handle_callback(cb):
    adm = cb["from"]["id"]
    if adm not in ADMINS:
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Faqat admin", show_alert="true")
        return
    if cb["data"].startswith("tk_"):
        return handle_task_cb(cb)
    action, tid = cb["data"].split(":")
    tid = int(tid)
    new = "approved" if action == "ok" else "rejected"
    with db() as c:
        t = c.execute("UPDATE topups SET status=%s WHERE id=%s AND status='pending' "
                      "RETURNING user_id, coins", (new, tid)).fetchone()
        if t and new == "approved":
            c.execute("UPDATE users SET balance=balance+%s WHERE id=%s",
                      (t["coins"], t["user_id"]))
            log_coin(c, t["user_id"], t["coins"], "topup")
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
    image: str = ""  # base64 JPEG (prefikssiz)


class Give(BaseModel):
    user_id: int
    amount: int


def require_admin(uid):
    if uid not in ADMINS:
        raise HTTPException(403, "Faqat admin")


@app.post("/api/admin/auction")
def new_auction(a: NewAuction, x_init_data: str = Header()):
    require_admin(auth(x_init_data))
    img = None
    if a.image:
        try:
            raw = base64.b64decode(a.image, validate=True)
        except Exception:
            raise HTTPException(400, "Rasm noto'g'ri")
        if len(raw) > 300_000 or not raw.startswith(b"\xff\xd8"):
            raise HTTPException(400, "Rasm juda katta yoki JPEG emas")
        img = a.image
    with db() as c:
        c.execute("INSERT INTO auctions(title,start_price,ends_at,image) VALUES(%s,%s,%s,%s)",
                  (a.title, a.start_price, int(time.time()) + a.minutes * 60, img))
    return {"ok": True}


@app.post("/api/admin/give")
def give(g: Give, x_init_data: str = Header()):
    require_admin(auth(x_init_data))
    with db() as c:
        c.execute("INSERT INTO users(id,name,balance) VALUES(%s,%s,%s) "
                  "ON CONFLICT(id) DO UPDATE SET balance=users.balance+%s",
                  (g.user_id, "", g.amount, g.amount))
    return {"ok": True}


@app.post("/api/admin/take")
def take(g: Give, x_init_data: str = Header()):
    """Admin: foydalanuvchi balansidan coin ayirish (balans 0 dan pastga tushmaydi)."""
    require_admin(auth(x_init_data))
    with db() as c:
        r = c.execute("UPDATE users SET balance=GREATEST(balance-%s,0) WHERE id=%s "
                      "RETURNING balance", (g.amount, g.user_id)).fetchone()
    if not r:
        raise HTTPException(404, "Foydalanuvchi topilmadi")
    return {"balance": r["balance"]}


class Del(BaseModel):
    id: int


@app.get("/api/admin/auctions")
def admin_auctions(x_init_data: str = Header()):
    require_admin(auth(x_init_data))
    with db() as c:
        rows = c.execute("SELECT id, title, status, start_price, top_bid, top_user FROM auctions "
                         "ORDER BY id DESC LIMIT 50").fetchall()
    return [{"id": r["id"], "title": r["title"], "status": r["status"],
             "price": max(r["start_price"], r["top_bid"]),
             "has_bids": r["top_user"] is not None} for r in rows]


@app.post("/api/admin/auction/delete")
def delete_auction(d: Del, x_init_data: str = Header()):
    """Xato yoki eski e'lonni o'chirish. Faol e'londagi stavkalar qatnashchilarga qaytariladi."""
    require_admin(auth(x_init_data))
    with db() as c:
        a = c.execute("SELECT id, status, top_user, top_bid FROM auctions WHERE id=%s FOR UPDATE",
                      (d.id,)).fetchone()
        if not a:
            raise HTTPException(404, "E'lon topilmadi")
        if a["status"] == "active":
            if FRIENDS_MODE:
                for r in c.execute("SELECT user_id, MAX(amount) AS m FROM bids "
                                   "WHERE auction_id=%s GROUP BY user_id", (d.id,)).fetchall():
                    c.execute("UPDATE users SET balance=balance+%s WHERE id=%s",
                              (r["m"], r["user_id"]))
            elif a["top_user"] is not None:
                c.execute("UPDATE users SET balance=balance+%s WHERE id=%s",
                          (a["top_bid"], a["top_user"]))
        c.execute("DELETE FROM bids WHERE auction_id=%s", (d.id,))
        c.execute("DELETE FROM auctions WHERE id=%s", (d.id,))
    return {"ok": True}


@app.post("/api/admin/clear-ended")
def clear_ended(x_init_data: str = Header()):
    require_admin(auth(x_init_data))
    with db() as c:
        c.execute("DELETE FROM bids WHERE auction_id IN "
                  "(SELECT id FROM auctions WHERE status='ended')")
        n = c.execute("DELETE FROM auctions WHERE status='ended' RETURNING id").fetchall()
    return {"deleted": len(n)}


@app.get("/api/rating")
def rating(period: str = "all", x_init_data: str = Header()):
    uid = auth(x_init_data)
    span = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400}.get(period)
    with db() as c:
        if span:
            rows = c.execute(
                "SELECT u.id, u.name, u.photo, SUM(l.amount) AS coins FROM users u "
                "JOIN coin_log l ON l.user_id=u.id "
                "WHERE l.ts>%s AND l.kind IN ('task','referral','topup') "
                "GROUP BY u.id, u.name, u.photo ORDER BY coins DESC, u.id LIMIT 100",
                (int(time.time()) - span,)).fetchall()
        else:
            rows = c.execute("SELECT id, name, photo, balance AS coins FROM users "
                             "ORDER BY balance DESC, id LIMIT 100").fetchall()
    return [{"name": r["name"] or "Foydalanuvchi", "photo": r["photo"], "coins": r["coins"],
             "me": r["id"] == uid} for r in rows]


def norm_chat(v):
    v = (v or "").strip()
    m = re.match(r"^(?:https?://)?t\.me/([A-Za-z0-9_]{4,})/?$", v)
    if m:
        return "@" + m.group(1)
    return v if re.match(r"^@[A-Za-z0-9_]{4,}$", v) else None


@app.get("/api/tasks")
def tasks(x_init_data: str = Header()):
    uid = auth(x_init_data)
    with db() as c:
        rows = c.execute(
            "SELECT t.id, t.title, t.descr, t.reward, t.chat, k.status FROM tasks t "
            "LEFT JOIN task_claims k ON k.task_id=t.id AND k.user_id=%s "
            "WHERE t.active ORDER BY t.id DESC", (uid,)).fetchall()
    return [{"id": r["id"], "title": r["title"], "descr": r["descr"], "reward": r["reward"],
             "status": r["status"],
             "url": ("https://t.me/" + r["chat"][1:]) if r["chat"] else ""} for r in rows]


class Claim(BaseModel):
    id: int


@app.post("/api/tasks/check")
def check_task(cl: Claim, x_init_data: str = Header()):
    uid = auth(x_init_data)
    with db() as c:
        t = c.execute("SELECT id, reward, chat FROM tasks WHERE id=%s AND active",
                      (cl.id,)).fetchone()
        done = c.execute("SELECT 1 FROM task_claims WHERE task_id=%s AND user_id=%s "
                         "AND status='approved'", (cl.id, uid)).fetchone()
    if not t:
        raise HTTPException(404, "Vazifa topilmadi")
    if done:
        raise HTTPException(400, "Bu vazifa allaqachon bajarilgan")
    if not t["chat"]:
        raise HTTPException(400, "Bu vazifa uchun tekshiruv sozlanmagan")
    r = tg("getChatMember", chat_id=t["chat"], user_id=uid)
    if not r or not r.get("ok"):
        raise HTTPException(400, "Tekshirib bo'lmadi: bot kanalda admin emas yoki nomi noto'g'ri")
    m = r["result"]
    if not (m["status"] in ("creator", "administrator", "member")
            or (m["status"] == "restricted" and m.get("is_member"))):
        raise HTTPException(400, "Siz hali obuna bo'lmagansiz")
    with db() as c:
        k = c.execute("INSERT INTO task_claims(task_id,user_id,status,ts) VALUES(%s,%s,'approved',%s) "
                      "ON CONFLICT (task_id,user_id) DO UPDATE SET status='approved', ts=EXCLUDED.ts "
                      "WHERE task_claims.status<>'approved' RETURNING id",
                      (t["id"], uid, int(time.time()))).fetchone()
        if k:
            c.execute("UPDATE users SET balance=balance+%s WHERE id=%s", (t["reward"], uid))
            log_coin(c, uid, t["reward"], "task")
    if not k:
        raise HTTPException(400, "Bu vazifa allaqachon bajarilgan")
    return {"reward": t["reward"]}


class TaskIn(BaseModel):
    title: str
    descr: str = ""
    reward: int
    chat: str = ""


@app.post("/api/admin/task")
def new_task(t: TaskIn, x_init_data: str = Header()):
    require_admin(auth(x_init_data))
    chat = norm_chat(t.chat)
    if not t.title.strip() or t.reward < 0 or t.reward > 1_000_000:
        raise HTTPException(400, "Ma'lumot noto'g'ri")
    if not chat:
        raise HTTPException(400, "Kanal yoki guruh @username yoki t.me/username ko'rinishida bo'lsin")
    with db() as c:
        c.execute("INSERT INTO tasks(title,descr,reward,chat) VALUES(%s,%s,%s,%s)",
                  (t.title.strip(), t.descr.strip(), t.reward, chat))
    return {"ok": True}


@app.post("/api/admin/task/delete")
def del_task(d: Del, x_init_data: str = Header()):
    require_admin(auth(x_init_data))
    with db() as c:
        c.execute("UPDATE tasks SET active=FALSE WHERE id=%s", (d.id,))
    return {"ok": True}
