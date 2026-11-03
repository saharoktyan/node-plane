"""Shared presentation for delivered notifications and their Inbox entries."""
from datetime import datetime

from .i18n import tr


def is_service(item):
    return item.get('notification_kind', 'announcement') != 'announcement'


def notification_label(item, locale):
    if is_service(item):
        sender = 'System'
    elif item.get('sender_username'):
        sender = '@' + item['sender_username'].lstrip('@')
    else:
        sender = tr(locale, 'notification.administrator')
    date = datetime.fromisoformat(item['created_at']).strftime('%d/%m/%y')
    prefix = '' if item.get('viewed_at') else tr(locale, 'announce.new') + ' · '
    return prefix + tr(locale, 'notification.from', sender=sender) + ' · ' + date


def notification_content(item, locale):
    if is_service(item):
        kind = item['notification_kind']
        values = {**(item.get('payload') or {}), 'service': item['service_title']}
        # An exhausted quota suspended access, limited speed or only reports it.
        body = 'body'
        if kind == 'quota_exhausted' and values.get('suspended'):
            body = 'body_suspended'
        elif kind == 'quota_exhausted' and values.get('throttled'):
            from .routers.admin_speed_limits import rate_label
            body, values['speed'] = 'body_throttled', rate_label(locale, values['throttled'])
        return (tr(locale, f'notification.{kind}.title'), (
            tr(locale, 'notification.from', sender='System'),
            tr(locale, f'notification.{kind}.{body}', **values),
        ))
    sender = item.get('sender_username')
    return (tr(locale, 'announce.notification'),
            ((tr(locale, 'notification.from', sender='@' + sender.lstrip('@')),)
             if sender else ()) + (item['text'],))
