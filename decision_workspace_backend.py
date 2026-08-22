from workspace import register_workspace


def register_decision_workspace(app):
    """Register the real transaction-backed decision workspace."""
    register_workspace(app)
