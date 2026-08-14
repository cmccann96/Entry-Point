"""Measuring the mislabel rate against image ground truth.

This is the real Stage 1 measurement. The strategy does not rest on how often a
title is *blank* -- that was one observation. It rests on how often a title is
*wrong*, and specifically on how often it is wrong in the profitable direction:
the seller claims a lesser variant than the card actually is.

Only an image settles that. So the input here is a sale annotated with a
confirmed variant, established by looking at the photo -- by eye, or later by a
vision model. The four observations that resolve a variant, per the brief:

  * is there a star above the rarity code (parallel or above)
  * is the art manga panels, painted full-bleed, or painted inside a frame
  * is there a gold stamp, WINNER stamp, or printed serial
  * is it slabbed, and if so what grade and grader

Crucially this can be done by hand. Fifty listings eyeballed in an afternoon
gives a usable estimate, with no vision API and no live scanner. That is the
cheapest path to a real answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .power import wilson_interval
from .sources.base import SoldSale
from .stats import median as _median
from .variants import Variant, infer_variant


@dataclass(frozen=True)
class VerifiedSale:
    """A sale whose true variant was confirmed from its image."""

    sale: SoldSale
    true_variant_key: str
    notes: str = ""


@dataclass
class MislabelReport:
    """How often, and in which direction, titles misdescribe the card."""

    total: int = 0
    correct: int = 0
    under_claimed: int = 0  # card is dearer than the title implies -- the edge
    over_claimed: int = 0  # card is cheaper than the title implies -- the trap
    exploitable: int = 0  # under-claimed AND sold below the true variant's median
    under_claim_gaps: list[float] = field(default_factory=list)  # AUD left on the table
    examples: list[tuple[str, str, str, float]] = field(default_factory=list)

    @property
    def mislabel_rate(self) -> float:
        return 0.0 if self.total == 0 else (self.total - self.correct) / self.total

    @property
    def under_claim_rate(self) -> float:
        return 0.0 if self.total == 0 else self.under_claimed / self.total

    @property
    def exploitable_rate(self) -> float:
        """The parameter the strategy actually depends on."""
        return 0.0 if self.total == 0 else self.exploitable / self.total

    @property
    def exploitable_interval(self) -> tuple[float, float]:
        if self.total == 0:
            return (0.0, 1.0)
        return wilson_interval(self.exploitable, self.total)

    @property
    def median_gap_aud(self) -> float:
        """Median value left on the table by an under-claimed listing."""
        return _median(self.under_claim_gaps) if self.under_claim_gaps else 0.0

    def decides_against(self, required_rate: float) -> str:
        low, high = self.exploitable_interval
        if self.total == 0:
            return "NO DATA"
        if high < required_rate:
            return "DECIDES: NO-GO"
        if low > required_rate:
            return "DECIDES: GO"
        return "INCONCLUSIVE"


def measure_mislabel_rate(
    verified: list[VerifiedSale],
    variants: list[Variant],
    reference_prices: dict[str, float],
) -> MislabelReport:
    """Compare what titles claimed against what the images showed.

    ``reference_prices`` maps variant key -> trailing median AUD, and decides
    the *direction* of a mislabel: a card is under-claimed when its true variant
    is dearer than the one its title implied.

    A mislabel is only counted as exploitable when the card additionally sold
    below its true variant's median. A comic parallel described as "parallel"
    that still fetched full comic-parallel money was correctly priced by the
    market despite the bad title -- the bidders saw the photo. Those sales are
    the reason the raw mislabel rate overstates the opportunity.
    """
    report = MislabelReport()

    for entry in verified:
        posterior = infer_variant(entry.sale.title, variants)
        claimed_key = posterior.most_likely.key
        true_key = entry.true_variant_key

        report.total += 1
        if claimed_key == true_key:
            report.correct += 1
            continue

        claimed_price = reference_prices.get(claimed_key)
        true_price = reference_prices.get(true_key)
        if claimed_price is None or true_price is None:
            # Direction is undecidable without both benchmarks. Counted as a
            # mislabel, but not attributed to either direction.
            continue

        if true_price > claimed_price:
            report.under_claimed += 1
            if entry.sale.price_aud < true_price:
                report.exploitable += 1
                report.under_claim_gaps.append(true_price - entry.sale.price_aud)
                report.examples.append(
                    (
                        entry.sale.title,
                        posterior.most_likely.describe(),
                        next(
                            (v.describe() for v in variants if v.key == true_key),
                            true_key,
                        ),
                        true_price - entry.sale.price_aud,
                    )
                )
        else:
            report.over_claimed += 1

    return report


def parse_verified(
    sales: list[SoldSale],
    truth: dict[str, str],
    variants: list[Variant],
) -> list[VerifiedSale]:
    """Attach confirmed variants to sales.

    ``truth`` maps a sale title to a variant *label* or key, as recorded while
    looking at photos. An unrecognised label raises rather than being dropped --
    a typo silently discarding a verification would bias the measurement toward
    whatever was easy to label.
    """
    by_label = {v.label: v.key for v in variants if v.label}
    by_key = {v.key: v.key for v in variants}

    verified: list[VerifiedSale] = []
    unknown: list[str] = []
    for sale in sales:
        label = truth.get(sale.title)
        if label is None:
            continue
        key = by_key.get(label) or by_label.get(label)
        if key is None:
            unknown.append(label)
            continue
        verified.append(VerifiedSale(sale=sale, true_variant_key=key))

    if unknown:
        raise ValueError(
            f"unrecognised variant label(s): {sorted(set(unknown))}. "
            f"Known labels: {sorted(by_label)}"
        )
    return verified
