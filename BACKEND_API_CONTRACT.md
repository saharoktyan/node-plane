# Backend API v1: контракт и авторизация

Дата: 2026-09-28. Статус: проект контракта для реализации; маршруты и новая
авторизация пока не работают. Приоритет — первая вертикаль identity → профиль
→ доступные ноды → выдача конфигов. HTTP-фреймворк выбираем отдельно.

## 1. Обнаруженные сценарии и владельцы

| Сценарий | Текущий код | Новый владелец |
| --- | --- | --- |
| /start, регистрация и привязка профиля | handlers/user_common.py, services/profile_state.py | backend identity/account service |
| Админ и доступ к боту | handlers/admin_common.py, user_common.py, ui/menu.py | backend authorization |
| Запрос, approve/reject, доступ к установке | handlers/user_profile.py, telegram_users | backend access-request service |
| Профиль, срок действия, freeze, назначения протоколов | handlers/admin_wizard.py, services/profile_state.py | backend profile/access service |
| AWG/Xray выдача, восстановление peer/UUID | handlers/user_getkey.py, services/xray.py | backend config-issuance scenario |
| Реестр, pending settings, apply, lifecycle | handlers/admin_server_wizard.py, services/server_registry.py | backend node service |
| Reconcile и построение node.env | driver и Python services | backend desired-state service → explicit driver command |
| История и дедупликация | services/driver_commands.py, Rust driver | backend business command + driver execution journal |
| Обновления, rollout, backups/reset | handlers/user_profile.py, services/updates.py, app_settings.py | backend maintenance/deployment scenarios |
| Диалоги, QR, локализация, сообщения | handlers, utils/tg.py, keyboards.py | Telegram adapter |

Backend владеет бизнес-БД. Ноды и agent не получают её credentials. Нативные
файлы/URI готовит backend; QR и экран формирует интерфейс из подтверждённого
результата. Счётчики и alerts Pro не переносим в активную базовую политику.

## 2. Identity и модель доступа

- Account: внутренний неизменяемый UUID, role `member|admin`, статус доступа
  `pending|approved|rejected|disabled`, revision. Аккаунт не равен VPN-профилю.
- ExternalIdentity: provider + provider_subject → account_id. Для Telegram
  используется числовой user ID, не username, chat_id или имя профиля.
- Profile: неизменяемый UUID, существующее runtime_name, display_name,
  owner_account_id (может отсутствовать у админского профиля), frozen,
  expires_at, desired_revision. Переименование label не меняет runtime identity.
- Grant: profile_id + node_key + protocol `awg|xray`; один Xray grant разрешает
  доступные транспорты tcp/xhttp, не создаёт самостоятельных тарифных сущностей.
- Principal: проверенные credentials клиента, тип `adapter|account|service`,
  scopes, состояние/revocation. Actor: конечный account либо сервисный actor.

Решение доступа вычисляет backend: аутентифицированный principal + разрешённая
делегация + действующий actor + permission + ownership + состояние ресурса.
`admin` не отменяет frozen/expired при обычной выдаче рабочего конфига.
Для админской выдачи чужого конфига нужна отдельная permission и аудит.

Первоначального администратора назначает локальный bootstrap установщика
по явно заданному Telegram ID. Не включать auto-admin для первого публичного
запроса. Бот не хранит авторитетный список ADMIN_IDS.

По решению пользователя текущая разработка не обязана сохранять существующие
telegram_users, profiles, ноды и конфиги. Предпочтительна чистая целевая схема.
После её стабилизации — ручное добавление прошлых пользователей или небольшой
импорт Telegram identities из старой БД. Совпадение username не связывает
аккаунты; grants к старым нодам и VPN credentials не импортируются.

## 3. Аутентификация v1

Backend первоначально слушает localhost. Даже на localhost запросы требуют
аутентификации. Credentials backend не являются токеном Telegram Bot API.

В первой поставке — случайные opaque bearer tokens, выпущенные локальным
backend-admin инструментом: ID токена, хеш секрета, тип principal, scopes,
created_at, expires_at, revoked_at. Полный секрет возвращается только при
создании, хранится в файле 0600 у клиента; не попадает в URLs, аудит и логи.
Все поля токена/срок/отзыв проверяются backend. Установщик и backend доступны
для восстановления credentials без рабочего бота.

Telegram adapter token разрешает `identity.telegram.resolve` и `delegate.telegram`.
Для делегированного запроса:

```http
Authorization: Bearer <adapter-secret>
X-Node-Plane-Telegram-User-ID: 123456789
```

Backend проверяет тип token и scopes, находит привязанную identity и вычисляет
actor. При отсутствии identity делегированные бизнес-запросы возвращают
`identity_not_registered`; регистрация — отдельный запрос. Число в заголовке
не является доказательством личности без доверенного adapter principal.

Обычный account token уже связан с account_id; делегационные заголовки для
него запрещены. Service actor получает только конкретные permissions фонового
сценария и не выдаёт себя за человека. Role/scopes/actor из JSON игнорировать
нельзя: недопустимые поля отклоняются. Эффективные права — пересечение
разрешений principal, actor и resource policy.

Доверительная граница: Telegram adapter обладает возможностью представлять
привязанных Telegram пользователей, включая админов. Компрометация adapter
credentials нарушает эту границу; скрытые поля/кнопки этого не исправляют.
Для будущих Web/CLI не используем этот токен или доверенные заголовки:
они получают собственные account credentials. Browser session/login и
удалённая публичная экспозиция не входят в первую вертикаль.

Отзыв credentials и смена доступа вступают в силу на следующем запросе,
не только после перезапуска. Новые mutating команды повторно проверяют права
при dispatch; уже отправленная удалённая операция при отзыве автоматически
не отменяется. Обслуживание/revoke не блокируется из-за frozen VPN-профиля.

## 4. Permissions v1

| Permission | Member | Admin |
| --- | --- | --- |
| account.self.read, account.self.preferences.write | свой account, включая pending | свой account |
| access_requests.self.create/read | свой запрос, если requests включены | свой запрос |
| profiles.self.read, nodes.available.read | approved, собственные профили/назначения | собственные профили/назначения |
| configs.self.issue/read | approved, ownership, действующий profile и grant | те же проверки |
| access_requests.manage, accounts.manage | нет | да |
| profiles.manage, grants.manage | нет | да |
| configs.manage.issue/read | нет | да, аудит и действующий профиль |
| nodes.manage, nodes.inspect, nodes.execute | нет | да |
| operations.read | свои команды, отфильтрованные результаты | все команды |
| maintenance.manage, settings.manage | нет | да |
| diagnostics.sensitive.read | нет | да, без secret payload по умолчанию |

Admin не получает автоматически права чтения любых private keys в любой
операции: credential результат выдачи защищён configs permission отдельно.
Disabled account не может выполнять бизнес-запросы. Pending/rejected может
читать собственный статус; возможность нового запроса определяется политикой.

Features endpoint отражает загруженные модули и доступные actor возможности;
это подсказка клиенту, не замена permission/licence проверки на маршруте.
Pro-маршруты регистрируются модулем; без модуля отсутствуют. Core самостоятельно
работает без лицензии; read/export/revoke Pro-данных при истечении лицензии
определяются политикой модуля, а не глобальным отключением backend.

## 5. HTTP и модели

Префикс `/api/v1`, JSON UTF-8, UTC timestamps, opaque UUID IDs. Node key
сохраняется из реестра и валидируется; внутренний label не используется как ID.
Списки: `items`, opaque `next_cursor`, limit 1..100 (default 25), стабильный
порядок. Secrets отсутствуют в list/status ответах. Не возвращать таблицы БД
как API-модели. Поля новых запросов валидируются строго.

Ответ ошибки:

```json
{"error":{"code":"profile_frozen","message":"Profile is frozen",
"request_id":"uuid","details":{"profile_id":"uuid"}}}
```

Коды: 401 invalid/expired/revoked token; 403 permission_denied либо ограничение
профиля; 404 для неизвестного или чужого ресурса; 409 revision_conflict,
idempotency_conflict, request_already_decided; 422 invalid_input;
503 dependency_unavailable. Нет grpc traceback, SQL, shell output или credentials.
Клиент локализует code; message диагностический, не parseable бизнес-результат.

Чтения возвращают revision/ETag; изменения существующих desired ресурсов
требуют `If-Match` точной revision (без него 428, устаревшая 412). Request
ожидает только сохраняемые параметры, не произвольный shell/script/SQL.
Для команды apply/reinstall тело содержит проверяемый expected_desired_revision.

## 6. Маршруты первой вертикали

| Метод/путь | Назначение и доступ |
| --- | --- |
| GET /health/live | минимальный liveness, без auth и внутренней диагностики |
| GET /health/ready | readiness без чувствительных данных; 503 при неготовности |
| POST /api/v1/integrations/telegram/identities/resolve | только adapter scope; body telegram_user_id, username/display_name как metadata; идемпотентная регистрация member без выдачи доступа |
| GET /api/v1/me | статус actor, role, permissions; без VPN-ключей |
| PATCH /api/v1/me/preferences | язык; не role/access |
| GET /api/v1/features | code, available, reason_code; без Telegram callback/markup |
| GET /api/v1/me/profiles | собственные профили |
| GET /api/v1/profiles/{id} | self.read или profiles.manage; безопасная модель |
| GET /api/v1/me/nodes | назначенные enabled ноды, label, доступные протоколы/транспорты; без SSH credentials |
| POST /api/v1/profiles/{id}/config-issuances | grant + ownership/configs.manage; body node_key, protocol, transport, format; создаёт команду подготовки актуального конфига |
| GET /api/v1/operations/{id} | состояние доступной actor операции |
| GET /api/v1/config-issuances/{id}/artifact | повторная текущая проверка доступа, подтверждённая revision; файл/URI, Cache-Control: no-store |

Варианты выдачи: awg `vpn_uri|vpn_file|conf_file`, xray `vless_uri` + transport
`tcp|xhttp`. Не выдавать конфиг при pending server settings, frozen/expired,
отозванном grant, неизвестной актуальности или недоступном агенте. Backend
возвращает filename/media_type и credential artifact; не QR и инструкции UI.
Если revision/grant/server identity изменилась после подготовки, artifact
возвращает `config_stale`/403 и нужна новая issuance, старый кеш не выдаётся.
Артефакты имеют ограниченную retention; не включаются в журнал общего чтения
операций. Точный срок retention и доставка бинарных файлов уточняются при
реализации. Ответы credentials и request body не логировать.

## 7. Следующие группы API

- Access requests: POST/GET `/me/access-requests`, admin GET `/access-requests`,
  POST `/{id}/decision` с approve/reject; закрытый запрос нельзя решить повторно
  другим решением. Approval account не означает grant к каждому VPN-серверу.
- Accounts: admin list/read, change access/role, связывание профиля через
  проверенную процедуру; защита от удаления последнего admin. Нельзя принимать
  пользовательское изменение owner_account_id как обычное редактирование.
- Profiles: admin create/edit/delete, PATCH grants с desired_revision,
  freeze/unfreeze. Удаление — команда отзыва runtime, затем удаления данных,
  с описанным partial failure; не безусловное DELETE бизнес-записи.
- Nodes: admin list/create/read/edit desired settings; GET diagnostics/runtime;
  POST commands с закрытым enum: probe, check_ports, open_ports, install_docker,
  bootstrap, reinstall, apply_settings, sync_runtime, reconcile, regenerate_awg,
  remove_node. Настройки отдельно показывают desired/applied revisions.
- Remove node: полноценная очистка по умолчанию; registry-only — отдельная
  подтверждаемая команда, доступная после typed unreachable/timeout результата,
  с сохранённым неизвестным исходом VPS. UI не принимает решение по строке ошибки.
- Operations: list с фильтрацией actor/resource до limit; safe result summary,
  typed errors, parent/child IDs. Проверки configs permission при чтении secrets.
- Maintenance: settings, update check/plan/execute, driver-agent rollout,
  backups/export/restore, reset; закрытые параметры и отдельная permission.
  Не создавать generic execute-shell endpoint. Backend не уничтожает сам себя
  до долговечного принятия update/reset ответственным исполнителем.
- Notifications: backend создаёт события с recipient account ID; адаптер
  доставляет их через свою identity mapping. Не хранить Telegram message IDs
  как идентификаторы бизнес-запросов/операций.

## 8. Команды и долговечность

Mutating запросы, resolve и issuance требуют `Idempotency-Key` (UUID).
Scope: installation + principal + actor + ключ; fingerprint включает method,
route, canonical body и expected revision. Другая команда с тем же ключом →409.
Повтор проверяет текущую авторизацию, возвращает прежний ID/результат, не
исполняет удалённое действие заново. Не выдавать старый credential результат
после изменения доступа. Retention command keys определяет безопасное окно
повторов; клиент не делает повтор после него без проверки outcome. Политика
удаления ключей фиксируется перед реализацией, не чистится независимо от истории.

Бизнес-команда сохраняется в PostgreSQL вместе с desired state и outbox до
remote dispatch. Executor забирает её с lease и сохраняет driver command_id;
повтор dispatch использует тот же driver ID. До такой реализации HTTP 202
не означает долговечное принятие: существующий синхронный driver RPC сам
по себе не является очередью backend.

Целевой HTTP 202: `{operation_id, status_url}`, без secret artifact.
Статусы: pending/running/succeeded/failed/unknown_outcome. Partial failures
сохраняют результаты по нодам и желаемое состояние, не маскируют partial как
успех. После crash/timeout неизвестный исход требует inspection/reconcile;
lease expiry не разрешает новую мутацию с новым ключом. История driver должна
оставаться доступной до разрешения ambiguity. Per-node dispatch координируется
для profile/node commands; бизнес-операция может иметь несколько driver children.

## 9. Приёмка авторизации до подключения нового бота

- Неавторизованный principal, revoked/expired token и disabled actor отклоняются.
- Account token не может представляться другим actor; adapter без delegation
  scope не может подставить Telegram ID; unknown identity не получает auto-admin.
- Username collision не связывает чужой профиль; будущий импорт идентифицирует
  пользователей по Telegram ID и не назначает admin автоматически.
- Member не видит чужие profiles/operations/artifacts и SSH/private credentials.
- Frozen/expired/revoked grant блокирует issuance и старые artifact URLs.
- Approve/reject нельзя выполнить конфликтующим повтором; approve не создаёт
  grants автоматически.
- Повтор команды не повторяет remote mutation; неверная revision отклоняется
  до изменения desired state/dispatch. Права проверяются на сервере.
- Пропавший agent/driver и crash backend сохраняют неопределённость outcome.
- Pro-кнопка/feature flag не обходит backend permission/license checks.

## 10. Следующая конкретная работа

Перенести resolve/me/profiles/available nodes в backend application layer без
Telegram imports; добавить authorization policy и тесты ownership/delegation.
Затем оформить OpenAPI первой вертикали и HTTP transport. Config issuance
подключать с command journal/executor, сохраняя гарантии актуальности и runtime
identity. До этого документ — контракт цели, не обещание готовых HTTP ручек.

## Реализация первой части — 2026-09-28

В app/backend добавлены независимые от Telegram модели principal/account/actor,
policy permissions и ownership, проверки grant/freeze/expiry для выдачи и чтения
artifact, сервис resolve/me и SQL-репозиторий identity с явной инициализацией
схемы. Регистрация создаёт pending member, не связывает профиль по имени и
не назначает администратора. Повтор сохраняет identity без лишних аккаунтов.

Проверки principal относятся к уже аутентифицированному объекту: этот слой не
проверяет bearer-секрет. Token storage/verifier, bootstrap администратора и profile ownership, HTTP API, features и config issuance executor
ещё не реализованы. Существующий бот не переключён на этот backend.
SQLite-тесты SQL-репозитория и policy проходят; конкурентная регистрация на
настоящем PostgreSQL остаётся обязательной интеграционной проверкой.

## Credentials и bootstrap — реализация

Добавлен CredentialService: opaque bearer verifier, хеш секрета в БД,
scopes/type/account binding, expiry и revoke. Локальный backend.admin_cli
создаёт схему, назначает approved admin по явно заданному Telegram ID,
выпускает credentials в новый файл 0600 и отзывает их по ID. Не реализуется
как HTTP route. Срок по умолчанию — 30 дней; локальный rotation — выпуск
нового токена, переключение клиента и revoke старого. Не выдаём секрет в stdout.

Восемь дополнительных тестов проверяют неверные credentials, изменение секрета,
expiry/revoke, ограничения scopes, bootstrap, смену роли и защиту secret-файла.
HTTP transport и service-actor workflows остаются следующими шагами.

## HTTP первая вертикаль — реализация

Добавлен FastAPI transport с фабрикой backend.http_api:application, localhost
запуском через Uvicorn, /health/live, /health/ready, Telegram identity resolve
и /api/v1/me. Bearer verifier создаёт principal; actor разрешается backend.
Input models строгие, роль/account delegation из body не принимается; ошибки
редактируются в безопасный envelope с request ID. OpenAPI содержит bearer scheme,
actor/key headers; snapshot — backend-openapi.json.

Resolve требует UUID Idempotency-Key, сохраняет ключ и target Telegram ID
атомарно с identity, конфликт ключа →409. Этот журнал не является очередью
долгих операций. HTTP schema readiness пока проверяет только identity slice.
GET profiles/nodes/features, config issuance, lifecycle commands и installer
service integration ещё отсутствуют. Действующий бот не переключён.

## Profiles / available nodes — реализация

Добавлены чистые backend_profiles/backend_nodes/backend_grants. Профиль имеет
UUID, отдельное runtime/display имя, nullable owner, frozen/expiry и revision.
CLI init-schema создаёт таблицы; create-profile доступен только локально.

GET /me/profiles, /profiles/{id}, /me/nodes подключены с авторизацией и keyset
pagination. Фильтрация ownership выполняется до limit. Ноды фильтруются по
active owned grants, enabled, поддерживаемому protocol и состоянию профиля.
Ответы типизированы в OpenAPI и не содержат SSH/ключей/runtime payload.

Семь новых HTTP проверок проходят. API изменения профиля/назначений, управление
нодами и перевод driver с legacy таблиц на явные задания пока не реализованы.
Чистые read models не подменяют существующий provisioning и не создают grants.

## Profile mutations — реализация

POST /profiles, PATCH /profiles/{id}, PATCH /profiles/{id}/grants реализованы
для desired state с permission checks, UUID command key, optimistic If-Match,
ETag, атомарной заменой grants и сохранённым результатом повтора. Owner назначается
только при create, runtime_name генерируется и остаётся неизменяемым. Nullable
expiry нормализуется в UTC; frozen принимает только boolean.

Ответ содержит profile, grants и runtime_status=not_dispatched: dispatch,
executor, отзыв/создание пользователей на нодах и applied revision ещё не
реализованы. Не возвращаем 202/успешную runtime-операцию за простую запись БД.
DELETE profile остаётся закрытым до реализации подтверждённого runtime cleanup.
Восемь дополнительных HTTP-тестов проверяют permissions, replay, revision
conflicts, atomic grants и validation. PostgreSQL concurrency ещё не проверен.


### Журнал операций нового backend — 2026-09-28

Реализован атомарный outbox: изменения профиля/grants, ответ для повтора команды,
operation и задачи по node/protocol сохраняются одной транзакцией. Ответ содержит
operation_id и runtime_status: awaiting_executor либо no_targets. Это заменяет
прежний промежуточный not_dispatched. GET /api/v1/operations/{id} проверяет actor
и permissions; внутренние intent snapshots и runtime_name не выдаются.

Отзыв учитывает прежние grants и исторические цели операций; freeze и уже
истёкший expires_at создают delete intent. Повтор команды возвращает прежнюю
operation без повторной постановки. Проверены rollback при сбое outbox,
изоляция чужих операций и отсутствие внутренних полей в HTTP-ответах.

Исполнитель пока не подключён. Старый provisioning RPC driver всё ещё читает
legacy бизнес-таблицы, поэтому следующий шаг — явный контракт исполнения,
стабильные credentials, сериализация задач по ноде и recovery неопределённого
результата. Автоматическая обработка будущего истечения срока тоже впереди.
Проверки на реальном PostgreSQL и ноде остаются обязательными.


### Явный driver-контракт и первый исполнитель — 2026-09-28

Добавлен ProvisioningService.ApplyProfileIntent: один node/protocol/action,
revision, runtime_name и явная Xray identity. Требуется стабильный command ID;
новый RPC не читает и не меняет прежние бизнес-таблицы. Driver сохраняет результат
и отклоняет изменённое задание с прежним ID. Ошибка агента считается неизвестным
результатом, а не доказательством отсутствия удалённых изменений.

Новый backend хранит постоянные Xray UUID/short ID и передаёт их в intent.
Добавлен отдельный CLI executor: атомарный claim перед RPC, приватное сохранение
результата, порядок ревизий и блокировка очереди ноды при неизвестном результате.
После перезапуска running-задачи блокируются; слепого повтора нет. Один worker
host, обязательный общий file lock; распределённое исполнение не заявлено.

Это реализация для новой тестовой схемы: прежний промежуточный backend требует
чистой БД, init-schema не выполняет миграцию изменённых ограничений. Текущие
установки автоматически не очищаются. Исполнитель ещё не подключён к установщику
и боту. Впереди recovery/reconciliation, плановый expiry, выдача конфигов и
интеграционные проверки на PostgreSQL с настоящими driver/agent.


### Восстановление подтверждённого результата — 2026-09-28

Добавлен read-only RPC GetOperationByCommand и восстановление blocked-задач
по сохранённому command ID. Успешный результат ApplyProfileIntent с совпадающими
node/runtime profile восстанавливает статус и приватный payload без повторной
мутации. Следующие ревизии после этого могут исполняться. Missing/running/failed,
несовпадение цели и ошибка связи оставляют ноду заблокированной.

Сверка по ListRemoteProfiles намеренно не используется как доказательство:
агент читает файлы конфигов, а не подтверждённое live-состояние AWG/Xray.
Полная reconciliation для interrupted/failed результатов ещё впереди;
автоматическая повторная выдача доступа в этом случае не выполняется.


### Live-проверка профилей — 2026-09-28

Добавлены agent InspectProfile и driver InspectProfileIntent. Read-only runtime
helper проверяет Xray через HandlerService для TCP/XHTTP (UUID + flow), AWG через
wg show dump внутри контейнера (peer key + PSK + AllowedIPs), сравнивает результат
с файлом под общим mutation lock. RPC возвращает только булевы признаки.
Отсутствие сохранённой AWG peer identity, ошибка API/контейнера/конфига и
некорректный ответ означают unknown, а не отсутствие доступа.

Worker сохраняет наблюдения для blocked-задач; GET operation показывает inspection
и inspected_at. Неудачная повторная проверка заменяет прежнее наблюдение на
available=false. Наблюдение не разблокирует failed/interrupted команды:
необходимо исключить позднее применение старого запроса через fenced execution
и repair-контракт. Автоматически восстанавливается только подтверждённый успех
из журнала driver. Проверки с настоящими контейнерами и PostgreSQL впереди.
Промежуточная схема outbox изменилась; приёмка — на чистой тестовой БД.


### Ревизии и журнал исполнения агента — 2026-09-28

Явные задания нового backend теперь проходят agent ApplyProfileIntent. Durable
SQLite-журнал сохраняет command ID, fingerprint всего payload, revision fence
по profile/protocol и результат. Общий flock сериализует мутации; revision и
running записываются до запуска runtime. Старые ревизии и конфликтующие ID
отклоняются, успешные повторы возвращают сохранённый AWG/result без мутации.

Незавершённая/ошибочная внешняя операция сохраняет running и блокирует дальнейшие
явные задания ноды, включая после рестарта. Read-only RecoverProfileIntent
проверяет полный payload и позволяет восстановить успех агента, завершившегося
после таймаута driver. Backend использует его при missing/failed driver record.
Это закрывает позднее успешное завершение; действительно прерванные команды
требуют отдельного audited repair/reset, автоматического обхода блокировки нет.

Граница гарантии — новый explicit backend path. Legacy RPC, node maintenance и
ручные правки пока не координируются этим журналом; их интеграция ещё впереди.
Runtime cleanup сохраняет fence. Полное удаление агента сначала останавливает
сервис и дочерние процессы, затем удаляет journal/lock вместе с его файлами.
Проверки на PostgreSQL и настоящих контейнерах остаются впереди.


### Защита перехода от legacy RPC — 2026-09-28

Legacy Add/Delete Xray/AWG на обновлённом агенте теперь используют общий lock и
проверяют revision fence перед запуском скрипта. Для backend-managed профиля
старая команда отклоняется; другие legacy-профили продолжают работать. Проверка
и запуск ребёнка держат один lock, так что между ними не может появиться новая
ревизия. Повреждённый журнал либо отсутствие helper при существующем журнале
означают fail-closed.

Legacy init/deploy/apply-settings/sync-xray и регенерация AWG entropy блокируются
при наличии любого fence. SyncNodeEnv, DeleteRuntime и запись активных конфигов
через SyncRuntimeFiles также получают maintenance guard; обновление packaged
runtime-кода остаётся возможным. UninstallAgent запрещён для fenced ноды и
сохраняет marker до фактической остановки сервиса, закрывая окно отложенного
systemd удаления. Если scheduling не удался, marker остаётся для ручной проверки.

Контракт контролируемого repair/decommission всё ещё нужен. Legacy full cleanup
сейчас отказывается удалять fenced ноду, чтобы не потерять журнал. Прямые
root/операторские изменения Docker и файлов вне RPC-гарантии. Изменение ещё не
выкачено на тестовые VPS и требует совместного обновления agent/runtime assets.


### Административное восстановление blocked — 2026-09-28

Добавлен явный ResolveProfileIntent и локальная команда backend.admin_cli
resolve-blocked (approved admin account + тот же file lock, что у worker).
После systemd-рестарта агента новая instance ID позволяет под общим lock
сверить live-состояние и навсегда пометить старый command ID как superseded.
Без рестарта либо при недоступной инспекции блокировка остаётся. Старая мутация
не повторяется. Backend атомарно помечает старые задачи profile/node superseded,
увеличивает desired_revision и ставит актуальное состояние в outbox. Результат
фиксируется в backend_repairs; если удалённый шаг прошёл, но транзакция backend
упала, повтор получает прежний agent audit record. Дальше worker применяет
новую ревизию.

Это trusted-local maintenance, не Telegram/public HTTP. Гарантия требует
systemd-остановки прежней process group; ручные root-операции и потенциально
задержавшееся действие Docker daemon за этой границей. Проверка на реальной
ноде обязательна перед включением сценария в установку. Если для AWG утрачен
peer identity, инспекция не подтверждает отсутствие и repair отказывается.
Backend-owned decommission и обновление настроек протоколов ещё впереди.
