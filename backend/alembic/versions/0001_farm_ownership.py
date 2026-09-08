"""farm ownership (owner_id + is_shared) with safe backfill

Adiciona o vínculo User -> Farm (PR #3): `farms.owner_id` e `farms.is_shared`
(novo booleano de "farm vitrine" compartilhada). Em seguida faz o backfill
SEGURO de bancos já existentes:

  * toda fazenda sem dono (owner_id NULL — banco legado) passa a pertencer ao
    usuário admin de demonstração (admin@orion.com; se não existir, ao 1º
    usuário da base);
  * a fazenda de demonstração (id=1) é marcada como compartilhada
    (is_shared=1) para funcionar como vitrine legível por qualquer usuário
    autenticado.

A migração NUNCA apaga o banco. SQLite 3.37+ aceita ALTER TABLE ADD COLUMN
diretamente (atômico, preserva dados). Em versões anteriores, o app aplica o
fallback em `database.ensure_owner_columns()` no boot. Para garantir o commit
em qualquer versão do Alembic/SQLite, a migração abre SUA PRÓPRIA conexão
(autocommit) em vez de depender da transação do Alembic.

Revision ID: 0001_farm_ownership
Revises:
Create Date: 2026-09-08 00:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import create_engine, text


# revision identifiers, used by Alembic.
revision: str = "0001_farm_ownership"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _own_connection():
    """Conexão própria (autocommit) apontando para o mesmo DATABASE_URL.

    Evita depende da transação que o Alembic abre em torno do upgrade() —
    alguns ambientes não persistem DDL/backfill feitos nessa conexão.
    """
    url = op.get_bind().engine.url  # mesma URL do settings.database_url
    eng = create_engine(str(url), connect_args={"isolation_level": None} if str(url).startswith("sqlite") else {})
    return eng


def upgrade() -> None:
    eng = _own_connection()
    with eng.connect() as conn:
        insp = sa.inspect(conn)
        if not insp.has_table("farms"):
            return  # tabela será criada pelo create_all
        cols = {c["name"] for c in insp.get_columns("farms")}

        if "owner_id" not in cols:
            conn.execute(text("ALTER TABLE farms ADD COLUMN owner_id INTEGER"))
        if "is_shared" not in cols:
            conn.execute(text("ALTER TABLE farms ADD COLUMN is_shared BOOLEAN NOT NULL DEFAULT 0"))
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_farms_owner_id ON farms (owner_id)"))
        except Exception:
            pass

        # ----- Backfill seguro -----
        admin_row = conn.execute(
            text("SELECT id FROM users WHERE email = :e LIMIT 1"),
            {"e": "admin@orion.com"},
        ).fetchone()
        admin_id = admin_row[0] if admin_row else None
        if admin_id is None:
            first = conn.execute(text("SELECT id FROM users ORDER BY id LIMIT 1")).fetchone()
            admin_id = first[0] if first else None

        if admin_id is not None:
            conn.execute(
                text("UPDATE farms SET owner_id = :a WHERE owner_id IS NULL"),
                {"a": admin_id},
            )
        conn.execute(text("UPDATE farms SET is_shared = 1 WHERE id = 1"))
    eng.dispose()


def downgrade() -> None:
    eng = _own_connection()
    with eng.connect() as conn:
        insp = sa.inspect(conn)
        if not insp.has_table("farms"):
            return
        cols = {c["name"] for c in insp.get_columns("farms")}
        if "owner_id" in cols:
            conn.execute(text("ALTER TABLE farms DROP COLUMN owner_id"))
        if "is_shared" in cols:
            conn.execute(text("ALTER TABLE farms DROP COLUMN is_shared"))
    eng.dispose()
