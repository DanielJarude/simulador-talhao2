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

No SQLite, a tabela `farms` é reconstruída pelo Alembic batch mode somente
quando necessário. A reconstrução preserva dados, índices e as referências de
`talhoes.farm_id`, e cria a FK física `farms.owner_id -> users.id`.
`is_shared` recebe um DEFAULT 0 temporário para a cópia das linhas existentes;
após o backfill, uma segunda etapa remove o default físico para que o schema
final seja equivalente ao produzido por `Base.metadata.create_all()`.

A migration usa exclusivamente a conexão administrada pelo Alembic. O controle
transacional do SQLite fica em `alembic/env.py`; não há conexão AUTOCOMMIT
paralela.

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


_FARM_OWNER_FK = ("users", ("owner_id",), ("id",))


def _index_matches(index: dict, columns: tuple[str, ...]) -> bool:
    return tuple(index.get("column_names") or ()) == columns


def _owner_foreign_key_matches(foreign_key: dict) -> bool:
    return (
        foreign_key.get("referred_table") == _FARM_OWNER_FK[0]
        and tuple(foreign_key.get("constrained_columns") or ()) == _FARM_OWNER_FK[1]
        and tuple(foreign_key.get("referred_columns") or ()) == _FARM_OWNER_FK[2]
    )


def _validate_existing_owner_values(bind: sa.Connection, inspector: sa.Inspector) -> None:
    """Fail before a table rebuild if an existing owner points nowhere."""
    if not inspector.has_table("users"):
        raise RuntimeError(
            "Não é possível criar farms.owner_id -> users.id: tabela users ausente."
        )

    invalid = bind.execute(
        sa.text(
            """
            SELECT f.id, f.owner_id
              FROM farms AS f
              LEFT JOIN users AS u ON u.id = f.owner_id
             WHERE f.owner_id IS NOT NULL
               AND u.id IS NULL
             LIMIT 1
            """
        )
    ).first()
    if invalid is not None:
        raise RuntimeError(
            "Não é possível criar a FK farms.owner_id -> users.id: "
            f"farm id={invalid[0]} possui owner_id inexistente ({invalid[1]})."
        )


def _backfill(bind: sa.Connection) -> None:
    """Apply the ownership rules without overwriting explicit owners."""
    admin_row = bind.execute(
        sa.text("SELECT id FROM users WHERE email = :email LIMIT 1"),
        {"email": "admin@orion.com"},
    ).first()
    admin_id = admin_row[0] if admin_row else None

    if admin_id is None:
        first_user = bind.execute(
            sa.text("SELECT id FROM users ORDER BY id LIMIT 1")
        ).first()
        admin_id = first_user[0] if first_user else None

    if admin_id is not None:
        bind.execute(
            sa.text("UPDATE farms SET owner_id = :owner_id WHERE owner_id IS NULL"),
            {"owner_id": admin_id},
        )

    # Só a fazenda demo é compartilhada por padrão. Não altera as demais.
    bind.execute(sa.text("UPDATE farms SET is_shared = 1 WHERE id = 1"))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("farms"):
        return  # tabela será criada pelo create_all; nada a migrar

    columns = {column["name"]: column for column in inspector.get_columns("farms")}
    indexes = {index["name"]: index for index in inspector.get_indexes("farms")}
    foreign_keys = inspector.get_foreign_keys("farms")

    owner_column = columns.get("owner_id")
    shared_column = columns.get("is_shared")

    # Se a coluna já foi criada por uma tentativa anterior, não permitimos que
    # um batch copie dados inválidos para uma constraint nova.
    if owner_column is not None:
        _validate_existing_owner_values(bind, inspector)
    elif not inspector.has_table("users"):
        raise RuntimeError(
            "Não é possível migrar farms sem a tabela users necessária para owner_id."
        )

    owner_foreign_keys = [
        foreign_key
        for foreign_key in foreign_keys
        if tuple(foreign_key.get("constrained_columns") or ()) == ("owner_id",)
    ]
    matching_owner_fk = any(
        _owner_foreign_key_matches(foreign_key)
        for foreign_key in owner_foreign_keys
    )
    incompatible_owner_fk = [
        foreign_key
        for foreign_key in owner_foreign_keys
        if not _owner_foreign_key_matches(foreign_key)
    ]
    if incompatible_owner_fk:
        raise RuntimeError(
            "A tabela farms já possui uma FK incompatível em owner_id; "
            "migration interrompida antes da reconstrução."
        )

    shared_default = shared_column.get("default") if shared_column else None
    remove_shared_default = shared_column is None or shared_default is not None

    # O batch é necessário para adicionar a FK no SQLite. Mesmo quando só
    # falta um índice, fazer a operação no mesmo rebuild mantém a definição da
    # tabela e os índices coerentes.
    needs_owner_index = not _index_matches(
        indexes.get("ix_farms_owner_id", {}), ("owner_id",)
    )
    needs_id_index = not _index_matches(indexes.get("ix_farms_id", {}), ("id",))
    needs_shared_type = (
        shared_column is not None
        and str(shared_column["type"]).upper() != "BOOLEAN"
    )
    needs_shared_nullable_fix = (
        shared_column is not None and bool(shared_column.get("nullable", True))
    )
    needs_owner_nullable_fix = (
        owner_column is not None and not bool(owner_column.get("nullable", True))
    )

    needs_batch = any(
        (
            owner_column is None,
            shared_column is None,
            not matching_owner_fk,
            needs_owner_index,
            needs_id_index,
            needs_shared_type,
            needs_shared_nullable_fix,
            needs_owner_nullable_fix,
        )
    )

    if shared_column is not None and needs_shared_nullable_fix:
        # Remove NULLs before imposing NOT NULL in the table copy.
        bind.execute(sa.text("UPDATE farms SET is_shared = 0 WHERE is_shared IS NULL"))

    if needs_batch:
        with op.batch_alter_table("farms", recreate="always") as batch:
            if owner_column is None:
                batch.add_column(sa.Column("owner_id", sa.Integer(), nullable=True))
            elif needs_owner_nullable_fix:
                batch.alter_column(
                    "owner_id",
                    existing_type=owner_column["type"],
                    nullable=True,
                )

            if shared_column is None:
                # O default é deliberadamente temporário. Ele é removido em
                # outro batch, depois da cópia e do backfill.
                batch.add_column(
                    sa.Column(
                        "is_shared",
                        sa.Boolean(),
                        nullable=False,
                        server_default=sa.text("0"),
                    )
                )
            else:
                if needs_shared_type or needs_shared_nullable_fix:
                    batch.alter_column(
                        "is_shared",
                        existing_type=shared_column["type"],
                        type_=sa.Boolean(),
                        nullable=False,
                    )

            if not matching_owner_fk:
                # O batch do Alembic exige um nome para a constraint. A
                # comparação de paridade usa a definição da FK (não o nome),
                # pois create_all() deixa essa constraint sem nome no SQLite.
                batch.create_foreign_key(
                    "fk_farms_owner_id_users",
                    "users",
                    ["owner_id"],
                    ["id"],
                )

            if needs_owner_index:
                existing_owner_index = indexes.get("ix_farms_owner_id")
                if existing_owner_index is not None:
                    batch.drop_index("ix_farms_owner_id")
                batch.create_index("ix_farms_owner_id", ["owner_id"])

            if needs_id_index:
                existing_id_index = indexes.get("ix_farms_id")
                if existing_id_index is not None:
                    batch.drop_index("ix_farms_id")
                batch.create_index("ix_farms_id", ["id"])

    # Backfill ocorre antes de remover o default temporário. owner_id é
    # nullable, portanto a FK já pode existir durante esta etapa.
    _backfill(bind)

    if remove_shared_default:
        # Não combinar esta operação com add_column no mesmo batch: isso faria
        # a cópia tentar inserir NULL em is_shared NOT NULL antes de o default
        # ser aplicado em alguns SQLite/SQLAlchemy.
        with op.batch_alter_table("farms", recreate="always") as batch:
            batch.alter_column(
                "is_shared",
                existing_type=sa.Boolean(),
                server_default=None,
                nullable=False,
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("farms"):
        return

    columns = {column["name"] for column in inspector.get_columns("farms")}
    indexes = {index["name"] for index in inspector.get_indexes("farms")}
    if not {"owner_id", "is_shared"}.intersection(columns):
        return

    with op.batch_alter_table("farms", recreate="always") as batch:
        if "ix_farms_owner_id" in indexes:
            batch.drop_index("ix_farms_owner_id")
        if "owner_id" in columns:
            batch.drop_column("owner_id")
        if "is_shared" in columns:
            batch.drop_column("is_shared")
