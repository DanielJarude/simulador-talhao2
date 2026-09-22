"""canonical farm location metadata (idempotent for create_all and legacy DBs).
Revision ID: 0002_canonical_location
Revises: 0001_farm_ownership
"""
from alembic import op
import sqlalchemy as sa
revision='0002_canonical_location'; down_revision='0001_farm_ownership'; branch_labels=None; depends_on=None
COLUMNS={
 'state': sa.Column('state',sa.String(),nullable=True),
 'state_code': sa.Column('state_code',sa.String(),nullable=True),
 'location_source': sa.Column('location_source',sa.String(),nullable=True),
 'location_status': sa.Column('location_status',sa.String(),nullable=True),
 'location_divergence_km': sa.Column('location_divergence_km',sa.Float(),nullable=True),
}
def upgrade():
    existing={c['name'] for c in sa.inspect(op.get_bind()).get_columns('farms')}
    for name,column in COLUMNS.items():
        if name not in existing:
            with op.batch_alter_table('farms') as b: b.add_column(column)
    bind=op.get_bind()
    bind.execute(sa.text("UPDATE farms SET location_source='legacy' WHERE location_source IS NULL"))
    bind.execute(sa.text("UPDATE farms SET location_status='unverified' WHERE location_status IS NULL"))
    with op.batch_alter_table('farms') as b:
        b.alter_column('location_source', existing_type=sa.String(), nullable=False)
        b.alter_column('location_status', existing_type=sa.String(), nullable=False)
def downgrade():
    existing={c['name'] for c in sa.inspect(op.get_bind()).get_columns('farms')}
    for name in reversed(COLUMNS):
        if name in existing:
            with op.batch_alter_table('farms') as b: b.drop_column(name)
