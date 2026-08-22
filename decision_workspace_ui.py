from flask import Blueprint, render_template, session
from app import login_required, Transaction
from workspace import build_discovery, recurring_candidates, WorkspaceDecision, dashboard_totals

decision_ui_bp = Blueprint("decision_ui", __name__)

@decision_ui_bp.route("/decision-workspace")
@login_required
def decision_workspace_home():
    user_id = session["user_id"]
    discovery = build_discovery(user_id)
    candidates = recurring_candidates(user_id)
    decisions = WorkspaceDecision.query.filter_by(user_id=user_id).order_by(WorkspaceDecision.updated_at.desc()).all()
    current_software_spend = round(sum(c["amount"] for c in candidates if c["is_known"]), 2)
    serialized = [{
        "id": d.id,
        "tool_name": d.tool_name,
        "action": d.action,
        "savings_monthly": round(d.savings_monthly or 0, 2),
        "risk_rating": d.risk_rating,
        "status": d.status,
        "active": d.active,
    } for d in decisions]
    return render_template(
        "decision_workspace_sidebar.html",
        discovery=discovery,
        candidates=candidates,
        decisions=serialized,
        totals=dashboard_totals(user_id),
        current_software_spend=current_software_spend,
    )

def register_decision_workspace_ui(app):
    if "decision_ui.decision_workspace_home" not in app.view_functions:
        app.register_blueprint(decision_ui_bp)
