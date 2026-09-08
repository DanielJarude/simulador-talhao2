"""
Engine e sessão SQLAlchemy. A URL vem de `config.settings` (12-factor),
em vez de um caminho relative hardcoded.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from config import settings

_connect_args = {}
if settings.database_url.startswith("sqlite"):
    # Necessário porque o FastAPI pode usar a sessão em threads distintas
    _connect_args["check_same_thread"] = False

engine = create_engine(settings.database_url, connect_args=_connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """Dependência do FastAPI: uma sessão por request, sempre encerrada."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
