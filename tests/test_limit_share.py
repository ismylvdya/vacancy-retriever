"""Statistics → Runs «measured % of 5h»: from the 5-hour limit Claude reports after every call."""

from datetime import UTC, datetime

from vacancy_retriever.web.views import measured_limit_share

START = datetime(2026, 10, 31, 8, 0, tzinfo=UTC)
T = int(START.timestamp())


def call(i, five, resets):
    return {"id": i, "five_hour": five, "five_hour_resets_at": resets}


def test_window_known_before_the_run():
    earlier = [call(1, 0.30, T + 3600)]
    assert measured_limit_share([call(2, 0.33, T + 3600), call(3, 0.41, T + 3600)], earlier, START) == (11.0, False)


def test_window_opened_by_the_run_starts_at_zero():
    assert measured_limit_share([call(1, 0.02, T + 5 * 3600), call(2, 0.07, T + 5 * 3600)], [], START) == (7.0, False)


def test_window_reset_during_the_run_adds_both_parts():
    calls = [call(2, 0.90, T + 600), call(3, 0.95, T + 600), call(4, 0.03, T + 600 + 5 * 3600)]
    assert measured_limit_share(calls, [call(1, 0.88, T + 600)], START) == (10.0, False)


def test_unknown_start_is_a_lower_bound_and_no_data_is_none():
    assert measured_limit_share([call(1, 0.50, T + 3600), call(2, 0.55, T + 3600)], [], START) == (5.0, True)
    assert measured_limit_share([{"id": 1, "five_hour": None, "five_hour_resets_at": None}], [], START) is None
