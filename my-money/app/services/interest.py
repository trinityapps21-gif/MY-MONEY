"""Loan interest and repayment-schedule engine (pure functions, Decimal only).

Terms, stated explicitly so a monthly rate is never mistaken for an annual one:

* ``rate_bp``      interest rate in basis points of a percent (10.00% = 1000)
* ``rate_period``  the period the rate applies to: WEEK, MONTH or YEAR
                   ("10% per MONTH" is 10% for every month of the loan)
* ``duration`` + ``duration_unit``  loan length in WEEKs or MONTHs
* ``frequency``    repayment frequency: WEEKLY or MONTHLY

FLAT:      interest = principal x rate x number_of_rate_periods_in_the_loan.
           Principal and interest are split evenly over the instalments
           (any rounding remainder goes on the last instalment).
REDUCING:  equal-instalment amortisation. The per-instalment rate is
           rate x rate_periods_per_year / instalments_per_year, and each
           instalment's interest is charged on the remaining principal.

All amounts are integers in minor units; every rounding is ROUND_HALF_UP.
"""
import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

PERIODS_PER_YEAR = {"WEEK": 52, "MONTH": 12, "YEAR": 1}
FREQ_PER_YEAR = {"WEEKLY": 52, "MONTHLY": 12}
UNIT_PER_YEAR = {"WEEK": 52, "MONTH": 12}
MAX_INSTALMENTS = 520


@dataclass
class Instalment:
    number: int
    due_date: date
    principal: int
    interest: int

    @property
    def total(self):
        return self.principal + self.interest


@dataclass
class ScheduleResult:
    instalments: list
    total_principal: int
    total_interest: int

    @property
    def total_repayable(self):
        return self.total_principal + self.total_interest


def _round(d: Decimal) -> int:
    return int(d.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    year, month = d.year + m // 12, m % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def instalment_count(duration: int, unit: str, frequency: str) -> int:
    years = Decimal(duration) / UNIT_PER_YEAR[unit]
    return max(1, _round(years * FREQ_PER_YEAR[frequency]))


def due_dates(first: date, frequency: str, n: int) -> list:
    if frequency == "MONTHLY":
        return [add_months(first, i) for i in range(n)]
    return [first + timedelta(days=7 * i) for i in range(n)]


def build_schedule(principal, rate_bp, rate_period, method, duration, duration_unit, frequency, first_repayment_date):
    if principal <= 0:
        raise ValueError("Principal must be greater than zero")
    if duration <= 0:
        raise ValueError("Loan duration must be greater than zero")
    if rate_bp < 0:
        raise ValueError("Interest rate cannot be negative")
    if rate_period not in PERIODS_PER_YEAR or duration_unit not in UNIT_PER_YEAR or frequency not in FREQ_PER_YEAR:
        raise ValueError("Invalid interest period, duration unit or repayment frequency")
    if method not in ("FLAT", "REDUCING"):
        raise ValueError("Invalid interest method")

    n = instalment_count(duration, duration_unit, frequency)
    if n > MAX_INSTALMENTS:
        raise ValueError("Too many instalments for this duration and frequency")
    dates = due_dates(first_repayment_date, frequency, n)
    rate = Decimal(rate_bp) / Decimal(10000)
    years = Decimal(duration) / UNIT_PER_YEAR[duration_unit]
    P = Decimal(principal)
    rows = []

    if method == "FLAT":
        total_interest = _round(P * rate * (years * PERIODS_PER_YEAR[rate_period]))
        base_p, rem_p = divmod(principal, n)
        base_i, rem_i = divmod(total_interest, n)
        for k in range(n):
            last = k == n - 1
            rows.append(Instalment(k + 1, dates[k], base_p + (rem_p if last else 0), base_i + (rem_i if last else 0)))
    else:
        annual = rate * PERIODS_PER_YEAR[rate_period]
        i = annual / FREQ_PER_YEAR[frequency]
        if i == 0:
            payment = P / n
        else:
            payment = P * i / (1 - (1 + i) ** (-n))
        pay_int = _round(payment)
        balance = principal
        for k in range(n):
            interest = _round(Decimal(balance) * i)
            if k == n - 1:
                princ = balance
            else:
                princ = max(0, min(balance, pay_int - interest))
            rows.append(Instalment(k + 1, dates[k], princ, interest))
            balance -= princ

    return ScheduleResult(rows, sum(r.principal for r in rows), sum(r.interest for r in rows))
