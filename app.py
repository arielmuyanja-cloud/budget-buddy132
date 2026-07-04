from openai import OpenAI
from flask import Flask, render_template, request, redirect, session
import sqlite3
from collections import defaultdict
import os
from dotenv import load_dotenv

load_dotenv()

# ================= OPENAI =================
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

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
            price REAL
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
        SELECT amount, category
        FROM transactions
        WHERE username=?
    """, (username,))

    rows = c.fetchall()

    transactions = []
    total = 0
    category_totals = defaultdict(float)

    for r in rows:
        amount = float(r[0])
        category = r[1]

        transactions.append({"amount": amount, "category": category})
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


# ================= CHECKOUT =================
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

    return render_template('checkout.html', plan=plan, plan_name=details["name"], price=details["price"])


# ================= SUBSCRIBE (called after checkout form submit) =================
@app.route('/subscribe', methods=['POST'])
def subscribe():
    if 'user' not in session:
        return redirect('/login')

    username = session['user']
    plan = request.form.get('plan')

    prices = {
        "personal_starter": 5,
        "personal_plus": 10,
        "personal_pro": 25,
        "personal_premium": 50,
        "team_starter": 100,
        "team_growth": 200,
        "business_pro": 500,
        "enterprise": 1000
    }

    price = prices.get(plan, 5)

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


# ================= PRICING PAGE =================
@app.route('/pricing')
def pricing():
    return render_template('pricing.html')


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
# ================= RUN =================
if __name__ == '__main__':
    app.run(debug=True)
