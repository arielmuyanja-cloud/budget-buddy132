import os
import csv
import io
import json
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import psycopg2
import psycopg2.extras
import openai

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "budget_buddy_secret_key_12345")

# Initialize OpenAI
openai.api_key = os.getenv("OPENAI_API_KEY")

# ================= DATABASE SETUP =================
db_url = os.getenv("DATABASE_URL")

PLAN_DISPLAY_NAMES = {
    "personal_pro": "Personal Pro",
    "team_starter": "Team Starter",
    "business_pro": "Business VIP",
    "enterprise": "Custom Enterprise Build"
}

PLAN_PRICES = {
    "personal_pro": 19,
    "team_starter": 99,
    "business_pro": 299,
    "enterprise": 2500
}

def get_db_connection():
    if db_url:
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
                password VARCHAR(255) NOT NULL,
                account_type VARCHAR(50) DEFAULT 'personal',
                is_admin BOOLEAN DEFAULT FALSE
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
        c.execute("""
            CREATE TABLE IF NOT EXISTS enterprise_requests (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                email VARCHAR(255) NOT NULL,
                company VARCHAR(255) NOT NULL,
                phone VARCHAR(100),
                requirements TEXT NOT NULL,
                created_at VARCHAR(100) NOT NULL
            );
        """)

        # Migrations
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS account_type VARCHAR(50) DEFAULT 'personal';")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE;")
        c.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS plan VARCHAR(100) DEFAULT 'free';")
        c.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS price NUMERIC DEFAULT 0;")
        c.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'active';")

    else:
        # SQLite syntax
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                account_type TEXT DEFAULT 'personal',
                is_admin INTEGER DEFAULT 0
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
        c.execute("""
            CREATE TABLE IF NOT EXISTS enterprise_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                company TEXT NOT NULL,
                phone TEXT,
                requirements TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)

    conn.commit()
    conn.close()

try:
    init_db()
except Exception as e:
    print("Database initialization warning:", e)


# ================= AUTH ROUTES =================

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        account_type = request.form.get('account_type', 'personal')

        if not username or not password:
            flash("Username and password are required.", "error")
            return render_template('register.html')

        conn = get_db_connection()
        c = conn.cursor()
        param = "%s" if db_url else "?"
        
        try:
            c.execute(f"INSERT INTO users (username, password, account_type) VALUES ({param}, {param}, {param})", 
                      (username, password, account_type))
            conn.commit()

            # Set initial free subscription status
            c.execute(f"INSERT INTO subscriptions (username, plan, price, status) VALUES ({param}, 'free', 0, 'active')",
                      (username,))
            conn.commit()

            session['user'] = username
            session['account_type'] = account_type
            session['is_admin'] = False
            flash("Account created successfully!", "success")
            return redirect('/')
        except Exception as e:
            print("Register error:", e)
            flash("Username already exists. Please login.", "error")
        finally:
            conn.close()

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        conn = get_db_connection()
        c = conn.cursor()
        param = "%s" if db_url else "?"

        c.execute(f"SELECT * FROM users WHERE username={param} AND password={param}", (username, password))
        user = c.fetchone()
        conn.close()

        if user:
            session['user'] = username
            session['account_type'] = user['account_type'] if 'account_type' in user.keys() else 'personal'
            session['is_admin'] = bool(user['is_admin']) if 'is_admin' in user.keys() else False
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
    account_type = session.get('account_type', 'personal')

    conn = get_db_connection()
    c = conn.cursor()
    param = "%s" if db_url else "?"

    if request.method == 'POST':
        amount = request.form.get('amount')
        description = request.form.get('description')
        income_amount = request.form.get('income_amount')
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        if amount and description:
            try:
                c.execute(
                    f"INSERT INTO transactions (username, description, amount, type, date) VALUES ({param}, {param}, {param}, {param}, {param})",
                    (username, description, float(amount), 'expense', now)
                )
                conn.commit()
                flash("Expense logged successfully!", "success")
            except ValueError:
                flash("Please enter a valid expense amount.", "error")

        elif income_amount:
            try:
                c.execute(
                    f"INSERT INTO transactions (username, description, amount, type, date) VALUES ({param}, {param}, {param}, {param}, {param})",
                    (username, "Revenue/Income", float(income_amount), 'income', now)
                )
                conn.commit()
                flash("Income updated successfully!", "success")
            except ValueError:
                flash("Please enter a valid income amount.", "error")

        conn.close()
        return redirect('/')

    c.execute(f"SELECT * FROM transactions WHERE username={param} ORDER BY id DESC", (username,))
    transactions = c.fetchall()

    c.execute(f"SELECT * FROM subscriptions WHERE username={param}", (username,))
    sub = c.fetchone()
    conn.close()

    total_income = sum(
        t['amount'] if isinstance(t, dict) or hasattr(t, '__getitem__') else t[3] 
        for t in transactions 
        if (t['type'] if isinstance(t, dict) or hasattr(t, '__getitem__') else t[4]) == 'income'
    )
    total_expense = sum(
        t['amount'] if isinstance(t, dict) or hasattr(t, '__getitem__') else t[3] 
        for t in transactions 
        if (t['type'] if isinstance(t, dict) or hasattr(t, '__getitem__') else t[4]) == 'expense'
    )
    balance = total_income - total_expense

    template_name = 'business_dashboard.html' if account_type == 'business' else 'index.html'

    formatted_sub = None
    if sub:
        plan_raw = sub['plan'] if isinstance(sub, dict) or hasattr(sub, '__getitem__') else sub[1]
        status_raw = sub['status'] if isinstance(sub, dict) or hasattr(sub, '__getitem__') else sub[4]
        formatted_sub = {
            'plan': PLAN_DISPLAY_NAMES.get(plan_raw, plan_raw.title()),
            'status': status_raw
        }

    return render_template(
        template_name,
        username=username,
        account_type=account_type,
        transactions=transactions,
        total_income=total_income,
        total_expense=total_expense,
        income=total_income,
        expense=total_expense,
        total=total_expense,
        balance=balance,
        remaining=balance,
        subscription=formatted_sub
    )


# ================= AI LEAK DETECTION =================

def extract_recurring_subscriptions(transactions):
    ai_and_saas_keywords = [
        'openai', 'chatgpt', 'anthropic', 'claude', 'midjourney', 'github', 'copilot',
        'google one', 'notion', 'slack', 'zoom', 'adobe', 'canva', 'linkedin', 
        'hubspot', 'salesforce', 'figma', 'zapier', 'aws', 'render', 'heroku',
        'grammarly', 'jasper', 'descript', 'elevenlabs', 'perplexity'
    ]

    detected_subscriptions = []
    total_monthly_spend = 0.0

    for tx in transactions:
        desc_lower = str(tx.get('description', '')).lower()
        is_sub = any(keyword in desc_lower for keyword in ai_and_saas_keywords)
        
        if is_sub or 'sub' in desc_lower or 'membership' in desc_lower or 'recurring' in desc_lower:
            amount = float(tx.get('amount', 0))
            if tx.get('type') == 'expense' and amount > 0:
                detected_subscriptions.append({
                    "name": tx.get('description'),
                    "amount": amount
                })
                total_monthly_spend += amount

    annual_potential_savings = total_monthly_spend * 12

    return {
        "subscriptions": detected_subscriptions,
        "monthly_total": round(total_monthly_spend, 2),
        "annual_total": round(annual_potential_savings, 2)
    }


def analyze_statement_leaks(transactions, account_type):
    sub_summary = extract_recurring_subscriptions(transactions)

    if not os.getenv("OPENAI_API_KEY"):
        return {
            "insights": ["API key not set for AI insights. Configure OPENAI_API_KEY in Render."],
            "monthly_savings": sub_summary['monthly_total'],
            "annual_savings": sub_summary['annual_total'],
            "subs": sub_summary['subscriptions']
        }

    prompt = f"""
    You are an expert financial auditor analyzing a {account_type} bank statement.
    
    Recurring Subscriptions & AI Tool Debits Found:
    {json.dumps(sub_summary['subscriptions'])}
    Total Monthly Recurring Cost: ${sub_summary['monthly_total']}
    Total Potential Annual Savings: ${sub_summary['annual_total']}

    Examine the statement data:
    1. Highlight redundant AI tools, software, or memberships.
    2. Tell the user EXACTLY how much they save per year by cutting redundant tools.
    3. Provide 2 actionable cost-cutting recommendations.

    Transactions JSON:
    {json.dumps(transactions[:40])}

    Keep output concise, bold key dollar amounts, and use bullet points. Under 200 words.
    """

    try:
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=350
        )
        return {
            "insights": response.choices[0].message.content.split('\n'),
            "monthly_savings": sub_summary['monthly_total'],
            "annual_savings": sub_summary['annual_total'],
            "subs": sub_summary['subscriptions']
        }
    except Exception as e:
        print("OpenAI Audit Error:", e)
        return {
            "insights": ["Unable to run AI analysis right now."],
            "monthly_savings": sub_summary['monthly_total'],
            "annual_savings": sub_summary['annual_total'],
            "subs": sub_summary['subscriptions']
        }


@app.route('/upload_statement', methods=['POST'])
def upload_statement():
    if 'user' not in session:
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    if 'file' not in request.files:
        flash("No file uploaded", "error")
        return redirect('/')

    file = request.files['file']
    if file.filename == '':
        flash("No selected file", "error")
        return redirect('/')

    if file and file.filename.endswith('.csv'):
        username = session['user']
        stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
        csv_input = list(csv.DictReader(stream))

        conn = get_db_connection()
        c = conn.cursor()
        param = "%s" if db_url else "?"
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        tx_list = []
        for row in csv_input:
            description = row.get('Description') or row.get('Narration') or row.get('Payee') or row.get('Memo') or 'Bank Transaction'
            raw_amount = row.get('Amount') or row.get('Transaction Amount') or 0
            
            try:
                amount = float(str(raw_amount).replace('$', '').replace(',', ''))
            except ValueError:
                continue

            t_type = 'income' if amount > 0 else 'expense'
            if 'Type' in row and row['Type'].lower() in ['credit', 'deposit']:
                t_type = 'income'

            c.execute(
                f"INSERT INTO transactions (username, description, amount, type, date) VALUES ({param}, {param}, {param}, {param}, {param})",
                (username, description, abs(amount), t_type, now)
            )
            tx_list.append({"description": description, "amount": abs(amount), "type": t_type})

        conn.commit()
        conn.close()

        audit_result = analyze_statement_leaks(tx_list, session.get('account_type', 'personal'))
        session['last_audit'] = audit_result

        flash(f"Successfully processed statement! Found {len(tx_list)} transactions.", "success")
        return redirect('/')
    else:
        flash("Please upload a valid CSV bank statement.", "error")
        return redirect('/')


# ================= PRICING, CHECKOUT & ENTERPRISE =================

@app.route('/pricing')
def pricing():
    selected_plan = request.args.get('plan')
    plan_display = PLAN_DISPLAY_NAMES.get(selected_plan, selected_plan) if selected_plan else None
    plan_price = PLAN_PRICES.get(selected_plan, 19) if selected_plan else None

    return render_template(
        'pricing.html', 
        selected_plan=selected_plan,
        plan_display=plan_display,
        plan_price=plan_price
    )


@app.route('/checkout', methods=['GET', 'POST'])
def checkout():
    if 'user' not in session:
        flash("Please log in first to purchase or upgrade a plan.", "error")
        return redirect('/login')

    plan = request.args.get('plan', 'personal_pro')
    if plan not in PLAN_PRICES:
        flash("Invalid plan selected.", "error")
        return redirect('/pricing')

    return redirect(url_for('pricing', plan=plan))


@app.route('/confirm_manual_payment', methods=['POST'])
def confirm_manual_payment():
    if 'user' not in session:
        flash("Please log in to submit a payment verification.", "error")
        return redirect('/login')

    username = session['user']
    plan = request.form.get('plan', '').strip()
    reference = request.form.get('reference', '').strip()

    if not plan or plan not in PLAN_PRICES:
        flash("Invalid or missing subscription plan.", "error")
        return redirect('/pricing')

    if not reference:
        flash("Please provide a valid transaction reference or ID.", "error")
        return redirect(url_for('pricing', plan=plan))

    price = PLAN_PRICES.get(plan, 19)

    conn = get_db_connection()
    c = conn.cursor()
    param = "%s" if db_url else "?"

    try:
        if db_url:
            conflict_clause = """
                ON CONFLICT(username)
                DO UPDATE SET plan=EXCLUDED.plan, price=EXCLUDED.price, order_tracking_id=EXCLUDED.order_tracking_id, status='pending_verification'
            """
            c.execute(f"""
                INSERT INTO subscriptions (username, plan, price, order_tracking_id, status)
                VALUES ({param}, {param}, {param}, {param}, 'pending_verification')
                {conflict_clause}
            """, (username, plan, price, f"MANUAL-{reference}"))
        else:
            c.execute(f"""
                INSERT INTO subscriptions (username, plan, price, order_tracking_id, status)
                VALUES ({param}, {param}, {param}, {param}, 'pending_verification')
                ON CONFLICT(username) DO UPDATE SET
                plan=excluded.plan, price=excluded.price, order_tracking_id=excluded.order_tracking_id, status='pending_verification'
            """, (username, plan, price, f"MANUAL-{reference}"))

        conn.commit()
        flash("Your payment has been submitted for manual verification.", "success")
    except Exception as e:
        print("Payment submission error:", e)
        conn.rollback()
        flash("There was an error saving your payment verification. Please try again.", "error")
    finally:
        conn.close()

    return redirect('/')


@app.route('/enterprise_request', methods=['POST'])
def enterprise_request():
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip()
    company = request.form.get('company', '').strip()
    phone = request.form.get('phone', '').strip()
    requirements = request.form.get('requirements', '').strip()

    if not name or not email or not company or not requirements:
        flash("Please fill in all required fields for the enterprise consultation request.", "error")
        return redirect('/pricing#enterprise-consultation')

    conn = get_db_connection()
    c = conn.cursor()
    param = "%s" if db_url else "?"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        c.execute(
            f"INSERT INTO enterprise_requests (name, email, company, phone, requirements, created_at) VALUES ({param}, {param}, {param}, {param}, {param}, {param})",
            (name, email, company, phone, requirements, now)
        )
        conn.commit()
        flash("Thank you. We'll contact you within 24 hours.", "success")
    except Exception as e:
        print("Enterprise request DB error:", e)
        conn.rollback()
        flash("Failed to submit request. Please try again.", "error")
    finally:
        conn.close()

    return redirect('/pricing')


# ================= ADMIN MANUAL APPROVAL =================

@app.route('/admin')
def admin_dashboard():
    if 'user' not in session or not session.get('is_admin'):
        flash("Unauthorized access. Admin privileges required.", "error")
        return redirect('/')

    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute("SELECT * FROM subscriptions")
    subs = c.fetchall()

    c.execute("SELECT * FROM enterprise_requests ORDER BY id DESC")
    enterprise_reqs = c.fetchall()

    conn.close()

    return render_template('admin.html', subscriptions=subs, enterprise_requests=enterprise_reqs)


@app.route('/admin/approve/<username>/<plan>')
def admin_approve(username, plan):
    if 'user' not in session or not session.get('is_admin'):
        flash("Unauthorized action.", "error")
        return redirect('/')

    price = PLAN_PRICES.get(plan, 19)

    conn = get_db_connection()
    c = conn.cursor()
    param = "%s" if db_url else "?"

    try:
        if db_url:
            conflict_clause = """
                ON CONFLICT(username)
                DO UPDATE SET plan=EXCLUDED.plan, price=EXCLUDED.price, status='active'
            """
            c.execute(f"""
                INSERT INTO subscriptions (username, plan, price, status)
                VALUES ({param}, {param}, {param}, 'active')
                {conflict_clause}
            """, (username, plan, price))
        else:
            c.execute(f"""
                INSERT INTO subscriptions (username, plan, price, status)
                VALUES ({param}, {param}, {param}, 'active')
                ON CONFLICT(username) DO UPDATE SET
                plan=excluded.plan, price=excluded.price, status='active'
            """, (username, plan, price))

        conn.commit()
        flash(f"Successfully activated {username} on {PLAN_DISPLAY_NAMES.get(plan, plan)} plan!", "success")
    except Exception as e:
        print("Admin approval error:", e)
        conn.rollback()
        flash("Failed to approve user subscription.", "error")
    finally:
        conn.close()

    return redirect('/admin')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
