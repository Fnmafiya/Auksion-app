import os, json, hmac, hashlib, time, sqlite3, asyncio
from urllib.parse import parse_qsl
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
DB = "auction.db"


def db():
    c = sqlite3.connect(DB, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, name TEXT, balance INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS auctions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, start_price INTEGER,
        ends_at INTEGER, top_bid INTEGER DEFAULT 0, top_user INTEGER,
        status TEXT DEFAULT 'active');
    CREATE TABLE IF NOT EXISTS bids(
        id INTEGER PRIMARY KEY AUTOINCREMENT, auction_id INTEGER,
        user_id INTEGER, amount INTEGER, ts INTEGER);
    """)
    c.close()


async def closer():
    while True:
        c = db()
        c.execute("UPDATE auctions SET status='ended' WHERE status='active' AND ends_at<=?",
                  (int(time.time()),))
        c.close()
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app):
    init_db()
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
    c = db()
    c.execute("INSERT INTO users(id,name) VALUES(?,?) "
              "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
              (u["id"], u.get("first_name", "")))
    c.close()
    return u["id"]


@app.get("/")
def index():
    return FileResponse("index.html")


@app.get("/api/me")
def me(x_init_data: str = Header()):
    uid = auth(x_init_data)
    c = db()
    row = c.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()
    c.close()
    return {"id": uid, "balance": row["balance"], "is_admin": uid in ADMINS}


@app.get("/api/auctions")
def auctions(x_init_data: str = Header()):
    uid = auth(x_init_data)
    c = db()
    rows = c.execute("SELECT * FROM auctions WHERE status='active' "
                     "ORDER BY ends_at").fetchall()
    c.close()
    return [{
        "id": r["id"], "title": r["title"],
        "price": max(r["start_price"], r["top_bid"]),
        "ends_at": r["ends_at"],
        "is_top": r["top_user"] == uid,
        "has_bids": r["top_user"] is not None,
    } for r in rows]


class Bid(BaseModel):
    auction_id: int
    amount: int


@app.post("/api/bid")
def bid(b: Bid, x_init_data: str = Header()):
    uid = auth(x_init_data)
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        a = c.execute("SELECT * FROM auctions WHERE id=?", (b.auction_id,)).fetchone()
        if not a or a["status"] != "active" or a["ends_at"] <= time.time():
            raise HTTPException(400, "Auksion tugagan")
        min_bid = a["start_price"] if a["top_user"] is None else a["top_bid"] + 1
        if b.amount < min_bid:
            raise HTTPException(400, f"Kamida {min_bid} coin")
        bal = c.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()["balance"]
        own = a["top_bid"] if a["top_user"] == uid else 0
        if bal + own < b.amount:
            raise HTTPException(400, "Coin yetarli emas")
        if a["top_user"] is not None:  # oldingi yetakchiga qaytarish
            c.execute("UPDATE users SET balance=balance+? WHERE id=?",
                      (a["top_bid"], a["top_user"]))
        c.execute("UPDATE users SET balance=balance-? WHERE id=?", (b.amount, uid))
        c.execute("UPDATE auctions SET top_bid=?, top_user=? WHERE id=?",
                  (b.amount, uid, a["id"]))
        c.execute("INSERT INTO bids(auction_id,user_id,amount,ts) VALUES(?,?,?,?)",
                  (a["id"], uid, b.amount, int(time.time())))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()
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
    c = db()
    c.execute("INSERT INTO auctions(title,start_price,ends_at) VALUES(?,?,?)",
              (a.title, a.start_price, int(time.time()) + a.minutes * 60))
    c.close()
    return {"ok": True}


@app.post("/api/admin/give")
def give(g: Give, x_init_data: str = Header()):
    require_admin(auth(x_init_data))
    c = db()
    c.execute("INSERT INTO users(id,name,balance) VALUES(?,?,?) "
              "ON CONFLICT(id) DO UPDATE SET balance=balance+?",
              (g.user_id, "", g.amount, g.amount))
    c.close()
    return {"ok": True}
