import asyncio
from contextlib import suppress
import os
import logging
from pathlib import Path
import aiohttp
from aiogram import Bot, Dispatcher

from .commands import register_commands
from .backend import BackendClient
from .announcement_delivery import delivery_loop
from .routers import admin_announcements
from .routers import admin_system_cleanup
from .routers import admin_alerts, admin_recovery
from .routers.common import BackendMiddleware, LocaleMiddleware, NotificationStateMiddleware, shutdown_refreshes
from .routers import user, admin_requests, admin_profiles, admin_nodes, admin_settings, admin_node_tools, admin_updates, admin_backups
from .routers import user_devices
from .routers import admin_installation_defaults
from .routers import navigation
from . import update_panels

async def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(name)s | %(message)s')
    token = os.environ['BOT_TOKEN']
    adapter_file = Path(os.environ['NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE'])
    adapter_token = adapter_file.read_text(encoding='utf-8').strip()
    if not adapter_token:
        raise ValueError('backend adapter credential is empty')
        
    base_url = os.environ.get('NODE_PLANE_BACKEND_URL', 'http://127.0.0.1:8080')
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with Bot(token=token) as bot:
            await register_commands(bot)
            backend = BackendClient(session, base_url, adapter_token)
            dispatcher = Dispatcher()
            dispatcher.update.outer_middleware(BackendMiddleware(backend))
            dispatcher.update.outer_middleware(LocaleMiddleware())
            dispatcher.update.outer_middleware(NotificationStateMiddleware())
            dispatcher.update.outer_middleware(navigation.NavigationGuardMiddleware())
            
            dispatcher.include_router(navigation.router)
            dispatcher.include_router(user.router)
            dispatcher.include_router(user_devices.router)
            dispatcher.include_router(admin_installation_defaults.router)
            dispatcher.include_router(admin_requests.router)
            dispatcher.include_router(admin_profiles.router)
            dispatcher.include_router(admin_node_tools.router)
            dispatcher.include_router(admin_nodes.router)
            dispatcher.include_router(admin_settings.router)
            dispatcher.include_router(admin_recovery.router)
            dispatcher.include_router(admin_updates.router)
            dispatcher.include_router(admin_backups.router)
            from .routers import admin_temporary_configs
            dispatcher.include_router(admin_temporary_configs.router)
            dispatcher.include_router(admin_announcements.router)
            dispatcher.include_router(admin_alerts.router)
            dispatcher.include_router(admin_system_cleanup.router)
            
            delivery_task = asyncio.create_task(delivery_loop(bot, backend))
            try:
                update_panels.configure(Path(os.environ.get('NODE_PLANE_SHARED_DIR', '/opt/node-plane/shared'))
                    / 'data' / 'telegram-update-panels.sqlite3')
                await update_panels.resume(bot, dispatcher, backend)
                await dispatcher.start_polling(bot)
            finally:
                await shutdown_refreshes()
                delivery_task.cancel()
                with suppress(asyncio.CancelledError):
                    await delivery_task

if __name__ == '__main__':
    asyncio.run(main())
