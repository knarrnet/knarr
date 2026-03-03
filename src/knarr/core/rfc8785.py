# Vendored from rfc8785 v0.1.4 — Trail of Bits
# License: Apache 2.0
# Source: https://pypi.org/project/rfc8785/
# Frozen standard (RFC 8785) — no upstream updates to track.
#
# JSON Canonicalization Scheme per RFC 8785.
# Single-file vendor to avoid a 4th pip dependency.

from __future__ import annotations

import math
import re
import typing
from io import BytesIO

_Scalar = typing.Union[bool, int, str, float, None]

_Value = typing.Union[
    _Scalar,
    typing.Sequence["_Value"],
    typing.Tuple["_Value"],
    typing.Mapping[str, "_Value"],
]

_INT_MAX = 2**53 - 1
_INT_MIN = -(2**53) + 1

# Adapted from Andrew Rundgren's reference implementation (Apache 2.0).
# See: https://github.com/cyberphone/json-canonicalization
_ESCAPE = re.compile(r'[\x00-\x1f\\"\b\f\n\r\t]')
_ESCAPE_DCT = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}
for i in range(0x20):
    _ESCAPE_DCT.setdefault(chr(i), f"\\u{i:04x}")


class CanonicalizationError(ValueError):
    """The base error for all errors during canonicalization."""
    pass


class IntegerDomainError(CanonicalizationError):
    """The given integer exceeds the safe integer domain for JSON floats."""

    def __init__(self, n: int) -> None:
        super().__init__(f"{n} exceeds safe integer domain for JSON floats")


class FloatDomainError(CanonicalizationError):
    """The given float is not representable in JCS (infinite, NaN, etc.)."""

    def __init__(self, f: float) -> None:
        super().__init__(f"{f} is not representable in JCS")


def _serialize_str(s: str, sink: typing.IO[bytes]) -> None:
    """Serialize a string as a JSON string, per RFC 8785 3.2.2.2."""

    def _replace(match: re.Match) -> str:
        return _ESCAPE_DCT[match.group(0)]

    sink.write(b'"')
    try:
        sink.write(_ESCAPE.sub(_replace, s).encode("utf-8"))
    except UnicodeEncodeError as e:
        raise CanonicalizationError("input contains non-UTF-8 codepoints") from e
    sink.write(b'"')


def _serialize_float(f: float, sink: typing.IO[bytes]) -> None:
    """Serialize a float per ECMA 262 7.1.12.1, amended by RFC 8785 3.2.2.3."""

    if math.isnan(f) or math.isinf(f):
        raise FloatDomainError(f)

    if f == 0:
        sink.write(b"0")
        return

    if f < 0:
        sink.write(b"-")
        _serialize_float(-f, sink)
        return

    stringified = str(f)

    exponent_str = ""
    exponent_value = 0
    q = stringified.find("e")
    if q > 0:
        exponent_str = stringified[q:]
        if exponent_str[2:3] == "0":
            exponent_str = exponent_str[:2] + exponent_str[3:]
        stringified = stringified[0:q]
        exponent_value = int(exponent_str[1:])

    first = stringified
    dot = ""
    last = ""
    q = stringified.find(".")
    if q > 0:
        dot = "."
        first = stringified[:q]
        last = stringified[q + 1:]

    if last == "0":
        dot = ""
        last = ""

    if exponent_value > 0 and exponent_value < 21:
        first += last
        last = ""
        dot = ""
        exponent_str = ""
        q = exponent_value - len(first)
        while q >= 0:
            q -= 1
            first += "0"
    elif exponent_value < 0 and exponent_value > -7:
        last = first + last
        first = "0"
        dot = "."
        exponent_str = ""
        q = exponent_value
        while q < -1:
            q += 1
            last = "0" + last

    sink.write(f"{first}{dot}{last}{exponent_str}".encode())


def dumps(obj: _Value) -> bytes:
    """Perform JCS serialization, returning canonical bytes."""
    sink = BytesIO()
    dump(obj, sink)
    return sink.getvalue()


def dump(obj: _Value, sink: typing.IO[bytes]) -> None:
    """Perform JCS serialization of `obj` into `sink`."""

    if obj is None:
        sink.write(b"null")
    elif isinstance(obj, bool):
        obj = bool(obj)
        if obj is True:
            sink.write(b"true")
        else:
            sink.write(b"false")
    elif isinstance(obj, int):
        obj = int(obj)
        if obj < _INT_MIN or obj > _INT_MAX:
            raise IntegerDomainError(obj)
        sink.write(str(obj).encode("utf-8"))
    elif isinstance(obj, str):
        _serialize_str(obj, sink)
    elif isinstance(obj, float):
        obj = float(obj)
        _serialize_float(obj, sink)
    elif isinstance(obj, (list, tuple)):
        obj = list(obj)
        if not obj:
            sink.write(b"[]")
            return
        sink.write(b"[")
        for idx, elem in enumerate(obj):
            if idx > 0:
                sink.write(b",")
            dump(elem, sink)
        sink.write(b"]")
    elif isinstance(obj, dict):
        obj = dict(obj)
        if not obj:
            sink.write(b"{}")
            return
        # RFC 8785 3.2.3: keys sorted by UTF-16BE encoding.
        try:
            obj_sorted = sorted(obj.items(), key=lambda kv: kv[0].encode("utf-16be"))
        except AttributeError:
            raise CanonicalizationError("object keys must be strings")
        sink.write(b"{")
        for idx, (key, value) in enumerate(obj_sorted):
            if idx > 0:
                sink.write(b",")
            _serialize_str(key, sink)
            sink.write(b":")
            dump(value, sink)
        sink.write(b"}")
    else:
        raise CanonicalizationError(f"unsupported type: {type(obj)}")
