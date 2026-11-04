"""SQLAlchemy infrastructure. Importing this module never opens a connection."""
from sqlalchemy import MetaData, create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })


def create_db_engine(database_url: str, **kwargs) -> Engine:
    url = make_url(database_url)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    if url.drivername != "postgresql+psycopg":
        raise ValueError("Market data persistence requires PostgreSQL with psycopg")
    connect_args = {"connect_timeout": 3, **kwargs.pop("connect_args", {})}
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args, **kwargs)
