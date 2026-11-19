"""Turnstile, 30-minute inactivity sign-out, biometric endpoints and speed helpers."""
import time
from unittest import mock

from app.extensions import db
from app.models import User, WebAuthnCredential
from tests.conftest import PW, make_user


def _on(app):
    app.config.update(TURNSTILE_SITE_KEY="site", TURNSTILE_SECRET_KEY="secret")


def _cf(success=True):
    m = mock.Mock()
    m.json.return_value = {"success": success, "error-codes": [] if success else ["invalid-input-response"]}
    return m


# ---------------------------------------------------------------- Turnstile
def test_turnstile_widget_shown_on_all_forms(app):
    _on(app)
    c = app.test_client()
    for url in ("/login", "/register", "/forgot-password", "/recover"):
        assert b"cf-turnstile" in c.get(url).data, url


def test_login_blocked_without_token_and_never_checks_password(app):
    _on(app)
    make_user("Ann", "0772111111", "ann@x.com")
    c = app.test_client()
    with mock.patch("app.services.turnstile.requests.post") as post:
        r = c.post("/login", data={"identifier": "ann@x.com", "password": PW})
        assert r.status_code == 400 and b"security check" in r.data
        post.assert_not_called()
    assert c.get("/groups/").status_code in (302, 404)  # still signed out


def test_login_ok_with_valid_token_and_fails_when_cloudflare_rejects(app):
    _on(app)
    make_user("Ann", "0772111111", "ann@x.com")
    c = app.test_client()
    with mock.patch("app.services.turnstile.requests.post", return_value=_cf(False)):
        r = c.post("/login", data={"identifier": "ann@x.com", "password": PW, "cf-turnstile-response": "bad"})
        assert r.status_code == 400
    with mock.patch("app.services.turnstile.requests.post", return_value=_cf(True)) as post:
        r = c.post("/login", data={"identifier": "ann@x.com", "password": PW, "cf-turnstile-response": "good"})
        assert r.status_code == 302
        assert post.call_args.kwargs["data"]["response"] == "good"


def test_turnstile_fails_closed_when_cloudflare_unreachable(app):
    _on(app)
    make_user("Ann", "0772111111", "ann@x.com")
    with mock.patch("app.services.turnstile.requests.post", side_effect=OSError("down")):
        r = app.test_client().post("/login", data={"identifier": "ann@x.com", "password": PW, "cf-turnstile-response": "t"})
    assert r.status_code == 400


def test_signup_forgot_recover_and_reset_need_turnstile(app):
    _on(app)
    c = app.test_client()
    with mock.patch("app.services.turnstile.requests.post") as post:
        assert c.post("/register", data={"full_name": "Zed Zed", "phone": "0772999999", "password": PW,
                                         "confirm_password": PW, "group_name": "G"}).status_code == 400
        assert User.query.filter_by(phone="0772999999").first() is None
        assert c.post("/forgot-password", data={"identifier": "x"}).status_code == 400
        assert c.post("/recover", data={"identifier": "x", "recovery_code": "x", "password": PW, "confirm_password": PW}).status_code == 400
        post.assert_not_called()


def test_reset_with_token_needs_turnstile(app, world):
    from app.auth import _serializer
    from app.models import PasswordResetRequest
    _on(app)
    req = PasswordResetRequest(user_id=world.mem_a.id, status="APPROVED")
    db.session.add(req)
    db.session.commit()
    token = _serializer().dumps({"id": world.mem_a.id, "h": world.mem_a.password_hash[-12:], "r": req.id})
    c = app.test_client()
    r = c.post(f"/reset-password/{token}", data={"password": "NewPass123", "confirm_password": "NewPass123"})
    assert r.status_code == 400
    with mock.patch("app.services.turnstile.requests.post", return_value=_cf(True)):
        r = c.post(f"/reset-password/{token}", data={"password": "NewPass123", "confirm_password": "NewPass123",
                                                     "cf-turnstile-response": "ok"})
    assert r.status_code == 302


def test_biometric_options_need_turnstile(app):
    _on(app)
    c = app.test_client()
    with mock.patch("app.services.turnstile.requests.post") as post:
        r = c.post("/login/biometric/options", json={})
        assert r.status_code == 400 and r.get_json()["turnstile"] is True
        post.assert_not_called()
    with mock.patch("app.services.turnstile.requests.post", return_value=_cf(True)):
        r = c.post("/login/biometric/options", json={"cf-turnstile-response": "ok"})
    assert r.status_code == 200 and "challenge" in r.get_json()


# ---------------------------------------------------------------- inactivity
def test_session_expires_after_30_idle_minutes(app, world):
    c = world.ca
    assert c.get("/g/%d/" % world.ga.id).status_code == 200
    with c.session_transaction() as s:
        assert "last_activity" in s
        s["last_activity"] = int(time.time()) - 31 * 60
    r = c.get("/g/%d/" % world.ga.id)
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    with c.session_transaction() as s:
        assert "_user_id" not in s


def test_active_session_stays_and_is_refreshed(app, world):
    c = world.ca
    with c.session_transaction() as s:
        s["last_activity"] = int(time.time()) - 29 * 60
    assert c.post("/session/keepalive").status_code == 200
    with c.session_transaction() as s:
        assert time.time() - s["last_activity"] < 5


def test_idle_fetch_gets_401_json(app, world):
    c = world.ca
    with c.session_transaction() as s:
        s["last_activity"] = int(time.time()) - 31 * 60
    r = c.get("/notifications/recent", headers={"X-Requested-With": "fetch"})
    assert r.status_code == 401 and r.get_json()["code"] == "idle_timeout"


def test_passive_requests_do_not_extend_session(app, world):
    c = world.ca
    with c.session_transaction() as s:
        s["last_activity"] = int(time.time()) - 10 * 60
    c.get("/notifications/recent", headers={"X-Requested-With": "fetch", "X-Mm-Passive": "1"})
    with c.session_transaction() as s:
        assert time.time() - s["last_activity"] > 9 * 60


def test_no_remember_cookie(app):
    make_user("Ann", "0772111111", "ann@x.com")
    r = app.test_client().post("/login", data={"identifier": "ann@x.com", "password": PW, "remember": "on"})
    assert "remember_token" not in (r.headers.get("Set-Cookie") or "")


# ---------------------------------------------------------------- biometrics
def test_biometric_enrol_requires_password_and_login(app, world):
    assert app.test_client().post("/account/biometric/register/options", json={}).status_code in (302, 401)
    r = world.cm.post("/account/biometric/register/options", json={"current_password": "wrong"})
    assert r.status_code == 400
    r = world.cm.post("/account/biometric/register/options", json={"current_password": PW})
    assert r.status_code == 200
    opts = r.get_json()
    assert opts["authenticatorSelection"]["userVerification"] == "required"
    assert opts["authenticatorSelection"]["residentKey"] == "required"


def test_biometric_login_rejects_unknown_or_replayed_credentials(app):
    c = app.test_client()
    assert c.post("/login/biometric/options", json={}).status_code == 200  # Turnstile off in tests
    r = c.post("/login/biometric/verify", json={"credential": {"id": "nope", "response": {}}})
    assert r.status_code == 400
    r = c.post("/login/biometric/verify", json={"credential": {"id": "nope", "response": {}}})  # challenge already used
    assert r.status_code == 400


def test_password_reset_removes_enrolled_phones(app, world):
    db.session.add(WebAuthnCredential(user_id=world.mem_a.id, credential_id="abc", public_key="k", sign_count=0))
    db.session.commit()
    from tests.conftest import RC
    c = app.test_client()
    c.post("/recover", data={"identifier": "m@x.com", "recovery_code": RC, "password": "Brand9New", "confirm_password": "Brand9New"})
    assert WebAuthnCredential.query.filter_by(user_id=world.mem_a.id).count() == 0


def test_remove_biometric_only_own(app, world):
    cred = WebAuthnCredential(user_id=world.mem_a.id, credential_id="abc", public_key="k", sign_count=0)
    db.session.add(cred)
    db.session.commit()
    assert world.ca.post(f"/account/biometric/{cred.id}/delete").status_code == 404
    assert world.cm.post(f"/account/biometric/{cred.id}/delete").status_code == 302
    assert WebAuthnCredential.query.count() == 0


# ---------------------------------------------------------------- speed
def test_static_urls_are_versioned_and_service_worker_is_served(app):
    c = app.test_client()
    html = c.get("/login").data.decode()
    assert "vendor/lucide" in html and "unpkg.com" not in html and "cdn.jsdelivr" not in html
    assert "app.js?v=" in html
    sw = c.get("/sw.js")
    assert sw.status_code == 200 and sw.headers["Service-Worker-Allowed"] == "/"
    st = c.get("/static/js/app.js?v=1")
    assert "immutable" in st.headers["Cache-Control"]


def test_platform_settings_cached_and_refreshed_on_change(app):
    from app.services.seed import get_setting, set_setting
    from sqlalchemy import event
    n = []
    event.listen(db.engine, "before_cursor_execute", lambda *a: n.append(1))
    get_setting("platform_name"); first = len(n)
    for _ in range(5):
        get_setting("maintenance_mode")
    assert len(n) == first  # no extra queries
    set_setting("platform_name", "ZED")
    assert get_setting("platform_name") == "ZED"


def test_page_counters_use_one_query(app, world):
    from sqlalchemy import event
    stmts = []
    event.listen(db.engine, "before_cursor_execute", lambda c, cur, st, *a: stmts.append(st))
    world.ca.get("/g/%d/members/" % world.ga.id)
    counters = [s for s in stmts if "FROM notifications" in s and "count" in s.lower()]
    assert len(counters) <= 1


# ---------------------------------------------------------------- full biometric round trip (software "phone")
class FakePhone:
    """Does what a phone's fingerprint sensor + secure chip do, so the whole WebAuthn flow is verified for real."""
    def __init__(self, origin="http://localhost", rp="localhost"):
        import hashlib, os
        from cryptography.hazmat.primitives.asymmetric import ec
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cid = os.urandom(16)
        self.origin, self.rp_hash, self.count = origin, hashlib.sha256(rp.encode()).digest(), 0

    @staticmethod
    def _b64(b):
        from webauthn.helpers import bytes_to_base64url
        return bytes_to_base64url(b)

    def create(self, opts):
        import json, struct
        from webauthn.helpers import encode_cbor
        nums = self.key.public_key().public_numbers()
        cose = {1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")}
        auth = self.rp_hash + bytes([0x45]) + struct.pack(">I", 0) + b"\x00" * 16 + struct.pack(">H", len(self.cid)) + self.cid + encode_cbor(cose)
        cd = json.dumps({"type": "webauthn.create", "challenge": opts["challenge"], "origin": self.origin}).encode()
        return {"id": self._b64(self.cid), "rawId": self._b64(self.cid), "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": self._b64(cd),
                             "attestationObject": self._b64(encode_cbor({"fmt": "none", "attStmt": {}, "authData": auth}))}}

    def get(self, opts, user_id, uv=True):
        import hashlib, json, struct
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        self.count += 1
        auth = self.rp_hash + bytes([0x05 if uv else 0x01]) + struct.pack(">I", self.count)
        cd = json.dumps({"type": "webauthn.get", "challenge": opts["challenge"], "origin": self.origin}).encode()
        sig = self.key.sign(auth + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": self._b64(self.cid), "rawId": self._b64(self.cid), "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": self._b64(cd), "authenticatorData": self._b64(auth), "signature": self._b64(sig),
                             "userHandle": self._b64(str(user_id).encode())}}


def _enrol(client, phone):
    opts = client.post("/account/biometric/register/options", json={"current_password": PW}).get_json()
    return client.post("/account/biometric/register/verify", json={"credential": phone.create(opts), "label": "Test phone"})


def test_full_biometric_enrol_and_sign_in(app, world):
    phone = FakePhone()
    assert _enrol(world.cm, phone).status_code == 200
    assert WebAuthnCredential.query.filter_by(user_id=world.mem_a.id).count() == 1

    c = app.test_client()  # a fresh, signed-out phone
    opts = c.post("/login/biometric/options", json={}).get_json()
    r = c.post("/login/biometric/verify", json={"credential": phone.get(opts, world.mem_a.id)})
    assert r.status_code == 200 and r.get_json()["ok"]
    assert c.get("/g/%d/" % world.ga.id).status_code == 200          # really signed in as Mark
    assert WebAuthnCredential.query.first().sign_count == 1


def test_biometric_sign_in_refuses_tampering(app, world):
    phone = FakePhone()
    _enrol(world.cm, phone)
    c = app.test_client()
    # signature from a different key
    opts = c.post("/login/biometric/options", json={}).get_json()
    forged = FakePhone(); forged.cid = phone.cid
    assert c.post("/login/biometric/verify", json={"credential": forged.get(opts, world.mem_a.id)}).status_code == 400
    # no fingerprint/face check performed on the phone (UV flag off)
    opts = c.post("/login/biometric/options", json={}).get_json()
    assert c.post("/login/biometric/verify", json={"credential": phone.get(opts, world.mem_a.id, uv=False)}).status_code == 400
    # replaying an old, already-used response
    opts = c.post("/login/biometric/options", json={}).get_json()
    good = phone.get(opts, world.mem_a.id)
    assert c.post("/login/biometric/verify", json={"credential": good}).status_code == 200
    c2 = app.test_client()
    c2.post("/login/biometric/options", json={})
    assert c2.post("/login/biometric/verify", json={"credential": good}).status_code == 400


def test_suspended_user_cannot_use_biometrics(app, world):
    phone = FakePhone()
    _enrol(world.cm, phone)
    world.mem_a.status = "SUSPENDED"
    db.session.commit()
    c = app.test_client()
    opts = c.post("/login/biometric/options", json={}).get_json()
    assert c.post("/login/biometric/verify", json={"credential": phone.get(opts, world.mem_a.id)}).status_code == 403
