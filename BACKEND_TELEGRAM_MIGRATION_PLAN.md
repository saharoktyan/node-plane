# План отдельного backend и нового Telegram-интерфейса

Current-scope note: this migration still targets the existing VPN workflows.
Future curated services and the indefinite deferral of Pro/licensing are
described in [SERVICE_EXPANSION_ARCHITECTURE.md](SERVICE_EXPANSION_ARCHITECTURE.md).
Any Pro-related steps below are historical options, not current work items.

## Updated execution order — 2026-09-28

Finish the backend product scenarios and operational integration before building
the replacement Telegram client. The existing PTB v13 bot remains the test
interface for its legacy stack during this period; it is not connected to the
new backend through temporary production adapters. Exercise the new backend
directly through API tests, local administration and disposable-node tests.

"Backend complete" means the target API supports account and access-request
management, VPN profile/grant lifecycle, current AWG/Xray config issuance,
node registration/settings/provisioning/removal, operations and recovery, and
the controller maintenance needed for an ordinary installation. It also means
the backend and worker run as installed services, use PostgreSQL, and pass the
end-to-end tests without a Telegram process. This is a bounded migration
milestone, not a promise to implement future privacy services or Pro features.

Only then create a new aiogram 3 Telegram client. Use the old bot as a map of
user journeys and navigation, not as a screen-by-screen implementation spec.
The replacement should have one persistent, edited control message per chat
where practical. New messages are acceptable when needed for a downloadable
artifact, QR code, notification, or a Telegram API/client limitation. Keep
those messages few and manage their lifecycle deliberately.

Rich Messages are a candidate rendering format, not a prerequisite for the
backend. Telegram Bot API supports edited rich messages, collapsible details,
in-message callback buttons and embedded media. Test a real single-message
prototype on Android, iOS and Desktop before committing all screens to this
format. A Rich Message is still a Telegram message, not an HTML mini-site;
interactive state changes require bot callbacks and rendering. The Bot API
copy-text button is limited to 256 characters, so never truncate a long AWG
key to make it copyable. Preserve downloadable `.vpn`/`.conf` artifacts and
QR delivery. aiogram 3.31 provides typed Rich Message methods and models;
still keep a plain-message fallback for Telegram clients that cannot display
the intended screen. Do not add Rich Message support
to the legacy PTB v13 bot.

API references: [Telegram Bot API rich-message formatting](https://core.telegram.org/bots/api#rich-message-formatting-options),
[message editing](https://core.telegram.org/bots/api#editmessagetext),
[copy-text limit](https://core.telegram.org/bots/api#copytextbutton), and
[aiogram Rich Message API](https://docs.aiogram.dev/en/v3.31.0/api/methods/send_rich_message.html).

This section supersedes the earlier stage-D instruction to migrate Telegram
screens alongside each backend vertical slice and the earlier PTB v22 choice.
The target architecture and
acceptance criteria below remain applicable; historical progress notes remain
as written.

Status (2026-09-28): backend implementation is underway; the new Telegram
client has not started. The Russian sections below record the original plan,
with the updated order above taking precedence where they differ.

Этот план уточняет следующий этап бесплатного open-core. Выделение backend,
переход на aiogram 3 и переработка экранов выполняются в одной программе работ.
Внутри неё сначала проверяется API, затем каждый Telegram-сценарий переносится
сразу на новый контракт, новую библиотеку и новое представление. Старый интерфейс
не переносим на aiogram 3 отдельной промежуточной миграцией.

## 1. Результат и границы

```text
Telegram (aiogram 3) / будущие CLI, Web, приложения
                         ↓ API
Backend: авторизация, профили, ноды, желаемое состояние, Core/Pro
                         ↓ gRPC
Rust driver: исполнение, координация, журнал операций
                         ↓ mTLS / gRPC
Node agent: локальное управление Xray/AWG runtime
```

Backend — отдельный сервис, первоначально на Python. Бот — клиент API:
локализация, экраны, диалоги, преобразование Telegram identity в контекст
аутентифицированного запроса. Бот не обращается к бизнес-БД, driver, SSH или
node agent напрямую и не принимает решения о доступе, provisioning и лицензии.

Backend владеет бизнес-данными, проверкой прав и желаемым состоянием. Driver
получает конкретное задание и возвращает наблюдения и результат; ему остаётся
его собственный журнал исполнения, но не вычисление политики из бизнес-таблиц.
Agent не знает о Telegram, аккаунтах и тарифах.

Обычная установка размещает сервисы на одном хосте, backend слушает localhost.
API проектируется пригодным для других клиентов; обязательная публичная
экспозиция, сайт и CLI не входят в этот этап. Удалённый доступ требует отдельной
настройки TLS и клиентских учётных данных, а не открытия localhost API без защиты.

## 2. Этап A — контракт и инвентаризация

- [ ] Составить список существующих пользовательских и административных
  сценариев, фоновых задач и оставшихся прямых путей исполнения.
- [ ] Описать API независимо от Telegram: аккаунты/полномочия, профили и доступ,
  запросы доступа, ноды и настройки, актуальные конфиги, диагностика и операции.
- [ ] Определить модели actor, permissions, request/result/error и версии API;
  документировать контракт, например через OpenAPI.
- [ ] Разделить credentials Telegram-адаптера и конечного пользователя:
  backend доверяет привязке Telegram identity только от аутентифицированного
  адапптера, затем самостоятельно проверяет права действия. Переданный actor ID
  сам по себе не является авторизацией. CLI/Web в будущем получают собственную
  аутентификацию, не возможность представляться произвольным Telegram ID.
- [ ] Определить идентификатор команды, revision желаемого состояния и правила
  конфликтов/повторной отправки. Ошибки имеют машинный код и поля, локализация
  выполняется клиентом; консольный вывод не становится бизнес-контрактом.
- [ ] Зафиксировать совместимость backend, driver/agent и Telegram-адаптера.

Критерий: основные сценарии описаны запросами и результатами без Update,
CallbackContext, callback payload и локализованного текста.

## 3. Этап B — бизнес-сценарии и граница driver

- [ ] Выделить backend-сценарии с общей авторизацией: сначала чтение профиля/нод
  и выдача конфигов, затем доступ/freeze и запросы доступа, затем lifecycle ноды.
- [ ] Перенести сбор желаемого состояния, подготовку node.env и бизнес-решения
  reconcile из driver в backend. Эволюционировать protobuf по одному сценарию,
  не ломая ещё используемые контракты при переходе.
- [ ] Сохранять желаемое состояние и идентичность команды до dispatch; отдельно
  фиксировать подтверждённый результат. Не удерживать бизнес-транзакцию БД
  открытой на время удалённой установки.
- [ ] Распространить дедупликацию на профильные операции и согласовать
  сериализацию мутаций одной ноды, включая settings/provisioning/reinstall.
- [ ] Перенести технические фоновые задания в владельца их сценария. Боту
  оставить доставку уведомлений и состояние диалога. Pro-политику не развивать.
- [ ] Устранить прямые обходы driver; определить безопасный recovery-сценарий
  для runtime без записи в реестре вместо скрытой очистки из интерфейса.

Критерий: каждый перенесённый сценарий можно вызвать и проверить без Telegram;
права проверяются backend, driver получает явное намерение.

## 4. Этап C — отдельный сервис и операции

- [ ] Выбрать HTTP/API стек и реализовать запуск отдельного backend-сервиса,
  проверку готовности, подключение PostgreSQL и клиента driver.
- [ ] Предоставить API для backend-сценариев и изолировать доступ к credentials
  конфигов/истории; не логировать содержимое ключей и токены.
- [ ] Долгие операции возвращают ID и читаются через API. Ввести надёжное
  принятие задания и восстановление исполнителя до перехода на асинхронный
  ответ. API-таймаут или закрытие клиента не должны повторять мутацию.
- [ ] Разделить бизнес-операцию и вложенные операции driver, сохранив связь ID.
  При рестарте с неизвестным результатом показывать неопределённый исход и
  необходимость проверки, не объявлять действие безопасно неисполненным.
- [ ] На первом этапе получать прогресс опросом состояния. Streaming и
  удалённую отмену добавлять только при реальной необходимости и определённой
  семантике. Автоматический повтор мутаций без дедупликации запрещён.
- [ ] Проверить API отдельно: права, повтор команд, конфликт ревизий,
  конкуренция, недоступность driver/agent и рестарт backend/driver.

Критерий: backend обслуживает основные операции без запущенного бота; долгие
задачи переживают отключение клиента с доступным статусом или явно неизвестным
исходом, без бесконтрольного повторного исполнения.

## 5. Этап D — Telegram сразу на aiogram 3 и Rich Messages

- [ ] Проверить поддержку необходимых Rich Messages в выбранной версии aiogram
  и Telegram API. Если нужны отсутствующие методы, решение остаётся внутри
  нового aiogram-адаптера; временный слой для PTB v13 не создаём.
- [ ] Создать lifecycle aiogram 3, API-клиент, модели диалогов и единый слой
  rendering/send/edit/error. Учесть асинхронность, таймауты, лимиты Telegram,
  остановку процесса и необходимые фоновые задачи.
- [ ] Переносить вертикальными сценариями: основное меню и профиль → получение
  конфигов → запросы доступа → профили/доступ → ноды/настройки → обслуживание
  и обновления. Каждый сценарий сразу использует backend API и новые экраны.
- [ ] Сделать компактные карточки, встроенные callback-действия и раскрываемые
  сведения. Основные действия и навигация остаются заметными на телефоне.
- [ ] AWG/Xray: краткая карточка, файлы и QR, ключ и инструкция скрыты по
  умолчанию. Копирование предлагается только при допустимой длине; длинный
  ключ не обрезается. Нативный .vpn и совместимый .conf сохраняются.
- [ ] При каждом действии получения конфигов проверять текущий доступ и
  актуальность через backend; старый экран не должен обходить отзыв/freeze.
- [ ] Определить поведение старых callback-кнопок после обновления: безопасный
  переход к актуальному экрану вместо выполнения устаревшего действия.
- [ ] Проверить реальные Android/iOS/Desktop клиенты Telegram и нужный fallback
  интерфейса. Успешный ответ Bot API не заменяет проверку отображения.

Rich Messages и удобство базовых экранов доступны в бесплатном Core.
Pro добавляет бизнес-функции и соответствующие экраны, а не платный способ
рендеринга. Это не расширяет текущий объём разработки Pro.

## 6. Этап E — установка, переход и удаление старого кода

- [ ] Добавить backend в systemd-установку, env, healthcheck и обновление;
  определить миграции БД, порядок готовности служб и совместимость релизов.
- [ ] Для текущей разработки использовать чистую установку и целевую схему:
  совместимость промежуточных БД, нод, протоколов и конфигов не требуется.
  После стабилизации схемы предусмотреть ручное добавление старых пользователей
  либо небольшой импорт из старой БД; не переносить runtime/ноды/ключи.
- [ ] Сначала проверить systemd-вариант, используемый для текущих live тестов.
  Portable Docker-установка также должна получить согласованную схему до
  объявления общего перехода завершённым; образ бота сейчас не публикуем.
- [ ] После переключения удалить PTB v13 runtime, старые обработчики и прямые
  импорты бизнес-БД/driver из Telegram-адаптера. Не держать два полноценных
  режима исполнения как постоянную архитектуру.
- [ ] Обновить документы границ, установки, поддержки и релизный чеклист.

Критерий: чистая установка и обновление существующей установки работают;
перезапуск бота не прерывает управление операциями; backend можно использовать
независимо от Telegram.

## 7. Сквозная приёмка

- [ ] PostgreSQL и отдельная тестовая нода: bootstrap, выдача Xray/AWG,
  изменение/применение настроек, reconcile/drift, reinstall и полное удаление.
- [ ] Отзыв/freeze отключает действующие конфиги; восстановление доступа
  сохраняет identity там, где это поддержано; чистая переустановка выдаёт
  реально актуальные конфиги.
- [ ] Xray API-пользователь работает после рестарта контейнера; отзыв сохраняется.
- [ ] Backend отвергает неавторизованные действия независимо от интерфейса.
- [ ] Повторы, параллельные изменения и рестарты не дублируют опасные операции.
- [ ] Недоступность backend/driver/agent отображается понятным состоянием;
  неизвестный исход не маскируется под успех или гарантированный отказ.
- [ ] Полное удаление очищает bot-owned runtime, конфиги, ключи и agent;
  чужие данные и ресурсы не удаляются.

Уже подтверждённые live проверки сохраняются в ROADMAP.md. Неподтверждённые
исправления предыдущего этапа проверяем до соответствующего переноса или в
его сквозном тесте; не считаем их автоматически завершёнными. Исследование
TCP в iOS/v2rayBox остаётся отложенным по решению пользователя.

## 8. Следующий этап Pro

После стабилизации этих границ — адаптация имеющегося сервера авторизации для
лицензирования и один настоящий Pro-модуль. Общие backend/driver/agent не
форкаем. Возможности и лицензия проверяются backend, бесплатное ядро работает
без сервера лицензий. Универсальный каталог расширений заранее не строим.

## Результат начала этапа A — 2026-09-28

Создан [BACKEND_API_CONTRACT.md](BACKEND_API_CONTRACT.md): инвентаризация
сценариев, account/profile/principal/actor, доверенная Telegram-делегация,
permissions, первая вертикаль API и правила commands/issuance. Это проект
контракта; реализация авторизации/маршрутов и сквозные проверки ещё впереди.

## Допуск на несовместимые изменения — 2026-09-28

Пользователь подтвердил: текущая установка тестовая, пользователь один.
Разрешены несовместимые изменения API, схемы БД, аккаунтов, нод и конфигов.
Не строим двойные пути и цепочки миграций ради промежуточных версий. Это меняет
предыдущие требования обязательного сохранения данных и совместимости при
переходе. На этапе разработки приёмка ориентируется на чистую установку.

После фиксации целевой модели требуется возможность добавить старых
пользователей вручную либо небольшим скриптом. Минимальная информация:
Telegram ID и необходимые административные настройки пользователя; точный
состав переноса уточнить по старой БД. Старые VPN credentials, grants к старым
нодам и provisioning state переносить не требуется. Username не используется
для идентификации. Импорт не должен превращать пользователей в администраторов
или автоматически связывать разные аккаунты по совпадению имени.

Разрешение относится к переработке продукта; очистка конкретной работающей
установки выполняется как отдельный явный шаг установки/reset, а не побочный
эффект запуска нового кода. После стабилизации версии совместимость обновлений
снова становится требованием.


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

### Первый этап вывода ноды — 2026-09-28

Для backend-managed нод добавлен доверенный локальный `drain-node`. Под тем же
эксклюзивным lock, что worker, команда одной транзакцией отключает новые grant,
снимает текущие и ставит delete intent для каждого профиля, когда-либо
направленного на ноду. Ранее поставленные, но ещё не выполненные ensure-intent
для отключённой ноды worker помечает superseded, не отправляя их агенту. Повтор
drain возвращает первоначальные operation ID без роста ревизий. Статус
`node-drain-status` показывает ожидающие/blocked задачи и подтверждает завершение
отзывов лишь после успешных последних delete-intent.

Это только первая фаза: запись ноды и агент остаются на месте. Далее нужен
отдельный контракт очистки runtime и удаления агента, который умеет проверить
результат и пережить сбой между удалённым действием и записью в backend; лишь
после него разрешено удалить запись ноды. Для недоступного агента потребуется
явное решение «удалить только из реестра» с сохранением неопределённости по VPS.
Реальные PostgreSQL/agent проверки этой новой фазы ещё не выполнены.

### Контролируемая удалённая очистка — 2026-09-28

Добавлены отдельные `DecommissionNode` driver RPC и подготовительная фаза
агента. Агент сохраняет тип последней команды в journal fence; старый журнал
без этого поля получает миграцию при следующем intent. Перед удалением runtime
агент под общим lock проверяет, что нет незавершённых команд и каждый последний
fence указывает на успешно завершённое удаление. Затем он синхронно пишет
durable marker, запрещающий новые backend и legacy мутации. После удаления
runtime этот marker проверяется самим Rust-агентом, поскольку Python-helper
уже удалён вместе с runtime.

Локальная команда `cleanup-node-step` под тем же worker lock проводит одну
фазу за вызов: prepare → проверенное удаление runtime/configs → планирование
удаления агента через systemd. Финальный transient unit удаляет SSH-ключ бота
после остановки агента; при ошибке планирования ключ остаётся для диагностики.
Backend записывает
стабильный command UUID и фазу до RPC. Безопасные фазы допускают повтор после
таймаута; перед последней фазой фиксируется `uninstall_uncertain`, поэтому
потеря ответа не приводит к слепому повтору. Даже при полученном ответе
`uninstall_scheduled` не является доказательством, что systemd успешно удалил
service и бинарник. Запись ноды остаётся в backend. После начала cleanup новые
изменения профилей не создают задач для этой ноды.

Следующее: независимая проверка завершения agent uninstall (в том числе при
неопределённом ответе), очистка оставшихся следов вроде созданных firewall
правил, финализация записи ноды и явный registry-only сценарий для недоступной
VPS. Не запускать этот путь на тестовой VPS до проверки обновления агента и
PostgreSQL-интеграции; пока проверены только локальные тесты и компиляция Rust.

### Проверка хоста и удаление записи — 2026-09-28

Добавлена неизменяемая привязка ноды к локальному хосту либо SSH-адресу до
начала drain. После `uninstall_scheduled` или `uninstall_uncertain` доверенная
команда `verify-and-remove-node` выполняет read-only проверку на привязанной
машине: systemd-сервис не активен, стандартные файлы/каталоги агента и
runtime отсутствуют, процесс агента не запущен, ключ бота отсутствует в
стандартных authorized_keys, дефолтные контейнеры отсутствуют. Для удалённой
ноды нужен отдельный root SSH-доступ и заранее закреплённый host key. Только
после успешной проверки запись ноды удаляется, а метод/адрес и результат
сохраняются в неизменяемом tombstone.

Для исчезнувшей VPS доступна отдельная локальная команда
`remove-node-registry-only` с обязательной причиной и явным принятием
непроверенного remote-состояния. Она завершает все незаконченные задачи ноды
как superseded, удаляет её из backend-реестра и сохраняет tombstone с числом
брошенных задач. Никакое удаление на VPS при этом не подразумевается. Старые
исторические target больше не создают новые intent для этого ключа.

При удалении агента его transient unit убирает точное совпадение SSH-ключа
бота из home агента и стандартных `/root` и `/home/*`. Перед drain привязка
адреса теперь проверяет `node_key` работающего агента на хосте и сохраняет
отпечаток `/etc/machine-id`; финальная проверка сверяет тот же отпечаток.
Неверно указанный чистый хост больше не может пройти такую проверку.
Этот механизм не защищает от клонированного machine-id или компрометации
root-доступа и всё ещё требует проверки на реальной ноде.

Пока host check проверяет стандартные пути и контейнеры, но не доказывает
отсутствие пользовательских override-путей, нестандартных home или старых UFW-правил. Firewall
cleanup и проверка на PostgreSQL/реальной ноде остаются перед production
cutover. Новая схема ещё не связана с инсталлятором и Telegram UI.

### Backend node inventory — 2026-09-28

The standalone backend now owns admin node inventory reads and desired-state
edits. New nodes start disabled with desired revision 1 and applied revision 0.
Create and update use idempotency keys; updates also require an exact revision.
Retired keys cannot be reused, and a protocol cannot be removed while grants
still reference it. This is an inventory slice, not a runtime deployment path.

Next, connect backend-owned provisioning to the driver, capture verified
applied settings and node identity, and only then implement config issuance.
Issuance must reject pending settings, missing runtime confirmation, frozen or
expired profiles, and revoked grants. The existing Telegram bot still uses its
legacy registry until the new aiogram adapter replaces it.

The first driver integration is a separate read-only `InspectBackendNode` RPC.
It contacts the configured agent without querying the legacy server registry,
checks the returned node key, and exposes runtime health and config-file
presence through an admin-only backend endpoint. This observation deliberately
does not advance `applied_revision`; only the separate explicit backend
settings command can acknowledge a revision after runtime verification.

The explicit backend settings command is now implemented for an already
bootstrapped node. It is queued by an admin API, applied by the same locked
worker that processes profile intents, and journaled on the agent under the
shared mutation fence. A successful agent result includes the exact desired
revision and settings digest; only then does the backend advance its applied
revision and enable the node. Timeout or malformed confirmation blocks the
task. An agent/runtime absent during read-only preflight leaves the task queued
for a later worker run. Worker restart performs read-only recovery from the
agent journal.
The installer now has an explicit backend-node mode for local and SSH hosts.
It confirms the draft in backend storage, reuses the existing mTLS and binary
rollout, updates only the selected agent target, and verifies the driver route
without querying the legacy server registry. The settings worker can then
prepare that clean node. Preparation copies runtime assets and
installs Docker before a durable settings command creates missing protocol
configs. Permanently blocked settings tasks have an explicit restart-and-repair
path that queues a fresh revision. PostgreSQL and live-agent integration remain
before production cutover. Config issuance is
still absent.
