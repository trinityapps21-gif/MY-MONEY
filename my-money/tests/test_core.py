import pytest
import io
from datetime import date

from app.extensions import db
from app.models import Borrower, Contribution, GroupMember, Loan, Notification, StoredFile, Transaction
from app.services import ledger
from tests.conftest import PW, add_member, login, loan_form, make_group, make_user, png_bytes


def test_health_and_public(app):
    c = app.test_client()
    assert c.get("/health").status_code == 200
    for p in ("/", "/about", "/contact", "/login", "/register", "/forgot-password"):
        assert c.get(p).status_code == 200, p


def test_login_and_group_registration(app):
    c = app.test_client()
    r = c.post("/register", data={"full_name": "New Person", "phone": "0773000001", "email": "n@x.com", "password": PW,
                                  "confirm_password": PW, "group_name": "Fresh Group", "currency": "UGX"})
    assert r.status_code == 302
    assert c.get(r.headers["Location"]).headers["Location"].endswith("/account/recovery-code")  # first sign-in
    assert c.get("/account/recovery-code").status_code == 200
    assert c.post("/account/recovery-code/confirm", data={"saved": "1"}).status_code == 302
    assert c.get(r.headers["Location"]).status_code == 200
    bad = app.test_client().post("/login", data={"identifier": "n@x.com", "password": "wrong"})
    assert bad.status_code == 200


def test_all_pages_render_for_admin(app, world):
    b = f"/g/{world.ga.id}"
    for p in ("/", "/members", "/members/pending", "/invitations", "/savings/", "/savings/new", "/savings/me",
              "/borrowers/", "/borrowers/new", "/loans/", "/loans/new", "/repayments/", "/repayments/new",
              "/feed/", "/feed/activity", "/community/", "/community/posts", "/community/reports",
              "/community/moderation-log", "/announcements/", "/announcements/new", "/announcements/history",
              "/reports/", "/transactions/", "/transactions/new", "/settings"):
        r = world.ca.get(b + p)
        assert r.status_code == 200, (p, r.status_code)
    for p in ("/notifications/", "/profile", "/groups?all=1", "/groups/new"):
        assert world.ca.get(p).status_code == 200, p
    for k in ("savings", "monthly", "annual", "statement", "outstanding_loans", "interest_income"):
        for fmt in ("html", "pdf", "xlsx", "csv"):
            assert world.ca.get(f"{b}/reports/{k}?format={fmt}").status_code == 200, (k, fmt)


def test_member_dashboard_and_official_only(app, world):
    b = f"/g/{world.ga.id}"
    assert world.cm.get(b + "/").status_code == 200
    assert world.cm.get(b + "/savings/me").status_code == 200
    assert world.cm.get(b + "/loans/").status_code == 403
    assert world.cm.get(b + "/settings").status_code == 403
    assert world.cm.post(b + "/savings/new", data={}).status_code == 403


def test_platform_admin_pages(app, world):
    for p in ("/platform/", "/platform/groups", f"/platform/groups/{world.ga.id}", "/platform/users",
              "/platform/analytics", "/platform/audit", "/platform/settings"):
        assert world.cp.get(p).status_code == 200, p
    assert world.ca.get("/platform/").status_code in (403, 404)


def test_invitation_approval_flow(app, world):
    b = f"/g/{world.ga.id}"
    world.ca.post(b + "/invitations", data={"label": "x", "expires_days": 5})
    from app.models import GroupInvitation
    tok = GroupInvitation.query.filter_by(group_id=world.ga.id).first().token
    c = app.test_client()
    assert c.get(f"/join/{tok}").status_code == 200
    r = c.post(f"/join/{tok}", data={"full_name": "Joiner One", "phone": "0774000001", "email": "j@x.com",
                                     "password": PW, "confirm_password": PW})
    assert r.status_code in (200, 302)
    m = GroupMember.query.filter_by(group_id=world.ga.id).filter(GroupMember.status == "PENDING").first()
    assert m is not None
    assert world.ca.post(f"{b}/members/{m.id}/decide", data={"action": "approve"}).status_code == 302
    assert db.session.get(GroupMember, m.id).status == "ACTIVE"


def test_contribution_duplicate_and_reversal_reconciles(app, world):
    b = f"/g/{world.ga.id}"
    d = date.today().isoformat()
    f = {"member_id": world.ma.id, "amount": "50000", "category": "MONTHLY", "contribution_date": d,
         "payment_method": "Cash", "idempotency_key": "k1"}
    world.ca.post(b + "/savings/new", data=f)
    world.ca.post(b + "/savings/new", data=f)  # duplicate submit
    assert Contribution.query.filter_by(group_id=world.ga.id).count() == 1
    assert ledger.member_savings(world.ga.id, world.ma.id) == 50000
    c = Contribution.query.first()
    world.ca.post(f"{b}/savings/{c.id}/reverse", data={"reason": "mistake"})
    assert ledger.member_savings(world.ga.id, world.ma.id) == 0
    t = ledger.totals(world.ga.id)
    assert t["total_savings"] == 0 and ledger.cash_balance(world.ga.id) == 0


def _make_loan(world, **kw):
    b = f"/g/{world.ga.id}"
    world.ca.post(b + "/borrowers/new", data={"name": "Carl Trader", "category": "Trader"})
    bid = Borrower.query.filter_by(group_id=world.ga.id).first().id
    world.ca.post(b + "/savings/new", data={"member_id": world.ma.id, "amount": "5000000", "category": "MONTHLY",
                                            "contribution_date": date.today().isoformat(), "idempotency_key": "seed"})
    world.ca.post(b + "/loans/new", data=loan_form(bid, **kw))
    loan = Loan.query.filter_by(group_id=world.ga.id).first()
    assert loan is not None
    for a in ("approve", "disburse"):
        world.ca.post(f"{b}/loans/{loan.id}/action", data={"action": a})
    return db.session.get(Loan, loan.id), b


def test_flat_loan_lifecycle_and_dashboard_reconciliation(app, world):
    loan, b = _make_loan(world)
    assert loan.status == "ACTIVE"
    assert loan.total_interest == 300000  # 10% per month x 3 months flat
    world.ca.post(b + "/repayments/new", data={"loan_id": loan.id, "amount": "400000",
                                               "payment_date": date.today().isoformat(), "payment_method": "Cash",
                                               "idempotency_key": "r1"})
    world.ca.post(b + "/repayments/new", data={"loan_id": loan.id, "amount": "400000",
                                               "payment_date": date.today().isoformat(), "payment_method": "Cash",
                                               "idempotency_key": "r1"})  # duplicate
    from app.models import LoanRepayment
    assert LoanRepayment.query.filter_by(loan_id=loan.id).count() == 1
    from app.services import analytics
    from app.services.loans import outstanding
    from app.services.timeutil import local_today
    s = analytics.group_summary(world.ga, local_today(world.ga.timezone), None, None)
    assert s["available_cash"] == ledger.cash_balance(world.ga.id)
    assert s["total_savings"] == 5000000
    db_rows = Transaction.query.filter_by(group_id=world.ga.id).all()
    inn = sum(t.amount for t in db_rows if t.direction == "IN")
    out = sum(t.amount for t in db_rows if t.direction == "OUT")
    assert inn - out == s["available_cash"]
    po, io_ = outstanding(db.session.get(Loan, loan.id))
    assert po + io_ == 1300000 - 400000


def test_interest_engine_flat_reducing_and_period_labels():
    from datetime import date as d
    from app.services import interest
    start = d(2026, 1, 31)
    flat = interest.build_schedule(1000000, 1000, "MONTH", "FLAT", 3, "MONTH", "MONTHLY", start)
    assert flat.total_interest == 300000 and flat.total_principal == 1000000
    yearly = interest.build_schedule(1000000, 1000, "YEAR", "FLAT", 3, "MONTH", "MONTHLY", start)
    assert yearly.total_interest == 25000
    red = interest.build_schedule(1000000, 1000, "MONTH", "REDUCING", 3, "MONTH", "MONTHLY", start)
    assert red.total_principal == 1000000 and 0 < red.total_interest < 300000
    assert len(red.instalments) == 3


@pytest.mark.skip(reason="The platform interest page was replaced by per-group rules (Group Rules).")
def test_loan_terms_unchanged_when_platform_defaults_change(app, world):
    loan, b = _make_loan(world)
    before = (loan.rate_bp, loan.rate_period, loan.total_interest, loan.method)
    r = world.cp.post(data={"default_rate": "25", "default_rate_period": "YEAR",
                                                  "default_method": "REDUCING", "reason": "policy"})
    assert r.status_code in (200, 302)
    db.session.expire_all()
    l2 = db.session.get(Loan, loan.id)
    assert (l2.rate_bp, l2.rate_period, l2.total_interest, l2.method) == before


def test_tenant_isolation(app, world):
    a, bq = f"/g/{world.ga.id}", f"/g/{world.gb.id}"
    # Group B admin cannot see group A
    for p in ("/", "/members", "/savings/", "/loans/", "/community/", "/reports/", "/settings"):
        assert world.cb.get(a + p).status_code == 404, p
    # A loan in group A cannot be fetched via group B URLs
    loan, _ = _make_loan(world)
    assert world.cb.get(f"{bq}/loans/{loan.id}").status_code == 404
    assert world.cb.post(f"{bq}/loans/{loan.id}/action", data={"action": "write_off", "reason": "x"}).status_code == 404
    bid = Borrower.query.filter_by(group_id=world.ga.id).first().id
    assert world.cb.get(f"{bq}/borrowers/{bid}").status_code == 404
    tx = Transaction.query.filter_by(group_id=world.ga.id).first()
    assert world.cb.post(f"{bq}/transactions/{tx.id}/reverse", data={"reason": "x"}).status_code == 404
    # Another group's file ids are not viewable
    from app.services import storage
    sf = storage.store_bytes(b"%PDF-1.4 test", "x.pdf", "application/pdf", group_id=world.ga.id,
                             user_id=world.admin_a.id, kind="doc")
    db.session.commit()
    assert world.cb.get(f"/files/{sf.id}").status_code in (403, 404)
    assert world.ca.get(f"/files/{sf.id}").status_code in (200, 302)
    # Anonymous users are redirected
    assert app.test_client().get(a + "/").status_code in (302, 401)


def test_storage_routing_and_logo(app, world):
    from app.services import storage
    from app.services.storage_providers.memory import MemoryProvider
    assert storage.provider_name_for("image/png") == "uploadcare"
    assert storage.provider_name_for("application/pdf") == "imagekit"
    r = world.ca.post(f"/g/{world.ga.id}/settings/logo", data={"logo": (io.BytesIO(png_bytes()), "logo.png")},
                      content_type="multipart/form-data")
    assert r.status_code == 302
    db.session.expire_all()
    from app.models import Group
    grp = db.session.get(Group, world.ga.id)
    sf = db.session.get(StoredFile, grp.logo_file_id)
    assert sf.provider == "uploadcare" and sf.group_id == world.ga.id
    assert world.ca.get(f"/files/{sf.id}").status_code == 200
    bad = world.ca.post(f"/g/{world.ga.id}/settings/logo", data={"logo": (io.BytesIO(b"not an image"), "x.png")},
                        content_type="multipart/form-data")
    assert bad.status_code == 302 and db.session.get(Group, world.ga.id).logo_file_id == sf.id


def test_announcement_and_notification(app, world):
    b = f"/g/{world.ga.id}"
    r = world.ca.post(b + "/announcements/new", data={"title": "Meeting", "content": "Saturday 10am",
                                                      "is_published": "1", "is_pinned": "1"})
    assert r.status_code == 302
    assert b"Meeting" in world.cm.get(b + "/announcements/").data
    assert Notification.query.filter_by(user_id=world.mem_a.id).count() >= 1
    assert world.cm.get("/notifications/recent").status_code == 200
