"""Standalone final verifier copied outside the installation before removal."""

import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import Request, urlopen


def verified(config, exit_code):
    if exit_code or any(Path(path).exists() or Path(path).is_symlink()
                        for path in config['paths']):
        return False
    for unit in config['units']:
        path = Path(config['units_root']) / unit
        if path.exists() or path.is_symlink():
            return False
        result = subprocess.run(['systemctl', 'is-active', unit],
            capture_output=True, text=True, timeout=10)
        if result.returncode not in (3, 4) or result.stdout.strip() not in ('inactive', 'unknown', 'failed'):
            return False
    container = config.get('postgres_container')
    if container:
        result = subprocess.run(['docker', 'ps', '-a', '--format', '{{.Names}}'],
            capture_output=True, text=True, timeout=15)
        if result.returncode or container in result.stdout.splitlines():
            return False
    return True


def finish(config, exit_code):
    try:
        success = verified(config, exit_code)
    except (OSError, subprocess.TimeoutExpired):
        success = False
    ru = config['locale'] == 'ru'
    text = ('Пока! Node Plane удалён.' if ru else 'Bye-bye! Node Plane has been removed.') if success else (
        'Удаление Node Plane не подтверждено. Проверьте журнал удаления systemd.' if ru else
        'Node Plane removal could not be verified. Check the systemd removal journal.')
    payload = json.dumps({'chat_id': config['chat_id'], 'message_id': config['message_id'],
        'text': text, 'reply_markup': {'inline_keyboard': []}}).encode()
    # Do not print exceptions: HTTP request URLs contain the bot token.
    for attempt in range(3):
        try:
            request = Request('https://api.telegram.org/bot' + config['token'] + '/editMessageText',
                data=payload, headers={'Content-Type': 'application/json'})
            with urlopen(request, timeout=15) as response:
                if json.load(response).get('ok'):
                    return
        except Exception:
            pass
        if attempt < 2:
            time.sleep(2)
    print('Could not deliver the final removal notification', file=sys.stderr)


if __name__ == '__main__':
    finish(json.loads(Path(sys.argv[1]).read_text()), int(sys.argv[2]))
