import io
import re
from datetime import date

from openpyxl import Workbook, load_workbook

from app.extensions import db
from app.models import (Contribution, GroupMember, ImportBatch, Loan, LoanRepayment, Notification, StoredFile,
                        Transaction, User)
from app.services import ledger
from tests.conftest import PW

MEMBERS = """full_name,phone,email,role,joined_date
Grace Nambi,0772111111,grace@x.com,MEMBER,15/03/2023
John Okello,0772222222,,TREASURER,01/01/2024
Bad Phone,12,,MEMBER,
Grace Again,0772111111,,MEMBER,
"""


def up(client, gid, kind, name, data, mime="text/csv"):
    return client.post(f"/g/{gid}/import/upload", data={"kind": kind, "file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data")


def batch_of(resp):
    return int(re.search(r"/import/batch/(\d+)", resp.headers["Location"]).group(1))


def import_members(world):
    r = up(world.ca, world.ga.id, "members", "m.csv", MEMBERS.encode())
    bid = batch_of(r)
    world.ca.post(f"/g/{world.ga.id}/import/batch/{bid}/apply")
    return bid


def test_templates_and_pages(app, world):
    b = f"/g/{world.ga.id}/import"
    assert world.ca.get(b + "/").status_code == 200
    for kind in ("members", "contributions", "loans", "repayments"):
        for fmt in ("csv", "xlsx"):
            assert world.ca.get(f"{b}/template/{kind}.{fmt}").status_code == 200
    assert world.cm.get(b + "/").status_code == 403  # members cannot import
    assert world.cb.get(b + "/").status_code == 404


def test_members_preview_then_confirm_is_quiet(app, world):
    b = f"/g/{world.ga.id}/import"
    r = up(world.ca, world.ga.id, "members", "m.csv", MEMBERS.encode())
    bid = batch_of(r)
    page = world.ca.get(f"{b}/batch/{bid}")
    assert page.status_code == 200 and b"Preview only" in page.data
    assert GroupMember.query.filter_by(group_id=world.ga.id).count() == 2  # nothing saved by the preview
    batch = db.session.get(ImportBatch, bid)
    assert (batch.total_rows, batch.ok_rows, batch.error_rows) == (4, 2, 2)
    before = Notification.query.count()
    world.ca.post(f"{b}/batch/{bid}/apply")
    assert GroupMember.query.filter_by(group_id=world.ga.id, status="ACTIVE").count() == 4
    assert Notification.query.count() == before  # no notification flood
    u = User.query.filter_by(phone="0772222222").first()
    assert GroupMember.query.filter_by(group_id=world.ga.id, user_id=u.id).first().role == "TREASURER"
    assert world.ca.post(f"{b}/batch/{bid}/apply").status_code == 302  # cannot apply twice
    assert GroupMember.query.filter_by(group_id=world.ga.id).count() == 4
    assert world.ca.get(f"{b}/batch/{bid}/errors.csv").status_code == 200


def test_imported_member_claims_account_via_forgot_password(app, world):
    import_members(world)
    anon = app.test_client()
    anon.post("/forgot-password", data={"identifier": "0772111111"})
    from app.models import PasswordResetRequest
    assert PasswordResetRequest.query.count() == 1
    assert anon.post("/login", data={"identifier": "0772111111", "password": "guess"}).status_code == 200


def test_full_flow_contributions_loans_repayments_and_undo(app, world):
    import_members(world)
    gid, b = world.ga.id, f"/g/{world.ga.id}/import"
    contribs = ("phone,amount,date,category,payment_method,reference\n"
                "0772111111,\"UGX 1,000,000\",05/06/2024,OPENING,Cash,R1\n"
                "0772222222,500000,06/06/2024,monthly,mtn momo,R2\n"
                "0772999999,100,06/06/2024,,,\n"
                "0772222222,500000,06/06/2024,monthly,,\n")
    cb = batch_of(up(world.ca, gid, "contributions", "c.csv", contribs.encode()))
    assert (db.session.get(ImportBatch, cb).ok_rows, db.session.get(ImportBatch, cb).error_rows) == (2, 2)
    n_before = Notification.query.count()
    world.ca.post(f"{b}/batch/{cb}/apply")
    assert Notification.query.count() == n_before
    assert Contribution.query.filter_by(group_id=gid).count() == 2
    assert ledger.totals(gid)["total_savings"] == 1500000
    assert Contribution.query.filter_by(notes="Opening balance (imported).").count() == 1
    # re-uploading the same file is caught as duplicates
    again = batch_of(up(world.ca, gid, "contributions", "c.csv", contribs.encode()))
    assert db.session.get(ImportBatch, again).ok_rows == 0

    loans = ("borrower_name,principal,rate,rate_period,method,duration,duration_unit,disbursement_date,loan_ref\n"
             "Carl Trader,1000000,10,month,flat,3,months,01/05/2024,L-1\n"
             "Dina Shop,200000,5,MONTH,Reducing balance,4,MONTH,01/05/2024,L-2\n"
             "Bad Loan,1000,10,month,wrong,3,months,01/05/2024,\n")
    lb = batch_of(up(world.ca, gid, "loans", "l.csv", loans.encode()))
    assert db.session.get(ImportBatch, lb).ok_rows == 2
    world.ca.post(f"{b}/batch/{lb}/apply")
    carl = Loan.query.filter_by(group_id=gid, principal=1000000).first()
    assert carl.status == "ACTIVE" and carl.total_interest == 300000 and carl.method == "FLAT"
    assert ledger.cash_balance(gid) == 1500000 - 1200000

    reps = ("loan_ref,amount,date,reference\nL-1,400000,01/06/2024,P1\nL-1,400000,01/07/2024,P2\n"
            "L-1,900000,01/08/2024,P3\nZZ,1,01/08/2024,\n")
    rb = batch_of(up(world.ca, gid, "repayments", "r.csv", reps.encode()))
    assert db.session.get(ImportBatch, rb).ok_rows == 2  # third exceeds balance, fourth unknown loan
    world.ca.post(f"{b}/batch/{rb}/apply")
    assert LoanRepayment.query.filter_by(loan_id=carl.id, status="POSTED").count() == 2  # identical amounts allowed
    db.session.expire_all()
    from app.services.loans import outstanding
    assert sum(outstanding(db.session.get(Loan, carl.id))) == 1300000 - 800000
    assert ledger.cash_balance(gid) == 1500000 - 1200000 + 800000
    assert Notification.query.filter(Notification.kind == "REPAYMENT").count() == 0

    # a loans batch cannot be undone while repayments exist
    r = world.ca.post(f"{b}/batch/{lb}/undo", follow_redirects=True)
    assert b"Undo the repayments import first" in r.data
    # undo in reverse order restores the ledger exactly
    world.ca.post(f"{b}/batch/{rb}/undo")
    world.ca.post(f"{b}/batch/{lb}/undo")
    world.ca.post(f"{b}/batch/{cb}/undo")
    db.session.expire_all()
    assert db.session.get(ImportBatch, cb).status == "UNDONE"
    assert ledger.cash_balance(gid) == 0 and ledger.totals(gid)["total_savings"] == 0
    rows = Transaction.query.filter_by(group_id=gid).all()
    assert sum(t.amount for t in rows if t.direction == "IN") == sum(t.amount for t in rows if t.direction == "OUT")


def test_excel_upload_and_member_undo_keeps_members_with_records(app, world):
    import_members(world)
    gid, b = world.ga.id, f"/g/{world.ga.id}/import"
    wb = Workbook()
    ws = wb.active
    ws.append(["Member", "Mobile", "Amount Paid", "Date Paid"])  # unusual headers are understood
    ws.append(["Grace Nambi", "0772111111", 75000, date(2024, 7, 1)])
    buf = io.BytesIO()
    wb.save(buf)
    xb = batch_of(up(world.ca, gid, "contributions", "c.xlsx", buf.getvalue()))
    assert db.session.get(ImportBatch, xb).ok_rows == 1
    world.ca.post(f"{b}/batch/{xb}/apply")
    assert ledger.member_savings(gid, GroupMember.query.join(User, GroupMember.user_id == User.id)
                                 .filter(User.phone == "0772111111").first().id) == 75000
    mb = ImportBatch.query.filter_by(group_id=gid, kind="members").first().id
    world.ca.post(f"{b}/batch/{mb}/undo")
    active = {m.user.phone for m in GroupMember.query.filter_by(group_id=gid, status="ACTIVE")}
    assert "0772111111" in active and "0772222222" not in active  # Grace has savings so she stays


def test_pdf_text_table_is_read_and_scanned_pdf_is_kept_as_reference(app, world):
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
    from reportlab.lib import colors
    import_members(world)
    gid = world.ga.id
    buf = io.BytesIO()
    t = Table([["phone", "amount", "date"], ["0772111111", "20000", "02/02/2024"], ["0772222222", "30000", "03/02/2024"]])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    SimpleDocTemplate(buf, pagesize=A4).build([t])
    pb = batch_of(up(world.ca, gid, "contributions", "c.pdf", buf.getvalue()))
    assert db.session.get(ImportBatch, pb).ok_rows == 2
    # scanned PDF = a picture with no text
    from PIL import Image
    img, sb = Image.new("RGB", (600, 800), "white"), io.BytesIO()
    img.save(sb, "PDF")
    before = StoredFile.query.count()
    r = up(world.ca, gid, "contributions", "scan.pdf", sb.getvalue())
    assert r.headers["Location"].endswith("/import/")
    assert StoredFile.query.count() == before + 1
    sf = StoredFile.query.filter_by(kind="import").order_by(StoredFile.id.desc()).first()
    assert sf.provider == "imagekit" and sf.group_id == gid
    assert world.ca.get(f"/files/{sf.id}").status_code in (200, 302)
    assert world.cm.get(f"/files/{sf.id}").status_code == 404  # private: officials only
    assert world.cb.get(f"/files/{sf.id}").status_code == 404
    page = world.ca.get(f"/g/{gid}/import/").data
    assert b"scan.pdf" in page and b"reference only" in page


def test_import_isolation_and_bad_files(app, world):
    import_members(world)
    gid, b = world.ga.id, f"/g/{world.ga.id}/import"
    bid = batch_of(up(world.ca, gid, "contributions", "c.csv",
                      b"phone,amount,date\n0772111111,5000,01/01/2024\n"))
    assert world.cb.get(f"/g/{world.gb.id}/import/batch/{bid}").status_code == 404
    assert world.cb.post(f"/g/{world.gb.id}/import/batch/{bid}/apply").status_code == 404
    assert world.cb.post(f"/g/{world.gb.id}/import/batch/{bid}/undo").status_code == 404
    assert world.cm.post(f"{b}/batch/{bid}/apply").status_code == 403
    # group B cannot import group A's phone numbers as members of its own contributions
    r = up(world.cb, world.gb.id, "contributions", "c.csv", b"phone,amount,date\n0772111111,5000,01/01/2024\n")
    assert db.session.get(ImportBatch, batch_of(r)).ok_rows == 0
    # bad inputs are rejected with a message, not a crash
    for name, data in (("x.exe", b"MZ"), ("empty.csv", b""), ("nocols.csv", b"a,b\n1,2\n"), ("fake.pdf", b"not a pdf")):
        resp = up(world.ca, gid, "members", name, data)
        assert resp.status_code == 302 and "/batch/" not in resp.headers["Location"], name
    assert up(world.ca, gid, "nonsense", "m.csv", MEMBERS.encode()).status_code == 302
