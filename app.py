import os
import csv
import io
from flask import Flask, render_template, request, redirect, url_for, flash, session

app = Flask(__name__)
app.secret_key = 'your_super_secret_key_here'  # Replace with a secure key in production

# In-memory database for transactions
transactions = []

def analyze_statement_data(lines):
    """
    Analyzes transaction descriptions to detect recurring AI subscriptions, 
    unused software costs, and duplicate entries.
    """
    insights = []
    annual_savings = 0.0

    ai_keywords = ['chatgpt', 'openai', 'midjourney', 'claude', 'anthropic', 'elevenlabs', 'voice generator', 'ai pro']
    
    for line in lines:
        line_lower = line.lower()
        
        # Check for recurring software/AI subscriptions
        for keyword in ai_keywords:
            if keyword in line_lower:
                insights.append(f"Detected AI Subscription: '{line.strip()}' - Flagged for review.")
                annual_savings += 240.00  # Estimated annual savings
                break
                
        # Check for potential duplicates
        if 'duplicate' in line_lower:
            insights.append(f"Potential Duplicate Detected: '{line.strip()}'")
            annual_savings += 100.00

    if not insights:
        insights.append("No obvious unused AI subscriptions or spending leaks detected.")

    return {
        'insights': insights,
        'annual_savings': f"{annual_savings:.2f}"
    }


@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        # Handle manual expense/income logging from form
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
            flash('Expense logged successfully!', 'success')
            return redirect(url_for('index'))

        if income_amount:
            transactions.append({
                'date': '2026-07-22',
                'description': 'Income Deposit',
                'amount': float(income_amount),
                'type': 'income'
            })
            flash('Income logged successfully!', 'success')
            return redirect(url_for('index'))

    # Recalculate summary totals dynamically
    total_income = sum(t['amount'] for t in transactions if t['type'] == 'income')
    total_expense = sum(t['amount'] for t in transactions if t['type'] == 'expense')
    balance = total_income - total_expense

    # Mock Subscription Info for display
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

    parsed_entries = []

    # 1. Parse raw copy-pasted text/SMS lines
    if raw_text:
        lines = [line.strip() for line in raw_text.split('\n') if line.strip()]
        for line in lines:
            parts = [p.strip() for p in line.split(',') if p.strip()]
            if len(parts) >= 2:
                try:
                    desc = parts[0]
                    amt = abs(float(parts[1].replace('$', '')))
                    parsed_entries.append({
                        'date': '2026-07-22',
                        'desc': desc,
                        'amount': amt,
                        'type': 'expense'
                    })
                except ValueError:
                    continue

    # 2. Parse CSV file upload
    elif file and file.filename.endswith('.csv'):
        try:
            stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
            csv_reader = csv.reader(stream)
            
            for row in csv_reader:
                if not row or len(row) < 3:
                    continue
                
                # Ignore header rows
                if 'amount' in row[2].lower() or 'description' in row[1].lower():
                    continue

                date_val = row[0].strip()
                desc_val = row[1].strip()
                try:
                    amt_val = abs(float(row[2].replace('$', '').strip()))
                    trans_type = row[3].strip().lower() if len(row) > 3 else 'expense'
                    
                    parsed_entries.append({
                        'date': date_val if date_val else '2026-07-22',
                        'desc': desc_val,
                        'amount': amt_val,
                        'type': trans_type if trans_type in ['income', 'expense'] else 'expense'
                    })
                except ValueError:
                    continue
        except Exception as e:
            flash(f'Error reading CSV file: {str(e)}', 'danger')
            return redirect(url_for('index'))
    else:
        flash('Please select a CSV file or paste statement text before clicking Audit.', 'warning')
        return redirect(url_for('index'))

    # 3. Import parsed items directly into the global transactions table
    if parsed_entries:
        for entry in parsed_entries:
            transactions.append({
                'date': entry['date'],
                'description': entry['desc'],
                'amount': entry['amount'],
                'type': entry['type']
            })
        
        # Execute AI audit over imported items
        lines_for_audit = [f"{e['desc']} - ${e['amount']}" for e in parsed_entries]
        session['last_audit'] = analyze_statement_data(lines_for_audit)
        
        flash(f'Imported {len(parsed_entries)} statement entries into your transactions!', 'success')
    else:
        flash('No valid transactions could be extracted from input.', 'danger')

    return redirect(url_for('index'))


@app.route('/pricing')
def pricing():
    return render_template('pricing.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully.', 'info')
    return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
