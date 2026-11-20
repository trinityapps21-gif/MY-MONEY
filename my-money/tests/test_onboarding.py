"""One-time welcome sequence: founders get splash + Founding Billionaire modal, joined members get the splash only."""
from app.extensions import db
from app.models import GroupMember, GroupInvitation
from tests.conftest import PW, add_member, make_user

SPLASH = ("Welcome, Future Billionaires!", "You have officially joined Emtrixz Technology.", "Preparing your group vault...")
MODAL = ("Founding Billionaire", "Getting started", "Invite Members", "Share your unique group link to bring everyone on board.",
         "Configure Group Rules", "Set your savings target, contribution frequency, and payout schedule.",
         "Make Your First Deposit", "Record or submit your initial contribution to activate the group.",
         "Full Transparency", "Real-time visibility into group balances and transaction logs for all members.",
         "Automated Reminders", "Scheduled payment alerts that eliminate manual follow-ups.",
         "Centralized Record-Keeping", "Secure, auditable group tracking without scattered spreadsheets or chat threads.",
         "Build Wealth")


def _dash(client, world):
    return client.get(f"/g/{world.ga.id}/").data.decode()


def _join_and_approve(app, world):
    world.ca.post(f"/g/{world.ga.id}/invitations", data={"label": "x", "expires_days": 5})
    tok = GroupInvitation.query.filter_by(group_id=world.ga.id).first().token
    c = app.test_client()
    c.post(f"/join/{tok}", data={"full_name": "Joiner One", "phone": "0774000001", "email": "j@x.com",
                                 "password": PW, "confirm_password": PW})
    m = GroupMember.query.filter_by(group_id=world.ga.id, status="PENDING").first()
    return c, m


def test_founder_sees_splash_then_modal_content(app, world):
    html = _dash(world.ca, world)
    assert 'id="ob-root"' in html and 'data-kind="FOUNDER"' in html
    for text in SPLASH + MODAL:
        assert text.replace("'", "&rsquo;") in html or text in html, text
    assert "Skip" in html and 'role="progressbar"' in html and 'data-seconds="5"' in html
    assert f'href="/g/{world.ga.id}/invitations"' in html and f'href="/g/{world.ga.id}/settings"' in html
    assert f'href="/g/{world.ga.id}/savings/new"' in html
    assert "js/onboarding.js" in html


def test_shown_on_whichever_group_page_is_opened_first(app, world):
    assert 'id="ob-root"' in world.ca.get(f"/g/{world.ga.id}/members").data.decode()


def test_marked_done_and_never_shown_again(app, world):
    mem = GroupMember.query.filter_by(group_id=world.ga.id, user_id=world.ga.owner_id).one()
    assert mem.onboarding_pending and mem.onboarding_completed_at is None
    assert world.ca.post(f"/g/{world.ga.id}/onboarding/complete").status_code == 200
    assert world.ca.post(f"/g/{world.ga.id}/onboarding/complete").status_code == 200        # idempotent
    db.session.refresh(mem)
    assert not mem.onboarding_pending and mem.onboarding_completed_at is not None
    html = _dash(world.ca, world)
    assert 'id="ob-root"' not in html and "onboarding.js" not in html and "Founding Billionaire" not in html


def test_joined_member_gets_the_welcome_message_only(app, world):
    c, m = _join_and_approve(app, world)
    assert world.ca.post(f"/g/{world.ga.id}/members/{m.id}/decide", data={"action": "approve"}).status_code == 302
    db.session.refresh(m)
    assert m.onboarding_pending
    html = _dash(c, world)
    assert 'data-kind="MEMBER"' in html
    for text in SPLASH:
        assert text in html
    for text in ("Founding Billionaire", "Getting started", "Full Transparency", "Automated Reminders", "Let&rsquo;s Build Wealth",
                 "ob-modal", "Invite Members"):
        assert text not in html, text
    assert c.post(f"/g/{world.ga.id}/onboarding/complete").status_code == 200
    assert 'id="ob-root"' not in _dash(c, world)


def test_flags_are_per_person_and_per_group(app, world):
    c, m = _join_and_approve(app, world)
    world.ca.post(f"/g/{world.ga.id}/members/{m.id}/decide", data={"action": "approve"})
    world.ca.post(f"/g/{world.ga.id}/onboarding/complete")                              # founder finishes
    db.session.refresh(m)
    assert m.onboarding_pending                                                       # joiner's own welcome untouched
    assert 'data-kind="MEMBER"' in _dash(c, world)
    # the same founder starting a second group gets a fresh welcome for it only
    r = world.ca.post("/groups/new", data={"group_name": "Second Savers", "currency": "UGX"})
    assert r.status_code == 302
    second = GroupMember.query.filter(GroupMember.group_id != world.ga.id, GroupMember.user_id == world.ga.owner_id).one()
    assert second.onboarding_pending
    assert 'id="ob-root"' in world.ca.get(r.headers["Location"]).data.decode()
    assert 'id="ob-root"' not in _dash(world.ca, world)


def test_existing_members_and_importer_style_rows_never_see_it(app, world):
    assert 'id="ob-root"' not in _dash(world.cm, world)                               # member added directly
    assert GroupMember.query.filter_by(group_id=world.ga.id, user_id=world.mem_a.id).one().onboarding_pending is False


def test_registration_lands_on_dashboard_with_founder_welcome(app):
    c = app.test_client()
    r = c.post("/register", data={"full_name": "Fran Founder", "phone": "0772555000", "email": "fran@x.com", "password": PW,
                                  "confirm_password": PW, "group_name": "Fresh Savers", "currency": "UGX"})
    assert r.status_code == 302
    # new accounts first save their recovery code, then land on the dashboard (via /groups), where the welcome plays
    assert "/account/recovery-code" in c.get(r.headers["Location"]).headers["Location"]
    assert c.get("/account/recovery-code").status_code == 200            # shows (and remembers) the code
    done = c.post("/account/recovery-code/confirm", data={"saved": "1"}, follow_redirects=True)
    html = done.data.decode()
    assert done.request.path.startswith("/g/") and 'data-kind="FOUNDER"' in html and "Welcome, Future Billionaires!" in html


def test_lending_business_gets_no_welcome(app):
    from app.groups import create_group_from_form
    owner = make_user("Lola Lender", "0772000101", "lola@x.com")
    grp, err = create_group_from_form(owner, {"group_name": "Quick Cash Ltd", "currency": "UGX", "business_type": "LENDING"}, None)
    db.session.commit()
    from tests.conftest import login
    assert 'id="ob-root"' not in login(app, "lola@x.com").get(f"/g/{grp.id}/").data.decode()


def test_complete_endpoint_security(app, world):
    assert app.test_client().post(f"/g/{world.ga.id}/onboarding/complete").status_code in (302, 401)
    assert world.cb.post(f"/g/{world.ga.id}/onboarding/complete").status_code in (403, 404)     # other group's admin
    assert world.ca.get(f"/g/{world.ga.id}/onboarding/complete").status_code == 405


def test_images_fall_back_cleanly_and_are_configurable(app, world):
    html = _dash(world.ca, world)                                  # default paths: files not supplied yet
    assert "data-ob-portrait" not in html and 'rel="preload"' not in html and "ob-wordmark" in html
    assert "ob-portrait-fallback" in html
    app.config.update(ONBOARDING_CEO_IMAGE="img/logo.png", ONBOARDING_BRAND_LOGO="img/favicon.png")
    html = _dash(world.ca, world)
    assert "data-ob-portrait" in html and 'width="132"' in html and 'height="132"' in html
    assert 'rel="preload" as="image"' in html and "img/logo.png" in html and 'alt="Emtrixz Technology"' in html
    app.config.update(ONBOARDING_CEO_IMAGE="../config.py")         # never serve outside the static folder
    assert "data-ob-portrait" not in _dash(world.ca, world)


def test_script_and_styles_are_served(app):
    c = app.test_client()
    assert c.get("/static/js/onboarding.js").status_code == 200
    assert b".ob-splash" in c.get("/static/css/app.css").data
