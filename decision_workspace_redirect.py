from flask import request, redirect, url_for


def register_dashboard_redirect(app):
    """Make the transaction-backed Decision Workspace the default dashboard.

    The existing financial dashboard remains available with ?view=classic.
    """
    @app.before_request
    def _route_dashboard_to_workspace():
        if request.endpoint == "dashboard" and request.args.get("view") != "classic":
            return redirect(url_for("workspace.workspace_home"))
        return None
