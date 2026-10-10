"""Persist only open update-panel identities, never commands or credentials."""
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

_path = None
_panels = {}


def identity(state):
    key = getattr(state, 'key', None)
    return json.dumps(asdict(key), sort_keys=True) if key is not None else None


def configure(path):
    global _path, _panels
    _path = Path(path)
    _path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(_path) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS panels (identity TEXT PRIMARY KEY, payload TEXT, saved REAL)')
        conn.execute('DELETE FROM panels WHERE saved < ?', (time.time() - 7 * 86400,))
        _panels = {row[0]: json.loads(row[1]) for row in conn.execute('SELECT identity,payload FROM panels')}
    _path.chmod(0o600)


def forget(state):
    key = identity(state) if _path else None
    if key in _panels:
        with sqlite3.connect(_path) as conn:
            conn.execute('DELETE FROM panels WHERE identity=?', (key,))
        _panels.pop(key, None)


async def remember(state, job_id, message_id, *, page=0, opened=False):
    key = identity(state) if _path else None
    if key is None:
        return
    data = await state.get_data()
    value = dict(job_id=job_id, message_id=message_id, page=page, opened=opened,
                 locale=data.get('locale', 'en'))
    if _panels.get(key) == value:
        return
    with sqlite3.connect(_path) as conn:
        conn.execute('INSERT INTO panels VALUES (?,?,?) ON CONFLICT(identity) DO UPDATE '
            'SET payload=excluded.payload,saved=excluded.saved',
            (key, json.dumps(value), time.time()))
    _panels[key] = value


async def resume(bot, dispatcher, backend):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from .routers.admin_updates import show_job
    from .routers.common import schedule_refresh
    for encoded, panel in list(_panels.items()):
        key = StorageKey(**json.loads(encoded))
        if key.bot_id != bot.id:
            continue
        state = FSMContext(storage=dispatcher.storage, key=key)
        await state.update_data(locale=panel['locale'], control_message_id=panel['message_id'])
        query = SimpleNamespace(from_user=SimpleNamespace(id=key.user_id),
            message=SimpleNamespace(chat=SimpleNamespace(id=key.chat_id), message_id=panel['message_id']))
        async def refresh(query=query, state=state, panel=panel):
            await show_job(query, bot, backend, state, panel['job_id'],
                page=panel['page'], opened=panel['opened'])
        schedule_refresh(state, refresh, interval=0)
