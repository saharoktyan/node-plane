"""Shared presentation for delivered notifications and their Inbox entries."""
from datetime import datetime

from .i18n import tr


def notification_label(item, locale):
    if item.get('notification_kind') == 'access_expired':
        sender = 'System'
    elif item.get('sender_username'):
        sender = '@' + item['sender_username'].lstrip('@')
    else:
        sender = tr(locale, 'notification.administrator')
    date = datetime.fromisoformat(item['created_at']).strftime('%d/%m/%y')
    prefix = '' if item.get('viewed_at') else tr(locale, 'announce.new') + ' · '
    return prefix + tr(locale, 'notification.from', sender=sender) + ' · ' + date


def notification_content(item, locale):
    if item.get('notification_kind') == 'access_expired':
        return (tr(locale, 'notification.access_expired.title'), (
            tr(locale, 'notification.from', sender='System'),
            tr(locale, 'notification.access_expired.body', service=item['service_title']),
        ))
    sender = item.get('sender_username')
    return (tr(locale, 'announce.notification'),
            ((tr(locale, 'notification.from', sender='@' + sender.lstrip('@')),)
             if sender else ()) + (item['text'],))
