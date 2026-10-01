# Roadmap

Current priority (2026-09-24): stabilize the shared free core, Rust driver and
node agent, and separate backend business scenarios from the Telegram adapter.
See [CORE_ARCHITECTURE.md](CORE_ARCHITECTURE.md) for the execution contract.
The backend must leave room for additional VPN protocols and a small, curated
set of privacy services without turning the Telegram bot into a general-purpose
orchestrator. See [SERVICE_EXPANSION_ARCHITECTURE.md](SERVICE_EXPANSION_ARCHITECTURE.md).
Pro licensing and feature gates are deferred indefinitely. Device management,
VLESS subscriptions, traffic limits, expanded metrics and alerts remain outside
the current delivery scope; their eventual packaging is undecided. The
version-oriented sections below are historical direction, not the current
delivery order.

Current core checkpoint (2026-09-25): driver and agent run on both the local
and SSH nodes. AmneziaWG 3.1 connects and passes traffic on the local node.
Xray profile synchronization on Moscow works again after restoring its missing
`msk1=127.0.0.1:50061` entry in `NODE_AGENT_TARGETS`. Xray client traffic and
the full protocol lifecycle still need live testing.

## Current open-core checkpoint — 2026-09-28

The execution migration is implemented: gRPC-only Rust driver, mutual-TLS
local/SSH agents, persistent operation history and command deduplication,
AWG 3.1, Xray 26.3.27 and durable Xray HandlerService user changes.
Alpha.23 contains profile suspension/AWG identity recovery and full-cleanup
fixes; alpha.24 contains installer/healthcheck fixes. The user considers the
installer issue closed. Historical checklists below are not a complete account
of remaining implementation work.

Remaining shared-core work, in priority order:
- [ ] Keep shared account, node, authorization and operation contracts
  independent of VPN protocols while preserving the current VPN-specific
  profile/grant/config path. Define service-instance and access-enrollment
  resources only when a second service provides a concrete contract.
- [ ] Extract business scenarios from Telegram handlers into backend entry
  points with explicit actors, inputs and shared authorization. Keep Telegram
  responsible for dialogue, localization and rendering only.
- [ ] Complete backend/driver data ownership: backend constructs explicit
  execution intent and desired-state revisions; driver stops reconstructing
  policy from business tables and returns structured observations/results.
- [ ] Extend retained command identities beyond admin node actions to profile
  workflows; coordinate concurrent mutations on the same node and complete
  structured error handling. Durable asynchronous execution, live progress,
  cancellation and automatic retry are still unimplemented; do not introduce
  them without durable acceptance and ambiguity/recovery tests.
- [ ] Audit exceptional orphan runtime cleanup and remaining legacy Pro
  collector/alert code against the shared-core boundary. Replace the host-local
  cleanup exception once a driver-managed recovery path exists.
- [ ] Close outstanding integration checks on PostgreSQL and a disposable node:
  freeze/unfreeze; AWG old-key restoration; full removal including agent and
  credentials; Basic settings; Xray granted access after container restart;
  fresh bootstrap; drift/reconcile and invalid-config/rollback failures.
  Keep unreported checks open. iOS/v2rayBox TCP investigation is deferred by
  user request; the same TCP config works in Android and Linux NekoBox.
- [ ] Execute the combined [backend and Telegram migration plan](BACKEND_TELEGRAM_MIGRATION_PLAN.md):
  finish and validate the standalone backend and worker, including installation
  and disposable-node tests → build a new aiogram 3 client against the complete
  backend API → replace the old adapter. Do not wire the legacy bot to the new
  backend or convert its screens as an interim step. The new client
  keeps the one-edited-message navigation concept but redesigns the screens.
  Prototype Rich Messages on real Telegram clients before relying on embedded
  media and controls for the whole interface.

Backend migration progress: access-request creation, self/admin reads, and
approve/reject decisions now have backend-owned storage, authorization, and
idempotent HTTP commands. Administrator account list/detail and guarded
role/status changes are also available, with revision checks and last-admin
protection. The old Telegram request UI still uses legacy state; notification
delivery and adapter cutover remain open. Approval changes account status only
and does not grant VPN access.

Interface/product decision:
- Rich Messages and basic interface usability belong to the shared product.
  Compact node cards, collapsed config/instruction blocks and navigation are
  not licensed features.
- A paid edition and licensing integration are not planned for the current
  development cycle. Revisit monetization only after the core has users and
  measurable maintenance costs.
- Config delivery should retain native files/QR and optional expanded text.
  Copy buttons must check Telegram's text-length limit; long AWG keys must not
  be truncated to fit.

Next core tasks:
- [x] Show a concise result after successful agent setup; keep full service
  logs available on failure.
- [x] Make gRPC the only driver backend and remove the `inprocess` adapter.
  Installation and maintenance still need validation on a separate node.
- [x] Offer config preservation during reinstall only when agent diagnostics
  confirms an existing config; block the action when diagnostics is unavailable.
- [x] Deploy node-agent on the controller for `transport=local`, bind it to
  `127.0.0.1`, and route local node operations through the same gRPC driver.
  Live rollout on a separate local node is still awaiting validation.
- [x] Remove Python's direct node command transport and the unused Python
  bootstrap/provisioning implementations. Node diagnostics now use driver RPCs.
  Legacy traffic sampling and alert jobs are disabled pending a separate
  product decision; old samples remain in the database.
- [x] Research AmneziaWG changes from the current `0.2.16` image to upstream
  `3.1.20260828` and define the migration. See
  [AMNEZIAWG_UPGRADE_PLAN.md](AMNEZIAWG_UPGRADE_PLAN.md). Existing client
  configs may be invalidated during an explicit in-place migration; all users
  must receive fresh configs afterwards.
- [x] Implement the AmneziaWG 3.1 runtime, config/export schema, existing-profile
  refresh, and driver/agent rollout path described there; local tests and a
  container configuration smoke test pass.
- [ ] Validate AmneziaWG 3.1 on the separate node with real clients and traffic,
  both `.conf` and `vpn://` imports, reinstall/settings changes, and rollback.
- [ ] Finish protocol configuration and live client checks, especially Xray TCP
  and XHTTP access on the local and SSH nodes.
- [x] Verify settings application with existing users: newly issued configs
  contain the applied server parameters and work; AWG `.conf` imports pass
  traffic (user verified on 2026-09-26).
- [x] Investigate Amnezia's generic names on `.conf` import; `vpn://` already
  carries the node title and profile name.
  Upstream `extractWireGuardConfig` assigns `nextAvailableServerName()` and
  does not read a name from the file. A native Amnezia file is needed to keep
  an automatic title; a `.conf` field/comment cannot fix this in Node Plane.
- YouTube ads investigation: no ad blocking is configured by Node Plane.
  AWG currently routes IPv4 only (`0.0.0.0/0`); direct IPv6 is a possible
  explanation if the client does not block it. Confirm on the device before
  changing routing. Google also uses location signals beyond the exit IP.
- [x] Validate the issuance fix after clean reinstall: restore the missing
  AWG peer/Xray UUID through the driver before issuing a config. User verified
  fresh working XHTTP and AWG `vpn://`/`.vpn` configs on alpha.22. TCP remains
  under investigation on iOS; it works in the configured Android NekoBox.
- [x] Move Xray user add/update/remove to HandlerService without routine
  container restarts. Persist the same users in `config.json`, verify both live
  inbounds, and mount the config directory so a container restart reads updates.
  Local integration testing covered add, restart recovery, and delete.
- [x] Validate Xray API rollout on an existing node: after one container
  recreation, subsequent user operations do not restart it and newly issued
  configs pass traffic (user verified on 2026-09-26).
- [ ] Validate the bootstrap timeout and local-agent target recovery fixes
  included in alpha.21 on the separate node.
- Validate issued Xray/AWG configs, settings changes, reinstall, cleanup, and
  failure scenarios on the separate test node before treating the core cycle
  as complete.

This document outlines the planned direction for upcoming Node Plane releases.

It is intentionally lightweight:
- version-oriented
- focused on major themes
- flexible enough to change as implementation details become clearer

## 0.4.x — Storage and Control Plane

Primary goal:
- improve reliability, data model flexibility, and execution architecture

Planned work:
- use PostgreSQL as the only backend database
- initialize the current schema directly; legacy database imports are unsupported
- introduce a Rust control service alongside the bot
- introduce a node agent that runs on managed servers
- establish a gRPC channel between the control service and each node
- reduce dependence on shell-script-based orchestration for runtime actions
- adopt Xray API for dynamic user and traffic operations where it is a better fit than config rewrites
- improve latency and consistency of node operations

Why this comes first:
- PostgreSQL is a better foundation for more complex account and device models
- the current shell-driven runtime flow works, but is not the long-term architecture
- moving to a dedicated control layer will make future features easier to build and maintain

Expected direction:
- keep config files as the source of truth for static Xray runtime setup
- use Xray API for dynamic operations such as user changes and live traffic/stat access

## 0.5.x — Devices and Access Model

Primary goal:
- move from user-level configs to device-level access management

Planned work:
- support multiple devices per account
- issue configs per device instead of per user
- revoke and regenerate access at device level
- track usage per device in overview screens
- add per-user device limits

Notes:
- this is especially important for AWG, where per-device configs are functionally important
- for Xray, per-device separation is also useful for better traffic visibility and account management

## 0.6.x — Access UX

Primary goal:
- make access management and config retrieval significantly easier for both admins and users

Planned work:
- one-click access grants by region
- one-click access grants across all matching servers and protocols
- simpler bulk access workflows for admins
- more convenient config retrieval for users
- clearer protocol and server selection flows
- less friction in request approval and access issuance

## Later Ideas

These are likely directions, but not yet assigned to a specific release:
- deeper replacement of shell-script workflows with structured control-plane operations
- richer observability and health reporting
- more granular admin tooling for bulk operations
- broader traffic and usage reporting
- further UX cleanup in both admin and user flows

## Guiding Principles

The roadmap follows three broader priorities:

1. Architecture
- PostgreSQL
- Rust control service
- node agent
- gRPC-based coordination

2. Access model
- devices instead of only users
- per-device limits
- clearer access ownership and traffic attribution

3. UX
- simpler operator workflows
- simpler config delivery
- less manual repetition in day-to-day administration

- AWG: добавлен файл `.vpn` для AmneziaVPN с сохранением названия; `.conf` оставлен для отдельных AWG 3.1 клиентов. Импорт Windows/Android AmneziaWG принимает `.conf`/ZIP, не `.vpn`. Проверка импорта `.vpn` в приложении остаётся в ручных тестах.

## Live test checkpoint — 2026-09-26, alpha.22

Confirmed by the user:
- Clean reinstall: fresh XHTTP and AWG native configs connect; `.vpn` works.
- Xray revoke takes effect immediately; granting access restores the same client config.
- AWG revoke takes effect immediately; revocation survives a container restart.
- Xray revocation survives a container restart; regrant restores the same config.
- Offline agent: profile updates fail, cleanup offers registry-only removal; recovery works after agent start.
- Fresh SSH node: Docker installation succeeds.

Corrections released in alpha.23 (remaining live checks below):
- Freeze/unfreeze now revokes/restores both protocols through the driver. Saving a frozen profile and old config buttons cannot enable access.
- AWG revoked peer keys and IP are retained securely on the node; regrant restores the same peer. Clean reinstall cannot restore credentials tied to the previous server key.
- Failed probe uses a warning and readable agent status; Docker installation output is summarized.
- Basic edits mark settings pending; Apply changes appears only in the node settings root, with pending-change hints in subsections.
- Node removal is offered only on the card. Full cleanup removes managed runtime and schedules agent/systemd/binary/TLS removal, then the driver waits for the agent to stop. No global Docker image pruning.

Remaining live checks:
- Freeze/unfreeze and AWG old-config recovery, including after restart.
- Full node removal: agent process, binary, unit and node credentials are gone; bot registry entry is removed.
- Pending Basic settings apply only via the root Apply changes button.
- Add via Xray API, then restart the container while access remains granted: the same client config still works.
- Fresh-node complete bootstrap and client traffic in one run.
- TCP on iOS: REALITY/VLESS accepts the profile and logs outbound requests. IPv4 HTTPS from lv1 succeeds, IPv6 HTTPS fails. Android NekoBox works with previously adjusted settings. User reports working NekoBox uses Google DoH, direct DNS 8.8.8.8, IPv6 disabled and ipv4_only throughout. Equivalent iOS settings did not resolve the timeout. The same TCP config works in Linux NekoBox as well; at user request further iOS/v2rayBox investigation is deferred. No speculative TCP protocol changes made.

- Full cleanup audit: remove effective external Xray/AWG config paths, config backups and locks, and AWG saved/revoked client keys. Agent removal uses its actual executable/config/TLS/state/log paths. AWG is stopped gracefully so its exit handler removes its NAT rules and interface; fixed the entrypoint replacing the shell and losing that handler. SSH-key cleanup errors prevent a successful deletion report.
- Controller cleanup also removes the deleted node’s TLS directory and persisted NODE_AGENT_TARGETS entry, retaining the CA and credentials of other nodes. Docker daemon unavailability blocks cleanup instead of claiming success without inspecting containers.

- Post-alpha.23 installer audit: systemd unit installation defaults to Yes (automatic in non-interactive mode); interactive acceptance is retained for the post-rollout bot restart. Root installations no longer require sudo for the service step. Healthcheck reads shared .env, checks the installed venv interpreter/pip, identifies the actual bot unit via LoadState and does not require Compose in simple mode. Verified with a mocked systemd host without Compose; fresh-host installation remains a live check.

- Backend API contract design started: see [BACKEND_API_CONTRACT.md](BACKEND_API_CONTRACT.md). Identity/delegation, resource permissions and initial API routes are specified as implementation targets; no new API server is running yet.

- Backend identity foundation implemented in app/backend: resolve/me, explicit delegation, scope/role/resource policy and additive SQL identity repository. Eleven isolated tests pass. No HTTP/token verifier or existing-account import is connected yet; production Telegram behavior remains on the old path.

- Development compatibility decision (2026-09-28): current test accounts,
  nodes and configs may be replaced; no interim migration chain is required.
  Prefer the clean target architecture/schema. After stabilization, support
  manual recreation or a small legacy-user import keyed by Telegram ID;
  old node/runtime credentials need not survive. See the migration plan’s
  compatibility override. Stable-release updates will require compatibility.

- Backend credentials/bootstrap implemented: opaque bearer hash verification, expiry/revocation, scoped principals and trusted local admin CLI with exclusive 0600 secret-file creation. HTTP integration and PostgreSQL concurrency gate remain pending.

- Initial FastAPI HTTP slice implemented: live/identity-schema readiness, authenticated identity resolve/me, strict input, safe errors, persistent registration idempotency and OpenAPI snapshot. Eight HTTP tests pass. Installer wiring, profiles/nodes and durable long-operation executor are not implemented yet.

- Backend profile/available-node reads implemented on clean UUID ownership/grant models, with safe typed responses and keyset pagination. Local profile creation is available; HTTP mutations, node management and driver integration remain pending. Seven additional HTTP checks pass; no production cutover.

- Profile desired-state mutations implemented with admin permissions, atomic grants, persistent idempotency and optimistic revisions. Runtime is explicitly not_dispatched; actual driver/executor integration is the next step. Eight additional HTTP mutation checks pass.


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


### 2026-10-01: Native controller cleanup

Factory reset and full systemd removal now have backend-owned durable jobs and
localized aiogram screens, including typed confirmation, node verification,
explicit retries and delivery-before-shutdown acknowledgment. Reset retains the
operator/client credential and one recovery snapshot. Full removal is scoped to
installer-owned resources; no host-wide Docker pruning is performed. Existing
installations need a systemd installer rerun to write the ownership manifest.
The remaining gate is live acceptance of these destructive workflows and the
combined end-to-end parity matrix. Legacy command aliases are deferred for separate
review, as requested. Automated coverage does not replace VPS/PostgreSQL acceptance.
