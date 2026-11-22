"""Per-group late-fine terms, frozen on each loan: changing a group's rules never touches existing loans."""
from datetime import date, timedelta
from types import SimpleNamespace as NS

from app.services import fine_terms, late_fees

D = date(2026, 1, 1)


def T(**kw):
    base = dict(fine_grace_days=7, fine_period_days=7, fine_type="PERCENT", fine_rate_bp=500, fine_fixed=0, fine_capped=True)
    base.update(kw)
    return NS(**base)


def fine(terms, days, amount=50_000):
    return late_fees.assess(amount, D, D + timedelta(days=days), "COMPLETED", terms=terms)["fine"]


def test_each_group_has_own_terms():
    assert fine(T(), 21) == 5_000                                   # 5% x 2 blocks
    assert fine(T(fine_grace_days=0, fine_period_days=1, fine_rate_bp=100), 10) == 5_000   # 1% x 10 days
    assert fine(T(fine_grace_days=14), 20) == 0                     # still in a longer grace


def test_old_loan_keeps_old_terms_after_group_change():
    old_loan, new_group = T(), T(fine_rate_bp=1000)
    assert fine(old_loan, 28) == 7_500 and fine(new_group, 28) == 15_000
    assert not fine_terms.same(old_loan, new_group)


def test_fixed_free_and_uncapped():
    assert fine(T(fine_type="FIXED", fine_fixed=2_000), 21) == 4_000
    assert fine(T(fine_rate_bp=0), 60) == 0
    a = late_fees.assess(50_000, D, D + timedelta(days=60), "COMPLETED", terms=T(fine_type="FIXED", fine_fixed=2_000))
    assert a["fine"] == 14_000 and not a["capped"]                 # 7 blocks x 2,000
    assert fine(T(fine_capped=False), 300) > 50_000
    assert late_fees.assess(50_000, D, D + timedelta(days=30), "COMPLETED", terms=T(fine_rate_bp=0))["flag"] == "Overdue - No Late Fine"


def test_describe_and_headline():
    assert "5%" in fine_terms.describe(T(), "UGX", 0)
    assert fine_terms.describe(T(fine_rate_bp=0)) == "No late fine is charged."
    assert fine_terms.headline(T()).endswith("capped")
