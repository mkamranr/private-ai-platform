"""Cost rates for chargeback reports (expansion E9, plan §5.14 and §14 "Advanced chargeback").

A rate prices one model's traffic from a moment onwards: an amount per million prompt
tokens, per million completion tokens and, optionally, per GPU-hour held by that model's
deployments. ``model = '*'`` is the default for models that have no rate of their own.

Rates are a **schedule**, not a setting. A price change is a new row with a later
``effective_from``, so a report for last quarter still uses last quarter's price. The
rate that applies to a call is the latest one for its model that had started when the call
was made; a model's own rate always beats the default.

Every figure computed from these rates is an **estimate** in the platform currency
(``chargeback.currency``). The platform knows tokens and GPU reservations; it does not
know what the hardware, power or licences cost, so an operator enters local rates and
the reports say they are estimates (plan §5.14).
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: ``model`` value meaning "every model without a rate of its own".
DEFAULT_RATE_MODEL = "*"

#: Enough precision for a fraction of a cent per million tokens, and for totals well
#: beyond any realistic month.
RATE_PRECISION = (18, 6)


class CostRate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "cost_rates"
    __table_args__ = (
        # Also the index the rate lookup uses: (model, effective_from DESC).
        UniqueConstraint("model", "effective_from", name="uq_cost_rates_model_effective"),
        CheckConstraint(
            "prompt_per_million >= 0 AND completion_per_million >= 0 "
            "AND (gpu_hour IS NULL OR gpu_hour >= 0)",
            name="cost_rate_non_negative",
        ),
    )

    #: A model name as the gateway records it (``usage_records.model``), or ``'*'``.
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    effective_from: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    prompt_per_million: Mapped[Decimal] = mapped_column(
        Numeric(*RATE_PRECISION), default=Decimal(0), nullable=False
    )
    completion_per_million: Mapped[Decimal] = mapped_column(
        Numeric(*RATE_PRECISION), default=Decimal(0), nullable=False
    )
    #: Null: GPU time is not priced for this model (as opposed to priced at zero).
    gpu_hour: Mapped[Decimal | None] = mapped_column(Numeric(*RATE_PRECISION))
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class CostBudgetScope(enum.StrEnum):
    """What a cost budget caps: everything, one team's usage, or one API key's."""

    GLOBAL = "global"
    TEAM = "team"
    API_KEY = "api_key"


#: ``subject`` of a global budget, which has no single subject.
GLOBAL_BUDGET_SUBJECT = "*"


class CostBudget(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A monthly spending limit that raises alerts, never refusals (E12).

    Spend is the chargeback estimate for the calendar month so far (UTC), at the rates in
    ``cost_rates``. The budget raises a WARNING alert at ``warn_percent`` and a CRITICAL
    one at 100%. It does not stop traffic: an estimate built from operator-entered rates is
    not a sound basis for refusing requests. Token quotas do that, with numbers the
    gateway measures itself.
    """

    __tablename__ = "cost_budgets"
    __table_args__ = (
        UniqueConstraint("scope", "subject", name="uq_cost_budgets_scope_subject"),
        CheckConstraint("scope IN ('global','team','api_key')", name="cost_budget_scope"),
        CheckConstraint("monthly_amount > 0", name="cost_budget_positive"),
        CheckConstraint("warn_percent BETWEEN 1 AND 99", name="cost_budget_warn_percent"),
    )

    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    #: A team id or API key id, or ``'*'`` for the global budget.
    subject: Mapped[str] = mapped_column(String(64), nullable=False)
    monthly_amount: Mapped[Decimal] = mapped_column(Numeric(*RATE_PRECISION), nullable=False)
    warn_percent: Mapped[int] = mapped_column(Integer, default=80, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
