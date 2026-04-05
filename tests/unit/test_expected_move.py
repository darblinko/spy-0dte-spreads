"""Tests for expected move calculation."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from market.expected_move import (
    compute_expected_move,
    get_expected_range,
    is_strike_outside_expected_move,
)


class TestComputeExpectedMove:
    def test_basic_calculation(self):
        # ATM call=$2.00, ATM put=$2.00, straddle=$4.00
        # Expected move = $4.00 * 0.85 = $3.40
        result = compute_expected_move(2.00, 2.00)
        assert result == pytest.approx(3.40)

    def test_asymmetric_straddle(self):
        # Call=$2.50, Put=$1.50, straddle=$4.00
        result = compute_expected_move(2.50, 1.50)
        assert result == pytest.approx(3.40)

    def test_custom_multiplier(self):
        result = compute_expected_move(2.00, 2.00, multiplier=1.0)
        assert result == pytest.approx(4.00)

    def test_zero_prices(self):
        result = compute_expected_move(0.0, 0.0)
        assert result == 0.0


class TestGetExpectedRange:
    def test_basic_range(self):
        lower, upper = get_expected_range(525.0, 3.40)
        assert lower == pytest.approx(521.60)
        assert upper == pytest.approx(528.40)

    def test_zero_move(self):
        lower, upper = get_expected_range(525.0, 0.0)
        assert lower == 525.0
        assert upper == 525.0


class TestIsStrikeOutsideExpectedMove:
    def test_put_outside(self):
        # SPY=525, expected_move=3.40, lower bound=521.60
        # Put at 521 is outside
        assert is_strike_outside_expected_move(521.0, 525.0, 3.40, "put") is True

    def test_put_inside(self):
        # Put at 523 is inside expected move
        assert is_strike_outside_expected_move(523.0, 525.0, 3.40, "put") is False

    def test_call_outside(self):
        # Upper bound = 528.40, call at 529 is outside
        assert is_strike_outside_expected_move(529.0, 525.0, 3.40, "call") is True

    def test_call_inside(self):
        # Call at 527 is inside expected move
        assert is_strike_outside_expected_move(527.0, 525.0, 3.40, "call") is False

    def test_put_at_boundary(self):
        # Put exactly at lower bound = not outside
        assert is_strike_outside_expected_move(521.60, 525.0, 3.40, "put") is False

    def test_call_at_boundary(self):
        # Call exactly at upper bound = not outside
        assert is_strike_outside_expected_move(528.40, 525.0, 3.40, "call") is False
