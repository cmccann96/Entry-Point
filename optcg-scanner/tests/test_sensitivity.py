"""Tests for the analytic feasibility study."""

from __future__ import annotations

import pytest

from optcg.sensitivity import (
    MeasuredDistribution,
    estimate_opportunities,
    fit_lognormal,
    prob_below_discount,
    required_listing_failure_rate,
    required_probability_for_gate,
)
from optcg.stats import norm_cdf, norm_ppf, quantile

# The operator's measured raw distribution for OP06-118, 2026 YTD.
RAW = MeasuredDistribution(
    label="OP06-118 raw", median=1098.0, q1=960.0, q3=1475.0, cv=0.311, n=13, window_months=7.5
)


class TestStats:
    def test_norm_cdf_known_values(self):
        assert norm_cdf(0.0) == pytest.approx(0.5)
        assert norm_cdf(1.96) == pytest.approx(0.975, abs=1e-3)

    def test_norm_ppf_inverts_cdf(self):
        for p in (0.01, 0.1, 0.25, 0.5, 0.75, 0.99):
            assert norm_cdf(norm_ppf(p)) == pytest.approx(p, abs=1e-6)

    def test_quantile_interpolates(self):
        assert quantile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)


class TestLognormalFit:
    def test_left_and_right_sigmas_disagree(self):
        # The core finding: the measured quartiles are asymmetric in log space,
        # so a single symmetric fit is not defensible.
        fits = fit_lognormal(RAW)
        assert fits.sigma_from_q3 > fits.sigma_from_q1

    def test_left_tail_is_thinner_than_the_cv_implies(self):
        # This is why the headline 31.1% CV overstates the buyable opportunity.
        fits = fit_lognormal(RAW)
        assert fits.sigma_from_q1 < fits.sigma_from_cv

    def test_skew_ratio_flags_asymmetry(self):
        assert fit_lognormal(RAW).skew_ratio > 1.5

    def test_symmetric_distribution_has_matching_sigmas(self):
        symmetric = MeasuredDistribution(
            label="sym", median=1000.0, q1=800.0, q3=1250.0, cv=0.3, n=10, window_months=12
        )
        fits = fit_lognormal(symmetric)
        assert fits.skew_ratio == pytest.approx(1.0, abs=0.01)


class TestOpportunityRate:
    def test_deeper_discounts_are_rarer(self):
        fits = fit_lognormal(RAW)
        shallow = prob_below_discount(fits.sigma_from_q1, 0.20)
        deep = prob_below_discount(fits.sigma_from_q1, 0.50)
        assert shallow > deep

    def test_sales_per_year_annualises_the_window(self):
        assert RAW.sales_per_year == pytest.approx(13 * 12 / 7.5)

    def test_gate_is_not_reached_on_measured_left_tail(self):
        # Reproduces the verdict: on the operator's own numbers the continuous
        # dispersion does not deliver 5 deep-tail events per card-year.
        fits = fit_lognormal(RAW)
        probability = prob_below_discount(fits.sigma_from_q1, 0.30)
        assert probability * RAW.sales_per_year < 5.0

    def test_listing_failure_rate_needed_is_positive(self):
        fits = fit_lognormal(RAW)
        continuous = prob_below_discount(fits.sigma_from_q1, 0.30)
        assert required_listing_failure_rate(RAW, 5.0, continuous) > 0

    def test_required_probability_scales_with_sale_frequency(self):
        assert required_probability_for_gate(RAW, 5.0) > required_probability_for_gate(
            MeasuredDistribution("busy", 1000, 900, 1200, 0.3, 100, 12), 5.0
        )

    def test_estimates_cover_all_channels(self):
        from optcg.friction import SaleChannel

        for est in estimate_opportunities(RAW):
            assert set(est.net_ev_by_channel) == set(SaleChannel)


class TestValidation:
    def test_rejects_inconsistent_quartiles(self):
        with pytest.raises(ValueError):
            MeasuredDistribution("bad", median=100, q1=200, q3=300, cv=0.3, n=10, window_months=12)

    def test_rejects_tiny_sample(self):
        with pytest.raises(ValueError):
            MeasuredDistribution("bad", median=100, q1=90, q3=110, cv=0.3, n=1, window_months=12)

    def test_rejects_bad_discount(self):
        with pytest.raises(ValueError):
            prob_below_discount(0.3, 1.5)
