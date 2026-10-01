"""The supported command menu; actions use existing screen/backend paths."""
from aiogram.types import BotCommand
from .i18n import tr

COMMANDS = ('start', 'help', 'id', 'version', 'status')

async def register_commands(bot):
    for language in ('', 'en', 'ru'):
        await bot.set_my_commands(
            [BotCommand(command=name, description=tr(language or 'en', 'command.menu.'+name))
             for name in COMMANDS], language_code=language)
