# Budget Buddy Decision Workspace backend

The Decision Workspace is now backed by the existing Transaction table and a persistent WorkspaceDecision table.

Flow: CSV import -> transaction evidence -> discovery -> stage decision -> toggle scenario -> risk verification -> savings buckets / ledger.

Endpoints:
- GET /api/workspace/discovery
- POST /api/workspace/decision
- POST /api/workspace/decision/<id>/toggle
- POST /api/workspace/decision/<id>/risk

Risk priority: Q1/Q4 yes = HIGH; otherwise Q2/Q3 yes = MEDIUM; all no = LOW.
