from contextlib import asynccontextmanager

from fastapi import FastAPI

from stockapp.config import Settings
from stockapp.modules.health.api.routes import router as health_router
from stockapp.modules.market_data.api.routes import install_error_handlers, router as market_router
from stockapp.modules.technical.api.routes import router as technical_router
from stockapp.shared.infrastructure.database import create_db_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Engine creation is lazy: liveness does not require a live database. Each
    # query uses a consistent read-only snapshot; writes retain their own engine.
    engine = create_db_engine(Settings().database_url, isolation_level="REPEATABLE READ")
    app.state.market_query_engine = engine
    try:
        yield
    finally:
        engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Stock Analysis Platform", version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(market_router)
    app.include_router(technical_router)
    install_error_handlers(app)
    return app


app = create_app()
