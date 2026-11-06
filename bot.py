import os, re, sqlite3, logging, time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup)
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          CallbackQueryHandler, MessageHandler, filters)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DB_PATH", "/data/bot.db")
MIN_WITHDRAW = Decimal(os.getenv("MIN_WITHDRAW", "5"))
MAX_AMOUNT = Decimal(os.getenv("MAX_AMOUNT", "100000"))
MAX_PENDING_SUBS = 5
COOLDOWN = 3  # seconds between sensitive commands

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("otp-work-bot")

WALLET_RE = re.compile(r"0x[a-fA-F0-9]{40}")
TXID_RE = re.compile(r"0x[a-fA-F0-9]{64}")
SECRET_RE = re.compile(
    r"(?i)(otp|one[\s._-]*time|verification|verify code|2fa|password|passcode|pin\b|login code)")
_last = {}


# ---------- Database ----------
def connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


@contextmanager
def tx():
    """One atomic transaction; always closes the connection."""
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.execute("COMMIT")
    except BaseException:
        try:
            c.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        c.close()


def init_db():
    c = connect()
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
          referred_by INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS tasks(
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
          description TEXT NOT NULL, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS submissions(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          task_id INTEGER NOT NULL, proof TEXT NOT NULL,
          status TEXT DEFAULT 'pending', reward TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS ledger(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          amount TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN('credit','debit')),
          note TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS withdrawals(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          amount TEXT NOT NULL, wallet TEXT NOT NULL,
          status TEXT DEFAULT 'pending', txid TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS i_led ON ledger(user_id);
        CREATE INDEX IF NOT EXISTS i_sub ON submissions(user_id,status);
        CREATE INDEX IF NOT EXISTS i_wd ON withdrawals(user_id,status);
        """)
        try:
            c.execute("ALTER TABLE tasks ADD COLUMN reward TEXT DEFAULT '0'")
        except sqlite3.OperationalError:
            pass  # column already exists
    finally:
        c.close()


def bal(c, uid):
    total = Decimal("0")
    for r in c.execute("SELECT amount,kind FROM ledger WHERE user_id=?", (uid,)):
        a = Decimal(r["amount"])
        total += a if r["kind"] == "credit" else -a
    return total


def add_ledger(c, uid, amount, kind, note):
    c.execute("INSERT INTO ledger(user_id,amount,kind,note) VALUES(?,?,?,?)",
              (uid, str(amount), kind, note))


# ---------- Helpers ----------
def is_admin(update):
    u, ch = update.effective_user, update.effective_chat
    return bool(u and ch and u.id in ADMIN_IDS and ch.type == "private")


def throttled(uid):
    now = time.monotonic()
    if now - _last.get(uid, 0) < COOLDOWN:
        return True
    _last[uid] = now
    return False


def money(x):
    try:
        d = Decimal(x)
        if not d.is_finite() or d <= 0 or d > MAX_AMOUNT:
            raise ValueError
        q = d.quantize(Decimal("0.01"))
        if q != d:
            raise ValueError
        return q
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid positive amount (max 2 decimal places).")


def clean(text, limit=500):
    """Single line, no tappable fake commands in admin messages."""
    return " ".join(text.split()).replace("/", "\uff0f")[:limit]


async def notify(context, uid, text):
    try:
        await context.bot.send_message(uid, text)
    except Exception:
        log.warning("notify failed for %s", uid)


def register(update, ref=None):
    u = update.effective_user
    with tx() as c:
        if c.execute("SELECT 1 FROM users WHERE user_id=?", (u.id,)).fetchone():
            return
        if ref is not None and (ref == u.id or not c.execute(
                "SELECT 1 FROM users WHERE user_id=?", (ref,)).fetchone()):
            ref = None
        c.execute("INSERT INTO users(user_id,username,first_name,referred_by) VALUES(?,?,?,?)",
                  (u.id, u.username or "", u.first_name or "", ref))


# ---------- User commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ref = None
    if context.args and re.fullmatch(r"ref\d{1,15}", context.args[0]):
        ref = int(context.args[0][3:])
    register(update, ref)
    await update.message.reply_text(
        "Welcome! Here you can submit approved simple tasks and apply for rewards.\n\n"
        "/tasks - available tasks\n/profile - your profile\n/balance - your balance\n"
        "/history - recent activity\n/referral - referral link\n/withdraw - withdraw\n/help - help",
        reply_markup=MAIN_MENU)


async def help_cmd(update, context):
    await update.message.reply_text(
        "How it works:\n1) See tasks with /tasks\n2) /submit TASK_ID proof\n"
        "3) After admin review, the approved reward is added to your balance.\n"
        f"Minimum withdrawal: {MIN_WITHDRAW} USDT (BEP20).\n"
        "Never submit any account login code, OTP, password or 2FA code.")


MAIN_MENU = ReplyKeyboardMarkup(
    [["📋 Tasks", "💰 Balance"], ["📜 History", "💸 Withdraw"], ["👤 Profile", "❓ Help"]],
    resize_keyboard=True)


def task_list_markup():
    with tx() as c:
        rows = c.execute("SELECT id,title,reward FROM tasks WHERE active=1 ORDER BY id").fetchall()
    if not rows:
        return None
    buttons = [[InlineKeyboardButton(f"{r['title']} + ({r['reward'] or '0'}) USDT",
                                     callback_data=f"task:{r['id']}")] for r in rows]
    return InlineKeyboardMarkup(buttons)


async def tasks_cmd(update, context):
    markup = task_list_markup()
    if not markup:
        return await update.message.reply_text("No tasks available right now.")
    await update.message.reply_text("📋 Select a task:", reply_markup=markup)


async def task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "tasks:back":
        markup = task_list_markup()
        if not markup:
            return await q.edit_message_text("No tasks available right now.")
        return await q.edit_message_text("📋 Select a task:", reply_markup=markup)
    m = re.fullmatch(r"task:(\d+)", q.data or "")
    if not m:
        return
    with tx() as c:
        r = c.execute("SELECT * FROM tasks WHERE id=? AND active=1", (int(m.group(1)),)).fetchone()
    if not r:
        return await q.edit_message_text("Task not found or closed.")
    back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="tasks:back")]])
    await q.edit_message_text(
        f"#{r['id']} - {r['title']}\nReward: {r['reward'] or '0'} USDT\n\n{r['description']}\n\n"
        f"To submit:\n/submit {r['id']} your proof\n"
        "Never send any OTP, password or 2FA code.",
        reply_markup=back)


async def submit(update, context):
    uid = update.effective_user.id
    if len(context.args) < 2:
        return await update.message.reply_text(
            "Usage: /submit TASK_ID proof/description\nDo not send any OTP or secret code.")
    if throttled(uid):
        return await update.message.reply_text("Please wait a moment and try again.")
    try:
        tid = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("Invalid Task ID.")
    raw = " ".join(context.args[1:])
    if SECRET_RE.search(raw):
        return await update.message.reply_text(
            "For security, OTPs, passwords and verification codes are not accepted.")
    proof = clean(raw)
    register(update)
    err, sid = None, None
    with tx() as c:
        if not c.execute("SELECT 1 FROM tasks WHERE id=? AND active=1", (tid,)).fetchone():
            err = "Task not found or closed."
        elif c.execute("SELECT COUNT(*) n FROM submissions WHERE user_id=? AND status='pending'",
                       (uid,)).fetchone()["n"] >= MAX_PENDING_SUBS:
            err = "You have too many pending submissions. Please wait for review."
        elif c.execute("SELECT 1 FROM submissions WHERE user_id=? AND task_id=? AND status='pending'",
                       (uid, tid)).fetchone():
            err = "You already have a pending submission for this task."
        else:
            sid = c.execute("INSERT INTO submissions(user_id,task_id,proof) VALUES(?,?,?)",
                            (uid, tid, proof)).lastrowid
    if err:
        return await update.message.reply_text(err)
    await update.message.reply_text(f"Submission #{sid} received. An admin will review it.")
    for aid in ADMIN_IDS:
        await notify(context, aid,
            f"New submission #{sid}\nUser: {uid}\nTask: {tid}\nProof: {proof}\n\n"
            f"Approve: /approve_task {sid} AMOUNT\nReject: /reject_task {sid} reason")


async def profile(update, context):
    u = update.effective_user
    with tx() as c:
        n = c.execute("SELECT COUNT(*) n FROM submissions WHERE user_id=?", (u.id,)).fetchone()["n"]
        b = bal(c, u.id)
    await update.message.reply_text(
        f"User ID: {u.id}\nName: {u.full_name}\nSubmissions: {n}\nBalance: {b} USDT")


async def balance_cmd(update, context):
    with tx() as c:
        b = bal(c, update.effective_user.id)
    await update.message.reply_text(f"Your balance: {b} USDT\nNetwork: BEP20")


async def history(update, context):
    uid = update.effective_user.id
    with tx() as c:
        rows = c.execute("SELECT kind,amount,note,created_at FROM ledger WHERE user_id=? "
                         "ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
        subs = c.execute("SELECT id,task_id,status,reward FROM submissions WHERE user_id=? "
                         "ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
    a = ["Recent transactions:"] + [
        f"{r['kind']} {r['amount']} - {r['note'] or ''} ({r['created_at']})" for r in rows]
    a += ["\nRecent submissions:"] + [
        f"#{r['id']} task {r['task_id']}: {r['status']} / {r['reward'] or '-'}" for r in subs]
    await update.message.reply_text("\n".join(a)[:4000])


async def referral(update, context):
    uid = update.effective_user.id
    with tx() as c:
        n = c.execute("SELECT COUNT(*) n FROM users WHERE referred_by=?", (uid,)).fetchone()["n"]
    me = await context.bot.get_me()
    await update.message.reply_text(
        f"Your referral link:\nhttps://t.me/{me.username}?start=ref{uid}\n"
        f"Users referred: {n}\n(Referral bonus is currently disabled.)")


async def withdraw(update, context):
    uid = update.effective_user.id
    if len(context.args or []) != 2:
        return await update.message.reply_text(
            "Usage: /withdraw AMOUNT BEP20_WALLET_ADDRESS\n"
            "Example: /withdraw 5 0x... (use your own address)")
    if throttled(uid):
        return await update.message.reply_text("Please wait a moment and try again.")
    try:
        amount = money(context.args[0])
    except ValueError as e:
        return await update.message.reply_text(str(e))
    wallet = context.args[1]
    if not WALLET_RE.fullmatch(wallet):
        return await update.message.reply_text(
            "Invalid BEP20 wallet address. It must start with 0x and be 42 characters long.")
    if amount < MIN_WITHDRAW:
        return await update.message.reply_text(f"Minimum withdrawal is {MIN_WITHDRAW} USDT.")
    register(update)
    err, wid = None, None
    with tx() as c:  # check + insert + debit are atomic
        if c.execute("SELECT 1 FROM withdrawals WHERE user_id=? AND status='pending'",
                     (uid,)).fetchone():
            err = "You already have a pending withdrawal request."
        elif bal(c, uid) < amount:
            err = "Insufficient balance."
        else:
            wid = c.execute("INSERT INTO withdrawals(user_id,amount,wallet) VALUES(?,?,?)",
                            (uid, str(amount), wallet)).lastrowid
            add_ledger(c, uid, amount, "debit", f"Withdrawal request #{wid}")
    if err:
        return await update.message.reply_text(err)
    await update.message.reply_text(f"Withdrawal request #{wid} received. An admin will review it.")
    for aid in ADMIN_IDS:
        await notify(context, aid,
            f"Withdrawal #{wid}\nUser: {uid}\nAmount: {amount} USDT\nBEP20: {wallet}\n\n"
            f"After paying: /paid {wid} TXID\nReject/refund: /refund {wid} reason")


# ---------- Admin commands ----------
async def addtask(update, context):
    if not is_admin(update):
        return
    raw = " ".join(context.args)
    parts = [x.strip() for x in raw.split("|")]
    if len(parts) != 3:
        return await update.message.reply_text("Usage: /addtask title | description | reward\nExample: /addtask Join channel | Join and send screenshot | 0.004")
    title, desc, reward_raw = parts
    try:
        reward = money(reward_raw)
    except ValueError as e:
        return await update.message.reply_text(str(e))
    if not title or not desc or len(title) > 60 or len(desc) > 1500:
        return await update.message.reply_text("Title (max 60) and description (max 1500) are required.")
    with tx() as c:
        tid = c.execute("INSERT INTO tasks(title,description,reward) VALUES(?,?,?)",
                        (title, desc, str(reward))).lastrowid
    await update.message.reply_text(f"Task #{tid} created.")


async def approve_task(update, context):
    if not is_admin(update):
        return
    if len(context.args) != 2:
        return await update.message.reply_text("Usage: /approve_task SUBMISSION_ID AMOUNT")
    try:
        sid = int(context.args[0])
        amt = money(context.args[1])
    except ValueError:
        return await update.message.reply_text("Invalid ID or amount.")
    with tx() as c:
        r = c.execute("SELECT * FROM submissions WHERE id=? AND status='pending'", (sid,)).fetchone()
        if r:
            c.execute("UPDATE submissions SET status='approved',reward=? WHERE id=?", (str(amt), sid))
            add_ledger(c, r["user_id"], amt, "credit", f"Task submission #{sid} approved")
    if not r:
        return await update.message.reply_text("Pending submission not found.")
    await update.message.reply_text(f"Submission #{sid} approved. {amt} USDT credited.")
    await notify(context, r["user_id"], f"Your submission #{sid} was approved. {amt} USDT credited.")


async def reject_task(update, context):
    if not is_admin(update):
        return
    if not context.args:
        return await update.message.reply_text("Usage: /reject_task SUBMISSION_ID reason")
    try:
        sid = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("Invalid ID.")
    reason = clean(" ".join(context.args[1:])) or "Not approved under the rules"
    with tx() as c:
        r = c.execute("SELECT * FROM submissions WHERE id=? AND status='pending'", (sid,)).fetchone()
        if r:
            c.execute("UPDATE submissions SET status='rejected' WHERE id=?", (sid,))
    if not r:
        return await update.message.reply_text("Pending submission not found.")
    await update.message.reply_text(f"Submission #{sid} rejected.")
    await notify(context, r["user_id"], f"Submission #{sid} rejected: {reason}")


async def pending(update, context):
    if not is_admin(update):
        return
    with tx() as c:
        s = c.execute("SELECT id,user_id,task_id FROM submissions WHERE status='pending' ORDER BY id").fetchall()
        w = c.execute("SELECT id,user_id,amount,wallet FROM withdrawals WHERE status='pending' ORDER BY id").fetchall()
    msg = ["Pending submissions:"] + [f"#{r['id']} user={r['user_id']} task={r['task_id']}" for r in s]
    msg += ["\nPending withdrawals:"] + [
        f"#{r['id']} user={r['user_id']} amount={r['amount']} wallet={r['wallet']}" for r in w]
    await update.message.reply_text("\n".join(msg)[:4000])


async def paid(update, context):
    if not is_admin(update):
        return
    if len(context.args) != 2:
        return await update.message.reply_text("Usage: /paid WITHDRAWAL_ID TXID")
    try:
        wid = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("Invalid ID.")
    txid = context.args[1]
    if not TXID_RE.fullmatch(txid):
        return await update.message.reply_text("Invalid TXID. Expected 0x followed by 64 hex characters.")
    with tx() as c:
        r = c.execute("SELECT * FROM withdrawals WHERE id=? AND status='pending'", (wid,)).fetchone()
        if r:
            c.execute("UPDATE withdrawals SET status='paid',txid=? WHERE id=?", (txid, wid))
    if not r:
        return await update.message.reply_text("Pending withdrawal not found.")
    await update.message.reply_text(
        f"Withdrawal #{wid} marked as paid. Verify the TXID on the blockchain explorer.")
    await notify(context, r["user_id"], f"Your withdrawal #{wid} has been paid.\nTXID: {txid}")


async def refund(update, context):
    if not is_admin(update):
        return
    if not context.args:
        return await update.message.reply_text("Usage: /refund WITHDRAWAL_ID reason")
    try:
        wid = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("Invalid ID.")
    reason = clean(" ".join(context.args[1:])) or "Request cancelled"
    with tx() as c:
        r = c.execute("SELECT * FROM withdrawals WHERE id=? AND status='pending'", (wid,)).fetchone()
        if r:
            c.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (wid,))
            add_ledger(c, r["user_id"], Decimal(r["amount"]), "credit", f"Refund for withdrawal #{wid}")
    if not r:
        return await update.message.reply_text("Pending withdrawal not found.")
    await update.message.reply_text(f"Withdrawal #{wid} cancelled; balance refunded.")
    await notify(context, r["user_id"], f"Withdrawal #{wid} cancelled: {reason}. Balance refunded.")


async def on_error(update, context):
    log.exception("Unhandled error", exc_info=context.error)


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing")
    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS environment variable is missing")
    init_db()
    app = Application.builder().token(TOKEN).build()
    handlers = [
        ("start", start), ("help", help_cmd), ("tasks", tasks_cmd), ("submit", submit),
        ("profile", profile), ("balance", balance_cmd), ("history", history),
        ("referral", referral), ("withdraw", withdraw), ("addtask", addtask),
        ("approve_task", approve_task), ("reject_task", reject_task), ("pending", pending),
        ("paid", paid), ("refund", refund)]
    for cmd, fn in handlers:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(CallbackQueryHandler(task_callback, pattern=r"^(task:\d+|tasks:back)$"))
    menu_map = {"📋 Tasks": tasks_cmd, "💰 Balance": balance_cmd, "📜 History": history,
                "💸 Withdraw": withdraw, "👤 Profile": profile, "❓ Help": help_cmd}
    for label, fn in menu_map.items():
        app.add_handler(MessageHandler(filters.Text([label]) & ~filters.COMMAND, fn))
    app.add_error_handler(on_error)
    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
