"""Variant identification from listing photographs.

This is the module that makes the strategy testable. The edge is sellers
describing cards wrongly, and text cannot detect that: if the title is wrong,
trusting the title is wrong by construction. Only the image resolves it.

It reads the four tells from the brief, which between them determine the variant:

  1. a star above the rarity code  -> parallel or above
  2. art style: manga panels (black-and-white Oda art), painted full-bleed, or
     painted inside a frame
  3. gold stamp, WINNER stamp, or a printed serial number
  4. slabbed, and if so what grade and grader

Results are cached by image URL. Vision is the expensive step, so it runs only
on listings already triaged by information value -- and the cache means
re-scanning the same card number repeatedly costs nothing after the first pass.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from .variants import Treatment, Variant

_MODEL = "claude-opus-5"

# Sent identically for every image, so it caches. Written to be substantive
# enough to clear the 512-token cache minimum on this model -- the taxonomy is
# what makes the read reliable, so there is no reason to trim it.
_SYSTEM = """\
You identify One Piece TCG card variants from photographs. Report what is \
visible in the image. Never infer a variant from the seller's title, from \
price, or from what would be more valuable -- the entire purpose of this task \
is to check the image against a claim that may be wrong.

Rarity on these cards is four independent axes stacked on one card number, and \
one number can span a 50x price range. Read each axis separately.

BASE RARITY: the letter code printed by the collector number. One of C, UC, R, \
L (Leader), SR, or SEC (Secret Rare). Report exactly what is printed.

TREATMENT: **the star above the rarity code is the decisive signal, not the \
art style.** Look specifically at the space directly above the printed rarity \
letters. If there is no star, the treatment is `base` -- whatever the artwork \
looks like. Report `null` rather than guessing when the bottom-right corner is \
too small, blurred, or cropped to see whether a star is present.

Tiers, ascending (all of these require a star):
  - parallel: same painted art as the base card, with foiling/texture applied.
  - sp (Special Rare): alternate painted artwork, typically full-bleed, no frame.
  - manga: black-and-white manga panel artwork from Oda's original pages.
  - comic: comic/red parallel. Heavily stylised colour, usually red dominant.

**Art style alone does NOT determine treatment, and this is the most common \
way to get this wrong.** Manga-panel artwork is the ORDINARY base artwork for \
many cards -- EVENT cards especially, which frequently print manga panels with \
speech bubbles as their standard art. Such a card is a common worth a few \
dollars, not a manga rare worth four figures. Classifying it as `manga` on art \
style alone produces exactly the error this task exists to prevent, in the \
expensive direction. If the card says EVENT above its name and shows manga \
panels, that is almost certainly base art: check for the star before calling \
it anything else.

Base rarity and treatment are INDEPENDENT. A comic parallel of a SEC card is \
still a SEC. A card can legitimately be both.

CHANNEL: booster (no marking), promo, prize, or serialised winner card. Look \
for a gold stamp, a WINNER stamp, or a printed serial number such as 12/100. \
Any of those means the card is not an ordinary booster pull.

REGION: JP, EN, CN, KR or FR. Read the printed text on the card itself, not \
the listing language.

SLAB: is the card encased in a graded slab? If so report the grader (PSA, BGS, \
CGC, ACE) and the numeric grade from the label.

If the image is too small, blurred, cropped, or obstructed to read an axis, \
set that axis to null and say so in `unreadable`. A null is a correct and \
useful answer. A guess is not -- a wrong read here corrupts the measurement \
this feeds."""


class CardRead(BaseModel):
    """What the model could see in one image."""

    base_rarity: str | None = Field(
        None, description="Printed rarity code: C, UC, R, L, SR, SEC. Null if unreadable."
    )
    star_above_rarity: bool | None = Field(
        None, description="Is a star printed above the rarity code? Null if unreadable."
    )
    art_style: str | None = Field(
        None,
        description=(
            "Observed artwork only, independent of tier: painted_framed, "
            "painted_fullbleed, manga_panels, comic_stylised."
        ),
    )
    card_type: str | None = Field(
        None,
        description=(
            "Printed type above the card name: CHARACTER, EVENT, STAGE, LEADER. "
            "EVENT cards routinely use manga-panel base art -- recording this "
            "makes the commonest false positive auditable after the fact."
        ),
    )
    treatment: str | None = Field(
        None,
        description=(
            "One of: base, parallel, sp, manga, comic. Driven by the star above "
            "the rarity code, NOT by art style. Null if the corner is unreadable."
        ),
    )
    has_gold_stamp: bool = Field(False, description="Gold stamp visible.")
    has_winner_stamp: bool = Field(False, description="WINNER stamp visible.")
    serial_number: str | None = Field(None, description="Printed serial, e.g. '12/100'.")
    region: str | None = Field(None, description="JP, EN, CN, KR, FR from printed card text.")
    is_slabbed: bool = Field(False, description="Encased in a graded slab.")
    grader: str | None = Field(None, description="PSA, BGS, CGC, ACE.")
    grade: str | None = Field(None, description="Numeric grade from the slab label.")
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="0-1 confidence in the treatment call."
    )
    unreadable: list[str] = Field(
        default_factory=list, description="Axes that could not be read, and why."
    )


@dataclass(frozen=True)
class VisionResult:
    read: CardRead
    image_url: str
    from_cache: bool

    def matches(self, variant: Variant) -> bool | None:
        """Does the image agree with ``variant``? None when undeterminable."""
        if self.read.treatment is None:
            return None
        return self.read.treatment == variant.treatment.value

    def implied_treatment(self) -> Treatment | None:
        if self.read.treatment is None:
            return None
        try:
            return Treatment(self.read.treatment)
        except ValueError:
            return None


class VisionCache:
    """SQLite cache keyed by image URL.

    Vision is the only per-listing cost in the system. Caching by URL means a
    card scanned last week is free this week, which is what makes repeated
    scanning of the same watchlist affordable.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS vision ("
            "  image_url TEXT PRIMARY KEY,"
            "  payload   TEXT NOT NULL,"
            "  model     TEXT NOT NULL,"
            "  read_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        self._conn.commit()

    def get(self, image_url: str) -> CardRead | None:
        row = self._conn.execute(
            "SELECT payload FROM vision WHERE image_url = ?", (image_url,)
        ).fetchone()
        return CardRead(**json.loads(row[0])) if row else None

    def put(self, image_url: str, read: CardRead, model: str = _MODEL) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO vision (image_url, payload, model) VALUES (?, ?, ?)",
            (image_url, read.model_dump_json(), model),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


class VisionReader:
    """Reads card variants from listing images via the Claude API."""

    def __init__(self, cache: VisionCache, client=None, model: str = _MODEL) -> None:
        self.cache = cache
        self.model = model
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client

    def read(self, image_url: str, force: bool = False) -> VisionResult:
        """Identify the card in one image."""
        if not force:
            cached = self.cache.get(image_url)
            if cached is not None:
                return VisionResult(read=cached, image_url=image_url, from_cache=True)

        response = self.client.messages.parse(
            model=self.model,
            max_tokens=4096,
            # Adaptive thinking: distinguishing a standard parallel from an SP
            # from a comic parallel is a genuine visual judgement, not a lookup.
            thinking={"type": "adaptive"},
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "url", "url": image_url}},
                        {
                            "type": "text",
                            "text": (
                                "Identify this card. Read each axis independently and "
                                "report null for anything the image does not show clearly."
                            ),
                        },
                    ],
                }
            ],
            output_format=CardRead,
        )

        # Opus 5 can decline via safety classifiers, returning HTTP 200 with an
        # empty or partial content array. Reading parsed_output unconditionally
        # would raise something unhelpful several frames away from the cause.
        if response.stop_reason == "refusal":
            raise RuntimeError(
                f"vision request refused for {image_url} "
                f"(category: {getattr(response.stop_details, 'category', None)})"
            )

        read = response.parsed_output
        self.cache.put(image_url, read, self.model)
        return VisionResult(read=read, image_url=image_url, from_cache=False)

    def read_listing(self, image_urls: tuple[str, ...]) -> VisionResult | None:
        """Read the first image that yields a determinable treatment.

        eBay listings lead with a front-of-card shot, but not always -- some
        lead with a slab angle or a bundle photo. Falling through to later
        images costs a little more and recovers listings a first-image-only
        read would drop.
        """
        first: VisionResult | None = None
        for url in image_urls[:3]:
            result = self.read(url)
            first = first or result
            if result.read.treatment is not None:
                return result
        return first
