[English version](install.md)

# Установка контроллера

Это руководство описывает установку Node Plane непосредственно на Linux-сервере. Для установки с компьютера используйте [Workstation](workstation.ru.md).

## Требования

- Linux с systemd и root/sudo-доступом; Debian/Ubuntu для автоматической установки Docker.
- Python 3.11 или 3.12, Git для метаданных релизов, CA-сертификаты и доступ к GitHub и Telegram.
- Токен Telegram-бота и числовые Telegram ID первых администраторов.
- PostgreSQL: существующая БД или локальный Docker-контейнер установщика.

Установщик выбирает Python 3.12 или 3.11. Переменная `NODE_PLANE_PYTHON_BIN` позволяет указать интерпретатор. Каталог загрузки должен находиться вне `/opt/node-plane`.

## Загрузка и установка

Выполните на сервере от root:

```sh
curl -fsSL https://raw.githubusercontent.com/saharoktyan/node-plane/dev/scripts/controller_release.py -o /tmp/node-plane-controller-release.py
python3 /tmp/node-plane-controller-release.py download --branch main --destination /opt/node-plane-install
cd /opt/node-plane-install
./scripts/install.sh --mode simple
```

`--branch dev` выбирает опубликованный релиз разработки, `--branch main` — стабильный. Для конкретного тега добавьте к команде загрузки `--ref vX.Y.Z`. Для загрузки нужен опубликованный релиз с артефактами.

Введите токен бота и ID администраторов. Оставьте `POSTGRES_DSN` пустым для локальной PostgreSQL или укажите DSN существующей БД. Установщик создаёт Python-окружение, схему БД, службы контроллера и драйвер.

## Установка без диалога

Создайте приватный файл окружения по [`.env.example`](../.env.example). Заполните `BOT_TOKEN` и `ADMIN_IDS`; числовые ID разделяются запятыми. Для внешней БД укажите `POSTGRES_DSN`.

```sh
chmod 600 /root/node-plane.env
./scripts/install.sh --mode simple --non-interactive --env-file /root/node-plane.env
```

Храните файл приватно. Не отправляйте его содержимое в журналы и сообщения поддержки.

## Параметры окружения

| Переменная | Значение |
| --- | --- |
| `BOT_TOKEN` | Токен Telegram-бота |
| `ADMIN_IDS` | Числовые Telegram ID первых администраторов |
| `POSTGRES_DSN` | Подключение внешней PostgreSQL; пустое значение выбирает локальную |
| `NODE_PLANE_BASE_DIR` | Корень установки; по умолчанию `/opt/node-plane` |
| `NODE_PLANE_SHARED_DIR` | Постоянные данные; по умолчанию `/opt/node-plane/shared` |
| `NODE_PLANE_UPDATE_BRANCH` | Ветка релизов: `dev` или `main` |
| `SSH_KEY` | Приватный SSH-ключ контроллера для удалённых нод |

Полный шаблон: [`.env.example`](../.env.example). Созданные установщиком значения хранятся в общем окружении. Изменение настроек подключения не переносит существующую БД или каталог установки.

## Проверка установки

```sh
cd /opt/node-plane/current
./scripts/healthcheck.sh --mode simple
systemctl status node-plane-backend node-plane-telegram node-plane-driver --no-pager
systemctl status node-plane-backend-worker --no-pager
```

Откройте бота от администратора и отправьте `/start`. Создайте [ноду](nodes.ru.md), запустите её установку и [выдайте доступ профилю](access.ru.md).

## Quick Start

В боте кнопка **Быстрый старт** доступна администратору в главном меню. В NPW прогресс показывается справа от сайдбара, когда раздел не выбран; из любого раздела руководство можно открыть через **F1**. NPW обновляет прогресс раз в секунду, пока руководство открыто. При переходе в другой раздел опрос прекращается.

В NPW руководство проверяет настройки серверов, первую ноду и применение протоколов. **Confirm installation defaults for servers** подтверждает значения из **Node Plane Settings → Basic → Installation defaults**. Кнопка **Continue** ведёт к следующему шагу; до установки использует уже выбранный профиль. Создание устройства и получение обычного конфига выполняются в Telegram-боте, где руководство также проверяет выдачу конфига. Полученный конфиг нужно импортировать в VPN-клиент и проверить соединение.

После **Готово / Done** руководство больше не показывается этому администратору в данной установке. Завершение NPW отключает F1 и сохраняется отдельно от бота; в боте требуется успешная выдача AWG- или VLESS-конфига.

## Файлы и службы

| Путь | Содержимое |
| --- | --- |
| `/opt/node-plane/releases/` | Установленные релизы контроллера |
| `/opt/node-plane/current` | Активный релиз |
| `/opt/node-plane/shared/` | Окружение, данные БД, учётные данные, бэкапы и журналы |
| `/etc/node-plane/` | Конфигурация локальной ноды, если установлен локальный агент |

Основные службы: `node-plane-backend`, `node-plane-telegram`, `node-plane-driver`, служба worker. У каждой управляемой ноды свой агент. Локальный агент контроллера может использовать только одна локальная нода.

Обновление, сброс и удаление описаны в [Обслуживании](maintenance.ru.md). При ошибках установки и проверки см. [Устранение неполадок](troubleshooting.ru.md).
