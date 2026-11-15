"""Money helpers. Amounts are stored as integer minor units; Decimal is used for
all arithmetic and parsing so no float ever touches a financial value."""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

ZERO_DECIMAL_CURRENCIES = {"UGX", "RWF", "JPY", "KRW", "XOF", "XAF", "BIF", "GNF", "CLP", "VND", "TZS"}
CURRENCIES = ["UGX", "KES", "TZS", "RWF", "USD", "EUR", "GBP", "NGN", "GHS", "ZAR"]


def decimals_for(currency: str) -> int:
    return 0 if currency.upper() in ZERO_DECIMAL_CURRENCIES else 2


def to_minor(value, decimals: int) -> int:
    """Parse user input ('1,500,000' / '2500.50') into integer minor units.
    Raises ValueError when the value has more precision than the currency allows."""
    if value is None or str(value).strip() == "":
        raise ValueError("Amount is required")
    try:
        d = Decimal(str(value).replace(",", "").replace(" ", ""))
    except InvalidOperation:
        raise ValueError("Enter a valid amount")
    if not d.is_finite():
        raise ValueError("Enter a valid amount")
    scaled = d * (Decimal(10) ** decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"Amount can have at most {decimals} decimal place(s)")
    return int(scaled)


def from_minor(minor: int, decimals: int) -> Decimal:
    return (Decimal(int(minor or 0)) / (Decimal(10) ** decimals)).quantize(Decimal(1) / (Decimal(10) ** decimals)) \
        if decimals else Decimal(int(minor or 0))


def fmt(minor, currency="UGX", decimals=0, prefix=True) -> str:
    d = from_minor(minor or 0, decimals)
    s = f"{d:,.{decimals}f}"
    return f"{currency} {s}" if prefix else s


def fmt_compact(minor, currency="UGX", decimals=0) -> str:
    d = abs(from_minor(minor or 0, decimals))
    sign = "-" if (minor or 0) < 0 else ""
    for limit, suffix in ((Decimal(10) ** 9, "B"), (Decimal(10) ** 6, "M"), (Decimal(10) ** 3, "K")):
        if d >= limit:
            v = (d / limit).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            text = f"{v:f}".rstrip("0").rstrip(".") if v == v.to_integral_value() else f"{v:f}"
            return f"{sign}{currency} {text}{suffix}"
    return f"{sign}{currency} {d:,.{decimals}f}"


def parse_rate_bp(value) -> int:
    """'10' or '10.5' (percent) -> basis points of a percent (1000 / 1050)."""
    try:
        d = Decimal(str(value).replace("%", "").strip())
    except InvalidOperation:
        raise ValueError("Enter a valid interest rate")
    bp = d * 100
    if bp != bp.to_integral_value() or bp < 0:
        raise ValueError("Interest rate can have at most 2 decimal places")
    return int(bp)


def rate_pct(rate_bp: int) -> str:
    d = Decimal(rate_bp) / 100
    return f"{d:f}".rstrip("0").rstrip(".") if d != d.to_integral_value() else f"{int(d)}"
