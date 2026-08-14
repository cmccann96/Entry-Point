"""Tests for card-number extraction and variant inference.

Titles here are constructed fixtures exercising parser behaviour. They are not
market data and no price conclusions are drawn from them.
"""

from __future__ import annotations

import pytest

from optcg.variants import (
    Channel,
    Region,
    Treatment,
    Variant,
    detect_features,
    extract_card_numbers,
    infer_variant,
)


def _variants(card: str = "OP06-118") -> list[Variant]:
    return [
        Variant(card, "SEC", Treatment.BASE, Channel.BOOSTER, Region.JP, 100.0, "plain SEC"),
        Variant(card, "SEC", Treatment.PARALLEL, Channel.BOOSTER, Region.JP, 30.0, "parallel"),
        Variant(card, "SEC", Treatment.MANGA, Channel.BOOSTER, Region.JP, 8.0, "manga"),
        Variant(card, "SEC", Treatment.COMIC, Channel.BOOSTER, Region.JP, 2.0, "comic"),
    ]


class TestCardNumberExtraction:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("One Piece OP06-118 Zoro", ["OP06-118"]),
            ("op06-118 zoro manga", ["OP06-118"]),
            ("OP 06-118 Roronoa", ["OP06-118"]),
            ("ST01-001 Luffy Leader", ["ST01-001"]),
            ("EB01-006 alt art", ["EB01-006"]),
            ("PRB01-012 parallel", ["PRB01-012"]),
            ("Promo P-001 tournament", ["P-001"]),
            ("OP6-118 shorthand", ["OP06-118"]),
        ],
    )
    def test_extracts_and_normalises(self, text, expected):
        assert extract_card_numbers(text) == expected

    def test_multiple_numbers_deduped_in_order(self):
        text = "Bundle OP06-118 and ST01-001 plus another OP06-118"
        assert extract_card_numbers(text) == ["OP06-118", "ST01-001"]

    def test_no_number_returns_empty(self):
        assert extract_card_numbers("Roronoa Zoro manga rare japanese") == []

    def test_empty_input(self):
        assert extract_card_numbers("") == []


class TestFeatureDetection:
    def test_super_parallel_suppresses_generic_parallel(self):
        # "Super Parallel" is the top tier. It must not also fire the generic
        # 'parallel' feature, which would double-count the same substring.
        features = detect_features("Zoro Super Parallel OP06-118")
        assert "super_parallel" in features
        assert "parallel" not in features

    def test_generic_parallel_still_fires_alone(self):
        assert "parallel" in detect_features("Zoro Parallel OP06-118")

    def test_japanese_terms(self):
        assert "super_parallel" in detect_features("スーパーパラレル ゾロ")
        assert "comic" in detect_features("レッドパラレル")

    def test_graded_detection(self):
        assert "graded" in detect_features("PSA 10 Zoro OP06-118")
        assert "graded" in detect_features("BGS 9.5 gem mint")

    def test_serial_patterns(self):
        assert "serial" in detect_features("WINNER card 12/100")


class TestVariantInference:
    def test_bare_title_falls_back_to_prior(self):
        # The deepest mispricings come from titles with no variant descriptor.
        # With no evidence the posterior must equal the population prior.
        posterior = infer_variant("One Piece OP06-118", _variants())
        total = 140.0
        assert posterior.probabilities[_variants()[0].key] == pytest.approx(100 / total)
        assert posterior.probabilities[_variants()[3].key] == pytest.approx(2 / total)

    def test_bare_title_carries_no_information(self):
        # Posterior entropy alone does NOT separate these two cases: with a
        # concentrated prior, an uninformative title yields a concentrated
        # posterior and so scores as low-entropy. Information gain is the
        # measure that actually answers "did the seller disambiguate anything".
        bare = infer_variant("One Piece OP06-118", _variants())
        labelled = infer_variant("OP06-118 comic parallel red", _variants())
        assert bare.information_gain == pytest.approx(0.0)
        assert labelled.information_gain > 0.3

    def test_undescribed_titles_are_flagged_by_absence_of_evidence(self):
        # The $30.64 case: no variant descriptor at all.
        assert infer_variant("One Piece OP06-118", _variants()).is_undescribed
        assert infer_variant("OP06-118 japanese", _variants()).is_undescribed
        assert not infer_variant("OP06-118 manga rare", _variants()).is_undescribed

    def test_price_dispersion_measures_what_ambiguity_is_worth(self):
        prices = {v.key: p for v, p in zip(_variants(), [25.0, 70.0, 1098.0, 1500.0])}
        bare = infer_variant("One Piece OP06-118", _variants())
        labelled = infer_variant("OP06-118 comic parallel red", _variants())
        # A bare title leaves a 60x price range live; a labelled one collapses it.
        assert bare.price_dispersion(prices) > 50.0
        assert labelled.price_dispersion(prices) < bare.price_dispersion(prices)

    def test_manga_descriptor_selects_manga(self):
        posterior = infer_variant("OP06-118 Zoro manga rare japanese", _variants())
        assert posterior.most_likely.treatment is Treatment.MANGA

    def test_comic_descriptor_selects_comic(self):
        posterior = infer_variant("OP06-118 Zoro comic parallel", _variants())
        assert posterior.most_likely.treatment is Treatment.COMIC

    def test_super_parallel_is_not_read_as_a_downgrade(self):
        # The specific trap from the brief: a JP seller's "Super Parallel" is the
        # most valuable tier, and must outrank the plain printing decisively.
        posterior = infer_variant("OP06-118 スーパーパラレル", _variants())
        plain = _variants()[0].key
        assert posterior.most_likely.treatment in (Treatment.MANGA, Treatment.COMIC)
        assert posterior.probabilities[plain] < 0.05

    def test_probabilities_sum_to_one(self):
        for title in ["OP06-118", "OP06-118 manga", "OP06-118 SEC parallel PSA 10"]:
            posterior = infer_variant(title, _variants())
            assert sum(posterior.probabilities.values()) == pytest.approx(1.0)

    def test_ambiguity_bounded(self):
        for title in ["", "OP06-118", "OP06-118 comic parallel serial 1/50"]:
            assert 0.0 <= infer_variant(title, _variants()).ambiguity <= 1.0

    def test_single_candidate_has_zero_ambiguity(self):
        posterior = infer_variant("OP06-118", _variants()[:1])
        assert posterior.ambiguity == 0.0
        assert posterior.confidence == pytest.approx(1.0)

    def test_graded_flag_surfaces(self):
        assert infer_variant("OP06-118 PSA 10", _variants()).is_graded()
        assert not infer_variant("OP06-118 raw", _variants()).is_graded()

    def test_requires_candidates(self):
        with pytest.raises(ValueError):
            infer_variant("OP06-118", [])
