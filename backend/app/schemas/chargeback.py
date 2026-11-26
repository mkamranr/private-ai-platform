"""Chargeback rates and cost reports (E9).

Amounts are ``Decimal`` and serialise as strings: a currency amount that went through a
float would not always add up. ``null`` means *not priced*, which is not the same as
zero.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RateStatus = Literal["in_force", "scheduled", "superseded"]
GroupBy = Literal["team", "api_key", "user", "end_user", "model"]


class CostRateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(
        min_length=1,
        max_length=128,
        description="Model name, '*' for the default, or 'tool:<name>' / 'tool:*' for tools",
    )
    prompt_per_million: Decimal = Field(default=Decimal(0), ge=0, max_digits=18, decimal_places=6)
    completion_per_million: Decimal = Field(
        default=Decimal(0), ge=0, max_digits=18, decimal_places=6
    )
    gpu_hour: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=6)
    unit_price: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=18,
        decimal_places=6,
        description=(
            "E67: per audio minute (transcription model), per million characters (speech "
            "model) or per call (tool rate)"
        ),
    )
    effective_from: dt.datetime | None = Field(
        default=None, description="When the rate starts. Defaults to now; may be in the past."
    )
    note: str | None = Field(default=None, max_length=500)


class CostRateDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


class CostRateRead(BaseModel):
    id: uuid.UUID
    model: str
    effective_from: dt.datetime
    superseded_at: dt.datetime | None
    status: RateStatus
    prompt_per_million: Decimal
    completion_per_million: Decimal
    gpu_hour: Decimal | None
    unit_price: Decimal | None = None
    note: str | None
    created_at: dt.datetime
    currency: str


class CostRatesResponse(BaseModel):
    currency: str
    items: list[CostRateRead]


class ChargebackRow(BaseModel):
    key: str | None
    label: str
    detail: str | None
    #: End-user rows only: whether the name came from a trusted frontend.
    trusted: bool | None
    requests: int
    prompt_tokens: int
    completion_tokens: int
    token_cost: Decimal | None
    gpu_hours: float
    gpu_cost: Decimal | None
    total_cost: Decimal | None
    unpriced_requests: int
    partial_requests: int
    #: E67: speech in its own units, and agent tool calls.
    audio_seconds: int = 0
    speech_characters: int = 0
    speech_cost: Decimal | None = None
    tool_calls: int = 0
    tool_cost: Decimal | None = None
    unpriced_tool_calls: int = 0
    #: E86: pages read by OCR.
    ocr_pages: int = 0
    ocr_cost: Decimal | None = None
    #: Timings (E40). None when nothing was measured; TTFT and tok/s are from streamed
    #: calls only, and ``timed_streams`` says how many.
    avg_latency_ms: float | None = None
    latency_p95_ms: float | None = None
    ttft_p50_ms: float | None = None
    ttft_p95_ms: float | None = None
    output_tps_p50: float | None = None
    timed_streams: int = 0


class ChargebackTotals(BaseModel):
    requests: int
    prompt_tokens: int
    completion_tokens: int
    token_cost: Decimal | None
    gpu_hours: float
    gpu_cost: Decimal | None
    total_cost: Decimal | None
    unpriced_requests: int
    partial_requests: int
    #: E67: speech in its own units, and agent tool calls.
    audio_seconds: int = 0
    speech_characters: int = 0
    speech_cost: Decimal | None = None
    tool_calls: int = 0
    tool_cost: Decimal | None = None
    unpriced_tool_calls: int = 0
    #: E86: pages read by OCR.
    ocr_pages: int = 0
    ocr_cost: Decimal | None = None
    #: Timings (E40). None when nothing was measured; TTFT and tok/s are from streamed
    #: calls only, and ``timed_streams`` says how many.
    avg_latency_ms: float | None = None
    latency_p95_ms: float | None = None
    ttft_p50_ms: float | None = None
    ttft_p95_ms: float | None = None
    output_tps_p50: float | None = None
    timed_streams: int = 0


class ChargebackReport(BaseModel):
    currency: str
    #: E86: the currencies the report can be shown in, and, when shown in another than
    #: the one it is priced in (``priced_in``), the exchange rate applied.
    currencies: list[str] = []
    exchange_rate: Decimal | None = None
    priced_in: str | None = None
    group_by: GroupBy
    start: dt.date
    end: dt.date
    period_start: dt.datetime
    period_end: dt.datetime
    generated_at: dt.datetime
    estimate: bool
    note: str
    #: E103: usage records from before this day may have been deleted (null: all kept),
    #: and a note when the period starts before it.
    records_kept_since: dt.date | None = None
    pruned_note: str | None = None
    rows: list[ChargebackRow]
    #: GPU time of models nobody called. Only for callers who see every team.
    idle: ChargebackRow | None
    truncated: bool
    unpriced_models: list[str]
    rates_defined: bool
    totals: ChargebackTotals


# -- cost budgets (E12) ------------------------------------------------------------
BudgetScope = Literal["global", "team", "api_key"]
BudgetStatus = Literal["ok", "warning", "exceeded", "unpriced", "disabled"]


class CostBudgetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: BudgetScope
    subject: str | None = Field(
        default=None, max_length=64, description="Team or API key id. Ignored for 'global'."
    )
    monthly_amount: Decimal = Field(gt=0, max_digits=18, decimal_places=6)
    warn_percent: int = Field(default=80, ge=1, le=99)
    description: str | None = Field(default=None, max_length=500)
    enabled: bool = True


class CostBudgetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    monthly_amount: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=6)
    warn_percent: int | None = Field(default=None, ge=1, le=99)
    description: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None


class CostBudgetRead(BaseModel):
    id: uuid.UUID
    scope: BudgetScope
    subject: str
    subject_label: str
    monthly_amount: Decimal
    warn_percent: int
    description: str | None
    enabled: bool
    #: Estimated spend this calendar month (UTC); null when none of it is priced.
    spend: Decimal | None
    #: Some usage had no rate, so ``spend`` is a lower bound.
    spend_partial: bool
    percent: int | None
    status: BudgetStatus
    currency: str
    created_at: dt.datetime
    updated_at: dt.datetime


class CostBudgetsResponse(BaseModel):
    currency: str
    month_start: dt.date
    items: list[CostBudgetRead]
