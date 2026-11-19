"""The Savings button: group total + everyone's contributions, open to admins and ordinary members."""
from datetime import date

from app.models import GroupMember
from tests.conftest import add_member, make_user


def _contribute(world, member, amount, key):
    r = world.ca.post(f"/g/{world.ga.id}/savings/new", data={
        "member_id": member.id, "amount": str(amount), "category": "MONTHLY",
        "contribution_date": date.today().isoformat(), "idempotency_key": key})
    assert r.status_code == 302


def _page(client, world, qs=""):
    return client.get(f"/g/{world.ga.id}/savings/group{qs}")


def test_admin_and_ordinary_member_both_see_group_total_and_everyones_savings(app, world):
    _contribute(world, world.ma, 50_000, "k1")
    _contribute(world, world.adm_a_m, 120_000, "k2")
    for client in (world.ca, world.cm):                       # admin and ordinary member
        r = _page(client, world)
        assert r.status_code == 200
        html = r.data.decode()
        assert "Group total savings" in html and "170,000" in html
        assert "Ann Admin" in html and "Mark Member" in html
        assert "120,000" in html and "50,000" in html
    # admin saved the most, so is ranked first
    table = _page(world.cm, world).data.decode().split("<tbody>")[1]
    assert table.index("Ann Admin") < table.index("Mark Member")


def test_all_contributions_tab_and_member_filter_for_ordinary_member(app, world):
    _contribute(world, world.ma, 50_000, "k1")
    _contribute(world, world.adm_a_m, 120_000, "k2")
    html = _page(world.cm, world, "?view=all").data.decode()
    assert "Ann Admin" in html and "Mark Member" in html and "120,000" in html
    assert "Receipt" not in html                                 # receipt numbers stay with officials
    only = _page(world.cm, world, f"?view=all&member={world.adm_a_m.id}").data.decode()
    assert "120,000" in only and "has saved" in only
    assert "Receipt" in _page(world.ca, world, "?view=all").data.decode()


def test_reversed_contribution_not_counted(app, world):
    from app.models import Contribution
    _contribute(world, world.ma, 50_000, "k1")
    c = Contribution.query.first()
    world.ca.post(f"/g/{world.ga.id}/savings/{c.id}/reverse", data={"reason": "mistake"})
    html = _page(world.cm, world).data.decode()
    assert "Group total savings" in html and "50,000" not in html.split("<tbody>")[1]
    assert "Reversed" in _page(world.cm, world, "?view=all").data.decode()


def test_rows_add_up_to_group_total(app, world):
    from app.services import group_savings
    _contribute(world, world.ma, 50_000, "k1")
    _contribute(world, world.adm_a_m, 70_000, "k2")
    d = group_savings.breakdown(world.ga)
    assert d["total"] == 120_000 and sum(r["total"] for r in d["rows"]) + d["other"] == d["total"]
    assert d["rows"][0]["total"] == 70_000 and abs(sum(r["share"] for r in d["rows"]) - 100) < 0.01
    assert d["rows"][0]["last"] == date.today()


def test_page_is_private_to_the_group(app, world):
    _contribute(world, world.ma, 50_000, "k1")
    assert world.cb.get(f"/g/{world.ga.id}/savings/group").status_code in (403, 404)     # other group's admin
    outsider = make_user("Olly Outsider", "0772000055", "o@x.com")
    from tests.conftest import login
    assert login(app, "o@x.com").get(f"/g/{world.ga.id}/savings/group").status_code in (403, 404)
    assert app.test_client().get(f"/g/{world.ga.id}/savings/group").status_code in (302, 401)
    pending = GroupMember(group_id=world.ga.id, user_id=outsider.id, role="MEMBER", status="PENDING")
    from app.extensions import db
    db.session.add(pending)
    db.session.commit()
    assert login(app, "o@x.com").get(f"/g/{world.ga.id}/savings/group").status_code in (403, 404)


def test_savings_buttons_lead_to_the_group_page(app, world):
    target = f"/g/{world.ga.id}/savings/group"
    for client in (world.ca, world.cm):
        html = client.get(f"/g/{world.ga.id}/").data.decode()
        assert f'href="{target}"' in html                       # menu button and dashboard tile
    assert f'href="/g/{world.ga.id}/savings/"' in world.ca.get(f"/g/{world.ga.id}/").data.decode()   # admins keep "Record Contributions"


def test_lending_business_has_no_savings_page(app, world):
    from app.groups import create_group_from_form
    from app.extensions import db
    owner = make_user("Lola Lender", "0772000101", "l@x.com")
    grp, err = create_group_from_form(owner, {"group_name": "Quick Cash Ltd", "currency": "UGX", "business_type": "LENDING"}, None)
    db.session.commit()
    from tests.conftest import login
    assert login(app, "l@x.com").get(f"/g/{grp.id}/savings/group").status_code == 404
