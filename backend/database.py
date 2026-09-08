"""
Engine e sessão SQLAlchemy. A URL vem de `config.settings` (12-factor),
em vez de um caminho relative hardcoded.
"""
from sqlalchemy import create_engine, inspect, text
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


def _farm_table_columns() -> set[str] | None:
    """Conjunto de colunas da tabela `farms` (None se a tabela não existir)."""
    inspector = inspect(engine)
    if not inspector.has_table("farms"):
        return None
    return {c["name"] for c in inspector.get_columns("farms")}


def ensure_owner_columns() -> None:
    """
    Garante que as colunas de ownership existem em bancos SQLite JÁ EXISTENTES.

    Bancos novos são criados completos por `Base.metadata.create_all` (no
    lifespan). Bancos legados (criados antes do PR #3) NÃO ganham as novas
    colunas via `create_all` — por isso aplicamos um ALTER idempotente aqui,
    como fallback de defesa antes do `alembic upgrade head` (ver `main.py`).

    Em produção o caminho preferido é `alembic upgrade head`; este helper é a
    rede de segurança para quem sobe a app direto sem rodar migrations.
    NUNCA apagamos nem recriamos o banco.
    """
    cols = _farm_table_columns()
    if cols is None:
        return  # tabela será criada pelo create_all no lifespan

    with engine.begin() as conn:
        if "owner_id" not in cols:
            conn.execute(text("ALTER TABLE farms ADD COLUMN owner_id INTEGER"))
        if "is_shared" not in cols:
            # SQLite não aceita DEFAULT em ADD COLUMN de forma portável antes
            # da 3.37; definimos 0 explicitamente em linhas existentes.
            conn.execute(text("ALTER TABLE farms ADD COLUMN is_shared INTEGER NOT NULL DEFAULT 0"))
            conn.execute(text("UPDATE farms SET is_shared = 0 WHERE is_shared IS NULL"))
