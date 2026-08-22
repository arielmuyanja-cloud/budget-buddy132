# Decision Workspace

The Render entrypoint is `workspace_app:app`. Normal authenticated `/dashboard` visits redirect to `/decision-workspace`, while `/dashboard?view=classic` keeps the original financial dashboard available.

The workspace uses transaction-backed discovery and the existing saved-decision APIs for the simulator, risk checks, savings buckets, and ledger.
