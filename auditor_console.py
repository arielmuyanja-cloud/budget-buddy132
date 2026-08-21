"""Auditor Console adapter.

This module intentionally does not invent outputs from audit services that are
not present in the deployed repository. When the phase service modules are
available, their adapters can be connected here without changing the UI.
"""

import importlib


def _service_status(module_name):
    try:
        importlib.import_module(module_name)
        return "COMPLETE", "Service module present"
    except ModuleNotFoundError:
        return "PENDING", "Service module not committed"
    except Exception as exc:
        return "CHECK", f"Import error: {type(exc).__name__}"


def register(app):
    @app.route("/auditor-console")
    def auditor_console():
        from flask import render_template, session
        from app import User, Transaction, compute_financial_health, get_monthly_series, detect_financial_risks

        user_id = session.get("user_id")
        if not user_id:
            from flask import redirect, url_for
            return redirect(url_for("login"))

        user = User.query.get(user_id)
        transactions = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.date.asc()).all()
        revenue = sum(t.amount for t in transactions if t.type == "INCOME")
        expenses = sum(t.amount for t in transactions if t.type == "EXPENSE")
        profit = revenue - expenses
        months = get_monthly_series(user.id)
        risks = detect_financial_risks(months, {})
        health = compute_financial_health(revenue, expenses, [], user.id)

        services = [
            ("CSV & Recurring", "app", "complete"),
            ("Vendor Directory", "services.vendor_service", "service"),
            ("Overlap Engine", "services.overlap_service", "service"),
            ("Opportunity Logic", "services.opportunity_service", "service"),
            ("Simulator Service", "services.simulator_service", "service"),
        ]
        module_status = []
        for label, module_name, kind in services:
            if kind == "complete":
                status, detail = "COMPLETE", "Core statement analytics present"
            else:
                status, detail = _service_status(module_name)
            module_status.append({"label": label, "status": status, "detail": detail})

        sample_rows = []
        for tx in transactions[:8]:
            classification = tx.type
            inference = "Recorded transaction only; no usage inference"
            sample_rows.append({
                "fact": f"{tx.description} | ${tx.amount:,.2f} | {tx.date}",
                "classification": classification,
                "inference": inference,
                "verified": True,
            })

        return render_template(
            "auditor_console.html",
            user=user,
            module_status=module_status,
            health_score=health,
            anomaly_count=len(risks),
            transaction_count=len(transactions),
            revenue=revenue,
            expenses=expenses,
            profit=profit,
            risk_count=len(risks),
            risk_summary=risks[0]["narrative"] if risks else "No financial risk flags detected from the available records.",
            verification_rows=sample_rows,
            service_modules_available=all(item["status"] == "COMPLETE" for item in module_status),
        )

    return app
