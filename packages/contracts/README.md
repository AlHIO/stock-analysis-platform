# API contracts

FastAPI routes and Pydantic response models are the source of truth. The reviewed
OpenAPI snapshot is [apps/api/openapi.json](../../apps/api/openapi.json); the live
schema is served at `/openapi.json`.

From the repository root, with the API installed:

```bash
.venv/bin/python apps/api/scripts/export_openapi.py
.venv/bin/python apps/api/scripts/export_openapi.py --check
```

The snapshot is checked against runtime by Pytest. Generate it after changing
routes/schemas; do not edit it manually. Future generated TypeScript clients belong
here. Query APIs do not call providers or imply realtime prices.
