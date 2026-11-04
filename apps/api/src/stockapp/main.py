from fastapi import FastAPI
from stockapp.modules.health.api.routes import router


def create_app() -> FastAPI:
    app = FastAPI(title="Stock Analysis Platform", version="0.1.0")
    app.include_router(router)
    return app


app = create_app()
