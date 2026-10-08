"""Unit tests for the natural-language question layer."""

from __future__ import annotations

from app.matching import MatchResult
from app.nlq import answer_question


def _result(day: str, status: str) -> MatchResult:
    return MatchResult(status=status, day=day)


def all_results() -> list[MatchResult]:
    return [
        # Week of 2026-03-02 (Mon-Wed): one missed session.
        _result("2026-03-02", "missed"),
        _result("2026-03-03", "completed"),
        _result("2026-03-04", "completed"),
        # Week of 2026-03-09 (Mon-Tue): a perfect week.
        _result("2026-03-09", "completed"),
        _result("2026-03-10", "completed"),
    ]


CONTEXT = {
    "2026-03-02": {"sleep_hours": 5.5, "resting_hr": 58},
    "2026-03-03": {"sleep_hours": 6.0, "resting_hr": 56},
    "2026-03-04": {"sleep_hours": 7.0, "resting_hr": 54},
    "2026-03-09": {"sleep_hours": 8.0, "resting_hr": 48},
    "2026-03-10": {"sleep_hours": 7.8, "resting_hr": 47},
}


class TestMetricQuestions:
    def test_sleep_in_missed_weeks(self):
        out = answer_question(
            "How did my sleep look in weeks I missed a session?", all_results(), CONTEXT
        )
        assert out["understood"] is True
        assert out["data"]["metric"] == "sleep_hours"
        assert out["data"]["status_filter"] == "missed"
        assert out["data"]["value"] == round((5.5 + 6.0 + 7.0) / 3, 2)
        assert out["data"]["n_days"] == 3

    def test_resting_hr_in_perfect_weeks(self):
        out = answer_question(
            "What was my resting heart rate in weeks I completed everything?",
            all_results(),
            CONTEXT,
        )
        assert out["data"]["metric"] == "resting_hr"
        assert out["data"]["status_filter"] == "perfect"
        assert out["data"]["value"] == round((48 + 47) / 2, 2)
        assert out["data"]["n_days"] == 2

    def test_metric_overall_when_no_status_given(self):
        context = {**CONTEXT, "2026-03-02": {**CONTEXT["2026-03-02"], "hrv_overnight": 40}}
        out = answer_question("What's my overnight HRV like overall?", all_results(), context)
        assert out["data"]["metric"] == "hrv_overnight"
        assert out["data"]["status_filter"] is None
        assert out["data"]["n_days"] == 1

    def test_no_data_for_metric_in_range(self):
        out = answer_question(
            "How did my sleep look in weeks I missed a session?", all_results(), {}
        )
        assert out["understood"] is True
        assert out["data"]["value"] is None
        assert "No sleep data" in out["text"]


class TestCountQuestions:
    def test_how_many_sessions_missed(self):
        out = answer_question("How many sessions did I miss?", all_results(), CONTEXT)
        assert out["understood"] is True
        assert out["data"]["count"] == 1

    def test_how_many_unplanned(self):
        results = all_results() + [_result("2026-03-11", "unplanned")]
        out = answer_question("How many unplanned workouts were there?", results, CONTEXT)
        assert out["data"]["count"] == 1


class TestAdherenceQuestion:
    def test_adherence_summary(self):
        out = answer_question("What's my adherence?", all_results(), CONTEXT)
        assert out["understood"] is True
        assert "summary" in out["data"]


class TestUnrecognised:
    def test_unknown_question_offers_suggestions(self):
        out = answer_question("What's the weather like?", all_results(), CONTEXT)
        assert out["understood"] is False
        assert out["data"]["suggestions"]

    def test_empty_question(self):
        out = answer_question("", all_results(), CONTEXT)
        assert out["understood"] is False
