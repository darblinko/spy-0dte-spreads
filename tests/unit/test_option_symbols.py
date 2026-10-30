"""Tests for OCC option symbol encoding and decoding."""

from datetime import date

import pytest

from common.option_symbols import OptionContract, build_option_symbol, parse_option_symbol


class TestBuildOptionSymbol:
    def test_strike_is_encoded_in_thousandths(self):
        symbol = build_option_symbol("SPY", date(2026, 4, 4), "put", 520.0)
        assert symbol == "SPY260404P00520000"

    def test_fractional_strike(self):
        symbol = build_option_symbol("SPY", date(2026, 4, 4), "call", 520.5)
        assert symbol == "SPY260404C00520500"

    def test_unknown_option_type_raises(self):
        with pytest.raises(ValueError):
            build_option_symbol("SPY", date(2026, 4, 4), "straddle", 520.0)

    def test_non_positive_strike_raises(self):
        with pytest.raises(ValueError):
            build_option_symbol("SPY", date(2026, 4, 4), "put", 0.0)


class TestParseOptionSymbol:
    def test_strike_is_decoded_from_thousandths(self):
        contract = parse_option_symbol("SPY260404P00520000")
        assert contract == OptionContract("SPY", date(2026, 4, 4), "put", 520.0)

    def test_call(self):
        assert parse_option_symbol("SPY260404C00530500").option_type == "call"

    def test_malformed_symbol_raises(self):
        with pytest.raises(ValueError):
            parse_option_symbol("SPY260404P520")

    @pytest.mark.parametrize("strike", [1.0, 480.0, 520.0, 520.5, 600.0])
    @pytest.mark.parametrize("option_type", ["put", "call"])
    def test_round_trip(self, strike, option_type):
        expiration = date(2026, 10, 30)
        symbol = build_option_symbol("SPY", expiration, option_type, strike)
        assert parse_option_symbol(symbol) == OptionContract("SPY", expiration, option_type, strike)
