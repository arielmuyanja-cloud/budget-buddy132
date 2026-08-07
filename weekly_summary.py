
"""Optional cron entry point.
Set DATABASE_PATH, SECRET_KEY, SMTP_* and run this once each Monday with:
python weekly_summary.py
"""
import os
from app import db, generate_insights, totals, send_email

conn = db()
users = conn.execute("SELECT * FROM users WHERE tier='PRO'").fetchall()
conn.close()

for user in users:
    income, expense = totals(user["id"])
    body = (
        f"Budget Buddy weekly summary for {user['first_name']}.\n\n"
        f"Revenue this month: ${income:,.2f}\n"
        f"Expenses this month: ${expense:,.2f}\n"
        f"Profit: ${income-expense:,.2f}\n\n"
        + "\n".join(f"- {x}" for x in generate_insights(user["id"]))
    )
    send_email(user["email"], "Your Budget Buddy weekly summary", body)
