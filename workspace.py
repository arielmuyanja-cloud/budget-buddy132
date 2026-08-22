import re
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, session
from sqlalchemy import inspect, text
from app import db, Transaction, login_required

workspace_bp = Blueprint("workspace", __name__)
VENDOR_DIRECTORY = {
    "asana": ("Asana", "Project Management"), "clickup": ("ClickUp", "Project Management"), "monday": ("monday.com", "Project Management"), "trello": ("Trello", "Project Management"),
    "notion": ("Notion", "Knowledge Management"), "slack": ("Slack", "Team Communication"), "zoom": ("Zoom", "Video Communication"), "loom": ("Loom", "Video Communication"),
    "canva": ("Canva", "Design"), "figma": ("Figma", "Design"), "adobe": ("Adobe", "Design"), "hubspot": ("HubSpot", "CRM / Marketing Automation"), "mailchimp": ("Mailchimp", "Email Marketing"),
    "semrush": ("Semrush", "SEO"), "ahrefs": ("Ahrefs", "SEO"), "google workspace": ("Google Workspace", "Productivity Suite"), "microsoft 365": ("Microsoft 365", "Productivity Suite"),
    "github": ("GitHub", "Developer Tools"), "vercel": ("Vercel", "Hosting / Deployment"), "render": ("Render", "Hosting / Deployment"), "aws": ("AWS", "Cloud Infrastructure"),
    "openai": ("OpenAI", "AI / API"), "chatgpt": ("ChatGPT", "AI / API"),
}

class WorkspaceDecision(db.Model):
    __tablename__ = "workspace_decision"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    tool_name = db.Column(db.String(200), nullable=False)
    action = db.Column(db.String(20), nullable=False, default="INVESTIGATE")
    savings_monthly = db.Column(db.Float, nullable=False, default=0.0)
    risk_rating = db.Column(db.String(20), nullable=False, default="UNKNOWN")
    status = db.Column(db.String(20), nullable=False, default="Open")
    active = db.Column(db.Boolean, nullable=False, default=True)
    q1_client_workflow = db.Column(db.Boolean, nullable=True)
    q2_active_use = db.Column(db.Boolean, nullable=True)
    q3_important_files = db.Column(db.Boolean, nullable=True)
    q4_critical_integrations = db.Column(db.Boolean, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def clean_merchant(text_value):
    value = (text_value or "").lower()
    value = re.sub(r"[^a-z0-9 ]+", " ", value)
    value = re.sub(r"\b(inc|ltd|llc|com|payment|purchase|card|pos|online)\b", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def classify_merchant(description):
    cleaned = clean_merchant(description)
    for key, (name, taxonomy) in VENDOR_DIRECTORY.items():
        if key in cleaned or key in (description or "").lower():
            return name, taxonomy
    return None, None


def evidence_score(months, vendor, taxonomy, overlap=False, stable_amount=False):
    score = 25 if months >= 3 else -20
    if vendor: score += 20
    if taxonomy: score += 20
    if overlap: score += 15
    if stable_amount: score += 10
    return max(0, min(score, 90))


def evidence_tier(score):
    return "Strong" if score >= 70 else "Medium" if score >= 40 else "Weak"


def recurring_candidates(user_id):
    transactions = Transaction.query.filter_by(user_id=user_id, type="EXPENSE").order_by(Transaction.date.asc()).all()
    groups = {}
    for tx in transactions:
        key = clean_merchant(tx.description)
        if key: groups.setdefault(key, {"raw": tx.description, "transactions": []})["transactions"].append(tx)
    candidates = []
    for key, group in groups.items():
        txs = group["transactions"]
        month_keys = sorted({(t.date.year, t.date.month) for t in txs})
        if len(month_keys) < 2: continue
        amounts = [float(t.amount or 0) for t in txs]
        vendor, taxonomy = classify_merchant(group["raw"])
        stable = bool(amounts) and max(amounts) - min(amounts) < 0.01
        consecutive = all((month_keys[i][0] * 12 + month_keys[i][1]) - (month_keys[i-1][0] * 12 + month_keys[i-1][1]) == 1 for i in range(1, len(month_keys)))
        score = evidence_score(len(month_keys), vendor, taxonomy, False, stable)
        candidates.append({"merchant_key":key,"raw_merchant":group["raw"],"vendor":vendor or group["raw"],"taxonomy":taxonomy,"amount":round(sum(amounts)/len(amounts),2),"appearances":len(txs),"months":len(month_keys),"consecutive":consecutive,"points":score,"evidence_strength":evidence_tier(score),"is_known":bool(vendor)})
    return candidates


def build_discovery(user_id):
    candidates = recurring_candidates(user_id)
    data_months = len({(t.date.year, t.date.month) for t in Transaction.query.filter_by(user_id=user_id).all()})
    known = [c for c in candidates if c["is_known"]]
    pairs = []
    for i, a in enumerate(known):
        for b in known[i + 1:]:
            if a["taxonomy"] and a["taxonomy"] == b["taxonomy"]:
                score = evidence_score(data_months, a["vendor"], a["taxonomy"], True, a["amount"] == b["amount"])
                pairs.append((max(a["amount"], b["amount"]), a, b, score))
    if pairs:
        _, a, b, score = max(pairs, key=lambda x: x[0])
        return {"mode":"overlap","title":f"{a['vendor']} + {b['vendor']}","a":a,"b":b,"taxonomy":a["taxonomy"],"evidence_strength":evidence_tier(score),"score":score,"months":data_months,"message":f"Potential overlap: both tools are classified as {a['taxonomy']}. Transaction data cannot establish redundant functionality or active use."}
    unknown = [c for c in candidates if not c["is_known"]]
    if unknown:
        c = max(unknown, key=lambda x:x["amount"])
        return {"mode":"unknown","candidate":c,"evidence_strength":c["evidence_strength"],"score":c["points"],"months":data_months,"message":"No defensible overlap was found. This is the largest unidentified recurring charge requiring review."}
    return {"mode":"none","months":data_months,"message":"No defensible recurring software opportunity was found yet. Import at least two months of history."}


def serialize_decision(d):
    return {"id":d.id,"tool_name":d.tool_name,"action":d.action,"savings_monthly":round(d.savings_monthly or 0,2),"risk_rating":d.risk_rating,"status":d.status,"active":d.active,"answers":{"q1":d.q1_client_workflow,"q2":d.q2_active_use,"q3":d.q3_important_files,"q4":d.q4_critical_integrations}}


def risk_from_answers(answers):
    if answers.get("q1") or answers.get("q4"): return "HIGH", "Rejected"
    if answers.get("q2") or answers.get("q3"): return "MEDIUM", "Investigating"
    return "LOW", "Verified"


def dashboard_totals(user_id):
    rows = WorkspaceDecision.query.filter_by(user_id=user_id).all()
    low = sum(d.savings_monthly for d in rows if d.active and d.risk_rating == "LOW" and d.status == "Verified")
    review = sum(d.savings_monthly for d in rows if d.active and d.risk_rating in ("UNKNOWN", "MEDIUM"))
    rejected = sum(d.savings_monthly for d in rows if d.risk_rating == "HIGH" or d.status == "Rejected")
    active = sum(d.savings_monthly for d in rows if d.active)
    return {"verified_low_risk_monthly":round(low,2),"potential_under_review_monthly":round(review,2),"rejected_monthly":round(rejected,2),"total_simulated_monthly":round(active,2),"annualized_potential":round(active*12,2)}


def ensure_workspace_columns():
    db.create_all()
    inspector = inspect(db.engine)
    columns = {c["name"] for c in inspector.get_columns("workspace_decision")} if "workspace_decision" in inspector.get_table_names() else set()
    additions = {"q1_client_workflow":"BOOLEAN","q2_active_use":"BOOLEAN","q3_important_files":"BOOLEAN","q4_critical_integrations":"BOOLEAN"}
    for name, sql_type in additions.items():
        if name not in columns:
            db.session.execute(text(f"ALTER TABLE workspace_decision ADD COLUMN {name} {sql_type}"))
    db.session.commit()


@workspace_bp.route("/workspace")
@login_required
def workspace_home():
    user_id = session["user_id"]
    discovery = build_discovery(user_id)
    candidates = recurring_candidates(user_id)
    decisions = WorkspaceDecision.query.filter_by(user_id=user_id).order_by(WorkspaceDecision.updated_at.desc()).all()
    current_spend = round(sum(c["amount"] for c in candidates if c["is_known"]), 2)
    return render_template("decision_workspace.html", discovery=discovery, candidates=candidates, decisions=[serialize_decision(d) for d in decisions], totals=dashboard_totals(user_id), current_software_spend=current_spend)


@workspace_bp.route("/api/workspace/discovery", methods=["GET"])
@login_required
def discovery_api():
    return jsonify(build_discovery(session["user_id"]))


@workspace_bp.route("/api/workspace/decision", methods=["POST"])
@login_required
def add_decision():
    data = request.get_json(silent=True) or request.form
    tool_name = (data.get("tool_name") or "").strip()
    if not tool_name: return jsonify({"success":False,"message":"Tool name is required."}),400
    try: savings = max(0.0, float(data.get("savings_monthly") or 0))
    except (TypeError, ValueError): return jsonify({"success":False,"message":"Savings must be a number."}),400
    action = (data.get("action") or "INVESTIGATE").upper()
    d = WorkspaceDecision(user_id=session["user_id"],tool_name=tool_name,action=action,savings_monthly=savings)
    db.session.add(d); db.session.commit()
    return jsonify({"success":True,"decision":serialize_decision(d),"totals":dashboard_totals(session["user_id"])})


@workspace_bp.route("/api/workspace/decision/<int:decision_id>/risk", methods=["POST"])
@login_required
def verify_risk(decision_id):
    d = WorkspaceDecision.query.filter_by(id=decision_id,user_id=session["user_id"]).first_or_404()
    data = request.get_json(silent=True) or request.form
    answers = {k:str(data.get(k,"false")).lower() in ("true","1","yes","on") for k in ("q1","q2","q3","q4")}
    d.q1_client_workflow,d.q2_active_use,d.q3_important_files,d.q4_critical_integrations = answers["q1"],answers["q2"],answers["q3"],answers["q4"]
    d.risk_rating,d.status = risk_from_answers(answers)
    db.session.commit()
    return jsonify({"success":True,"decision":serialize_decision(d),"totals":dashboard_totals(session["user_id"])})


@workspace_bp.route("/api/workspace/decision/<int:decision_id>/toggle", methods=["POST"])
@login_required
def toggle_decision(decision_id):
    d = WorkspaceDecision.query.filter_by(id=decision_id,user_id=session["user_id"]).first_or_404()
    d.active = not d.active
    db.session.commit()
    return jsonify({"success":True,"decision":serialize_decision(d),"totals":dashboard_totals(session["user_id"])})


def register_workspace(app):
    if "workspace_home" not in app.view_functions: app.register_blueprint(workspace_bp)
    with app.app_context(): ensure_workspace_columns()
