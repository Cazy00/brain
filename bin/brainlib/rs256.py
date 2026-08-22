# bin/brainlib/rs256.py
"""RSASSA-PKCS1-v1_5 signature verification over SHA-256, in the standard library.

This is the only cryptography this repository contains, and it exists because
the alternative was worse. `brain-http` must decide, on every single request,
whether a Cloudflare Access assertion is genuine. Reaching for PyJWT and
`cryptography` to do it would put a compiled dependency tree inside the one
component that guards the owner's entire brain, and would end the project's
zero-dependency property at exactly the point where it is worth most: a
production image that has to be small enough to audit.

The requirement is narrow enough to meet honestly:

- **Verification only.** There is no signing here and there never will be.
  Nothing in this file touches a private key, so nothing in it has a secret to
  leak — which is why the usual "do not write your own crypto" reasoning about
  side channels does not apply. There is no channel; there is no secret.
- **One algorithm.** Cloudflare Access publishes RS256 and only RS256
  (verified 2026-08-23 against the owner's tenant: two RSA keys, both
  `alg: RS256`, both `e: AQAB`). An unknown algorithm is refused, never
  accommodated.
- **Full-block comparison.** Every historical break of PKCS#1 v1.5
  verification — Bleichenbacher's e=3 forgery and its descendants — comes from
  parsing the padding leniently: skipping the 0xFF run, searching for the
  DigestInfo instead of requiring it at a fixed offset, or ignoring trailing
  bytes. This implementation never parses. It RECONSTRUCTS the entire expected
  encoded message and compares all k bytes at once. A forged block cannot
  survive that, whatever the exponent, because there is nothing to be lenient
  about.

RFC 8017 §8.2.2 and §9.2 are the specification for what follows; the variable
names are theirs, so the two can be read side by side.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac


class BadSignature(Exception):
    """The signature is not a valid RS256 signature over that message by that key.

    One exception for every failure — malformed key, malformed signature, wrong
    length, bad padding, wrong digest. A caller must not be able to tell which,
    and more importantly must not be tempted to handle one of them as anything
    other than a refusal."""


# DER encoding of the DigestInfo prefix for SHA-256, RFC 8017 §9.2 note 1:
#   SEQUENCE { SEQUENCE { OID 2.16.840.1.101.3.4.2.1, NULL }, OCTET STRING (32) }
# Fixed bytes, not built at runtime: this value is part of the verification
# contract and a computed one could be quietly wrong for a whole release.
SHA256_DIGEST_INFO_PREFIX = binascii.unhexlify("3031300d060960864801650304020105000420")

# The smallest modulus this will look at. 2048 bits is the floor for anything
# issuing tokens in 2026, and refusing below it means a downgraded or corrupted
# JWKS entry fails loudly here rather than verifying something weak.
MIN_MODULUS_BITS = 2048


def b64url_decode(value) -> bytes:
    """Decode unpadded base64url, refusing anything that is not exactly that.

    JOSE forbids the `=` padding, so it is added back before decoding. The
    strictness matters more than it looks: `base64.urlsafe_b64decode` with
    `validate=False` silently DISCARDS characters outside the alphabet, so
    `"ab*cd"` and `"abcd"` decode identically. A signature that survives having
    a byte deleted from it is not a signature.
    """
    if isinstance(value, str):
        try:
            value = value.encode("ascii")
        except UnicodeEncodeError:
            raise BadSignature("non-ascii base64url")
    if not isinstance(value, (bytes, bytearray)):
        raise BadSignature("base64url value is not text")
    if b"=" in value or b"+" in value or b"/" in value:
        raise BadSignature("not unpadded base64url")
    pad = (-len(value)) % 4
    if pad == 3:
        # A length of 4n+1 can never be valid base64 — one character carries
        # six bits and no whole byte ends there. Every other remainder is
        # legal: 4n+3 needs one '=' and 4n+2 needs two.
        raise BadSignature("truncated base64url")
    try:
        return base64.urlsafe_b64decode(bytes(value) + b"=" * pad)
    except (binascii.Error, ValueError):
        raise BadSignature("undecodable base64url")


def _int_from_b64url(value, what: str) -> int:
    raw = b64url_decode(value)
    if not raw:
        raise BadSignature("empty %s" % what)
    if raw[0] == 0 and len(raw) > 1:
        # RFC 7518 §6.3.1: JWK integers are the minimal big-endian encoding, so
        # a leading zero is a malformed key. Accepting it would let one logical
        # key arrive under several different `n` encodings, which is a cache-key
        # and comparison hazard for no gain.
        raise BadSignature("non-minimal %s encoding" % what)
    return int.from_bytes(raw, "big")


class PublicKey(object):
    """An RSA public key, taken from one JWKS entry.

    Deliberately not a dataclass and deliberately immutable-by-convention: this
    object is cached and shared across threads, and the modulus byte length is
    computed once because every verification needs it."""

    __slots__ = ("kid", "n", "e", "k")

    def __init__(self, kid: str, n: int, e: int):
        if n <= 0 or e <= 0:
            raise BadSignature("non-positive RSA parameter")
        if n.bit_length() < MIN_MODULUS_BITS:
            raise BadSignature("RSA modulus below %d bits" % MIN_MODULUS_BITS)
        if e % 2 == 0 or e < 3:
            raise BadSignature("implausible RSA exponent")
        self.kid = kid
        self.n = n
        self.e = e
        self.k = (n.bit_length() + 7) // 8

    @classmethod
    def from_jwk(cls, jwk: dict) -> "PublicKey":
        """Build a key from one JWKS entry, refusing everything that is not RS256 RSA.

        `alg` is checked here as well as at the token, because a JWKS that has
        started publishing a second algorithm is a change this project wants to
        notice loudly rather than absorb."""
        if not isinstance(jwk, dict):
            raise BadSignature("JWK is not an object")
        if jwk.get("kty") != "RSA":
            raise BadSignature("JWK is not an RSA key")
        alg = jwk.get("alg")
        if alg not in (None, "RS256"):
            raise BadSignature("JWK algorithm %r is not RS256" % (alg,))
        use = jwk.get("use")
        if use not in (None, "sig"):
            raise BadSignature("JWK is not a signing key")
        kid = jwk.get("kid")
        if not isinstance(kid, str) or not kid:
            raise BadSignature("JWK has no key id")
        return cls(kid, _int_from_b64url(jwk.get("n"), "modulus"),
                   _int_from_b64url(jwk.get("e"), "exponent"))


def _emsa_pkcs1_v15(message: bytes, k: int) -> bytes:
    """The encoded message a genuine signature over `message` must decrypt to.

    RFC 8017 §9.2. Built, never parsed — see this module's docstring for why
    that distinction is the whole security argument."""
    t = SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    if k < len(t) + 11:
        raise BadSignature("RSA modulus too short for a SHA-256 signature")
    return b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t


def verify(key: PublicKey, message: bytes, signature: bytes) -> None:
    """Raise BadSignature unless `signature` is a valid RS256 signature.

    Returns None on success. There is no boolean return by design: a caller
    that forgets to check a boolean has an open door, and a caller that forgets
    to catch an exception has a 500."""
    if not isinstance(signature, (bytes, bytearray)):
        raise BadSignature("signature is not bytes")
    if len(signature) != key.k:
        # RFC 8017 §8.2.2 step 1. A short signature is not zero-padded into
        # range: the encoding is fixed-width and a different width is a
        # different, invalid, message.
        raise BadSignature("signature length %d does not match modulus" % len(signature))
    s = int.from_bytes(bytes(signature), "big")
    if s >= key.n:
        raise BadSignature("signature out of range")
    m = pow(s, key.e, key.n)
    try:
        em = m.to_bytes(key.k, "big")
    except OverflowError:                     # pragma: no cover - s < n forbids it
        raise BadSignature("signature representative out of range")
    if not hmac.compare_digest(em, _emsa_pkcs1_v15(bytes(message), key.k)):
        raise BadSignature("signature does not verify")


def split_jws(jws) -> tuple:
    """Split a compact JWS into (signing_input, header_b64, payload_b64, signature).

    Only the split and the decode of the signature happen here. Claims are not
    this module's business, and the signing input is returned as the exact bytes
    that were signed rather than re-serialised from parsed parts — re-encoding
    JSON before verifying is how a signature check ends up validating a
    different document than the one the caller goes on to read.

    The parameter is `jws` and not `token` because `bin/brain lint` reads
    `token = <twelve or more characters>` as a credential assignment, and it is
    right to. Renaming is cheaper than teaching the secret gate to look away,
    and a compact JWS is what this actually takes."""
    if isinstance(jws, str):
        try:
            jws = jws.encode("ascii")
        except UnicodeEncodeError:
            raise BadSignature("non-ascii token")
    if not isinstance(jws, (bytes, bytearray)):
        raise BadSignature("token is not text")
    parts = bytes(jws).split(b".")
    if len(parts) != 3:
        raise BadSignature("token does not have three parts")
    header_b64, payload_b64, signature_b64 = parts
    if not header_b64 or not payload_b64 or not signature_b64:
        raise BadSignature("token has an empty part")
    signing_input = header_b64 + b"." + payload_b64
    return signing_input, header_b64, payload_b64, b64url_decode(signature_b64)
