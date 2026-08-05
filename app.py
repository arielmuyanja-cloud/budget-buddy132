import os
import io
import csv
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
import stripe

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "your-secret-key")

# Configure Stripe Keys
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "sk_test_12345")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "whsec_12345")

# Define Tier Limits
TIER_LIMITS = {
    'FREE': 3,     # Limit: 3 CSV/statement files max for free tier
    'PAID': 50     # Limit: Up to 50 statements for paid tier ($600)
}

def get_user_tier(user_id):
    """
    Fetch user tier from your database.
    Replace this helper with your actual database query (e.g., SQLite / PostgreSQL).
    """
    # Example mock check:
    # return db.query("SELECT tier FROM users WHERE id = %s", user_id)
    return session.get('user_tier', 'FREE')

@app.route('/upload-statements', methods=['POST'])
def upload_statements():
    user_id = session.get('user_id')
    user_tier = get_user_tier(user_id)
    allowed_limit = TIER_LIMITS.get(user_tier, 3)

    if 'statements' not in request.files:
        return jsonify({'error': 'No statement files selected.'}), 400

    files = request.files.getlist('statements')

    if not files or files[0].filename == '':
        return jsonify({'error': 'No valid files uploaded.'}), 400

    # Enforce Tier File Upload Limits
    if len(files) > allowed_limit:
        return jsonify({
            'error': 'Upload limit exceeded.',
            'message': f'Your current {user_tier} tier permits up to {allowed_limit} files. You attempted to upload {len(files)} files.',
            'tier': user_tier,
            'upgrade_required': True
        }), 403

    processed_results = []
    
    # Process uploaded CSV / Statement files
    for file in files:
        if file.filename.endswith('.csv'):
            stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
            csv_input = csv.reader(stream)
            # Basic parsing logic
            row_count = sum(1 for row in csv_input)
            processed_results.append({'filename': file.filename, 'rows': row_count})

    if user_tier == 'FREE':
        return jsonify({
            'status': 'success',
            'tier': 'FREE',
            'processed_count': len(files),
            'teaser_summary': {
                'estimated_savings': '$1,200 - $3,500 / year',
                'detected_leaks': ['Unused software seats', 'Duplicate SaaS subscriptions']
            },
            'message': 'Free teaser scan complete. Upgrade to process up to 50 statements and unlock complete audit details.'
        }), 200

    # Paid Tier Response
    return jsonify({
        'status': 'success',
        'tier': 'PAID',
        'processed_count': len(files),
        'audit_results': processed_results,
        'message': 'Deep 48-hour audit scan initiated successfully.'
    }), 200

@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError as e:
        return 'Invalid payload', 400
    except stripe.error.SignatureVerificationError as e:
        return 'Invalid signature', 400

    # Handle successful payment confirmation
    if event['type'] == 'payment_intent.succeeded':
        payment_intent = event['data']['object']
        user_id = payment_intent.get('metadata', {}).get('user_id')
        
        if user_id:
            # Upgrade user status in session or DB
            # db.execute("UPDATE users SET tier = 'PAID' WHERE id = %s", user_id)
            session['user_tier'] = 'PAID'
            print(f"User {user_id} upgraded to PAID tier. Upload limit expanded to 50 files.")

    return jsonify({'status': 'success'}), 200
