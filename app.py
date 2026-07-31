import os
import csv
import io
from flask import Flask, render_template, request, redirect, url_for, flash, session, Response
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'default_fallback_secret_key_12345')

basedir = os.path.abspath(os.path.dirname(__file__))
db_url = os.getenv('DATABASE_URL', 'sqlite:///' + os.path.join(basedir, 'budget.db'))

if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(20), nullable=False)
    description = db.Column(db.String(200), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    type = db.Column(db.String(10), nullable=False)

with app.app_context():
    db.create_all()

def analyze_statement_data(lines):
    insights = []
    annual_savings = 0.0
    monthly_savings = 0.0
    subs = []
    ai_keywords = ['chatgpt', 'openai', 'midjourney', 'claude', 'anthropic', 'elevenlabs', 'slack', 'canva', 'adobe', 'hubspot']
    
    for line in lines:
        line_lower = line.lower()
        for keyword in ai_keywords:
            if keyword in line_lower:
                insights.append(f"Detected SaaS Subscription: '{line.strip()}' - Flagged for review.")
                monthly_savings += 50.00
                annual_savings += 600.00
                subs.append({'name': line.strip(), 'amount': 50.00})
                break

    if not insights:
        insights.append("No obvious unused subscriptions detected yet.")

    return {
        'insights': insights,
        'monthly_savings': monthly_savings,
        'annual_savings': annual_savings,
        'subs': subs
    }

# 1. LANDING PAGE
@app.route('/')
def index():
    return render_template('index.html')

# 2. LANDING PAGE FORM SUBMIT -> REDIRECT TO DASHBOARD
@app.route('/login_audit', methods=['POST'])
def login_audit():
    agency_name = request.form.get('agency_name', 'Agency User')
    email = request.form.get('work_email')
    
    session['username'] = agency_name
    session['work_email'] = email
    
    return redirect(url_for('dashboard'))

# 3. DASHBOARD ROUTE
@app.route('/dashboard')
def dashboard():
    username = session.get('username', 'Guest Business')
    all_transactions = Transaction.query.all()
    
    total_income = sum(t.amount for t in all_transactions if t.type == 'income')
    total_expense = sum(t.amount for t in all_transactions if t.type == 'expense')
    balance = total_income - total_expense

    return render_template(
        'business_dashboard.html',
        username=username,
        total_income=total_income,
        total_expense=total_expense,
        balance=balance,
        transactions=all_transactions
    )

# 4. CSV UPLOAD ROUTE (REDIRECTS BACK TO DASHBOARD)
@app.route('/upload_statement', methods=['POST'])
def upload_statement():
    file = request.files.get('file')
    parsed_entries = []

    if file and file.filename.endswith('.csv'):
        try:
            stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
            csv_reader = csv.reader(stream)
            for row in csv_reader:
                if not row or len(row) < 3 or 'amount' in row[2].lower() or 'description' in row[1].lower():
                    continue
                try:
                    parsed_entries.append({
                        'date': row[0].strip() if row[0].strip() else '2026-08-01',
                        'desc': row[1].strip(),
                        'amount': abs(float(row[2].replace('$', '').strip())),
                        'type': row[3].strip().lower() if len(row) > 3 and row[3].strip().lower() in ['income', 'expense'] else 'expense'
                    })
                except ValueError:
                    continue
        except Exception as e:
            flash(f'Error reading CSV file: {str(e)}', 'danger')
            return redirect(url_for('dashboard'))

    if parsed_entries:
        for entry in parsed_entries:
            db.session.add(Transaction(
                date=entry['date'],
                description=entry['desc'],
                amount=entry['amount'],
                type=entry['type']
            ))
        db.session.commit()
        
        lines_for_audit = [f"{e['desc']}" for e in parsed_entries]
        session['last_audit'] = analyze_statement_data(lines_for_audit)
        flash(f'Successfully imported and saved {len(parsed_entries)} transactions!', 'success')

    return redirect(url_for('dashboard'))

@app.route('/pricing')
def pricing():
    return render_template('pricing.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully.', 'info')
    return redirect(url_for('index'))

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
