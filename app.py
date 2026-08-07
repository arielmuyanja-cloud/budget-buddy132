import os
import csv
import io
import json
from datetime import datetime
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, 
    url_for, flash, jsonify, session
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "budget-buddy-super-secret-key-2026")

# --- DATABASE CONFIGURATION ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL', f"sqlite:///{os.path.join(BASE_DIR, 'budget.db')}"
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- DATABASE MODELS ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    agency_name = db.Column(db.String(120), nullable=True)
    plan_tier = db.Column(db.String(20), default="FREE")  # "FREE" or "PRO"
    is_verified = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    transactions = db.relationship('Transaction', backref='user', lazy=True, cascade="all, delete-orphan")
    categories = db.relationship('Category', backref='user', lazy=True, cascade="all, delete-orphan")
    budgets = db.relationship('Budget', backref='user', lazy=True, cascade="all, delete-orphan")
    goals = db.relationship('Goal', backref='user', lazy=True, cascade="all, delete-orphan")

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    type = db.Column(db.String(10), nullable=False)  # 'INCOME' or 'EXPENSE'

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    description = db.Column(db.String(200), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    type = db.Column(db.String(10), nullable=False)  # 'INCOME' or 'EXPENSE'
    category = db.Column(db.String(50), nullable=False, default="General")

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

# Initialize Database Schema
with app.app_context():
    db.create_all()

# --- AUTH DECORATOR ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please log in to access this page.", "warning")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- PRO PLAN & FINANCIAL ANALYSIS ENGINES ---
KNOWN_SUBSCRIPTIONS = [
    "adobe", "chatgpt", "canva", "google workspace", "slack", 
    "zoom", "semrush", "hubspot", "github", "render", "clickup", "meta"
]

def analyze_subscriptions(transactions):
    detected = []
    seen = set()
    for t in transactions:
        desc_lower = t.description.lower()
        for sub in KNOWN_SUBSCRIPTIONS:
            if sub in desc_lower and sub not in seen:
                detected.append({
                    "name": t.description, 
                    "amount": t.amount, 
                    "category": t.category
                })
                seen.add(sub)
                break
    return detected

def calculate_health_score(revenue, expenses, budgets, user_id):
    score = 100
    if revenue > 0 and (expenses / revenue) > 0.7:
        score -= 20
    elif revenue == 0 and expenses > 0:
        score -= 30
        
    for b in budgets:
        spent = sum(
            t.amount for t in Transaction.query.filter_by(
                user_id=user_id, category=b.category, type='EXPENSE'
            ).all()
        )
        if spent > b.monthly_limit and b.monthly_limit > 0:
            score -= 10

    return max(0, min(100, score))

def generate_ai_insights(revenue, expenses, transactions):
    insights = []
    if expenses > revenue and revenue > 0:
        insights.append("Warning: Total expenses exceed revenue this month.")
    elif revenue > 0:
        margin = ((revenue - expenses) / revenue) * 100
        insights.append(f"Healthy net profit margin at {margin:.1f}%.")

    subs = analyze_subscriptions(transactions)
    if subs:
        total_sub_cost = sum(s['amount'] for s in subs)
        insights.append(f"Detected {len(subs)} active subscriptions costing approx. ${total_sub_cost:.2f}/month.")
    else:
        insights.append("No active software subscription spikes detected in current statements.")
        
    return insights

# --- FEATURE 1: AUTHENTICATION ROUTES ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')
        agency_name = request.form.get('agency_name')

        if User.query.filter_by(email=email).first():
            flash("Email address already registered.", "danger")
            return redirect(url_for('register'))

        hashed_pw = generate_password_hash(password, method='scrypt')
        new_user = User(email=email, password_hash=hashed_pw, agency_name=agency_name)
        db.session.add(new_user)
        db.session.commit()

        # Feature 4: Seed default categories upon registration
        defaults = [
            Category(user_id=new_user.id, name="Software", type="EXPENSE"),
            Category(user_id=new_user.id, name="Advertising", type="EXPENSE"),
            Category(user_id=new_user.id, name="Payroll", type="EXPENSE"),
            Category(user_id=new_user.id, name="Client Revenue", type="INCOME")
        ]
        db.session.add_all(defaults)
        db.session.commit()

        session['user_id'] = new_user.id
        flash("Registration successful! Welcome aboard.", "success")
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
            flash("Logged in successfully.", "success")
            return redirect(url_for('dashboard'))
        else:
            flash("Invalid email or password.", "danger")

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for('login'))

# --- FEATURE 2, 8 & 9: MAIN DASHBOARD & CHARTS ---
@app.route('/dashboard')
@login_required
def dashboard():
    user = User.query.get(session['user_id'])
    query_str = request.args.get('q', '').strip().lower()
    
    tx_query = Transaction.query.filter_by(user_id=user.id)
    
    # Feature 8: Search Filter
    if query_str:
        tx_query = tx_query.filter(Transaction.description.ilike(f"%{query_str}%"))
        
    txs = tx_query.order_by(Transaction.date.desc()).all()
    
    total_revenue = sum(t.amount for t in txs if t.type == 'INCOME')
    total_expenses = sum(t.amount for t in txs if t.type == 'EXPENSE')
    net_profit = total_revenue - total_expenses

    # Feature 5 & 6: Budgets, Progress Bars & Alerts
    budgets = Budget.query.filter_by(user_id=user.id).all()
    budget_progress = []
    alerts = []
    for b in budgets:
        spent = sum(t.amount for t in txs if t.type == 'EXPENSE' and t.category.lower() == b.category.lower())
        pct = min(100, int((spent / b.monthly_limit) * 100)) if b.monthly_limit > 0 else 0
        rem = b.monthly_limit - spent
        budget_progress.append({
            "id": b.id, "category": b.category, "limit": b.monthly_limit,
            "spent": spent, "remaining": rem, "pct": pct
        })
        if spent > b.monthly_limit:
            alerts.append(f"Budget Alert: Category '{b.category}' exceeded set threshold by ${abs(rem):.2f}!")

    # Feature 10 (PRO): Goal Tracking
    goals = Goal.query.filter_by(user_id=user.id).all()
    goal_data = []
    for g in goals:
        pct = min(100, int((g.current_amount / g.target_amount) * 100)) if g.target_amount > 0 else 0
        goal_data.append({"id": g.id, "title": g.title, "target": g.target_amount, "current": g.current_amount, "pct": pct})

    # PRO Features
    health_score = calculate_health_score(total_revenue, total_expenses, budgets, user.id)
    ai_insights = generate_ai_insights(total_revenue, total_expenses, txs)
    detected_subs = analyze_subscriptions(txs)
    categories = Category.query.filter_by(user_id=user.id).all()

    return render_template(
        'business_dashboard.html',
        user=user,
        transactions=txs,
        total_revenue=total_revenue,
        total_expenses=total_expenses,
        net_profit=net_profit,
        budgets=budget_progress,
        alerts=alerts,
        goals=goal_data,
        health_score=health_score,
        ai_insights=ai_insights,
        subscriptions=detected_subs,
        categories=categories,
        search_query=query_str
    )

# --- FEATURE 3: TRANSACTION CRUD ROUTING ---
@app.route('/transactions/add', methods=['POST'])
@login_required
def add_transaction():
    desc = request.form.get('description')
    amount = float(request.form.get('amount', 0))
    t_type = request.form.get('type')
    category = request.form.get('category', 'General')
    
    tx = Transaction(user_id=session['user_id'], description=desc, amount=amount, type=t_type, category=category)
    db.session.add(tx)
    db.session.commit()
    flash("Transaction recorded.", "success")
    return redirect(url_for('dashboard'))

@app.route('/transactions/delete/<int:tx_id>', methods=['POST'])
@login_required
def delete_transaction(tx_id):
    tx = Transaction.query.filter_by(id=tx_id, user_id=session['user_id']).first_or_404()
    db.session.delete(tx)
    db.session.commit()
    flash("Transaction deleted.", "info")
    return redirect(url_for('dashboard'))

# --- FEATURE 4: CATEGORY MANAGEMENT ---
@app.route('/categories/add', methods=['POST'])
@login_required
def add_category():
    name = request.form.get('name')
    cat_type = request.form.get('type', 'EXPENSE')
    if name:
        cat = Category(user_id=session['user_id'], name=name, type=cat_type)
        db.session.add(cat)
        db.session.commit()
        flash(f"Category '{name}' created.", "success")
    return redirect(url_for('dashboard'))

# --- FEATURE 5 & 6: BUDGETS & GOALS ---
@app.route('/budgets/set', methods=['POST'])
@login_required
def set_budget():
    category = request.form.get('category')
    limit = float(request.form.get('monthly_limit', 0))
    
    b = Budget.query.filter_by(user_id=session['user_id'], category=category).first()
    if b:
        b.monthly_limit = limit
    else:
        b = Budget(user_id=session['user_id'], category=category, monthly_limit=limit)
        db.session.add(b)
        
    db.session.commit()
    flash("Budget saved.", "success")
    return redirect(url_for('dashboard'))

@app.route('/goals/add', methods=['POST'])
@login_required
def add_goal():
    title = request.form.get('title')
    target = float(request.form.get('target_amount', 0))
    
    goal = Goal(user_id=session['user_id'], title=title, target_amount=target, current_amount=0.0)
    db.session.add(goal)
    db.session.commit()
    flash("Goal initialized.", "success")
    return redirect(url_for('dashboard'))

# --- FEATURE 7 & HOMEPAGE CSV SCANNER ---
@app.route('/upload-statements', methods=['POST'])
def upload_statements():
    files = request.files.getlist('csv_files')
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None
    
    # 3-file Free Limit Enforcement
    is_pro = user and user.plan_tier == 'PRO'
    max_allowed = 50 if is_pro else 3

    if len(files) > max_allowed:
        return jsonify({
            "error": f"Free plan allows up to {max_allowed} CSV files. Upgrade to PRO to scan up to 50 statements."
        }), 403

    parsed_count = 0
    total_spend = 0.0

    for file in files:
        if file and file.filename.endswith('.csv'):
            stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
            csv_input = csv.DictReader(stream)
            for row in csv_input:
                row_keys = {k.lower().strip(): v for k, v in row.items()}
                desc = row_keys.get('description') or row_keys.get('name') or 'CSV Transaction'
                amt_str = str(row_keys.get('amount', '0')).replace('$', '').replace(',', '')
                try:
                    amt = float(amt_str)
                except ValueError:
                    continue

                t_type = 'INCOME' if row_keys.get('transaction type', '').lower() == 'credit' or amt > 0 else 'EXPENSE'
                category = row_keys.get('category', 'Imported')

                if user_id:
                    tx = Transaction(user_id=user_id, description=desc, amount=abs(amt), type=t_type, category=category)
                    db.session.add(tx)
                
                if t_type == 'EXPENSE':
                    total_spend += abs(amt)
                parsed_count += 1

    if user_id:
        db.session.commit()

    return jsonify({
        "status": "success",
        "processed_files": len(files),
        "total_records": parsed_count,
        "estimated_annual_waste": round(total_spend * 0.15, 2)  # Teaser formula: ~15% estimated SaaS waste
    })

# --- HOMEPAGE / LANDING ROUTE ---
@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    app.run(debug=True)
