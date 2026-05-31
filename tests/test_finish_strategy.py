import logging

import pytest

from birdbuddy.sightings import SightingFinishStrategy


def test_missing_value_returns_mystery(caplog):
    with caplog.at_level(logging.WARNING, logger="birdbuddy"):
        result = SightingFinishStrategy("nonexistent_strategy")
    assert result is SightingFinishStrategy.MYSTERY
    assert "Unexpected SightingFinishStrategy" in caplog.text


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("recognized", SightingFinishStrategy.RECOGNIZED),
        ("best_guess", SightingFinishStrategy.BEST_GUESS),
        ("mystery", SightingFinishStrategy.MYSTERY),
    ],
)
def test_known_values_still_resolve(value, expected):
    assert SightingFinishStrategy(value) is expected
