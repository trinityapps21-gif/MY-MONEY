import io
from datetime import date, timedelta

import pytest

from app import create_app
from app.extensions import db
from app.services.seed import seed_defaults

PW = "Passw0rd!x1"
RC = "ABCDE-FGHJK-LMNPQ-RSTUV"  # recovery code given to test users


@pytest.fixture()
def app():
    a = create_app({"SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:", "SECRET_KEY": "x", "TESTING": True,
                    "STORAGE_BACKEND": "memory", "WTF_CSRF_ENABLED": False, "RATELIMIT_ENABLED": False})
    @a.before_request
    def _reset_g():  # tests hold one outer app context; isolate `g` per request like production
        from flask import g
        for k in list(g.__dict__):
            g.pop(k)

    with a.app_context():
        db.create_all()
        seed_defaults()
        yield a
        db.session.remove()
        db.engine.dispose()


def make_user(name, phone, email, admin=False, with_code=True):
    from werkzeug.security import generate_password_hash
    from app.models import User, utcnow
    u = User(full_name=name, phone=phone, email=email, password_hash=generate_password_hash(PW), is_platform_admin=admin)
    if with_code:
        u.recovery_code_hash, u.recovery_code_created_at = generate_password_hash(RC.replace("-", "")), utcnow()
    db.session.add(u)
    db.session.commit()
    return u


def make_group(owner, name="Group A"):
    from app.groups import create_group_from_form
    grp, err = create_group_from_form(owner, {"group_name": name, "currency": "UGX"}, None)
    assert not err, err
    db.session.commit()
    return grp


def add_member(grp, user, role="MEMBER"):
    from app.models import GroupMember
    m = GroupMember(group_id=grp.id, user_id=user.id, role=role, status="ACTIVE")
    db.session.add(m)
    db.session.commit()
    return m


def login(app, email):
    c = app.test_client()
    r = c.post("/login", data={"identifier": email, "password": PW})
    assert r.status_code == 302
    return c


class World:
    """Two isolated groups, each with an admin and a member, plus a platform admin."""


@pytest.fixture()
def world(app):
    w = World()
    w.admin_a = make_user("Ann Admin", "0772000001", "a@x.com")
    w.admin_b = make_user("Bea Admin", "0772000002", "b@x.com")
    w.mem_a = make_user("Mark Member", "0772000003", "m@x.com")
    w.plat = make_user("Pat Platform", "0772000009", "p@x.com", admin=True)
    w.ga, w.gb = make_group(w.admin_a, "Group A"), make_group(w.admin_b, "Group B")
    w.ma = add_member(w.ga, w.mem_a)
    from app.models import GroupMember
    w.adm_a_m = GroupMember.query.filter_by(group_id=w.ga.id, user_id=w.admin_a.id).first()
    w.adm_b_m = GroupMember.query.filter_by(group_id=w.gb.id, user_id=w.admin_b.id).first()
    w.ca, w.cb, w.cm, w.cp = (login(app, e) for e in ("a@x.com", "b@x.com", "m@x.com", "p@x.com"))
    w.today = date.today()
    return w


def loan_form(borrower_id, **kw):
    d = date.today()
    f = {"borrower_id": borrower_id, "principal": "1000000", "rate": "10", "rate_period": "MONTH", "method": "FLAT",
         "duration": 3, "duration_unit": "MONTH", "frequency": "MONTHLY", "disbursement_date": d.isoformat(),
         "first_repayment_date": (d + timedelta(days=30)).isoformat()}
    f.update(kw)
    return f


def png_bytes():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (40, 40), (10, 90, 200)).save(b, "PNG")
    return b.getvalue()
