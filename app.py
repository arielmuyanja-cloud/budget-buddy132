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
db_url = os.environ.get(
    'DATABASE_URL',
    f"sqlite:///{os.path.join(os.path.abspath(os.path.dirname(__file__)), 'budget.db')}"
)

if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Keep Render PostgreSQL connections healthy.
if db_url.startswith("postgresql://"):
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "pool_timeout": 30,
        "pool_size": 5,
        "max_overflow": 10,
        "connect_args": {
            "sslmode": "require"
        }
    }

db = SQLAlchemy(app)

# External API Keys (Flutterwave, OpenAI, Gemini)
FLW_PUBLIC_KEY = os.environ.get("FLW_PUBLIC_KEY", "FLWPUBK_TEST-xxxxxxxx")
FLW_SECRET_KEY = os.environ.get("FLW_SECRET_KEY", "FLWSECK_TEST-xxxxxxxx")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

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
    currency = db.Column(db.String(10), default="USD")
    tax_rate = db.Column(db.Float, default=20.0)  # Default 20% tax reserve suggestion
    cash_balance = db.Column(db.Float, default=0.0)  # For runway calculation
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
    client_name = db.Column(db.String(100), nullable=True)  # Client tagging for agency profitability

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(50), nullable=False)
    type = db.Column(db.String(10), nullable=False, default="EXPENSE")

class CategoryRule(db.Model):
    """Auto-categorization rule: if keyword in description, map to target category"""
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
    plan_requested = db.Column(db.String(20), nullable=False)   # STARTER, GROWTH, PRO
    amount = db.Column(db.Float, nullable=False)
    reference_code = db.Column(db.String(100), nullable=False)  # Sendwave transaction/reference code
    sender_name = db.Column(db.String(120), nullable=True)      # name payment was sent under, if different
    status = db.Column(db.String(20), default="PENDING")        # PENDING, APPROVED, REJECTED
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    admin_notes = db.Column(db.String(300), nullable=True)
    action_token = db.Column(db.String(64), unique=True, nullable=True)

    user = db.relationship('User', backref=db.backref('sendwave_payments', cascade="all, delete-orphan"))

# Automatic Schema Initialization & Safe Migrations for PostgreSQL and SQLite
with app.app_context():
    db.create_all()
    try:
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
                    db.session.execute(text(f"ALTER TABLE \"{table}\" ADD COLUMN IF NOT EXISTS {col} {col_type}"))
                else:
                    db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                db.session.commit()
            except Exception:
                db.session.rollback()
    except Exception as e:
        db.session.rollback()
        app.logger.warning(f"Startup migrations status: {e}")

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

@app.context_processor
def inject_current_user():
    user = User.query.get(session['user_id']) if 'user_id' in session else None
    return {'current_user': user}

# --- ANALYTICS & RECURRING ENGINE ---
KNOWN_SUBSCRIPTIONS = ["adobe", "chatgpt", "canva", "google workspace", "slack", "zoom", "hubspot", "semrush", "github", "render", "meta", "linkedin", "figma", "notion", "aws", "openai"]

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

def compute_client_profitability(user_id):
    """Computes revenue, expense, and net margin on a per-client level."""
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
    """Calculates cash runway in months based on cash balance and 90-day average monthly expense."""
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
MARKETING_KEYWORDS = ["advertising", "marketing", "ads", "ppc", "paid media"]

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
    
    # Monthly tax reserve calculation
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
            app.logger.warning(f"Gemini call failed, trying next provider: {e}")

    if OPENAI_API_KEY:
        try:
            client = openai.OpenAI(api_key=OPENAI_API_KEY)
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
            app.logger.warning(f"OpenAI call failed: {e}")

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
    """Matches description against user's CategoryRules for automatic categorization."""
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

        # Seed Default Categories
        defaults = [
            Category(user_id=user.id, name="Advertising", type="EXPENSE"),
            Category(user_id=user.id, name="Software", type="EXPENSE"),
            Category(user_id=user.id, name="Payroll", type="EXPENSE"),
            Category(user_id=user.id, name="Contractors", type="EXPENSE"),
            Category(user_id=user.id, name="Client Revenue", type="INCOME")
        ]
        db.session.add_all(defaults)

        # Seed Default Smart Categorization Rules
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
            user.tax_rate = float(request.form.get('tax_rate', user.tax_rate or 20.0))
            user.cash_balance = float(request.form.get('cash_balance', user.cash_balance or 0.0))
        except (ValueError, TypeError):
            pass

        new_pw = request.form.get('password')
        if new_pw:
            user.password_hash = generate_password_hash(new_pw, method='scrypt')
        db.session.commit()
        flash("Settings and financial parameters updated.", "success")
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
        tx_query = tx_query.filter_by(type=type_filter)
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

    # Analytics Engine
    detected_subs = detect_subscriptions(transactions)
    health_score = compute_financial_health(total_revenue, total_expenses, budgets, user.id)
    runway_data = compute_runway(user)
    tax_reserve = total_revenue * ((user.tax_rate or 20.0) / 100.0)

    # Monthly Trends Data for Charts
    chart_categories = {}
    for t in transactions:
        if t.type == 'EXPENSE':
            chart_categories[t.category] = chart_categories.get(t.category, 0) + t.amount

    user_clients = Client.query.filter_by(user_id=user.id).all()

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
        runway=runway_data,
        tax_reserve=tax_reserve,
        clients=user_clients,
        categories=Category.query.filter_by(user_id=user.id).all(),
        chart_categories_json=json.dumps(chart_categories)
    )

# --- TRANSACTION & CATEGORY CRUD ---
@app.route('/transactions/add', methods=['POST'])
@login_required
def add_transaction():
    user = User.query.get(session['user_id'])
    
    # Feature gating: Limit free accounts to 50 transactions
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
    t_type = request.form.get('type', 'EXPENSE')
    category = request.form.get('category', 'General')
    client_name = request.form.get('client_name', '').strip() or None
    date_str = request.form.get('date')

    t_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else datetime.utcnow().date()

    # Apply auto rules if category is default
    if category in ['General', 'Imported']:
        category = apply_auto_rules(user.id, desc, category)

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
    t_type = request.form.get('type', tx.type)
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
        cat_type = request.form.get('type', 'EXPENSE')
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
    category = request.form.get('category', '').strip()
    try:
        limit = max(0.0, float(request.form.get('monthly_limit', 0)))
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
    title = request.form.get('title', '').strip()
    try:
        target = max(0.0, float(request.form.get('target_amount', 0)))
    except (ValueError, TypeError):
        target = 0.0
    db.session.add(Goal(user_id=session['user_id'], title=title[:100], target_amount=target))
    db.session.commit()
    flash("Savings goal created.", "success")
    return redirect(url_for('dashboard'))

# --- CSV IMPORT & EXPORT ---
@app.route('/import-csv', methods=['POST'])
@login_required
def import_csv():
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
        app.logger.info(f"User {session['user_id']} starting CSV import. Detected headers: {normalized_headers}")

        imported_count = 0
        skipped_count = 0
        duplicate_count = 0
        user_id = session["user_id"]

        # Cache existing recent transactions for duplicate detection
        existing_tx_set = {
            (t.date, round(t.amount, 2), t.description.strip().lower())
            for t in Transaction.query.filter_by(user_id=user_id).all()
        }

        # Cache rules for fast in-loop matching
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

                # 1. Description
                description = None
                for field in DESC_FIELDS:
                    if row_map.get(field):
                        description = row_map[field]
                        break
                if not description:
                    description = "CSV Import"
                description = str(description)[:200]

                # 2. Date
                date_val = None
                for field in DATE_FIELDS:
                    if row_map.get(field):
                        date_val = row_map[field]
                        break

                parsed_date, date_valid = parse_csv_date(date_val)
                if not date_valid:
                    skipped_count += 1
                    app.logger.warning(f"CSV row {row_idx} skipped: Unrecognized date format '{date_val}'")
                    continue

                # 3. Amount & Type
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
                    app.logger.warning(f"CSV row {row_idx} skipped: Missing or invalid numeric amount.")
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

                # Duplicate Detection (skip same date, amount, description)
                tx_signature = (parsed_date, round(final_amount, 2), description.strip().lower())
                if tx_signature in existing_tx_set:
                    duplicate_count += 1
                    continue

                # 4. Category & Smart Rule Match
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

                # 5. Client tag if present in CSV
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
                app.logger.warning(f"CSV row {row_idx} error: {row_err}")
                continue

        db.session.commit()

        msg = f"Successfully imported {imported_count} transaction{'s' if imported_count != 1 else ''}."
        if duplicate_count > 0:
            msg += f" {duplicate_count} duplicate{'s were' if duplicate_count != 1 else ' was'} skipped."
        if skipped_count > 0:
            msg += f" {skipped_count} row{'s' if skipped_count != 1 else ''} skipped due to invalid data."
        
        flash(msg, "success" if imported_count > 0 else "info")
        app.logger.info(f"CSV import completed for user {user_id}: {imported_count} imported, {duplicate_count} dupes, {skipped_count} skipped.")

    except Exception as e:
        db.session.rollback()
        app.logger.exception(f"CSV import failed: {e}")
        flash("Failed to process CSV statement.", "danger")

    return redirect(url_for('dashboard'))

# --- AI RECEIPT & INVOICE OCR (Gemini / OpenAI) ---
@app.route('/api/scan-receipt', methods=['POST'])
@login_required
@pro_required
def scan_receipt():
    """Extracts merchant, amount, date, and category recommendation from receipt image."""
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
        app.logger.warning(f"Receipt OCR failed: {e}")
        
    return jsonify({"error": "Could not extract receipt data. Ensure GEMINI_API_KEY is configured."}), 500

# --- FINANCIAL SCENARIO SIMULATOR (PRO) ---
@app.route('/api/simulate-scenario', methods=['POST'])
@login_required
@pro_required
def simulate_scenario():
    """Simulates hiring, ad spend increase, or client loss on future 90-day margin."""
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
@login_required
@pro_required
def advanced_reports():
    user = User.query.get(session['user_id'])
    txs = Transaction.query.filter_by(user_id=user.id).all()
    client_pnl = compute_client_profitability(user.id)
    runway = compute_runway(user)
    
    this_month_expenses = sum(t.amount for t in txs if t.type == 'EXPENSE' and t.date.month == datetime.utcnow().month)
    forecasted_exp = this_month_expenses * 1.08
    
    return render_template('advanced_reports.html', txs=txs, forecast=forecasted_exp, client_pnl=client_pnl, runway=runway)

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

# --- FLUTTERWAVE BILLING INTEGRATION ---
@app.route('/pricing')
def pricing():
    return render_template('pricing.html')

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
SENDWAVE_PLAN_AMOUNTS = {"STARTER": 49, "GROWTH": 149, "PRO": 299}

def send_sendwave_review_email(payment):
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

# --- ACCEPT/DECLINE STRAIGHT FROM THE REVIEW EMAIL (token-gated) ---
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
