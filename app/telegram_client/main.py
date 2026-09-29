import asyncio
import os
from pathlib import Path
import aiohttp
from aiogram import Bot, Dispatcher

from .backend import BackendClient
from .routers.common import BackendMiddleware
from .routers import user, admin_requests, admin_profiles, admin_nodes, admin_settings

async def main() -> None:
    token = os.environ['BOT_TOKEN']
    adapter_file = Path(os.environ['NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE'])
    adapter_token = adapter_file.read_text(encoding='utf-8').strip()
    if not adapter_token:
        raise ValueError('backend adapter credential is empty')
        
    base_url = os.environ.get('NODE_PLANE_BACKEND_URL', 'http://127.0.0.1:8080')
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with Bot(token=token) as bot:
            backend = BackendClient(session, base_url, adapter_token)
            dispatcher = Dispatcher()
            dispatcher.update.outer_middleware(BackendMiddleware(backend))
            
            dispatcher.include_router(user.router)
            dispatcher.include_router(admin_requests.router)
            dispatcher.include_router(admin_profiles.router)
            dispatcher.include_router(admin_nodes.router)
            dispatcher.include_router(admin_settings.router)
            
            await dispatcher.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
