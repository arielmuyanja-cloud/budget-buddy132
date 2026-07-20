from openai import OpenAI
from flask import Flask, render_template, request, redirect, session, jsonify, flash
import sqlite3
from collections import defaultdict
import os
import hmac
import hashlib
import json
from dotenv import load_dotenv
import stripe
import requests
import base64

load_dotenv()

# ================= OPENAI =================
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ================= STRIPE =================
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

# ================= PADDLE =================
PADDLE_API_KEY = os.getenv("PADDLE_API_KEY")
PADDLE_WEBHOOK_SECRET = os.getenv("PADDLE_WEBHOOK_SECRET")
PADDLE_ENV = os.getenv("PADDLE_ENV", "sandbox")

PADDLE_PRICE_IDS = {
    "personal_starter": "pri_01kx6csjf90zmje1xac6cmta01",
    "personal_plus": "pri_01kx6crwt4w6vpm667k063ttcc",
    "personal_pro": "pri_01kx6cqzp8hfxy3pjssdsh2c8h",
    "personal_premium": "pri_01kx6cq20c34grwqr8asmwrqv6",
    "team_starter": "pri_01kx6cmjgfekem0ymchv43emqc",
    "team_growth": "pri_01kx6ckm0y2tsy1k1z4vyprceb",
    "business_pro": "pri_01kx514exdyyx28eswrqbmtbx8",
}

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


# ================= INIT DB (Neon Postgres Migration) =================
db_url = os.environ.get('DATABASE_URL')
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

def get_db_connection():
    if db_url:
        import psycopg2
        return psycopg2.connect(db_url)
    else:
        return sqlite3.connect('budget.db')

def init_db():
    conn = get_db_connection()
    c = conn.cursor()

    id_type = "SERIAL PRIMARY KEY" if db_url else "INTEGER PRIMARY KEY AUTOINCREMENT"

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            id {id_type},
            first_name TEXT,
            last_name TEXT,
            email TEXT,
            username TEXT UNIQUE,
            password TEXT,
            profile_type TEXT
        )
    """)

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS transactions (
            id {id_type},
            username TEXT,
            amount REAL,
            category TEXT
        )
    """)

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS income (
            id {id_type},
            username TEXT UNIQUE,
            amount REAL
        )
    """)

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS goals (
            id {id_type},
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

    c.execute("""
        CREATE TABLE IF NOT EXISTS bank_connections (
            username TEXT UNIQUE,
            access_url TEXT
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

        conn = get_db_connection()
        c = conn.cursor()
        param = "%s" if db_url else "?"

        try:
            c.execute(f"""
                INSERT INTO users
                (first_name, last_name, email, username, password, profile_type)
                VALUES ({param}, {param}, {param}, {param}, {param}, {param})
            """, (first_name, last_name, email, username, password, profile_type))

            conn.commit()
        except Exception:
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

        conn = get_db_connection()
        c = conn.cursor()
        param = "%s" if db_url else "?"

        c.execute(f"""
            SELECT * FROM users
            WHERE username={param} AND password={param}
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

    conn = get_db_connection()
    c = conn.cursor()
    param = "%s" if db_url else "?"

    c.execute(f"""
        SELECT first_name, last_name, profile_type
        FROM users
        WHERE username={param}
    """, (username,))

    user = c.fetchone()

    if not user:
        return redirect('/login')

    first_name, last_name, profile_type = user
    initials = first_name[0].upper() + last_name[0].upper()

    c.execute(f"""
        SELECT id, amount, category
        FROM transactions
        WHERE username={param}
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

    c.execute(f"SELECT amount FROM income WHERE username={param}", (username,))
    income_row = c.fetchone()
    income = income_row[0] if income_row else 0

    c.execute(f"""
        SELECT id, name, target, saved
        FROM goals
        WHERE username={param}
    """, (username,))

    goals = [
        {"id": g[0], "name": g[1], "target": g[2], "saved": g[3]}
        for g in c.fetchall()
    ]

    c.execute(f"SELECT plan FROM subscriptions WHERE username={param}", (username,))
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

    conn = get_db_connection()
    c = conn.cursor()
    param = "%s" if db_url else "?"

    c.execute(f"""
        INSERT INTO transactions (username, amount, category)
        VALUES ({param}, {param}, {param})
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

    conn = get_db_connection()
    c = conn.cursor()
    param = "%s" if db_url else "?"

    if db_url:
        c.execute(f"""
            INSERT INTO income (username, amount)
            VALUES ({param}, {param})
            ON CONFLICT(username) DO UPDATE SET amount=EXCLUDED.amount
        """, (username, amount))
    else:
        c.execute(f"""
            INSERT INTO income (username, amount)
            VALUES ({param}, {param})
            ON CONFLICT(username) DO UPDATE SET amount=excluded.amount
        """, (username, amount))

    conn.commit()
    conn.close()

    return redirect('/')


# ================= STRIPE CHECKOUT =================
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


# ================= STRIPE SUBSCRIBE =================
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

    conn = get_db_connection()
    c = conn.cursor()
    param = "%s" if db_url else "?"

    if db_url:
        c.execute(f"""
            INSERT INTO subscriptions (username, plan, price)
            VALUES ({param}, {param}, {param})
            ON CONFLICT(username)
            DO UPDATE SET plan=EXCLUDED.plan, price=EXCLUDED.price
        """, (username, plan, price))
    else:
        c.execute(f"""
            INSERT INTO subscriptions (username, plan, price)
            VALUES ({param}, {param}, {param})
            ON CONFLICT(username)
            DO UPDATE SET plan=excluded.plan, price=excluded.price
        """, (username, plan, price))

    conn.commit()
    conn.close()

    return redirect('/')


# ================= PADDLE WEBHOOK =================
def verify_paddle_signature(raw_body, paddle_signature_header):
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
            conn = get_db_connection()
            c = conn.cursor()
            param = "%s" if db_url else "?"
            
            conflict_clause = """
                ON CONFLICT(username)
                DO UPDATE SET
                    plan=EXCLUDED.plan,
                    price=EXCLUDED.price,
                    paddle_subscription_id=EXCLUDED.paddle_subscription_id,
                    paddle_customer_id=EXCLUDED.paddle_customer_id,
                    status=EXCLUDED.status
            """ if db_url else """
                ON CONFLICT(username)
                DO UPDATE SET
                    plan=excluded.plan,
                    price=excluded.price,
                    paddle_subscription_id=excluded.paddle_subscription_id,
                    paddle_customer_id=excluded.paddle_customer_id,
                    status=excluded.status
            """

            c.execute(f"""
                INSERT INTO subscriptions (username, plan, price, paddle_subscription_id, paddle_customer_id, status)
                VALUES ({param}, {param}, {param}, {param}, {param}, {param})
                {conflict_clause}
            """, (username, plan, price, subscription_id, customer_id, status))
            conn.commit()
            conn.close()

    elif event_type in ('subscription.canceled', 'subscription.paused'):
        subscription_id = data.get('id')
        status = data.get('status')

        conn = get_db_connection()
        c = conn.cursor()
        param = "%s" if db_url else "?"
        
        c.execute(f"""
            UPDATE subscriptions
            SET plan='free', status={param}
            WHERE paddle_subscription_id={param}
        """, (status, subscription_id))
        conn.commit()
        conn.close()

    return jsonify({"status": "ok"}), 200


# ================= LEGAL PAGES =================
@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')


@app.route('/refund-policy')
def refund_policy():
    return render_template('refund.html')


# ================= CONTACT PAGE =================
@app.route('/contact')
def contact():
    return '''
    <html>
        <head>
            <title>Contact Us - Budget Buddy</title>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
        </head>
        <body style="background:#0f172a; color:white; font-family:sans-serif; text-align:center; padding:100px 20px;">
            <div style="max-width:500px; margin:0 auto; background:#1e293b; padding:40px; border-radius:12px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.5);">
                <h1 style="margin-bottom:10px; color:#3b82f6;">Contact Budget Buddy Support</h1>
                <p style="color:#94a3b8; line-height:1.6;">Have questions about our Enterprise plans, custom multi-seat setups, or need dedicated billing help?</p>
                <div style="background:#0f172a; padding:15px; border-radius:8px; margin:25px 0; font-size:18px; border:1px solid #334155;">
                    Email us at: <a href="mailto:arielmuyanja.cloud@gmail.com" style="color:#60a5fa; text-decoration:none; font-weight:bold;">arielmuyanja.cloud@gmail.com</a>
                </div>
                <p style="font-size:14px; color:#64748b;">Our average review and response window is within 24 business hours.</p>
                <br>
                <a href="/pricing" style="color:#94a3b8; text-decoration:underline;">Back to Pricing</a>
            </div>
        </body>
    </html>
    '''


# ================= SIMPLEFIN BANK LINKING =================
@app.route('/link-bank', methods=['POST'])
def link_bank():
    if 'user' not in session:
        return redirect('/login')

    username = session['user']
    setup_token = request.form.get('simplefin_token', '').strip()

    if not setup_token:
        flash("Token cannot be empty!", "error")
        return redirect('/ai')

    try:
        claim_url = base64.b64decode(setup_token).decode('utf-8')
        response = requests.post(claim_url)
        
        if response.status_code == 200:
            access_url = response.text.strip()

            conn = get_db_connection()
            c = conn.cursor()
            param = "%s" if db_url else "?"
            
            if db_url:
                c.execute(f"""
                    INSERT INTO bank_connections (username, access_url)
                    VALUES ({param}, {param})
                    ON CONFLICT(username) DO UPDATE SET access_url=EXCLUDED.access_url
                """, (username, access_url))
            else:
                c.execute(f"""
                    INSERT INTO bank_connections (username, access_url)
                    VALUES ({param}, {param})
                    ON CONFLICT(username) DO UPDATE SET access_url=excluded.access_url
                """, (username, access_url))
                
            conn.commit()
            conn.close()
            
            flash("🏦 Bank connection linked successfully!", "success")
            return redirect('/')
        else:
            flash(f"SimpleFIN rejected token exchange. Code: {response.status_code}", "error")
            return redirect('/ai')
            
    except Exception as e:
        flash(f"Error processing token: {str(e)}", "error")
        return redirect('/ai')


# ================= FETCH BANK DATA =================
@app.route('/fetch-bank-transactions')
def fetch_bank_transactions():
    if 'user' not in session:
        return redirect('/login')

    username = session['user']

    conn = get_db_connection()
    c = conn.cursor()
    param = "%s" if db_url else "?"
    c.execute(f"SELECT access_url FROM bank_connections WHERE username={param}", (username,))
    row = c.fetchone()
    conn.close()

    if not row:
        flash("No linked bank account found! Please link one first.", "error")
        return redirect('/ai')

    access_url = row[0]

    try:
        scheme, rest = access_url.split('//', 1)
        auth, rest = rest.split('@', 1)
        
        target_url = scheme + '//' + rest + '/accounts'
        bank_user, bank_pass = auth.split(':', 1)
        
        response = requests.get(target_url, auth=(bank_user, bank_pass))
        
        if response.status_code == 200:
            bank_data = response.json()
            accounts = bank_data.get('accounts', [])
            all_imported = 0
            
            for account in accounts:
                transactions = account.get('transactions', [])
                
                conn = get_db_connection()
                c = conn.cursor()
                
                for tx in transactions:
                    raw_amount = float(tx.get('amount', 0))
                    
                    if raw_amount < 0:
                        amount = abs(raw_amount)
                        category = tx.get('description', 'Bank Transaction')
                        
                        c.execute(f"""
                            INSERT INTO transactions (username, amount, category)
                            VALUES ({param}, {param}, {param})
                        """, (username, amount, category))
                        all_imported += 1
                        
                conn.commit()
                conn.close()

            flash(f"Successfully synced! Imported {all_imported} new bank transactions.", "success")
            return redirect('/')
        else:
            flash(f"Failed to fetch data from SimpleFIN. Code: {response.status_code}", "error")
            return redirect('/ai')

    except Exception as e:
        flash(f"Error pulling bank data: {str(e)}", "error")
        return redirect('/ai')


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

        conn = get_db_connection()
        c = conn.cursor()
        param = "%s" if db_url else "?"

        c.execute(f"SELECT amount FROM income WHERE username={param}", (username,))
        income_row = c.fetchone()
        income = income_row[0] if income_row else 0

        c.execute(f"SELECT amount FROM transactions WHERE username={param}", (username,))
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

    conn = get_db_connection()
    c = conn.cursor()
    param = "%s" if db_url else "?"

    c.execute(f"""
        INSERT INTO goals (username, name, target, saved)
        VALUES ({param}, {param}, {param}, 0)
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

    conn = get_db_connection()
    c = conn.cursor()
    param = "%s" if db_url else "?"

    c.execute(f"""
        UPDATE goals
        SET saved = saved + {param}
        WHERE id = {param}
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

    conn = get_db_connection()
    c = conn.cursor()
    param = "%s" if db_url else "?"

    c.execute(f"""
        DELETE FROM transactions
        WHERE id={param} AND username={param}
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

    conn = get_db_connection()
    c = conn.cursor()
    param = "%s" if db_url else "?"

    c.execute(f"DELETE FROM income WHERE username={param}", (username,))

    conn.commit()
    conn.close()

    return redirect('/')


# ================= DELETE GOAL =================
@app.route('/delete_goal/<int:goal_id>', methods=['POST'])
def delete_goal(goal_id):
    if 'user' not in session:
        return redirect('/login')

    username = session['user']

    conn = get_db_connection()
    c = conn.cursor()
    param = "%s" if db_url else "?"

    c.execute(f"""
        DELETE FROM goals
        WHERE id={param} AND username={param}
    """, (goal_id, username))

    conn.commit()
    conn.close()

    return redirect('/')


# ================= RUN =================
if __name__ == '__main__':
    app.run(debug=True)
