"""Recover navigation when an administrator loses access to an open panel."""
from aiogram import BaseMiddleware

from .backend import BackendError
from .i18n import normalize_locale
from .navigation import parent_path


def admin_panel(values, callback=None):
    parents = (values.get('navigation_screen') or {}).get('parents') or []
    return (bool(values.get('account_menu_recovery_pending') or values.get('admin_panel_active'))
        or any(p.get('callback') == 'admin_menu' for p in parents)
        or any(p.callback == 'admin_menu' for p in parent_path(callback or '',
            normalize_locale(values.get('locale')), values)))


async def leave_admin_panel(bot, backend, state, user_id, chat_id, message_id, account):
    from .routers.common import cancel_refresh
    from .routers.user import clear_artifacts, show_home
    values = await state.get_data()
    message_id = values.get('control_message_id') or message_id
    cancel_refresh(state)
    await clear_artifacts(bot, chat_id, state)
    await state.clear()
    await state.update_data(locale=normalize_locale(account.get('locale') or values.get('locale')),
        control_message_id=message_id, account_menu_recovery_pending=True,
        **{key: value for key, value in values.items() if key.startswith('notification_')})
    await show_home(chat_id, user_id, bot, backend, state, message_id, account=account)
    await state.update_data(account_menu_recovery_pending=False)


class AdminAccessMiddleware(BaseMiddleware):
    """Check fresh account state before admin routing or draft-discard guards."""
    async def __call__(self, handler, event, data):
        state, backend = data.get('state'), data.get('backend')
        source = getattr(event, 'callback_query', None) or getattr(event, 'message', None)
        if state is None or backend is None or source is None or source.from_user is None:
            return await handler(event, data)
        values = await state.get_data()
        callback = getattr(source, 'data', None)
        if callback and callback.startswith('u:'):
            from .routers.user import actions
            action = actions.get(callback[2:])
            if action and action.owner_id != source.from_user.id:
                return await handler(event, data)
        if not admin_panel(values, callback):
            return await handler(event, data)
        account = await backend.me(source.from_user.id)
        if account.get('role') != 'admin' or account.get('status') != 'approved':
            if callback is not None:
                await source.answer()
            message = getattr(source, 'message', None) if callback is not None else source
            if message is not None:
                await leave_admin_panel(data['bot'], backend, state, source.from_user.id,
                    message.chat.id, message.message_id, account)
            return
        try:
            return await handler(event, data)
        except BackendError as exc:
            if exc.status != 403:
                raise
            account = await backend.me(source.from_user.id)
            if account.get('role') == 'admin' and account.get('status') == 'approved':
                raise
            message = getattr(source, 'message', None) if callback is not None else source
            if message is not None:
                await leave_admin_panel(data['bot'], backend, state, source.from_user.id,
                    message.chat.id, message.message_id, account)
