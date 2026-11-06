"""Texts an administrator writes in several languages: tariff titles and
descriptions, announcements, the bot's messages to visitors.

A value is `{"source": "ru", "texts": {"ru": "…", "en": "…"},
"fallback": {"en": "ru"}}`. Only the source language needs a text. A
language without its own text shows another one's: the one chosen in
`fallback`, else the source. The languages are those this version supports;
a language a later version adds starts with the source's text until the
administrator writes its own or picks one.
"""
import json

from .authorization import AccessDenied

LANGUAGES = ('en', 'ru')
DEFAULT_LANGUAGE = 'en'


def language(value):
    """A supported language from a locale or a Telegram language code."""
    value = (value or '').lower().replace('_', '-').split('-')[0]
    return value if value in LANGUAGES else DEFAULT_LANGUAGE


def plain(text, source=DEFAULT_LANGUAGE):
    """A text written in one language."""
    return {'source': source, 'texts': {source: text}, 'fallback': {}}


def checked(value, *, max_length, multiline=True):
    """The value in its stored form, or `invalid_localized_text`: the source
    has a text, every text fits, a fallback points at a language with its
    own text; empty texts are dropped and a fallback to the source is the
    default, so it is not kept."""
    def refuse():
        raise AccessDenied('invalid_localized_text', 422)

    if not isinstance(value, dict) or set(value) - {'source', 'texts', 'fallback'}:
        refuse()
    source, texts, fallback = value.get('source'), value.get('texts'), value.get('fallback') or {}
    if source not in LANGUAGES or not isinstance(texts, dict) or not isinstance(fallback, dict):
        refuse()
    result = {}
    for name, text in texts.items():
        if name not in LANGUAGES or not isinstance(text, str):
            refuse()
        text = text.strip()
        if len(text) > max_length or (not multiline and '\n' in text):
            refuse()
        if text:
            result[name] = text
    if source not in result:
        refuse()
    chosen = {}
    for name, target in fallback.items():
        if name not in LANGUAGES or name in result or target not in result:
            refuse()
        if target != source:
            chosen[name] = target
    return {'source': source, 'texts': {name: result[name] for name in LANGUAGES if name in result},
            'fallback': dict(sorted(chosen.items()))}


def resolve(value, wanted):
    """The text a person with this language reads."""
    wanted = language(wanted)
    texts = value['texts']
    if wanted in texts:
        return texts[wanted]
    return texts[value.get('fallback', {}).get(wanted, value['source'])]


def public(value):
    """For an editor: every supported language and whose text it shows."""
    return {**value, 'shown': {name: name if name in value['texts']
                               else value.get('fallback', {}).get(name, value['source']) for name in LANGUAGES}}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(raw, default=None):
    """A stored value; a plain string from before is a text in the default language."""
    if raw is None:
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return plain(raw)
    return value if isinstance(value, dict) and 'texts' in value else plain(str(value))


def account_language(conn, account_id):
    """The language an account reads: its own, else its Telegram's."""
    row = conn.execute('''SELECT a.locale, d.locale AS telegram_locale, d.language_code
        FROM backend_accounts a
        LEFT JOIN backend_external_identities i ON i.account_id = a.id AND i.provider = 'telegram'
        LEFT JOIN backend_telegram_identity_details d ON d.subject = i.subject
        WHERE a.id = ? ORDER BY i.subject LIMIT 1''', (account_id,)).fetchone()
    if row is None:
        return DEFAULT_LANGUAGE
    return language(row['locale'] or row['telegram_locale'] or row['language_code'])


def actor_language(conn, actor):
    """The language of whoever asks: an account, or a bot visitor."""
    from .telegram_visitors import TelegramVisitor
    if isinstance(actor, TelegramVisitor):
        return language(actor.details.get('locale') or actor.details.get('language_code'))
    return account_language(conn, actor.account.id)
