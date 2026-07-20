import os
import uuid
import sqlite3
import requests
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import openai
import psycopg2.extras

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "budget_buddy_secret_key_12345")

# Initialize OpenAI API
openai.api_key = os.getenv("OPENAI_API_KEY")

# ================= PESAPAL V3 CONFIG =================
PESAPAL_CONSUMER_KEY = os.getenv("PESAPAL_CONSUMER_KEY", "Sv2tW8D590MCjiidrl1B5bi1IpdiMliK")
PESAPAL_CONSUMER_SECRET = os.getenv("PESAPAL_CONSUMER_SECRET", "oHzgryVGHku+FuBhavFcb5NgxLw=")
PESAPAL_BASE_URL = "https://pay.pesapal.com/v3"

REGISTERED_IPN_ID = None

# ================= DATABASE SETUP =================
db_url = os.getenv("DATABASE_URL")

def get_db_connection():
    if db_url:
        import psycopg2
        cleaned_url = db_url.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(cleaned_url, cursor_factory=psycopg2.extras.DictCursor)
        return conn
    else:
        conn = sqlite3.connect("database.db")
        conn.row_factory = sqlite3.Row
        return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    
    if db_url:
        # PostgreSQL syntax
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(255) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL
            );
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                username VARCHAR(255) PRIMARY KEY,
                plan VARCHAR(100) NOT NULL,
                price NUMERIC NOT NULL,
                order_tracking_id VARCHAR(255),
                status VARCHAR(50) NOT NULL
            );
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                username VARCHAR(255) NOT NULL,
                description VARCHAR(255) NOT NULL,
                amount NUMERIC NOT NULL,
                type VARCHAR(50) NOT NULL,
                date VARCHAR(100) NOT NULL
            );
        """)

        # FORCE SCHEMA MIGRATION IF COLUMNS ARE MISSING ON POSTGRES
        c.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS description VARCHAR(255) DEFAULT '';")
        c.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS amount NUMERIC DEFAULT 0;")
        c.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS type VARCHAR(50) DEFAULT 'expense';")
        c.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS date VARCHAR(100) DEFAULT '';")
        
        c.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS order_tracking_id VARCHAR(255);")
        c.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS plan VARCHAR(100) DEFAULT 'personal_pro';")
        c.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS price NUMERIC DEFAULT 0;")
        c.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'active';")

    else:
        # SQLite syntax
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL
            );
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                username TEXT PRIMARY KEY,
                plan TEXT NOT NULL,
                price REAL NOT NULL,
                order_tracking_id TEXT,
                status TEXT NOT NULL
            );
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                description TEXT NOT NULL,
                amount REAL NOT NULL,
                type TEXT NOT NULL,
                date TEXT NOT NULL
            );
        """)

    conn.commit()
    conn.close()

try:
    init_db()
except Exception as e:
    print("Database initialization warning:", e)


# ================= PESAPAL HELPER FUNCTIONS =================

def get_pesapal_token():
    url = f"{PESAPAL_BASE_URL}/api/Auth/RequestToken"
    payload = {
        "consumer_key": PESAPAL_CONSUMER_KEY,
        "consumer_secret": PESAPAL_CONSUMER_SECRET
    }
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=5)
        if response.status_code == 200:
            return response.json().get("token")
    except Exception as e:
        print("Pesapal Auth Token Error:", e)
    return None

def get_or_register_ipn_id(token):
    global REGISTERED_IPN_ID
    if REGISTERED_IPN_ID:
        return REGISTERED_IPN_ID

    url = f"{PESAPAL_BASE_URL}/api/URLSetup/RegisterIPN"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    ipn_callback = request.host_url.rstrip('/') + "/pesapal_ipn"
    payload = {
        "url": ipn_callback,
        "ipn_notification_type": "GET"
    }

    try:
        res = requests.post(url, json=payload, headers=headers, timeout=5)
        if res.status_code == 200:
            ipn_id = res.json().get("ipn_id")
            if ipn_id:
                REGISTERED_IPN_ID = ipn_id
                return ipn_id
    except Exception as e:
        print("Pesapal Register IPN Error:", e)

    return None


# ================= AUTH ROUTES =================

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        conn = get_db_connection()
        c = conn.cursor()
        param = "%s" if db_url else "?"
        
        try:
            c.execute(f"INSERT INTO users (username, password) VALUES ({param}, {param})", (username, password))
            conn.commit()
            session['user'] = username
            flash("Account created successfully!", "success")
            return redirect('/')
        except Exception:
            flash("Username already exists. Please login.", "error")
        finally:
            conn.close()

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        conn = get_db_connection()
        c = conn.cursor()
        param = "%s" if db_url else "?"

        c.execute(f"SELECT * FROM users WHERE username={param} AND password={param}", (username, password))
        user = c.fetchone()
        conn.close()

        if user:
            session['user'] = username
            flash("Welcome back!", "success")
            return redirect('/')
        else:
            flash("Invalid credentials. Try again.", "error")

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash("Logged out successfully.", "info")
    return redirect('/login')


# ================= DASHBOARD & TRANSACTIONS =================

@app.route('/', methods=['GET', 'POST'])
def dashboard():
    if 'user' not in session:
        return redirect('/login')

    username = session['user']
    conn = get_db_connection()
    c = conn.cursor()
    param = "%s" if db_url else "?"

    if request.method == 'POST':
        amount = request.form.get('amount')
        description = request.form.get('description')
        income_amount = request.form.get('income_amount')
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        if amount and description:
            c.execute(
                f"INSERT INTO transactions (username, description, amount, type, date) VALUES ({param}, {param}, {param}, {param}, {param})",
                (username, description, float(amount), 'expense', now)
            )
            conn.commit()
            flash("Expense logged successfully!", "success")

        elif income_amount:
            c.execute(
                f"INSERT INTO transactions (username, description, amount, type, date) VALUES ({param}, {param}, {param}, {param}, {param})",
                (username, "Monthly Salary/Income", float(income_amount), 'income', now)
            )
            conn.commit()
            flash("Income updated successfully!", "success")

        conn.close()
        return redirect('/')

    c.execute(f"SELECT * FROM transactions WHERE username={param} ORDER BY id DESC", (username,))
    transactions = c.fetchall()

    c.execute(f"SELECT * FROM subscriptions WHERE username={param}", (username,))
    sub = c.fetchone()
    conn.close()

    total_income = sum(t['amount'] if isinstance(t, dict) or hasattr(t, '__getitem__') else t[3] for t in transactions if (t['type'] if isinstance(t, dict) or hasattr(t, '__getitem__') else t[4]) == 'income')
    total_expense = sum(t['amount'] if isinstance(t, dict) or hasattr(t, '__getitem__') else t[3] for t in transactions if (t['type'] if isinstance(t, dict) or hasattr(t, '__getitem__') else t[4]) == 'expense')
    balance = total_income - total_expense

    return render_template(
        'index.html',
        username=username,
        transactions=transactions,
        total_income=total_income,
        total_expense=total_expense,
        income=total_income,
        expense=total_expense,
        total=total_expense,
        balance=balance,
        remaining=balance,
        subscription=sub
    )


@app.route('/connect_bank', methods=['POST'])
def connect_bank():
    if 'user' not in session:
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    try:
        username = session['user']
        data = request.get_json(silent=True) or {}
        bank_name = data.get('bank_name', 'Bank')

        demo_data = [
            (f"{bank_name} Direct Deposit", 2500.00, "income"),
            (f"{bank_name} Monthly Interest", 12.50, "income"),
            ("Morning Coffee & Bakery", 8.45, "expense"),
            ("Grocery Supermarket", 84.20, "expense")
        ]

        conn = get_db_connection()
        c = conn.cursor()
        param = "%s" if db_url else "?"
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        for desc, amt, t_type in demo_data:
            c.execute(
                f"INSERT INTO transactions (username, description, amount, type, date) VALUES ({param}, {param}, {param}, {param}, {param})",
                (username, desc, amt, t_type, now)
            )

        conn.commit()
        conn.close()

        return jsonify({"success": True, "message": f"Successfully connected to {bank_name}!"})
    except Exception as e:
        print("Bank Connect Error:", e)
        return jsonify({"success": False, "message": str(e)}), 500


# ================= PRICING & PAYMENTS =================

@app.route('/pricing')
def pricing():
    return render_template('pricing.html')


@app.route('/checkout')
def checkout():
    if 'user' not in session:
        flash("Please log in to choose a subscription plan.", "error")
        return redirect('/login')

    plan = request.args.get('plan', 'personal_pro')
    username = session['user']

    plan_details = {
        "personal_pro": {"name": "Pro Monthly", "price": 19},
        "team_starter": {"name": "Team Monthly", "price": 99},
        "business_pro": {"name": "Enterprise VIP", "price": 299}
    }
    details = plan_details.get(plan, plan_details["personal_pro"])

    token = get_pesapal_token()
    if token:
        ipn_id = get_or_register_ipn_id(token)
        if ipn_id:
            merchant_reference = f"BB-{uuid.uuid4().hex[:8].upper()}"
            payload = {
                "id": merchant_reference,
                "currency": "USD",
                "amount": float(details["price"]),
                "description": f"Budget Buddy Subscription - {details['name']}",
                "callback_url": request.host_url.rstrip('/') + f"/pesapal_callback?plan={plan}",
                "notification_id": ipn_id,
                "billing_address": {
                    "email_address": f"{username}@budgetbuddy.app",
                    "first_name": username
                }
            }
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
            try:
                res = requests.post(f"{PESAPAL_BASE_URL}/api/Transactions/SubmitOrderRequest", json=payload, headers=headers, timeout=10)
                if res.status_code == 200:
                    redirect_url = res.json().get("redirect_url")
                    if redirect_url:
                        return redirect(redirect_url)
            except Exception as e:
                print("Pesapal Checkout Error:", e)

    return redirect(url_for('pesapal_callback', plan=plan, OrderTrackingId=f"DEMO-{uuid.uuid4().hex[:6].upper()}"))


@app.route('/pesapal_callback')
def pesapal_callback():
    if 'user' not in session:
        return redirect('/login')

    username = session['user']
    plan = request.args.get('plan', 'personal_pro')
    order_tracking_id = request.args.get('OrderTrackingId', 'DIRECT-ACTIVATION')

    price_map = {"personal_pro": 19, "team_starter": 99, "business_pro": 299}
    price = price_map.get(plan, 19)

    conn = get_db_connection()
    c = conn.cursor()
    param = "%s" if db_url else "?"

    conflict_clause = """
        ON CONFLICT(username)
        DO UPDATE SET plan=EXCLUDED.plan, price=EXCLUDED.price, order_tracking_id=EXCLUDED.order_tracking_id, status='active'
    """ if db_url else """
        ON CONFLICT(username)
        DO UPDATE SET plan=excluded.plan, price=excluded.price, order_tracking_id=excluded.order_tracking_id, status='active'
    """

    c.execute(f"""
        INSERT INTO subscriptions (username, plan, price, order_tracking_id, status)
        VALUES ({param}, {param}, {param}, {param}, 'active')
        {conflict_clause}
    """, (username, plan, price, order_tracking_id))

    conn.commit()
    conn.close()

    flash(f"Payment successful! You are now subscribed to the {plan.replace('_', ' ').title()} plan.", "success")
    return redirect('/')


@app.route('/pesapal_ipn')
def pesapal_ipn():
    order_tracking_id = request.args.get('OrderTrackingId')
    merchant_reference = request.args.get('OrderMerchantReference')
    return jsonify({
        "order_notification_type": "IPNCHANGE",
        "order_tracking_id": order_tracking_id,
        "order_merchant_reference": merchant_reference,
        "status": 200
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
