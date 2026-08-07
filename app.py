
import os
import io
import csv
import json
import sqlite3
import secrets
import smtplib
from functools import wraps
from datetime import datetime, date, timedelta
from email.message import EmailMessage
from collections import defaultdict

from flask import (
    Flask, request, render_template, redirect, url_for, flash,
    session, jsonify, send_file
)
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import stripe
except Exception:
    stripe = None

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-in-production")
DATABASE = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "budget_buddy.db"))

PRO_PRICE = 29
FREE_TRANSACTION_LIMIT = 250
PRO_TRANSACTION_LIMIT = 10000

DEFAULT_CATEGORIES = [
    ("Revenue", "income"),
    ("Advertising", "expense"),
    ("Software", "expense"),
    ("Payroll", "expense"),
    ("Contractors", "expense"),
    ("Office", "expense"),
    ("Travel", "expense"),
    ("Taxes", "expense"),
    ("Bank Fees", "expense"),
    ("Other", "expense"),
]

MERCHANTS = {
    "adobe": "Adobe",
    "canva": "Canva",
    "chatgpt": "ChatGPT",
    "openai": "OpenAI",
    "google workspace": "Google Workspace",
    "google": "Google",
    "meta": "Meta",
    "facebook": "Meta",
    "microsoft": "Microsoft",
    "slack": "Slack",
    "notion": "Notion",
    "zoom": "Zoom",
    "hubspot": "HubSpot",
    "semrush": "Semrush",
    "ahrefs": "Ahrefs",
    "quickbooks": "QuickBooks",
    "dropbox": "Dropbox",
    "mailchimp": "Mailchimp",
}


def db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        profile_type TEXT DEFAULT 'general',
        tier TEXT DEFAULT 'FREE',
        email_verified INTEGER DEFAULT 0,
        verify_token TEXT,
        reset_token TEXT,
        reset_expires TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('income','expense')),
        UNIQUE(user_id, name),
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        txn_date TEXT NOT NULL,
        description TEXT NOT NULL,
        amount REAL NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('income','expense')),
        category_id INTEGER,
        notes TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS budgets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        amount REAL NOT NULL,
        month TEXT NOT NULL,
        category_id INTEGER,
        UNIQUE(user_id, name, month),
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
        FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        target REAL NOT NULL,
        current REAL DEFAULT 0,
        deadline TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        read INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """)
    conn.commit()

    for u in conn.execute("SELECT id FROM users").fetchall():
        for name, kind in DEFAULT_CATEGORIES:
            conn.execute(
                "INSERT OR IGNORE INTO categories(user_id,name,kind) VALUES(?,?,?)",
                (u["id"], name, kind)
            )
    conn.commit()
    conn.close()


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return user


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            flash("Please log in first.", "warning")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def pro_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("login"))
        if user["tier"] != "PRO":
            flash("That feature is included in Pro for $29/month.", "info")
            return redirect(url_for("pricing"))
        return fn(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_globals():
    return {"current_user": current_user(), "pro_price": PRO_PRICE}


def send_email(to_email, subject, body):
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("SMTP_FROM", username or "")
    if not all([host, username, password, sender]):
        return False
    try:
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.starttls()
            server.login(username, password)
            server.send_message(msg)
        return True
    except Exception:
        return False


def seed_user_categories(user_id):
    conn = db()
    for name, kind in DEFAULT_CATEGORIES:
        conn.execute(
            "INSERT OR IGNORE INTO categories(user_id,name,kind) VALUES(?,?,?)",
            (user_id, name, kind)
        )
    conn.commit()
    conn.close()


def parse_money(value):
    if value is None:
        return None
    s = str(value).strip().replace("$", "").replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def normalize_kind(raw, amount):
    r = str(raw or "").strip().lower()
    if r in ("income", "credit", "deposit", "revenue"):
        return "income"
    if r in ("expense", "debit", "withdrawal"):
        return "expense"
    return "income" if amount is not None and amount >= 0 else "expense"


def parse_csv(content):
    stream = io.StringIO(content.decode("utf-8", errors="ignore") if isinstance(content, bytes) else content)
    reader = csv.DictReader(stream)
    headers = [h.strip() if h else "" for h in (reader.fieldnames or [])]
    if not headers:
        return [], {}
    lower = {h.lower(): h for h in headers}

    def find(*names):
        for n in names:
            for h in headers:
                if n in h.lower():
                    return h
        return None

    date_col = find("date", "posted", "transaction date")
    desc_col = find("description", "merchant", "memo", "details", "name")
    amount_col = find("amount", "total", "value")
    debit_col = find("debit", "withdrawal")
    credit_col = find("credit", "deposit")
    type_col = find("type", "transaction type")
    category_col = find("category")

    mapping = {
        "date": date_col,
        "description": desc_col,
        "amount": amount_col,
        "debit": debit_col,
        "credit": credit_col,
        "type": type_col,
        "category": category_col,
    }

    rows = []
    for raw in reader:
        desc = (raw.get(desc_col, "") if desc_col else "").strip()
        if not desc:
            desc = "Imported transaction"
        d = (raw.get(date_col, "") if date_col else "").strip()
        if not d:
            d = date.today().isoformat()

        amount = parse_money(raw.get(amount_col)) if amount_col else None
        kind = normalize_kind(raw.get(type_col) if type_col else "", amount)

        if amount is None:
            debit = parse_money(raw.get(debit_col)) if debit_col else None
            credit = parse_money(raw.get(credit_col)) if credit_col else None
            if credit is not None:
                amount, kind = abs(credit), "income"
            elif debit is not None:
                amount, kind = abs(debit), "expense"
        else:
            amount = abs(amount)

        if amount is None:
            continue

        rows.append({
            "date": d,
            "description": desc,
            "amount": round(amount, 2),
            "kind": kind,
            "category": (raw.get(category_col, "") if category_col else "").strip(),
        })
    return rows, mapping


def month_range(month=None):
    month = month or date.today().strftime("%Y-%m")
    start = datetime.strptime(month + "-01", "%Y-%m-%d").date()
    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)
    return start.isoformat(), end.isoformat()


def get_transactions(user_id, search="", kind="", category_id="", month=""):
    conn = db()
    sql = """
        SELECT t.*, c.name AS category_name
        FROM transactions t
        LEFT JOIN categories c ON c.id=t.category_id
        WHERE t.user_id=?
    """
    params = [user_id]
    if search:
        sql += " AND (LOWER(t.description) LIKE ? OR LOWER(COALESCE(t.notes,'')) LIKE ?)"
        q = "%" + search.lower() + "%"
        params += [q, q]
    if kind in ("income", "expense"):
        sql += " AND t.kind=?"
        params.append(kind)
    if category_id:
        sql += " AND t.category_id=?"
        params.append(category_id)
    if month:
        start, end = month_range(month)
        sql += " AND t.txn_date>=? AND t.txn_date<?"
        params += [start, end]
    sql += " ORDER BY t.txn_date DESC, t.id DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def totals(user_id, month=None):
    start, end = month_range(month)
    conn = db()
    row = conn.execute("""
        SELECT
          COALESCE(SUM(CASE WHEN kind='income' THEN amount ELSE 0 END),0) income,
          COALESCE(SUM(CASE WHEN kind='expense' THEN amount ELSE 0 END),0) expense
        FROM transactions
        WHERE user_id=? AND txn_date>=? AND txn_date<?
    """, (user_id, start, end)).fetchone()
    conn.close()
    return float(row["income"]), float(row["expense"])


def create_alert(user_id, title, message):
    conn = db()
    conn.execute(
        "INSERT INTO alerts(user_id,title,message,created_at) VALUES(?,?,?,?)",
        (user_id, title, message, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def recurring_subscriptions(user_id):
    conn = db()
    rows = conn.execute("""
        SELECT description, COUNT(*) count, AVG(amount) avg_amount, MAX(txn_date) last_date
        FROM transactions
        WHERE user_id=? AND kind='expense'
        GROUP BY LOWER(description)
        HAVING COUNT(*) >= 2
        ORDER BY avg_amount DESC
    """, (user_id,)).fetchall()
    conn.close()
    result = []
    for r in rows:
        desc = r["description"]
        normalized = desc.lower()
        merchant = next((name for key, name in MERCHANTS.items() if key in normalized), desc)
        result.append({
            "merchant": merchant,
            "description": desc,
            "count": r["count"],
            "monthly": round(float(r["avg_amount"]), 2),
            "last_date": r["last_date"],
        })
    return result[:20]


def financial_health(user_id):
    income, expense = totals(user_id)
    profit = income - expense
    margin = (profit / income * 100) if income else 0
    subs = recurring_subscriptions(user_id)
    recurring = sum(x["monthly"] for x in subs)
    score = 50
    if income > 0:
        score += 15
    if margin >= 30:
        score += 20
    elif margin >= 15:
        score += 10
    elif margin < 0:
        score -= 20
    if recurring <= income * 0.15 if income else recurring == 0:
        score += 10
    if expense <= income if income else expense == 0:
        score += 5
    return max(0, min(100, int(score))), round(margin, 1), round(recurring, 2)


def generate_insights(user_id):
    income, expense = totals(user_id)
    last_month = date.today().replace(day=1) - timedelta(days=1)
    prev_month = last_month.strftime("%Y-%m")
    prev_income, prev_expense = totals(user_id, prev_month)
    insights = []

    if income:
        margin = (income - expense) / income * 100
        insights.append(f"Your current-month profit margin is {margin:.1f}%.")
    if prev_expense:
        change = (expense - prev_expense) / prev_expense * 100
        if change >= 10:
            insights.append(f"Expenses are up {change:.0f}% versus last month. Review the biggest categories.")
        elif change <= -10:
            insights.append(f"Expenses are down {abs(change):.0f}% versus last month.")
        else:
            insights.append("Expenses are broadly stable versus last month.")
    if prev_income:
        change = (income - prev_income) / prev_income * 100
        insights.append(f"Revenue is {'up' if change >= 0 else 'down'} {abs(change):.0f}% versus last month.")

    conn = db()
    cats = conn.execute("""
        SELECT COALESCE(c.name,'Uncategorized') category, SUM(t.amount) total
        FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
        WHERE t.user_id=? AND t.kind='expense'
        GROUP BY t.category_id ORDER BY total DESC LIMIT 3
    """, (user_id,)).fetchall()
    conn.close()
    for c in cats:
        insights.append(f"{c['category']} is one of your largest expense categories at ${float(c['total']):,.2f} this month.")

    subs = recurring_subscriptions(user_id)
    if subs:
        total = sum(s["monthly"] for s in subs)
        insights.append(f"I found {len(subs)} repeated merchants totaling about ${total:,.2f}/month.")
    if not insights:
        insights.append("Add a few transactions to unlock personalized financial insights.")
    return insights[:6]


def savings_recommendations(user_id):
    income, expense = totals(user_id)
    recs = []
    subs = recurring_subscriptions(user_id)
    if subs:
        total = sum(x["monthly"] for x in subs)
        recs.append(f"Review your {len(subs)} recurring merchants. They total about ${total:,.2f}/month.")
    if income and expense > income * 0.8:
        recs.append("Expenses are using more than 80% of revenue. Review discretionary spending before increasing fixed costs.")
    if expense == 0 and income > 0:
        recs.append("No expenses are logged for this month. Make sure your expense data is complete before judging profitability.")
    if not recs:
        recs.append("No major savings warning was detected from the data currently entered.")
    return recs


def forecast(user_id):
    today = date.today()
    income, expense = totals(user_id)
    days = today.day
    days_in_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    factor = days_in_month.day / max(days, 1)
    return round(income * factor, 2), round(expense * factor, 2)


@app.route("/")
def home():
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        first = request.form.get("first_name", "").strip()
        last = request.form.get("last_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        profile = request.form.get("profile_type", "general")

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
            return render_template("register.html")
        if not first or not last or not email or not username:
            flash("Please complete all fields.", "danger")
            return render_template("register.html")

        conn = db()
        try:
            token = secrets.token_urlsafe(32)
            cur = conn.execute("""
                INSERT INTO users(first_name,last_name,email,username,password_hash,profile_type,verify_token,created_at)
                VALUES(?,?,?,?,?,?,?,?)
            """, (first, last, email, username, generate_password_hash(password), profile, token, datetime.utcnow().isoformat()))
            user_id = cur.lastrowid
            for name, kind in DEFAULT_CATEGORIES:
                conn.execute("INSERT OR IGNORE INTO categories(user_id,name,kind) VALUES(?,?,?)", (user_id, name, kind))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            flash("That email or username is already registered.", "danger")
            return render_template("register.html")

        conn.close()
        link = url_for("verify_email", token=token, _external=True)
        sent = send_email(email, "Verify your Budget Buddy account", f"Welcome to Budget Buddy.\n\nVerify your email:\n{link}")
        if sent:
            flash("Account created. Check your email to verify your account.", "success")
        else:
            flash("Account created. Email delivery is not configured yet; use the verification link shown below.", "warning")
            session["dev_verify_link"] = link
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/verify/<token>")
def verify_email(token):
    conn = db()
    user = conn.execute("SELECT id FROM users WHERE verify_token=?", (token,)).fetchone()
    if not user:
        conn.close()
        flash("Verification link is invalid or expired.", "danger")
        return redirect(url_for("login"))
    conn.execute("UPDATE users SET email_verified=1, verify_token=NULL WHERE id=?", (user["id"],))
    conn.commit()
    conn.close()
    flash("Email verified. You can log in.", "success")
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip().lower()
        password = request.form.get("password", "")
        conn = db()
        user = conn.execute(
            "SELECT * FROM users WHERE LOWER(email)=? OR LOWER(username)=?",
            (identifier, identifier)
        ).fetchone()
        conn.close()
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid login details.", "danger")
            return render_template("login.html")
        session["user_id"] = user["id"]
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        conn = db()
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user:
            token = secrets.token_urlsafe(32)
            expires = (datetime.utcnow() + timedelta(hours=1)).isoformat()
            conn.execute("UPDATE users SET reset_token=?, reset_expires=? WHERE id=?", (token, expires, user["id"]))
            conn.commit()
            link = url_for("reset_password", token=token, _external=True)
            sent = send_email(email, "Reset your Budget Buddy password", f"Reset your password here:\n{link}\n\nThis link expires in one hour.")
            if not sent:
                session["dev_reset_link"] = link
        conn.close()
        flash("If that email exists, a reset link has been created.", "info")
        return redirect(url_for("forgot_password"))
    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE reset_token=?", (token,)).fetchone()
    if not user or not user["reset_expires"] or datetime.fromisoformat(user["reset_expires"]) < datetime.utcnow():
        conn.close()
        flash("Reset link is invalid or expired.", "danger")
        return redirect(url_for("forgot_password"))
    if request.method == "POST":
        password = request.form.get("password", "")
        if len(password) < 8:
            conn.close()
            flash("Password must be at least 8 characters.", "danger")
            return render_template("reset_password.html")
        conn.execute(
            "UPDATE users SET password_hash=?, reset_token=NULL, reset_expires=NULL WHERE id=?",
            (generate_password_hash(password), user["id"])
        )
        conn.commit()
        conn.close()
        flash("Password changed. Please log in.", "success")
        return redirect(url_for("login"))
    conn.close()
    return render_template("reset_password.html")


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = current_user()
    if request.method == "POST":
        first = request.form.get("first_name", "").strip()
        last = request.form.get("last_name", "").strip()
        conn = db()
        conn.execute("UPDATE users SET first_name=?, last_name=? WHERE id=?", (first, last, user["id"]))
        conn.commit()
        conn.close()
        flash("Profile updated.", "success")
        return redirect(url_for("profile"))
    return render_template("profile.html", user=user)


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    search = request.args.get("search", "").strip()
    kind = request.args.get("kind", "")
    category_id = request.args.get("category", "")
    txns = get_transactions(user["id"], search, kind, category_id, month)
    income, expense = totals(user["id"], month)
    conn = db()
    categories = conn.execute("SELECT * FROM categories WHERE user_id=? ORDER BY kind,name", (user["id"],)).fetchall()
    budgets = conn.execute("SELECT * FROM budgets WHERE user_id=? AND month=? ORDER BY name", (user["id"], month)).fetchall()
    goals = conn.execute("SELECT * FROM goals WHERE user_id=? ORDER BY created_at DESC", (user["id"],)).fetchall()
    alerts = conn.execute("SELECT * FROM alerts WHERE user_id=? AND read=0 ORDER BY id DESC LIMIT 5", (user["id"],)).fetchall()
    conn.close()

    budget_items = []
    for b in budgets:
        if b["category_id"]:
            spent = sum(float(t["amount"]) for t in get_transactions(user["id"], category_id=str(b["category_id"]), month=month, kind="expense"))
        else:
            spent = expense
        budget_items.append({
            "id": b["id"], "name": b["name"], "amount": float(b["amount"]),
            "spent": spent, "remaining": float(b["amount"]) - spent,
            "percent": min(100, round(spent / float(b["amount"]) * 100, 1)) if b["amount"] else 0
        })

    score, margin, recurring = financial_health(user["id"])
    pro = user["tier"] == "PRO"
    insights = generate_insights(user["id"]) if pro else []
    recommendations = savings_recommendations(user["id"]) if pro else []
    projected_income, projected_expense = forecast(user["id"]) if pro else (None, None)

    return render_template(
        "dashboard.html",
        user=user, txns=txns, categories=categories, budgets=budget_items, goals=goals,
        alerts=alerts, income=income, expense=expense, profit=income-expense,
        month=month, score=score, margin=margin, recurring=recurring,
        insights=insights, recommendations=recommendations,
        projected_income=projected_income, projected_expense=projected_expense, pro=pro
    )


@app.route("/transactions/add", methods=["POST"])
@login_required
def add_transaction():
    user = current_user()
    conn = db()
    count = conn.execute("SELECT COUNT(*) n FROM transactions WHERE user_id=?", (user["id"],)).fetchone()["n"]
    if user["tier"] != "PRO" and count >= FREE_TRANSACTION_LIMIT:
        conn.close()
        flash("Free plan is limited to 250 transactions. Upgrade to Pro for more.", "info")
        return redirect(url_for("pricing"))

    amount = parse_money(request.form.get("amount"))
    if amount is None or amount <= 0:
        conn.close()
        flash("Enter a valid amount.", "danger")
        return redirect(url_for("dashboard"))

    conn.execute("""
        INSERT INTO transactions(user_id,txn_date,description,amount,kind,category_id,notes,created_at)
        VALUES(?,?,?,?,?,?,?,?)
    """, (
        user["id"], request.form.get("txn_date") or date.today().isoformat(),
        request.form.get("description", "Transaction").strip(), abs(amount),
        request.form.get("kind", "expense"), request.form.get("category_id") or None,
        request.form.get("notes", "").strip(), datetime.utcnow().isoformat()
    ))
    conn.commit()
    conn.close()
    flash("Transaction added.", "success")
    return redirect(url_for("dashboard"))


@app.route("/transactions/<int:txn_id>/edit", methods=["POST"])
@login_required
def edit_transaction(txn_id):
    user = current_user()
    amount = parse_money(request.form.get("amount"))
    if amount is None or amount <= 0:
        flash("Enter a valid amount.", "danger")
        return redirect(url_for("dashboard"))
    conn = db()
    conn.execute("""
        UPDATE transactions SET txn_date=?,description=?,amount=?,kind=?,category_id=?,notes=?
        WHERE id=? AND user_id=?
    """, (
        request.form.get("txn_date"), request.form.get("description"),
        abs(amount), request.form.get("kind"), request.form.get("category_id") or None,
        request.form.get("notes", ""), txn_id, user["id"]
    ))
    conn.commit()
    conn.close()
    flash("Transaction updated.", "success")
    return redirect(url_for("dashboard"))


@app.route("/transactions/<int:txn_id>/delete", methods=["POST"])
@login_required
def delete_transaction(txn_id):
    user = current_user()
    conn = db()
    conn.execute("DELETE FROM transactions WHERE id=? AND user_id=?", (txn_id, user["id"]))
    conn.commit()
    conn.close()
    flash("Transaction deleted.", "success")
    return redirect(url_for("dashboard"))


@app.route("/categories", methods=["POST"])
@login_required
def categories_manage():
    user = current_user()
    action = request.form.get("action")
    conn = db()
    if action == "add":
        name = request.form.get("name", "").strip()
        kind = request.form.get("kind", "expense")
        if name:
            try:
                conn.execute("INSERT INTO categories(user_id,name,kind) VALUES(?,?,?)", (user["id"], name, kind))
                conn.commit()
                flash("Category added.", "success")
            except sqlite3.IntegrityError:
                flash("That category already exists.", "warning")
    elif action == "rename":
        conn.execute("UPDATE categories SET name=? WHERE id=? AND user_id=?", (request.form.get("name"), request.form.get("id"), user["id"]))
        conn.commit()
        flash("Category renamed.", "success")
    elif action == "delete":
        conn.execute("DELETE FROM categories WHERE id=? AND user_id=?", (request.form.get("id"), user["id"]))
        conn.commit()
        flash("Category deleted.", "success")
    conn.close()
    return redirect(url_for("dashboard"))


@app.route("/budgets", methods=["POST"])
@login_required
def budgets_manage():
    user = current_user()
    conn = db()
    action = request.form.get("action")
    if action == "add":
        amount = parse_money(request.form.get("amount"))
        if amount and amount > 0:
            try:
                conn.execute("""
                    INSERT INTO budgets(user_id,name,amount,month,category_id)
                    VALUES(?,?,?,?,?)
                """, (user["id"], request.form.get("name"), amount, request.form.get("month") or date.today().strftime("%Y-%m"), request.form.get("category_id") or None))
                conn.commit()
                flash("Budget created.", "success")
            except sqlite3.IntegrityError:
                flash("A budget with that name already exists for this month.", "warning")
    elif action == "delete":
        conn.execute("DELETE FROM budgets WHERE id=? AND user_id=?", (request.form.get("id"), user["id"]))
        conn.commit()
        flash("Budget deleted.", "success")
    conn.close()
    return redirect(url_for("dashboard"))


@app.route("/goals", methods=["POST"])
@login_required
def goals_manage():
    user = current_user()
    conn = db()
    action = request.form.get("action")
    if action == "add":
        target = parse_money(request.form.get("target"))
        current = parse_money(request.form.get("current")) or 0
        if target and target > 0:
            conn.execute("""
                INSERT INTO goals(user_id,name,target,current,deadline,created_at)
                VALUES(?,?,?,?,?,?)
            """, (user["id"], request.form.get("name"), target, current, request.form.get("deadline") or None, datetime.utcnow().isoformat()))
            conn.commit()
            flash("Goal created.", "success")
    elif action == "update":
        current = parse_money(request.form.get("current")) or 0
        conn.execute("UPDATE goals SET current=? WHERE id=? AND user_id=?", (current, request.form.get("id"), user["id"]))
        conn.commit()
        flash("Goal updated.", "success")
    elif action == "delete":
        conn.execute("DELETE FROM goals WHERE id=? AND user_id=?", (request.form.get("id"), user["id"]))
        conn.commit()
        flash("Goal deleted.", "success")
    conn.close()
    return redirect(url_for("dashboard"))


@app.route("/alerts/read/<int:alert_id>", methods=["POST"])
@login_required
def read_alert(alert_id):
    user = current_user()
    conn = db()
    conn.execute("UPDATE alerts SET read=1 WHERE id=? AND user_id=?", (alert_id, user["id"]))
    conn.commit()
    conn.close()
    return redirect(url_for("dashboard"))


@app.route("/import-csv", methods=["GET", "POST"])
@login_required
def import_csv():
    user = current_user()
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename.lower().endswith(".csv"):
            flash("Please upload a CSV file.", "danger")
            return redirect(url_for("import_csv"))
        content = file.read()
        rows, mapping = parse_csv(content)
        if not rows:
            flash("No usable transactions were found.", "danger")
            return redirect(url_for("import_csv"))
        if user["tier"] != "PRO":
            conn = db()
            existing = conn.execute("SELECT COUNT(*) n FROM transactions WHERE user_id=?", (user["id"],)).fetchone()["n"]
            conn.close()
            rows = rows[:max(0, FREE_TRANSACTION_LIMIT-existing)]
        session["csv_preview"] = rows[:500]
        session["csv_mapping"] = mapping
        return redirect(url_for("import_csv"))
    preview = session.get("csv_preview", [])
    mapping = session.get("csv_mapping", {})
    return render_template("import_csv.html", preview=preview, mapping=mapping)


@app.route("/import-csv/confirm", methods=["POST"])
@login_required
def confirm_csv():
    user = current_user()
    rows = session.pop("csv_preview", [])
    session.pop("csv_mapping", None)
    if not rows:
        flash("No CSV preview is waiting to be imported.", "warning")
        return redirect(url_for("import_csv"))
    conn = db()
    categories = {r["name"].lower(): r["id"] for r in conn.execute("SELECT id,name FROM categories WHERE user_id=?", (user["id"],)).fetchall()}
    for row in rows:
        cat_id = categories.get(row.get("category", "").lower())
        conn.execute("""
            INSERT INTO transactions(user_id,txn_date,description,amount,kind,category_id,created_at)
            VALUES(?,?,?,?,?,?,?)
        """, (user["id"], row["date"], row["description"], row["amount"], row["kind"], cat_id, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    flash(f"Imported {len(rows)} transactions.", "success")
    return redirect(url_for("dashboard"))


@app.route("/reports")
@login_required
def reports():
    user = current_user()
    if user["tier"] != "PRO":
        return redirect(url_for("pricing"))
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    txns = get_transactions(user["id"], month=month)
    income, expense = totals(user["id"], month)
    conn = db()
    by_cat = conn.execute("""
        SELECT COALESCE(c.name,'Uncategorized') category, SUM(t.amount) total
        FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
        WHERE t.user_id=? AND t.kind='expense' AND t.txn_date>=? AND t.txn_date<?
        GROUP BY t.category_id ORDER BY total DESC
    """, (user["id"], *month_range(month))).fetchall()
    conn.close()
    return render_template("reports.html", user=user, month=month, txns=txns, income=income, expense=expense, by_cat=by_cat)


@app.route("/reports/export.csv")
@login_required
@pro_required
def export_csv():
    user = current_user()
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    txns = get_transactions(user["id"], month=month)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Description", "Type", "Category", "Amount", "Notes"])
    for t in txns:
        writer.writerow([t["txn_date"], t["description"], t["kind"], t["category_name"] or "", f'{float(t["amount"]):.2f}', t["notes"]])
    return send_file(io.BytesIO(output.getvalue().encode()), mimetype="text/csv", as_attachment=True, download_name=f"budget-buddy-{month}.csv")


@app.route("/reports/export.pdf")
@login_required
@pro_required
def export_pdf():
    if not REPORTLAB_AVAILABLE:
        return "PDF export requires reportlab. Run pip install -r requirements.txt.", 500
    user = current_user()
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    txns = get_transactions(user["id"], month=month)
    income, expense = totals(user["id"], month)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    y = height - 50
    c.setFont("Helvetica-Bold", 18)
    c.drawString(40, y, f"Budget Buddy Financial Report — {month}")
    y -= 30
    c.setFont("Helvetica", 11)
    c.drawString(40, y, f"Revenue: ${income:,.2f}    Expenses: ${expense:,.2f}    Profit: ${income-expense:,.2f}")
    y -= 30
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "Date")
    c.drawString(120, y, "Description")
    c.drawString(350, y, "Type")
    c.drawString(430, y, "Amount")
    y -= 18
    c.setFont("Helvetica", 9)
    for t in txns[:45]:
        if y < 45:
            c.showPage()
            y = height - 50
        c.drawString(40, y, str(t["txn_date"])[:12])
        c.drawString(120, y, str(t["description"])[:34])
        c.drawString(350, y, str(t["kind"]))
        c.drawString(430, y, f"${float(t['amount']):,.2f}")
        y -= 14
    c.save()
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"budget-buddy-{month}.pdf")


@app.route("/pro")
@login_required
@pro_required
def pro():
    user = current_user()
    score, margin, recurring = financial_health(user["id"])
    forecast_income, forecast_expense = forecast(user["id"])
    return render_template(
        "pro.html", user=user, insights=generate_insights(user["id"]),
        recommendations=savings_recommendations(user["id"]),
        subscriptions=recurring_subscriptions(user["id"]),
        score=score, margin=margin, recurring=recurring,
        forecast_income=forecast_income, forecast_expense=forecast_expense
    )


@app.route("/weekly-summary", methods=["POST"])
@login_required
@pro_required
def weekly_summary():
    user = current_user()
    income, expense = totals(user["id"])
    body = (
        f"Budget Buddy weekly summary for {user['first_name']}.\n\n"
        f"Revenue this month: ${income:,.2f}\n"
        f"Expenses this month: ${expense:,.2f}\n"
        f"Profit: ${income-expense:,.2f}\n\n"
        + "\n".join(f"- {x}" for x in generate_insights(user["id"]))
    )
    sent = send_email(user["email"], "Your Budget Buddy weekly summary", body)
    if sent:
        flash("Weekly summary sent to your email.", "success")
    else:
        flash("SMTP is not configured. The summary can be sent automatically once SMTP credentials are added.", "warning")
    return redirect(url_for("pro"))


@app.route("/pricing")
def pricing():
    return render_template("pricing.html")


@app.route("/checkout")
@login_required
def checkout():
    plan = request.args.get("plan")
    if plan != "pro":
        return redirect(url_for("pricing"))
    if stripe is None or not os.environ.get("STRIPE_SECRET_KEY") or not os.environ.get("STRIPE_PRICE_ID"):
        flash("Stripe is not configured yet. Add STRIPE_SECRET_KEY and STRIPE_PRICE_ID in your environment.", "warning")
        return redirect(url_for("pricing"))
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    checkout_session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": os.environ["STRIPE_PRICE_ID"], "quantity": 1}],
        customer_email=current_user()["email"],
        success_url=url_for("dashboard", _external=True) + "?upgraded=1",
        cancel_url=url_for("pricing", _external=True),
        metadata={"user_id": str(current_user()["id"])},
    )
    return redirect(checkout_session.url, code=303)


@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    if stripe is None:
        return jsonify({"error": "Stripe unavailable"}), 503
    payload = request.data
    signature = request.headers.get("Stripe-Signature")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    try:
        event = stripe.Webhook.construct_event(payload, signature, secret)
    except Exception:
        return "Invalid webhook", 400

    if event["type"] in ("checkout.session.completed", "invoice.paid"):
        obj = event["data"]["object"]
        user_id = (obj.get("metadata") or {}).get("user_id")
        if user_id:
            conn = db()
            conn.execute("UPDATE users SET tier='PRO' WHERE id=?", (user_id,))
            conn.commit()
            conn.close()
    elif event["type"] in ("customer.subscription.deleted", "invoice.payment_failed"):
        obj = event["data"]["object"]
        user_id = (obj.get("metadata") or {}).get("user_id")
        if user_id:
            conn = db()
            conn.execute("UPDATE users SET tier='FREE' WHERE id=?", (user_id,))
            conn.commit()
            conn.close()

    return jsonify({"success": True})


# Backwards-compatible SaaS audit endpoint from the original app.
@app.route("/upload-statements", methods=["POST"])
def upload_statements():
    email = request.form.get("email", "").strip().lower()
    files = request.files.getlist("statements")
    pasted = request.form.get("pasted_csv", "").strip()
    if not email:
        return jsonify({"error": "Email required"}), 400
    if not files and not pasted:
        return jsonify({"error": "Upload a CSV or paste CSV data"}), 400

    conn = db()
    user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    tier = user["tier"] if user else "FREE"
    limit = 50 if tier == "PRO" else 3
    valid_files = [f for f in files if f and f.filename]
    inputs = len(valid_files) + (1 if pasted else 0)
    if inputs > limit:
        return jsonify({"error": "Upload limit exceeded", "allowed": limit, "tier": tier}), 403

    total = 0
    results = []
    for f in valid_files:
        rows, _ = parse_csv(f.read())
        total += len(rows)
        results.append({"name": f.filename, "transactions": len(rows)})
    if pasted:
        rows, _ = parse_csv(pasted)
        total += len(rows)
        results.append({"name": "pasted_csv", "transactions": len(rows)})

    estimated = min(max(1200, total * 100), 3500)
    return jsonify({
        "success": True,
        "tier": tier,
        "processed_count": inputs,
        "message": "Full audit available in your dashboard." if tier == "PRO" else "Upgrade to Pro to unlock the complete financial dashboard and analysis.",
        "teaser_summary": {"estimated_savings": f"${estimated:,}"},
        "results": results
    })


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=os.environ.get("FLASK_DEBUG") == "1")
