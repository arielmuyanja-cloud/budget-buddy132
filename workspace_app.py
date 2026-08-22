from app import app
from workspace import register_workspace
from profit_simulator import register_profit_simulator
from decision_workspace_redirect import register_dashboard_redirect

register_workspace(app)
register_profit_simulator(app)
register_dashboard_redirect(app)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
