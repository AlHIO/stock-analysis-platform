from contextlib import asynccontextmanager

from fastapi import FastAPI

from stockapp.config import Settings
from stockapp.modules.health.api.routes import router as health_router
from stockapp.modules.market_data.api.routes import install_error_handlers, router as market_router
from stockapp.modules.technical.api.routes import router as technical_router
from stockapp.modules.institutional.api.routes import (
    router as institutional_router, install_error_handlers as install_institutional_errors,
)
from stockapp.modules.ownership.api.routes import router as ownership_router, install_error_handlers as install_ownership_errors
from stockapp.modules.margin.api.routes import router as margin_router, install_error_handlers as install_margin_errors
from stockapp.modules.lending.api.routes import router as lending_router, install_error_handlers as install_lending_errors
from stockapp.modules.chip_analysis.api.routes import router as chip_router, install_error_handlers as install_chip_errors
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
    app.include_router(institutional_router)
    app.include_router(ownership_router)
    app.include_router(margin_router)
    app.include_router(lending_router)
    app.include_router(chip_router)
    install_error_handlers(app)
    install_institutional_errors(app)
    install_ownership_errors(app)
    install_margin_errors(app)
    install_lending_errors(app)
    install_chip_errors(app)
    return app


app = create_app()
