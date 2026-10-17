from tabule import signature

BODY = b'{"id": 101, "status": "resolved"}'
NOW = 1_800_000_000


def test_valid_signature_is_accepted():
    ts = str(NOW)
    sig = signature.sign("segredo", ts, BODY)
    assert signature.verify("segredo", BODY, ts, sig, now=NOW)


def test_wrong_secret_is_rejected():
    ts = str(NOW)
    sig = signature.sign("outro", ts, BODY)
    assert not signature.verify("segredo", BODY, ts, sig, now=NOW)


def test_tampered_body_is_rejected():
    ts = str(NOW)
    sig = signature.sign("segredo", ts, BODY)
    assert not signature.verify("segredo", BODY + b" ", ts, sig, now=NOW)


def test_stale_timestamp_is_rejected():
    ts = str(NOW - 1000)
    sig = signature.sign("segredo", ts, BODY)
    assert not signature.verify("segredo", BODY, ts, sig, now=NOW)


def test_missing_or_invalid_headers_are_rejected():
    assert not signature.verify("segredo", BODY, None, None, now=NOW)
    assert not signature.verify("segredo", BODY, "abc", "sha256=00", now=NOW)
    assert not signature.verify("", BODY, str(NOW), "sha256=00", now=NOW)
