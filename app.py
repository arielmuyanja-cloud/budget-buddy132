import os
import io
import csv
import json

from flask import Flask, request, render_template, jsonify

import stripe


app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "change-this-secret"
)



# -----------------------------
# STRIPE CONFIG
# -----------------------------

stripe.api_key = os.environ.get(
    "STRIPE_SECRET_KEY",
    "sk_test_xxxxx"
)

STRIPE_WEBHOOK_SECRET = os.environ.get(
    "STRIPE_WEBHOOK_SECRET",
    "whsec_xxxxx"
)



# -----------------------------
# USER TIERS
# -----------------------------

TIER_LIMITS = {

    "FREE": 3,

    "PAID": 50

}



# -----------------------------
# SIMPLE DATABASE
# -----------------------------

DATABASE = "users.json"



def load_users():

    if not os.path.exists(DATABASE):

        return {}

    with open(DATABASE, "r") as f:

        return json.load(f)




def save_users(users):

    with open(DATABASE, "w") as f:

        json.dump(
            users,
            f,
            indent=4
        )




def get_user(email):

    users = load_users()

    return users.get(
        email,
        {
            "tier":"FREE"
        }
    )




def upgrade_user(email):

    users = load_users()

    users[email] = {

        "tier":"PAID"

    }

    save_users(users)





# -----------------------------
# HOME
# -----------------------------


@app.route("/")
def home():

    return render_template(
        "index.html"
    )





# -----------------------------
# UPLOAD + AUDIT
# -----------------------------


@app.route(
    "/upload-statements",
    methods=["POST"]
)

def upload_statements():


    agency = request.form.get(
        "agency_name"
    )


    email = request.form.get(
        "email"
    )


    files = request.files.getlist(
        "statements"
    )


    pasted_csv = request.form.get(
        "pasted_csv",
        ""
    ).strip()





    if not email:

        return jsonify({

            "error":"Email required"

        }),400





    if not files and not pasted_csv:

        return jsonify({

            "error":
            "Upload a CSV or paste CSV data"

        }),400





    user = get_user(email)


    tier = user["tier"]


    limit = TIER_LIMITS[tier]





    inputs = len(
        [
            f for f in files
            if f.filename
        ]
    )


    if pasted_csv:

        inputs += 1





    if inputs > limit:


        return jsonify({

            "error":
            "Upload limit exceeded",

            "allowed":
            limit,

            "tier":
            tier

        }),403





    results=[]

    total_transactions=0





    # PROCESS FILE UPLOADS

    for file in files:


        if not file.filename:

            continue



        if not file.filename.lower().endswith(".csv"):

            continue



        content = file.stream.read().decode(

            "utf-8",

            errors="ignore"

        )


        stream = io.StringIO(
            content
        )


        reader = csv.DictReader(
            stream
        )


        rows=list(reader)



        count=len(rows)



        total_transactions += count



        results.append({

            "name":
            file.filename,

            "transactions":
            count

        })






    # PROCESS PASTED CSV


    if pasted_csv:


        stream=io.StringIO(
            pasted_csv
        )


        reader=csv.DictReader(
            stream
        )


        rows=list(reader)



        count=len(rows)



        total_transactions += count



        results.append({

            "name":
            "pasted_csv",

            "transactions":
            count

        })






    # SAVINGS ESTIMATE

    estimated_savings=max(

        1200,

        total_transactions * 100

    )


    estimated_savings=min(

        estimated_savings,

        3500

    )





    # FREE USER


    if tier=="FREE":


        return render_template(

            "teaser.html",

            agency=agency,

            files=inputs,

            transactions=total_transactions,

            savings=f"${estimated_savings:,}",

            leaks=[

                "Unused SaaS seats",

                "Duplicate subscriptions",

                "Old employee accounts",

                "Hidden renewals"

            ]

        )






    # PAID USER


    return render_template(

        "paid_report.html",

        agency=agency,

        results=results,

        transactions=total_transactions

    )








# -----------------------------
# STRIPE WEBHOOK
# -----------------------------


@app.route(
    "/stripe-webhook",
    methods=["POST"]
)

def stripe_webhook():


    payload=request.data

    signature=request.headers.get(
        "Stripe-Signature"
    )


    try:

        event=stripe.Webhook.construct_event(

            payload,

            signature,

            STRIPE_WEBHOOK_SECRET

        )


    except Exception:

        return "Invalid webhook",400





    if event["type"]=="payment_intent.succeeded":


        payment=event["data"]["object"]



        email=payment.get(
            "metadata",
            {}
        ).get(
            "email"
        )



        if email:

            upgrade_user(email)



    return jsonify({

        "success":True

    })





if __name__=="__main__":

    app.run(

        host="0.0.0.0",

        port=5000,

        debug=True

    )
