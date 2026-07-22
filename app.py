import os
import csv
import io
from flask import Flask, render_template, request, redirect, url_for, flash, session, Response
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv

# Load environment variables from .env file if available
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'default_fallback_secret_key_12345')

# ---------------------------------------------------------------------------
# DATA PERSISTENCE SETUP
# Uses DATABASE_URL (PostgreSQL) if available on your host, otherwise SQLite
# ---------------------------------------------------------------------------
basedir = os.path.abspath(os.path.dirname(__file__))
db_url = os.getenv('DATABASE_URL', 'sqlite:///' + os.path.join(basedir, 'budget.db'))

# Fix for Render/Heroku postgres:// URI compatibility with SQLAlchemy
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Database Model for Transactions
class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(20), nullable=False)
    description = db.Column(db.String(200), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    type = db.Column(db.String(10), nullable=False)  # 'income' or 'expense'

# Initialize database tables
with app.app_context():
    db.create_all()


# ---------------------------------------------------------------------------
# AI AUDIT LOGIC
# ---------------------------------------------------------------------------
def analyze_statement_data(lines):
    insights = []
    annual_savings = 0.0
    ai_keywords = ['chatgpt', 'openai', 'midjourney', 'claude', 'anthropic', 'elevenlabs', 'voice generator', 'ai pro']
    
    for line in lines:
        line_lower = line.lower()
        for keyword in ai_keywords:
            if keyword in line_lower:
                insights.append(f"Detected AI Subscription: '{line.strip()}' - Flagged for review.")
                annual_savings += 240.00
                break
                
        if 'duplicate' in line_lower:
            insights.append(f"Potential Duplicate Detected: '{line.strip()}'")
            annual_savings += 100.00

    if not insights:
        insights.append("No obvious unused AI subscriptions or spending leaks detected.")

    return {
        'insights': insights,
        'annual_savings': f"{annual_savings:.2f}"
    }


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        description = request.form.get('description')
        amount = request.form.get('amount')
        income_amount = request.form.get('income_amount')

        if description and amount:
            new_expense = Transaction(
                date='2026-07-22',
                description=description,
                amount=float(amount),
                type='expense'
            )
            db.session.add(new_expense)
            db.session.commit()
            flash('Expense logged and saved to database!', 'success')
            return redirect(url_for('index'))

        if income_amount:
            new_income = Transaction(
                date='2026-07-22',
                description='Income Deposit',
                amount=float(income_amount),
                type='income'
            )
            db.session.add(new_income)
            db.session.commit()
            flash('Income logged and saved to database!', 'success')
            return redirect(url_for('index'))

    # Fetch persisted transactions from Database
    all_transactions = Transaction.query.all()
    
    total_income = sum(t.amount for t in all_transactions if t.type == 'income')
    total_expense = sum(t.amount for t in all_transactions if t.type == 'expense')
    balance = total_income - total_expense

    subscription = {'plan': 'Free', 'status': 'active'}

    return render_template(
        'index.html',
        username='User',
        account_type='PERSONAL',
        subscription=subscription,
        total_income=total_income,
        total_expense=total_expense,
        balance=balance,
        transactions=all_transactions
    )


@app.route('/upload_statement', methods=['POST'])
def upload_statement():
    file = request.files.get('file')
    raw_text = request.form.get('raw_text', '').strip()
    parsed_entries = []

    # 1. Parse raw text input
    if raw_text:
        lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
        for line in lines:
            parts = [p.strip() for p in line.split(',') if p.strip()]
            if len(parts) >= 2:
                try:
                    desc = parts[0]
                    amt = abs(float(parts[1].replace('$', '')))
                    parsed_entries.append({'date': '2026-07-22', 'desc': desc, 'amount': amt, 'type': 'expense'})
                except ValueError:
                    continue

    # 2. Parse uploaded CSV file
    elif file and file.filename.endswith('.csv'):
        try:
            stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
            csv_reader = csv.reader(stream)
            for row in csv_reader:
                if not row or len(row) < 3 or 'amount' in row[2].lower() or 'description' in row[1].lower():
                    continue
                try:
                    parsed_entries.append({
                        'date': row[0].strip() if row[0].strip() else '2026-07-22',
                        'desc': row[1].strip(),
                        'amount': abs(float(row[2].replace('$', '').strip())),
                        'type': row[3].strip().lower() if len(row) > 3 and row[3].strip().lower() in ['income', 'expense'] else 'expense'
                    })
                except ValueError:
                    continue
        except Exception as e:
            flash(f'Error reading CSV file: {str(e)}', 'danger')
            return redirect(url_for('index'))
    else:
        flash('Please select a CSV file or paste statement text.', 'warning')
        return redirect(url_for('index'))

    # Save imported transactions into Database
    if parsed_entries:
        for entry in parsed_entries:
            db.session.add(Transaction(
                date=entry['date'],
                description=entry['desc'],
                amount=entry['amount'],
                type=entry['type']
            ))
        db.session.commit()
        
        lines_for_audit = [f"{e['desc']} - ${e['amount']}" for e in parsed_entries]
        session['last_audit'] = analyze_statement_data(lines_for_audit)
        flash(f'Successfully imported and saved {len(parsed_entries)} transactions!', 'success')
    else:
        flash('No valid transactions extracted.', 'danger')

    return redirect(url_for('index'))


# ---------------------------------------------------------------------------
# EXPORT REPORT ROUTE (CSV Download)
# ---------------------------------------------------------------------------
@app.route('/export_report')
def export_report():
    all_transactions = Transaction.query.all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # CSV Header
    writer.writerow(['ID', 'Date', 'Description', 'Type', 'Amount ($)'])
    
    # CSV Data Rows
    for t in all_transactions:
        writer.writerow([t.id, t.date, t.description, t.type.upper(), f"{t.amount:.2f}"])
        
    output.seek(0)
    
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=Budget_Buddy_Statement_Report.csv"}
    )


@app.route('/pricing')
def pricing():
    return "Pricing Page Coming Soon!"


@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully.', 'info')
    return redirect(url_for('index'))


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
