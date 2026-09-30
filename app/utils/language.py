"""The header selection is the shared language for all generated content."""

SUPPORTED_LANGUAGES = {"en", "fr"}


def get_language(config=None):
    lang = config.get("site_lang") if isinstance(config, dict) else getattr(config, "site_lang", None)
    return lang if isinstance(lang, str) and lang in SUPPORTED_LANGUAGES else "en"


def set_language(config, lang):
    if lang not in SUPPORTED_LANGUAGES:
        raise ValueError("Unsupported language")
    config.site_lang = lang
    # Keep legacy fields consistent for older API clients and installations.
    config.ollama_prompt_lang = lang
    config.chat_lang = lang


def language_instruction(lang):
    if lang == "fr":
        return "Réponds en français, même si le contexte, les instructions personnalisées ou les messages précédents sont dans une autre langue. Conserve les logs, commandes et identifiants tels quels."
    return "Respond in English, even if context, custom instructions or previous messages use another language. Preserve logs, commands and identifiers verbatim."
