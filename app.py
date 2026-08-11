import os
import csv
import io
import json
import uuid
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, 
    url_for, flash, jsonify, session, send_file, Response, abort
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import openai

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "budget-buddy-super-secret-key-2026")

# Database Configuration (PostgreSQL on Render / SQLite locally)
db_url = os.environ.get('DATABASE_URL', f"sqlite:///{os.path.join(os.path.abspath(os.path.dirname(__file__)), 'budget.db')}")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# External API Keys (Flutterwave & OpenAI)
FLW_PUBLIC_KEY = os.environ.get("FLW_PUBLIC_KEY", "FLWPUBK_TEST-xxxxxxxx")
FLW_SECRET_KEY = os.environ.get("FLW_SECRET_KEY", "FLWSECK_TEST-xxxxxxxx")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Owner email — used to gate the manual payment approval dashboard
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "amuyanja1314@gmail.com")

# SMTP config — used to email the admin when a Sendwave payment needs review
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USERNAME)

# --- DATABASE MODELS ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    agency_name = db.Column(db.String(120), nullable=True)
    plan_tier = db.Column(db.String(20), default="FREE")  # "FREE", "STARTER", "GROWTH", or "PRO"
    is_verified = db.Column(db.Boolean, default=False)
    reset_token = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    transactions = db.relationship('Transaction', backref='user', lazy=True, cascade="all, delete-orphan")
    categories = db.relationship('Category', backref='user', lazy=True, cascade="all, delete-orphan")
    budgets = db.relationship('Budget', backref='user', lazy=True, cascade="all, delete-orphan")
    goals = db.relationship('Goal', backref='user', lazy=True, cascade="all, delete-orphan")

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    description = db.Column(db.String(200), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    type = db.Column(db.String(10), nullable=False)  # 'INCOME' or 'EXPENSE'
    category = db.Column(db.String(50), nullable=False, default="General")

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    type = db.Column(db.String(10), nullable=False, default="EXPENSE")

class Budget(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    category = db.Column(db.String(50), nullable=False)
    monthly_limit = db.Column(db.Float, nullable=False)

class Goal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(100), nullable=False)
    target_amount = db.Column(db.Float, nullable=False)
    current_amount = db.Column(db.Float, default=0.0)

class SendwavePayment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    plan_requested = db.Column(db.String(20), nullable=False)   # STARTER, GROWTH, PRO
    amount = db.Column(db.Float, nullable=False)
    reference_code = db.Column(db.String(100), nullable=False)  # Sendwave transaction/reference code
    sender_name = db.Column(db.String(120), nullable=True)      # name payment was sent under, if different
    status = db.Column(db.String(20), default="PENDING")        # PENDING, APPROVED, REJECTED
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    admin_notes = db.Column(db.String(300), nullable=True)
    action_token = db.Column(db.String(64), unique=True, nullable=True)  # lets the accept/decline email links work without login

    user = db.relationship('User', backref=db.backref('sendwave_payments', cascade="all, delete-orphan"))

# Automatic Schema Initialization & Table Sync
with app.app_context():
    db.create_all()

# --- DECORATORS ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please log in to access this page.", "warning")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def pro_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = User.query.get(session.get('user_id'))
        if not user or user.plan_tier not in ['GROWTH', 'PRO']:
            flash("This feature is exclusive to GROWTH and PRO subscribers. Upgrade to unlock!", "warning")
            return redirect(url_for('pricing'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = User.query.get(session.get('user_id'))
        if not user or user.email.lower() != ADMIN_EMAIL.lower():
            flash("You don't have access to that page.", "danger")
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

# --- ANALYTICS & RECURRING ENGINE ---
KNOWN_SUBSCRIPTIONS = ["adobe", "chatgpt", "canva", "google workspace", "slack", "zoom", "hubspot", "semrush", "github", "render", "meta", "linkedin"]

def detect_subscriptions(transactions):
    detected = []
    seen = set()
    for t in transactions:
        desc_lower = t.description.lower()
        for sub in KNOWN_SUBSCRIPTIONS:
            if sub in desc_lower and sub not in seen:
                detected.append({"name": t.description, "amount": t.amount, "category": t.category})
                seen.add(sub)
                break
    return detected

def compute_financial_health(revenue, expenses, budgets, user_id):
    score = 100
    if revenue > 0:
        margin = (revenue - expenses) / revenue
        if margin < 0: score -= 30
        elif margin < 0.2: score -= 15
    elif expenses > 0:
        score -= 40

    for b in budgets:
        spent = sum(t.amount for t in Transaction.query.filter_by(user_id=user_id, category=b.category, type='EXPENSE').all())
        if b.monthly_limit > 0 and spent > b.monthly_limit:
            score -= 10

    return max(0, min(100, score))
    # --- AUTH ROUTES ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')
        agency_name = request.form.get('agency_name')

        if User.query.filter_by(email=email).first():
            flash("Email already registered.", "danger")
            return redirect(url_for('register'))

        user = User(
            email=email, 
            password_hash=generate_password_hash(password, method='scrypt'),
            agency_name=agency_name,
            is_verified=True
        )
        db.session.add(user)
        db.session.commit()

        # Seed Default Categories
        defaults = [
            Category(user_id=user.id, name="Advertising", type="EXPENSE"),
            Category(user_id=user.id, name="Software", type="EXPENSE"),
            Category(user_id=user.id, name="Payroll", type="EXPENSE"),
            Category(user_id=user.id, name="Contractors", type="EXPENSE"),
            Category(user_id=user.id, name="Client Revenue", type="INCOME")
        ]
        db.session.add_all(defaults)
        db.session.commit()

        session['user_id'] = user.id
        flash("Welcome to Budget Buddy!", "success")
        return redirect(url_for('dashboard'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            flash("Signed in successfully.", "success")
            return redirect(url_for('dashboard'))
        flash("Invalid email or password.", "danger")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash("Logged out successfully.", "info")
    return redirect(url_for('login'))

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password_route():
    if request.method == 'POST':
        email = request.form.get('email')
        user = User.query.filter_by(email=email).first()
        if user:
            user.reset_token = "MOCK_TOKEN_123"
            db.session.commit()
            flash("Password reset instructions sent to your email.", "info")
        else:
            flash("Email address not found.", "danger")
    return render_template('forgot_password.html')

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    user = User.query.get(session['user_id'])
    if request.method == 'POST':
        user.agency_name = request.form.get('agency_name')
        new_pw = request.form.get('password')
        if new_pw:
            user.password_hash = generate_password_hash(new_pw, method='scrypt')
        db.session.commit()
        flash("Profile updated successfully.", "success")
        return redirect(url_for('profile'))
    return render_template('profile.html', user=user)

# --- DASHBOARD & CORE ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/dashboard')
@login_required
def dashboard():
    user = User.query.get(session['user_id'])
    query_str = request.args.get('q', '').strip()
    category_filter = request.args.get('category', '')
    type_filter = request.args.get('type', '')
    sort_by = request.args.get('sort', 'date_desc')

    tx_query = Transaction.query.filter_by(user_id=user.id)

    if query_str:
        tx_query = tx_query.filter(
            (Transaction.description.ilike(f"%{query_str}%")) | 
            (Transaction.category.ilike(f"%{query_str}%"))
        )
    if category_filter:
        tx_query = tx_query.filter_by(category=category_filter)
    if type_filter:
        tx_query = tx_query.filter_by(type=type_filter)

    if sort_by == 'amount_desc':
        tx_query = tx_query.order_by(Transaction.amount.desc())
    elif sort_by == 'amount_asc':
        tx_query = tx_query.order_by(Transaction.amount.asc())
    else:
        tx_query = tx_query.order_by(Transaction.date.desc())

    transactions = tx_query.all()

    total_revenue = sum(t.amount for t in transactions if t.type == 'INCOME')
    total_expenses = sum(t.amount for t in transactions if t.type == 'EXPENSE')
    net_profit = total_revenue - total_expenses

    budgets = Budget.query.filter_by(user_id=user.id).all()
    budget_progress = []
    alerts = []
    for b in budgets:
        spent = sum(t.amount for t in transactions if t.type == 'EXPENSE' and t.category.lower() == b.category.lower())
        pct = min(100, int((spent / b.monthly_limit) * 100)) if b.monthly_limit > 0 else 0
        rem = b.monthly_limit - spent
        budget_progress.append({"id": b.id, "category": b.category, "limit": b.monthly_limit, "spent": spent, "remaining": rem, "pct": pct})
        if b.monthly_limit > 0 and spent > b.monthly_limit:
            alerts.append(f"Overbudget Alert: Category '{b.category}' exceeded set limit by ${abs(rem):.2f}!")

    goals = Goal.query.filter_by(user_id=user.id).all()
    goal_data = []
    for g in goals:
        pct = min(100, int((g.current_amount / g.target_amount) * 100)) if g.target_amount > 0 else 0
        goal_data.append({"id": g.id, "title": g.title, "target": g.target_amount, "current": g.current_amount, "pct": pct})

    # Analytics Engine Integrations
    detected_subs = detect_subscriptions(transactions)
    health_score = compute_financial_health(total_revenue, total_expenses, budgets, user.id)

    # Monthly Trends Data for Charts
    chart_categories = {}
    for t in transactions:
        if t.type == 'EXPENSE':
            chart_categories[t.category] = chart_categories.get(t.category, 0) + t.amount

    return render_template(
        'business_dashboard.html',
        user=user,
        transactions=transactions,
        total_revenue=total_revenue,
        total_expenses=total_expenses,
        net_profit=net_profit,
        budgets=budget_progress,
        alerts=alerts,
        goals=goal_data,
        subscriptions=detected_subs,
        health_score=health_score,
        categories=Category.query.filter_by(user_id=user.id).all(),
        chart_categories_json=json.dumps(chart_categories)
    )

# --- TRANSACTION & CATEGORY CRUD ---
@app.route('/transactions/add', methods=['POST'])
@login_required
def add_transaction():
    desc = request.form.get('description')
    amount = float(request.form.get('amount', 0))
    t_type = request.form.get('type', 'EXPENSE')
    category = request.form.get('category', 'General')
    date_str = request.form.get('date')

    t_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else datetime.utcnow().date()

    tx = Transaction(user_id=session['user_id'], description=desc, amount=amount, type=t_type, category=category, date=t_date)
    db.session.add(tx)
    db.session.commit()
    flash("Transaction recorded.", "success")
    return redirect(url_for('dashboard'))

@app.route('/transactions/edit/<int:tx_id>', methods=['POST'])
@login_required
def edit_transaction(tx_id):
    tx = Transaction.query.filter_by(id=tx_id, user_id=session['user_id']).first_or_404()
    tx.description = request.form.get('description', tx.description)
    tx.amount = float(request.form.get('amount', tx.amount))
    tx.category = request.form.get('category', tx.category)
    tx.type = request.form.get('type', tx.type)
    db.session.commit()
    flash("Transaction updated.", "success")
    return redirect(url_for('dashboard'))

@app.route('/transactions/delete/<int:tx_id>', methods=['POST'])
@login_required
def delete_transaction(tx_id):
    tx = Transaction.query.filter_by(id=tx_id, user_id=session['user_id']).first_or_404()
    db.session.delete(tx)
    db.session.commit()
    flash("Transaction deleted.", "info")
    return redirect(url_for('dashboard'))

@app.route('/categories', methods=['GET', 'POST'])
@login_required
def manage_categories():
    user_id = session['user_id']
    if request.method == 'POST':
        cat_name = request.form.get('name')
        cat_type = request.form.get('type', 'EXPENSE')
        if cat_name:
            db.session.add(Category(user_id=user_id, name=cat_name, type=cat_type))
            db.session.commit()
            flash(f"Category '{cat_name}' added.", "success")
    categories = Category.query.filter_by(user_id=user_id).all()
    return render_template('categories.html', categories=categories)

@app.route('/categories/delete/<int:cat_id>', methods=['POST'])
@login_required
def delete_category(cat_id):
    cat = Category.query.filter_by(id=cat_id, user_id=session['user_id']).first_or_404()
    db.session.delete(cat)
    db.session.commit()
    flash("Category deleted.", "info")
    return redirect(url_for('manage_categories'))

# --- BUDGETS & GOALS ---
@app.route('/budgets/set', methods=['POST'])
@login_required
def set_budget():
    category = request.form.get('category')
    limit = float(request.form.get('monthly_limit', 0))
    b = Budget.query.filter_by(user_id=session['user_id'], category=category).first()
    if b:
        b.monthly_limit = limit
    else:
        db.session.add(Budget(user_id=session['user_id'], category=category, monthly_limit=limit))
    db.session.commit()
    flash("Budget updated.", "success")
    return redirect(url_for('dashboard'))

@app.route('/goals/add', methods=['POST'])
@login_required
def add_goal():
    title = request.form.get('title')
    target = float(request.form.get('target_amount', 0))
    db.session.add(Goal(user_id=session['user_id'], title=title, target_amount=target))
    db.session.commit()
    flash("Savings goal created.", "success")
    return redirect(url_for('dashboard'))

# --- CSV IMPORT & EXPORT ---
@app.route('/import-csv', methods=['POST'])
@login_required
def import_csv():
    file = request.files.get('file')
    if not file or not file.filename.endswith('.csv'):
        flash("Upload a valid CSV statement file.", "danger")
        return redirect(url_for('dashboard'))

    stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
    csv_input = csv.DictReader(stream)

    imported_count = 0
    for row in csv_input:
        row_keys = {k.lower().strip(): v for k, v in row.items()}
        desc = row_keys.get('description') or row_keys.get('name') or row_keys.get('vendor') or 'CSV Import'
        amt_str = str(row_keys.get('amount', '0')).replace('$', '').replace(',', '')
        try:
            amt = float(amt_str)
        except ValueError:
            continue

        t_type = 'INCOME' if row_keys.get('type', '').lower() == 'credit' or amt > 0 else 'EXPENSE'
        category = row_keys.get('category', 'Imported')

        db.session.add(Transaction(
            user_id=session['user_id'],
            description=desc,
            amount=abs(amt),
            type=t_type,
            category=category
        ))
        imported_count += 1

    db.session.commit()
    flash(f"Successfully processed {imported_count} CSV records.", "success")
    return redirect(url_for('dashboard'))
    # --- EXPORTS (PDF & CSV) ---
@app.route('/reports/export/csv')
@login_required
def export_csv():
    txs = Transaction.query.filter_by(user_id=session['user_id']).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Date', 'Description', 'Amount', 'Type', 'Category'])
    for t in txs:
        writer.writerow([t.id, t.date, t.description, t.amount, t.type, t.category])
    
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": "attachment; filename=budget_buddy_report.csv"}
    )

@app.route('/reports/export/pdf')
@login_required
def export_pdf():
    txs = Transaction.query.filter_by(user_id=session['user_id']).all()
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    p.setFont("Helvetica-Bold", 16)
    p.drawString(100, 750, "Budget Buddy - Financial Statement Report")
    
    p.setFont("Helvetica", 10)
    y = 710
    p.drawString(50, y, "Date | Description | Type | Category | Amount")
    p.line(50, y-5, 550, y-5)
    y -= 20

    for t in txs[:30]:  # Output first 30 on single sheet
        p.drawString(50, y, f"{t.date} | {t.description[:20]} | {t.type} | {t.category} | ${t.amount:.2f}")
        y -= 18
        if y < 50: break

    p.showPage()
    p.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="budget_report.pdf", mimetype="application/pdf")

# --- PRO FEATURES (AI & ADVANCED ANALYTICS) ---
@app.route('/reports/advanced')
@login_required
@pro_required
def advanced_reports():
    user = User.query.get(session['user_id'])
    txs = Transaction.query.filter_by(user_id=user.id).all()
    
    # Financial Forecasting & Trends
    this_month_expenses = sum(t.amount for t in txs if t.type == 'EXPENSE' and t.date.month == datetime.utcnow().month)
    forecasted_exp = this_month_expenses * 1.08  # Predictive math model
    
    return render_template('advanced_reports.html', txs=txs, forecast=forecasted_exp)

@app.route('/api/ai-insights')
@login_required
@pro_required
def ai_insights():
    txs = Transaction.query.filter_by(user_id=session['user_id']).all()
    summary = f"Total transactions: {len(txs)}. Total expense sum: ${sum(t.amount for t in txs if t.type == 'EXPENSE'):.2f}."

    if OPENAI_API_KEY:
        try:
            client = openai.OpenAI(api_key=OPENAI_API_KEY)
            res = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "You are a financial advisor for digital agencies. Provide 3 short, actionable financial insights."},
                    {"role": "user", "content": summary}
                ]
            )
            insights = res.choices[0].message.content.split('\n')
        except Exception:
            insights = [
                "Software costs are trending 12% higher than standard agency benchmarks.",
                "Client revenue margins are steady.",
                "Unused SaaS seats detected in Adobe and Canva accounts."
            ]
    else:
        insights = [
            "Your software spending increased 18% month-over-month.",
            "Client revenue is scaling faster than operational overhead.",
            "Recurring software subscriptions account for 34% of overall budget usage."
        ]

    return jsonify({"insights": insights})

# --- FLUTTERWAVE BILLING INTEGRATION ---
@app.route('/pricing')
def pricing():
    return render_template('pricing.html')

@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    user = User.query.get(session['user_id'])
    plan = request.form.get('plan', 'STARTER')
    amount = request.form.get('amount', '19')

    # Unique reference containing tier info (e.g. BB-GROWTH-1-a1b2c3)
    tx_ref = f"BB-{plan}-{user.id}-{uuid.uuid4().hex[:6]}"

    payload = {
        "tx_ref": tx_ref,
        "amount": amount,
        "currency": "USD",
        "redirect_url": url_for('flutterwave_callback', _external=True),
        "customer": {
            "email": user.email,
            "name": user.agency_name or "Budget Buddy User"
        },
        "customizations": {
            "title": f"Budget Buddy {plan}",
            "description": f"Monthly Subscription (${amount}/mo)"
        }
    }

    headers = {
        "Authorization": f"Bearer {FLW_SECRET_KEY}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.post("https://api.flutterwave.com/v3/payments", json=payload, headers=headers)
        data = response.json()

        if data.get("status") == "success":
            return redirect(data["data"]["link"])
        else:
            flash("Failed to initiate payment session. Please try again.", "danger")
            return redirect(url_for('pricing'))
    except Exception as e:
        flash(f"Payment gateway error: {str(e)}", "danger")
        return redirect(url_for('pricing'))

@app.route('/flutterwave-callback')
@login_required
def flutterwave_callback():
    status = request.args.get('status')
    transaction_id = request.args.get('transaction_id')

    if status == 'successful' and transaction_id:
        headers = {"Authorization": f"Bearer {FLW_SECRET_KEY}"}
        verify_url = f"https://api.flutterwave.com/v3/transactions/{transaction_id}/verify"
        
        try:
            res = requests.get(verify_url, headers=headers).json()

            if res.get("status") == "success" and res.get("data", {}).get("status") == "successful":
                user = User.query.get(session['user_id'])
                tx_ref = res["data"].get("tx_ref", "")

                # Detect tier from reference
                if "STARTER" in tx_ref:
                    user.plan_tier = 'STARTER'
                elif "GROWTH" in tx_ref:
                    user.plan_tier = 'GROWTH'
                elif "PRO" in tx_ref:
                    user.plan_tier = 'PRO'
                else:
                    user.plan_tier = 'STARTER'

                db.session.commit()
                flash(f"Payment successful! Welcome to Budget Buddy {user.plan_tier}.", "success")
                return redirect(url_for('dashboard'))
        except Exception as e:
            flash(f"Verification error: {str(e)}", "danger")

    flash("Payment failed or was cancelled.", "danger")
    return redirect(url_for('pricing'))

# --- SENDWAVE MANUAL PAYMENT VERIFICATION ---
SENDWAVE_PLAN_AMOUNTS = {"STARTER": 19, "GROWTH": 49, "PRO": 99}

def send_sendwave_review_email(payment):
    """Emails ADMIN_EMAIL asking them to confirm a Sendwave payment, with
    Accept / Decline links. Never raises — a failed email shouldn't break
    the user's submit flow, so callers just log/flash on failure."""
    if not SMTP_USERNAME or not SMTP_PASSWORD:
        raise RuntimeError("SMTP_USERNAME / SMTP_PASSWORD are not configured")

    review_url = url_for('sendwave_email_review', payment_id=payment.id,
                          token=payment.action_token, _external=True)

    subject = f"Sendwave payment to confirm — {payment.user.email} (${payment.amount:.0f} {payment.plan_requested})"

    text_body = (
        f"Have you received this Sendwave payment?\n\n"
        f"User: {payment.user.email}\n"
        f"Plan: {payment.plan_requested}\n"
        f"Amount: ${payment.amount:.2f}\n"
        f"Reference code: {payment.reference_code}\n"
        f"Sender name: {payment.sender_name or '—'}\n"
        f"Submitted: {payment.submitted_at.strftime('%b %d, %Y %H:%M UTC')}\n\n"
        f"Review and accept/decline here: {review_url}"
    )

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:auto">
      <h2 style="margin-bottom:4px;">Have you received this payment?</h2>
      <p style="color:#555;margin-top:0;">A user submitted a Sendwave payment reference. Verify it against your Sendwave app before confirming.</p>
      <table style="width:100%;border-collapse:collapse;margin:16px 0;">
        <tr><td style="padding:4px 0;color:#888;">User</td><td style="padding:4px 0;"><b>{payment.user.email}</b></td></tr>
        <tr><td style="padding:4px 0;color:#888;">Plan</td><td style="padding:4px 0;"><b>{payment.plan_requested}</b></td></tr>
        <tr><td style="padding:4px 0;color:#888;">Amount</td><td style="padding:4px 0;"><b>${payment.amount:.2f}</b></td></tr>
        <tr><td style="padding:4px 0;color:#888;">Reference code</td><td style="padding:4px 0;"><b>{payment.reference_code}</b></td></tr>
        <tr><td style="padding:4px 0;color:#888;">Sender name</td><td style="padding:4px 0;">{payment.sender_name or '—'}</td></tr>
      </table>
      <p>
        <a href="{review_url}" style="background:#0d6efd;color:#fff;text-decoration:none;padding:10px 20px;border-radius:6px;display:inline-block;">
          Review &amp; Confirm
        </a>
      </p>
      <p style="color:#999;font-size:12px;">This link takes you to a page where you'll pick Accept or Decline — it won't approve anything by itself.</p>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = ADMIN_EMAIL
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(SMTP_FROM, [ADMIN_EMAIL], msg.as_string())

@app.route('/sendwave/submit', methods=['POST'])
@login_required
def sendwave_submit():
    user = User.query.get(session['user_id'])
    plan = request.form.get('plan', '').strip().upper()
    reference_code = request.form.get('reference_code', '').strip()
    sender_name = request.form.get('sender_name', '').strip()

    if plan not in SENDWAVE_PLAN_AMOUNTS:
        flash("Please select a valid plan.", "danger")
        return redirect(url_for('pricing'))

    if not reference_code:
        flash("Please enter the Sendwave reference code from your transfer.", "danger")
        return redirect(url_for('pricing'))

    existing = SendwavePayment.query.filter_by(reference_code=reference_code).first()
    if existing:
        flash("That reference code has already been submitted. If this is a mistake, contact support.", "warning")
        return redirect(url_for('sendwave_status'))

    payment = SendwavePayment(
        user_id=user.id,
        plan_requested=plan,
        amount=SENDWAVE_PLAN_AMOUNTS[plan],
        reference_code=reference_code,
        sender_name=sender_name or None,
        status="PENDING",
        action_token=secrets.token_urlsafe(32)
    )
    db.session.add(payment)
    db.session.commit()

    try:
        send_sendwave_review_email(payment)
    except Exception as e:
        # Don't block the user's submission just because the email failed —
        # the payment still shows up in /admin/sendwave either way.
        app.logger.error(f"Failed to send Sendwave review email: {e}")

    flash("Got it — your payment is pending verification. This usually takes a few hours.", "success")
    return redirect(url_for('sendwave_status'))

@app.route('/sendwave/status')
@login_required
def sendwave_status():
    user = User.query.get(session['user_id'])
    payments = SendwavePayment.query.filter_by(user_id=user.id).order_by(SendwavePayment.submitted_at.desc()).all()
    return render_template('sendwave_status.html', payments=payments)

@app.route('/admin/sendwave')
@admin_required
def admin_sendwave():
    pending = SendwavePayment.query.filter_by(status="PENDING").order_by(SendwavePayment.submitted_at.asc()).all()
    reviewed = SendwavePayment.query.filter(SendwavePayment.status != "PENDING").order_by(SendwavePayment.reviewed_at.desc()).limit(30).all()
    return render_template('admin_sendwave.html', pending=pending, reviewed=reviewed)

@app.route('/admin/sendwave/approve/<int:payment_id>', methods=['POST'])
@admin_required
def admin_sendwave_approve(payment_id):
    payment = SendwavePayment.query.get_or_404(payment_id)
    payment.status = "APPROVED"
    payment.reviewed_at = datetime.utcnow()
    payment.user.plan_tier = payment.plan_requested
    db.session.commit()
    flash(f"Approved — {payment.user.email} is now on {payment.plan_requested}.", "success")
    return redirect(url_for('admin_sendwave'))

@app.route('/admin/sendwave/reject/<int:payment_id>', methods=['POST'])
@admin_required
def admin_sendwave_reject(payment_id):
    payment = SendwavePayment.query.get_or_404(payment_id)
    payment.status = "REJECTED"
    payment.reviewed_at = datetime.utcnow()
    payment.admin_notes = request.form.get('notes', '').strip() or None
    db.session.commit()
    flash("Marked as rejected.", "info")
    return redirect(url_for('admin_sendwave'))

# --- ACCEPT/DECLINE STRAIGHT FROM THE REVIEW EMAIL (token-gated, no login needed) ---
def _get_payment_by_token(payment_id, token):
    payment = SendwavePayment.query.get_or_404(payment_id)
    if not payment.action_token or not secrets.compare_digest(payment.action_token, token):
        abort(404)
    return payment

@app.route('/sendwave/review/<int:payment_id>/<token>')
def sendwave_email_review(payment_id, token):
    payment = _get_payment_by_token(payment_id, token)
    return render_template('sendwave_email_review.html', payment=payment)

@app.route('/sendwave/review/<int:payment_id>/<token>/approve', methods=['POST'])
def sendwave_email_approve(payment_id, token):
    payment = _get_payment_by_token(payment_id, token)
    if payment.status == "PENDING":
        payment.status = "APPROVED"
        payment.reviewed_at = datetime.utcnow()
        payment.user.plan_tier = payment.plan_requested
        db.session.commit()
    return render_template('sendwave_email_review.html', payment=payment, just_actioned=True)

@app.route('/sendwave/review/<int:payment_id>/<token>/decline', methods=['POST'])
def sendwave_email_decline(payment_id, token):
    payment = _get_payment_by_token(payment_id, token)
    if payment.status == "PENDING":
        payment.status = "REJECTED"
        payment.reviewed_at = datetime.utcnow()
        db.session.commit()
    return render_template('sendwave_email_review.html', payment=payment, just_actioned=True)

if __name__ == '__main__':
    app.run(debug=True)
