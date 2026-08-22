from app import app
from workspace import register_workspace

register_workspace(app)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
