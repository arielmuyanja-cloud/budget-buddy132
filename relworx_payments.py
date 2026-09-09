import os
import re
import uuid
import requests
from flask import render_template, request, redirect, url_for, jsonify, session, flash

RELWORX_BASE_URL = os.getenv("RELWORX_BASE_URL", "https://payments.relworx.com/api")
RELWORX_API_KEY = os.getenv("RELWORX_API_KEY", "")
RELWORX_ACCOUNT_NO = os.getenv("RELWORX_ACCOUNT_NO", "")

PLAN_AMOUNTS_UGX = {
    "STARTER": int(os.getenv("RELWORX_STARTER_UGX", "0") or 0),
    "GROWTH": int(os.getenv("RELWORX_GROWTH_UGX", "0") or 0),
    "PRO": int(os.getenv("RELWORX_PRO_UGX", "0") or 0),
}


def _headers():
    return {
        "Authorization": f"Bearer {RELWORX_API_KEY}",
        "Accept": "application/vnd.relworx.v2",
        "Content-Type": "application/json",
    }


def _normalize_uganda_msisdn(value):
    raw = re.sub(r"[\s()-]", "", (value or "").strip())
    if raw.startswith("+256"):
        raw = raw[1:]
    elif raw.startswith("0"):
        raw = "256" + raw[1:]
    if re.fullmatch(r"2567\d{8}", raw):
        return "+" + raw
    return None


def _current_user():
    from app import User
    user_id = session.get("user_id")
    return User.query.get(user_id) if user_id else None


def register_relworx(app):
    """Replace the old checkout view with Relworx mobile-money collection.

    The existing pricing forms still post to create_checkout_session; we replace
    only that endpoint's view function so the rest of the application is left intact.
    """
    from app import User, db

    @app.post("/_relworx_checkout_internal")
    def _relworx_checkout_internal():
        user = _current_user()
        if not user:
            return redirect(url_for("login"))

        plan = (request.form.get("plan") or "STARTER").upper()
        if plan not in PLAN_AMOUNTS_UGX:
            flash("Invalid plan selected.", "danger")
            return redirect(url_for("pricing"))

        amount_ugx = PLAN_AMOUNTS_UGX[plan]
        phone_raw = request.form.get("phone", "")

        if not phone_raw:
            return render_template(
                "relworx_checkout.html",
                plan=plan,
                amount_ugx=amount_ugx,
                pending=None,
                error=None,
            )

        if not RELWORX_API_KEY or not RELWORX_ACCOUNT_NO:
            return render_template(
                "relworx_checkout.html",
                plan=plan,
                amount_ugx=amount_ugx,
                pending=None,
                error="Relworx is not fully configured on the server yet.",
            ), 503

        if amount_ugx <= 0:
            return render_template(
                "relworx_checkout.html",
                plan=plan,
                amount_ugx=amount_ugx,
                pending=None,
                error=f"Set RELWORX_{plan}_UGX in Render before taking payments.",
            ), 503

        msisdn = _normalize_uganda_msisdn(phone_raw)
        if not msisdn:
            return render_template(
                "relworx_checkout.html",
                plan=plan,
                amount_ugx=amount_ugx,
                pending=None,
                error="Enter a valid Uganda mobile-money number, e.g. 0771234567.",
            ), 400

        reference = f"BB-{plan}-{uuid.uuid4().hex[:8]}"
        payload = {
            "account_no": RELWORX_ACCOUNT_NO,
            "reference": reference,
            "msisdn": msisdn,
            "currency": "UGX",
            "amount": amount_ugx,
            "description": f"Budget Buddy {plan} plan",
        }

        try:
            response = requests.post(
                f"{RELWORX_BASE_URL}/mobile-money/request-payment",
                json=payload,
                headers=_headers(),
                timeout=15,
            )
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            app.logger.error("Relworx request failed: %s", type(exc).__name__)
            return render_template(
                "relworx_checkout.html",
                plan=plan,
                amount_ugx=amount_ugx,
                pending=None,
                error="Could not reach the payment service. Please try again.",
            ), 502

        if not data.get("success"):
            return render_template(
                "relworx_checkout.html",
                plan=plan,
                amount_ugx=amount_ugx,
                pending=None,
                error=data.get("message") or "Relworx rejected the payment request.",
            ), 400

        internal_reference = data.get("internal_reference")
        if not internal_reference:
            return render_template(
                "relworx_checkout.html",
                plan=plan,
                amount_ugx=amount_ugx,
                pending=None,
                error="Relworx did not return a transaction reference.",
            ), 502

        session["relworx_pending"] = {
            "plan": plan,
            "amount": amount_ugx,
            "currency": "UGX",
            "reference": reference,
            "internal_reference": internal_reference,
        }
        session.modified = True

        return render_template(
            "relworx_checkout.html",
            plan=plan,
            amount_ugx=amount_ugx,
            pending=session["relworx_pending"],
            error=None,
        )

    @app.get("/relworx-status")
    def relworx_status():
        user = _current_user()
        if not user:
            return jsonify({"success": False, "message": "Please log in."}), 401

        pending = session.get("relworx_pending")
        if not pending:
            return jsonify({"success": False, "status": "none"}), 404

        try:
            response = requests.get(
                f"{RELWORX_BASE_URL}/mobile-money/check-request-status",
                params={
                    "internal_reference": pending["internal_reference"],
                    "account_no": RELWORX_ACCOUNT_NO,
                },
                headers=_headers(),
                timeout=15,
            )
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            app.logger.error("Relworx status check failed: %s", type(exc).__name__)
            return jsonify({"success": False, "message": "Unable to check payment status right now."}), 502

        request_status = str(data.get("request_status") or data.get("status") or "pending").lower()
        normalized = "pending"

        if request_status in {"success", "successful", "completed"}:
            try:
                paid_amount = float(data.get("amount") or 0)
            except (TypeError, ValueError):
                paid_amount = 0.0
            paid_currency = str(data.get("currency") or "").upper()

            if paid_currency != pending["currency"] or paid_amount != float(pending["amount"]):
                return jsonify({
                    "success": False,
                    "status": "mismatch",
                    "message": "Payment details did not match this order.",
                }), 409

            user.plan_tier = pending["plan"]
            db.session.commit()
            session.pop("relworx_pending", None)
            session.modified = True
            normalized = "success"

        elif request_status in {"failed", "cancelled", "canceled"}:
            normalized = "failed"

        return jsonify({
            "success": True,
            "status": normalized,
            "plan": pending["plan"],
            "message": data.get("message", ""),
        })

    # The original /create-checkout-session route is already registered by app.py.
    # Swap its implementation after app.py is loaded, keeping pricing.html unchanged.
    app.view_functions["create_checkout_session"] = _relworx_checkout_internal
