from flask import Blueprint, render_template, request, jsonify, session
from app import db, Transaction, login_required, call_ai_provider
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


@profit_simulator_bp.route("/api/profit-leak-simulator/ai-summary", methods=["POST"])
@login_required
def simulator_ai_summary():
    user_id = session["user_id"]
    decisions = WorkspaceDecision.query.filter_by(user_id=user_id, active=True).order_by(WorkspaceDecision.savings_monthly.desc()).all()
    candidates = recurring_candidates(user_id)
    totals = _totals(user_id)

    if not decisions and not candidates:
        return jsonify({
            "success": True,
            "ai_generated": False,
            "summary": "No transaction history to work with yet — import at least two months of bank/card statements so there's real data to validate a simulation against.",
        })

    # Vendors already reviewed and risk-checked by the user in the Decision Workspace.
    staged_names = {d.tool_name.strip().lower() for d in decisions}
    decision_lines = [
        f"- {d.tool_name}: ${d.savings_monthly:.2f}/mo, risk={d.risk_rating}, status={d.status} (user-verified)"
        for d in decisions
    ]

    # Vendors detected directly in transaction history that haven't been staged/reviewed yet.
    unstaged = [c for c in candidates if c["vendor"].strip().lower() not in staged_names]
    unstaged.sort(key=lambda c: c["amount"], reverse=True)
    candidate_lines = [
        f"- {c['vendor']}: ${c['amount']:.2f}/mo, seen {c['appearances']}x across {c['months']} month(s), "
        f"evidence={c['evidence_strength']}, category={c['taxonomy'] or 'uncategorized'} (from transactions, not yet reviewed)"
        for c in unstaged[:15]
    ]

    question = (
        f"Current total monthly software spend detected from transactions: ${totals['current_monthly']:.2f}.\n\n"
        "Decisions the user has already staged and risk-checked in the Decision Workspace:\n"
        + ("\n".join(decision_lines) if decision_lines else "(none staged yet)") +
        "\n\nOther recurring vendors found directly in transaction history that have NOT been reviewed for risk yet:\n"
        + ("\n".join(candidate_lines) if candidate_lines else "(none — everything recurring has already been staged)") +
        "\n\nBased ONLY on the data above, tell this agency owner what to simulate cutting first. "
        "Clearly separate user-verified savings from unreviewed transaction-detected savings — "
        "never state an unreviewed amount as guaranteed or safe to cut without verification. "
        "Cite exact dollar amounts and vendor names from the data given. 4-5 sentences max."
    )
    system_prompt = (
        "You are a blunt, evidence-based financial advisor for a small marketing agency. "
        "You must ground every claim strictly in the transaction-derived data provided — never invent a tool, "
        "vendor, or dollar figure that isn't listed. Distinguish clearly between VERIFIED savings (already "
        "risk-checked by the user) and UNVERIFIED savings (detected in transactions but not yet reviewed) — "
        "never present unverified numbers as guaranteed. No bullet points, no markdown, plain sentences, under 130 words."
    )

    ai_summary = call_ai_provider(system_prompt, question)
    if ai_summary:
        return jsonify({"success": True, "ai_generated": True, "summary": ai_summary})

    # Rule-based fallback if no API key is configured or the call failed/timed out.
    if decisions:
        top = decisions[0]
        fallback = (
            f"Start with {top.tool_name} — your biggest verified saving at ${top.savings_monthly:.2f}/mo "
            f"(risk: {top.risk_rating}). Across all {len(decisions)} staged decisions you're looking at "
            f"${totals['potential_monthly']:.2f}/mo (${totals['potential_annual']:.2f}/yr) in potential savings. "
        )
    else:
        fallback = ""
    if unstaged:
        top_c = unstaged[0]
        fallback += (
            f"Transaction history also shows {top_c['vendor']} at ${top_c['amount']:.2f}/mo "
            f"({top_c['evidence_strength']} evidence) that hasn't been risk-checked yet — verify it in the "
            "Decision Workspace before counting it as savings."
        )
    if not fallback:
        fallback = "Nothing to recommend yet — stage a decision in the Decision Workspace or import more transaction history."
    return jsonify({"success": True, "ai_generated": False, "summary": fallback})


def register_profit_simulator(app):
    if "profit_simulator.simulator_home" not in app.view_functions:
        app.register_blueprint(profit_simulator_bp)
