"""Variant identification for One Piece TCG listings.

The exploitable fact this module encodes: one card number can carry several
variants spanning a 50x+ price range, and listing titles routinely fail to
disambiguate them. We therefore key on the *card number* (sellers copy it off
the card, so it is reliable) and treat the variant as a latent variable
inferred from whatever descriptors the title happens to carry.

Two outputs matter downstream and they are deliberately kept separate:

  * ``posterior``  -- distribution over candidate variants
  * ``ambiguity``  -- normalised Shannon entropy of that posterior

Ambiguity is a property of the *listing text*. Cheapness (how far the ask sits
below the trailing median for the most likely variant) is a property of the
*price*. Conflating them was the bug in the earlier prototype: a correctly
labelled but underpriced card scores zero ambiguity and is still an
opportunity. Callers must rank on ``max(ambiguity_ev, cheapness_ev)``, never a
product. This module supplies only the ambiguity half.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum

# --------------------------------------------------------------------------
# Card numbers
# --------------------------------------------------------------------------

# OPTCG collector numbers: OP##-###, ST##-###, EB##-###, PRB##-### and P-###
# promos. Sellers transcribe these with assorted spacing, so tolerate it.
_SET_CARD_RE = re.compile(
    r"\b(OP|ST|EB|PRB)\s*[-‐-―]?\s*(\d{1,2})\s*[-‐-―]\s*(\d{3})\b",
    re.IGNORECASE,
)
_PROMO_RE = re.compile(r"\bP\s*[-‐-―]\s*(\d{3})\b", re.IGNORECASE)


def extract_card_numbers(text: str) -> list[str]:
    """Return normalised card numbers found in ``text``, in order, deduped.

    Normal form is upper-case with a two-digit set index: ``OP06-118``.
    """
    if not text:
        return []

    found: list[str] = []
    for match in _SET_CARD_RE.finditer(text):
        prefix, set_no, card_no = match.groups()
        found.append(f"{prefix.upper()}{int(set_no):02d}-{card_no}")
    for match in _PROMO_RE.finditer(text):
        found.append(f"P-{match.group(1)}")

    seen: set[str] = set()
    ordered: list[str] = []
    for number in found:
        if number not in seen:
            seen.add(number)
            ordered.append(number)
    return ordered


# --------------------------------------------------------------------------
# Variant axes
# --------------------------------------------------------------------------


class Treatment(str, Enum):
    """Print treatment, ascending in desirability.

    A star above the rarity code marks a parallel. The tiers above that are
    distinguished by art style rather than by the star alone.
    """

    BASE = "base"
    PARALLEL = "parallel"
    SP = "sp"  # Special Rare
    MANGA = "manga"  # black-and-white Oda manga panels
    COMIC = "comic"  # comic / red parallel


class Channel(str, Enum):
    """Distribution channel."""

    BOOSTER = "booster"
    PROMO = "promo"
    PRIZE = "prize"
    SERIAL = "serial"  # gold stamp / printed serial number winner cards


class Region(str, Enum):
    JP = "JP"
    EN = "EN"
    CN = "CN"
    KR = "KR"
    FR = "FR"


@dataclass(frozen=True)
class Variant:
    """One concrete printing of a card number."""

    card_number: str
    base_rarity: str  # C, UC, R, L, SR, SEC
    treatment: Treatment
    channel: Channel
    region: Region
    # Relative population prior. Higher = more copies in circulation. These are
    # supplied by the operator from print-run knowledge; they are NOT prices and
    # are NOT inferred from market data.
    prior_weight: float = 1.0
    label: str = ""

    @property
    def key(self) -> str:
        return (
            f"{self.card_number}|{self.region.value}|{self.base_rarity}"
            f"|{self.treatment.value}|{self.channel.value}"
        )

    def describe(self) -> str:
        if self.label:
            return self.label
        bits = [self.card_number, self.region.value, self.base_rarity]
        if self.treatment is not Treatment.BASE:
            bits.append(self.treatment.value)
        if self.channel is not Channel.BOOSTER:
            bits.append(self.channel.value)
        return " ".join(bits)


# --------------------------------------------------------------------------
# Lexical evidence
# --------------------------------------------------------------------------

# Each feature is a detector over the listing title/specifics. Features are
# deliberately coarse and explainable -- when this misfires we want to see why.
_FEATURES: dict[str, re.Pattern[str]] = {
    # "Super Parallel" is the trap. Japanese sellers use it for the MOST
    # valuable tier; read as English it sounds like a lesser "parallel". It is
    # matched before the generic parallel detector and weighted accordingly.
    "super_parallel": re.compile(
        r"(super\s*para|スーパーパラレル)", re.I
    ),
    "comic": re.compile(
        r"(comic|red\s*para|レッドパラレル)", re.I
    ),
    "manga": re.compile(
        r"(manga|マンガ|black\s*(and|&)?\s*white|b\s*&\s*w)", re.I
    ),
    "sp": re.compile(r"(\bsp\b|special\s*rare)", re.I),
    "parallel": re.compile(
        r"(parallel|パラレル|alt(ernate)?\s*art|\balt\b)", re.I
    ),
    "sec": re.compile(r"\bsec(ret)?\b", re.I),
    "sr": re.compile(r"\bsr\b", re.I),
    "leader": re.compile(r"(\bl\b|leader)", re.I),
    "serial": re.compile(r"(serial|\bwinner\b|優勝|\d{1,3}\s*/\s*\d{2,4})", re.I),
    "stamp": re.compile(r"(gold\s*stamp|champion|winner\s*stamp|\bstamp(ed)?\b)", re.I),
    "graded": re.compile(r"(\bpsa\b|\bbgs\b|\bcgc\b|\bace\b|slab|graded|gem\s*mt)", re.I),
    "region_jp": re.compile(r"(japan|japanese|\bjp\b|日本)", re.I),
    "region_en": re.compile(r"(english|\ben\b|\beng\b)", re.I),
}

# Likelihood ratios: feature -> treatment -> multiplier on the prior.
# 1.0 means uninformative. Values are odds multipliers, not probabilities.
_TREATMENT_LR: dict[str, dict[Treatment, float]] = {
    # Any explicit parallel-family descriptor makes a plain base printing very
    # unlikely. All such features suppress BASE identically -- an inconsistency
    # here silently leaves the cheap reading alive on strong evidence.
    "super_parallel": {
        Treatment.BASE: 0.02,
        Treatment.PARALLEL: 0.6,
        Treatment.SP: 1.5,
        Treatment.MANGA: 6.0,
        Treatment.COMIC: 8.0,
    },
    "comic": {
        Treatment.BASE: 0.02,
        Treatment.PARALLEL: 0.2,
        Treatment.SP: 0.3,
        Treatment.MANGA: 1.0,
        Treatment.COMIC: 25.0,
    },
    "manga": {
        Treatment.BASE: 0.02,
        Treatment.PARALLEL: 0.3,
        Treatment.SP: 0.5,
        Treatment.MANGA: 20.0,
        Treatment.COMIC: 2.0,
    },
    "sp": {
        Treatment.BASE: 0.1,
        Treatment.PARALLEL: 0.8,
        Treatment.SP: 8.0,
        Treatment.MANGA: 1.0,
        Treatment.COMIC: 0.8,
    },
    "parallel": {
        Treatment.BASE: 0.1,
        Treatment.PARALLEL: 4.0,
        Treatment.SP: 2.0,
        Treatment.MANGA: 2.0,
        Treatment.COMIC: 2.0,
    },
}

_CHANNEL_LR: dict[str, dict[Channel, float]] = {
    "serial": {
        Channel.BOOSTER: 0.05,
        Channel.PROMO: 0.3,
        Channel.PRIZE: 3.0,
        Channel.SERIAL: 25.0,
    },
    "stamp": {
        Channel.BOOSTER: 0.2,
        Channel.PROMO: 1.0,
        Channel.PRIZE: 6.0,
        Channel.SERIAL: 6.0,
    },
}

_REGION_LR: dict[str, dict[Region, float]] = {
    "region_jp": {Region.JP: 6.0, Region.EN: 0.2, Region.CN: 1.0, Region.KR: 1.0, Region.FR: 0.2},
    "region_en": {Region.JP: 0.2, Region.EN: 6.0, Region.CN: 0.5, Region.KR: 0.5, Region.FR: 0.5},
}

_RARITY_LR: dict[str, dict[str, float]] = {
    "sec": {"SEC": 6.0, "SR": 0.5, "L": 0.3, "R": 0.2, "UC": 0.1, "C": 0.1},
    "sr": {"SEC": 0.6, "SR": 6.0, "L": 0.5, "R": 0.3, "UC": 0.1, "C": 0.1},
    "leader": {"SEC": 0.3, "SR": 0.4, "L": 6.0, "R": 0.3, "UC": 0.2, "C": 0.2},
}


def detect_features(text: str) -> set[str]:
    """Return the set of evidence features present in ``text``.

    ``super_parallel`` suppresses the generic ``parallel`` feature so the two do
    not double-count the same substring.
    """
    if not text:
        return set()
    present = {name for name, pattern in _FEATURES.items() if pattern.search(text)}
    if "super_parallel" in present:
        present.discard("parallel")
    return present


@dataclass
class VariantPosterior:
    """Inference result for one listing."""

    card_number: str
    probabilities: dict[str, float]
    variants: dict[str, Variant]
    features: set[str] = field(default_factory=set)
    priors: dict[str, float] = field(default_factory=dict)

    @property
    def most_likely(self) -> Variant:
        key = max(self.probabilities, key=lambda k: self.probabilities[k])
        return self.variants[key]

    @property
    def confidence(self) -> float:
        return max(self.probabilities.values()) if self.probabilities else 0.0

    @property
    def ambiguity(self) -> float:
        """Normalised Shannon entropy in [0, 1].

        0.0 means the title pins the variant exactly. 1.0 means the title is
        uninformative and every candidate remains equally live -- the "no
        variant descriptor at all" case that produces the deepest mispricings.
        """
        n = len(self.probabilities)
        if n <= 1:
            return 0.0
        entropy = -sum(p * math.log(p) for p in self.probabilities.values() if p > 0)
        return entropy / math.log(n)

    @property
    def information_gain(self) -> float:
        """How much the listing text moved us off the population prior, in [0, 1].

        KL(posterior || prior), normalised. 0.0 means the text told us nothing.

        This exists because posterior entropy alone is *not* a sufficient
        ambiguity measure. When the prior is concentrated -- as it always is,
        since plain printings vastly outnumber comic parallels -- an
        uninformative title yields a concentrated posterior and therefore low
        entropy, making a title that says nothing look confidently identified.
        That is exactly backwards.

        Use ``ambiguity`` for the spread of the variant posterior, and this for
        whether the seller actually disambiguated anything. A listing with low
        information gain over a prior spanning a 50x price range is the deep
        mispricing candidate, whatever its entropy.
        """
        if not self.priors or len(self.probabilities) <= 1:
            return 0.0
        divergence = sum(
            p * math.log(p / self.priors[key])
            for key, p in self.probabilities.items()
            if p > 0 and self.priors.get(key, 0) > 0
        )
        return max(0.0, min(divergence / math.log(len(self.probabilities)), 1.0))

    @property
    def is_undescribed(self) -> bool:
        """True when the text carries no variant-determining descriptor at all.

        The June sale at $30.64 under a title with no variant descriptor is this
        case. It is the highest-value signal in the system and it is detected by
        absence of evidence, not by entropy.
        """
        return not (self.features - {"graded", "region_jp", "region_en"})

    def price_dispersion(self, reference_prices: dict[str, float]) -> float:
        """Max/min reference price across variants still live above 1% posterior.

        The honest measure of what ambiguity is worth: a title that fails to
        distinguish two variants priced 50x apart carries real risk and real
        opportunity; one that fails to distinguish two near-identically priced
        variants carries neither.
        """
        live = [
            reference_prices[key]
            for key, p in self.probabilities.items()
            if p > 0.01 and key in reference_prices and reference_prices[key] > 0
        ]
        if len(live) < 2:
            return 1.0
        return max(live) / min(live)

    def is_graded(self) -> bool:
        return "graded" in self.features


def infer_variant(text: str, candidates: list[Variant]) -> VariantPosterior:
    """Infer a posterior over ``candidates`` from listing ``text``.

    Naive Bayes over independent evidence features. With no features present the
    posterior collapses to the population prior, which is the correct behaviour:
    a bare title tells us nothing beyond which printings exist.
    """
    if not candidates:
        raise ValueError("infer_variant requires at least one candidate variant")

    features = detect_features(text)
    scores: dict[str, float] = {}

    for variant in candidates:
        score = max(variant.prior_weight, 1e-9)
        for feature in features:
            score *= _TREATMENT_LR.get(feature, {}).get(variant.treatment, 1.0)
            score *= _CHANNEL_LR.get(feature, {}).get(variant.channel, 1.0)
            score *= _REGION_LR.get(feature, {}).get(variant.region, 1.0)
            score *= _RARITY_LR.get(feature, {}).get(variant.base_rarity, 1.0)
        scores[variant.key] = score

    prior_total = sum(max(v.prior_weight, 1e-9) for v in candidates)
    priors = {v.key: max(v.prior_weight, 1e-9) / prior_total for v in candidates}

    total = sum(scores.values())
    if total <= 0:
        uniform = 1.0 / len(candidates)
        probabilities = {v.key: uniform for v in candidates}
    else:
        probabilities = {key: score / total for key, score in scores.items()}

    return VariantPosterior(
        card_number=candidates[0].card_number,
        probabilities=probabilities,
        variants={v.key: v for v in candidates},
        features=features,
        priors=priors,
    )
