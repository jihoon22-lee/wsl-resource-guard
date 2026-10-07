"""Web Push (RFC 8030) with aes128gcm payloads (RFC 8291) and VAPID (RFC 8292).

Runs in the root service controller and the owner's guard daemon, both on the
system python3 whose `python3-cryptography` package provides ECDH, ECDSA and
AES-GCM. The web process never imports this module: it only relays the public
key and browser subscriptions to the controller.

Endpoints are restricted to the browsers' push services so a stored
subscription cannot turn either process into a request relay.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

from .safe_read import read_text
from urllib.parse import urlsplit

PUSH_HOSTS = (
    'fcm.googleapis.com',             # Chrome, Edge (Chromium), Android
    'updates.push.services.mozilla.com',  # Firefox
    '.push.apple.com',                # Safari / iOS home-screen apps
    '.notify.windows.com',            # legacy Edge (WNS)
)
VAPID_FILE = 'push-vapid.json'
SUBSCRIPTIONS_FILE = 'push-subscriptions.json'
EXPIRED_FILE = 'push-expired.json'
MAX_SUBSCRIPTIONS = 10
RECORD_SIZE = 4096


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def available() -> tuple[bool, str]:
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return False, 'python3-cryptography가 설치되어 있지 않습니다.'
    return True, ''


def endpoint_allowed(endpoint: object) -> bool:
    if not isinstance(endpoint, str) or len(endpoint) > 1024:
        return False
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return False
    host = (parts.hostname or '').lower()
    return (parts.scheme == 'https' and not parts.username and not parts.password
            and any(host == h.lstrip('.') or (h.startswith('.') and host.endswith(h))
                    for h in PUSH_HOSTS))


def validate_subscription(sub: object) -> dict:
    """Normalized {endpoint, keys: {p256dh, auth}} or ValueError."""
    if not isinstance(sub, dict) or not endpoint_allowed(sub.get('endpoint')):
        raise ValueError('지원하는 브라우저 푸시 서비스의 구독이 아닙니다.')
    keys = sub.get('keys') if isinstance(sub.get('keys'), dict) else {}
    try:
        p256dh, auth = unb64url(str(keys.get('p256dh', ''))), unb64url(str(keys.get('auth', '')))
    except (ValueError, TypeError):
        raise ValueError('구독 키를 해석할 수 없습니다.') from None
    if len(p256dh) != 65 or p256dh[0] != 4 or len(auth) != 16:
        raise ValueError('구독 키 형식이 올바르지 않습니다.')
    return {'endpoint': sub['endpoint'], 'keys': {'p256dh': b64url(p256dh), 'auth': b64url(auth)}}


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b'\x01', hashlib.sha256).digest()[:length]


def _public_bytes(public_key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return public_key.public_bytes(serialization.Encoding.X962,
                                   serialization.PublicFormat.UncompressedPoint)


def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None,
            sender_private=None) -> bytes:
    """RFC 8291 aes128gcm body: header (salt, rs, keyid=sender key) + one record."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ua_public = unb64url(p256dh)
    auth_secret = unb64url(auth)
    sender = sender_private or ec.generate_private_key(ec.SECP256R1())
    as_public = _public_bytes(sender.public_key())
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = sender.exchange(ec.ECDH(), receiver)
    ikm = _hkdf(auth_secret, shared, b'WebPush: info\x00' + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b'Content-Encoding: aes128gcm\x00', 16)
    nonce = _hkdf(salt, ikm, b'Content-Encoding: nonce\x00', 12)
    if len(payload) + 1 + 16 > RECORD_SIZE:
        raise ValueError('푸시 본문이 너무 깁니다.')
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b'\x02', None)
    header = salt + RECORD_SIZE.to_bytes(4, 'big') + bytes([len(as_public)]) + as_public
    return header + ciphertext


def generate_vapid() -> dict:
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, 'big')
    return {'private_key': b64url(private), 'public_key': b64url(_public_bytes(key.public_key())),
            'created_at': time.time()}


def vapid_header(endpoint: str, vapid: dict, subject: str, now: float | None = None) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    parts = urlsplit(endpoint)
    claims = {'aud': f'{parts.scheme}://{parts.netloc}',
              'exp': int((now or time.time()) + 12 * 3600), 'sub': subject}
    signing_input = (b64url(json.dumps({'typ': 'JWT', 'alg': 'ES256'}, separators=(',', ':')).encode())
                     + '.' + b64url(json.dumps(claims, separators=(',', ':')).encode()))
    key = ec.derive_private_key(int.from_bytes(unb64url(vapid['private_key']), 'big'), ec.SECP256R1())
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    token = signing_input + '.' + b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))
    return f"vapid t={token}, k={vapid['public_key']}"


def send(subscription: dict, vapid: dict, subject: str, message: dict,
         urgency: str = 'normal', ttl: int = 86400, opener=urllib.request.urlopen) -> tuple[bool, str, bool]:
    """POST one push. Returns (sent, detail, expired)."""
    endpoint = subscription['endpoint']
    if not endpoint_allowed(endpoint):
        return False, 'endpoint not allowed', True
    body = encrypt(json.dumps(message, ensure_ascii=False).encode(), subscription['keys']['p256dh'],
                   subscription['keys']['auth'])
    request = urllib.request.Request(endpoint, data=body, method='POST', headers={
        'Content-Encoding': 'aes128gcm', 'Content-Type': 'application/octet-stream',
        'TTL': str(ttl), 'Urgency': urgency,
        'Authorization': vapid_header(endpoint, vapid, subject)})
    try:
        with opener(request, timeout=10) as response:
            return True, f'HTTP {response.status}', False
    except urllib.error.HTTPError as exc:
        # 404/410: the browser dropped the subscription; stop sending to it.
        return False, f'HTTP {exc.code}', exc.code in (404, 410)
    except (urllib.error.URLError, OSError) as exc:
        return False, f'{type(exc).__name__}: {getattr(exc, "reason", exc)}'[:200], False


def load_json(path: Path, default):
    try:
        data = json.loads(read_text(path, max_bytes=1024 * 1024))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


DEFAULT_SHARED = Path('/var/lib/wrg-shared')


def subscription_count(shared_dir: Path = DEFAULT_SHARED, state_dir: Path | None = None) -> int:
    expired = gone_endpoints(state_dir) if state_dir else set()
    return sum(1 for s in load_json(shared_dir / SUBSCRIPTIONS_FILE, [])
               if isinstance(s, dict) and s.get('endpoint') not in expired)


def send_all(shared_dir: Path, state_dir: Path | None, subject: str | None, message: dict,
             urgency: str = 'normal', opener=urllib.request.urlopen) -> tuple[int, int, str]:
    """Push to every stored subscription. Returns (sent, attempted, detail).

    The daemon cannot write the root-owned shared directory, so endpoints
    that the push service reports as gone are listed in the owner's state
    directory; the controller prunes them on the next subscription change.
    """
    vapid = load_json(shared_dir / VAPID_FILE, {})
    subject = subject or vapid.get('subject') or 'https://localhost'
    subscriptions = load_json(shared_dir / SUBSCRIPTIONS_FILE, [])
    expired_path = state_dir / EXPIRED_FILE if state_dir else None
    expired = set(load_json(expired_path, [])) if expired_path else set()
    before = set(expired)
    targets = [s for s in subscriptions if isinstance(s, dict) and s.get('endpoint') not in expired]
    if not vapid.get('private_key') or not targets:
        return 0, 0, 'no subscriptions'
    sent, details = 0, []
    for sub in targets:
        ok, detail, gone = send(sub, vapid, subject, message, urgency, opener=opener)
        sent += ok
        details.append(detail)
        if gone:
            expired.add(sub['endpoint'])
    if expired_path and expired != before:
        try:
            expired_path.parent.mkdir(parents=True, exist_ok=True)
            expired_path.write_text(json.dumps(sorted(expired)), encoding='utf-8')
        except OSError:
            pass
    gone = sorted(expired - before)
    return sent, len(targets), ', '.join(details) + (f' · 만료 {len(gone)}개' if gone else '')


def gone_endpoints(state_dir: Path) -> set[str]:
    return {item for item in load_json(state_dir / EXPIRED_FILE, []) if isinstance(item, str)}
