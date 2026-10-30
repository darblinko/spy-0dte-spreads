"""OCC option symbols: root, YYMMDD expiration, C or P, then the strike times 1,000 in 8 digits.

Example: SPY260404P00520000 is the SPY put with strike 520 expiring 2026-04-04.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

_SYMBOL_PATTERN = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")
_TYPE_TO_CHAR = {"call": "C", "put": "P"}
_CHAR_TO_TYPE = {"C": "call", "P": "put"}
_STRIKE_SCALE = 1000
_MAX_SCALED_STRIKE = 10**8 - 1


@dataclass(frozen=True)
class OptionContract:
    underlying: str
    expiration: date
    option_type: str  # "call" or "put"
    strike: float


def build_option_symbol(underlying: str, expiration: date, option_type: str, strike: float) -> str:
    """Encode a contract as an OCC symbol."""
    if option_type not in _TYPE_TO_CHAR:
        raise ValueError(f"Unknown option type: {option_type!r}")
    scaled_strike = round(strike * _STRIKE_SCALE)
    if not 0 < scaled_strike <= _MAX_SCALED_STRIKE:
        raise ValueError(f"Strike out of range for an OCC symbol: {strike}")
    type_char = _TYPE_TO_CHAR[option_type]
    return f"{underlying}{expiration:%y%m%d}{type_char}{scaled_strike:08d}"


def parse_option_symbol(symbol: str) -> OptionContract:
    """Decode an OCC symbol. Raises ValueError for a malformed symbol."""
    match = _SYMBOL_PATTERN.match(symbol)
    if match is None:
        raise ValueError(f"Not an OCC option symbol: {symbol!r}")
    underlying, expiration_text, type_char, strike_text = match.groups()
    return OptionContract(
        underlying=underlying,
        expiration=datetime.strptime(expiration_text, "%y%m%d").date(),
        option_type=_CHAR_TO_TYPE[type_char],
        strike=int(strike_text) / _STRIKE_SCALE,
    )
