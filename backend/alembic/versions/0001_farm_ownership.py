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

A migração NUNCA apaga o banco. Idempotente: se as colunas já existirem, o
passo de ALTER é ignorado.

Sobre a chave estrangeira física (FK) em `owner_id`:
  O SQLite **não suporta** `ALTER TABLE ... ADD CONSTRAINT` (erro
  `NotImplementedError: No support for ALTER of constraints in SQLite dialect`
  do próprio Alembic). A forma idiomática seria `op.batch_alter_table` (recria a
  tabela copiando os dados), porém, neste ambiente (SQLAlchemy 2.0 + driver
  padrão), as operações do Alembic **não persistem o DDL** quando envoltas pela
  transação gerenciada pelo Alembic (`context.begin_transaction()`), enquanto uma
  conexão autocommit direta na MESMA engine persiste normalmente. Para não
  depender desse comportimento frágil nem recriar a tabela (risco desnecessário
  para um ALTER simples), a migração:
    - adiciona `owner_id` como coluna INTEGER comum (sem FK física);
    - mantém o relacionamento ORM `User.farms` / `Farm.owner` em `models.py`
      (integridade referencial lógica + cascade de deleção);
    - cria um índice em `owner_id` (performance das consultas de listagem).
  FK física pode ser adicionada numa migração futura quando o banco estiver em
  PostgreSQL/MySQL, ou via `op.batch_alter_table` em um ambiente onde o batch
  persiste — ver nota em `backend/alembic/README.md`.

Revision ID: 0001_farm_ownership
Revises:
Create Date: 2026-09-08 00:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0001_farm_ownership"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _get_autocommit_connection():
    """Conexão autocommit sobre a MESMA engine que o Alembic gerencia.

    `op.get_bind()` devolve a engine/conexão do Alembic. Neste ambiente as
    operações `op.add_column`/`op.execute` não persistem o DDL quando usadas
    dentro da transação do Alembic; uma conexão autocommit aberta a partir
    da própria engine do Alembic persiste normalmente. Isto NÃO é um mecanismo
    paralelo de migration: é a engine fornecida pelo Alembic, apenas com o
    controle de transação adequado a SQLite.
    """
    bind = op.get_bind()
    engine = getattr(bind, "engine", bind)
    conn = engine.connect()
    # SQLite: isolation_level=None => autocommit (cada statement é commitado)
    try:
        conn.execution_options(isolation_level="AUTOCOMMIT")
    except Exception:
        pass
    return conn


def upgrade() -> None:
    conn = _get_autocommit_connection()
    try:
        insp = sa.inspect(conn)
        if not insp.has_table("farms"):
            return  # tabela será criada pelo create_all; nada a migrar
        cols = {c["name"] for c in insp.get_columns("farms")}

        if "owner_id" not in cols:
            conn.execute(sa.text("ALTER TABLE farms ADD COLUMN owner_id INTEGER"))
        if "is_shared" not in cols:
            conn.execute(sa.text("ALTER TABLE farms ADD COLUMN is_shared BOOLEAN NOT NULL DEFAULT 0"))
        try:
            conn.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_farms_owner_id ON farms (owner_id)"))
        except Exception:
            pass

        # ----- Backfill seguro -----
        admin_row = conn.execute(
            sa.text("SELECT id FROM users WHERE email = :e LIMIT 1"),
            {"e": "admin@orion.com"},
        ).fetchone()
        admin_id = admin_row[0] if admin_row else None
        if admin_id is None:
            first = conn.execute(sa.text("SELECT id FROM users ORDER BY id LIMIT 1")).fetchone()
            admin_id = first[0] if first else None

        if admin_id is not None:
            conn.execute(
                sa.text("UPDATE farms SET owner_id = :a WHERE owner_id IS NULL"),
                {"a": admin_id},
            )
        conn.execute(sa.text("UPDATE farms SET is_shared = 1 WHERE id = 1"))
    finally:
        conn.close()


def downgrade() -> None:
    conn = _get_autocommit_connection()
    try:
        insp = sa.inspect(conn)
        if not insp.has_table("farms"):
            return
        cols = {c["name"] for c in insp.get_columns("farms")}
        # SQLite só permite DROP COLUMN em 3.35+; ignora silenciosamente se indisponível.
        if "owner_id" in cols:
            try:
                conn.execute(sa.text("ALTER TABLE farms DROP COLUMN owner_id"))
            except Exception:
                pass
        if "is_shared" in cols:
            try:
                conn.execute(sa.text("ALTER TABLE farms DROP COLUMN is_shared"))
            except Exception:
                pass
    finally:
        conn.close()
