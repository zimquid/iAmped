"""Device-bound iTunesDB authentication used by nano 3G/4G and iPod Classic.

This is a Python port of libgpod's BSD-licensed ``itdb_hash58.c`` algorithm.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import re
import struct
from pathlib import Path

HASH_OFFSET = 0x58
HASH_SIZE = 20
HASHING_SCHEME_OFFSET = 0x30
HASH58_SCHEME = 1

_TABLE1 = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16"
)
_TABLE2 = bytes.fromhex(
    "52096ad53036a538bf40a39e81f3d7fb7ce339829b2fff87348e4344c4dee9cb"
    "547b9432a6c2233dee4c950b42fac34e082ea16628d924b2765ba2496d8bd125"
    "72f8f66486689816d4a45ccc5d65b6926c704850fdedb9da5e154657a78d9d84"
    "90d8ab008cbcd30af7e45805b8b34506d02c1e8fca3f0f02c1afbd0301138a6b"
    "3a9111414f67dcea97f2cfcef0b4e67396ac7422e7ad3585e2f937e81c75df6e"
    "47f11a711d29c5896fb7620eaa18be1bfc563e4bc6d279209adbc0fe78cd5af4"
    "1fdda8338807c731b11210592780ec5f60517fa919b54a0d2de57a9f93c99cef"
    "a0e03b4dae2af5b0c8ebbb3c83539961172b047eba77d626e169146355210c7d"
)
_FIXED = bytes.fromhex("6723fe304533f890992107c1d012b2a10781")
_HEX_GUID = re.compile(r"^[0-9a-fA-F]{16}$")


def normalize_guid(value: str) -> str:
    guid = re.sub(r"[^0-9a-fA-F]", "", value or "").upper()
    if not _HEX_GUID.fullmatch(guid):
        raise ValueError("FireWire GUID must contain exactly 16 hexadecimal digits")
    return guid


def _key(guid: str) -> bytes:
    firewire_id = bytes.fromhex(normalize_guid(guid))
    transformed = bytearray()
    for a, b in zip(firewire_id[::2], firewire_id[1::2]):
        current = 1 if a == 0 or b == 0 else math.lcm(a, b)
        high, low = (current >> 8) & 0xFF, current & 0xFF
        transformed.extend(
            (_TABLE1[high], _TABLE2[high], _TABLE1[low], _TABLE2[low])
        )
    return hashlib.sha1(_FIXED + transformed).digest().ljust(64, b"\0")


def calculate(database: bytes, guid: str) -> bytes:
    if len(database) < HASH_OFFSET + HASH_SIZE or database[:4] != b"mhbd":
        raise ValueError("Not a hash58-capable iTunesDB")
    work = bytearray(database)
    work[0x18:0x20] = b"\0" * 8
    work[0x32:0x46] = b"\0" * 20
    work[HASH_OFFSET:HASH_OFFSET + HASH_SIZE] = b"\0" * HASH_SIZE
    struct.pack_into("<H", work, HASHING_SCHEME_OFFSET, HASH58_SCHEME)
    return hmac.new(_key(guid), work, hashlib.sha1).digest()


def sign(database: bytes, guid: str) -> bytes:
    signed = bytearray(database)
    struct.pack_into("<H", signed, HASHING_SCHEME_OFFSET, HASH58_SCHEME)
    signed[HASH_OFFSET:HASH_OFFSET + HASH_SIZE] = calculate(signed, guid)
    return bytes(signed)


def verify(database: bytes, guid: str) -> bool:
    if len(database) < HASH_OFFSET + HASH_SIZE:
        return False
    return hmac.compare_digest(
        database[HASH_OFFSET:HASH_OFFSET + HASH_SIZE],
        calculate(database, guid),
    )


def guid_from_device(device_path: str, usb_serial: str = "") -> str:
    device = Path(device_path) / "iPod_Control" / "Device"
    try:
        for line in (device / "SysInfo").read_text(errors="replace").splitlines():
            if line.lower().lstrip().startswith("firewireguid") and ":" in line:
                return normalize_guid(line.split(":", 1)[1])
    except OSError:
        pass
    try:
        text = (device / "SysInfoExtended").read_text(errors="replace")
        match = re.search(
            r"<key>\s*FirewireGuid\s*</key>\s*<(?:string|integer)>([^<]+)",
            text, re.IGNORECASE)
        if match:
            return normalize_guid(match.group(1))
    except OSError:
        pass
    if usb_serial:
        return normalize_guid(usb_serial)
    raise RuntimeError("This iPod requires hash58, but its FireWire GUID was not found")
