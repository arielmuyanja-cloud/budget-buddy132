import os
import io
import csv
import json

from flask import Flask, request, jsonify, render_template

import stripe


app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "change-this-secret"
)


# -----------------------------
# STRIPE
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
# TIERS
# -----------------------------

TIER_LIMITS = {

    "FREE": 3,

    "PAID": 50

}



# -----------------------------
# DATABASE
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
            "tier": "FREE"
        }
    )




def upgrade_user(email):

    users = load_users()


    users[email] = {

        "tier": "PAID"

    }


    save_users(users)




# -----------------------------
# PAGES
# -----------------------------


@app.route("/")
def home():

    return render_template(
        "index.html"
    )




@app.route("/pricing")
def pricing():

    return "Pricing"





# -----------------------------
# UPLOAD AUDIT
# -----------------------------


@app.route(
    "/upload-statements",
    methods=["POST"]
)

def upload_statements():


    email = request.form.get(
        "email"
    )


    agency = request.form.get(
        "agency_name"
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

            "error":
            "Email required"

        }),400




    if not files and not pasted_csv:

        return jsonify({

            "error":
            "Upload CSV files or paste CSV data"

        }),400




    user = get_user(email)


    tier = user["tier"]


    limit = TIER_LIMITS[tier]



    # Count all inputs

    total_inputs = len(
        [
            f for f in files
            if f.filename
        ]
    )


    if pasted_csv:

        total_inputs += 1




    if total_inputs > limit:


        return jsonify({

            "error":
            "Upload limit exceeded",


            "tier":
            tier,


            "allowed":
            limit,


            "message":
            "Upgrade to process up to 50 statements"

        }),403





    results = []


    total_transactions = 0





    # -----------------------------
    # PROCESS UPLOADED FILES
    # -----------------------------


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


        rows = list(reader)


        transactions = len(rows)



        total_transactions += transactions



        results.append({

            "source":
            file.filename,


            "transactions":
            transactions

        })






    # -----------------------------
    # PROCESS PASTED CSV
    # -----------------------------


    if pasted_csv:


        stream = io.StringIO(
            pasted_csv
        )


        reader = csv.DictReader(
            stream
        )


        rows = list(reader)


        transactions = len(rows)



        total_transactions += transactions




        results.append({

            "source":
            "pasted_csv",


            "transactions":
            transactions

        })






    estimated_savings = min(

        max(
            total_transactions * 5,
            1200
        ),

        3500

    )





    # -----------------------------
    # FREE REPORT
    # -----------------------------


    if tier == "FREE":


        return jsonify({

            "status":
            "success",


            "tier":
            "FREE",


            "agency":
            agency,


            "files_scanned":
            total_inputs,


            "transactions_found":
            total_transactions,



            "teaser_report":{


                "estimated_savings":

                f"${estimated_savings:,}",



                "possible_leaks":[

                    "Unused software seats",

                    "Duplicate SaaS subscriptions",

                    "Old employee accounts",

                    "Hidden renewals"

                ]

            },



            "message":

            "Upgrade to unlock the complete 48-hour audit"

        })







    # -----------------------------
    # PAID REPORT
    # -----------------------------


    return jsonify({

        "status":
        "success",


        "tier":
        "PAID",


        "agency":
        agency,


        "statements_processed":
        total_inputs,


        "transactions":
        total_transactions,


        "audit_results":
        results,


        "delivery":

        "Full audit delivered within 48 hours"

    })







# -----------------------------
# STRIPE WEBHOOK
# -----------------------------


@app.route(
    "/stripe-webhook",
    methods=["POST"]
)

def stripe_webhook():


    payload = request.data


    signature = request.headers.get(
        "Stripe-Signature"
    )



    try:

        event = stripe.Webhook.construct_event(

            payload,

            signature,

            STRIPE_WEBHOOK_SECRET

        )


    except Exception:

        return "Invalid webhook",400





    if event["type"] == "payment_intent.succeeded":


        payment = event["data"]["object"]



        email = payment.get(

            "metadata",

            {}

        ).get(

            "email"

        )



        if email:

            upgrade_user(email)


            print(
                f"{email} upgraded to PAID"
            )





    return jsonify({

        "success":True

    })







if __name__ == "__main__":


    app.run(

        host="0.0.0.0",

        port=5000,

        debug=True

    )
