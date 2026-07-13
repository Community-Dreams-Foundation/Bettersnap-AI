"""Canonical BetterSnap category / attire / background catalog — the SINGLE source
of truth for both deploy units:

  • Azure Functions app imports it as `shared.catalog` (validation in /jobs/submit,
    and it is served to the frontend via GET /catalog so the UI never hardcodes).
  • The inference container imports it as `catalog` (the Dockerfile COPYs this file
    to /app/catalog.py); main.py builds every prompt from it.

The IDs here are the contract across frontend ↔ Functions ↔ inference. Prompt phrases
live ONLY here so wording changes in one place. Attire phrases are gender-aware
(male / female / neutral); neutral is used whenever gender is absent/unknown — we
never assume male or female.

Each category:
  type        "professional" | "personal"      (drives the Basic single-type rule)
  label       display name
  lead        photograph lead-in ("A professional corporate headshot photograph of")
  lighting    optional list overriding the global lighting rotation (moody/warm/golden)
  custom      True only for custom_scene (user types the scene; no attire/bg menu)
  attires     { attire_id: {male, female, neutral} }
  backgrounds { background_id: "<background phrase>" }
"""

# ── Identity vocabulary (shared by TRAINING and GENERATION) ──────────────────
# The rare DreamBooth trigger token. Every per-user LoRA is trained with it in the
# instance prompt, and main.py bakes it into every generation prompt; without it the
# adapter loads but is never invoked → generic strangers.
IDENTITY_TRIGGER = "ohwx"

# gender → class word. This word MUST be identical at training time
# ("a photo of ohwx woman") and at generation time ("ohwx woman"), or the token the
# adapter was trained against is not the token the prompt fires. That is why it lives
# here, in the one module BOTH deploy units import, rather than being spelled out
# separately in function_app.py and main.py.
SUBJECT_NOUN = {"female": "woman", "male": "man", "neutral": "person"}


def normalize_gender(g: str) -> str:
    """Map any gender input to one of {'female','male','neutral'}. Never assumes a
    default — absent/unknown → 'neutral' (gender-neutral attire + subject noun)."""
    g = (g or "").strip().lower()
    if g in ("female", "woman", "women", "f", "she/her"):
        return "female"
    if g in ("male", "man", "men", "m", "he/him"):
        return "male"
    return "neutral"


def class_word(gender: str) -> str:
    """The DreamBooth class word for this user: woman | man | person."""
    return SUBJECT_NOUN[normalize_gender(gender)]


def instance_prompt(class_word_: str) -> str:
    """What the LoRA is trained to bind the trigger token to."""
    return f"a photo of {IDENTITY_TRIGGER} {class_word_}"


def class_prompt(class_word_: str) -> str:
    """The prior-preservation anchor. Pins the GENERIC class in place so the trigger
    token can only encode what makes THIS person different from it — i.e. their face."""
    return f"a photo of a {class_word_}"


# ── Professional (6) ─────────────────────────────────────────────────────────
_PROFESSIONAL = {
    "business_suit": {
        "type": "professional",
        "label": "Business Suit",
        "lead": "A professional corporate headshot photograph of",
        "attires": {
            "navy_suit_tie": {
                "male": "a sharp navy-blue business suit with a crisp white dress shirt and a silk tie",
                "female": "a tailored navy-blue business suit with a white blouse",
                "neutral": "a well-fitted navy-blue business suit",
            },
            "charcoal_three_piece": {
                "male": "a charcoal-grey three-piece business suit with a white shirt",
                "female": "a charcoal-grey tailored suit with a silk blouse",
                "neutral": "a charcoal-grey business suit",
            },
            "black_suit": {
                "male": "a classic black business suit with a white shirt and a dark tie",
                "female": "a classic black tailored suit with a white blouse",
                "neutral": "a classic black business suit",
            },
            "grey_open_collar": {
                "male": "a light-grey business suit with an open-collar shirt, no tie",
                "female": "a light-grey business suit with an elegant blouse",
                "neutral": "a light-grey business suit",
            },
            "pinstripe": {
                "male": "a dark pinstripe business suit with a white shirt and tie",
                "female": "a dark pinstripe tailored suit with a white blouse",
                "neutral": "a dark pinstripe business suit",
            },
        },
        "backgrounds": {
            "studio_gray": "against a smooth neutral gray photography studio backdrop",
            "studio_white": "against a clean pure white photography studio backdrop",
            "modern_office": "in a modern corporate office with softly blurred glass walls behind",
            "exec_office": "in an executive office with a softly blurred city skyline through the window",
            "corporate_lobby": "in a polished corporate lobby with softly blurred modern architecture",
        },
    },
    "healthcare": {
        "type": "professional",
        "label": "Healthcare / Medical",
        "lead": "A professional medical portrait photograph of",
        "attires": {
            "white_coat_scrubs": {
                "male": "a clean white medical coat over light-blue scrubs with a stethoscope around the neck",
                "female": "a clean white medical coat over light-blue scrubs with a stethoscope around the neck",
                "neutral": "a clean white medical coat over scrubs with a stethoscope",
            },
            "teal_scrubs": {
                "male": "teal medical scrubs with a lanyard ID badge",
                "female": "teal medical scrubs with a lanyard ID badge",
                "neutral": "teal medical scrubs with a lanyard ID badge",
            },
            "coat_shirt_tie": {
                "male": "a white medical coat over a collared shirt and tie",
                "female": "a white medical coat over a professional blouse",
                "neutral": "a white medical coat over professional attire",
            },
            "navy_scrubs": {
                "male": "navy-blue medical scrubs",
                "female": "navy-blue medical scrubs",
                "neutral": "navy-blue medical scrubs",
            },
            "lab_coat": {
                "male": "a crisp white lab coat with a stethoscope",
                "female": "a crisp white lab coat with a stethoscope",
                "neutral": "a crisp white lab coat with a stethoscope",
            },
        },
        "backgrounds": {
            "hospital_corridor": "in a bright modern hospital corridor with softly blurred medical equipment",
            "clinic_white": "against a clean clinical white backdrop",
            "exam_room": "in a modern medical exam room softly blurred behind",
            "medical_office": "in a bright medical office with softly blurred shelving",
            "studio_gray": "against a smooth neutral gray studio backdrop",
        },
    },
    "realtor": {
        "type": "professional",
        "label": "Realtor",
        "lead": "A professional real-estate agent portrait photograph of",
        "attires": {
            "navy_blazer_open": {
                "male": "a smart navy blazer over an open-collar white shirt, no tie",
                "female": "an elegant blazer over a smart blouse",
                "neutral": "a smart blazer over an open-collar shirt, polished business-casual",
            },
            "grey_blazer": {
                "male": "a light-grey blazer over a crisp shirt",
                "female": "a light-grey blazer over a smart top",
                "neutral": "a light-grey blazer over a crisp shirt",
            },
            "business_casual": {
                "male": "a fitted business-casual shirt with a blazer",
                "female": "a chic business-casual outfit with a blazer",
                "neutral": "polished business-casual attire with a blazer",
            },
            "slim_suit_no_tie": {
                "male": "a modern slim suit with an open collar, no tie",
                "female": "a modern tailored suit with a soft blouse",
                "neutral": "a modern slim suit with an open collar",
            },
            "knit_blazer": {
                "male": "a smart knit top under a tailored blazer",
                "female": "a smart knit top under a tailored blazer",
                "neutral": "a smart knit top under a tailored blazer",
            },
        },
        "backgrounds": {
            "luxury_home": "in front of a modern upscale home with softly blurred contemporary architecture",
            "real_estate_office": "in a bright modern real-estate office softly blurred behind",
            "staged_interior": "in a beautifully staged living-room interior softly blurred",
            "city_street": "on an upscale city street with softly blurred storefronts",
            "studio_white": "against a clean white studio backdrop",
        },
    },
    "legal": {
        "type": "professional",
        "label": "Lawyer / Legal",
        "lead": "A professional corporate headshot photograph of",
        "attires": {
            "dark_suit_tie": {
                "male": "a formal dark charcoal suit with a white shirt and a conservative tie",
                "female": "a formal dark tailored suit with a crisp blouse",
                "neutral": "formal dark legal-professional attire",
            },
            "black_suit": {
                "male": "a classic black suit with a white shirt and a dark tie",
                "female": "a classic black tailored suit with a white blouse",
                "neutral": "a classic black suit",
            },
            "navy_pinstripe": {
                "male": "a navy pinstripe suit with a white shirt and tie",
                "female": "a navy pinstripe tailored suit with a blouse",
                "neutral": "a navy pinstripe suit",
            },
            "three_piece_grey": {
                "male": "a formal three-piece grey suit with a tie",
                "female": "a formal three-piece grey tailored suit",
                "neutral": "a formal three-piece grey suit",
            },
            "suit_pocket_square": {
                "male": "a tailored dark suit with a tie and a pocket square",
                "female": "a tailored dark suit with an elegant blouse",
                "neutral": "a tailored dark suit",
            },
        },
        "backgrounds": {
            "law_library": "in a classic law office with a richly blurred wall of law books behind",
            "wood_office": "in a wood-paneled executive office softly blurred",
            "courthouse": "in front of softly blurred classical courthouse columns",
            "exec_office": "in an executive office with a softly blurred skyline",
            "studio_charcoal": "against a dark charcoal studio backdrop",
        },
    },
    "dark_studio": {
        "type": "professional",
        "label": "Dark Studio",
        "lead": "A dramatic professional portrait photograph of",
        "lighting": [
            "dramatic low-key studio lighting",
            "moody Rembrandt side lighting",
            "soft rim lighting against a dark background",
        ],
        "attires": {
            "black_suit_dark_shirt": {
                "male": "an elegant black suit with a black shirt",
                "female": "an elegant black formal outfit",
                "neutral": "elegant dark formal attire",
            },
            "black_turtleneck": {
                "male": "a sleek black turtleneck",
                "female": "a sleek black turtleneck",
                "neutral": "a sleek black turtleneck",
            },
            "charcoal_suit": {
                "male": "a charcoal suit with a dark shirt",
                "female": "a charcoal tailored suit, refined and dark",
                "neutral": "a charcoal dark formal suit",
            },
            "dark_blazer": {
                "male": "a dark tailored blazer over a black top",
                "female": "a dark tailored blazer over a black top",
                "neutral": "a dark tailored blazer over a black top",
            },
            "formal_black": {
                "male": "formal black-tie attire",
                "female": "an elegant black formal dress",
                "neutral": "elegant black formal attire",
            },
        },
        "backgrounds": {
            "black_studio": "against a deep black studio backdrop",
            "charcoal_studio": "against a dramatic dark charcoal studio background",
            "spotlight": "against a black background lit by a single soft spotlight",
            "moody_gradient": "against a dark moody gradient backdrop",
            "shadow_studio": "against a black studio backdrop with soft shadows",
        },
    },
    "formal_non_suit": {
        "type": "professional",
        "label": "Formal Non-Suit",
        "lead": "A professional headshot photograph of",
        "attires": {
            "dress_shirt": {
                "male": "a crisp light-blue dress shirt, smart and formal without a jacket or tie",
                "female": "an elegant formal blouse, polished without a blazer",
                "neutral": "smart formal attire without a jacket",
            },
            "white_shirt_open": {
                "male": "a crisp white dress shirt with the collar open",
                "female": "a crisp white formal blouse",
                "neutral": "a crisp white formal shirt",
            },
            "knit_sweater": {
                "male": "a fine-gauge knit sweater over a collared shirt",
                "female": "an elegant fine-knit top",
                "neutral": "a smart fine-knit top",
            },
            "smart_polo": {
                "male": "a smart dark polo shirt",
                "female": "an elegant smart top",
                "neutral": "a smart collared top",
            },
            "turtleneck": {
                "male": "a smart charcoal turtleneck",
                "female": "an elegant charcoal turtleneck",
                "neutral": "a smart charcoal turtleneck",
            },
        },
        "backgrounds": {
            "studio_gray": "against a smooth neutral gray studio backdrop",
            "studio_white": "against a clean white studio backdrop",
            "soft_gradient": "against a soft modern gradient backdrop",
            "warm_studio": "against a warm softly-lit studio backdrop",
            "office_blur": "in a modern office softly blurred behind",
        },
    },
}

# ── Personal (6) ─────────────────────────────────────────────────────────────
_PERSONAL = {
    "dating": {
        "type": "personal",
        "label": "Dating Profile",
        "lead": "A warm, approachable lifestyle portrait photograph of",
        "lighting": [
            "warm golden-hour light",
            "soft warm ambient light",
            "natural window light",
        ],
        "attires": {
            "casual_buttonup": {
                "male": "a well-fitted casual button-up shirt, relaxed and approachable",
                "female": "a flattering casual top, natural and approachable",
                "neutral": "a smart-casual relaxed top",
            },
            "knit_sweater": {
                "male": "a smart knit sweater",
                "female": "a soft stylish sweater",
                "neutral": "a smart knit sweater",
            },
            "tee_jacket": {
                "male": "a fitted t-shirt under a casual jacket",
                "female": "a stylish top under a casual jacket",
                "neutral": "a casual top under a light jacket",
            },
            "henley": {
                "male": "a casual henley shirt",
                "female": "a casual stylish blouse",
                "neutral": "a relaxed casual top",
            },
            "smart_casual": {
                "male": "a smart-casual shirt with rolled sleeves",
                "female": "a chic smart-casual outfit",
                "neutral": "a smart-casual outfit",
            },
        },
        "backgrounds": {
            "cafe_bokeh": "in a warm softly-lit cafe with cozy blurred bokeh",
            "golden_park": "outdoors at golden hour with soft blurred greenery",
            "city_evening": "on a city street in the evening with warm blurred lights",
            "cozy_interior": "in a cozy warmly-lit interior softly blurred",
            "rooftop_sunset": "on a rooftop at sunset with a softly blurred skyline",
        },
    },
    "travel": {
        "type": "personal",
        "label": "Travel Photo",
        "lead": "A natural candid travel portrait photograph of",
        "attires": {
            "casual_travel": {
                "male": "casual travel clothing with a comfortable shirt",
                "female": "casual travel clothing with a comfortable top",
                "neutral": "casual comfortable travel clothing",
            },
            "light_jacket": {
                "male": "a light travel jacket over a casual shirt",
                "female": "a light travel jacket over a casual top",
                "neutral": "a light travel jacket over casual clothing",
            },
            "summer_casual": {
                "male": "relaxed summer travel clothes",
                "female": "relaxed summer travel clothes",
                "neutral": "relaxed summer travel clothes",
            },
            "denim_casual": {
                "male": "a casual denim jacket over a plain top",
                "female": "a casual denim jacket over a plain top",
                "neutral": "a casual denim jacket over a plain top",
            },
            "explorer": {
                "male": "a practical casual outfit with a backpack strap visible",
                "female": "a practical casual outfit with a backpack strap visible",
                "neutral": "a practical casual outfit with a backpack strap visible",
            },
        },
        "backgrounds": {
            "euro_street": "on a scenic European city street softly blurred behind",
            "mountain_view": "at a scenic mountain overlook softly blurred",
            "coastal_view": "at a scenic coastal viewpoint with the sea blurred behind",
            "landmark": "in front of a softly blurred famous travel landmark",
            "old_town": "in a charming old-town street softly blurred",
        },
    },
    "graduation": {
        "type": "personal",
        "label": "Graduation",
        "lead": "A proud graduation portrait photograph of",
        "attires": {
            "gown_cap_stole": {
                "male": "a black graduation gown and mortarboard cap with a colored stole",
                "female": "a black graduation gown and mortarboard cap with a colored stole",
                "neutral": "a black graduation gown and mortarboard cap with a colored stole",
            },
            "gown_shirt": {
                "male": "a graduation gown over a collared shirt and tie",
                "female": "a graduation gown over a smart blouse",
                "neutral": "a graduation gown over smart attire",
            },
            "gown_honors": {
                "male": "a graduation gown with honor cords and a mortarboard cap",
                "female": "a graduation gown with honor cords and a mortarboard cap",
                "neutral": "a graduation gown with honor cords and a mortarboard cap",
            },
            "gown_navy": {
                "male": "a navy-blue graduation gown and cap",
                "female": "a navy-blue graduation gown and cap",
                "neutral": "a navy-blue graduation gown and cap",
            },
            "gown_diploma": {
                "male": "a black graduation gown and cap, holding a diploma",
                "female": "a black graduation gown and cap, holding a diploma",
                "neutral": "a black graduation gown and cap, holding a diploma",
            },
        },
        "backgrounds": {
            "campus_historic": "on a university campus with softly blurred historic buildings",
            "ceremony": "at a graduation ceremony with a softly blurred crowd",
            "academic_library": "in front of a softly blurred academic library",
            "campus_lawn": "on a green campus lawn softly blurred",
            "stage": "on a graduation stage softly blurred behind",
        },
    },
    "beach_sunset": {
        "type": "personal",
        "label": "Beach / Sunset",
        "lead": "A natural candid lifestyle portrait photograph of",
        "lighting": [
            "warm golden-hour sunlight",
            "soft sunset backlight",
            "warm natural light",
        ],
        "attires": {
            "linen_shirt": {
                "male": "a light open linen shirt, relaxed summer style",
                "female": "a light flowing summer outfit",
                "neutral": "relaxed light summer attire",
            },
            "white_tee": {
                "male": "a casual white t-shirt",
                "female": "a casual white summer top",
                "neutral": "a casual white summer top",
            },
            "beach_casual": {
                "male": "relaxed beachwear with an open shirt",
                "female": "relaxed beachwear with a light cover-up",
                "neutral": "relaxed beachwear",
            },
            "summer_outfit": {
                "male": "a light casual summer shirt",
                "female": "a flowing summer dress",
                "neutral": "light summer clothing",
            },
            "light_denim": {
                "male": "a light denim shirt, casual summer style",
                "female": "a light denim shirt, casual summer style",
                "neutral": "a light denim shirt, casual summer style",
            },
        },
        "backgrounds": {
            "beach_sunset": "on a beach at golden-hour sunset with the ocean softly blurred behind",
            "ocean_dusk": "by the ocean at dusk with warm blurred light",
            "palm_beach": "on a palm-lined beach softly blurred at sunset",
            "pier_sunset": "on a seaside pier at sunset softly blurred",
            "coastal_golden": "at a coastal viewpoint bathed in golden-hour light",
        },
    },
    "luxury": {
        "type": "personal",
        "label": "Luxury / Lifestyle",
        "lead": "An elegant lifestyle portrait photograph of",
        "attires": {
            "designer_jacket": {
                "male": "an upscale tailored designer jacket over a fine shirt",
                "female": "an elegant designer outfit, chic and stylish",
                "neutral": "upscale stylish designer attire",
            },
            "evening_formal": {
                "male": "a refined black tuxedo",
                "female": "an elegant evening gown",
                "neutral": "elegant formal eveningwear",
            },
            "silk_top": {
                "male": "a luxury silk shirt",
                "female": "a luxury silk blouse",
                "neutral": "a luxury silk top",
            },
            "cashmere": {
                "male": "a fine cashmere sweater, understated luxury",
                "female": "a fine cashmere sweater, understated luxury",
                "neutral": "a fine cashmere sweater, understated luxury",
            },
            "statement": {
                "male": "a bold designer blazer",
                "female": "a bold designer outfit with statement jewelry",
                "neutral": "a bold designer outfit",
            },
        },
        "backgrounds": {
            "penthouse": "in a luxurious modern penthouse interior softly blurred",
            "luxury_car": "beside a luxury car softly blurred",
            "upscale_lounge": "in an upscale lounge with elegant blurred decor",
            "marble_lobby": "in a marble luxury lobby softly blurred",
            "yacht_deck": "on a luxury yacht deck softly blurred",
        },
    },
    "custom_scene": {
        "type": "personal",
        "label": "Custom Scene",
        "lead": "A portrait photograph of",
        "custom": True,
        "attires": {},
        "backgrounds": {},
    },
}

CATEGORY_CATALOG = {**_PROFESSIONAL, **_PERSONAL}


# ── Lookups / validation (used by function_app.submit_job) ────────────────────
def category_type(category: str) -> str | None:
    c = CATEGORY_CATALOG.get(category)
    return c["type"] if c else None


def is_custom(category: str) -> bool:
    return bool(CATEGORY_CATALOG.get(category, {}).get("custom"))


def valid_attire_ids(category: str) -> set:
    return set(CATEGORY_CATALOG.get(category, {}).get("attires", {}).keys())


def valid_background_ids(category: str) -> set:
    return set(CATEGORY_CATALOG.get(category, {}).get("backgrounds", {}).keys())


def public_catalog() -> list:
    """Display catalog served to the frontend via GET /catalog (no prompt phrases
    hardcoded in the client). Each option carries a category-qualified `ref`
    ("category.option") — the GLOBAL, cross-category selection uses these refs
    because plain option ids (studio_gray, black_suit) repeat across categories."""
    def _labelize(id_: str) -> str:
        return id_.replace("_", " ").title()

    out = []
    for key, c in CATEGORY_CATALOG.items():
        out.append({
            "id": key,
            "type": c["type"],
            "label": c["label"],
            "custom": bool(c.get("custom")),
            "attires": [{"id": a, "ref": f"{key}.{a}", "label": _labelize(a)}
                        for a in c.get("attires", {})],
            "backgrounds": [{"id": b, "ref": f"{key}.{b}", "label": _labelize(b)}
                            for b in c.get("backgrounds", {})],
        })
    return out


# ── Category-qualified refs ("category.option") for the GLOBAL picker ──────────
# Selections span categories, so every attire/background is addressed as a ref.
def split_ref(ref: str):
    cat, _, opt = (ref or "").partition(".")
    return cat, opt


def valid_attire_ref(ref: str) -> bool:
    cat, opt = split_ref(ref)
    return opt in valid_attire_ids(cat)


def valid_background_ref(ref: str) -> bool:
    cat, opt = split_ref(ref)
    return opt in valid_background_ids(cat)


def ref_type(ref: str) -> str | None:
    cat, _ = split_ref(ref)
    return category_type(cat)


def attire_phrase_ref(ref: str, gender_key: str) -> str:
    cat, opt = split_ref(ref)
    return attire_phrase(cat, opt, gender_key)


def background_phrase_ref(ref: str) -> str:
    cat, opt = split_ref(ref)
    return background_phrase(cat, opt)


def lead_for_background_ref(ref: str) -> str:
    """Lead phrase (style) is DERIVED FROM THE BACKGROUND's category — a beach bg
    reads 'natural lifestyle portrait', an office bg 'corporate headshot', even if
    the attire came from a different category."""
    cat, _ = split_ref(ref)
    return lead_phrase(cat)


def lighting_for_background_ref(ref: str, default: list) -> list:
    """Lighting also follows the background (beach→golden, dark_studio→moody)."""
    cat, _ = split_ref(ref)
    return lighting_options(cat, default)


def build_combos_global(attire_refs: list, background_refs: list) -> list:
    """Cartesian product of the user's SELECTED attire refs × background refs,
    spanning any categories. Invalid refs are dropped. Returns (attire_ref,
    background_ref) tuples."""
    a_sel = [r for r in (attire_refs or []) if valid_attire_ref(r)]
    b_sel = [r for r in (background_refs or []) if valid_background_ref(r)]
    return [(a, b) for a in a_sel for b in b_sel]


# ── Prompt spec builder (used by the inference container / main.py) ───────────
def build_combos(category: str, attire_ids: list, background_ids: list) -> list:
    """Cartesian product of the SELECTED attires × backgrounds for a category.
    Falls back to ALL options in the category when a selection list is empty
    (spec: 'none selected → all in category'). Unknown ids are dropped. Returns a
    list of (attire_id, background_id) tuples; empty if the category has no menu."""
    cat = CATEGORY_CATALOG.get(category)
    if not cat:
        return []
    a_all = list(cat.get("attires", {}).keys())
    b_all = list(cat.get("backgrounds", {}).keys())
    a_sel = [a for a in (attire_ids or []) if a in cat.get("attires", {})] or a_all
    b_sel = [b for b in (background_ids or []) if b in cat.get("backgrounds", {})] or b_all
    return [(a, b) for a in a_sel for b in b_sel]


def attire_phrase(category: str, attire_id: str, gender_key: str) -> str:
    a = CATEGORY_CATALOG.get(category, {}).get("attires", {}).get(attire_id, {})
    return a.get(gender_key) or a.get("neutral") or "professional attire"


def background_phrase(category: str, background_id: str) -> str:
    return CATEGORY_CATALOG.get(category, {}).get("backgrounds", {}).get(
        background_id, "against a neutral studio backdrop"
    )


def lead_phrase(category: str) -> str:
    return CATEGORY_CATALOG.get(category, {}).get(
        "lead", "A professional headshot photograph of"
    )


def lighting_options(category: str, default: list) -> list:
    """Per-category lighting override (moody/warm/golden) or the global default."""
    return CATEGORY_CATALOG.get(category, {}).get("lighting") or default
