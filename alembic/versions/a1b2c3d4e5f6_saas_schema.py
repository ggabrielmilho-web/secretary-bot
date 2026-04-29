"""saas_schema

Revision ID: a1b2c3d4e5f6
Revises: 037530ad0721
Create Date: 2026-04-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '037530ad0721'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Novos campos na tabela users
    op.add_column('users', sa.Column('email', sa.String(length=200), nullable=True))
    op.add_column('users', sa.Column('plan', sa.String(length=20), nullable=True, server_default='trial'))
    op.add_column('users', sa.Column('trial_ends_at', sa.DateTime(), nullable=True))
    op.add_column('users', sa.Column('subscription_ends_at', sa.DateTime(), nullable=True))
    op.add_column('users', sa.Column('google_access_token', sa.Text(), nullable=True))
    op.add_column('users', sa.Column('google_refresh_token', sa.Text(), nullable=True))
    op.add_column('users', sa.Column('google_token_expiry', sa.DateTime(), nullable=True))
    op.add_column('users', sa.Column('mercadopago_customer_id', sa.String(length=200), nullable=True))
    op.add_column('users', sa.Column('last_payment_id', sa.String(length=200), nullable=True))

    # Tabela de cupons
    op.create_table(
        'coupons',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(length=50), nullable=False),
        sa.Column('plan', sa.String(length=20), nullable=False),
        sa.Column('duration_days', sa.Integer(), nullable=True),
        sa.Column('max_uses', sa.Integer(), nullable=True, server_default='1'),
        sa.Column('times_used', sa.Integer(), nullable=True, server_default='0'),
        sa.Column('is_active', sa.Boolean(), nullable=True, server_default='true'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_coupons_code'), 'coupons', ['code'], unique=True)

    # Tabela de pagamentos
    op.create_table(
        'payments',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('mercadopago_payment_id', sa.String(length=200), nullable=False),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('currency', sa.String(length=10), nullable=True, server_default='BRL'),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('plan', sa.String(length=20), nullable=True, server_default='monthly'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('mercadopago_payment_id'),
    )


def downgrade() -> None:
    op.drop_table('payments')
    op.drop_index(op.f('ix_coupons_code'), table_name='coupons')
    op.drop_table('coupons')
    op.drop_column('users', 'last_payment_id')
    op.drop_column('users', 'mercadopago_customer_id')
    op.drop_column('users', 'google_token_expiry')
    op.drop_column('users', 'google_refresh_token')
    op.drop_column('users', 'google_access_token')
    op.drop_column('users', 'subscription_ends_at')
    op.drop_column('users', 'trial_ends_at')
    op.drop_column('users', 'plan')
    op.drop_column('users', 'email')
