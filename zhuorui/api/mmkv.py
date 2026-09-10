"""Strict reader for the app's unencrypted MMKV v3/v4 storage snapshots.

Format references: Tencent/MMKV Core/MiniPBCoder.cpp and MMKVMetaInfo.hpp.
Only the current CRC-checked record region is read; old bytes are never scanned
for tokens. No encryption, expiration flags, recovery mode or partial decoding.
"""
import json
import struct
import zlib

from .errors import SessionError


def varint(data, position):
    value = 0
    for i in range(5):
        if position >= len(data):
            raise ValueError()
        byte = data[position]
        position += 1
        if i == 4 and byte > 15:
            raise ValueError()
        value |= (byte & 127) << (7 * i)
        if byte < 128:
            return value, position
    raise ValueError()


def field(data, position):
    size, position = varint(data, position)
    end = position + size
    if end > len(data):
        raise ValueError()
    return data[position:end], end


def decode(data, metadata):
    try:
        if len(data) < 4 or len(data) > 16 * 1024 * 1024 or len(metadata) < 112:
            raise ValueError()
        size = struct.unpack_from("<I", data)[0]
        crc, version = struct.unpack_from("<II", metadata)
        if version not in (3, 4) or any(metadata[12:28]) or any(metadata[104:112]):
            raise ValueError()
        if size != struct.unpack_from("<I", metadata, 28)[0] or size > len(data) - 4:
            raise ValueError()
        body = data[4:4 + size]
        if zlib.crc32(body) != crc:
            raise ValueError()
        if not body:
            return {}
        _, position = varint(body, 0)  # Initial container length is not updated on append.
        result = {}
        while position < len(body):
            key, position = field(body, position)
            if not key:
                raise ValueError()
            key = key.decode("utf8")
            value, position = field(body, position)
            if value:
                result[key] = value
            else:
                result.pop(key, None)
        return result
    except (ValueError, UnicodeError, struct.error):
        raise SessionError("App storage changed while reading, is damaged, or uses an unsupported MMKV format. Try again or use capture import.") from None


def json_value(records, key):
    try:
        value = records[key]
        raw, end = field(value, 0)
        if end != len(value):
            raise ValueError()
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (KeyError, ValueError, UnicodeError):
        raise SessionError("The app's saved account/settings are missing or unsupported; open the logged-in app or use capture import.") from None
