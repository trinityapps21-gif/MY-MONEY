import re

from app.extensions import db
from app.models import PasswordResetRequest, User
from tests.conftest import PW, RC, add_member, login, make_group, make_user
from werkzeug.security import check_password_hash

NEWPW = "Brand1New!pw"


def _signed_in_without_code(app, email="n@x.com"):
    c = app.test_client()
    assert c.post("/login", data={"identifier": email, "password": PW}).status_code == 302
    return c


def test_first_signin_forces_recovery_code_and_download(app, world):
    u = make_user("New Person", "0775000001", "n@x.com", with_code=False)
    add_member(world.ga, u)
    c = _signed_in_without_code(app)
    r = c.get(f"/g/{world.ga.id}/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/account/recovery-code")
    page = c.get("/account/recovery-code")
    assert page.status_code == 200
    code = re.search(r"[A-Z2-9]{5}(?:-[A-Z2-9]{5}){3}", page.get_data(as_text=True)).group(0)
    dl = c.get("/account/recovery-code/download")
    assert dl.status_code == 200 and code.encode() in dl.data and "attachment" in dl.headers["Content-Disposition"]
    # cannot continue without ticking the box; nothing is stored until confirmed
    assert c.post("/account/recovery-code/confirm", data={}).status_code == 302
    db.session.expire_all()
    assert User.query.filter_by(email="n@x.com").first().recovery_code_hash is None
    assert c.post("/account/recovery-code/confirm", data={"saved": "1"}).status_code == 302
    db.session.expire_all()
    assert User.query.filter_by(email="n@x.com").first().recovery_code_hash
    assert c.get(f"/g/{world.ga.id}/").status_code == 200
    # the plain code is never stored
    assert code.replace("-", "") not in User.query.filter_by(email="n@x.com").first().recovery_code_hash


def test_reset_with_recovery_code_is_single_use(app, world):
    c = app.test_client()
    bad = c.post("/recover", data={"identifier": "m@x.com", "recovery_code": "WRONG-WRONG-WRONG-WRONG",
                                   "password": NEWPW, "confirm_password": NEWPW})
    assert bad.status_code == 200 and b"not correct" in bad.data
    ok = c.post("/recover", data={"identifier": "m@x.com", "recovery_code": RC.lower(), "password": NEWPW,
                                  "confirm_password": NEWPW})
    assert ok.status_code == 302
    db.session.expire_all()
    u = User.query.filter_by(email="m@x.com").first()
    assert check_password_hash(u.password_hash, NEWPW) and u.recovery_code_hash is None
    again = c.post("/recover", data={"identifier": "m@x.com", "recovery_code": RC, "password": "Another1!pw",
                                     "confirm_password": "Another1!pw"})
    assert again.status_code == 200  # code was consumed
    c2 = app.test_client()
    assert c2.post("/login", data={"identifier": "m@x.com", "password": NEWPW}).status_code == 302
    assert c2.get(f"/g/{world.ga.id}/").headers["Location"].endswith("/account/recovery-code")  # new code issued


def test_forgot_password_goes_to_group_admin_who_sends_link(app, world):
    b = f"/g/{world.ga.id}"
    anon = app.test_client()
    r = anon.post("/forgot-password", data={"identifier": "m@x.com", "note": "It is Mark"})
    ghost = anon.post("/forgot-password", data={"identifier": "nobody@x.com"})
    assert r.status_code == ghost.status_code == 302  # no account enumeration
    assert PasswordResetRequest.query.count() == 1
    anon.post("/forgot-password", data={"identifier": "m@x.com"})
    assert PasswordResetRequest.query.count() == 1  # no duplicate pending request
    page = world.ca.get(b + "/members/password-resets")
    assert page.status_code == 200 and b"Mark Member" in page.data and b"It is Mark" in page.data
    assert world.cb.get(f"/g/{world.gb.id}/members/password-resets").status_code == 200
    assert b"Mark Member" not in world.cb.get(f"/g/{world.gb.id}/members/password-resets").data  # other group can't see it
    req = PasswordResetRequest.query.first()
    assert world.cb.post(f"/g/{world.gb.id}/members/password-resets/{req.id}", data={"action": "send"}).status_code == 404
    assert world.cm.get(b + "/members/password-resets").status_code == 403  # members can't
    sent = world.ca.post(f"{b}/members/password-resets/{req.id}", data={"action": "send"})
    assert sent.status_code == 200
    link = re.search(r"http://localhost[^\"<\s]*/reset-password/[^\"<\s]+", sent.get_data(as_text=True)).group(0)
    path = link.replace("http://localhost", "")
    assert anon.get(path).status_code == 200
    done = anon.post(path, data={"password": NEWPW, "confirm_password": NEWPW})
    assert done.status_code == 302
    db.session.expire_all()
    assert db.session.get(PasswordResetRequest, req.id).status == "COMPLETED"
    assert check_password_hash(User.query.filter_by(email="m@x.com").first().password_hash, NEWPW)
    assert anon.get(path).status_code == 302  # link cannot be reused


def test_reset_link_needs_admin_approval_and_rejection_works(app, world):
    from app.auth import make_reset_link
    anon = app.test_client()
    anon.post("/forgot-password", data={"identifier": "m@x.com"})
    req = PasswordResetRequest.query.first()
    with app.test_request_context("/"):
        link = make_reset_link(req)  # forged link for a request nobody approved
    assert anon.get(link.replace("http://localhost", "")).status_code == 302
    b = f"/g/{world.ga.id}"
    assert world.ca.post(f"{b}/members/password-resets/{req.id}", data={"action": "reject"}).status_code == 302
    db.session.expire_all()
    assert db.session.get(PasswordResetRequest, req.id).status == "REJECTED"
    assert anon.get(link.replace("http://localhost", "")).status_code == 302


def test_admin_cannot_approve_own_request_and_profile_new_code(app, world):
    anon = app.test_client()
    anon.post("/forgot-password", data={"identifier": "a@x.com"})
    req = PasswordResetRequest.query.first()
    r = world.ca.post(f"/g/{world.ga.id}/members/password-resets/{req.id}", data={"action": "send"})
    assert r.status_code == 302 and db.session.get(PasswordResetRequest, req.id).status == "PENDING"
    assert world.ca.post("/account/recovery-code/new", data={"current_password": "bad"}).status_code == 302
    ok = world.ca.post("/account/recovery-code/new", data={"current_password": PW})
    assert ok.headers["Location"].endswith("/account/recovery-code")
    assert b"Your new recovery code" in world.ca.get("/account/recovery-code").data
