"""Tests for md_automation.frames frame-spec parsing (no MDAnalysis needed)."""

import pytest

from md_automation.frames import parse_frames


def test_first_last():
    assert parse_frames("first,last", 334) == [0, 333]


def test_single_frame_trajectory():
    assert parse_frames("first,last", 1) == [0]


def test_explicit_indices():
    assert parse_frames("0,50,333", 334) == [0, 50, 333]


def test_every():
    assert parse_frames("every:100", 334) == [0, 100, 200, 300]
    with pytest.raises(ValueError):
        parse_frames("every:0", 10)


def test_quarters():
    assert parse_frames("quarters", 334) == [0, 111, 222, 333]
    assert parse_frames("quarters", 4) == [0, 1, 2, 3]


def test_range():
    assert parse_frames("range:5-8", 334) == [5, 6, 7, 8]
    # clamped to the last frame
    assert parse_frames("range:330-400", 334) == [330, 331, 332, 333]


def test_all_and_dedup():
    assert parse_frames("all", 4) == [0, 1, 2, 3]
    assert parse_frames("first,0,last", 10) == [0, 9]


def test_combined():
    assert parse_frames("first,last,range:5-6", 100) == [0, 5, 6, 99]


def test_errors():
    with pytest.raises(ValueError):
        parse_frames("first,last", 0)
    with pytest.raises(ValueError):
        parse_frames("999", 100)
    with pytest.raises(ValueError):
        parse_frames("bogus", 100)
    with pytest.raises(ValueError):
        parse_frames("range:1-x", 100)
