"""Explicit migrations; never invoked by API startup."""
from alembic import context
from stockapp.config import Settings
from stockapp.shared.infrastructure.database import Base, create_db_engine
from stockapp.modules.market_data.infrastructure import history_models, ingestion_models, models  # noqa: F401
from stockapp.modules.institutional.infrastructure import models as institutional_models  # noqa: F401
from stockapp.modules.institutional.infrastructure import ingestion_models as institutional_ingestion_models  # noqa: F401


from stockapp.modules.ownership.infrastructure import models as ownership_models  # noqa: F401
from stockapp.modules.margin.infrastructure import models as margin_models  # noqa: F401
from stockapp.modules.lending.infrastructure import models as lending_models  # noqa: F401


def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    engine = create_db_engine(Settings().database_url)
    try:
        context.configure(url=engine.url, target_metadata=Base.metadata, literal_binds=True,
                          dialect_opts={"paramstyle": "named"})
        with context.begin_transaction():
            context.run_migrations()
    finally:
        engine.dispose()
else:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        migrate(connection)
    else:
        engine = create_db_engine(Settings().database_url)
        try:
            with engine.connect() as connection:
                migrate(connection)
        finally:
            engine.dispose()
