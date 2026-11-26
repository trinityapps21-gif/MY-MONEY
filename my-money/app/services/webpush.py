"""Web Push (RFC 8030 / 8291 / 8292): pop-up notifications on Android, iPhone (installed app) and desktop browsers.

No third-party push library is needed: the message is encrypted (aes128gcm) and signed with a VAPID key using the
`cryptography` package that the app already depends on. The VAPID key comes from VAPID_PRIVATE_KEY (urlsafe base64 of the
32-byte private value; make one with `flask vapid-keys`). If it is not set, a stable key is derived from SECRET_KEY so push
works out of the box. Changing the key makes devices subscribe again automatically the next time they open the app.
"""
import base64
import json
import logging
import os
import threading
import time
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger("app.push")

# Push services are run by the browser makers; refusing any other host stops a user from making the server call arbitrary URLs.
ALLOWED_HOSTS = ("fcm.googleapis.com", ".push.services.mozilla.com", ".push.apple.com", ".notify.windows.com",
                 ".mozaws.net", "web.push.apple.com")
MAX_SUBSCRIPTIONS_PER_USER = 10
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551      # order of the P-256 curve


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def allowed_endpoint(url: str) -> bool:
    u = urlparse(url or "")
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host or u.port not in (None, 443) or len(url) > 600:
        return False
    return any(host == h.lstrip(".") or (h.startswith(".") and host.endswith(h)) for h in ALLOWED_HOSTS)


# ------------------------------------------------------------------------- VAPID key
def _private_key(secret_key: str = "") -> ec.EllipticCurvePrivateKey:
    raw = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if raw:
        return ec.derive_private_key(int.from_bytes(b64d(raw), "big"), ec.SECP256R1())
    seed = HKDF(algorithm=hashes.SHA256(), length=48, salt=b"my-money-vapid", info=b"web-push").derive(
        (secret_key or os.environ.get("SECRET_KEY", "")).encode() or b"dev-only")
    return ec.derive_private_key(int.from_bytes(seed, "big") % (_N - 1) + 1, ec.SECP256R1())


def _public_bytes(pub) -> bytes:
    return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def public_key(secret_key: str = "") -> str:
    """The application server key the browser needs to subscribe (urlsafe base64, uncompressed point)."""
    return b64e(_public_bytes(_private_key(secret_key).public_key()))


def generate_keys() -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    return b64e(key.private_numbers().private_value.to_bytes(32, "big")), b64e(_public_bytes(key.public_key()))


def _vapid_header(endpoint: str, subject: str, secret_key: str) -> str:
    key = _private_key(secret_key)
    u = urlparse(endpoint)
    head = b64e(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64e(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": subject},
                             separators=(",", ":")).encode())
    der = key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    jwt = f"{head}.{claims}.{b64e(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={jwt}, k={b64e(_public_bytes(key.public_key()))}"


# ------------------------------------------------------------------------- message encryption (RFC 8291)
def encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    ua_public = b64d(p256dh)
    auth_secret = b64d(auth)
    eph = ec.generate_private_key(ec.SECP256R1())
    eph_public = _public_bytes(eph.public_key())
    shared = eph.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret,
               info=b"WebPush: info\x00" + ua_public + eph_public).derive(shared)
    salt = os.urandom(16)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)           # \x02 marks the last (only) record
    return salt + (4096).to_bytes(4, "big") + bytes([len(eph_public)]) + eph_public + body


def send(endpoint: str, p256dh: str, auth: str, message: dict, *, subject: str, secret_key: str = "",
         ttl: int = 86400, timeout: int = 10) -> int:
    """Deliver one message. Returns the push service's HTTP status (201 = accepted, 404/410 = the device is gone), 0 on a network error."""
    import requests
    if not allowed_endpoint(endpoint):
        return 400
    data = json.dumps(message, separators=(",", ":")).encode()
    if len(data) > 3500:
        message = dict(message, body=(message.get("body") or "")[:200])
        data = json.dumps(message, separators=(",", ":")).encode()
    headers = {"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": str(ttl),
               "Urgency": "normal", "Authorization": _vapid_header(endpoint, subject, secret_key)}
    try:
        return requests.post(endpoint, data=encrypt(data, p256dh, auth), headers=headers, timeout=timeout).status_code
    except Exception:                                                     # noqa: BLE001 - a flaky push service must never break the app
        log.warning("push delivery failed", exc_info=True)
        return 0


# ------------------------------------------------------------------------- sending for the app
def payload_for(n, unread=None) -> dict:
    """What the device receives. `badge` is the person's unread count, shown as the red number on the app icon."""
    d = {"title": n.title, "body": n.body or "", "url": f"/notifications/{n.id}/open", "tag": f"n{n.id}", "id": n.id}
    if unread is not None:
        d["badge"] = int(unread)
    return d


def deliver_async(app, targets: list, message: dict) -> None:
    """targets: [(subscription_id, endpoint, p256dh, auth)]. Runs in a background thread so a request never waits on a push service."""
    if not targets or app.config.get("TESTING"):
        return
    subject = app.config.get("VAPID_SUBJECT") or "mailto:admin@example.com"
    secret = app.config.get("SECRET_KEY", "")

    def run():
        from app.extensions import db
        from app.models import PushSubscription
        dead, ok = [], []
        for sid, endpoint, p256dh, auth in targets:
            status = send(endpoint, p256dh, auth, message, subject=subject, secret_key=secret)
            (dead if status in (404, 410) else ok if 200 <= status < 300 else []).append(sid)
        with app.app_context():
            try:
                if dead:
                    PushSubscription.query.filter(PushSubscription.id.in_(dead)).delete(synchronize_session=False)
                if ok:
                    from app.models import utcnow
                    PushSubscription.query.filter(PushSubscription.id.in_(ok)).update(
                        {"last_sent_at": utcnow()}, synchronize_session=False)
                db.session.commit()
            except Exception:                                             # noqa: BLE001
                db.session.rollback()
                log.warning("could not update push subscriptions", exc_info=True)

    threading.Thread(target=run, daemon=True, name="push").start()
