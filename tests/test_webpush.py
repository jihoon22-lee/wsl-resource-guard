import json
import unittest

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # The web venv has no cryptography; the system python3 does.
    ec = None

from wsl_resource_guard import webpush
from wsl_resource_guard.webpush import b64url, unb64url

# RFC 8291 Appendix A.
PLAINTEXT = b'When I grow up, I want to be a watermelon'
AS_PRIVATE = 'yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw'
UA_PRIVATE = 'q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94'
UA_PUBLIC = 'BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4'
AUTH = 'BTBZMqHH6r4Tts7J_aSIgg'
SALT = 'DGv6ra1nlYgDCS1FRnbzlw'
EXPECTED_BODY = ('DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS'
                 '6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Q'
                 'ulcy4a-fN')


def decrypt(body: bytes, ua_private, auth: str) -> bytes:
    salt, rs, idlen = body[:16], int.from_bytes(body[16:20], 'big'), body[20]
    as_public = body[21:21 + idlen]
    ciphertext = body[21 + idlen:]
    ua_public = webpush._public_bytes(ua_private.public_key())
    shared = ua_private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), as_public))
    ikm = webpush._hkdf(unb64url(auth), shared, b'WebPush: info\x00' + ua_public + as_public, 32)
    cek = webpush._hkdf(salt, ikm, b'Content-Encoding: aes128gcm\x00', 16)
    nonce = webpush._hkdf(salt, ikm, b'Content-Encoding: nonce\x00', 12)
    assert rs == 4096
    padded = AESGCM(cek).decrypt(nonce, ciphertext, None)
    assert padded.endswith(b'\x02')
    return padded[:-1]


class EndpointTests(unittest.TestCase):
    def test_only_browser_push_services_are_accepted(self):
        for ok in ('https://fcm.googleapis.com/fcm/send/abc',
                   'https://updates.push.services.mozilla.com/wpush/v2/x',
                   'https://web.push.apple.com/QGx'):
            self.assertTrue(webpush.endpoint_allowed(ok), ok)
        for bad in ('http://fcm.googleapis.com/x', 'https://169.254.169.254/latest',
                    'https://evilpush.apple.com.example/x', 'https://u:p@fcm.googleapis.com/x',
                    'https://localhost:8765/healthz', None, 'x' * 2000):
            self.assertFalse(webpush.endpoint_allowed(bad), bad)

    def test_subscription_keys_are_checked(self):
        good = {'endpoint': 'https://fcm.googleapis.com/fcm/send/abc',
                'keys': {'p256dh': UA_PUBLIC, 'auth': AUTH}}
        self.assertEqual(webpush.validate_subscription(good)['keys']['auth'], AUTH)
        with self.assertRaises(ValueError):
            webpush.validate_subscription({**good, 'keys': {'p256dh': 'AAAA', 'auth': AUTH}})


@unittest.skipIf(ec is None, 'python3-cryptography is not installed in this interpreter')
class CryptoTests(unittest.TestCase):
    def test_rfc8291_vector_round_trips(self):
        sender = ec.derive_private_key(int.from_bytes(unb64url(AS_PRIVATE), 'big'), ec.SECP256R1())
        receiver = ec.derive_private_key(int.from_bytes(unb64url(UA_PRIVATE), 'big'), ec.SECP256R1())
        self.assertEqual(b64url(webpush._public_bytes(receiver.public_key())), UA_PUBLIC)
        body = webpush.encrypt(PLAINTEXT, UA_PUBLIC, AUTH, salt=unb64url(SALT), sender_private=sender)
        self.assertEqual(b64url(body), EXPECTED_BODY)
        self.assertEqual(decrypt(body, receiver, AUTH), PLAINTEXT)

    def test_vapid_token_verifies_with_the_public_key(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        vapid = webpush.generate_vapid()
        header = webpush.vapid_header('https://fcm.googleapis.com/fcm/send/abc', vapid,
                                      'https://pc.example.ts.net', now=1000)
        token = header.split('t=', 1)[1].split(',', 1)[0]
        signing_input, signature = token.rsplit('.', 1)
        claims = json.loads(unb64url(signing_input.split('.')[1]))
        self.assertEqual(claims['aud'], 'https://fcm.googleapis.com')
        raw = unb64url(signature)
        public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64url(vapid['public_key']))
        public.verify(encode_dss_signature(int.from_bytes(raw[:32], 'big'), int.from_bytes(raw[32:], 'big')),
                      signing_input.encode(), ec.ECDSA(hashes.SHA256()))

    def test_invalid_curve_point_rejected_before_registration(self):
        invalid = {'endpoint': 'https://fcm.googleapis.com/fcm/send/invalid',
                   'keys': {'p256dh': b64url(b'\x04' + bytes(64)), 'auth': AUTH}}
        with self.assertRaises(ValueError):
            webpush.validate_subscription(invalid)

    def test_invalid_stored_subscription_does_not_block_later_valid_delivery(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory)
            (shared / webpush.VAPID_FILE).write_text(json.dumps(webpush.generate_vapid()))
            good = {'endpoint': 'https://fcm.googleapis.com/fcm/send/good',
                    'keys': {'p256dh': UA_PUBLIC, 'auth': AUTH}}
            invalid = {**good, 'keys': {'p256dh': b64url(b'\x04' + bytes(64)), 'auth': AUTH}}
            (shared / webpush.SUBSCRIPTIONS_FILE).write_text(json.dumps([invalid, {'endpoint': []}, good]))
            response = MagicMock()
            response.__enter__.return_value.status = 201
            opener = MagicMock(return_value=response)
            sent, attempted, detail = webpush.send_all(shared,None,None,{'title':'fixture'},opener=opener)
            self.assertEqual((sent,attempted),(1,3))
            opener.assert_called_once()
            self.assertIn('ValueError',detail)
            self.assertNotIn(good['endpoint'],detail)

    def test_send_all_marks_gone_endpoints(self):
        import tempfile
        import urllib.error
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            shared, state = Path(directory) / 'shared', Path(directory) / 'state'
            shared.mkdir()
            (shared / webpush.VAPID_FILE).write_text(json.dumps(webpush.generate_vapid()))
            subs = [{'endpoint': f'https://fcm.googleapis.com/fcm/send/{i}',
                     'keys': {'p256dh': UA_PUBLIC, 'auth': AUTH}} for i in range(2)]
            (shared / webpush.SUBSCRIPTIONS_FILE).write_text(json.dumps(subs))

            def opener(request, timeout):
                if request.full_url.endswith('/1'):
                    raise urllib.error.HTTPError(request.full_url, 410, 'Gone', {}, None)

                class Response:
                    status = 201

                    def __enter__(self):
                        return self

                    def __exit__(self, *exc):
                        return False
                self.assertEqual(request.headers['Content-encoding'], 'aes128gcm')
                return Response()
            sent, attempted, _ = webpush.send_all(shared, state, 'https://pc', {'title': 't'}, opener=opener)
            self.assertEqual((sent, attempted), (1, 2))
            self.assertEqual(json.loads((state / webpush.EXPIRED_FILE).read_text()), [subs[1]['endpoint']])
            sent, attempted, _ = webpush.send_all(shared, state, 'https://pc', {'title': 't'}, opener=opener)
            self.assertEqual(attempted, 1)
