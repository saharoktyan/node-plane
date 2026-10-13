"""User documentation links shared by both settings screens."""
from aiogram.types import InlineKeyboardButton

from .screens import Section

DOCUMENTS = (
    ('install', 'Installation and Quick Start', 'Установка и Quick Start'),
    ('workstation', 'Workstation: installation management', 'Workstation: управление установкой'),
    ('telegram', 'Telegram bot', 'Telegram-бот'),
    ('nodes', 'Nodes and server setup', 'Ноды и установка серверов'),
    ('protocols', 'VLESS and AmneziaWG', 'VLESS и AmneziaWG'),
    ('access', 'Accounts, profiles and access', 'Аккаунты, профили и доступ'),
    ('settings', 'Settings', 'Настройки'),
    ('maintenance', 'Updates, backups and recovery', 'Обновления, бэкапы и восстановление'),
    ('temporary-configs', 'Temporary configurations', 'Временные конфигурации'),
    ('troubleshooting', 'Diagnostics and troubleshooting', 'Диагностика и решение проблем'),
)


def title(locale):
    return 'Документация' if locale == 'ru' else 'Documentation'


def section(locale):
    from config import APP_SEMVER
    branch = 'dev' if '-' in APP_SEMVER else 'main'
    suffix = '.ru' if locale == 'ru' else ''
    return Section('', inline_rows=tuple((InlineKeyboardButton(
        text=ru if locale == 'ru' else en,
        url=f'https://github.com/saharoktyan/node-plane/blob/{branch}/docs/{name}{suffix}.md',
        style='link'),) for name, en, ru in DOCUMENTS))
