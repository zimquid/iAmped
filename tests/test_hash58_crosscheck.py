"""Differential cross-check of iAmped's HASH58 signer against an independent
reference implementation.

Both iAmped's ``iamped.sync.hash58`` and iOpenPod's ``hash58.py`` are ports of
libgpod's ``itdb_hash58.c``. This test reimplements the algorithm a third time
from the primitives (standard AES S-box + inverse S-box, the libgpod key
derivation, and a hand-rolled HMAC-SHA1 exactly as iOpenPod writes it) and
asserts byte-for-byte agreement with iAmped over randomized inputs.

Purpose: prove the on-device signature is correct *before* writing to real
hardware. If iAmped's lookup tables, the FIXED constant, the zeroed field
offsets, or the HMAC construction ever drift, this fails loudly here instead of
producing an iPod that refuses to mount.
"""
from __future__ import annotations

import hashlib
import random
import struct
import unittest
from math import gcd

from iamped.sync import hash58


# ── Independent reference constants ─────────────────────────────────────
# The standard AES S-box (forward) and inverse S-box, straight from the AES
# spec (FIPS-197). libgpod's TABLE1/TABLE2 are exactly these. Typing them here
# independently means a typo in iAmped's copy is caught by test_tables_match.
_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16"
)
_INV_SBOX = bytes.fromhex(
    "52096ad53036a538bf40a39e81f3d7fb7ce339829b2fff87348e4344c4dee9cb"
    "547b9432a6c2233dee4c950b42fac34e082ea16628d924b2765ba2496d8bd125"
    "72f8f66486689816d4a45ccc5d65b6926c704850fdedb9da5e154657a78d9d84"
    "90d8ab008cbcd30af7e45805b8b34506d02c1e8fca3f0f02c1afbd0301138a6b"
    "3a9111414f67dcea97f2cfcef0b4e67396ac7422e7ad3585e2f937e81c75df6e"
    "47f11a711d29c5896fb7620eaa18be1bfc563e4bc6d279209adbc0fe78cd5af4"
    "1fdda8338807c731b11210592780ec5f60517fa919b54a0d2de57a9f93c99cef"
    "a0e03b4dae2af5b0c8ebbb3c83539961172b047eba77d626e169146355210c7d"
)
# libgpod key-derivation salt (itdb_hash58.c). Part of the algorithm, not
# copied from iAmped — a wrong _FIXED in iAmped would diverge here.
_REF_FIXED = bytes.fromhex("6723fe304533f890992107c1d012b2a10781")

# mhbd field offsets zeroed before hashing (libgpod itdb_hash58.c).
_OFF_DB_ID = 0x18       # 8 bytes
_OFF_UNK_0x32 = 0x32    # 20 bytes
_OFF_SCHEME = 0x30      # u16, set to 1
_OFF_HASH = 0x58        # 20 bytes


def _ref_key(firewire_id: bytes) -> bytes:
    """64-byte HMAC key from the 8-byte FireWire GUID (libgpod scheme)."""
    y = bytearray(16)
    for i in range(4):
        a, b = firewire_id[2 * i], firewire_id[2 * i + 1]
        cur = 1 if a == 0 or b == 0 else a * b // gcd(a, b)
        hi, lo = (cur >> 8) & 0xFF, cur & 0xFF
        y[4 * i] = _SBOX[hi]
        y[4 * i + 1] = _INV_SBOX[hi]
        y[4 * i + 2] = _SBOX[lo]
        y[4 * i + 3] = _INV_SBOX[lo]
    return hashlib.sha1(_REF_FIXED + bytes(y)).digest().ljust(64, b"\0")


def _ref_hmac_sha1(key64: bytes, message: bytes) -> bytes:
    """Hand-rolled HMAC-SHA1 exactly as iOpenPod writes it (key already 64B)."""
    inner = hashlib.sha1(bytes(k ^ 0x36 for k in key64) + message).digest()
    return hashlib.sha1(bytes(k ^ 0x5C for k in key64) + inner).digest()


def ref_sign(database: bytes, guid_hex: str) -> bytes:
    """Independent HASH58 signer. Returns the fully signed database.

    The digest is computed over a copy with db_id/unk_0x32/hash zeroed, but those
    two fields are *restored* in the output (libgpod backs them up and writes
    them back) — only the scheme byte and the hash itself change on disk.
    """
    firewire_id = bytes.fromhex(guid_hex)
    work = bytearray(database)
    work[_OFF_DB_ID:_OFF_DB_ID + 8] = b"\0" * 8
    work[_OFF_UNK_0x32:_OFF_UNK_0x32 + 20] = b"\0" * 20
    work[_OFF_HASH:_OFF_HASH + 20] = b"\0" * 20
    struct.pack_into("<H", work, _OFF_SCHEME, 1)
    digest = _ref_hmac_sha1(_ref_key(firewire_id), bytes(work))

    out = bytearray(database)  # start from the original — db_id/unk_0x32 intact
    struct.pack_into("<H", out, _OFF_SCHEME, 1)
    out[_OFF_HASH:_OFF_HASH + 20] = digest
    return bytes(out)


def _make_mhbd(rng: random.Random, size: int = 0x140) -> bytes:
    """A synthetic but structurally valid mhbd buffer."""
    buf = bytearray(rng.randbytes(size))
    buf[:4] = b"mhbd"
    struct.pack_into("<I", buf, 4, 0xF4)  # header length (hash58 mhbd)
    return bytes(buf)


class Hash58CrossCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.rng = random.Random(0xA11CE)

    def test_tables_and_constants_match(self) -> None:
        """iAmped's lookup tables/salt equal the AES-spec constants."""
        self.assertEqual(hash58._TABLE1, _SBOX, "forward S-box drift")
        self.assertEqual(hash58._TABLE2, _INV_SBOX, "inverse S-box drift")
        self.assertEqual(hash58._FIXED, _REF_FIXED, "key-derivation salt drift")
        self.assertEqual(hash58.HASH_OFFSET, _OFF_HASH)
        self.assertEqual(hash58.HASHING_SCHEME_OFFSET, _OFF_SCHEME)

    def test_signature_matches_reference(self) -> None:
        """iAmped.sign == independent reference over many random inputs."""
        for _ in range(500):
            db = _make_mhbd(self.rng)
            guid = self.rng.randbytes(8).hex().upper()
            self.assertEqual(
                hash58.sign(db, guid), ref_sign(db, guid),
                f"signature mismatch for guid={guid}")

    def test_round_trip_verifies(self) -> None:
        """A freshly signed DB verifies; a one-bit flip does not."""
        for _ in range(50):
            db = _make_mhbd(self.rng)
            guid = self.rng.randbytes(8).hex().upper()
            signed = hash58.sign(db, guid)
            self.assertTrue(hash58.verify(signed, guid))
            tampered = bytearray(signed)
            tampered[0x80] ^= 0x01  # flip a payload byte outside zeroed fields
            self.assertFalse(hash58.verify(bytes(tampered), guid))

    def test_guid_normalization_equivalence(self) -> None:
        """Colon/lowercase GUID forms sign identically to the bare hex."""
        db = _make_mhbd(self.rng)
        raw = "0011223344556677"
        colon = "00:11:22:33:44:55:66:77"
        self.assertEqual(hash58.sign(db, raw), hash58.sign(db, colon.lower()))
        self.assertEqual(hash58.sign(db, raw), ref_sign(db, raw))


if __name__ == "__main__":
    unittest.main()
