# Entry point kept for compatibility with the existing Render start command:
#   gunicorn --config gunicorn.conf.py workspace_app:app ...
#
# All optional modules (workspace, profit simulator, Relworx, NOWPayments) are
# now registered inside app.py itself, so importing `app` is enough.
from app import app  # noqa: F401

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
