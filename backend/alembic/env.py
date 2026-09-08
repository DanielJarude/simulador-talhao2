"""Alembic env — Orion Agro API.

Lê a URL do banco de `config.settings` (12-factor) e usa os metadados de
`database.Base` (modelos SQLAlchemy) como alvo do autogenerate. Suporta SQLite
e demais backends (offline + online). A ordem de import já garante que as
tabelas de `models.py` estejam registradas em `Base.metadata`.
"""
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# --- Preparar sys.path e importar a aplicação (models/database/config) ---
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(HERE)
sys.path.insert(0, BACKEND_DIR)

from config import settings  # noqa: E402
from database import Base  # noqa: E402
import models  # noqa: E402  (registra as tabelas em Base.metadata)

config = context.config

# Sobrepõe a URL do ini pela do .env/settings (única fonte de verdade).
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # suporta ALTER em SQLite
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        if is_sqlite:
            # Uma reconstrução SQLite em batch precisa remover a tabela antiga.
            # Como farms é referenciada por talhoes, o SQLite exige que a
            # checagem de FK esteja desligada durante a reconstrução. Fazemos
            # isso antes da transação explícita; a conexão é religada ao final
            # da migration e as conexões normais da aplicação permanecem com
            # PRAGMA foreign_keys=ON (database.py).
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()

        configure_kwargs = {
            "connection": connection,
            "target_metadata": target_metadata,
            "render_as_batch": is_sqlite,
            "compare_type": True,
            "compare_server_default": True,
        }
        if is_sqlite:
            # O SQLite recebe uma fronteira transacional real e explícita.
            # Assim DDL, DML da migration e alembic_version são commitados
            # juntos, sem conexão AUTOCOMMIT paralela.
            configure_kwargs["transactional_ddl"] = True

        context.configure(**configure_kwargs)
        with context.begin_transaction():
            context.run_migrations()

        if is_sqlite:
            # O batch foi concluído e a transação do Alembic foi commitada.
            # Reative o enforcement na própria conexão antes de fechá-la.
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
