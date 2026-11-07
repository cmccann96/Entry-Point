"""Sort a supermarket product name into a shopping category and sub-type.

`classify("Coles RSPCA Approved Chicken Breast Diced")` -> ("Meat", "Chicken")

Rules run in order and the first match wins, so the order matters: ready
meals and pre-made items are caught before their main ingredient (a beef pie
is a ready meal, not beef), and raw cuts of meat are caught before sauce
words (pork ribs in BBQ sauce are still meat).
"""
from __future__ import annotations

import re

# Display order of the main categories.
CATEGORY_ORDER = [
    "Meat",
    "Seafood",
    "Vegetables",
    "Dairy, eggs & tofu",
    "Pasta, rice & noodles",
    "Bread & wraps",
    "Pantry",
    "Ready meals & sides",
    "Other",
]


def _rx(words: str) -> re.Pattern:
    return re.compile(r"\b(?:" + words + r")\b", re.I)


READY_BRANDS = _rx(
    r"coles kitchen|coles perform|core powerfoods|lean cuisine|dineamic|herbert adams|borg'?s|"
    r"four'?n twenty|potato utopia|haldirams?|my muscle chef|youfoodz|coles x gami|hong kong dim sim|"
    r"coles made easy|on the menu"
)
FRESH_PASTA = _rx(r"ravioli|tortellini|agnolotti|gnocchi|cappelletti|fresh pasta")
READY_WORDS = _rx(
    r"pies?|quiche|pastizzi|gozleme|arancini|croquettes?|dumplings?|dim sims?|spring rolls?|"
    r"sausage rolls?|pakora|bhaji|paratha|samosas?|nuggets|kiev|bites|cannelloni|potato bake|gratin|"
    r"mashies|pizza|ready meal|meal|chips|wedges|hash browns?|fries|bao|buns?|gyoza|har ?gow|siu mai|"
    r"wontons?|parcels|pigs in blankets"
)
READY_SUBS = [
    ("Pies & pastries", _rx(r"pies?|quiche|pastizzi|gozleme|sausage rolls?|paratha|pizza")),
    ("Dumplings & finger food", _rx(r"dumplings?|dim sims?|spring rolls?|pakora|bhaji|samosas?|arancini|croquettes?|bites|nuggets|bao|buns?|gyoza|har ?gow|siu mai|wontons?|pigs in blankets")),
    ("Chips & sides", _rx(r"chips|wedges|hash browns?|fries|mashies|gratin|potato bake|salad")),
]

TOFU = _rx(r"tofu|tempeh")
CHEESE_PRODUCT = _rx(r"cheese (?:slices|block|shredded|grated|sticks|spread)")
COCONUT = _rx(r"coconut (?:milk|cream)")

SEAFOOD = _rx(
    r"salmon|prawns?|fish|barramundi|tuna|dory|whiting|basa|hoki|cod|snapper|flathead|calamari|squid|"
    r"mussels?|sardines?|oysters?(?! blade| mushroom)|crab|scallops?|marinara|trout|seafood|anchov(?:y|ies)|mackerel"
)
SEAFOOD_SUBS = [
    ("Tinned", _rx(r"tuna|sardines?|anchov(?:y|ies)|mackerel|in oil|in springwater|in brine|in tomato|tin|can")),
    ("Crumbed & frozen", _rx(r"crumbed|crumb|battered|oven bake|frozen|fingers|tempura|cakes?|salt and pepper")),
    ("Prawns & shellfish", _rx(r"prawns?|calamari|squid|mussels?|oysters?|crab|scallops?|marinara")),
]

MEAT = _rx(
    r"chicken|beef|pork|lamb|veal|turkey|kangaroo|goat|duck|bacon|ham|salami|chorizo|prosciutto|pancetta|"
    r"sausages?|mince|burgers?|patties|sliders|meatballs|rissoles|kofta|kransky|frankfurts?|franks|steaks?|"
    r"schnitzels?|ribs|porterhouse|scotch fillet|rump|brisket|strasburg|devon|kabana|cabanossi"
)
MEAT_CUTS = _rx(
    r"breast|thighs?|fillets?|mince|steaks?|chops|drumsticks?|wings?|roast|ribs|belly|cutlets|"
    r"tenderloins?|rump|scotch|porterhouse|sausages?|schnitzels?|diced|strips|stir fry|kebabs?|skewers?|"
    r"shoulder|leg|rack|whole|marylands?|brisket|patties|burgers?"
)
MEAT_SUBS = [
    ("Sausages", _rx(r"sausages?|chorizo|kransky|frankfurts?|franks|snags|bratwurst")),
    ("Mince", _rx(r"mince|meatballs|rissoles|kofta")),
    ("Burgers", _rx(r"burgers?|patties|sliders")),
    ("Bacon & deli", _rx(r"bacon|ham|salami|prosciutto|pancetta|deli|pastrami|strasburg|devon|kabana|cabanossi")),
    ("Chicken", _rx(r"chicken|turkey|duck")),
    ("Lamb", _rx(r"lamb")),
    ("Pork", _rx(r"pork")),
    ("Beef", _rx(r"beef|steaks?|veal|porterhouse|scotch fillet|rump|brisket")),
]

# Seafood named only as a flavour ("fish sauce", "oyster sauce") is a pantry item.
SEAFOOD_FLAVOUR = _rx(r"(?:fish|oyster|seafood|prawn|anchovy|crab) (?:sauce|stock|paste|flavour(?:ed)?)")
# Words that make anything a pantry product, even with a meat word in it ("Steak Seasoning Mix").
PANTRY_STRONG = _rx(r"seasoning|stock|gravy|recipe bases?|mix|flavour(?:ed)?|spice|curry powder|soup")
# The product itself is a sauce or a tinned meal ("Steak Sauce", "Curried Sausages Cooking Sauce").
SAUCE_PRODUCT = re.compile(r"(?:\bsauce\s*$|\bcooking sauce\b|^harvest\b)", re.I)
SAUCES = _rx(
    r"sauces?|seasoning|stock|gravy|soup|paste|marinade|dressing|recipe base|mix|ketchup|pesto|"
    r"flavour(?:ed)?|spice|curry powder|glaze|salsa|chutney|relish|mayo(?:nnaise)?|mustard"
)
TINS = _rx(
    r"diced tomatoes|crushed tomatoes|chopped tomatoes|passata|polpa|peeled tomatoes|puree|tins?|cans?|"
    r"semi[- ]dried|sun ?dried|pickled|marinated|roasted red pepper|in brine|jars?|strips|beans in|"
    r"chickpeas|lentils|kidney beans|cannellini"
)

DAIRY_SUBS = [
    ("Eggs", _rx(r"eggs")),
    ("Cheese", _rx(r"cheese|cheddar|brie|camembert|feta|mozzarella|parmesan|parmigiano|grana padano|haloumi|halloumi|ricotta|labneh|babybel|bocconcini|gouda|tasty")),
    ("Cream & milk", _rx(r"cream|milky?|custard")),
    ("Butter & yoghurt", _rx(r"butter|yoghurt|yogurt|ghee")),
]

PASTA_SUBS = [
    ("Fresh pasta", FRESH_PASTA),
    ("Noodles", _rx(r"noodles?|vermicelli|ramen|udon|soba")),
    ("Rice & grains", _rx(r"rice|couscous|quinoa|freekeh|barley|khichdi")),
    ("Pasta", _rx(
        r"pasta|spaghetti|spaghettini|spaghettoni|penne|fusilli|linguine|fettuccine|rigatoni|risoni|"
        r"macaroni|tagliatelle|lasagne|angel hair|chifferi|elbow|pipe rigate|orecchiette|farfalle|shells"
    )),
]

BREAD = _rx(r"wraps?|tortillas?|bread|rolls?|pita|naan|flatbreads?|pane|focaccia|bagels?|taco shells?|ciabatta|sourdough")

VEG = _rx(
    r"broccoli(?:ni)?|capsicums?|peppers?|zucchini|mushrooms?|potato(?:es)?|carrots?|spinach|tomato(?:es)?|"
    r"onions?|cauliflower|pumpkin|lettuce|salad|rocket|beans|peas|corn|cucumbers?|garlic|ginger|avocados?|"
    r"kale|cabbage|bok choy|asparagus|eggplant|celery|leeks?|beetroot|veg|vegetables|herbs|chillies|chilli|"
    r"shallots|spring onions?|sprouts|parsnips?|swede|silverbeet|choy sum"
)
FROZEN = _rx(r"frozen|birds eye|mccain|snap frozen")


def _sub(name: str, subs, default: str) -> str:
    for label, rx in subs:
        if rx.search(name):
            return label
    return default


def classify(name: str) -> tuple[str, str]:
    n = name or ""
    if READY_BRANDS.search(n):
        return "Ready meals & sides", _sub(n, READY_SUBS, "Ready meals")
    if FRESH_PASTA.search(n):
        return "Pasta, rice & noodles", "Fresh pasta"
    if READY_WORDS.search(n):
        return "Ready meals & sides", _sub(n, READY_SUBS, "Ready meals")
    if CHEESE_PRODUCT.search(n):
        return "Dairy, eggs & tofu", "Cheese"
    if TOFU.search(n):
        return "Dairy, eggs & tofu", "Tofu"
    if COCONUT.search(n):
        return "Pantry", "Tins & jars"
    if PANTRY_STRONG.search(n):
        return "Pantry", "Sauces & seasonings"
    if SEAFOOD.search(n) and not SEAFOOD_FLAVOUR.search(n):
        return "Seafood", _sub(n, SEAFOOD_SUBS, "Fish")
    if SAUCE_PRODUCT.search(n):
        return "Pantry", "Tins & jars" if n.lower().startswith("harvest") else "Sauces & seasonings"
    if MEAT.search(n) and MEAT_CUTS.search(n):
        return "Meat", _sub(n, MEAT_SUBS, "Other meat")
    if SAUCES.search(n):
        return "Pantry", "Sauces & seasonings"
    if BREAD.search(n):
        return "Bread & wraps", "Bread & wraps"
    for label, rx in DAIRY_SUBS:
        if rx.search(n):
            return "Dairy, eggs & tofu", label
    for label, rx in PASTA_SUBS:
        if rx.search(n):
            return "Pasta, rice & noodles", label
    if MEAT.search(n):
        return "Meat", _sub(n, MEAT_SUBS, "Other meat")
    if TINS.search(n):
        return "Pantry", "Tins & jars"
    if VEG.search(n):
        return "Vegetables", "Frozen" if FROZEN.search(n) else "Fresh"
    return "Other", "Other"
