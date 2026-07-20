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
PESAPAL_BASE_URL = "https://pay.pesapal.com/v3"  # Production URL

# Global IPN Cache to prevent redundant registration requests
REGISTERED_IPN_ID = None

# ================= DATABASE SETUP =================
db_url = os.getenv("DATABASE_URL")

def get_db_connection():
    """Connects to PostgreSQL with DictCursor if DATABASE_URL exists, otherwise uses SQLite."""
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
    """Initializes schema tables for Users, Subscriptions, and Transactions."""
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
    else:
        # Valid SQLite syntax
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

# Run DB init on app startup
try:
    init_db()
except Exception as e:
    print("Database initialization warning:", e)


# ================= PESAPAL HELPER FUNCTIONS =================

def get_pesapal_token():
    """Fetches a Bearer Auth Token from Pesapal v3."""
    url = f"{PESAPAL_BASE_URL}/api/Auth/RequestToken"
    payload = {
        "consumer_key": PESAPAL_CONSUMER_KEY,
        "consumer_secret": PESAPAL_CONSUMER_SECRET
    }
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json().get("token")
    except Exception as e:
        print("Pesapal Auth Token Error:", e)
    return None

def get_or_register_ipn_id(token):
    """Registers or fetches the notification_id required by Pesapal for orders."""
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
        res = requests.post(url, json=payload, headers=headers, timeout=10)
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


# ================= DASHBOARD & BANK LINKING =================

@app.route('/')
def dashboard():
    if 'user' not in session:
        return redirect('/login')

    username = session['user']
    conn = get_db_connection()
    c = conn.cursor()
    param = "%s" if db_url else "?"

    # Get user transactions
    c.execute(f"SELECT * FROM transactions WHERE username={param} ORDER BY id DESC", (username,))
    transactions = c.fetchall()

    # Get subscription status
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
        income=total_income,     # Passed for index.html line 69
        expense=total_expense,   # Passed for index.html expense references
        total=total_expense,     # Passed for index.html line 70
        balance=balance,
        subscription=sub
    )


@app.route('/connect_bank', methods=['POST'])
def connect_bank():
    """Simulates instant live bank linking by inserting initial demo transactions."""
    if 'user' not in session:
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    username = session['user']
    bank_name = request.json.get('bank_name', 'Bank')

    demo_data = [
        ("Initial Bank Deposit", 2500.00, "income"),
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


# ================= AI FINANCIAL COACH =================

@app.route('/ai_coach', methods=['POST'])
def ai_coach():
    """Receives financial question and queries OpenAI model."""
    if 'user' not in session:
        return jsonify({"response": "Please log in to consult your AI Coach."})

    user_query = request.json.get("message", "")
    username = session['user']

    # Fetch user's financial totals for context
    conn = get_db_connection()
    c = conn.cursor()
    param = "%s" if db_url else "?"
    c.execute(f"SELECT amount, type FROM transactions WHERE username={param}", (username,))
    txs = c.fetchall()
    conn.close()

    income = sum(t[0] for t in txs if t[1] == 'income')
    expense = sum(t[0] for t in txs if t[1] == 'expense')
    balance = income - expense

    system_prompt = f"""
    You are Budget Buddy's AI Financial Coach. 
    Current user financial context:
    - Total Income: ${income:.2f}
    - Total Expenses: ${expense:.2f}
    - Current Net Balance: ${balance:.2f}

    Provide actionable, empathetic, concise, and smart financial advice.
    """

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_query}
            ],
            max_tokens=250,
            temperature=0.7
        )
        reply = response.choices[0].message.content.strip()
        return jsonify({"response": reply})
    except Exception as e:
        print("OpenAI Error:", e)
        return jsonify({"response": "I'm currently unable to access AI advice. Please check your system setup."})


# ================= PRICING & PESAPAL PAYMENTS =================

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

    # Subscriptions in USD
    plan_details = {
        "personal_pro": {"name": "Pro Monthly", "price": 19},
        "team_starter": {"name": "Team Monthly", "price": 99},
        "business_pro": {"name": "Enterprise VIP", "price": 299}
    }
    
    details = plan_details.get(plan, plan_details["personal_pro"])
    
    token = get_pesapal_token()
    if not token:
        flash("Unable to initiate payment gateway authentication. Please try again.", "error")
        return redirect('/pricing')

    ipn_id = get_or_register_ipn_id(token)
    if not ipn_id:
        flash("Unable to register payment notification listener.", "error")
        return redirect('/pricing')

    # Generate unique transaction reference
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

    flash("Payment initiation failed. Please try again.", "error")
    return redirect('/pricing')


@app.route('/pesapal_callback')
def pesapal_callback():
    """Callback route executed when user completes Pesapal checkout."""
    if 'user' not in session:
        return redirect('/login')

    username = session['user']
    plan = request.args.get('plan', 'personal_pro')
    order_tracking_id = request.args.get('OrderTrackingId')

    price_map = {"personal_pro": 19, "team_starter": 99, "business_pro": 299}
    price = price_map.get(plan, 19)

    # Save active plan to DB
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

    flash("Payment successful! Your subscription is now fully active.", "success")
    return redirect('/')


@app.route('/pesapal_ipn')
def pesapal_ipn():
    """Instant Payment Notification endpoint triggered by Pesapal server."""
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
