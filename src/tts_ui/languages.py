LANG_MAP = {
    'ch_be': 'Bern',
    'ch_bs': 'Basel',
    'ch_gr': 'Graubünden',
    'ch_in': 'Innerschweiz',
    'ch_os': 'Ostschweiz',
    'ch_vs': 'Wallis',
    'ch_zh': 'Zürich',
    'de': 'Deutsch'
}

INV_LANG_MAP = {v:k for k,v in LANG_MAP.items()}


def resolve_language(language: str) -> str:
    """Accept either a language code (ch_be) or its display name (Bern)."""
    if language in LANG_MAP:
        return language
    if language in INV_LANG_MAP:
        return INV_LANG_MAP[language]
    raise ValueError(f"Unknown language '{language}', expected one of {list(LANG_MAP)}")
