"""
Unit tests for the Phase 5 statistics helper (backend/stats.py): verifies
the Wilson score interval and two-proportion difference CI against
hand-derived reference calculations BEFORE trusting the math against real
pipeline data, per the phase spec's explicit Verify requirement.

Run with pytest, or directly via `python3 -m tests.test_stats`.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from stats import wilson_ci, two_proportion_diff_ci


def test_wilson_ci_matches_hand_derived_reference():
    """successes=8, n=10, z=1.96 — worked by hand in stats.py's docstring:
        p = 0.8
        denom = 1 + 1.96^2/10 = 1.38416
        center = (0.8 + 1.96^2/20) / 1.38416 = 0.716775...
        half_width = 1.96*sqrt(0.8*0.2/10 + 1.96^2/400) / 1.38416 = 0.226622...
        -> (0.490157, 0.943319) [precisely: 0.49015684672072335, 0.9433190520193067]
    """
    lower, upper = wilson_ci(8, 10)
    assert math.isclose(lower, 0.490157, abs_tol=1e-5)
    assert math.isclose(upper, 0.943319, abs_tol=1e-5)


def test_wilson_ci_bounds_are_always_within_zero_one():
    """The whole point of Wilson over the naive normal approximation: never
    produces a bound outside [0, 1], even at extreme proportions/small n."""
    for successes, n in [(0, 5), (5, 5), (1, 1), (0, 1), (100, 100), (1, 1000)]:
        lower, upper = wilson_ci(successes, n)
        assert 0.0 <= lower <= 1.0
        assert 0.0 <= upper <= 1.0
        assert lower <= upper


def test_wilson_ci_zero_n_returns_zero_zero():
    assert wilson_ci(0, 0) == (0.0, 0.0)


def test_wilson_ci_widens_as_n_shrinks():
    """Same observed proportion (50%), smaller sample -> wider interval —
    the basic sanity property a confidence interval must have."""
    lo_large, hi_large = wilson_ci(50, 100)
    lo_small, hi_small = wilson_ci(5, 10)
    assert (hi_large - lo_large) < (hi_small - lo_small)


def test_two_proportion_diff_ci_matches_hand_derived_reference():
    """p_a=0.50 (n_a=100), p_b=0.60 (n_b=100), z=1.96 — worked by hand in
    stats.py's docstring:
        se = sqrt(0.5*0.5/100 + 0.6*0.4/100) = sqrt(0.0025+0.0024) = 0.07
        diff = 0.10, margin = 1.96*0.07 = 0.1372
        -> point_estimate=0.10, ci_95=[-0.0372, 0.2372]
    Interval includes zero here, so NOT significant at 95%."""
    result = two_proportion_diff_ci(50, 100, 60, 100)
    assert math.isclose(result["point_estimate"], 0.10, abs_tol=1e-9)
    assert math.isclose(result["ci_95"][0], -0.0372, abs_tol=1e-4)
    assert math.isclose(result["ci_95"][1], 0.2372, abs_tol=1e-4)
    assert result["significant"] is False


def test_two_proportion_diff_ci_significant_when_interval_excludes_zero():
    """A large, clearly-real difference at a reasonably large sample size
    must produce an interval that excludes zero."""
    result = two_proportion_diff_ci(200, 1000, 500, 1000)  # 20% vs 50%
    assert result["ci_95"][0] > 0
    assert result["significant"] is True


def test_two_proportion_diff_ci_zero_n_returns_none():
    result = two_proportion_diff_ci(0, 0, 5, 10)
    assert result["point_estimate"] is None
    assert result["ci_95"] == [None, None]
    assert result["significant"] is None


def test_two_proportion_diff_ci_shrinks_as_n_grows():
    """Same two proportions (30% vs 40%), 10x the sample size -> narrower
    interval — spot-checks the exact property named in the phase spec's
    Verify block (compare a larger category's N against a smaller one)."""
    small = two_proportion_diff_ci(30, 100, 40, 100)
    large = two_proportion_diff_ci(300, 1000, 400, 1000)
    small_width = small["ci_95"][1] - small["ci_95"][0]
    large_width = large["ci_95"][1] - large["ci_95"][0]
    assert large_width < small_width
    # point estimate must be identical (same underlying proportions) — only
    # the interval width should change with N.
    assert math.isclose(small["point_estimate"], large["point_estimate"], abs_tol=1e-9)


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASSED: {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAILED: {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    if failed:
        sys.exit(1)
