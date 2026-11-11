"""Chargeback: cost estimates from recorded usage and local rates (E9, plan §5.14, §14).

**What is priced.**

* Tokens: every gateway call in the period, at the rate in force for its model when it
  was made (see ``app.models.chargeback``).
* GPU time: the hours each model's deployments held their GPUs (``gpu_allocations``),
  at that model's ``gpu_hour`` rate, split at every rate change. A deployment's GPUs cost
  the same whether it answers one call or a thousand, so the hours are **shared out by
  token share**: a caller who sent 30 % of a model's tokens in the period carries 30 % of
  its GPU time. Hours no caller used (a model deployed and idle) are reported as **idle
  capacity**, not spread over whoever happened to call.

**What is never invented.** Usage with no applicable rate is *unpriced*, not free: it is
counted, and its cost is null. A model with no GPU rate has GPU hours and no GPU cost.
Streams the client abandoned count only the tokens recorded, so they are flagged
``partial``. External runtimes (Ollama, a team's own vLLM) hold no platform GPU
reservation, so they have no GPU time here. All amounts are estimates, and say so.

**Team scope.** A scoped caller sees only rows for their teams and shared usage. GPU
shares are still computed against everyone's tokens, and idle capacity is shown only to
callers who can see every team: a scoped report must not hand one team the whole GPU bill
because it cannot see the others' traffic.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import itertools
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.scope import Scope
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.chargeback import DEFAULT_RATE_MODEL, CostRate
from app.models.models_registry import UsageRecord
from app.repositories.chargeback import GROUPS, ChargebackRepository, CostRateRepository
from app.services.audit import AuditService

log = get_logger(__name__)

MILLION = Decimal(1_000_000)
HOUR = Decimal(3600)
#: Amounts are carried at full precision and rounded once, here, for display.
MONEY = Decimal("0.0001")
HOURS = Decimal("0.001")
#: A report longer than this is a data export, and belongs in a backup or a warehouse.
MAX_PERIOD_DAYS = 366
MAX_ROWS = 1000
MAX_RATE = Decimal(1_000_000_000)

#: Shown on every report and in every export.
ESTIMATE_NOTE = (
    "Estimates from local rates, not invoices. Token costs use the rate in force for each "
    "call's model when it was made. GPU hours are the time each model's deployments held "
    "their GPUs, shared out by each caller's share of that model's tokens."
)

NO_GROUP_LABEL = {
    "team": "Shared (no team)",
    "api_key": "No API key (internal callers)",
    "user": "No platform user",
    "end_user": "No end user given",
}
IDLE_KEY = "idle"
IDLE_LABEL = "Idle GPU capacity"


@dataclass(slots=True)
class _Line:
    """One report row while it is being built, at full precision."""

    key: str | None
    label: str
    detail: str | None = None
    trusted: bool | None = None
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    token_cost: Decimal | None = None
    gpu_hours: Decimal = Decimal(0)
    gpu_cost: Decimal | None = None
    unpriced_requests: int = 0
    partial_requests: int = 0
    #: Tokens per model, for sharing out GPU time.
    model_tokens: dict[str, int] = field(default_factory=dict)

    def add_token_cost(self, amount: Decimal) -> None:
        self.token_cost = (self.token_cost or Decimal(0)) + amount

    def add_gpu_cost(self, amount: Decimal) -> None:
        self.gpu_cost = (self.gpu_cost or Decimal(0)) + amount


@dataclass(slots=True)
class _ModelGpu:
    hours: Decimal = Decimal(0)
    #: Cost of the hours that had a GPU rate; ``None`` when none had one.
    cost: Decimal | None = None
    unpriced_hours: Decimal = Decimal(0)


def _money(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(MONEY)


def _hours(value: Decimal) -> float:
    return float(value.quantize(HOURS))


def _sum(values: Iterable[Decimal | None]) -> Decimal | None:
    present = [v for v in values if v is not None]
    return sum(present, Decimal(0)) if present else None


class RateSchedule:
    """Which rate applies to a model at a moment, from the full list of rates."""

    def __init__(self, rates: Sequence[CostRate]) -> None:
        self._by_model: dict[str, list[CostRate]] = defaultdict(list)
        for rate in rates:
            self._by_model[rate.model].append(rate)
        for items in self._by_model.values():
            items.sort(key=lambda r: r.effective_from)

    @staticmethod
    def _latest(items: list[CostRate], at: dt.datetime) -> CostRate | None:
        found = None
        for rate in items:
            if rate.effective_from > at:
                break
            found = rate
        return found

    def at(self, model: str, at: dt.datetime) -> CostRate | None:
        """The model's own latest started rate, else the default's."""
        own = self._latest(self._by_model.get(model, []), at)
        return own or self._latest(self._by_model.get(DEFAULT_RATE_MODEL, []), at)

    def segments(
        self, model: str, start: dt.datetime, end: dt.datetime
    ) -> list[tuple[dt.datetime, dt.datetime, CostRate | None]]:
        """``[start, end)`` cut wherever the applicable rate can change."""
        cuts = sorted(
            {
                rate.effective_from
                for name in (model, DEFAULT_RATE_MODEL)
                for rate in self._by_model.get(name, [])
                if start < rate.effective_from < end
            }
        )
        bounds = [start, *cuts, end]
        return [(a, b, self.at(model, a)) for a, b in itertools.pairwise(bounds)]


def period_bounds(
    start: dt.date, end: dt.date, *, now: dt.datetime | None = None
) -> tuple[dt.datetime, dt.datetime]:
    """``[start 00:00 UTC, end+1 00:00 UTC)``, refusing a reversed or oversized period."""
    if end < start:
        raise ValidationError("The period ends before it starts.", details={"field": "end"})
    if (end - start).days + 1 > MAX_PERIOD_DAYS:
        raise ValidationError(
            f"A report covers at most {MAX_PERIOD_DAYS} days.", details={"field": "start"}
        )
    now = now or dt.datetime.now(dt.UTC)
    if start > now.date():
        raise ValidationError("The period starts in the future.", details={"field": "start"})
    begin = dt.datetime.combine(start, dt.time.min, tzinfo=dt.UTC)
    finish = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC)
    return begin, finish


class ChargebackService:
    def __init__(
        self,
        rates: CostRateRepository,
        reports: ChargebackRepository,
        audit: AuditService,
        *,
        currency: str,
    ) -> None:
        self._rates = rates
        self._reports = reports
        self._audit = audit
        self.currency = currency

    async def commit(self) -> None:
        """Commit before the response is sent, so a page that reloads right after a
        change sees it."""
        await self._rates.session.commit()

    # -- rates --------------------------------------------------------------
    async def list_rates(self, *, now: dt.datetime | None = None) -> list[dict[str, Any]]:
        now = now or dt.datetime.now(dt.UTC)
        rates = list(await self._rates.list_all())
        by_model: dict[str, list[CostRate]] = defaultdict(list)
        for rate in rates:
            by_model[rate.model].append(rate)
        out = []
        for rate in rates:
            later = sorted(
                r.effective_from
                for r in by_model[rate.model]
                if r.effective_from > rate.effective_from
            )
            superseded_at = later[0] if later else None
            if rate.effective_from > now:
                status = "scheduled"
            elif superseded_at is not None and superseded_at <= now:
                status = "superseded"
            else:
                status = "in_force"
            out.append(
                {
                    "id": rate.id,
                    "model": rate.model,
                    "effective_from": rate.effective_from,
                    "superseded_at": superseded_at,
                    "status": status,
                    "prompt_per_million": rate.prompt_per_million,
                    "completion_per_million": rate.completion_per_million,
                    "gpu_hour": rate.gpu_hour,
                    "note": rate.note,
                    "created_at": rate.created_at,
                    "currency": self.currency,
                }
            )
        return out

    async def create_rate(
        self,
        *,
        actor: User,
        model: str,
        prompt_per_million: Decimal,
        completion_per_million: Decimal,
        gpu_hour: Decimal | None = None,
        effective_from: dt.datetime | None = None,
        note: str | None = None,
    ) -> CostRate:
        model = model.strip()
        if model != DEFAULT_RATE_MODEL and not await self._rates.model_is_known(model):
            raise ValidationError(
                f"No model named {model!r} is registered or has recorded usage. Use the "
                "name the gateway records, or '*' for the default rate.",
                details={"field": "model"},
            )
        for name, value in (
            ("prompt_per_million", prompt_per_million),
            ("completion_per_million", completion_per_million),
            ("gpu_hour", gpu_hour),
        ):
            if value is not None and not Decimal(0) <= value <= MAX_RATE:
                raise ValidationError(
                    f"{name} must be between 0 and {MAX_RATE}.", details={"field": name}
                )
        when = effective_from or dt.datetime.now(dt.UTC)
        if when.tzinfo is None:
            raise ValidationError(
                "effective_from needs a time zone, for example 2026-11-01T00:00:00Z.",
                details={"field": "effective_from"},
            )
        if await self._rates.get_exact(model, when) is not None:
            raise ConflictError(
                f"{model!r} already has a rate starting at {when.isoformat()}.",
                details={"field": "effective_from"},
            )
        rate = CostRate(
            model=model,
            effective_from=when,
            prompt_per_million=prompt_per_million,
            completion_per_million=completion_per_million,
            gpu_hour=gpu_hour,
            note=(note or "").strip() or None,
            created_by=actor.id,
        )
        self._rates.add(rate)
        await self._rates.flush()
        await self._rates.session.refresh(rate)
        await self._audit.record(
            AuditAction.COST_RATE_CREATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="cost_rate",
            resource_id=str(rate.id),
            metadata={
                **self._snapshot(rate),
                # A rate that starts in the past changes reports already read.
                "backdated": when < dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5),
            },
        )
        log.info("cost_rate_created", model=model, effective_from=when.isoformat())
        return rate

    async def delete_rate(self, rate_id: uuid.UUID, *, actor: User, reason: str | None) -> None:
        rate = await self._rates.get(rate_id)
        if rate is None:
            raise NotFoundError(f"No cost rate with id {rate_id}.")
        snapshot = self._snapshot(rate)
        await self._rates.delete(rate)
        await self._rates.flush()
        await self._audit.record(
            AuditAction.COST_RATE_DELETED,
            user_id=actor.id,
            username=actor.username,
            resource_type="cost_rate",
            resource_id=str(rate_id),
            metadata={**snapshot, "reason": reason},
        )

    def _snapshot(self, rate: CostRate) -> dict[str, Any]:
        return {
            "model": rate.model,
            "effective_from": rate.effective_from.isoformat(),
            "prompt_per_million": str(rate.prompt_per_million),
            "completion_per_million": str(rate.completion_per_million),
            "gpu_hour": None if rate.gpu_hour is None else str(rate.gpu_hour),
            "currency": self.currency,
        }

    # -- reports ------------------------------------------------------------
    async def report(
        self,
        *,
        group: str,
        start: dt.date,
        end: dt.date,
        scope: Scope,
        now: dt.datetime | None = None,
        limit: int | None = MAX_ROWS,
    ) -> dict[str, Any]:
        """The cost report. ``limit`` caps the rows returned (not the totals); an export
        passes ``None`` and gets every row."""
        if group not in GROUPS:
            raise ValidationError(
                f"Unknown grouping {group!r}.", details={"field": "group_by", "allowed": GROUPS}
            )
        now = now or dt.datetime.now(dt.UTC)
        begin, finish = period_bounds(start, end, now=now)
        rates = list(await self._rates.list_all())
        schedule = RateSchedule(rates)
        by_id = {rate.id: rate for rate in rates}

        lines: dict[tuple[Any, ...], _Line] = {}
        unpriced_models: set[str] = set()
        for row in await self._reports.usage(
            group=group, start=begin, end=finish, where=scope.clause(UsageRecord.team_id)
        ):
            line = self._line_for(group, row, lines)
            tokens = int(row["prompt_tokens"]) + int(row["completion_tokens"])
            line.requests += int(row["requests"])
            line.prompt_tokens += int(row["prompt_tokens"])
            line.completion_tokens += int(row["completion_tokens"])
            line.partial_requests += int(row["partial"])
            line.model_tokens[row["model"]] = line.model_tokens.get(row["model"], 0) + tokens
            rate = by_id.get(row["rate_id"]) if row["rate_id"] else None
            if rate is None:
                line.unpriced_requests += int(row["requests"])
                unpriced_models.add(row["model"])
                continue
            line.add_token_cost(
                Decimal(int(row["prompt_tokens"])) * rate.prompt_per_million / MILLION
                + Decimal(int(row["completion_tokens"])) * rate.completion_per_million / MILLION
            )

        gpu = self._gpu_by_model(
            await self._reports.model_gpu_reservations(start=begin, end=finish),
            schedule,
            begin,
            min(finish, now),
        )
        totals_by_model = await self._reports.tokens_by_model(start=begin, end=finish)
        for line in lines.values():
            for model, tokens in line.model_tokens.items():
                held = gpu.get(model)
                total = totals_by_model.get(model, 0)
                if held is None or not total or not tokens:
                    continue
                share = Decimal(tokens) / Decimal(total)
                line.gpu_hours += held.hours * share
                if held.cost is not None:
                    line.add_gpu_cost(held.cost * share)

        idle = None
        if scope.all_teams:
            idle = self._idle(gpu, totals_by_model)

        rows = sorted(
            (self._row(line) for line in lines.values()),
            key=lambda r: (
                -(r["total_cost"] or Decimal(0)),
                -(r["prompt_tokens"] + r["completion_tokens"]),
                r["label"],
            ),
        )
        # Totals are over every row, including any the response leaves out.
        every = [*rows, *([idle] if idle else [])]
        truncated = limit is not None and len(rows) > limit
        return {
            "currency": self.currency,
            "group_by": group,
            "start": start,
            "end": end,
            "period_start": begin,
            "period_end": finish,
            "generated_at": now,
            "estimate": True,
            "note": ESTIMATE_NOTE,
            "rows": rows if limit is None else rows[:limit],
            "idle": idle,
            "truncated": truncated,
            "unpriced_models": sorted(unpriced_models),
            "rates_defined": bool(rates),
            "totals": {
                "requests": sum(r["requests"] for r in rows),
                "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
                "completion_tokens": sum(r["completion_tokens"] for r in rows),
                "token_cost": _sum(r["token_cost"] for r in rows),
                "gpu_hours": round(sum(r["gpu_hours"] for r in every), 3),
                "gpu_cost": _sum(r["gpu_cost"] for r in every),
                "total_cost": _sum(r["total_cost"] for r in every),
                "unpriced_requests": sum(r["unpriced_requests"] for r in rows),
                "partial_requests": sum(r["partial_requests"] for r in rows),
            },
        }

    @staticmethod
    def _line_for(group: str, row: dict[str, Any], lines: dict[tuple[Any, ...], _Line]) -> _Line:
        if group == "model":
            ident: tuple[Any, ...] = (row["model"],)
        elif group == "end_user":
            ident = (row["end_user"], row["end_user_trusted"])
        else:
            ident = (row[f"{group}_id"],)
        line = lines.get(ident)
        if line is not None:
            return line
        if group == "model":
            line = _Line(key=row["model"], label=row["model"])
        elif group == "end_user":
            name = row["end_user"]
            line = _Line(
                key=name,
                label=name or NO_GROUP_LABEL["end_user"],
                trusted=bool(row["end_user_trusted"]) if name else None,
                detail=None
                if not name
                else ("trusted" if row["end_user_trusted"] else "self-reported"),
            )
        else:
            key = row[f"{group}_id"]
            if key is None:
                line = _Line(key=None, label=NO_GROUP_LABEL[group])
            else:
                detail = row.get("detail")
                if group == "api_key" and row.get("prefix"):
                    detail = f"{detail} · {row['prefix']}…" if detail else f"{row['prefix']}…"
                # A deleted team or user leaves its id on old usage; say so.
                line = _Line(
                    key=str(key),
                    label=row.get("label") or f"Deleted ({str(key)[:8]})",
                    detail=detail,
                )
        lines[ident] = line
        return line

    @staticmethod
    def _gpu_by_model(
        reservations: list[dict[str, Any]],
        schedule: RateSchedule,
        begin: dt.datetime,
        finish: dt.datetime,
    ) -> dict[str, _ModelGpu]:
        out: dict[str, _ModelGpu] = defaultdict(_ModelGpu)
        for item in reservations:
            start = max(item["reserved_at"], begin)
            stop = min(item["released_at"] or finish, finish)
            if stop <= start:
                continue
            held = out[item["model"]]
            for a, b, rate in schedule.segments(item["model"], start, stop):
                hours = Decimal((b - a).total_seconds()) / HOUR
                held.hours += hours
                if rate is None or rate.gpu_hour is None:
                    held.unpriced_hours += hours
                else:
                    held.cost = (held.cost or Decimal(0)) + hours * rate.gpu_hour
        return dict(out)

    @staticmethod
    def _idle(gpu: dict[str, _ModelGpu], totals: dict[str, int]) -> dict[str, Any] | None:
        """GPU time of models nobody called in the period."""
        idle = [(model, held) for model, held in gpu.items() if not totals.get(model)]
        if not idle:
            return None
        hours = sum((held.hours for _, held in idle), Decimal(0))
        cost = _sum(held.cost for _, held in idle)
        return {
            "key": IDLE_KEY,
            "label": IDLE_LABEL,
            "detail": ", ".join(sorted(model for model, _ in idle)),
            "trusted": None,
            "requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "token_cost": None,
            "gpu_hours": _hours(hours),
            "gpu_cost": _money(cost),
            "total_cost": _money(cost),
            "unpriced_requests": 0,
            "partial_requests": 0,
        }

    @staticmethod
    def _row(line: _Line) -> dict[str, Any]:
        token_cost = _money(line.token_cost)
        gpu_cost = _money(line.gpu_cost)
        return {
            "key": line.key,
            "label": line.label,
            "detail": line.detail,
            "trusted": line.trusted,
            "requests": line.requests,
            "prompt_tokens": line.prompt_tokens,
            "completion_tokens": line.completion_tokens,
            "token_cost": token_cost,
            "gpu_hours": _hours(line.gpu_hours),
            "gpu_cost": gpu_cost,
            "total_cost": _sum([token_cost, gpu_cost]),
            "unpriced_requests": line.unpriced_requests,
            "partial_requests": line.partial_requests,
        }

    # -- export -------------------------------------------------------------
    async def export_csv(
        self, *, actor: User, group: str, start: dt.date, end: dt.date, scope: Scope
    ) -> tuple[str, str]:
        """The report as CSV, and a file name. Audited: an export leaves the platform."""
        report = await self.report(group=group, start=start, end=end, scope=scope, limit=None)
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(
            [
                "group_by",
                "key",
                "label",
                "detail",
                "requests",
                "prompt_tokens",
                "completion_tokens",
                "token_cost",
                "gpu_hours",
                "gpu_cost",
                "total_cost",
                "unpriced_requests",
                "partial_requests",
                "currency",
                "period_start",
                "period_end",
                "estimate",
            ]
        )
        rows = [*report["rows"], *([report["idle"]] if report["idle"] else [])]
        for row in rows:
            writer.writerow(
                [
                    group,
                    _cell(row["key"]),
                    _cell(row["label"]),
                    _cell(row["detail"]),
                    row["requests"],
                    row["prompt_tokens"],
                    row["completion_tokens"],
                    _amount(row["token_cost"]),
                    row["gpu_hours"],
                    _amount(row["gpu_cost"]),
                    _amount(row["total_cost"]),
                    row["unpriced_requests"],
                    row["partial_requests"],
                    report["currency"],
                    report["period_start"].isoformat(),
                    report["period_end"].isoformat(),
                    "true",
                ]
            )
        await self._audit.record(
            AuditAction.CHARGEBACK_EXPORTED,
            user_id=actor.id,
            username=actor.username,
            resource_type="chargeback_report",
            resource_id=group,
            metadata={
                "group_by": group,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "rows": len(rows),
                "all_teams": scope.all_teams,
            },
        )
        name = f"chargeback-{group}-{start.isoformat()}-to-{end.isoformat()}.csv"
        return buffer.getvalue(), name


def _amount(value: Decimal | None) -> str:
    """Empty, not 0, for an unpriced amount."""
    return "" if value is None else str(value)


def _cell(value: Any) -> str:
    """A text cell a spreadsheet will not run as a formula.

    End-user names come from request bodies, so ``=HYPERLINK(...)`` is a value anyone
    with an API key can plant in this report.
    """
    if value is None:
        return ""
    text = str(value)
    if text and text[0] in "=+-@\t\r":
        return "'" + text
    return text
