from flask import Blueprint, render_template, request, jsonify, session
from app import db, Transaction, login_required
from workspace import WorkspaceDecision, recurring_candidates, risk_from_answers

profit_simulator_bp = Blueprint("profit_simulator", __name__)

SCENARIO_NOTICE = (
    "This is a scenario based on transaction history. Actual savings depend on "
    "successful cancellation and whether the tool can be removed without replacing its functionality."
)


def _totals(user_id):
    decisions = WorkspaceDecision.query.filter_by(user_id=user_id).all()
    active = [d for d in decisions if d.active]
    potential = sum(float(d.savings_monthly or 0) for d in active)
    verified = sum(float(d.savings_monthly or 0) for d in active if d.risk_rating == "LOW" and d.status == "Verified")
    under_review = sum(float(d.savings_monthly or 0) for d in active if d.risk_rating in ("UNKNOWN", "MEDIUM"))
    rejected = sum(float(d.savings_monthly or 0) for d in decisions if d.risk_rating == "HIGH" or d.status == "Rejected")
    current = sum(float(c["amount"] or 0) for c in recurring_candidates(user_id) if c["is_known"])
    return {
        "current_monthly": round(current, 2),
        "simulated_monthly": round(max(0, current - potential), 2),
        "potential_monthly": round(potential, 2),
        "potential_annual": round(potential * 12, 2),
        "illustrative_value": round(potential * 12 * 4, 2),
        "verified_monthly": round(verified, 2),
        "under_review_monthly": round(under_review, 2),
        "rejected_monthly": round(rejected, 2),
    }


@profit_simulator_bp.route("/profit-leak-simulator")
@login_required
def simulator_home():
    user_id = session["user_id"]
    decisions = WorkspaceDecision.query.filter_by(user_id=user_id).order_by(WorkspaceDecision.updated_at.desc()).all()
    return render_template(
        "profit_simulator.html",
        decisions=decisions,
        totals=_totals(user_id),
        scenario_notice=SCENARIO_NOTICE,
    )


@profit_simulator_bp.route("/api/profit-leak-simulator/decision/<int:decision_id>/risk", methods=["POST"])
@login_required
def simulator_risk(decision_id):
    decision = WorkspaceDecision.query.filter_by(id=decision_id, user_id=session["user_id"]).first_or_404()
    data = request.get_json(silent=True) or {}
    answers = {key: bool(data.get(key)) for key in ("q1", "q2", "q3", "q4")}
    decision.risk_rating, decision.status = risk_from_answers(answers)
    db.session.commit()
    return jsonify({"success": True, "risk": decision.risk_rating, "status": decision.status, "totals": _totals(session["user_id"])})


@profit_simulator_bp.route("/api/profit-leak-simulator/decision/<int:decision_id>/toggle", methods=["POST"])
@login_required
def simulator_toggle(decision_id):
    decision = WorkspaceDecision.query.filter_by(id=decision_id, user_id=session["user_id"]).first_or_404()
    decision.active = not decision.active
    db.session.commit()
    return jsonify({"success": True, "active": decision.active, "totals": _totals(session["user_id"])})


def register_profit_simulator(app):
    if "simulator_home" not in app.view_functions:
        app.register_blueprint(profit_simulator_bp)
