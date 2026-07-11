from openai import OpenAI
from flask import Flask, render_template, request, redirect, session, jsonify
import sqlite3
from collections import defaultdict
import os
import hmac
import hashlib
import json
from dotenv import load_dotenv
import stripe
import requests

load_dotenv()

# ================= OPENAI =================
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ================= STRIPE (kept in place, no longer linked from pricing page) =================
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

# ================= PADDLE =================
PADDLE_API_KEY = os.getenv("PADDLE_API_KEY")
PADDLE_WEBHOOK_SECRET = os.getenv("PADDLE_WEBHOOK_SECRET")
# Set to "sandbox" while testing, switch to "production" when you go live
PADDLE_ENV = os.getenv("PADDLE_ENV", "sandbox")

# Maps your plan keys to real Paddle Price IDs (from your sandbox dashboard)
PADDLE_PRICE_IDS = {
    "personal_starter": "pri_01kx6csjf90zmje1xac6cmta01",
    "personal_plus": "pri_01kx6crwt4w6vpm667k063ttcc",
    "personal_pro": "pri_01kx6cqzp8hfxy3pjssdsh2c8h",
    "personal_premium": "pri_01kx6cq20c34grwqr8asmwrqv6",
    "team_starter": "pri_01kx6cmjgfekem0ymchv43emqc",
    "team_growth": "pri_01kx6ckm0y2tsy1k1z4vyprceb",
    "business_pro": "pri_01kx514exdyyx28eswrqbmtbx8",
    # "enterprise" intentionally has no Paddle price - it stays a "Contact Sales" flow
}

# Reverse lookup: Paddle price_id -> (plan_key, price)
PLAN_PRICES = {
    "personal_starter": 5,
    "personal_plus": 10,
    "personal_pro": 25,
    "personal_premium": 50,
    "team_starter": 100,
    "team_growth": 200,
    "business_pro": 500,
    "enterprise": 1000
}

PRICE_ID_TO_PLAN = {v: k for k, v in PADDLE_PRICE_IDS.items()}


# ================= APP =================
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "budgetbuddy_secret")

budget_limit = 200


# ================= INIT DB =================
def init_db():
    conn = sqlite3.connect('budget.db')
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            first_name TEXT,
            last_name TEXT,
            email TEXT,
            username TEXT UNIQUE,
            password TEXT,
            profile_type TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            amount REAL,
            category TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS income (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            amount REAL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            name TEXT,
            target REAL,
            saved REAL DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            username TEXT UNIQUE,
            plan TEXT,
            price REAL,
            paddle_subscription_id TEXT,
            paddle_customer_id TEXT,
            status TEXT
        )
    """)

    conn.commit()
    conn.close()


init_db()


# ================= REGISTER =================
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        first_name = request.form['first_name']
        last_name = request.form['last_name']
        email = request.form['email']
        username = request.form['username']
        password = request.form['password']
        profile_type = request.form['profile_type']

        conn = sqlite3.connect('budget.db')
        c = conn.cursor()

        try:
            c.execute("""
                INSERT INTO users
                (first_name, last_name, email, username, password, profile_type)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (first_name, last_name, email, username, password, profile_type))

            conn.commit()
        except:
            conn.close()
            return "User already exists"

        conn.close()
        return redirect('/login')

    return render_template('register.html')


# ================= LOGIN =================
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = sqlite3.connect('budget.db')
        c = conn.cursor()

        c.execute("""
            SELECT * FROM users
            WHERE username=? AND password=?
        """, (username, password))

        user = c.fetchone()
        conn.close()

        if user:
            session['user'] = username
            return redirect('/')
        else:
            return "Invalid login"

    return render_template('login.html')


# ================= LOGOUT =================
@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect('/login')


# ================= HOME =================
@app.route('/')
def home():
    if 'user' not in session:
        return redirect('/login')

    username = session['user']

    conn = sqlite3.connect('budget.db')
    c = conn.cursor()

    # user info
    c.execute("""
        SELECT first_name, last_name, profile_type
        FROM users
        WHERE username=?
    """, (username,))

    user = c.fetchone()

    if not user:
        return redirect('/login')

    first_name, last_name, profile_type = user
    initials = first_name[0].upper() + last_name[0].upper()

    # transactions
    c.execute("""
        SELECT id, amount, category
        FROM transactions
        WHERE username=?
    """, (username,))

    rows = c.fetchall()

    transactions = []
    total = 0
    category_totals = defaultdict(float)

    for r in rows:
        expense_id = r[0]
        amount = float(r[1])
        category = r[2]

        transactions.append({"id": expense_id, "amount": amount, "category": category})
        total += amount
        category_totals[category] += amount

    # income
    c.execute("SELECT amount FROM income WHERE username=?", (username,))
    income_row = c.fetchone()
    income = income_row[0] if income_row else 0

    # goals
    c.execute("""
        SELECT id, name, target, saved
        FROM goals
        WHERE username=?
    """, (username,))

    goals = [
        {"id": g[0], "name": g[1], "target": g[2], "saved": g[3]}
        for g in c.fetchall()
    ]

    # current plan
    c.execute("SELECT plan FROM subscriptions WHERE username=?", (username,))
    plan_row = c.fetchone()
    current_plan = plan_row[0] if plan_row else "free"

    conn.close()

    remaining = income - total

    warning = None
    if income > 0:
        ratio = total / income
        if ratio > 0.8:
            warning = "⚠️ You're spending too fast!"
        elif ratio > 0.5:
            warning = "⚠️ Moderate spending"
        else:
            warning = "✅ Healthy spending"

    return render_template(
        "index.html",
        user=username,
        first_name=first_name,
        last_name=last_name,
        profile_type=profile_type,
        initials=initials,
        transactions=transactions,
        total=total,
        income=income,
        remaining=remaining,
        goals=goals,
        current_plan=current_plan,
        warning=warning,
        chart_labels=list(category_totals.keys()),
        chart_values=list(category_totals.values())
    )


# ================= ADD EXPENSE =================
@app.route('/add', methods=['POST'])
def add():
    if 'user' not in session:
        return redirect('/login')

    username = session['user']
    amount = float(request.form['amount'])
    category = request.form['category']

    conn = sqlite3.connect('budget.db')
    c = conn.cursor()

    c.execute("""
        INSERT INTO transactions (username, amount, category)
        VALUES (?, ?, ?)
    """, (username, amount, category))

    conn.commit()
    conn.close()

    return redirect('/')


# ================= SET INCOME =================
@app.route('/set_income', methods=['POST'])
def set_income():
    if 'user' not in session:
        return redirect('/login')

    username = session['user']
    amount = float(request.form['amount'])

    conn = sqlite3.connect('budget.db')
    c = conn.cursor()

    c.execute("""
        INSERT INTO income (username, amount)
        VALUES (?, ?)
        ON CONFLICT(username) DO UPDATE SET amount=excluded.amount
    """, (username, amount))

    conn.commit()
    conn.close()

    return redirect('/')


# ================= STRIPE CHECKOUT (kept, unused - pricing page now uses Paddle) =================
@app.route('/checkout')
def checkout():
    if 'user' not in session:
        return redirect('/login')

    plan = request.args.get('plan')

    plan_details = {
        "personal_starter": {"name": "Starter", "price": 5},
        "personal_plus": {"name": "Plus", "price": 10},
        "personal_pro": {"name": "Pro", "price": 25},
        "personal_premium": {"name": "Premium", "price": 50},
        "team_starter": {"name": "Team Starter", "price": 100},
        "team_growth": {"name": "Team Growth", "price": 200},
        "business_pro": {"name": "Business Pro", "price": 500},
        "enterprise": {"name": "Enterprise", "price": 1000}
    }

    details = plan_details.get(plan, {"name": "Starter", "price": 5})

    checkout_session = stripe.checkout.Session.create(
        payment_method_types=['card'],
        line_items=[{
            'price_data': {
                'currency': 'usd',
                'product_data': {
                    'name': f'Budget Buddy - {details["name"]}',
                },
                'unit_amount': details["price"] * 100,
                'recurring': {
                    'interval': 'month',
                },
            },
            'quantity': 1,
        }],
        mode='subscription',
        success_url=request.host_url + f'subscribe?plan={plan}&session_id={{CHECKOUT_SESSION_ID}}',
        cancel_url=request.host_url + 'pricing',
    )

    return redirect(checkout_session.url, code=303)


# ================= STRIPE SUBSCRIBE (kept, unused - Paddle uses /webhook/paddle instead) =================
@app.route('/subscribe')
def subscribe():
    if 'user' not in session:
        return redirect('/login')

    username = session['user']
    plan = request.args.get('plan')
    stripe_session_id = request.args.get('session_id')

    if not stripe_session_id:
        return redirect('/pricing')

    try:
        stripe_session = stripe.checkout.Session.retrieve(stripe_session_id)
        if stripe_session.payment_status != 'paid':
            return redirect('/pricing')
    except Exception:
        return redirect('/pricing')

    price = PLAN_PRICES.get(plan, 5)

    conn = sqlite3.connect('budget.db')
    c = conn.cursor()

    c.execute("""
        INSERT INTO subscriptions (username, plan, price)
        VALUES (?, ?, ?)
        ON CONFLICT(username)
        DO UPDATE SET plan=excluded.plan, price=excluded.price
    """, (username, plan, price))

    conn.commit()
    conn.close()

    return redirect('/')


# ================= PADDLE WEBHOOK =================
def verify_paddle_signature(raw_body, paddle_signature_header):
    """
    Paddle sends a header like: 'ts=1234567890;h1=abcdef...'
    We recompute the HMAC-SHA256 hash of 'timestamp:raw_body' using our
    webhook secret, and compare it to the h1 value Paddle sent.
    """
    if not paddle_signature_header or not PADDLE_WEBHOOK_SECRET:
        return False

    try:
        parts = dict(
            item.split('=', 1) for item in paddle_signature_header.split(';')
        )
        timestamp = parts.get('ts')
        signature = parts.get('h1')

        if not timestamp or not signature:
            return False

        signed_payload = f"{timestamp}:{raw_body.decode('utf-8')}"

        computed_signature = hmac.new(
            PADDLE_WEBHOOK_SECRET.encode('utf-8'),
            signed_payload.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(computed_signature, signature)
    except Exception:
        return False


@app.route('/webhook/paddle', methods=['POST'])
def paddle_webhook():
    raw_body = request.get_data()
    signature_header = request.headers.get('Paddle-Signature')

    if not verify_paddle_signature(raw_body, signature_header):
        return jsonify({"error": "invalid signature"}), 401

    event = json.loads(raw_body)
    event_type = event.get('event_type')
    data = event.get('data', {})

    if event_type in ('subscription.activated', 'subscription.updated', 'subscription.trialing'):
        custom_data = data.get('custom_data') or {}
        username = custom_data.get('username')

        items = data.get('items', [])
        price_id = items[0]['price']['id'] if items else None
        plan = PRICE_ID_TO_PLAN.get(price_id, 'unknown')
        price = PLAN_PRICES.get(plan, 0)

        subscription_id = data.get('id')
        customer_id = data.get('customer_id')
        status = data.get('status')

        if username:
            conn = sqlite3.connect('budget.db')
            c = conn.cursor()
            c.execute("""
                INSERT INTO subscriptions (username, plan, price, paddle_subscription_id, paddle_customer_id, status)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(username)
                DO UPDATE SET
                    plan=excluded.plan,
                    price=excluded.price,
                    paddle_subscription_id=excluded.paddle_subscription_id,
                    paddle_customer_id=excluded.paddle_customer_id,
                    status=excluded.status
            """, (username, plan, price, subscription_id, customer_id, status))
            conn.commit()
            conn.close()

    elif event_type in ('subscription.canceled', 'subscription.paused'):
        subscription_id = data.get('id')
        status = data.get('status')

        conn = sqlite3.connect('budget.db')
        c = conn.cursor()
        c.execute("""
            UPDATE subscriptions
            SET plan='free', status=?
            WHERE paddle_subscription_id=?
        """, (status, subscription_id))
        conn.commit()
        conn.close()

    return jsonify({"status": "ok"}), 200


# ================= PRICING PAGE =================
@app.route('/pricing')
def pricing():
    return render_template(
        'pricing.html',
        paddle_client_token=os.getenv("PADDLE_CLIENT_TOKEN"),
        paddle_env=PADDLE_ENV,
        paddle_price_ids=PADDLE_PRICE_IDS,
        username=session.get('user', '')
    )


# ================= AI CHAT =================
@app.route('/ai', methods=['GET', 'POST'])
def ai():
    if 'user' not in session:
        return redirect('/login')

    answer = None

    if request.method == 'POST':
        question = request.form['question']
        username = session['user']

        conn = sqlite3.connect('budget.db')
        c = conn.cursor()

        c.execute("SELECT amount FROM income WHERE username=?", (username,))
        income_row = c.fetchone()
        income = income_row[0] if income_row else 0

        c.execute("SELECT amount FROM transactions WHERE username=?", (username,))
        rows = c.fetchall()

        spent = sum(float(r[0]) for r in rows)

        conn.close()

        prompt = f"""
User income: {income}
User spending: {spent}
Remaining: {income - spent}

Question: {question}

Give short financial advice.
"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a financial assistant."},
                {"role": "user", "content": prompt}
            ]
        )

        answer = response.choices[0].message.content

    return render_template("ai.html", answer=answer)

# ================= ADD GOAL =================
@app.route('/add_goal', methods=['POST'])
def add_goal():
    if 'user' not in session:
        return redirect('/login')

    username = session['user']
    name = request.form['name']
    target = float(request.form['target'])

    conn = sqlite3.connect('budget.db')
    c = conn.cursor()

    c.execute("""
        INSERT INTO goals (username, name, target, saved)
        VALUES (?, ?, ?, 0)
    """, (username, name, target))

    conn.commit()
    conn.close()

    return redirect('/')
    # ================= ADD MONEY TO GOAL =================
@app.route('/add_to_goal/<int:goal_id>', methods=['POST'])
def add_to_goal(goal_id):
    if 'user' not in session:
        return redirect('/login')

    amount = float(request.form['amount'])

    conn = sqlite3.connect('budget.db')
    c = conn.cursor()

    c.execute("""
        UPDATE goals
        SET saved = saved + ?
        WHERE id = ?
    """, (amount, goal_id))

    conn.commit()
    conn.close()

    return redirect('/')


# ================= DELETE EXPENSE =================
@app.route('/delete_expense/<int:expense_id>', methods=['POST'])
def delete_expense(expense_id):
    if 'user' not in session:
        return redirect('/login')

    username = session['user']

    conn = sqlite3.connect('budget.db')
    c = conn.cursor()

    c.execute("""
        DELETE FROM transactions
        WHERE id=? AND username=?
    """, (expense_id, username))

    conn.commit()
    conn.close()

    return redirect('/')


# ================= DELETE INCOME =================
@app.route('/delete_income', methods=['POST'])
def delete_income():
    if 'user' not in session:
        return redirect('/login')

    username = session['user']

    conn = sqlite3.connect('budget.db')
    c = conn.cursor()

    c.execute("DELETE FROM income WHERE username=?", (username,))

    conn.commit()
    conn.close()

    return redirect('/')


# ================= DELETE GOAL =================
@app.route('/delete_goal/<int:goal_id>', methods=['POST'])
def delete_goal(goal_id):
    if 'user' not in session:
        return redirect('/login')

    username = session['user']

    conn = sqlite3.connect('budget.db')
    c = conn.cursor()

    c.execute("""
        DELETE FROM goals
        WHERE id=? AND username=?
    """, (goal_id, username))

    conn.commit()
    conn.close()

    return redirect('/')

# ================= RUN =================
if __name__ == '__main__':
    app.run(debug=True)
