"""Pop-up notifications: the message is encrypted so only the device can read it, and signed so push services accept it."""
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.services import webpush as wp


def _device():
    """A fake phone: its key pair and auth secret, as a browser would hand them to the server."""
    key = ec.generate_private_key(ec.SECP256R1())
    auth = os.urandom(16)
    return key, auth, wp.b64e(wp._public_bytes(key.public_key())), wp.b64e(auth)


def _decrypt(key, auth, body):                      # what the browser does (RFC 8291)
    salt, idlen = body[:16], body[20]
    eph, ct = body[21:21 + idlen], body[21 + idlen:]
    shared = key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), eph))
    ikm = HKDF(hashes.SHA256(), 32, auth, b"WebPush: info\x00" + wp._public_bytes(key.public_key()) + eph).derive(shared)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    plain = AESGCM(cek).decrypt(nonce, ct, None)
    assert plain.endswith(b"\x02")
    return plain[:-1]


def test_message_is_readable_only_by_the_device():
    key, auth, p256dh, auth_b64 = _device()
    msg = {"title": "Repayment received", "body": "UGX 220,000 from Mary", "url": "/notifications/5/open"}
    body = wp.encrypt(json.dumps(msg).encode(), p256dh, auth_b64)
    assert json.loads(_decrypt(key, auth, body)) == msg
    assert wp.encrypt(b"x", p256dh, auth_b64) != wp.encrypt(b"x", p256dh, auth_b64)      # fresh salt/key every time
    other, other_auth, _a, _b = _device()
    try:
        _decrypt(other, other_auth, body)
        raise AssertionError("another device must not be able to read it")
    except Exception as e:                                                                  # noqa: BLE001
        assert not isinstance(e, AssertionError)


def test_vapid_signature_verifies_with_the_published_key(monkeypatch):
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    header = wp._vapid_header("https://fcm.googleapis.com/fcm/send/abc", "mailto:a@b.co", "secret-1")
    token = header.split("t=")[1].split(",")[0]
    k = header.split("k=")[1]
    assert k == wp.public_key("secret-1")
    h, c, sig = token.split(".")
    claims = json.loads(wp.b64d(c))
    assert claims["aud"] == "https://fcm.googleapis.com" and claims["sub"] == "mailto:a@b.co"
    raw = wp.b64d(sig)
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), wp.b64d(k))
    pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
               f"{h}.{c}".encode(), ec.ECDSA(hashes.SHA256()))                              # raises if invalid


def test_key_is_stable_and_can_be_set_explicitly(monkeypatch):
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    assert wp.public_key("s") == wp.public_key("s") != wp.public_key("t")
    private, public = wp.generate_keys()
    monkeypatch.setenv("VAPID_PRIVATE_KEY", private)
    assert wp.public_key("anything") == public


def test_only_real_push_services_are_allowed():
    ok = ["https://fcm.googleapis.com/fcm/send/x", "https://updates.push.services.mozilla.com/wpush/v2/x",
          "https://web.push.apple.com/x", "https://wns2-par02p.notify.windows.com/w/x"]
    bad = ["http://fcm.googleapis.com/x", "https://evil.example.com/x", "https://localhost/x",
           "https://169.254.169.254/latest", "https://fcm.googleapis.com.evil.com/x", "https://fcm.googleapis.com:8443/x", ""]
    assert all(wp.allowed_endpoint(u) for u in ok)
    assert not any(wp.allowed_endpoint(u) for u in bad)


def test_send_posts_encrypted_signed_request(monkeypatch):
    import requests
    seen = {}

    class R:
        status_code = 201

    def fake_post(url, data=None, headers=None, timeout=None):
        seen.update(url=url, data=data, headers=headers)
        return R()

    monkeypatch.setattr(requests, "post", fake_post)
    key, auth, p256dh, auth_b64 = _device()
    status = wp.send("https://fcm.googleapis.com/fcm/send/abc", p256dh, auth_b64, {"title": "Hi"}, subject="mailto:a@b.co")
    assert status == 201 and seen["headers"]["Content-Encoding"] == "aes128gcm"
    assert seen["headers"]["Authorization"].startswith("vapid t=")
    assert json.loads(_decrypt(key, auth, seen["data"])) == {"title": "Hi"}
    assert wp.send("https://evil.example.com/x", p256dh, auth_b64, {}, subject="mailto:a@b.co") == 400


def test_payload_carries_the_unread_count_for_the_app_icon_badge():
    from types import SimpleNamespace as NS
    n = NS(id=7, title="Repayment received", body="UGX 220,000", user_id=1)
    assert wp.payload_for(n, 20)["badge"] == 20
    assert "badge" not in wp.payload_for(n)
