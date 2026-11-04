# Foundation decisions

- One Next.js application and one FastAPI modular monolith; PostgreSQL is durable storage, Redis is cache/future job infrastructure. No vendor integration, financial data schema, queue, scheduler or running worker in Phase 0.
- Backend module layers: API → application → domain. Infrastructure implements domain Protocols. API dependency providers are the current composition boundary; domain cannot import infrastructure.
- Shared `DataSource[T]` describes the vendor-neutral async source boundary. Future modules define concrete typed requests/results and implement adapters in infrastructure; the foundation makes no assumption about market-data providers.
- `/health` is process liveness. `/health/ready` performs bounded, parallel real dependency checks. Failure is HTTP 503 with sanitized status. Database access is async; no persistent client is created at import time.
- The Next.js server fetches backend readiness at request time. Docker internal URLs stay server-side; backend downtime produces a visible offline state instead of crashing the home page.
- PostgreSQL and Redis use named volumes, localhost-only published ports and readiness checks. This Compose setup is for local foundation validation, not a public production security configuration.
- Workers and contract generation are reserved boundaries, not empty services. Add them only with an authorized feature and operational need.

Phase 1A extends this foundation with a `market_data` domain/infrastructure module,
SQLAlchemy 2.x repositories, and explicit Alembic migrations. Canonical daily bars
and distinct normalized observations have separate tables; no raw provider data
or ingestion service is introduced. See [the database specification](../specs/phase-1a-database.md)
for transaction ownership, schema constraints and revision policy. Health checks
remain independent of schema migrations.
