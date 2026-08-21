"""Gunicorn worker hooks for Budget Buddy deployment compatibility."""


def post_worker_init(worker):
    import app as budget_buddy_app
    from forecast_guard import safe_compute_forecast
    from auditor_console import register

    # Replace only the runaway legacy forecast calculation. All other Flask
    # routes and business logic remain unchanged.
    budget_buddy_app.compute_forecast = safe_compute_forecast

    # Register the new console once per worker before it begins serving.
    if "auditor_console" not in budget_buddy_app.app.view_functions:
        register(budget_buddy_app.app)
