import os
import io
import csv
import json

from flask import (
    Flask,
    request,
    jsonify,
    render_template
)

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
# TIERS
# -----------------------------

TIER_LIMITS = {

    "FREE":3,

    "PAID":50

}



# -----------------------------
# SIMPLE USER STORAGE
# -----------------------------

DATABASE="users.json"



def load_users():

    if not os.path.exists(DATABASE):

        return {}

    with open(DATABASE,"r") as f:

        return json.load(f)




def save_users(users):

    with open(DATABASE,"w") as f:

        json.dump(
            users,
            f,
            indent=4
        )




def get_user(email):

    users=load_users()

    return users.get(
        email,
        {
            "tier":"FREE"
        }
    )




def upgrade_user(email):

    users=load_users()

    users[email]={

        "tier":"PAID"

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

    return "Pricing page"





# -----------------------------
# CSV UPLOAD
# -----------------------------


@app.route(
    "/upload-statements",
    methods=["POST"]
)

def upload_statements():


    email=request.form.get(
        "email"
    )


    agency=request.form.get(
        "agency_name"
    )



    if not email:

        return jsonify({

            "error":"Email required"

        }),400




    files=request.files.getlist(
        "statements"
    )



    if not files:

        return jsonify({

            "error":"No files uploaded"

        }),400




    user=get_user(email)


    tier=user["tier"]


    limit=TIER_LIMITS[tier]



    if len(files)>limit:

        return jsonify({

            "error":"Upload limit exceeded",

            "current_tier":tier,

            "allowed":limit,

            "message":
            "Upgrade to unlock 50 statements"

        }),403





    results=[]

    total_transactions=0



    for file in files:


        if not file.filename.endswith(".csv"):

            continue



        content=file.stream.read().decode(
            "utf-8",
            errors="ignore"
        )


        stream=io.StringIO(
            content
        )


        reader=csv.DictReader(
            stream
        )


        rows=list(reader)


        transactions=len(rows)


        total_transactions+=transactions



        results.append({

            "filename":
            file.filename,

            "transactions":
            transactions

        })





    estimated_savings=min(

        max(
            total_transactions*5,
            1200
        ),

        3500

    )





    # FREE USER REPORT

    if tier=="FREE":


        return jsonify({

            "status":"success",

            "tier":"FREE",

            "agency":agency,

            "files_scanned":
            len(files),

            "transactions":
            total_transactions,


            "teaser":{

                "potential_savings":
                f"${estimated_savings:,}",


                "issues_found":[

                    "Unused SaaS seats",

                    "Duplicate subscriptions",

                    "Unused licenses",

                    "Hidden price increases"

                ]

            },


            "message":
            "Upgrade for the complete 48-hour SaaS Leak Audit"

        })






    # PAID USER REPORT


    return jsonify({

        "status":"success",

        "tier":"PAID",

        "agency":agency,

        "files_scanned":
        len(files),

        "transactions":
        total_transactions,

        "audit_results":
        results,

        "delivery":
        "48 hour full audit"

    })






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


            print(
                f"{email} upgraded"
            )





    return jsonify({

        "received":True

    })





if __name__=="__main__":

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )
