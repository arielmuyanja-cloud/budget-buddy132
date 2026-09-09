from app import app
from workspace import register_workspace
from profit_simulator import register_profit_simulator
from relworx_payments import register_relworx

try:
    register_workspace(app)
except Exception as e:
    app.logger.error(f"workspace registration failed: {e}")

try:
    register_profit_simulator(app)
except Exception as e:
    app.logger.error(f"profit simulator registration failed: {e}")

try:
    register_relworx(app)
except Exception as e:
    app.logger.error(f"Relworx registration failed: {e}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
