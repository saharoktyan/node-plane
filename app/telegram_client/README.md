# Telegram client

The aiogram client calls the backend HTTP API with its private adapter credential. It owns localized screens, navigation, drafts and message delivery. Backend services own authorization and mutations.

The supported controller installer configures `node-plane-telegram.service`, the backend URL and credential file. For a configured development environment:

```sh
PYTHONPATH=app .venv/bin/python -m telegram_client.main
```

Use a separate development bot token and database. Do not run another poller against a production token.

Read [Telegram interface development](../../docs/development/telegram-ui.md) for rendering, callbacks and progress. User instructions are in [Telegram](../../docs/telegram.md) ([Russian](../../docs/telegram.ru.md)). Testing is described in [Development and testing](../../docs/development/testing.md).
