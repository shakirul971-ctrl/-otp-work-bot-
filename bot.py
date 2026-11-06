import os, re, sqlite3, logging
from decimal import Decimal, InvalidOperation
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DB_PATH", "/data/bot.db")
MIN_WITHDRAW = Decimal(os.getenv("MIN_WITHDRAW", "5"))
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("otp-work-bot")

def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
          referred_by INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tasks(
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
          description TEXT NOT NULL, active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS submissions(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          task_id INTEGER NOT NULL, proof TEXT NOT NULL,
          status TEXT DEFAULT 'pending', reward TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS ledger(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          amount TEXT NOT NULL, kind TEXT NOT NULL, note TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS withdrawals(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          amount TEXT NOT NULL, wallet TEXT NOT NULL,
          status TEXT DEFAULT 'pending', txid TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
def is_admin(uid): return uid in ADMIN_IDS
def balance(uid):
    with db() as c:
        rows=c.execute("SELECT amount,kind FROM ledger WHERE user_id=?",(uid,)).fetchall()
    total=Decimal("0")
    for r in rows:
        a=Decimal(r["amount"])
        total += a if r["kind"]=="credit" else -a
    return total
def money(x):
    try:
        d=Decimal(x)
        if not d.is_finite() or d <= 0: raise ValueError()
        return d
    except (InvalidOperation, ValueError):
        raise ValueError("সঠিক ধনাত্মক পরিমাণ লিখুন।")
def add_ledger(uid, amount, kind, note):
    with db() as c:
        c.execute("INSERT INTO ledger(user_id,amount,kind,note) VALUES(?,?,?,?)",
                  (uid,str(amount),kind,note))
def register(update, ref=None):
    u=update.effective_user
    with db() as c:
        exists=c.execute("SELECT user_id FROM users WHERE user_id=?",(u.id,)).fetchone()
        if not exists:
            if ref == u.id: ref=None
            c.execute("INSERT INTO users(user_id,username,first_name,referred_by) VALUES(?,?,?,?)",
                      (u.id,u.username or "",u.first_name or "",ref))
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ref=None
    if context.args and context.args[0].startswith("ref"):
        try: ref=int(context.args[0][3:])
        except ValueError: pass
    register(update, ref)
    await update.message.reply_text(
        "স্বাগতম! এখানে অনুমোদিত সাধারণ টাস্ক জমা দিয়ে রিওয়ার্ডের আবেদন করা যায়।\n\n"
        "/tasks — উপলভ্য টাস্ক\n/profile — প্রোফাইল\n/balance — ব্যালেন্স\n"
        "/history — ইতিহাস\n/referral — রেফারেল লিংক\n/withdraw — উত্তোলনের নিয়ম\n/help — সাহায্য")
async def help_cmd(update, context):
    await update.message.reply_text(
        "কাজের নিয়ম:\n1) /tasks দিয়ে টাস্ক দেখুন\n2) /submit TASK_ID প্রমাণ লিখুন\n"
        "3) Admin যাচাই করলে অনুমোদিত রিওয়ার্ড ব্যালেন্সে যোগ হবে।\n"
        f"উত্তোলনের সর্বনিম্ন পরিমাণ {MIN_WITHDRAW} USDT।\n"
        "কোনো অ্যাকাউন্টের লগইন কোড/OTP, পাসওয়ার্ড বা 2FA কোড জমা দেবেন না।")
async def tasks_cmd(update, context):
    with db() as c: rows=c.execute("SELECT * FROM tasks WHERE active=1 ORDER BY id").fetchall()
    if not rows: return await update.message.reply_text("এখন কোনো টাস্ক নেই।")
    msg="\n\n".join(f"#{r['id']} — {r['title']}\n{r['description']}" for r in rows)
    await update.message.reply_text(msg)
async def submit(update, context):
    if len(context.args)<2:
        return await update.message.reply_text("ব্যবহার: /submit TASK_ID প্রমাণ/বর্ণনা\nকোনো OTP বা গোপন কোড দেবেন না।")
    try: tid=int(context.args[0])
    except ValueError: return await update.message.reply_text("Task ID সঠিক নয়।")
    proof=" ".join(context.args[1:]).strip()
    if re.search(r'(?i)\b(otp|one.time.password|verification code|2fa|password)\b', proof):
        return await update.message.reply_text("নিরাপত্তার কারণে OTP, পাসওয়ার্ড বা ভেরিফিকেশন কোড গ্রহণ করা হয় না।")
    with db() as c:
        task=c.execute("SELECT id FROM tasks WHERE id=? AND active=1",(tid,)).fetchone()
        if not task: return await update.message.reply_text("টাস্ক পাওয়া যায়নি বা বন্ধ।")
        cur=c.execute("INSERT INTO submissions(user_id,task_id,proof) VALUES(?,?,?)",(update.effective_user.id,tid,proof))
        sid=cur.lastrowid
    await update.message.reply_text(f"সাবমিশন #{sid} জমা হয়েছে। Admin যাচাই করবেন।")
    for aid in ADMIN_IDS:
        try: await context.bot.send_message(aid, f"নতুন সাবমিশন #{sid}\nUser: {update.effective_user.id}\nTask: {tid}\nপ্রমাণ: {proof}\nApprove: /approve_task {sid} AMOUNT\nReject: /reject_task {sid} কারণ")
        except Exception: pass
async def profile(update, context):
    u=update.effective_user
    with db() as c:
        n=c.execute("SELECT COUNT(*) n FROM submissions WHERE user_id=?",(u.id,)).fetchone()["n"]
    await update.message.reply_text(f"User ID: {u.id}\nনাম: {u.full_name}\nসাবমিশন: {n}\nব্যালেন্স: {balance(u.id)} USDT")
async def balance_cmd(update, context):
    await update.message.reply_text(f"আপনার ব্যালেন্স: {balance(update.effective_user.id)} USDT\nNetwork: BEP20")
async def history(update, context):
    uid=update.effective_user.id
    with db() as c:
        rows=c.execute("SELECT kind,amount,note,created_at FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 10",(uid,)).fetchall()
        subs=c.execute("SELECT id,task_id,status,reward FROM submissions WHERE user_id=? ORDER BY id DESC LIMIT 10",(uid,)).fetchall()
    a=["সাম্প্রতিক লেনদেন:"]+[f"{r['kind']} {r['amount']} — {r['note'] or ''} ({r['created_at']})" for r in rows]
    a+=["\nসাম্প্রতিক টাস্ক:"]+[f"#{r['id']} টাস্ক {r['task_id']}: {r['status']} / {r['reward'] or '-'}" for r in subs]
    await update.message.reply_text("\n".join(a))
async def referral(update, context):
    uid=update.effective_user.id
    with db() as c: n=c.execute("SELECT COUNT(*) n FROM users WHERE referred_by=?",(uid,)).fetchone()["n"]
    me=await context.bot.get_me()
    await update.message.reply_text(f"আপনার রেফারেল লিংক:\nhttps://t.me/{me.username}?start=ref{uid}\nরেফার করা ইউজার: {n}\n(রেফারেল বোনাস বর্তমানে চালু নেই।)")
async def withdraw(update, context):
    if len(context.args)!=2:
        return await update.message.reply_text("ব্যবহার: /withdraw AMOUNT BEP20_WALLET_ADDRESS\nউদাহরণ: /withdraw 5 0x... (নিজের ঠিকানা ব্যবহার করুন)")
    try: amount=money(context.args[0])
    except ValueError as e: return await update.message.reply_text(str(e))
    wallet=context.args[1]
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}",wallet):
        return await update.message.reply_text("BEP20 wallet address সঠিক নয়। 0x দিয়ে শুরু হওয়া 42 অক্ষরের ঠিকানা দিন।")
    if amount<MIN_WITHDRAW: return await update.message.reply_text(f"সর্বনিম্ন উত্তোলন {MIN_WITHDRAW} USDT।")
    if balance(update.effective_user.id)<amount: return await update.message.reply_text("পর্যাপ্ত ব্যালেন্স নেই।")
    with db() as c:
        cur=c.execute("INSERT INTO withdrawals(user_id,amount,wallet) VALUES(?,?,?)",(update.effective_user.id,str(amount),wallet))
        wid=cur.lastrowid
    add_ledger(update.effective_user.id,amount,"debit",f"Withdrawal request #{wid}")
    await update.message.reply_text(f"উত্তোলন আবেদন #{wid} জমা হয়েছে। Admin যাচাই করবেন।")
    for aid in ADMIN_IDS:
        try: await context.bot.send_message(aid,f"Withdrawal #{wid}\nUser: {update.effective_user.id}\nAmount: {amount} USDT\nBEP20: {wallet}\nপেমেন্ট সম্পন্ন হলে: /paid {wid} TXID\nReject/refund: /refund {wid} কারণ")
        except Exception: pass
async def addtask(update, context):
    if not is_admin(update.effective_user.id): return
    raw=" ".join(context.args)
    if "|" not in raw: return await update.message.reply_text("ব্যবহার: /addtask শিরোনাম | বিবরণ")
    title,desc=[x.strip() for x in raw.split("|",1)]
    with db() as c: cur=c.execute("INSERT INTO tasks(title,description) VALUES(?,?)",(title,desc))
    await update.message.reply_text(f"টাস্ক #{cur.lastrowid} তৈরি হয়েছে।")
async def approve_task(update, context):
    if not is_admin(update.effective_user.id): return
    if len(context.args)!=2: return await update.message.reply_text("ব্যবহার: /approve_task SUBMISSION_ID AMOUNT")
    try: sid=int(context.args[0]); amt=money(context.args[1])
    except ValueError: return await update.message.reply_text("ID বা পরিমাণ সঠিক নয়।")
    with db() as c:
        r=c.execute("SELECT * FROM submissions WHERE id=? AND status='pending'",(sid,)).fetchone()
        if not r: return await update.message.reply_text("Pending submission পাওয়া যায়নি।")
        c.execute("UPDATE submissions SET status='approved',reward=? WHERE id=?",(str(amt),sid))
    add_ledger(r["user_id"],amt,"credit",f"Task submission #{sid} approved")
    await update.message.reply_text(f"সাবমিশন #{sid} অনুমোদিত। {amt} USDT যোগ হয়েছে।")
    try: await context.bot.send_message(r["user_id"],f"আপনার টাস্ক #{sid} অনুমোদিত। {amt} USDT যোগ হয়েছে।")
    except Exception: pass
async def reject_task(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: return await update.message.reply_text("ব্যবহার: /reject_task SUBMISSION_ID কারণ")
    try: sid=int(context.args[0])
    except ValueError: return
    reason=" ".join(context.args[1:]) or "নীতিমালা অনুযায়ী অনুমোদিত হয়নি"
    with db() as c:
        r=c.execute("SELECT * FROM submissions WHERE id=? AND status='pending'",(sid,)).fetchone()
        if not r: return await update.message.reply_text("Pending submission পাওয়া যায়নি।")
        c.execute("UPDATE submissions SET status='rejected' WHERE id=?",(sid,))
    await update.message.reply_text(f"সাবমিশন #{sid} বাতিল হয়েছে।")
    try: await context.bot.send_message(r["user_id"],f"সাবমিশন #{sid} বাতিল: {reason}")
    except Exception: pass
async def pending(update, context):
    if not is_admin(update.effective_user.id): return
    with db() as c:
        s=c.execute("SELECT id,user_id,task_id FROM submissions WHERE status='pending' ORDER BY id").fetchall()
        w=c.execute("SELECT id,user_id,amount,wallet FROM withdrawals WHERE status='pending' ORDER BY id").fetchall()
    msg=["Pending submissions:"]+[f"#{r['id']} user={r['user_id']} task={r['task_id']}" for r in s]
    msg+=["\nPending withdrawals:"]+[f"#{r['id']} user={r['user_id']} amount={r['amount']} wallet={r['wallet']}" for r in w]
    await update.message.reply_text("\n".join(msg))
async def paid(update, context):
    if not is_admin(update.effective_user.id): return
    if len(context.args)!=2: return await update.message.reply_text("ব্যবহার: /paid WITHDRAWAL_ID TXID")
    try: wid=int(context.args[0])
    except ValueError: return
    txid=context.args[1]
    with db() as c:
        r=c.execute("SELECT * FROM withdrawals WHERE id=? AND status='pending'",(wid,)).fetchone()
        if not r: return await update.message.reply_text("Pending withdrawal পাওয়া যায়নি।")
        c.execute("UPDATE withdrawals SET status='paid',txid=? WHERE id=?",(txid,wid))
    await update.message.reply_text(f"Withdrawal #{wid} paid হিসেবে চিহ্নিত। বাস্তবে TXID যাচাই করে নিন।")
    try: await context.bot.send_message(r["user_id"],f"আপনার withdrawal #{wid} paid হয়েছে। TXID: {txid}")
    except Exception: pass
async def refund(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: return await update.message.reply_text("ব্যবহার: /refund WITHDRAWAL_ID কারণ")
    try: wid=int(context.args[0])
    except ValueError: return
    reason=" ".join(context.args[1:]) or "আবেদন বাতিল"
    with db() as c:
        r=c.execute("SELECT * FROM withdrawals WHERE id=? AND status='pending'",(wid,)).fetchone()
        if not r: return await update.message.reply_text("Pending withdrawal পাওয়া যায়নি।")
        c.execute("UPDATE withdrawals SET status='rejected' WHERE id=?",(wid,))
    add_ledger(r["user_id"],Decimal(r["amount"]),"credit",f"Refund for withdrawal #{wid}")
    await update.message.reply_text(f"Withdrawal #{wid} বাতিল; ব্যালেন্স ফেরত দেওয়া হয়েছে।")
    try: await context.bot.send_message(r["user_id"],f"Withdrawal #{wid} বাতিল: {reason}। ব্যালেন্স ফেরত দেওয়া হয়েছে।")
    except Exception: pass

def main():
    if not TOKEN: raise RuntimeError("BOT_TOKEN environment variable is missing")
    if not ADMIN_IDS: raise RuntimeError("ADMIN_IDS environment variable is missing")
    init_db()
    app=Application.builder().token(TOKEN).build()
    handlers=[
      ("start",start),("help",help_cmd),("tasks",tasks_cmd),("submit",submit),
      ("profile",profile),("balance",balance_cmd),("history",history),
      ("referral",referral),("withdraw",withdraw),("addtask",addtask),
      ("approve_task",approve_task),("reject_task",reject_task),("pending",pending),
      ("paid",paid),("refund",refund)]
    for cmd,fn in handlers: app.add_handler(CommandHandler(cmd,fn))
    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__": main()
