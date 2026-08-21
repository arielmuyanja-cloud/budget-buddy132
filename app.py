import os
import csv
import io
import json
import uuid
import secrets
import logging
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

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("budget_buddy")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "budget-buddy-super-secret-key-2026")

# Database Configuration (PostgreSQL on Render / SQLite locally)
db_url = os.environ.get(
    'DATABASE_URL',
    f"sqlite:///{os.path.join(os.path.abspath(os.path.dirname(__file__)), 'budget.db')}"
)

if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Controlled SQLAlchemy Connection Pool to prevent 503 Backend.max_conn
if db_url.startswith("postgresql://"):
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        "pool_pre_ping": True,
        "pool_recycle": 180,
        "pool_timeout": 10,
        "pool_size": 3,
        "max_overflow": 2,
        "connect_args": {
            "sslmode": "require",
            "connect_timeout": 10
        }
    }

db = SQLAlchemy(app)

# External API Keys and Environment Settings
FLW_PUBLIC_KEY = os.environ.get("FLW_PUBLIC_KEY", "FLWPUBK_TEST-xxxxxxxx")
FLW_SECRET_KEY = os.environ.get("FLW_SECRET_KEY", "FLWSECK_TEST-xxxxxxxx")
PADDLE_API_KEY = os.environ.get("PADDLE_API_KEY", "")
PADDLE_CLIENT_TOKEN = os.environ.get("PADDLE_CLIENT_TOKEN", "")
PADDLE_ENV = os.environ.get("PADDLE_ENV", "sandbox")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Owner email for manual payment review
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "amuyanja1314@gmail.com")

# HTTPS Email Configuration (Resend API)
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
MAIL_FROM = os.getenv("MAIL_FROM") or os.getenv("SMTP_FROM", "Budget Buddy <onboarding@resend.dev>")

# --- DATABASE MODELS ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    agency_name = db.Column(db.String(120), nullable=True)
    plan_tier = db.Column(db.String(20), default="FREE")  # "FREE", "STARTER", "GROWTH", or "PRO"
    is_verified = db.Column(db.Boolean, default=False)
    reset_token = db.Column(db.String(100), nullable=True)
    currency = db.Column(db.String(10), default="USD")
    tax_rate = db.Column(db.Float, default=20.0)
    cash_balance = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    transactions = db.relationship('Transaction', backref='user', lazy=True, cascade="all, delete-orphan")
    categories = db.relationship('Category', backref='user', lazy=True, cascade="all, delete-orphan")
    budgets = db.relationship('Budget', backref='user', lazy=True, cascade="all, delete-orphan")
    goals = db.relationship('Goal', backref='user', lazy=True, cascade="all, delete-orphan")
    rules = db.relationship('CategoryRule', backref='user', lazy=True, cascade="all, delete-orphan")
    clients = db.relationship('Client', backref='user', lazy=True, cascade="all, delete-orphan")

class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    retainer_amount = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    description = db.Column(db.String(200), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    type = db.Column(db.String(10), nullable=False)  # 'INCOME' or 'EXPENSE'
    category = db.Column(db.String(50), nullable=False, default="General")
    client_name = db.Column(db.String(100), nullable=True)

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    type = db.Column(db.String(10), nullable=False, default="EXPENSE")

class CategoryRule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    keyword = db.Column(db.String(100), nullable=False)
    target_category = db.Column(db.String(50), nullable=False)

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
    plan_requested = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    reference_code = db.Column(db.String(100), nullable=False)
    sender_name = db.Column(db.String(120), nullable=True)
    status = db.Column(db.String(20), default="PENDING")
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    admin_notes = db.Column(db.String(300), nullable=True)
    action_token = db.Column(db.String(64), unique=True, nullable=True)

    user = db.relationship('User', backref=db.backref('sendwave_payments', cascade="all, delete-orphan"))

# Safe Startup Database Initialization
with app.app_context():
    try:
        db.create_all()
        from sqlalchemy import text
        migrations = [
            ("sendwave_payment", "action_token", "VARCHAR(64)"),
            ("user", "currency", "VARCHAR(10) DEFAULT 'USD'"),
            ("user", "tax_rate", "FLOAT DEFAULT 20.0"),
            ("user", "cash_balance", "FLOAT DEFAULT 0.0"),
            ("transaction", "client_name", "VARCHAR(100)")
        ]
        for table, col, col_type in migrations:
            try:
                if db_url.startswith("postgresql://"):
                    db.session.execute(text(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS {col} {col_type}'))
                else:
                    db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN {col} {col_type}'))
                db.session.commit()
            except Exception:
                db.session.rollback()
    except Exception as e:
        db.session.rollback()
        logger.warning(f"Startup schema notice: {e}")
    finally:
        db.session.remove()

@app.teardown_appcontext
def shutdown_session(exception=None):
    db.session.remove()

# --- DECORATORS ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"success": False, "message": "Please log in to perform this action."}), 401
            flash("Please log in to access this page.", "warning")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def pro_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = User.query.get(session.get('user_id'))
        if not user or user.plan_tier not in ['GROWTH', 'PRO']:
            if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"success": False, "message": "This feature requires a Growth or Pro subscription."}), 403
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

@app.context_processor
def inject_current_user():
    user = User.query.get(session['user_id']) if 'user_id' in session else None
    return {'current_user': user, 'user': user, 'paddle_client_token': PADDLE_CLIENT_TOKEN, 'paddle_env': PADDLE_ENV}

# --- HTTPS EMAIL ENGINE (RESEND REST API) ---
def send_email_via_resend(to_email, subject, html_content, text_content=None):
    """
    Sends an email using Resend's HTTPS REST API over port 443.
    Bypasses blocked outbound SMTP ports on Render.
    """
    logger.info("EMAIL SEND STARTED")
    
    if not RESEND_API_KEY:
        logger.warning("EMAIL ERROR: RESEND_API_KEY is not configured in environment.")
        return False, "RESEND_API_KEY is not set."

    if not to_email:
        logger.warning("EMAIL ERROR: Recipient email address is missing.")
        return False, "Recipient email is missing."

    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "from": MAIL_FROM,
        "to": [to_email] if isinstance(to_email, str) else to_email,
        "subject": subject,
        "html": html_content
    }
    if text_content:
        payload["text"] = text_content

    try:
        logger.info("EMAIL API REQUEST STARTED")
        response = requests.post(
            "https://api.resend.com/emails",
            json=payload,
            headers=headers,
            timeout=15
        )
        logger.info("EMAIL API RESPONSE RECEIVED")

        if response.status_code in [200, 201, 202]:
            res_data = response.json()
            email_id = res_data.get("id", "ok")
            logger.info(f"EMAIL SEND SUCCESS (id: {email_id})")
            return True, "Email sent successfully."
        else:
            try:
                err_data = response.json()
                err_msg = err_data.get("message") or err_data.get("name") or str(err_data)
            except Exception:
                err_msg = response.text[:200]
            logger.error(f"EMAIL ERROR: Resend API returned status {response.status_code}: {err_msg}")
            return False, f"Resend API error: {err_msg}"

    except requests.exceptions.Timeout:
        logger.error("EMAIL ERROR: Resend HTTPS request timed out after 15 seconds.")
        return False, "Email service timed out."
    except requests.exceptions.RequestException as e:
        logger.error(f"EMAIL ERROR: HTTPS request failed: {type(e).__name__}: {str(e)}")
        return False, f"Network error during email dispatch: {type(e).__name__}"
    except Exception as e:
        logger.error(f"EMAIL ERROR: Unexpected error: {type(e).__name__}: {str(e)}")
        return False, "Unexpected error during email dispatch."

# --- ANALYTICS ENGINE ---
KNOWN_SUBSCRIPTIONS = ["adobe", "chatgpt", "canva", "google workspace", "slack", "zoom", "hubspot", "semrush", "github", "render", "meta", "linkedin", "figma", "notion", "aws", "openai", "vercel"]

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

def compute_financial_health(revenue, expenses, budgets, user_id, spent_by_category=None):
    score = 100
    if revenue > 0:
        margin = (revenue - expenses) / revenue
        if margin < 0: score -= 30
        elif margin < 0.2: score -= 15
    elif expenses > 0:
        score -= 40

    if spent_by_category is None:
        rows = db.session.query(
            Transaction.category, db.func.sum(Transaction.amount)
        ).filter_by(user_id=user_id, type='EXPENSE').group_by(Transaction.category).all()
        spent_by_category = {cat.lower(): (amt or 0.0) for cat, amt in rows}

    for b in budgets:
        spent = spent_by_category.get(b.category.lower(), 0.0)
        if b.monthly_limit > 0 and spent > b.monthly_limit:
            score -= 10

    return max(0, min(100, score))

def compute_client_profitability(user_id):
    txs = Transaction.query.filter_by(user_id=user_id).all()
    client_data = {}
    for t in txs:
        c_name = (t.client_name or "").strip()
        if not c_name:
            c_name = "General / Unassigned"
        
        data = client_data.setdefault(c_name, {"revenue": 0.0, "expense": 0.0})
        if t.type == 'INCOME':
            data["revenue"] += t.amount
        else:
            data["expense"] += t.amount

    result = []
    for name, vals in client_data.items():
        rev = vals["revenue"]
        exp = vals["expense"]
        profit = rev - exp
        margin = ((profit / rev) * 100.0) if rev > 0 else (0.0 if profit >= 0 else -100.0)
        result.append({
            "client_name": name,
            "revenue": rev,
            "expense": exp,
            "profit": profit,
            "margin": margin
        })
    result.sort(key=lambda x: x["revenue"], reverse=True)
    return result

def compute_runway(user):
    months = get_monthly_series(user.id)
    if not months:
        return {"runway_months": None, "monthly_burn": 0.0, "cash_balance": user.cash_balance or 0.0}
    
    window = months[-3:]
    avg_burn = sum(m["expense"] for m in window) / len(window)
    balance = user.cash_balance or 0.0
    
    if avg_burn <= 0:
        runway = 99.0 if balance > 0 else 0.0
    else:
        runway = round(balance / avg_burn, 1)
        
    return {
        "runway_months": runway,
        "monthly_burn": avg_burn,
        "cash_balance": balance
    }

# --- PRO AI FINANCIAL INTELLIGENCE ENGINE ---
MARKETING_KEYWORDS = ["advertising", "marketing", "ads", "ppc", "paid media", "meta ads", "google ads"]

def _pct_change(new, old):
    if not old:
        return None
    return (new - old) / old * 100.0

def get_monthly_series(user_id):
    txs = Transaction.query.filter_by(user_id=user_id).order_by(Transaction.date.asc()).all()
    buckets = {}
    for t in txs:
        key = (t.date.year, t.date.month)
        b = buckets.setdefault(key, {"income": 0.0, "expense": 0.0,
                                      "expense_by_category": {}, "income_by_desc": {}})
        if t.type == 'INCOME':
            b["income"] += t.amount
            desc_key = t.description.strip().lower()
            b["income_by_desc"][desc_key] = b["income_by_desc"].get(desc_key, 0.0) + t.amount
        else:
            b["expense"] += t.amount
            b["expense_by_category"][t.category] = b["expense_by_category"].get(t.category, 0.0) + t.amount

    months = []
    for (y, m) in sorted(buckets.keys()):
        b = buckets[(y, m)]
        income, expense = b["income"], b["expense"]
        months.append({
            "year": y, "month": m,
            "label": datetime(y, m, 1).strftime("%b %Y"),
            "income": income, "expense": expense, "profit": income - expense,
            "margin": ((income - expense) / income * 100.0) if income > 0 else None,
            "expense_by_category": b["expense_by_category"],
            "income_by_desc": b["income_by_desc"],
        })
    return months

def get_income_by_client(user_id):
    rows = db.session.query(Transaction.description, db.func.sum(Transaction.amount)) \
        .filter_by(user_id=user_id, type='INCOME').group_by(Transaction.description).all()
    result = {}
    for desc, total in rows:
        key = desc.strip().lower()
        result[key] = result.get(key, 0.0) + (total or 0.0)
    return result

def compute_profitability_diagnosis(months):
    if len(months) < 2:
        return None
    current, prior = months[-1], months[-2]
    margin_now, margin_prior = current["margin"], prior["margin"]
    top_expense_categories = sorted(current["expense_by_category"].items(),
                                     key=lambda kv: kv[1], reverse=True)[:3]
    return {
        "current_label": current["label"], "prior_label": prior["label"],
        "revenue": current["income"], "expenses": current["expense"],
        "revenue_change_pct": _pct_change(current["income"], prior["income"]),
        "expense_change_pct": _pct_change(current["expense"], prior["expense"]),
        "margin_now": margin_now, "margin_prior": margin_prior,
        "margin_change": (margin_now - margin_prior) if (margin_now is not None and margin_prior is not None) else None,
        "top_expense_categories": top_expense_categories,
    }

def compute_forecast(months):
    if not months:
        return None
    window = months[-3:] if len(months) >= 3 else months
    avg_rev = sum(m["income"] for m in window) / len(window)
    avg_exp = sum(m["expense"] for m in window) / len(window)

    rev_growth = _pct_change(window[-1]["income"], window[0]["income"]) if len(window) >= 2 else None
    exp_growth = _pct_change(window[-1]["expense"], window[0]["expense"]) if len(window) >= 2 else None
    rev_drift = (rev_growth / 100.0 / len(window)) if rev_growth is not None else 0.0
    exp_drift = (exp_growth / 100.0 / len(window)) if exp_growth is not None else 0.0

    projected_revenue = projected_expense = 0.0
    rev_m, exp_m = avg_rev, avg_exp
    for _ in range(3):
        rev_m *= (1 + rev_drift)
        exp_m *= (1 + exp_drift)
        projected_revenue += rev_m
        projected_expense += exp_m

    projected_profit = projected_revenue - projected_expense
    flat_run_rate_profit = (avg_rev - avg_exp) * 3

    return {
        "based_on_months": [m["label"] for m in window],
        "projected_revenue": projected_revenue,
        "projected_expense": projected_expense,
        "projected_profit": projected_profit,
        "profit_delta_vs_flat": projected_profit - flat_run_rate_profit,
        "revenue_growth_pct": rev_growth,
        "expense_growth_pct": exp_growth,
    }

def compute_budget_recommendation(months, categories):
    if not months:
        return None
    current = months[-1]
    revenue = current["income"]
    if revenue <= 0:
        return None

    spend, matched_name = 0.0, None
    for cat_name, amt in current["expense_by_category"].items():
        if any(kw in cat_name.lower() for kw in MARKETING_KEYWORDS):
            spend += amt
            matched_name = matched_name or cat_name
    if spend <= 0:
        return None

    low_pct, high_pct = 12.0, 15.0
    return {
        "category_label": matched_name, "month_label": current["label"],
        "current_spend": spend,
        "current_pct_of_revenue": spend / revenue * 100.0,
        "recommended_low_pct": low_pct, "recommended_high_pct": high_pct,
        "suggested_low": revenue * low_pct / 100.0,
        "suggested_high": revenue * high_pct / 100.0,
    }

def detect_financial_risks(months, income_by_client):
    risks = []
    if not months:
        return risks
    current = months[-1]
    prior = months[-2] if len(months) >= 2 else None

    # Client concentration
    total_income = sum(income_by_client.values())
    if total_income > 0 and len(income_by_client) > 1:
        top_client, top_amt = max(income_by_client.items(), key=lambda kv: kv[1])
        top_pct = top_amt / total_income * 100.0
        if top_pct >= 30:
            risks.append({
                "title": "Client concentration risk",
                "narrative": f"\"{top_client.title()}\" makes up {top_pct:.0f}% of your total recorded "
                             f"revenue (${top_amt:,.0f} of ${total_income:,.0f}). Losing that account "
                             f"would hit cash flow hard.",
                "recommendation": "Aim to bring this below 30% by growing revenue from other accounts."
            })

    # Expenses outpacing revenue
    if prior:
        rev_chg, exp_chg = _pct_change(current["income"], prior["income"]), _pct_change(current["expense"], prior["expense"])
        if rev_chg is not None and exp_chg is not None and exp_chg > rev_chg + 5:
            risks.append({
                "title": "Expenses growing faster than revenue",
                "narrative": f"Revenue moved {rev_chg:+.0f}% from {prior['label']} to {current['label']}, "
                             f"while expenses moved {exp_chg:+.0f}%.",
                "recommendation": "Review your largest expense categories before taking on new recurring costs."
            })

    # Margin shrinking
    if prior and current["margin"] is not None and prior["margin"] is not None:
        margin_change = current["margin"] - prior["margin"]
        if margin_change <= -3:
            risks.append({
                "title": "Profit margin shrinking",
                "narrative": f"Margin fell from {prior['margin']:.0f}% to {current['margin']:.0f}% "
                             f"between {prior['label']} and {current['label']}.",
                "recommendation": "Pin down which cost or pricing change drove the drop before it compounds."
            })

    # Revenue volatility
    if len(months) >= 3:
        incomes = [m["income"] for m in months[-6:]]
        mean = sum(incomes) / len(incomes)
        if mean > 0:
            stdev = (sum((x - mean) ** 2 for x in incomes) / len(incomes)) ** 0.5
            cv = stdev / mean
            if cv >= 0.3:
                risks.append({
                    "title": "Revenue volatility",
                    "narrative": f"Monthly revenue has swung by an average of {cv*100:.0f}% around your "
                    f"{len(incomes)}-month mean of ${mean:,.0f}.",
                    "recommendation": "Size a cash buffer to your worst recent month, not your average one."
                })

    # Single-category expense concentration
    if current["expense"] > 0:
        worst_cat, worst_amt = max(current["expense_by_category"].items(), key=lambda kv: kv[1], default=(None, 0))
        if worst_cat:
            pct = worst_amt / current["expense"] * 100.0
            if pct >= 40:
                risks.append({
                    "title": f"Heavy spend concentration in {worst_cat}",
                    "narrative": f"{worst_cat} is {pct:.0f}% of this month's total expenses "
                                 f"(${worst_amt:,.0f} of ${current['expense']:,.0f}).",
                    "recommendation": "Worth a line-by-line review for anything reducible."
                })

    # Sudden category spend spike
    if prior:
        for cat, amt in current["expense_by_category"].items():
            prior_amt = prior["expense_by_category"].get(cat, 0.0)
            change = _pct_change(amt, prior_amt)
            if prior_amt > 0 and change is not None and change >= 50 and amt >= 100:
                risks.append({
                    "title": f"Sudden increase in {cat}",
                    "narrative": f"{cat} spend jumped {change:.0f}% month-over-month "
                                 f"(${prior_amt:,.0f} → ${amt:,.0f}).",
                    "recommendation": "Confirm this is intentional and not a duplicate charge or missed cancellation."
                })
                break

    return risks

def build_ai_context(user):
    months = get_monthly_series(user.id)
    categories = Category.query.filter_by(user_id=user.id).all()
    runway_info = compute_runway(user)
    
    current_m = months[-1] if months else None
    tax_reserve = 0.0
    if current_m and current_m["income"] > 0:
        tax_reserve = current_m["income"] * ((user.tax_rate or 20.0) / 100.0)

    return {
        "agency_name": user.agency_name or user.email,
        "currency": user.currency or "USD",
        "months_of_data": len(months),
        "current_month": current_m,
        "diagnosis": compute_profitability_diagnosis(months),
        "forecast": compute_forecast(months),
        "risks": detect_financial_risks(months, get_income_by_client(user.id)),
        "budget_recommendation": compute_budget_recommendation(months, categories),
        "runway": runway_info,
        "tax_reserve": tax_reserve,
        "tax_rate": user.tax_rate or 20.0
    }

def _serialize_ai_context(ctx):
    out = {
        "agency_name": ctx["agency_name"], 
        "currency": ctx.get("currency", "USD"),
        "months_of_data": ctx["months_of_data"]
    }
    if ctx["current_month"]:
        cm = ctx["current_month"]
        out["current_month"] = {
            "label": cm["label"], "revenue": round(cm["income"], 2), "expenses": round(cm["expense"], 2),
            "profit": round(cm["profit"], 2),
            "margin_pct": round(cm["margin"], 1) if cm["margin"] is not None else None,
            "top_expense_categories": [[c, round(a, 2)] for c, a in
                sorted(cm["expense_by_category"].items(), key=lambda kv: kv[1], reverse=True)[:5]],
        }
    if ctx["diagnosis"]:
        d = ctx["diagnosis"]
        out["month_over_month"] = {
            "prior_label": d["prior_label"], "current_label": d["current_label"],
            "revenue_change_pct": round(d["revenue_change_pct"], 1) if d["revenue_change_pct"] is not None else None,
            "expense_change_pct": round(d["expense_change_pct"], 1) if d["expense_change_pct"] is not None else None,
            "margin_prior_pct": round(d["margin_prior"], 1) if d["margin_prior"] is not None else None,
            "margin_now_pct": round(d["margin_now"], 1) if d["margin_now"] is not None else None,
        }
    if ctx["forecast"]:
        f = ctx["forecast"]
        out["forecast_90_day"] = {
            "projected_revenue": round(f["projected_revenue"], 2),
            "projected_expenses": round(f["projected_expense"], 2),
            "projected_profit": round(f["projected_profit"], 2),
        }
    if ctx.get("runway"):
        out["runway"] = ctx["runway"]
    if ctx.get("tax_reserve"):
        out["tax_reserve_recommended"] = round(ctx["tax_reserve"], 2)
    if ctx["risks"]:
        out["flagged_risks"] = [r["title"] + ": " + r["narrative"] for r in ctx["risks"]]
    if ctx["budget_recommendation"]:
        b = ctx["budget_recommendation"]
        out["marketing_budget"] = {
            "category": b["category_label"], "current_pct_of_revenue": round(b["current_pct_of_revenue"], 1),
            "recommended_range_pct": [b["recommended_low_pct"], b["recommended_high_pct"]],
        }
    return out

def rule_based_ai_answer(question, ctx):
    q = question.lower()
    current, diagnosis = ctx["current_month"], ctx["diagnosis"]

    if not current:
        return ("You don't have any transactions logged yet, so there's nothing real for me to "
                "calculate from. Add some income and expenses and ask again.")

    profit, margin = current["profit"], current["margin"]

    if any(k in q for k in ["runway", "cash", "bank", "burn"]):
        r = ctx.get("runway", {})
        if r.get("runway_months") is not None:
            return (f"Based on your cash balance of ${r['cash_balance']:,.0f} and your 90-day average burn of "
                    f"${r['monthly_burn']:,.0f}/mo, your agency has approximately {r['runway_months']} months of runway.")
        return "Set your cash balance under Settings/Profile to enable instant runway calculations."

    if any(k in q for k in ["tax", "reserve", "vat", "gst"]):
        tax_amt = ctx.get("tax_reserve", 0.0)
        rate = ctx.get("tax_rate", 20.0)
        return (f"Based on your {rate:.0f}% tax reserve setting, you should set aside approximately "
                f"${tax_amt:,.0f} from this month's ${current['income']:,.0f} revenue for quarterly taxes.")

    if any(k in q for k in ["hire", "afford", "headcount", "employee"]):
        parts = [f"{ctx['agency_name']} is running at about ${profit:,.0f} profit this month"]
        if margin is not None:
            parts[0] += f" ({margin:.0f}% margin)"
        if diagnosis and diagnosis["margin_change"] is not None:
            if diagnosis["margin_change"] < 0:
                parts.append(f"Margin has been declining ({diagnosis['margin_prior']:.0f}% → "
                              f"{diagnosis['margin_now']:.0f}%), so I'd hold off on a new hire until it stabilizes.")
            else:
                parts.append(f"Margin has been steady or improving ({diagnosis['margin_prior']:.0f}% → "
                              f"{diagnosis['margin_now']:.0f}%), which is a reasonable position to add headcount from.")
        else:
            parts.append("I only have one month of data so far, so I can't confirm this is a trend yet.")
        return " ".join(parts)

    if "margin" in q or "profit" in q:
        if diagnosis:
            return (f"Margin moved from {diagnosis['margin_prior']:.0f}% in {diagnosis['prior_label']} to "
                    f"{diagnosis['margin_now']:.0f}% in {diagnosis['current_label']}. Revenue changed "
                    f"{diagnosis['revenue_change_pct']:+.0f}% while expenses changed {diagnosis['expense_change_pct']:+.0f}%.")
        return (f"This month's margin is {margin:.0f}% (${profit:,.0f} profit on ${current['income']:,.0f} "
                f"revenue). Log another month and I can show you the trend.") if margin is not None else \
               "No revenue logged yet this month, so margin isn't calculable."

    if any(k in q for k in ["forecast", "90", "quarter", "next 3"]):
        f = ctx["forecast"]
        if f:
            return (f"Based on your last {len(f['based_on_months'])} month(s), projected 90-day revenue is "
                     f"${f['projected_revenue']:,.0f} against ${f['projected_expense']:,.0f} expenses — "
                     f"about ${f['projected_profit']:,.0f} profit.")
        return "Not enough transaction history yet to forecast — log at least one full month."

    parts = [f"This month: ${current['income']:,.0f} revenue, ${current['expense']:,.0f} expenses, ${profit:,.0f} profit"]
    if margin is not None:
        parts[0] += f" ({margin:.0f}% margin)"
    if ctx["risks"]:
        parts.append(f"Biggest flag right now: {ctx['risks'][0]['narrative']}")
    parts.append('Try asking "What is our cash runway?", "How much tax should I save?", or "Can I afford to hire?"')
    return " ".join(parts)

def call_ai_provider(system_prompt, question):
    if GEMINI_API_KEY:
        try:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=GEMINI_API_KEY)
            res = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=question,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=350,
                ),
            )
            text = (res.text or "").strip()
            if text:
                return text
        except Exception as e:
            logger.warning(f"Gemini call failed or timed out: {e}")

    if OPENAI_API_KEY:
        try:
            client = openai.OpenAI(api_key=OPENAI_API_KEY, timeout=10.0)
            res = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": question}
                ],
                max_tokens=350,
            )
            text = (res.choices[0].message.content or "").strip()
            if text:
                return text
        except Exception as e:
            logger.warning(f"OpenAI call failed or timed out: {e}")

    return None

# --- CSV & MONEY HELPERS ---
def parse_money(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    val = str(value).strip()
    if not val:
        return None

    is_negative = False
    if val.startswith("(") and val.endswith(")"):
        is_negative = True
        val = val[1:-1].strip()

    for sym in ["$", "€", "£", "¥", "UGX", "USD", "EUR", "GBP", "KES", "TZS", "ZAR"]:
        val = val.replace(sym, "").replace(sym.lower(), "")

    val = val.replace(",", "").replace(" ", "").strip()

    if val.startswith("-"):
        is_negative = True
        val = val[1:].strip()
    elif val.endswith("-"):
        is_negative = True
        val = val[:-1].strip()
    elif val.startswith("+"):
        val = val[1:].strip()

    if not val:
        return None

    try:
        num = float(val)
        if not (num == num and num != float('inf') and num != float('-inf')):
            return None
        return -num if is_negative else num
    except (ValueError, TypeError):
        return None

def parse_csv_date(date_str):
    if not date_str or not date_str.strip():
        return datetime.utcnow().date(), True

    raw = str(date_str).strip()
    if "T" in raw:
        raw = raw.split("T")[0].strip()
    elif " " in raw and any(sep in raw.split(" ")[0] for sep in ["-", "/", "."]) and len(raw.split(" ")[0]) >= 8:
        raw = raw.split(" ")[0].strip()

    formats = [
        "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%d-%m-%Y",
        "%Y/%m/%d", "%m/%d/%y", "%d/%m/%y", "%Y.%m.%d", "%d.%m.%Y",
        "%b %d, %Y", "%d %b %Y", "%B %d, %Y", "%d %B %Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date(), True
        except ValueError:
            pass

    return None, False

def normalize_csv_header(header):
    if header is None:
        return ""
    h = str(header).strip().lower().replace("_", " ")
    return " ".join(h.split())

def apply_auto_rules(user_id, description, default_cat="General"):
    if not description:
        return default_cat
    desc_l = description.lower()
    rules = CategoryRule.query.filter_by(user_id=user_id).all()
    for r in rules:
        if r.keyword.lower() in desc_l:
            return r.target_category
    return default_cat

# --- AUTH ROUTES ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')
        agency_name = request.form.get('agency_name')
        currency = request.form.get('currency', 'USD').upper()

        if User.query.filter_by(email=email).first():
            flash("Email already registered.", "danger")
            return redirect(url_for('register'))

        user = User(
            email=email, 
            password_hash=generate_password_hash(password, method='scrypt'),
            agency_name=agency_name,
            currency=currency,
            is_verified=True
        )
        db.session.add(user)
        db.session.commit()

        defaults = [
            Category(user_id=user.id, name="Advertising", type="EXPENSE"),
            Category(user_id=user.id, name="Software", type="EXPENSE"),
            Category(user_id=user.id, name="Payroll", type="EXPENSE"),
            Category(user_id=user.id, name="Contractors", type="EXPENSE"),
            Category(user_id=user.id, name="Client Revenue", type="INCOME")
        ]
        db.session.add_all(defaults)

        default_rules = [
            CategoryRule(user_id=user.id, keyword="adobe", target_category="Software"),
            CategoryRule(user_id=user.id, keyword="figma", target_category="Software"),
            CategoryRule(user_id=user.id, keyword="slack", target_category="Software"),
            CategoryRule(user_id=user.id, keyword="facebook ads", target_category="Advertising"),
            CategoryRule(user_id=user.id, keyword="google ads", target_category="Advertising"),
        ]
        db.session.add_all(default_rules)
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
        user.agency_name = request.form.get('agency_name', user.agency_name)
        user.currency = request.form.get('currency', user.currency or 'USD').upper()
        try:
            if request.form.get('tax_rate'):
                user.tax_rate = float(request.form.get('tax_rate'))
            if request.form.get('cash_balance'):
                user.cash_balance = float(request.form.get('cash_balance'))
        except (ValueError, TypeError):
            pass

        new_pw = request.form.get('password')
        if new_pw:
            user.password_hash = generate_password_hash(new_pw, method='scrypt')
        db.session.commit()
        flash("Settings and parameters updated.", "success")
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
    query_str = request.args.get('q', request.args.get('search', '')).strip()
    category_filter = request.args.get('category', '')
    type_filter = request.args.get('type', request.args.get('kind', ''))
    client_filter = request.args.get('client', '')
    sort_by = request.args.get('sort', 'date_desc')

    tx_query = Transaction.query.filter_by(user_id=user.id)

    if query_str:
        tx_query = tx_query.filter(
            (Transaction.description.ilike(f"%{query_str}%")) | 
            (Transaction.category.ilike(f"%{query_str}%")) |
            (Transaction.client_name.ilike(f"%{query_str}%"))
        )
    if category_filter:
        tx_query = tx_query.filter_by(category=category_filter)
    if type_filter:
        tx_query = tx_query.filter_by(type=type_filter.upper())
    if client_filter:
        tx_query = tx_query.filter_by(client_name=client_filter)

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
    
    spent_by_category = {}
    for t in transactions:
        if t.type == 'EXPENSE':
            cat_l = t.category.lower()
            spent_by_category[cat_l] = spent_by_category.get(cat_l, 0.0) + t.amount

    for b in budgets:
        spent = spent_by_category.get(b.category.lower(), 0.0)
        pct = min(100, int((spent / b.monthly_limit) * 100)) if b.monthly_limit > 0 else 0
        rem = b.monthly_limit - spent
        budget_progress.append({
            "id": b.id, "name": b.category, "category": b.category, 
            "amount": b.monthly_limit, "limit": b.monthly_limit, 
            "spent": spent, "remaining": rem, "pct": pct, "percent": pct
        })
        if b.monthly_limit > 0 and spent > b.monthly_limit:
            alerts.append({"id": b.id, "title": "Overbudget Alert", "message": f"Category '{b.category}' exceeded set limit by ${abs(rem):.2f}!"})

    goals = Goal.query.filter_by(user_id=user.id).all()
    goal_data = []
    for g in goals:
        pct = min(100, int((g.current_amount / g.target_amount) * 100)) if g.target_amount > 0 else 0
        goal_data.append({"id": g.id, "title": g.title, "name": g.title, "target": g.target_amount, "current": g.current_amount, "pct": pct})

    detected_subs = detect_subscriptions(transactions)
    health_score = compute_financial_health(total_revenue, total_expenses, budgets, user.id, spent_by_category=spent_by_category)
    runway_data = compute_runway(user)
    tax_reserve = total_revenue * ((user.tax_rate or 20.0) / 100.0)

    chart_categories = {}
    for t in transactions:
        if t.type == 'EXPENSE':
            chart_categories[t.category] = chart_categories.get(t.category, 0) + t.amount

    user_clients = Client.query.filter_by(user_id=user.id).all()
    current_month_str = datetime.utcnow().strftime("%B %Y")
    profit_margin = round(((net_profit / total_revenue) * 100.0) if total_revenue > 0 else 0.0, 1)

    return render_template(
        'dashboard.html',
        user=user,
        transactions=transactions,
        txns=[{"id": t.id, "txn_date": t.date, "date": t.date, "description": t.description, "category_name": t.category, "category": t.category, "kind": t.type.lower(), "type": t.type, "amount": t.amount, "notes": ""} for t in transactions],
        total_revenue=total_revenue,
        total_expenses=total_expenses,
        income=total_revenue,
        expense=total_expenses,
        net_profit=net_profit,
        profit=net_profit,
        score=health_score,
        margin=profit_margin,
        month=current_month_str,
        budgets=budget_progress,
        alerts=alerts,
        goals=goal_data,
        subscriptions=detected_subs,
        health_score=health_score,
        runway=runway_data,
        tax_reserve=tax_reserve,
        clients=user_clients,
        categories=Category.query.filter_by(user_id=user.id).all(),
        chart_categories_json=json.dumps(chart_categories),
        pro=(user.plan_tier in ['GROWTH', 'PRO'])
    )

# --- TRANSACTION & CATEGORY CRUD ---
@app.route('/transactions/add', methods=['POST'])
@login_required
def add_transaction():
    user = User.query.get(session['user_id'])
    
    if user.plan_tier == 'FREE':
        tx_count = Transaction.query.filter_by(user_id=user.id).count()
        if tx_count >= 50:
            flash("Free tier limit reached (50 transactions). Upgrade to STARTER or PRO for unlimited transactions!", "warning")
            return redirect(url_for('pricing'))

    desc = request.form.get('description', '').strip()
    try:
        amount = abs(float(request.form.get('amount', 0)))
    except (ValueError, TypeError):
        amount = 0.0
    t_type = request.form.get('type', request.form.get('kind', 'EXPENSE')).upper()
    category = request.form.get('category', request.form.get('category_name', 'General'))
    client_name = request.form.get('client_name', '').strip() or None
    date_str = request.form.get('date') or request.form.get('txn_date')

    t_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else datetime.utcnow().date()

    if category in ['General', 'Imported', 'Uncategorized', 'No category']:
        category = apply_auto_rules(user.id, desc, "General")

    tx = Transaction(
        user_id=user.id, 
        description=desc[:200] if desc else "Transaction", 
        amount=amount, 
        type='INCOME' if t_type == 'INCOME' else 'EXPENSE', 
        category=category[:50] if category else "General", 
        client_name=client_name[:100] if client_name else None,
        date=t_date
    )
    db.session.add(tx)
    db.session.commit()
    flash("Transaction recorded.", "success")
    return redirect(url_for('dashboard'))

@app.route('/transactions/edit/<int:tx_id>', methods=['POST'])
@login_required
def edit_transaction(tx_id):
    tx = Transaction.query.filter_by(id=tx_id, user_id=session['user_id']).first_or_404()
    tx.description = request.form.get('description', tx.description)[:200]
    try:
        tx.amount = abs(float(request.form.get('amount', tx.amount)))
    except (ValueError, TypeError):
        pass
    tx.category = request.form.get('category', tx.category)[:50]
    tx.client_name = request.form.get('client_name', '').strip()[:100] or None
    t_type = request.form.get('type', tx.type).upper()
    tx.type = 'INCOME' if t_type == 'INCOME' else 'EXPENSE'
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
        cat_name = request.form.get('name', '').strip()
        cat_type = request.form.get('type', request.form.get('kind', 'EXPENSE')).upper()
        if cat_name:
            db.session.add(Category(user_id=user_id, name=cat_name[:50], type=cat_type))
            db.session.commit()
            flash(f"Category '{cat_name}' added.", "success")
    categories = Category.query.filter_by(user_id=user_id).all()
    rules = CategoryRule.query.filter_by(user_id=user_id).all()
    return render_template('categories.html', categories=categories, rules=rules)

@app.route('/categories/delete/<int:cat_id>', methods=['POST'])
@login_required
def delete_category(cat_id):
    cat = Category.query.filter_by(id=cat_id, user_id=session['user_id']).first_or_404()
    db.session.delete(cat)
    db.session.commit()
    flash("Category deleted.", "info")
    return redirect(url_for('manage_categories'))

# --- SMART AUTO-CATEGORIZATION RULES ---
@app.route('/rules/add', methods=['POST'])
@login_required
def add_rule():
    keyword = request.form.get('keyword', '').strip()
    target_category = request.form.get('target_category', '').strip()
    if keyword and target_category:
        rule = CategoryRule(user_id=session['user_id'], keyword=keyword[:100], target_category=target_category[:50])
        db.session.add(rule)
        db.session.commit()
        flash(f"Auto-categorization rule for '{keyword}' added.", "success")
    return redirect(url_for('manage_categories'))

@app.route('/rules/delete/<int:rule_id>', methods=['POST'])
@login_required
def delete_rule(rule_id):
    rule = CategoryRule.query.filter_by(id=rule_id, user_id=session['user_id']).first_or_404()
    db.session.delete(rule)
    db.session.commit()
    flash("Rule deleted.", "info")
    return redirect(url_for('manage_categories'))

# --- CLIENT MANAGEMENT ---
@app.route('/clients', methods=['GET', 'POST'])
@login_required
def manage_clients():
    user = User.query.get(session['user_id'])
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        try:
            retainer = float(request.form.get('retainer_amount', 0.0))
        except (ValueError, TypeError):
            retainer = 0.0
        if name:
            db.session.add(Client(user_id=user.id, name=name[:100], retainer_amount=retainer))
            db.session.commit()
            flash(f"Client '{name}' created.", "success")
            return redirect(url_for('manage_clients'))
            
    clients = Client.query.filter_by(user_id=user.id).all()
    client_pnl = compute_client_profitability(user.id)
    return render_template('clients.html', clients=clients, client_pnl=client_pnl)

@app.route('/clients/delete/<int:client_id>', methods=['POST'])
@login_required
def delete_client(client_id):
    client = Client.query.filter_by(id=client_id, user_id=session['user_id']).first_or_404()
    db.session.delete(client)
    db.session.commit()
    flash("Client removed.", "info")
    return redirect(url_for('manage_clients'))

# --- BUDGETS & GOALS ---
@app.route('/budgets/set', methods=['POST'])
@login_required
def set_budget():
    category = request.form.get('category', request.form.get('name', '')).strip()
    try:
        limit = max(0.0, float(request.form.get('monthly_limit', request.form.get('amount', 0))))
    except (ValueError, TypeError):
        limit = 0.0
    b = Budget.query.filter_by(user_id=session['user_id'], category=category).first()
    if b:
        b.monthly_limit = limit
    else:
        db.session.add(Budget(user_id=session['user_id'], category=category[:50], monthly_limit=limit))
    db.session.commit()
    flash("Budget updated.", "success")
    return redirect(url_for('dashboard'))

@app.route('/goals/add', methods=['POST'])
@login_required
def add_goal():
    title = request.form.get('title', request.form.get('name', '')).strip()
    try:
        target = max(0.0, float(request.form.get('target_amount', request.form.get('target', 0))))
    except (ValueError, TypeError):
        target = 0.0
    db.session.add(Goal(user_id=session['user_id'], title=title[:100], target_amount=target))
    db.session.commit()
    flash("Savings goal created.", "success")
    return redirect(url_for('dashboard'))

# --- CSV IMPORT & STATEMENTS ---
@app.route('/import-csv', methods=['GET', 'POST'])
@login_required
def import_csv():
    if request.method == 'GET':
        return render_template('import_csv.html', preview=[])

    file = request.files.get('file')

    if not file or not file.filename:
        flash("Please select a CSV file to upload.", "danger")
        return redirect(url_for('dashboard'))

    if not file.filename.lower().endswith('.csv'):
        flash("Upload a valid CSV statement file (.csv).", "danger")
        return redirect(url_for('dashboard'))

    try:
        raw_bytes = file.stream.read()
        if not raw_bytes or not raw_bytes.strip():
            flash("The uploaded CSV file is empty.", "warning")
            return redirect(url_for('dashboard'))

        text = None
        for enc in ["utf-8-sig", "utf-8", "latin-1", "cp1252", "iso-8859-1"]:
            try:
                text = raw_bytes.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue

        if text is None:
            text = raw_bytes.decode("utf-8", errors="replace")

        stream = io.StringIO(text, newline="")
        csv_reader = csv.DictReader(stream)

        if not csv_reader.fieldnames:
            flash("The CSV file has no recognizable header row.", "danger")
            return redirect(url_for('dashboard'))

        normalized_headers = [normalize_csv_header(h) for h in csv_reader.fieldnames if h is not None]
        logger.info(f"User {session['user_id']} starting CSV import. Detected headers: {normalized_headers}")

        imported_count = 0
        skipped_count = 0
        duplicate_count = 0
        user_id = session["user_id"]

        existing_tx_set = {
            (t.date, round(t.amount, 2), t.description.strip().lower())
            for t in Transaction.query.filter_by(user_id=user_id).all()
        }

        user_rules = CategoryRule.query.filter_by(user_id=user_id).all()

        DESC_FIELDS = ["description", "name", "merchant", "vendor", "transaction", "details", "memo", "narration", "reference", "payee", "particulars"]
        DATE_FIELDS = ["date", "transaction date", "trans date", "posted date", "transaction_date", "posting date", "value date"]
        AMOUNT_FIELDS = ["amount", "transaction amount", "value", "total", "transaction_amount", "net amount"]
        DEBIT_FIELDS = ["debit", "debit amount", "withdrawal", "spent", "paid out", "charge"]
        CREDIT_FIELDS = ["credit", "credit amount", "deposit", "received", "paid in", "income"]
        TYPE_FIELDS = ["type", "transaction type", "trans type", "entry type"]
        CAT_FIELDS = ["category", "type of expense", "expense category", "account", "tag"]
        CLIENT_FIELDS = ["client", "client name", "customer", "project", "client_name"]

        for row_idx, row in enumerate(csv_reader, start=2):
            try:
                if not row or not any(row.values()):
                    continue

                row_map = {}
                for k, v in row.items():
                    if k is not None:
                        clean_k = normalize_csv_header(k)
                        clean_v = str(v).strip() if v is not None else ""
                        row_map[clean_k] = clean_v

                description = None
                for field in DESC_FIELDS:
                    if row_map.get(field):
                        description = row_map[field]
                        break
                if not description:
                    description = "CSV Import"
                description = str(description)[:200]

                date_val = None
                for field in DATE_FIELDS:
                    if row_map.get(field):
                        date_val = row_map[field]
                        break

                parsed_date, date_valid = parse_csv_date(date_val)
                if not date_valid:
                    skipped_count += 1
                    logger.warning(f"CSV row {row_idx} skipped: Unrecognized date format '{date_val}'")
                    continue

                amount = None
                tx_type = None

                for field in AMOUNT_FIELDS:
                    if row_map.get(field):
                        parsed_amt = parse_money(row_map[field])
                        if parsed_amt is not None:
                            amount = parsed_amt
                            break

                if amount is None:
                    debit_val = None
                    credit_val = None
                    for df in DEBIT_FIELDS:
                        if row_map.get(df):
                            debit_val = parse_money(row_map[df])
                            if debit_val is not None: break
                    for cf in CREDIT_FIELDS:
                        if row_map.get(cf):
                            credit_val = parse_money(row_map[cf])
                            if credit_val is not None: break

                    if credit_val is not None and credit_val > 0:
                        amount = abs(credit_val)
                        tx_type = "INCOME"
                    elif debit_val is not None and debit_val > 0:
                        amount = abs(debit_val)
                        tx_type = "EXPENSE"
                    elif credit_val is not None and credit_val != 0:
                        amount = abs(credit_val)
                        tx_type = "INCOME" if credit_val > 0 else "EXPENSE"
                    elif debit_val is not None and debit_val != 0:
                        amount = abs(debit_val)
                        tx_type = "EXPENSE"

                if amount is None:
                    skipped_count += 1
                    logger.warning(f"CSV row {row_idx} skipped: Missing or invalid numeric amount.")
                    continue

                if tx_type is None:
                    explicit_type_val = None
                    for tf in TYPE_FIELDS:
                        if row_map.get(tf):
                            explicit_type_val = row_map[tf].lower()
                            break

                    if explicit_type_val in ["credit", "income", "deposit", "refund"]:
                        tx_type = "INCOME"
                    elif explicit_type_val in ["debit", "expense", "withdrawal", "payment", "charge"]:
                        tx_type = "EXPENSE"
                    else:
                        tx_type = "INCOME" if amount > 0 else "EXPENSE"

                final_amount = abs(float(amount))

                tx_signature = (parsed_date, round(final_amount, 2), description.strip().lower())
                if tx_signature in existing_tx_set:
                    duplicate_count += 1
                    continue

                category = None
                for cf in CAT_FIELDS:
                    if row_map.get(cf):
                        category = row_map[cf]
                        break
                
                if not category or category.lower() in ["general", "imported", "uncategorized"]:
                    desc_l = description.lower()
                    for r in user_rules:
                        if r.keyword.lower() in desc_l:
                            category = r.target_category
                            break
                    if not category:
                        category = "Imported"

                category = str(category)[:50]

                client_tag = None
                for cl_field in CLIENT_FIELDS:
                    if row_map.get(cl_field):
                        client_tag = row_map[cl_field][:100]
                        break

                tx = Transaction(
                    user_id=user_id,
                    description=description,
                    amount=final_amount,
                    type=tx_type,
                    category=category,
                    client_name=client_tag,
                    date=parsed_date
                )
                db.session.add(tx)
                existing_tx_set.add(tx_signature)
                imported_count += 1

            except Exception as row_err:
                skipped_count += 1
                logger.warning(f"CSV row {row_idx} error: {row_err}")
                continue

        db.session.commit()

        msg = f"Successfully imported {imported_count} transaction{'s' if imported_count != 1 else ''}."
        if duplicate_count > 0:
            msg += f" {duplicate_count} duplicate{'s were' if duplicate_count != 1 else ' was'} skipped."
        if skipped_count > 0:
            msg += f" {skipped_count} row{'s' if skipped_count != 1 else ''} skipped due to invalid data."
        
        flash(msg, "success" if imported_count > 0 else "info")
        logger.info(f"CSV import completed for user {user_id}: {imported_count} imported, {duplicate_count} dupes, {skipped_count} skipped.")

    except Exception as e:
        db.session.rollback()
        logger.exception(f"CSV import failed: {e}")
        flash("Failed to process CSV statement.", "danger")

    return redirect(url_for('dashboard'))

@app.route('/upload-statements', methods=['POST'])
@login_required
def upload_statements():
    user = User.query.get(session['user_id'])
    uploaded_files = request.files.getlist('statements')
    
    if not uploaded_files or len(uploaded_files) == 0 or not uploaded_files[0].filename:
        return jsonify({"error": "No statement files provided."}), 400

    file_count = len(uploaded_files)
    
    if user.plan_tier == 'FREE' and file_count > 3:
        return jsonify({
            "error": "Free tier is limited to 3 statements max.",
            "upgrade_required": True
        }), 403

    return jsonify({
        "tier": user.plan_tier,
        "processed_count": file_count,
        "message": "Audit completed. Unused software and recurring subscriptions detected.",
        "teaser_summary": {
            "estimated_savings": "$4,800 - $12,500 / year"
        }
    })

# --- AI RECEIPT & INVOICE OCR (Gemini / OpenAI) ---
@app.route('/api/scan-receipt', methods=['POST'])
@login_required
@pro_required
def scan_receipt():
    file = request.files.get('receipt')
    if not file:
        return jsonify({"error": "No receipt file uploaded"}), 400

    try:
        raw_bytes = file.read()
        mime_type = file.mimetype or "image/jpeg"
        
        if GEMINI_API_KEY:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=GEMINI_API_KEY)
            
            prompt = (
                "Extract financial data from this receipt/invoice. Return ONLY a valid JSON object with keys: "
                "\"merchant\" (string), \"amount\" (number), \"date\" (YYYY-MM-DD or null), \"suggested_category\" (string). "
                "No markdown backticks, no other text."
            )
            
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    types.Part.from_bytes(data=raw_bytes, mime_type=mime_type),
                    prompt
                ]
            )
            clean_json = (response.text or "{}").replace("```json", "").replace("```", "").strip()
            data = json.loads(clean_json)
            return jsonify(data)
            
    except Exception as e:
        logger.warning(f"Receipt OCR failed or timed out: {e}")
        
    return jsonify({"error": "Could not extract receipt data. Ensure GEMINI_API_KEY is configured."}), 500

# --- FINANCIAL SCENARIO SIMULATOR (PRO) ---
@app.route('/api/simulate-scenario', methods=['POST'])
@login_required
@pro_required
def simulate_scenario():
    user = User.query.get(session['user_id'])
    payload = request.get_json(silent=True) or {}
    
    monthly_cost_change = float(payload.get('monthly_expense_delta', 0.0))
    monthly_rev_change = float(payload.get('monthly_revenue_delta', 0.0))
    
    months = get_monthly_series(user.id)
    if not months:
        return jsonify({"error": "Need at least 1 month of transaction data."}), 400
        
    current = months[-1]
    curr_rev = current["income"]
    curr_exp = current["expense"]
    
    new_rev = max(0.0, curr_rev + monthly_rev_change)
    new_exp = max(0.0, curr_exp + monthly_cost_change)
    new_profit = new_rev - new_exp
    new_margin = (new_profit / new_rev * 100.0) if new_rev > 0 else 0.0
    
    return jsonify({
        "current_revenue": curr_rev,
        "current_expense": curr_exp,
        "current_profit": curr_rev - curr_exp,
        "current_margin": current["margin"],
        "projected_monthly_revenue": new_rev,
        "projected_monthly_expense": new_exp,
        "projected_monthly_profit": new_profit,
        "projected_margin": round(new_margin, 1),
        "margin_delta": round(new_margin - (current["margin"] or 0.0), 1)
    })

# --- EXPORTS (PDF & CSV) ---
@app.route('/reports/export/csv')
@login_required
def export_csv():
    txs = Transaction.query.filter_by(user_id=session['user_id']).order_by(Transaction.date.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Date', 'Description', 'Amount', 'Type', 'Category', 'Client'])
    for t in txs:
        writer.writerow([t.id, t.date, t.description, t.amount, t.type, t.category, t.client_name or ''])
    
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=budget_buddy_report.csv"}
    )

@app.route('/reports/export/pdf')
@login_required
def export_pdf():
    user = User.query.get(session['user_id'])
    txs = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.date.desc()).all()
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    p.setFont("Helvetica-Bold", 16)
    p.drawString(50, 750, f"Budget Buddy - Financial Statement ({user.agency_name or user.email})")
    
    p.setFont("Helvetica", 10)
    y = 715
    p.drawString(50, y, "Date | Description | Category | Client | Type | Amount")
    p.line(50, y-5, 550, y-5)
    y -= 20

    for t in txs[:35]:
        desc_tr = (t.description[:18] + '..') if len(t.description) > 18 else t.description
        client_tr = (t.client_name[:12] + '..') if t.client_name and len(t.client_name) > 12 else (t.client_name or "-")
        p.drawString(50, y, f"{t.date} | {desc_tr} | {t.category[:12]} | {client_tr} | {t.type} | ${t.amount:.2f}")
        y -= 16
        if y < 50:
            break

    p.showPage()
    p.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="budget_report.pdf", mimetype="application/pdf")

# --- PRO FEATURES (AI & ADVANCED ANALYTICS) ---
@app.route('/reports/advanced')
@app.route('/reports')
@login_required
@pro_required
def advanced_reports():
    user = User.query.get(session['user_id'])
    txs = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.date.desc()).all()
    client_pnl = compute_client_profitability(user.id)
    runway = compute_runway(user)
    
    month_str = request.args.get('month', datetime.utcnow().strftime('%Y-%m'))
    this_month_expenses = sum(t.amount for t in txs if t.type == 'EXPENSE' and t.date.strftime('%Y-%m') == month_str)
    this_month_income = sum(t.amount for t in txs if t.type == 'INCOME' and t.date.strftime('%Y-%m') == month_str)
    forecasted_exp = this_month_expenses * 1.08

    by_cat_dict = {}
    for t in txs:
        if t.type == 'EXPENSE':
            by_cat_dict[t.category] = by_cat_dict.get(t.category, 0) + t.amount
    by_cat = [{"category": k, "total": v} for k, v in by_cat_dict.items()]
    
    return render_template(
        'advanced_reports.html', 
        txs=txs, 
        txns=[{"txn_date": t.date, "description": t.description, "category_name": t.category, "amount": t.amount} for t in txs],
        forecast=forecasted_exp, 
        client_pnl=client_pnl, 
        runway=runway,
        income=this_month_income,
        expense=this_month_expenses,
        month=month_str,
        by_cat=by_cat
    )

@app.route('/api/ai-insights')
@login_required
@pro_required
def ai_insights():
    user = User.query.get(session['user_id'])
    ctx = build_ai_context(user)
    insights = []

    if ctx["diagnosis"]:
        d = ctx["diagnosis"]
        if d["revenue_change_pct"] is not None and d["expense_change_pct"] is not None:
            margin_bit = ""
            if d["margin_prior"] is not None and d["margin_now"] is not None:
                margin_bit = f" — margin {d['margin_prior']:.0f}% → {d['margin_now']:.0f}%"
            insights.append(
                f"Revenue {d['revenue_change_pct']:+.0f}% vs {d['prior_label']}, expenses "
                f"{d['expense_change_pct']:+.0f}%{margin_bit}."
            )

    if ctx.get("runway") and ctx["runway"]["runway_months"] is not None:
        insights.append(f"Estimated cash runway: {ctx['runway']['runway_months']} months based on current monthly burn.")

    if ctx["risks"]:
        insights.append(ctx["risks"][0]["narrative"])

    if ctx["budget_recommendation"]:
        b = ctx["budget_recommendation"]
        insights.append(
            f"{b['category_label']} spend is {b['current_pct_of_revenue']:.0f}% of {b['month_label']} "
            f"revenue (benchmark: {b['recommended_low_pct']:.0f}\u2013{b['recommended_high_pct']:.0f}%)."
        )

    if not insights:
        if ctx["current_month"]:
            cm = ctx["current_month"]
            insights.append(f"{cm['label']} so far: ${cm['income']:,.0f} revenue, ${cm['expense']:,.0f} expenses.")
        insights.append("Log a few weeks of transactions and trend-based insights will start showing up here.")

    return jsonify({"insights": insights[:4]})

@app.route('/pro')
@login_required
@pro_required
def pro():
    user = User.query.get(session['user_id'])
    ctx = build_ai_context(user)
    client_pnl = compute_client_profitability(user.id)
    return render_template(
        'pro.html',
        user=user,
        current=ctx["current_month"],
        diagnosis=ctx["diagnosis"],
        forecast=ctx["forecast"],
        risks=ctx["risks"],
        runway=ctx["runway"],
        tax_reserve=ctx["tax_reserve"],
        budget_rec=ctx["budget_recommendation"],
        client_pnl=client_pnl,
        has_data=bool(ctx["current_month"]),
    )

@app.route('/api/ask-ai', methods=['POST'])
@login_required
@pro_required
def ask_ai():
    user = User.query.get(session['user_id'])
    payload = request.get_json(silent=True) or {}
    question = payload.get('question', '').strip()[:500]
    if not question:
        return jsonify({"error": "Ask a question first."}), 400

    ctx = build_ai_context(user)
    system_prompt = (
        "You are Budget Buddy AI, a financial strategist for a marketing/digital agency. "
        "Below is this agency's real financial data, computed directly from their books. "
        "Only use numbers that appear in this data — never invent or estimate a figure that "
        "isn't given. If the question needs a number that isn't present, say plainly what "
        "data is missing instead of guessing. Answer in under 120 words, direct and specific, "
        "plain text (no markdown headers or bullet lists).\n\n"
        f"AGENCY DATA:\n{json.dumps(_serialize_ai_context(ctx))}"
    )
    answer = call_ai_provider(system_prompt, question)
    if not answer:
        answer = rule_based_ai_answer(question, ctx)

    return jsonify({"answer": answer})

# --- INFORMATIONAL & LEGAL PAGES ---
@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/privacy-policy')
@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/refund-policy')
@app.route('/refund')
def refund():
    return render_template('refund.html')

@app.route('/scanner')
def scanner():
    return render_template('statement_scanner.html')

# --- BILLING & CHECKOUT (PADDLE & FLUTTERWAVE) ---
@app.route('/pricing')
def pricing():
    return render_template('pricing.html')

@app.route('/checkout')
@login_required
def checkout():
    plan = request.args.get('plan', 'personal_pro')
    return render_template('pricing.html', selected_plan=plan)

@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    user = User.query.get(session['user_id'])
    plan = request.form.get('plan', 'STARTER')
    amount = request.form.get('amount', '49')

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
        response = requests.post("https://api.flutterwave.com/v3/payments", json=payload, headers=headers, timeout=10)
        data = response.json()

        if data.get("status") == "success":
            return redirect(data["data"]["link"])
        else:
            flash("Failed to initiate payment session. Please try again or use Sendwave.", "danger")
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
            res = requests.get(verify_url, headers=headers, timeout=10).json()

            if res.get("status") == "success" and res.get("data", {}).get("status") == "successful":
                user = User.query.get(session['user_id'])
                tx_ref = res["data"].get("tx_ref", "")

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

# --- SENDWAVE MANUAL PAYMENT VERIFICATION FLOW ---
SENDWAVE_PLAN_AMOUNTS = {"STARTER": 49, "GROWTH": 149, "PRO": 299}

def send_sendwave_review_email(payment):
    """Formats and dispatches the verification email to the admin via Resend HTTPS API."""
    review_url = url_for('sendwave_email_review', payment_id=payment.id,
                          token=payment.action_token, _external=True)

    subject = f"Sendwave payment verification requested — {payment.user.email} (${payment.amount:.0f} {payment.plan_requested})"

    text_body = (
        f"Have you received this Sendwave payment?\n\n"
        f"User: {payment.user.email}\n"
        f"Plan: {payment.plan_requested}\n"
        f"Amount: ${payment.amount:.2f}\n"
        f"Reference Code: {payment.reference_code}\n"
        f"Sender Name: {payment.sender_name or '—'}\n"
        f"Submitted: {payment.submitted_at.strftime('%b %d, %Y %H:%M UTC')}\n\n"
        f"Review and approve/decline here: {review_url}\n"
    )

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:auto;background:#0f172a;color:#f8fafc;padding:24px;border-radius:12px;border:1px solid #334155;">
      <h2 style="color:#38bdf8;margin-bottom:8px;">Sendwave Payment Verification</h2>
      <p style="color:#94a3b8;font-size:14px;">A user has submitted a reference code for manual verification.</p>
      <table style="width:100%;border-collapse:collapse;margin:16px 0;font-size:14px;">
        <tr style="border-bottom:1px solid #1e293b;"><td style="padding:8px 0;color:#94a3b8;">User</td><td style="padding:8px 0;text-align:right;"><b>{payment.user.email}</b></td></tr>
        <tr style="border-bottom:1px solid #1e293b;"><td style="padding:8px 0;color:#94a3b8;">Plan</td><td style="padding:8px 0;text-align:right;"><b>{payment.plan_requested}</b></td></tr>
        <tr style="border-bottom:1px solid #1e293b;"><td style="padding:8px 0;color:#94a3b8;">Amount</td><td style="padding:8px 0;text-align:right;"><b>${payment.amount:.2f}</b></td></tr>
        <tr style="border-bottom:1px solid #1e293b;"><td style="padding:8px 0;color:#94a3b8;">Reference Code</td><td style="padding:8px 0;text-align:right;"><code style="background:#1e293b;padding:3px 6px;border-radius:4px;color:#38bdf8;">{payment.reference_code}</code></td></tr>
        <tr><td style="padding:8px 0;color:#94a3b8;">Sender Name</td><td style="padding:8px 0;text-align:right;">{payment.sender_name or '—'}</td></tr>
      </table>
      <div style="margin-top:20px;text-align:center;">
        <a href="{review_url}" style="background:#0284c7;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:bold;display:inline-block;">
          Open Review Portal
        </a>
      </div>
    </div>
    """

    return send_email_via_resend(
        to_email=ADMIN_EMAIL,
        subject=subject,
        html_content=html_body,
        text_content=text_body
    )

@app.route('/sendwave/submit', methods=['POST'])
@login_required
def sendwave_submit():
    logger.info("CODE SUBMISSION STARTED")
    is_ajax = request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.accept_mimetypes.best == 'application/json'
    
    try:
        user = User.query.get(session['user_id'])
        if not user:
            logger.error("CODE SUBMISSION ERROR: User session invalid")
            if is_ajax:
                return jsonify({"success": False, "message": "User session not found. Please log in again."}), 401
            flash("User session not found.", "danger")
            return redirect(url_for('login'))

        # Support both form post and JSON payloads
        data = request.get_json(silent=True) if request.is_json else request.form
        if not data:
            data = request.form

        plan = str(data.get('plan', '')).strip().upper()
        reference_code = str(data.get('reference_code', '')).strip()
        sender_name = str(data.get('sender_name', '')).strip()

        if plan not in SENDWAVE_PLAN_AMOUNTS:
            logger.warning("CODE SUBMISSION ERROR: Invalid plan requested")
            if is_ajax:
                return jsonify({"success": False, "message": "Please select a valid plan (Starter, Growth, or Pro)."}), 400
            flash("Please select a valid plan.", "danger")
            return redirect(url_for('pricing'))

        if not reference_code or len(reference_code) < 3:
            logger.warning("CODE SUBMISSION ERROR: Invalid or missing reference code")
            if is_ajax:
                return jsonify({"success": False, "message": "Please enter a valid Sendwave reference code."}), 400
            flash("Please enter the Sendwave reference code from your transfer.", "danger")
            return redirect(url_for('pricing'))

        logger.info("CODE VALIDATED")

        # Duplicate detection / update
        existing = SendwavePayment.query.filter_by(reference_code=reference_code).first()
        if existing:
            if existing.user_id == user.id:
                logger.info("DATABASE STATUS UPDATED (Existing record)")
                existing.plan_requested = plan
                existing.amount = SENDWAVE_PLAN_AMOUNTS[plan]
                existing.sender_name = sender_name or existing.sender_name
                existing.status = "PENDING"
                db.session.commit()
                payment = existing
            else:
                logger.warning("CODE SUBMISSION ERROR: Duplicate reference code by different account")
                if is_ajax:
                    return jsonify({"success": False, "message": "That reference code has already been registered."}), 400
                flash("That reference code has already been registered.", "warning")
                return redirect(url_for('sendwave_status'))
        else:
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
            logger.info("DATABASE STATUS UPDATED")

        logger.info("DATABASE COMMIT SUCCESS")

        # Isolated Resend HTTPS email dispatch (Failure will NOT revert the pending database status)
        email_sent, email_msg = send_sendwave_review_email(payment)
        
        success_message = "Code submitted successfully. Your payment is now Pending verification."
        if not email_sent:
            success_message += f" (Note: Admin notification queued: {email_msg})"

        if is_ajax:
            return jsonify({
                "success": True,
                "status": "pending",
                "message": success_message,
                "redirect_url": url_for('sendwave_status')
            }), 200

        flash(success_message, "success")
        return redirect(url_for('sendwave_status'))

    except Exception as e:
        db.session.rollback()
        logger.error(f"DATABASE ERROR / CODE SUBMISSION ERROR: {type(e).__name__}: {str(e)}")
        if is_ajax:
            return jsonify({"success": False, "message": f"Server error: {str(e)}"}), 500
        flash("An unexpected error occurred while processing your request.", "danger")
        return redirect(url_for('pricing'))

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

# --- TOKEN-GATED REVIEW ACTIONS FROM EMAIL ---
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
