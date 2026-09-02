"""
Phase 5 (feature-completion loop): small, dependency-free statistics
helpers for the baseline-vs-agent comparison. No scipy — this project
doesn't have it as a dependency, and a normal-approximation two-proportion
z-test needs nothing more than sqrt/log from the standard library.

CHOICE OF METRIC (documented per the phase spec's explicit request): the
95% CI computed here is on the RECOVERY-RATE delta (a difference of two
proportions — "what fraction of eligible records got recovered"), NOT the
dollar-amount delta.

Why: a proportion difference has a simple, standard, textbook closed-form
variance — p(1-p)/n on each side, summed for the difference (the normal-
approximation two-proportion z-test used below). A dollar-amount delta is a
difference of two SUMS of unequal, right-skewed amounts; its variance
depends on the full per-record amount distribution (not just a count), so a
correct CI for it would need either the individual recovered amounts (a
t-test / bootstrap over those values) or a substantially more involved
calculation — approximating it with proportion-CI math would silently give
a wrong (too narrow, usually) interval. Recovery rate is therefore both the
simpler AND the statistically correct choice given what this module
implements; the dollar-amount delta continues to be reported as a plain
point estimate elsewhere in the API, without a CI, rather than attaching a
CI to it that isn't actually valid.
"""

import math


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a single proportion (95% by default, via
    z=1.96). More accurate than the naive p +/- z*sqrt(p(1-p)/n) normal
    approximation for small n or proportions near 0/1 — it's derived by
    inverting the score test rather than assuming a symmetric normal
    interval, so it never produces bounds outside [0, 1] the way the naive
    approximation can. Returns (lower, upper), both clamped to [0, 1].

    Reference check (hand-derivable, used in this module's unit test):
    successes=8, n=10, z=1.96 -> (lower, upper) approx (0.490157, 0.943319).
    """
    if n <= 0:
        return (0.0, 0.0)
    p = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half_width = (z * math.sqrt((p * (1 - p) / n) + (z2 / (4 * n * n)))) / denom
    return (max(0.0, center - half_width), min(1.0, center + half_width))


def two_proportion_diff_ci(successes_a: int, n_a: int, successes_b: int, n_b: int, z: float = 1.96) -> dict:
    """95% (default, via z=1.96) CI for (p_b - p_a) using the standard
    normal-approximation two-proportion z-test with UNPOOLED variance —
    appropriate here since we're constructing a confidence interval for the
    difference itself, not testing a null hypothesis that the two
    proportions are equal (which would use pooled variance instead).

    In this project, a is the BASELINE recovery rate and b is the AGENT
    recovery rate, so a positive point_estimate means the agent outperformed
    the baseline.

    Returns a dict: point_estimate (p_b - p_a, or None if either n is 0),
    ci_95 ([lower, upper], or [None, None]), and significant (True if the
    interval excludes zero, i.e. a defensible "statistically significant at
    this sample size and confidence level" claim; False if it doesn't;
    None if undefined).

    Reference check (hand-derivable, used in this module's unit test):
    p_a=0.50 (n_a=100), p_b=0.60 (n_b=100), z=1.96 ->
    point_estimate=0.10, ci_95 approx [-0.0372, 0.2372].
    """
    if n_a <= 0 or n_b <= 0:
        return {"point_estimate": None, "ci_95": [None, None], "significant": None}
    p_a = successes_a / n_a
    p_b = successes_b / n_b
    diff = p_b - p_a
    se = math.sqrt((p_a * (1 - p_a) / n_a) + (p_b * (1 - p_b) / n_b))
    margin = z * se
    lower, upper = diff - margin, diff + margin
    significant = (lower > 0) or (upper < 0)
    return {"point_estimate": diff, "ci_95": [lower, upper], "significant": significant}
