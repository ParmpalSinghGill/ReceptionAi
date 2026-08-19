"""Password hashing — 05 §A1.4.

**Argon2id**, with parameters chosen rather than defaulted. bcrypt caps input
at 72 bytes (silently truncating a passphrase) and has no memory-hardness, so a
GPU farm attacks it at a rate memory-hard functions make uneconomic. Argon2id
is the OWASP recommendation and the RFC 9106 default choice for password
storage.

The two functions here are asymmetric on purpose:

* :func:`hash_password` returns a self-describing string — algorithm, version
  and parameters are encoded in it — so raising the cost later does not
  invalidate existing hashes. :func:`needs_rehash` detects those and the login
  path upgrades them transparently on a successful verify.
* :func:`verify_password` never raises on a bad password; it returns ``False``.
  A raising verifier tempts a caller into a bare ``except``, which is how "any
  exception means authenticated" gets written.
"""

from __future__ import annotations

from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

__all__ = ["ALGO_NAME", "hash_password", "needs_rehash", "verify_password"]

#: Stored in ``users.password_algo`` alongside the hash, so a future migration
#: can find every row using a superseded algorithm without parsing the hash.
ALGO_NAME: Final = "argon2id"

#: OWASP's second recommended configuration: 19 MiB, 2 iterations, 1 lane.
#: Memory cost is the parameter that matters — it is what makes a GPU attack
#: uneconomic — and 19 MiB is high enough to hurt an attacker while leaving a
#: login under ~50 ms on the API pods.
_HASHER: Final = PasswordHasher(
    time_cost=2,
    memory_cost=19_456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)


def hash_password(plaintext: str) -> str:
    """Hash a password. The result encodes its own parameters."""
    return _HASHER.hash(plaintext)


def verify_password(plaintext: str, stored_hash: str | None) -> bool:
    """Constant-time-ish verification. Returns ``False`` rather than raising.

    A ``None`` hash — an SSO-only user who never set a local password — still
    performs a dummy hash before returning ``False``. Without it, "no local
    password" returns in microseconds while "wrong password" takes ~40 ms, and
    that difference is a user-enumeration oracle measurable over the network.
    """
    if not stored_hash:
        _dummy_verify()
        return False
    try:
        return _HASHER.verify(stored_hash, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True when the hash was made with weaker parameters than current.

    Called on a *successful* login, which is the only moment the plaintext is
    in hand to re-hash with.
    """
    try:
        return _HASHER.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


#: A real Argon2 hash of a fixed string, verified against to burn the same time
#: an actual check would. Not a secret — its only job is to cost what a genuine
#: verification costs.
_DUMMY_HASH: Final = _HASHER.hash("receptionai-timing-equalizer")


def _dummy_verify() -> None:
    try:
        _HASHER.verify(_DUMMY_HASH, "not-the-password")
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        pass
