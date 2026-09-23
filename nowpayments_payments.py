"""
NOWPayments crypto payment integration for Budget Buddy.

Powers two flows, both confirmed automatically via IPN webhook instead of
manual review:
  1. Plan upgrades (Starter/Growth/Pro) — an extra "Pay with Crypto" option
     alongside the existing Relworx/Sendwave checkout on the pricing page.
  2. One-time paid audits — a public /audit/buy page for selling audits to
     leads who don't have (and don't need) a Budget Buddy account.

Env vars required:
  NOWPAYMENTS_API_KEY
  NOWPAYMENTS_IPN_SECRET
Optional:
  NOWPAYMENTS_AUDIT_PRICE_USD  (default 149)

Registration pattern mirrors relworx_payments.py: a register_nowpayments(app)
function called from workspace_app.py after app.py's routes are loaded.
"""

import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime

import requests
from flask import render_template, request, redirect, url_for, jsonify, session, flash

NOWPAYMENTS_API_BASE = "https://api.nowpayments.io/v1"
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "")
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")

PLAN_AMOUNTS_USD = {
    "STARTER": 49,
    "GROWTH": 149,
    "PRO": 299,
}

AUDIT_PRICE_USD = float(os.environ.get("NOWPAYMENTS_AUDIT_PRICE_USD", "149") or 149)


def _headers():
    return {
        "x-api-key": NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json",
    }


def _current_user():
    from app import User
    user_id = session.get("user_id")
    return User.query.get(user_id) if user_id else None


def _create_invoice(order_id, amount_usd, description, success_url, cancel_url, ipn_url):
    resp = requests.post(
        f"{NOWPAYMENTS_API_BASE}/invoice",
        headers=_headers(),
        json={
            "price_amount": amount_usd,
            "price_currency": "usd",
            "order_id": order_id,
            "order_description": description,
            "ipn_callback_url": ipn_url,
            "success_url": success_url,
            "cancel_url": cancel_url,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _verify_ipn_signature(raw_body: bytes, signature_header: str) -> bool:
    if not signature_header or not NOWPAYMENTS_IPN_SECRET:
        return False
    try:
        payload = json.loads(raw_body)
    except ValueError:
        return False
    sorted_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    expected_sig = hmac.new(
        NOWPAYMENTS_IPN_SECRET.encode("utf-8"),
        sorted_payload.encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(expected_sig, signature_header)


def register_nowpayments(app):
    from app import User, NowPaymentsOrder, db, send_email_via_resend, ADMIN_EMAIL

    # ---------- Plan upgrade checkout (extra option next to Relworx/Sendwave) ----------

    @app.post("/nowpayments/plan/<plan>")
    def nowpayments_plan_checkout(plan):
        user = _current_user()
        if not user:
            return redirect(url_for("login"))

        plan = plan.upper()
        if plan not in PLAN_AMOUNTS_USD:
            flash("Invalid plan selected.", "danger")
            return redirect(url_for("pricing"))

        if not NOWPAYMENTS_API_KEY or not NOWPAYMENTS_IPN_SECRET:
            flash("Crypto payments aren't fully configured yet. Try Sendwave instead.", "danger")
            return redirect(url_for("pricing"))

        amount = PLAN_AMOUNTS_USD[plan]
        order_id = f"BB-PLAN-{plan}-{user.id}-{uuid.uuid4().hex[:8]}"

        order = NowPaymentsOrder(
            user_id=user.id,
            order_type="PLAN",
            plan_requested=plan,
            order_id=order_id,
            amount_usd=amount,
            customer_email=user.email,
            status="PENDING",
        )
        db.session.add(order)
        db.session.commit()

        try:
            invoice = _create_invoice(
                order_id=order_id,
                amount_usd=amount,
                description=f"Budget Buddy {plan} plan",
                success_url=url_for("nowpayments_payment_result", outcome="success", order=order_id, _external=True),
                cancel_url=url_for("nowpayments_payment_result", outcome="cancelled", order=order_id, _external=True),
                ipn_url=url_for("nowpayments_webhook", _external=True),
            )
        except (requests.RequestException, ValueError) as exc:
            app.logger.error("NOWPayments invoice creation failed: %s", type(exc).__name__)
            flash("Could not reach the crypto payment service. Please try again.", "danger")
            return redirect(url_for("pricing"))

        invoice_url = invoice.get("invoice_url")
        if not invoice_url:
            flash("NOWPayments did not return a checkout link. Please try again.", "danger")
            return redirect(url_for("pricing"))

        return redirect(invoice_url)

    # ---------- One-time paid audit (public, no account required) ----------

    @app.route("/audit/buy", methods=["GET", "POST"])
    def audit_buy():
        if request.method == "GET":
            return render_template("audit_buy.html", price=AUDIT_PRICE_USD, error=None)

        email = (request.form.get("email") or "").strip()
        name = (request.form.get("name") or "").strip()
        agency = (request.form.get("agency") or "").strip()

        if not email or "@" not in email:
            return render_template("audit_buy.html", price=AUDIT_PRICE_USD, error="Enter a valid email address.")

        if not NOWPAYMENTS_API_KEY or not NOWPAYMENTS_IPN_SECRET:
            return render_template(
                "audit_buy.html", price=AUDIT_PRICE_USD,
                error="Crypto payments aren't fully configured yet. Please try again shortly.",
            ), 503

        order_id = f"BB-AUDIT-{uuid.uuid4().hex[:10]}"
        display_name = name or agency or email

        order = NowPaymentsOrder(
            user_id=None,
            order_type="AUDIT",
            order_id=order_id,
            amount_usd=AUDIT_PRICE_USD,
            customer_email=email,
            customer_name=display_name,
            status="PENDING",
        )
        db.session.add(order)
        db.session.commit()

        try:
            invoice = _create_invoice(
                order_id=order_id,
                amount_usd=AUDIT_PRICE_USD,
                description=f"Budget Buddy one-time software spend audit ({display_name})",
                success_url=url_for("nowpayments_payment_result", outcome="success", order=order_id, _external=True),
                cancel_url=url_for("nowpayments_payment_result", outcome="cancelled", order=order_id, _external=True),
                ipn_url=url_for("nowpayments_webhook", _external=True),
            )
        except (requests.RequestException, ValueError) as exc:
            app.logger.error("NOWPayments invoice creation failed: %s", type(exc).__name__)
            return render_template(
                "audit_buy.html", price=AUDIT_PRICE_USD,
                error="Could not reach the payment service. Please try again.",
            ), 502

        invoice_url = invoice.get("invoice_url")
        if not invoice_url:
            return render_template(
                "audit_buy.html", price=AUDIT_PRICE_USD,
                error="Payment provider did not return a checkout link. Please try again.",
            ), 502

        return redirect(invoice_url)

    # ---------- Shared success / cancelled landing page ----------

    @app.get("/payment/nowpayments-result")
    def nowpayments_payment_result():
        outcome = request.args.get("outcome", "success")
        order_id = request.args.get("order", "")
        order = NowPaymentsOrder.query.filter_by(order_id=order_id).first()
        return render_template("payment_result.html", outcome=outcome, order=order)

    # ---------- IPN webhook: this is what makes confirmation automatic ----------

    @app.post("/webhooks/nowpayments")
    def nowpayments_webhook():
        raw_body = request.get_data()
        signature = request.headers.get("x-nowpayments-sig", "")

        if not _verify_ipn_signature(raw_body, signature):
            app.logger.warning("NOWPayments webhook: invalid signature, rejected.")
            return jsonify({"error": "invalid signature"}), 401

        data = request.get_json(silent=True) or {}
        order_id = data.get("order_id")
        status = data.get("payment_status")
        payment_id = data.get("payment_id")

        order = NowPaymentsOrder.query.filter_by(order_id=order_id).first()
        if not order:
            app.logger.warning("NOWPayments webhook: unknown order_id %s", order_id)
            return jsonify({"received": True}), 200

        if payment_id:
            order.payment_id = str(payment_id)

        if status == "finished" and order.status != "PAID":
            order.status = "PAID"
            order.paid_at = datetime.utcnow()
            db.session.commit()

            if order.order_type == "PLAN" and order.user_id:
                user = User.query.get(order.user_id)
                if user:
                    user.plan_tier = order.plan_requested
                    db.session.commit()
                    send_email_via_resend(
                        user.email,
                        f"You're now on the {order.plan_requested} plan",
                        f"<p>Payment received — your Budget Buddy account is now on the "
                        f"<b>{order.plan_requested}</b> plan.</p>",
                    )

            elif order.order_type == "AUDIT":
                # Not self-serve yet — notify the admin to deliver the audit manually.
                send_email_via_resend(
                    ADMIN_EMAIL,
                    f"Paid audit order — {order.customer_email} (${order.amount_usd:.0f})",
                    f"<p>New one-time audit paid via NOWPayments.</p>"
                    f"<p>Customer: {order.customer_name or ''} &lt;{order.customer_email}&gt;<br>"
                    f"Order: {order.order_id}<br>"
                    f"Amount: ${order.amount_usd:.0f}</p>",
                )

        elif status in ("failed", "expired", "refunded") and order.status not in ("PAID",):
            order.status = status.upper()
            db.session.commit()
        else:
            db.session.commit()  # persist payment_id even for interim statuses

        return jsonify({"received": True}), 200
