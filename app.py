import os
import csv
import io
from flask import Flask, render_template, request, redirect, url_for, flash, session

app = Flask(__name__)
app.secret_key = 'your_super_secret_key_here'  # Change this to a secure random key

# Mock in-memory database for transactions
transactions = []

def analyze_statement_data(lines):
    """
    Analyzes raw text lines or CSV rows to detect recurring AI subscriptions and duplicate charges.
    """
    insights = []
    annual_savings = 0.0

    ai_keywords = ['chatgpt', 'openai', 'midjourney', 'claude', 'anthropic', 'elevenlabs', 'voice generator']
    
    for line in lines:
        line_lower = line.lower()
        
        # Check for AI software subscriptions
        for keyword in ai_keywords:
            if keyword in line_lower:
                insights.append(f"Detected AI Subscription: '{line.strip()}' - Consider canceling if underutilized.")
                annual_savings += 240.00  # Estimated annual cost ($20/mo)
                break
                
        # Check for duplicate charges or anomalies
        if 'duplicate' in line_lower:
            insights.append(f"Flagged Potential Duplicate: '{line.strip()}'")
            annual_savings += 100.00

    if not insights:
        insights.append("No obvious unused AI subscriptions or leaks detected in this statement.")

    return {
        'insights': insights,
        'annual_savings': f"{annual_savings:.2f}"
    }


@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        # Handlers for manual transaction logging
        description = request.form.get('description')
        amount = request.form.get('amount')
        income_amount = request.form.get('income_amount')

        if description and amount:
            transactions.append({
                'date': '2026-07-22',
                'description': description,
                'amount': float(amount),
                'type': 'expense'
            })
            flash('Expense saved successfully!', 'success')
            return redirect(url_for('index'))

        if income_amount:
            transactions.append({
                'date': '2026-07-22',
                'description': 'Income Deposit',
                'amount': float(income_amount),
                'type': 'income'
            })
            flash('Income added successfully!', 'success')
            return redirect(url_for('index'))

    # Calculate Totals
    total_income = sum(t['amount'] for t in transactions if t['type'] == 'income')
    total_expense = sum(t['amount'] for t in transactions if t['type'] == 'expense')
    balance = total_income - total_expense

    # Mock Subscription Info
    subscription = {
        'plan': 'Free',
        'status': 'active'
    }

    return render_template(
        'index.html',
        username='User',
        account_type='PERSONAL',
        subscription=subscription,
        total_income=total_income,
        total_expense=total_expense,
        balance=balance,
        transactions=transactions
    )


@app.route('/upload_statement', methods=['POST'])
def upload_statement():
    file = request.files.get('file')
    raw_text = request.form.get('raw_text', '').strip()

    # Option 1: User pasted raw statement or SMS text
    if raw_text:
        lines = [line for line in raw_text.split('\n') if line.strip()]
        session['last_audit'] = analyze_statement_data(lines)
        flash('Statement text processed and audited successfully!', 'success')
        return redirect(url_for('index'))

    # Option 2: Check if a file was selected
    if not file or file.filename == '':
        flash('Please select a CSV file or paste statement text before clicking Audit.', 'warning')
        return redirect(url_for('index'))

    # Option 3: User uploaded a CSV file
    if file and file.filename.endswith('.csv'):
        try:
            stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
            csv_reader = csv.reader(stream)
            lines = [", ".join(row) for row in csv_reader if row]
            
            session['last_audit'] = analyze_statement_data(lines)
            flash('CSV Statement uploaded and audited successfully!', 'success')
        except Exception as e:
            flash(f'Error reading CSV file: {str(e)}', 'danger')
        return redirect(url_for('index'))
    else:
        flash('Invalid file format. Please upload a standard .csv file.', 'danger')
        return redirect(url_for('index'))


@app.route('/pricing')
def pricing():
    return "Pricing Page Coming Soon!"


@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully.', 'info')
    return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
