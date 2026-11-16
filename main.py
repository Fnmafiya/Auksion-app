import os, re, json, hmac, hashlib, time, asyncio, base64
import html as _html
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
        # yangi qoida: har kim o'zining eng katta stavkasini "band" qiladi (eski qoidada faqat yetakchiniki)
        c.execute("ALTER TABLE auctions ADD COLUMN IF NOT EXISTS hold_all BOOLEAN")
        if not FRIENDS_MODE:
            # eski faol auksionlarda yetakchi bo'lmaganlarning stavkalari allaqachon qaytarilgan edi
            c.execute("DELETE FROM bids WHERE auction_id IN (SELECT id FROM auctions "
                      "WHERE hold_all IS NULL AND status='active') AND user_id IS DISTINCT FROM "
                      "(SELECT a.top_user FROM auctions a WHERE a.id=bids.auction_id)")
        c.execute("UPDATE auctions SET hold_all=TRUE WHERE hold_all IS NULL")
        c.execute("ALTER TABLE auctions ALTER COLUMN hold_all SET DEFAULT TRUE")
        c.execute("""CREATE TABLE IF NOT EXISTS duels(
            id BIGSERIAL PRIMARY KEY, p1 BIGINT, p2 BIGINT, m1 TEXT, m2 TEXT,
            status TEXT DEFAULT 'waiting', winner BIGINT, ts BIGINT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS checkers_games(
            id BIGSERIAL PRIMARY KEY, p1 BIGINT NOT NULL, p2 BIGINT,
            stake BIGINT NOT NULL, board JSONB NOT NULL, turn BIGINT NOT NULL,
            status TEXT DEFAULT 'waiting', winner BIGINT, ts BIGINT NOT NULL,
            updated_at BIGINT NOT NULL, move_no BIGINT DEFAULT 0)""")
        c.execute("CREATE INDEX IF NOT EXISTS checkers_waiting ON checkers_games(status, stake, ts)")
        c.execute("""CREATE TABLE IF NOT EXISTS checkers_voice(
            id BIGSERIAL PRIMARY KEY, game_id BIGINT NOT NULL, from_uid BIGINT NOT NULL,
            to_uid BIGINT NOT NULL, payload JSONB NOT NULL, ts BIGINT NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS checkers_voice_game ON checkers_voice(game_id, id)")
        c.execute("""CREATE TABLE IF NOT EXISTS platform_ledger(
            id BIGSERIAL PRIMARY KEY, game_id BIGINT, amount BIGINT NOT NULL,
            kind TEXT NOT NULL, ts BIGINT NOT NULL)""")


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


def esc_h(t):
    return _html.escape(str(t), quote=False)


def fmt(n):
    return f"{int(n):,}".replace(",", " ")


def card(chat_id, text, rows=None):
    """Chiroyli (HTML) xabar va ixtiyoriy tugmalar yuboradi."""
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": "true"}
    rows = [r for r in (rows or []) if r]
    if rows:
        p["reply_markup"] = json.dumps({"inline_keyboard": rows})
    return tg("sendMessage", **p)


def open_btn():
    base = os.environ.get("BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if not base:
        return []
    return [{"text": "🔨  Auksionni ochish", "web_app": {"url": base}, "style": "primary"}]


def admin_btn(text):
    name = admin_username()
    if not name:
        return []
    return [{"text": "💬  Admin bilan bog'lanish", "style": "primary",
             "url": f"https://t.me/{name}?" + urlencode({"text": text})}]


def topup_rows(tid, uid):
    return [[{"text": "✅  Tasdiqlash", "callback_data": f"ok:{tid}", "style": "success"},
             {"text": "❌  Rad etish", "callback_data": f"no:{tid}", "style": "danger"}],
            [{"text": "💬  Foydalanuvchi bilan chat", "url": f"tg://user?id={uid}"}]]


def topup_text(tid, name, uid, coins):
    return (f"<b>💰 Yangi coin so'rovi</b>  <code>#{tid}</code>\n"
            f"━━━━━━━━━━━━━━\n"
            f"👤 <b>{esc_h(name)}</b>  ·  <code>{uid}</code>\n"
            f"🪙 Miqdor: <b>{fmt(coins)} coin</b>\n"
            f"💵 To'lov: <b>{fmt(coins * COIN_PRICE)} so'm</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"<i>To'lovni lichkada olgach, tasdiqlang.</i>")


def set_webhook():
    base = os.environ.get("BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if base:
        tg("setWebhook", url=base.rstrip("/") + "/webhook", secret_token=WEBHOOK_SECRET,
           allowed_updates=json.dumps(["message", "callback_query"]))


def close_expired():
    with db() as c:
        rows = c.execute("UPDATE auctions SET status='ended' "
                         "WHERE status='active' AND ends_at<=%s "
                         "RETURNING id, title, top_bid, top_user", (int(time.time()),)).fetchall()
        for r in rows:
            bids = c.execute("SELECT user_id, MAX(amount) AS m FROM bids WHERE auction_id=%s "
                             "GROUP BY user_id", (r["id"],)).fetchall()
            if FRIENDS_MODE:
                spent = sum(b["m"] for b in bids)  # do'stlar rejimi: hamma stavka sarflanadi
            else:
                for b in bids:  # yutqazganlarga coin qaytariladi
                    if b["user_id"] != r["top_user"]:
                        c.execute("UPDATE users SET balance=balance+%s WHERE id=%s",
                                  (b["m"], b["user_id"]))
                spent = r["top_bid"] if r["top_user"] is not None else 0
            if spent and r["top_user"] is not None:
                log_coin(c, r["top_user"], -spent, "auction")
    for r in rows:
        if r["top_user"] is None:
            continue
        with db() as c:
            c.execute("UPDATE users SET wins=COALESCE(wins,0)+1 WHERE id=%s", (r["top_user"],))
        with db() as c:
            w = c.execute("SELECT name FROM users WHERE id=%s", (r["top_user"],)).fetchone()
        wname = (w["name"] if w else "") or "G'olib"
        card(r["top_user"],
             f"🏆 <b>Tabriklaymiz, siz g'olibsiz!</b>\n\n"
             f"🔨 Auksion: <b>{esc_h(r['title'])}</b>\n"
             f"🪙 Yakuniy stavka: <b>{fmt(r['top_bid'])} coin</b>\n\n"
             f"🎁 <b>Sovg'angizni olish uchun</b> admin bilan bog'laning, pastdagi tugmani bosing 👇",
             [admin_btn(f"Salom! \"{r['title']}\" auksionida yutdim, sovg'amni olmoqchiman."),
              open_btn()])
        for admin in ADMINS:
            card(admin,
                 f"🏁 <b>Auksion yakunlandi</b>\n\n"
                 f"🔨 <b>{esc_h(r['title'])}</b>\n"
                 f"🏆 G'olib: <b>{esc_h(wname)}</b>  ·  <code>{r['top_user']}</code>\n"
                 f"🪙 Stavka: <b>{fmt(r['top_bid'])} coin</b>",
                 [[{"text": "💬  G'olib bilan chat", "url": f"tg://user?id={r['top_user']}"}]])


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
                         "(image IS NOT NULL) AS has_image, "
                         "(SELECT COALESCE(MAX(b.amount),0) FROM bids b "
                         "WHERE b.auction_id=auctions.id AND b.user_id=%s) AS my_bid "
                         "FROM auctions WHERE status='active' ORDER BY ends_at", (uid,)).fetchall()
    return [{
        "id": r["id"], "title": r["title"],
        "price": max(r["start_price"], r["top_bid"]),
        "ends_at": r["ends_at"],
        "is_top": r["top_user"] == uid,
        "has_bids": r["top_user"] is not None,
        "image": bool(r["has_image"]),
        "my_bid": r["my_bid"],
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
        card(r["referrer"],
             f"🎁 <b>Do'stingiz faol bo'ldi!</b>\n\n"
             f"Taklif qilgan do'stingiz birinchi stavkasini qo'ydi.\n"
             f"🪙 Sizga <b>+{fmt(REFERRAL_BONUS)} coin</b> bonus berildi.", [open_btn()])


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
    """Hamma istagancha stavka qo'ya oladi. Eng yuqori stavka yetakchi bo'ladi,
    har kim o'zining eng katta stavkasini auksion tugaguncha band qiladi."""
    if b.amount <= 0:
        raise HTTPException(400, "Miqdor noto'g'ri")
    with db() as c:
        a = c.execute("SELECT id, status, ends_at, start_price, top_bid FROM auctions "
                      "WHERE id=%s FOR UPDATE", (b.auction_id,)).fetchone()
        if not a or a["status"] != "active" or a["ends_at"] <= time.time():
            raise HTTPException(400, "Auksion tugagan")
        if b.amount < a["start_price"]:
            raise HTTPException(400, f"Kamida {a['start_price']} coin")
        prev = c.execute("SELECT COALESCE(MAX(amount),0) AS m FROM bids "
                         "WHERE auction_id=%s AND user_id=%s", (a["id"], uid)).fetchone()["m"]
        if b.amount <= prev:
            raise HTTPException(400, f"Siz allaqachon {prev} coin qo'ygansiz, ko'proq yozing")
        u = c.execute("SELECT balance FROM users WHERE id=%s FOR UPDATE", (uid,)).fetchone()
        cost = b.amount - prev
        if u["balance"] < cost:
            raise HTTPException(400, "Coin yetarli emas")
        c.execute("UPDATE users SET balance=balance-%s WHERE id=%s", (cost, uid))
        leading = b.amount > a["top_bid"]
        if leading:  # faqat eng yuqori stavka yetakchi bo'ladi
            c.execute("UPDATE auctions SET top_bid=%s, top_user=%s WHERE id=%s",
                      (b.amount, uid, a["id"]))
        c.execute("INSERT INTO bids(auction_id,user_id,amount,ts) VALUES(%s,%s,%s,%s)",
                  (a["id"], uid, b.amount, int(time.time())))
    return {"ok": True, "leading": leading}



# --- Onlayn shashka (8x8, rus qoidasi: majburiy urish, dama uzoq yuradi) ---
CHECKERS_MIN_STAKE = 2
CHECKERS_MAX_STAKE = 1_000_000
CHECKERS_FEE_PERCENT = 25


def checkers_initial_board():
    b = [None] * 64
    for r in range(3):
        for col in range(8):
            if (r + col) % 2 == 1:
                b[r * 8 + col] = "b"
    for r in range(5, 8):
        for col in range(8):
            if (r + col) % 2 == 1:
                b[r * 8 + col] = "r"
    return b


def _inside(r, col):
    return 0 <= r < 8 and 0 <= col < 8


def _captures_from(board, start):
    piece = board[start]
    if not piece:
        return []
    side = piece.lower()
    king = piece.isupper()
    sr, sc = divmod(start, 8)
    dirs = [(-1,-1),(-1,1),(1,-1),(1,1)]
    results = []

    def walk(pos, b, path, taken):
        r, col = divmod(pos, 8)
        found = False
        for dr, dc in dirs:
            if king:
                rr, cc = r + dr, col + dc
                enemy = None
                while _inside(rr, cc):
                    ix = rr * 8 + cc
                    if b[ix]:
                        if b[ix].lower() == side or enemy is not None:
                            break
                        enemy = ix
                    elif enemy is not None:
                        nb = b.copy()
                        nb[pos] = None
                        nb[enemy] = None
                        nb[ix] = piece
                        found = True
                        walk(ix, nb, path + [ix], taken + [enemy])
                    rr += dr; cc += dc
            else:
                er, ec = r + dr, col + dc
                lr, lc = r + 2*dr, col + 2*dc
                if _inside(lr, lc) and _inside(er, ec):
                    enemy = er * 8 + ec
                    land = lr * 8 + lc
                    if b[enemy] and b[enemy].lower() != side and not b[land]:
                        nb = b.copy()
                        nb[pos] = None; nb[enemy] = None; nb[land] = piece
                        found = True
                        walk(land, nb, path + [land], taken + [enemy])
        if not found and taken:
            results.append({"path": path, "captures": taken})
    walk(start, board, [start], [])
    return results


def checkers_legal_moves(board, side):
    captures = []
    for i, piece in enumerate(board):
        if piece and piece.lower() == side:
            captures.extend(_captures_from(board, i))
    if captures:
        return captures
    moves = []
    for i, piece in enumerate(board):
        if not piece or piece.lower() != side:
            continue
        r, col = divmod(i, 8)
        dirs = [(-1,-1),(-1,1),(1,-1),(1,1)] if piece.isupper() else ([(-1,-1),(-1,1)] if side == "r" else [(1,-1),(1,1)])
        for dr, dc in dirs:
            rr, cc = r + dr, col + dc
            while _inside(rr, cc) and not board[rr*8+cc]:
                moves.append({"path": [i, rr*8+cc], "captures": []})
                if not piece.isupper():
                    break
                rr += dr; cc += dc
    return moves


def checkers_apply_move(board, move):
    b = board.copy()
    path = move["path"]
    piece = b[path[0]]
    b[path[0]] = None
    for ix in move["captures"]:
        b[ix] = None
    dest = path[-1]
    row = dest // 8
    if piece == "r" and row == 0: piece = "R"
    if piece == "b" and row == 7: piece = "B"
    b[dest] = piece
    return b


def checkers_view(g, uid):
    mine = "red" if g["p1"] == uid else "black"
    opponent = g["p2"] if g["p1"] == uid else g["p1"]
    return {"id": g["id"], "status": g["status"], "stake": g["stake"],
            "board": g["board"], "turn": g["turn"], "mine": mine,
            "opponent": opponent, "winner": g["winner"],
            "move_no": g.get("move_no", 0),
            "pot": g["stake"] * 2,
            "winner_prize": (g["stake"] * 2 * (100-CHECKERS_FEE_PERCENT)) // 100,
            "legal_moves": checkers_legal_moves(g["board"], "r" if g["p1"] == uid else "b")
                if g["status"] == "playing" and g["turn"] == uid else []}


def _checkers_finish(c, g, winner):
    if g["status"] != "playing":
        return
    if winner is None:
        # Durang yoki bekor qilingan o'yin: ikkala stavka ham qaytariladi.
        c.execute("UPDATE users SET balance=balance+%s WHERE id=ANY(%s)",
                  (g["stake"], [g["p1"], g["p2"]]))
    else:
        prize = (g["stake"] * 2 * (100-CHECKERS_FEE_PERCENT)) // 100
        c.execute("UPDATE users SET balance=balance+%s, wins=COALESCE(wins,0)+1 WHERE id=%s",
                  (prize, winner))
        log_coin(c, winner, prize, "checkers_win")
        # Platforma komissiyasi bankning 25 foizi sifatida qayd etiladi.
        fee = g["stake"] * 2 - prize
        c.execute("INSERT INTO platform_ledger(game_id,amount,kind,ts) VALUES(%s,%s,%s,%s)",
                  (g["id"], fee, "checkers_commission", int(time.time())))
    c.execute("UPDATE checkers_games SET status='done', winner=%s, updated_at=%s WHERE id=%s",
              (winner, int(time.time()), g["id"]))
    g["status"], g["winner"] = "done", winner


class CheckersJoin(BaseModel):
    stake: int


class CheckersMove(BaseModel):
    id: int
    path: list[int]


class CheckersSignal(BaseModel):
    payload: dict


class CheckersAction(BaseModel):
    id: int


@app.post("/api/checkers/join")
def checkers_join(j: CheckersJoin, x_init_data: str = Header()):
    uid = auth(x_init_data)
    if j.stake < CHECKERS_MIN_STAKE or j.stake > CHECKERS_MAX_STAKE:
        raise HTTPException(400, f"Stavka {CHECKERS_MIN_STAKE} dan {CHECKERS_MAX_STAKE} coin gacha bo'lishi kerak")
    now = int(time.time())
    with db() as c:
        active = c.execute("SELECT * FROM checkers_games WHERE (p1=%s OR p2=%s) AND status='playing' ORDER BY id DESC LIMIT 1 FOR UPDATE", (uid, uid)).fetchone()
        if active:
            return checkers_view(active, uid)
        mine = c.execute("SELECT * FROM checkers_games WHERE p1=%s AND status='waiting' ORDER BY id DESC LIMIT 1 FOR UPDATE", (uid,)).fetchone()
        if mine:
            return checkers_view(mine, uid)
        # Faqat aynan bir xil stavka qo'ygan raqib bilan juftlaydi.
        waiting = c.execute("SELECT * FROM checkers_games WHERE status='waiting' AND p1<>%s AND stake=%s AND ts>%s ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED",
                            (uid, j.stake, now-180)).fetchone()
        u = c.execute("SELECT balance FROM users WHERE id=%s FOR UPDATE", (uid,)).fetchone()
        if not u or u["balance"] < j.stake:
            raise HTTPException(400, "Balansda coin yetarli emas")
        c.execute("UPDATE users SET balance=balance-%s WHERE id=%s", (j.stake, uid))
        log_coin(c, uid, -j.stake, "checkers_stake")
        if waiting:
            g = c.execute("UPDATE checkers_games SET p2=%s,status='playing',turn=p1,updated_at=%s WHERE id=%s RETURNING *",
                          (uid, now, waiting["id"])).fetchone()
        else:
            g = c.execute("INSERT INTO checkers_games(p1,stake,board,turn,status,ts,updated_at) VALUES(%s,%s,%s,%s,'waiting',%s,%s) RETURNING *",
                          (uid, j.stake, json.dumps(checkers_initial_board()), uid, now, now)).fetchone()
    return checkers_view(g, uid)


@app.get("/api/checkers/{game_id}")
def checkers_state(game_id: int, x_init_data: str = Header()):
    uid = auth(x_init_data)
    now = int(time.time())
    with db() as c:
        g = c.execute("SELECT * FROM checkers_games WHERE id=%s FOR UPDATE", (game_id,)).fetchone()
        if not g or uid not in (g["p1"], g["p2"]):
            raise HTTPException(404, "O'yin topilmadi")
        if g["status"] == "waiting" and g["ts"] < now-180:
            c.execute("UPDATE users SET balance=balance+%s WHERE id=%s", (g["stake"], g["p1"]))
            c.execute("UPDATE checkers_games SET status='expired',updated_at=%s WHERE id=%s", (now, game_id))
            g["status"] = "expired"
        elif g["status"] == "playing" and g["updated_at"] < now-600:
            # 10 daqiqa yurishsiz qolsa, navbatdagi o'yinchi yutqazgan hisoblanadi.
            _checkers_finish(c, g, g["p2"] if g["turn"] == g["p1"] else g["p1"])
        return checkers_view(g, uid)


@app.post("/api/checkers/move")
def checkers_move(m: CheckersMove, x_init_data: str = Header()):
    uid = auth(x_init_data)
    if len(m.path) < 2 or any(i < 0 or i > 63 for i in m.path):
        raise HTTPException(400, "Yurish noto'g'ri")
    now = int(time.time())
    with db() as c:
        g = c.execute("SELECT * FROM checkers_games WHERE id=%s FOR UPDATE", (m.id,)).fetchone()
        if not g or g["status"] != "playing" or uid not in (g["p1"], g["p2"]):
            raise HTTPException(400, "O'yin faol emas")
        if g["turn"] != uid:
            raise HTTPException(400, "Hozir raqibning navbati")
        board = g["board"]
        side = "r" if uid == g["p1"] else "b"
        legal = checkers_legal_moves(board, side)
        move = next((x for x in legal if x["path"] == m.path), None)
        if not move:
            raise HTTPException(400, "Bu yurish qoidaga mos emas yoki majburiy urishni bajarmadingiz")
        board = checkers_apply_move(board, move)
        next_uid = g["p2"] if uid == g["p1"] else g["p1"]
        opp_side = "b" if side == "r" else "r"
        opp_pieces = any(x and x.lower() == opp_side for x in board)
        opp_moves = checkers_legal_moves(board, opp_side) if opp_pieces else []
        if not opp_pieces or not opp_moves:
            c.execute("UPDATE checkers_games SET board=%s,move_no=move_no+1,updated_at=%s WHERE id=%s",
                      (json.dumps(board), now, m.id))
            g["board"] = board
            _checkers_finish(c, g, uid)
        else:
            c.execute("UPDATE checkers_games SET board=%s,turn=%s,move_no=move_no+1,updated_at=%s WHERE id=%s",
                      (json.dumps(board), next_uid, now, m.id))
            g.update(board=board, turn=next_uid, move_no=g["move_no"]+1)
    return checkers_view(g, uid)


@app.post("/api/checkers/cancel")
def checkers_cancel(m: CheckersAction, x_init_data: str = Header()):
    uid = auth(x_init_data)
    with db() as c:
        g = c.execute("SELECT * FROM checkers_games WHERE id=%s FOR UPDATE", (m.id,)).fetchone()
        if not g or g["p1"] != uid or g["status"] != "waiting":
            raise HTTPException(400, "Kutishdagi o'yin topilmadi")
        c.execute("UPDATE users SET balance=balance+%s WHERE id=%s", (g["stake"], uid))
        c.execute("UPDATE checkers_games SET status='cancelled',updated_at=%s WHERE id=%s", (int(time.time()), m.id))
    return {"ok": True}


@app.post("/api/checkers/resign")
def checkers_resign(m: CheckersAction, x_init_data: str = Header()):
    uid = auth(x_init_data)
    with db() as c:
        g = c.execute("SELECT * FROM checkers_games WHERE id=%s FOR UPDATE", (m.id,)).fetchone()
        if not g or g["status"] != "playing" or uid not in (g["p1"], g["p2"]):
            raise HTTPException(400, "O'yin faol emas")
        _checkers_finish(c, g, g["p2"] if uid == g["p1"] else g["p1"])
    return {"ok": True}


@app.post("/api/checkers/voice/{game_id}")
def checkers_voice_send(game_id: int, sig: CheckersSignal, x_init_data: str = Header()):
    uid = auth(x_init_data)
    with db() as c:
        g = c.execute("SELECT p1,p2,status FROM checkers_games WHERE id=%s", (game_id,)).fetchone()
        if not g or uid not in (g["p1"], g["p2"]) or g["status"] != "playing":
            raise HTTPException(403, "Ovozli aloqa uchun o'yin faol bo'lishi kerak")
        other = g["p2"] if uid == g["p1"] else g["p1"]
        c.execute("INSERT INTO checkers_voice(game_id,from_uid,to_uid,payload,ts) VALUES(%s,%s,%s,%s,%s)",
                  (game_id, uid, other, json.dumps(sig.payload), int(time.time())))
    return {"ok": True}


@app.get("/api/checkers/voice/{game_id}")
def checkers_voice_get(game_id: int, after: int = 0, x_init_data: str = Header()):
    uid = auth(x_init_data)
    with db() as c:
        g = c.execute("SELECT p1,p2,status FROM checkers_games WHERE id=%s", (game_id,)).fetchone()
        if not g or uid not in (g["p1"], g["p2"]):
            raise HTTPException(403, "Ruxsat yo'q")
        rows = c.execute("SELECT id,payload FROM checkers_voice WHERE game_id=%s AND to_uid=%s AND id>%s ORDER BY id LIMIT 50",
                         (game_id, uid, after)).fetchall()
    return [{"id": r["id"], "payload": r["payload"]} for r in rows]


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
    for admin in ADMINS:
        card(admin, topup_text(row["id"], u["name"] or "Foydalanuvchi", uid, t.coins),
             topup_rows(row["id"], uid))
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
    bot = bot_username()
    share = []
    if bot:
        link = f"https://t.me/{bot}?start=ref_{uid}"
        share = [{"text": "🎁  Do'stlarni taklif qilish",
                  "url": "https://t.me/share/url?" + urlencode(
                      {"url": link, "text": f"Auksionga qo'shiling va {WELCOME_BONUS} coin sovg'a oling!"})}]
    name = esc_h(m["from"].get("first_name", "") or "do'stim")
    card(uid,
         f"<b>🔨 Auksionga xush kelibsiz, {name}!</b>\n\n"
         f"💎 Noyob mahsulotlarga <b>stavka qo'ying</b>\n"
         f"✅ Vazifalarni bajarib <b>coin yig'ing</b>\n"
         f"🎁 Do'stlaringizni taklif qilib <b>bonus oling</b>\n"
         f"🏆 Reytingda <b>peshqadam</b> bo'ling\n\n"
         f"<i>Boshlash uchun pastdagi tugmani bosing 👇</i>",
         [open_btn(), share])


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
        cap = topup_text(t["id"], m["from"].get("first_name", "") or "Foydalanuvchi", uid, t["coins"])
        kb = json.dumps({"inline_keyboard": topup_rows(t["id"], uid)})
        for admin in ADMINS:
            tg("copyMessage", chat_id=admin, from_chat_id=uid, message_id=m["message_id"],
               caption=cap, parse_mode="HTML", reply_markup=kb)
        card(uid, f"📨 <b>Chek qabul qilindi</b>  <code>#{t['id']}</code>\n\nAdmin tekshirmoqda, tez orada javob beramiz.")
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
        card(t["user_id"],
             f"✅ <b>To'lov tasdiqlandi!</b>\n\n"
             f"🪙 Hisobingizga <b>+{fmt(t['coins'])} coin</b> qo'shildi.\nOmad tilaymiz! 🍀",
             [open_btn()])
        head = "✅ <b>TASDIQLANDI</b>"
    else:
        card(t["user_id"],
             f"❌ <b>So'rov rad etildi</b>  <code>#{tid}</code>\n\n"
             f"Savollaringiz bo'lsa, admin bilan bog'laning 👇",
             [admin_btn("Salom! Coin so'rovim bo'yicha savolim bor.")])
        head = "❌ <b>RAD ETILDI</b>"
    msg = cb.get("message")
    if msg:
        txt = f"{head}  <code>#{tid}</code>\n🪙 {fmt(t['coins'])} coin  ·  👤 <code>{t['user_id']}</code>"
        ids = dict(chat_id=msg["chat"]["id"], message_id=msg["message_id"], parse_mode="HTML")
        if not tg("editMessageText", text=txt, **ids):
            tg("editMessageCaption", caption=txt, **ids)


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
        log_coin(c, g.user_id, g.amount, "admin_give")
    return {"ok": True}


@app.post("/api/admin/take")
def take(g: Give, x_init_data: str = Header()):
    """Admin: foydalanuvchi balansidan coin ayirish (balans 0 dan pastga tushmaydi)."""
    require_admin(auth(x_init_data))
    with db() as c:
        row = c.execute("SELECT balance FROM users WHERE id=%s FOR UPDATE", (g.user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Foydalanuvchi topilmadi")
        removed = min(row["balance"], max(g.amount, 0))
        c.execute("UPDATE users SET balance=balance-%s WHERE id=%s", (removed, g.user_id))
        if removed:
            log_coin(c, g.user_id, -removed, "admin_take")
    return {"balance": row["balance"] - removed}


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
        if a["status"] == "active":  # hamma qatnashchiga o'z stavkasi qaytariladi
            for r in c.execute("SELECT user_id, MAX(amount) AS m FROM bids "
                               "WHERE auction_id=%s GROUP BY user_id", (d.id,)).fetchall():
                c.execute("UPDATE users SET balance=balance+%s WHERE id=%s",
                          (r["m"], r["user_id"]))
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


@app.get("/api/admin/stats")
def admin_stats(x_init_data: str = Header()):
    """Diagnostika: coinlar qancha berilgan, chiqib ketgan, qolgan va auksionlarda band."""
    require_admin(auth(x_init_data))
    with db() as c:
        u = c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(balance),0) AS bal FROM users").fetchone()
        held = c.execute(
            "SELECT COALESCE(SUM(m),0) AS h FROM (SELECT MAX(b.amount) AS m FROM bids b "
            "JOIN auctions a ON a.id=b.auction_id WHERE a.status='active' "
            "GROUP BY b.auction_id, b.user_id) x").fetchone()["h"]
        kinds = c.execute("SELECT kind, SUM(amount) AS s FROM coin_log WHERE amount>0 "
                          "GROUP BY kind").fetchall()
        out = c.execute("SELECT COALESCE(SUM(-amount),0) AS s FROM coin_log WHERE amount<0").fetchone()["s"]
        act = c.execute("SELECT COUNT(*) AS n FROM auctions WHERE status='active'").fetchone()["n"]
        pend = c.execute("SELECT COUNT(*) AS n FROM topups WHERE status='pending'").fetchone()["n"]
    issued = {r["kind"]: int(r["s"]) for r in kinds}
    total = sum(issued.values())
    return {"users": u["n"], "balance": int(u["bal"]), "held": int(held), "issued": issued,
            "issued_total": total, "out": int(out), "active": act, "pending_topups": pend,
            "gap": total - int(out) - int(u["bal"]) - int(held)}
