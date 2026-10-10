"""HTTP transport for identity services; constructing an app performs no DDL."""

from uuid import UUID, uuid4
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Query, Request, Response, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from .authorization import (
    AccessDenied, Principal, PrincipalKind, SELF_PERMISSIONS, APPROVED_PERMISSIONS,
    ADMIN_PERMISSIONS, require_permission, resolve_actor,
)
from .credentials import CredentialService
from .announcements import AnnouncementService
from .alerts import AlertService
from .identity import IdentityService
from .identity_repository import SQLIdentityRepository
from .profiles import ProfileRepository, ProfileService
from .profile_commands import ProfileCommands
from .device_commands import DeviceCommands
from .installation_defaults import InstallationDefaults
from .operations import OperationRepository
from .access_requests import AccessRequestService
from .accounts import AccountService
from .nodes import NodeService
from .node_settings import NodeSettingsService
from .node_operations import NodeOperations
from .config_issuance import ConfigIssuanceService
from .agent_rollout import AgentRolloutService
from .node_lifecycle import NodeLifecycle
from .admin_overview import AdminOverviewService
from .node_overview import NodeOverviewService
from .system_settings import SystemSettingsService
from .traffic import TrafficService
from .updates import UpdateService
from .backups import BackupService
from config import APP_VERSION, SSH_KEY


class ResolveInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    telegram_user_id: Annotated[StrictInt, Field(gt=0, lt=2**63)]
    username: Annotated[StrictStr, Field(max_length=64)] | None = None
    first_name: Annotated[StrictStr, Field(max_length=128)] | None = None
    last_name: Annotated[StrictStr, Field(max_length=128)] | None = None
    language_code: Annotated[StrictStr, Field(max_length=16)] | None = None


class AccountOutput(BaseModel):
    id: str
    role: str
    status: str


class MeOutput(AccountOutput):
    announcement_silent: bool = False
    traffic_available: bool = False
    permissions: list[str]
    language_code: str | None = None
    locale: str | None = None
    locale_selected: bool = False


class AdminAccountOutput(AccountOutput):
    revision: int
    telegram_user_id: int | None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str | None = None
    locale: str | None = None
    locale_selected: bool = False


class AccountPage(BaseModel):
    items: list[AdminAccountOutput]
    next_cursor: str | None


class AccountEditInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Literal['member', 'admin'] | None = None
    status: Literal['pending', 'approved', 'rejected', 'disabled'] | None = None


class AccountPreferencesInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    locale: Literal['ru', 'en'] | None = None
    announcement_silent: StrictBool | None = None


class TrafficPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


class CleanupNotificationInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message_id: StrictInt = Field(gt=0)
    locale: Literal['en', 'ru'] = 'en'


class SystemCleanupPlanInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['reset', 'remove']
    cleanup_nodes: StrictBool


class SystemCleanupInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plan_id: UUID
    confirmation_phrase: StrictStr = Field(min_length=1, max_length=64)


class AnnouncementInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: StrictStr = Field(min_length=1, max_length=3000)


class AnnouncementDeliveryInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['sent', 'failed', 'unknown']


class AlertPreferencesInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool | None = None
    interval_minutes: Literal[5,15] | None = None
    notify_resolved: StrictBool | None = None


class ProfileOutput(BaseModel):
    id: UUID
    display_name: str
    owner_account_id: UUID | None
    frozen: bool
    expires_at: str | None
    desired_revision: int
    deleting: bool = False


class ProfilePage(BaseModel):
    items: list[ProfileOutput]
    next_cursor: str | None


class DeviceOutput(BaseModel):
    id: UUID
    profile_id: UUID
    display_name: str
    status: Literal['active', 'deleting', 'retired']
    revision: int
    created_at: str


class DeviceList(BaseModel):
    items: list[DeviceOutput]


class DeviceNameInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    display_name: Annotated[StrictStr, Field(min_length=1, max_length=256)]


class DeviceCommandOutput(BaseModel):
    device: DeviceOutput
    profile_revision: int
    operation_id: UUID | None
    runtime_status: str


class MemberProfileNode(BaseModel):
    key: str
    title: str
    flag: str
    region: str
    protocols: list[Literal['awg', 'xray']]


class ProfileTrafficItem(BaseModel):
    protocol: Literal['awg', 'xray']
    uplink_bytes: int
    downlink_bytes: int
    tracked_since: str
    last_sample_at: str


class ProfileNodeTrafficItem(BaseModel):
    node_key: str
    protocol: Literal['awg', 'xray']
    uplink_bytes: int
    downlink_bytes: int
    status: Literal['current', 'unknown']


class ProfileTrafficSummary(BaseModel):
    status: Literal['waiting', 'current', 'unknown']
    items: list[ProfileTrafficItem]
    month: str | None = None
    nodes: list[ProfileNodeTrafficItem] = []


class MemberProfileSummary(BaseModel):
    profile_id: UUID
    display_name: str
    frozen: bool
    expired: bool
    expires_at: str | None
    created_at: str | None
    nodes: list[MemberProfileNode]
    node_count: int
    protocol_count: int
    xray_count: int
    awg_count: int
    issued_count: int
    last_issued_at: str | None
    traffic: ProfileTrafficSummary | None = None


class ProblemNodeOutput(BaseModel):
    key: str
    title: str
    region: str = ''
    flag: str = ''


class AdminOverviewOutput(BaseModel):
    version: str
    nodes_total: int
    nodes_enabled: int
    profiles_total: int
    profiles_active: int
    profiles_frozen: int
    pending_requests: int
    problem_nodes: list[ProblemNodeOutput]


class UpdatePreferencesInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    auto_check_enabled: bool | None = Field(default=None, strict=True)
    auto_check_interval_minutes: Literal[15, 60, 360, 1440] | None = None
    branch: Literal['main', 'dev'] | None = None
    dev_track: Literal['tag', 'head'] | None = None


class RecoveryItemOutput(BaseModel):
    id: str
    kind: Literal['update', 'node', 'settings', 'bootstrap', 'agent', 'profile', 'removal', 'backup']
    status: str
    node_key: str
    error_code: str
    actions: list[Literal['cancel', 'recheck', 'resolve']]
    phase: str = ''
    subject_id: str = ''
    related_id: str = ''
    related_kind: str = ''
    next_step: str = ''


class RecoveryOverviewOutput(BaseModel):
    items: list[RecoveryItemOutput]
    offset: int
    page_size: int
    total: int
    maintenance_active: bool


class WorkstationAuditItemOutput(BaseModel):
    occurred_at: str
    session_id: str
    account_id: str
    account_label: str
    ssh_user: str
    device_fingerprint: str
    action: str
    phase: Literal['issued', 'revoked', 'admitted', 'completed']
    request_id: str | None
    command_id: str | None
    http_status: int | None
    target: str | None = None
    key_fingerprint: str | None = None
    outcome: Literal['admitted', 'succeeded', 'unconfirmed'] | None = None


class WorkstationAuditPageOutput(BaseModel):
    items: list[WorkstationAuditItemOutput]
    offset: int
    page_size: int
    total: int


class UpdateRunInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['version', 'stack', 'agents', 'runtimes']
    target_ref: StrictStr | None = None
    branch: Literal['main', 'dev'] | None = None


class BackupPreferencesInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool | None = None
    interval_hours: Literal[6,12,24] | None = None
    keep_count: Literal[5,10,20] | None = None


class BackupCommandInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['create','restore']
    backup_id: UUID | None = None
    checksum: StrictStr | None = Field(default=None,pattern=r'^[0-9a-f]{64}$')


class GrantPage(BaseModel):
    items: list['GrantInput']


class AvailableProtocol(BaseModel):
    kind: str
    transports: list[str]


class AvailableNode(BaseModel):
    key: str
    title: str
    region: str
    flag: str
    protocols: list[AvailableProtocol]


class NodePage(BaseModel):
    items: list[AvailableNode]
    next_cursor: str | None


class NodeSettingsInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    public_host: StrictStr | None = None
    xray_host: StrictStr | None = None
    xray_sni: StrictStr | None = None
    xray_tcp_port: StrictInt | None = None
    xray_xhttp_port: StrictInt | None = None
    xray_xhttp_path: StrictStr | None = None
    awg_public_host: StrictStr | None = None
    awg_port: StrictInt | None = None
    awg_port_mode: Literal['auto', 'manual'] | None = None
    xray_fingerprint: Literal['chrome', 'firefox', 'safari', 'ios', 'android', 'edge', 'random', 'randomized'] | None = None
    awg_interface: StrictStr | None = None
    awg_i1_preset: Literal['quic', 'dns', 'chaos'] | None = None


class NodeCreateInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    template: StrictStr | None = None
    key: StrictStr
    title: StrictStr
    region: StrictStr
    flag: StrictStr = ''
    protocols: list[Literal['awg', 'xray']]
    xray_transports: list[Literal['tcp', 'xhttp']] = Field(default_factory=list)
    settings: NodeSettingsInput = Field(default_factory=NodeSettingsInput)
    transport: Literal['local', 'ssh'] | None = None
    ssh_target: StrictStr | None = None
    notes: StrictStr = ''


class InstallationDefaultsInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    protocols: list[Literal['awg', 'xray']]
    xray_transports: list[Literal['tcp', 'xhttp']]
    settings: NodeSettingsInput


class GrantPolicyRuleInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope: Literal['all', 'region']
    region_id: UUID | None = None
    protocols: list[Literal['awg', 'xray']]


class GrantPolicyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    explicit_grants: list[dict[str, StrictStr]]
    rules: list[GrantPolicyRuleInput]
    exclusions: list[dict[str, StrictStr]]


class NodeEditInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: StrictStr | None = None
    region: StrictStr | None = None
    flag: StrictStr | None = None
    protocols: list[Literal['awg', 'xray']] | None = None
    xray_transports: list[Literal['tcp', 'xhttp']] | None = None
    settings: NodeSettingsInput | None = None
    transport: Literal['local', 'ssh'] | None = None
    ssh_target: StrictStr | None = None
    notes: StrictStr | None = None
    confirm_access_change: StrictBool = False


class AdminNodeOutput(BaseModel):
    key: str
    title: str
    region: str
    region_id: str | None = None
    flag: str
    enabled: bool
    policy_eligible: bool = False
    protocols: list[str]
    xray_transports: list[str]
    desired_revision: int
    applied_revision: int
    settings: dict[str, str | int]
    transport: str | None = None
    ssh_target: str | None = None
    notes: str = ''
    overview: dict | None = None


class AdminNodePage(BaseModel):
    items: list[AdminNodeOutput]
    next_cursor: str | None


class NodeTrafficItem(BaseModel):
    protocol: Literal['awg', 'xray']
    status: Literal['current', 'waiting', 'unknown']
    uplink_bytes: int | None
    downlink_bytes: int | None


class NodeTrafficSummary(BaseModel):
    month: str
    status: Literal['current', 'waiting', 'unknown']
    total_bytes: int | None
    items: list[NodeTrafficItem]


class AdminNodeOverviewOutput(BaseModel):
    node_key: str
    enabled: bool
    state: str
    desired_revision: int
    applied_revision: int
    settings_task_status: str | None
    settings_complete: bool
    access_total: int
    ready: int
    pending: int
    failed: int
    attention: int
    last_job: dict | None = None
    agent_rollout: dict | None = None
    bootstrap: dict | None = None
    removal_status: str | None = None
    traffic: NodeTrafficSummary | None = None


class NodeRuntimeObservation(BaseModel):
    node_key: str
    health_state: str
    runtime_version: str
    runtime_commit: str
    agent_version: str = ''
    agent_commit: str = ''
    xray_config_present: bool
    awg_config_present: bool
    desired_revision: int
    applied_revision: int
    settings_verified: bool


class NodeDiagnosticsOutput(BaseModel):
    node_key: str
    docker: Literal['ok', 'missing', 'invalid']
    runtime_root: Literal['ok', 'missing', 'invalid']
    xray_config: Literal['ok', 'missing', 'invalid']
    awg_config: Literal['ok', 'missing', 'invalid']
    runtime_version: str | None


class NodeSettingsTaskOutput(BaseModel):
    id: UUID
    node_key: str
    revision: int
    status: str
    error_code: str | None = None


class NodeActionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['bootstrap', 'reinstall_keep', 'reinstall_clean', 'cleanup_runtime',
        'install_docker', 'check_ports', 'open_ports', 'sync_runtime', 'sync_env',
        'sync_xray', 'regenerate_entropy', 'reconcile_access']
    revision: StrictInt


class NodeRemovalInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    retry: StrictBool = False


class AgentRolloutInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    transport: Literal['local', 'ssh']
    ssh_target: str | None = None
    ssh_port: int = 22
    install_rust: StrictBool = False


class AgentRolloutOutput(BaseModel):
    id: UUID
    node_key: str
    status: str
    failure_code: str | None = None
    journal_archives: list[str] = Field(default_factory=list)
    progress: dict[str, str] | None = None


class NodeMaintenanceOutput(BaseModel):
    node_key: str
    status: str
    operation_ids: list[str]
    pending_tasks: int
    blocked_tasks: int
    revocations_complete: bool
    cleanup_phase: str | None
    verification_target: str | None
    affected_profiles: int = 0


class VerificationTargetInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    transport: Literal['local', 'ssh']
    ssh_target: str | None = None
    ssh_port: Annotated[StrictInt, Field(ge=1, le=65535)] = 22


class VerificationTargetOutput(BaseModel):
    node_key: str
    target: str
    host_fingerprint: str


class DrainOutput(BaseModel):
    node_key: str
    status: str
    operation_ids: list[str]


class CleanupStepOutput(BaseModel):
    node_key: str
    phase: str
    command_id: str


class CleanupStepInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_phase: Literal['not_started', 'preparing', 'prepared', 'runtime_deleted']


class VerifiedRetirementOutput(BaseModel):
    node_key: str
    mode: Literal['verified']


class RegistryRetirementInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    accept_unverified_runtime: Literal[True]
    reason: Annotated[StrictStr, Field(min_length=20, max_length=500)]


class RegistryRetirementOutput(BaseModel):
    node_key: str
    mode: str
    unfinished_tasks: int


class ConfigIssuanceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    node_key: Annotated[str, Field(min_length=1, max_length=64)]
    protocol: Literal['xray', 'awg']
    transport: Literal['tcp', 'xhttp', 'vpn', 'conf']
    device_id: UUID | None = None


class ConfigIssuanceOutput(BaseModel):
    id: UUID
    profile_id: UUID
    node_key: str
    protocol: str
    transport: str
    status: str
    expires_at: str
    device_id: UUID | None = None


class ConfigArtifactFile(BaseModel):
    filename: str
    content: str


class ConfigArtifactOutput(BaseModel):
    filename: str | None
    media_type: str
    content: str
    display_name: str | None = None
    files: list[ConfigArtifactFile] = Field(default_factory=list)


class GrantInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    node_key: Annotated[str, Field(min_length=1, max_length=64)]
    protocol: str


class ProfileCreateInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    display_name: Annotated[str, Field(min_length=1, max_length=128)]
    owner_account_id: UUID | None = None
    expires_at: str | None = None
    grants: Annotated[list[GrantInput], Field(max_length=100)] = Field(default_factory=list)
    access_policy: GrantPolicyInput | None = None


class ProfileEditInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    display_name: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    frozen: bool | None = Field(default=None, strict=True)
    expires_at: str | None = None
    grants: Annotated[list[GrantInput], Field(max_length=100)] | None = None
    access_policy: GrantPolicyInput | None = None


class GrantsInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    grants: Annotated[list[GrantInput], Field(max_length=100)]


class ProfileCommandOutput(BaseModel):
    profile: ProfileOutput
    grants: list[GrantInput]
    runtime_status: str
    operation_id: UUID


class AccessRequestOutput(BaseModel):
    id: UUID
    account_id: UUID
    status: str
    created_at: str
    decided_at: str | None
    decided_by: UUID | None
    telegram_user_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str | None = None
    locale: str | None = None


class AccessRequestPage(BaseModel):
    pending_total: int | None = None
    items: list[AccessRequestOutput]
    next_cursor: str | None


class AccessDecisionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: Literal['approve', 'reject']


class AccessRequestPolicyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool | None = None
    gate_message: StrictStr | None = Field(default=None, max_length=500)
    notify_requests: StrictBool | None = None


class AccessRequestPolicyOutput(BaseModel):
    enabled: bool
    gate_message: str
    notify_requests: bool | None = None


class BotTitleInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: StrictStr = Field(min_length=1, max_length=64)


class BotTitleOutput(BaseModel):
    title: str


class VersionOutput(BaseModel):
    version: str


class ProfileInspectionOutput(BaseModel):
    available: bool
    disk_present: bool | None = None
    live_present: bool | None = None
    identity_matches: bool | None = None
    config_available: bool | None = None


class OperationTaskOutput(BaseModel):
    id: UUID
    node_key: str
    protocol: str
    action: str
    status: str
    inspected_at: str | None = None
    inspection: ProfileInspectionOutput | None = None
    device_id: UUID | None = None


class OperationOutput(BaseModel):
    id: UUID
    profile_id: UUID
    desired_revision: int
    status: str
    created_at: str
    tasks: list[OperationTaskOutput]


def create_app(db, *, node_driver=None, cleanup_host=None) -> FastAPI:
    app = FastAPI(title='Node Plane Backend', version='1.0.0', docs_url=None, redoc_url=None)
    identities = SQLIdentityRepository(db)
    credentials = CredentialService(db)
    service = IdentityService(identities)
    profiles = ProfileService(ProfileRepository(db))
    device_commands = DeviceCommands(db)
    installation_defaults = InstallationDefaults(db)
    profile_commands = ProfileCommands(db)
    operations = OperationRepository(db)
    admin_overview = AdminOverviewService(db)
    node_overview = NodeOverviewService(db)
    system_settings = SystemSettingsService(db)
    access_requests = AccessRequestService(db)
    accounts = AccountService(db)
    nodes = NodeService(db)
    node_settings = NodeSettingsService(db)
    node_jobs = NodeOperations(db)
    config_issuances = ConfigIssuanceService(db, node_driver)
    from .temporary_configs import TemporaryConfigService
    temporary_configs = TemporaryConfigService(db, node_driver)
    agent_rollouts = AgentRolloutService(db)
    from .node_bootstrap import NodeBootstrapService
    node_bootstraps = NodeBootstrapService(db)
    lifecycle = NodeLifecycle(db)
    update_observations = {}
    update_service = UpdateService(db, node_driver, observation_cache=update_observations)
    backup_service = BackupService(db)
    announcements = AnnouncementService(db)
    alerts = AlertService(db)
    from .system_cleanup import SystemCleanupService
    system_cleanup = SystemCleanupService(db, node_driver, host=cleanup_host)

    @contextmanager
    def live_updates():
        if node_driver is not None:
            yield update_service
        else:
            from .driver_transport import GrpcIntentDriver, local_channel
            with local_channel('127.0.0.1:50051') as channel:
                yield UpdateService(db, GrpcIntentDriver(channel, timeout=5), observation_cache=update_observations)

    @contextmanager
    def maintenance_lock(current):
        require_permission(current, 'maintenance.manage')
        shared = os.environ.get('NODE_PLANE_SHARED_DIR')
        if not shared:
            raise AccessDenied('maintenance_unavailable', 503)
        lock_path = Path(shared) / 'data' / 'backend-worker.lock'
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                yield
        except BlockingIOError:
            raise AccessDenied('maintenance_busy', 409) from None

    def error(request, code, status):
        headers = {'WWW-Authenticate': 'Bearer'} if status == 401 else None
        return JSONResponse({'error': {'code': code, 'message': code,
                            'request_id': request.state.request_id, 'details': {}}},
                            status_code=status, headers=headers)

    @app.middleware('http')
    async def request_context(request: Request, call_next):
        request.state.request_id = str(uuid4())
        audit_context = None
        audit_action = None
        audit_command = None
        try:
            if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
                from .workstation_audit import WorkstationAudit
                try:
                    audit_principal = credentials.authenticate(request.headers.get('Authorization'))
                except AccessDenied:
                    pass
                else:
                    audit_context = WorkstationAudit(db).context(audit_principal.id)
                    if audit_context:
                        audit_action = request.method + ' ' + request.url.path
                        raw_command = request.headers.get('Idempotency-Key')
                        try:
                            audit_command = str(UUID(raw_command)) if raw_command else None
                        except ValueError:
                            pass
                        WorkstationAudit(db).record(audit_context, audit_action, 'admitted',
                            request_id=request.state.request_id, command_id=audit_command)
            restoring = False
            cleaning = False
            if request.method not in {'GET','HEAD','OPTIONS'}:
                import re
                update_recovery = bool(re.fullmatch(r'/api/v1/system/updates/jobs/[0-9a-fA-F-]{36}/(cancel|recheck)', request.url.path))
                allowed = request.url.path.startswith('/api/v1/system/cleanup/') or request.url.path.endswith('/ack') or update_recovery
                if not allowed:
                    from .maintenance_gate import active
                    with db.connect() as conn:
                        cleaning = bool(active(conn))
            if request.method not in {'GET','HEAD','OPTIONS'} and request.url.path != '/api/v1/system/backups/jobs':
                with db.connect() as conn:
                    restoring = bool(conn.execute("SELECT 1 FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running') LIMIT 1").fetchone())
            if cleaning:
                response = error(request,'system_cleanup_in_progress',409)
            else:
                response = error(request,'restore_in_progress',409) if restoring else await call_next(request)
        except Exception as exc:
            # Do not serialize DB exceptions, request bodies or credentials.
            import logging
            import traceback
            frames = traceback.extract_tb(exc.__traceback__)
            location = ' -> '.join(f'{Path(frame.filename).name}:{frame.lineno}:{frame.name}' for frame in frames[-8:])
            logging.getLogger(__name__).error('Backend request failed: %s %s type=%s sqlstate=%s request_id=%s location=%s',
                request.method, request.url.path, type(exc).__name__, getattr(exc, 'sqlstate', None),
                request.state.request_id, location)
            response = error(request, 'internal_error', 500)
        response.headers['X-Request-ID'] = request.state.request_id
        response.headers['Cache-Control'] = 'no-store'
        if audit_context and audit_action:
            from .workstation_audit import WorkstationAudit
            try:
                WorkstationAudit(db).record(audit_context, audit_action, 'completed',
                    request_id=request.state.request_id, command_id=audit_command,
                    http_status=response.status_code)
            except Exception:
                # The admitted event survives; do not pretend a committed action
                # failed and invite replay just because result auditing failed.
                import logging
                logging.getLogger(__name__).error('Workstation audit outcome unconfirmed request_id=%s', request.state.request_id)
        return response

    @app.exception_handler(AccessDenied)
    async def denied(request: Request, exc: AccessDenied):
        return error(request, exc.code, exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError):
        # FastAPI's default validation response echoes submitted values.
        return error(request, 'invalid_input', 422)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return error(request, 'resource_not_found' if exc.status_code == 404 else 'http_error', exc.status_code)

    def header(request, name):
        values = request.headers.getlist(name)
        if len(values) > 1:
            raise AccessDenied('invalid_input', 422)
        return values[0] if values else None

    bearer = HTTPBearer(auto_error=False)

    def authenticate(request: Request, security: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)]) -> Principal:
        return credentials.authenticate(header(request, 'Authorization'))

    def actor(request: Request, principal: Annotated[Principal, Depends(authenticate)],
              delegated_user: Annotated[str | None, Header(alias="X-Node-Plane-Telegram-User-ID")] = None):
        raw = header(request, 'X-Node-Plane-Telegram-User-ID')
        if raw is not None and principal.kind != PrincipalKind.ADAPTER:
            raise AccessDenied('delegation_not_allowed')
        user_id = None
        if raw is not None:
            if not raw.isascii() or not raw.isdecimal() or len(raw) > 19:
                raise AccessDenied('invalid_telegram_identity', 422)
            user_id = int(raw)
        return resolve_actor(principal, identities, telegram_user_id=user_id)

    @app.get('/health/live')
    def live():
        return {'status': 'alive'}

    @app.get('/health/ready')
    def ready(request: Request):
        try:
            from db.migrations import check_schema
            check_schema(db)
            with db.connect() as conn:
                # Only readiness for this implemented slice, not node connectivity.
                conn.execute('SELECT id FROM backend_accounts LIMIT 1').fetchone()
                conn.execute('SELECT id FROM backend_credentials LIMIT 1').fetchone()
                conn.execute('SELECT account_id FROM backend_external_identities LIMIT 1').fetchone()
                conn.execute('SELECT account_id FROM backend_identity_commands LIMIT 1').fetchone()
                conn.execute('SELECT id FROM backend_profiles LIMIT 1').fetchone()
                conn.execute('SELECT id FROM backend_devices LIMIT 1').fetchone()
                conn.execute('SELECT key FROM backend_nodes LIMIT 1').fetchone()
                conn.execute('SELECT profile_id FROM backend_grants LIMIT 1').fetchone()
                conn.execute('SELECT command_key FROM backend_profile_commands LIMIT 1').fetchone()
                conn.execute('SELECT id FROM backend_operations LIMIT 1').fetchone()
                conn.execute('SELECT id,device_id FROM backend_operation_tasks LIMIT 1').fetchone()
                conn.execute('SELECT profile_id FROM backend_profile_identities LIMIT 1').fetchone()
                conn.execute('SELECT task_id FROM backend_repairs LIMIT 1').fetchone()
                conn.execute('SELECT node_key FROM backend_node_drains LIMIT 1').fetchone()
                conn.execute('SELECT node_key FROM backend_node_cleanup LIMIT 1').fetchone()
                conn.execute('SELECT node_key FROM backend_node_retirements LIMIT 1').fetchone()
                conn.execute('SELECT node_key FROM backend_node_verification_targets LIMIT 1').fetchone()
                conn.execute('SELECT fingerprint FROM backend_node_host_identities LIMIT 1').fetchone()
                conn.execute('SELECT id FROM backend_access_requests LIMIT 1').fetchone()
                conn.execute('SELECT key FROM backend_system_settings LIMIT 1').fetchone()
                conn.execute('SELECT profile_id FROM backend_traffic_usage LIMIT 1').fetchone()
                conn.execute('SELECT device_id FROM backend_traffic_peers LIMIT 1').fetchone()
                conn.execute('SELECT node_key FROM backend_node_traffic LIMIT 1').fetchone()
                conn.execute('SELECT id FROM backend_system_cleanup_jobs LIMIT 1').fetchone()
                conn.execute('SELECT actor_account_id FROM backend_account_commands LIMIT 1').fetchone()
                conn.execute('SELECT id FROM backend_account_guard LIMIT 1').fetchone()
                conn.execute('SELECT actor_account_id FROM backend_node_commands LIMIT 1').fetchone()
                conn.execute('SELECT id FROM backend_node_settings_tasks LIMIT 1').fetchone()
                conn.execute('SELECT id,device_id,device_revision FROM backend_config_issuances LIMIT 1').fetchone()
                conn.execute('SELECT id FROM backend_agent_rollouts LIMIT 1').fetchone()
                conn.execute('SELECT id FROM backend_update_jobs LIMIT 1').fetchone()
                conn.execute('SELECT job_id FROM backend_update_items LIMIT 1').fetchone()
                conn.execute('SELECT id FROM backend_announcements LIMIT 1').fetchone()
                conn.execute('SELECT id FROM backend_announcement_deliveries LIMIT 1').fetchone()
                conn.execute('SELECT node_key FROM backend_alert_state LIMIT 1').fetchone()
        except Exception:
            return error(request, 'dependency_unavailable', 503)
        return {'status': 'ready'}

    @app.post('/api/v1/integrations/telegram/identities/resolve', response_model=AccountOutput)
    def resolve(body: ResolveInput, request: Request, principal: Annotated[Principal, Depends(authenticate)],
                command_key: Annotated[str, Header(alias="Idempotency-Key")]):
        # Registration does not accept a delegated actor or username-based linking.
        if header(request, 'X-Node-Plane-Telegram-User-ID') is not None:
            raise AccessDenied('invalid_input', 422)
        raw_key = header(request, 'Idempotency-Key')
        try:
            key = str(UUID(raw_key))
        except (TypeError, ValueError, AttributeError):
            raise AccessDenied('invalid_idempotency_key', 422) from None
        account = service.resolve_telegram(principal, body.telegram_user_id, command_key=key,
            username=body.username, first_name=body.first_name, last_name=body.last_name,
            language_code=body.language_code)
        ProfileRepository(db).ensure_account_profile(account.id)
        return account

    @app.get('/api/v1/me', response_model=MeOutput)
    def me(current=Depends(actor)):
        require_permission(current, 'account.self.read')
        permissions = []
        for permission in sorted(SELF_PERMISSIONS | APPROVED_PERMISSIONS | ADMIN_PERMISSIONS):
            try:
                require_permission(current, permission)
            except AccessDenied:
                continue
            permissions.append(permission)
        return MeOutput(id=current.account.id, role=current.account.role, status=current.account.status,
                        permissions=permissions,
                        **system_settings.member_preferences(current),
                        **identities.telegram_details_for_account(current.account.id))

    @app.patch('/api/v1/me/preferences', response_model=MeOutput)
    def update_my_preferences(body: AccountPreferencesInput, current=Depends(actor)):
        require_permission(current, 'account.self.preferences.write')
        if not body.model_fields_set or any(value is None for value in body.model_dump(exclude_unset=True).values()):
            raise AccessDenied('invalid_input', 422)
        if body.locale is not None:
            identities.set_telegram_locale_for_account(current.account.id, body.locale)
        if body.announcement_silent is not None:
            system_settings.update_member_preferences(current, body.announcement_silent)
        permissions = []
        for permission in sorted(SELF_PERMISSIONS | APPROVED_PERMISSIONS | ADMIN_PERMISSIONS):
            try:
                require_permission(current, permission)
            except AccessDenied:
                continue
            permissions.append(permission)
        return MeOutput(id=current.account.id, role=current.account.role, status=current.account.status,
                        permissions=permissions,
                        **system_settings.member_preferences(current),
                        **identities.telegram_details_for_account(current.account.id))

    @app.post('/api/v1/announcements/preview')
    def announcement_preview(body: AnnouncementInput, current=Depends(actor)):
        return announcements.preview(current, body.text)

    @app.get('/api/v1/announcements')
    def latest_announcement(current=Depends(actor)):
        return announcements.latest(current)

    @app.get('/api/v1/system/traffic')
    def traffic_policy(current=Depends(actor)):
        return system_settings.traffic_policy(current)

    @app.get('/api/v1/system/cleanup')
    def cleanup_overview(current=Depends(actor)):
        return system_cleanup.overview(current)

    @app.post('/api/v1/system/cleanup/plans')
    def cleanup_plan(body:SystemCleanupPlanInput,current=Depends(actor)):
        return system_cleanup.plan(current,body.action,body.cleanup_nodes)

    @app.post('/api/v1/system/cleanup/jobs',status_code=202)
    def cleanup_queue(body:SystemCleanupInput,request:Request,current=Depends(actor)):
        return system_cleanup.queue(current,str(body.plan_id),body.confirmation_phrase,header(request,'Idempotency-Key'))

    @app.get('/api/v1/system/cleanup/jobs/{job_id}')
    def cleanup_job(job_id:UUID,current=Depends(actor)):
        return system_cleanup.get(current,str(job_id))

    @app.post('/api/v1/system/cleanup/jobs/{job_id}/retry')
    def cleanup_retry(job_id:UUID,current=Depends(actor)):
        return system_cleanup.retry(current,str(job_id))

    @app.post('/api/v1/system/cleanup/jobs/{job_id}/abort')
    def cleanup_abort(job_id:UUID,current=Depends(actor)):
        return system_cleanup.abort(current,str(job_id))

    @app.post('/api/v1/system/cleanup/jobs/{job_id}/shutdown-ack')
    def cleanup_shutdown_ack(job_id:UUID,body:CleanupNotificationInput | None=None,current=Depends(actor)):
        return system_cleanup.acknowledge_shutdown(current,str(job_id),
            body.model_dump() if body else None)

    @app.patch('/api/v1/system/traffic/preferences')
    def traffic_preferences(body: TrafficPolicyInput, current=Depends(actor)):
        return system_settings.update_traffic_policy(current, body.enabled)

    @app.get('/api/v1/system/alerts')
    def alert_overview(current=Depends(actor)):
        return alerts.overview(current)

    @app.post('/api/v1/system/alerts/{event_id}/dismiss')
    def alert_dismiss(event_id: UUID, current=Depends(actor)):
        return alerts.dismiss(current, str(event_id))

    @app.get('/api/v1/system/attention')
    def system_attention(current=Depends(actor)):
        require_permission(current, 'settings.manage')
        from app.services import updates as updater
        with db.connect() as conn:
            count = sum(not r['dismissed'] for r in AlertService.active(conn, current.account.id))
        return {'unacknowledged_alerts': count,
            'update_available': updater.get_updates_overview(refresh_run=False)['update_available']}

    @app.patch('/api/v1/system/alerts/preferences')
    def alert_preferences(body:AlertPreferencesInput,current=Depends(actor)):
        return alerts.preferences(current,body.model_dump(exclude_unset=True))

    @app.post('/api/v1/integrations/telegram/alerts/claim')
    def claim_alert(request:Request,principal=Depends(authenticate)):
        if header(request,'X-Node-Plane-Telegram-User-ID') is not None:
            raise AccessDenied('invalid_input',422)
        return {'delivery':alerts.claim(principal,header(request,'Idempotency-Key'))}

    @app.post('/api/v1/integrations/telegram/alerts/{delivery_id}/ack')
    def acknowledge_alert(delivery_id:UUID,body:AnnouncementDeliveryInput,request:Request,principal=Depends(authenticate)):
        if header(request,'X-Node-Plane-Telegram-User-ID') is not None:
            raise AccessDenied('invalid_input',422)
        return alerts.acknowledge(principal,str(delivery_id),header(request,'Idempotency-Key'),body.status)

    @app.post('/api/v1/announcements', status_code=202)
    def announcement_create(body: AnnouncementInput, request: Request, current=Depends(actor)):
        return announcements.queue(current, body.text, header(request,'Idempotency-Key'))

    @app.get('/api/v1/announcements/{announcement_id}')
    def announcement_status(announcement_id: UUID, current=Depends(actor)):
        return announcements.get(current,str(announcement_id))

    @app.post('/api/v1/integrations/telegram/announcements/claim')
    def claim_announcement(request: Request, principal=Depends(authenticate)):
        if header(request,'X-Node-Plane-Telegram-User-ID') is not None:
            raise AccessDenied('invalid_input',422)
        return {'delivery':announcements.claim(principal,header(request,'Idempotency-Key'))}

    @app.post('/api/v1/integrations/telegram/announcements/{delivery_id}/ack')
    def ack_announcement(delivery_id: UUID, body: AnnouncementDeliveryInput, request: Request, principal=Depends(authenticate)):
        if header(request,'X-Node-Plane-Telegram-User-ID') is not None:
            raise AccessDenied('invalid_input',422)
        return announcements.acknowledge(principal,str(delivery_id),header(request,'Idempotency-Key'),body.status)

    @app.post('/api/v1/me/access-requests', response_model=AccessRequestOutput, status_code=201)
    def create_access_request(request: Request,
                              command_key: Annotated[str, Header(alias='Idempotency-Key')], current=Depends(actor)):
        return access_requests.create(current, header(request, 'Idempotency-Key'))

    @app.get('/api/v1/system/access-requests', response_model=AccessRequestPolicyOutput)
    def get_access_request_policy(current=Depends(actor)):
        return access_requests.policy(current)

    @app.patch('/api/v1/system/access-requests', response_model=AccessRequestPolicyOutput)
    def edit_access_request_policy(body: AccessRequestPolicyInput, current=Depends(actor)):
        return access_requests.update_policy(current, **body.model_dump(exclude_unset=True))

    @app.get('/api/v1/system/bot-title', response_model=BotTitleOutput)
    def get_bot_title(current=Depends(actor)):
        return system_settings.bot_title(current)

    @app.patch('/api/v1/system/bot-title', response_model=BotTitleOutput)
    def edit_bot_title(body: BotTitleInput, current=Depends(actor)):
        return system_settings.update_bot_title(current, body.title)

    @app.get('/api/v1/system/version', response_model=VersionOutput)
    def get_system_version(current=Depends(actor)):
        require_permission(current, 'account.self.read')
        return {'version': APP_VERSION}

    @app.get('/api/v1/me/access-requests', response_model=AccessRequestPage)
    def own_access_requests(current=Depends(actor), limit: Annotated[int, Query(ge=1, le=100)] = 25,
                            cursor: Annotated[str | None, Query(max_length=512)] = None):
        return access_requests.list_own(current, limit=limit, cursor=cursor)

    @app.get('/api/v1/access-requests', response_model=AccessRequestPage)
    def pending_access_requests(current=Depends(actor), limit: Annotated[int, Query(ge=1, le=100)] = 25,
                                cursor: Annotated[str | None, Query(max_length=512)] = None,
                                search: Annotated[str | None, Query(max_length=128)] = None):
        return access_requests.list_pending(current, limit=limit, cursor=cursor,
                                            search=search)

    @app.get('/api/v1/access-requests/{request_id}', response_model=AccessRequestOutput)
    def get_pending_access_request(request_id: UUID, current=Depends(actor)):
        return access_requests.get_pending(current, str(request_id))

    @app.post('/api/v1/access-requests/{request_id}/decision', response_model=AccessRequestOutput)
    def decide_access_request(request_id: UUID, body: AccessDecisionInput, request: Request,
                              command_key: Annotated[str, Header(alias='Idempotency-Key')], current=Depends(actor)):
        return access_requests.decide(current, str(request_id), body.decision, header(request, 'Idempotency-Key'))

    @app.get('/api/v1/me/profiles', response_model=ProfilePage)
    def own_profiles(current=Depends(actor), limit: Annotated[int, Query(ge=1, le=100)] = 25,
                     cursor: Annotated[str | None, Query(max_length=512)] = None):
        return profiles.list_owned(current, limit=limit, cursor=cursor)

    @app.get('/api/v1/profiles/{profile_id}/summary', response_model=MemberProfileSummary)
    def admin_profile_summary(profile_id: UUID, current=Depends(actor)):
        return profiles.admin_summary(current, str(profile_id))

    @app.get('/api/v1/me/profiles/{profile_id}/summary', response_model=MemberProfileSummary)
    def own_profile_summary(profile_id: UUID, current=Depends(actor)):
        return profiles.own_summary(current, str(profile_id))

    @app.get('/api/v1/profiles', response_model=ProfilePage)
    def all_profiles(current=Depends(actor), limit: Annotated[int, Query(ge=1, le=100)] = 25,
                     cursor: Annotated[str | None, Query(max_length=512)] = None,
                     search: Annotated[str | None, Query(max_length=128)] = None):
        return profiles.list_all(current, limit=limit, cursor=cursor, search=search)

    @app.get('/api/v1/admin/overview', response_model=AdminOverviewOutput)
    def administration_overview(current=Depends(actor)):
        return {'version': APP_VERSION, **admin_overview.get(current)}

    @app.get('/api/v1/profiles/{profile_id}', response_model=ProfileOutput)
    def profile(profile_id: UUID, response: Response, current=Depends(actor)):
        result = profiles.get(current, str(profile_id))
        response.headers['ETag'] = '"' + str(result['desired_revision']) + '"'
        return result

    @app.get('/api/v1/profiles/{profile_id}/grants', response_model=GrantPage)
    def profile_grants(profile_id: UUID, current=Depends(actor)):
        return profiles.grants(current, str(profile_id))

    @app.get('/api/v1/profiles/{profile_id}/devices', response_model=DeviceList)
    def profile_devices(profile_id: UUID, current=Depends(actor)):
        return profiles.devices(current, str(profile_id))

    @app.post('/api/v1/profiles/{profile_id}/devices', response_model=DeviceCommandOutput, status_code=201)
    def create_device(profile_id: UUID, body: DeviceNameInput, request: Request,
                      response: Response, command_key: Annotated[str, Header(alias='Idempotency-Key')],
                      current=Depends(actor)):
        result = device_commands.execute(current, header(request, 'Idempotency-Key'),
            action='create', profile_id=str(profile_id), revision=revision_header(request),
            display_name=body.display_name)
        response.headers['ETag'] = '"' + str(result['device']['revision']) + '"'
        return result

    @app.patch('/api/v1/profiles/{profile_id}/devices/{device_id}', response_model=DeviceCommandOutput)
    def rename_device(profile_id: UUID, device_id: UUID, body: DeviceNameInput, request: Request,
                      response: Response, command_key: Annotated[str, Header(alias='Idempotency-Key')],
                      current=Depends(actor)):
        result = device_commands.execute(current, header(request, 'Idempotency-Key'),
            action='rename', profile_id=str(profile_id), device_id=str(device_id),
            revision=revision_header(request), display_name=body.display_name)
        response.headers['ETag'] = '"' + str(result['device']['revision']) + '"'
        return result

    @app.delete('/api/v1/profiles/{profile_id}/devices/{device_id}', response_model=DeviceCommandOutput, status_code=202)
    def delete_device(profile_id: UUID, device_id: UUID, request: Request,
                      response: Response, command_key: Annotated[str, Header(alias='Idempotency-Key')],
                      current=Depends(actor)):
        result = device_commands.execute(current, header(request, 'Idempotency-Key'),
            action='delete', profile_id=str(profile_id), device_id=str(device_id),
            revision=revision_header(request))
        response.headers['ETag'] = '"' + str(result['device']['revision']) + '"'
        return result

    @app.get('/api/v1/profiles/{profile_id}/operation', response_model=OperationOutput | None)
    def latest_profile_operation(profile_id: UUID, current=Depends(actor)):
        profiles.get(current, str(profile_id))
        return operations.latest_for_profile(current, str(profile_id))

    @app.get('/api/v1/me/nodes', response_model=NodePage)
    def own_nodes(current=Depends(actor), limit: Annotated[int, Query(ge=1, le=100)] = 25,
                  cursor: Annotated[str | None, Query(max_length=512)] = None):
        return profiles.available_nodes(current, limit=limit, cursor=cursor)

    @app.get('/api/v1/profiles/{profile_id}/nodes', response_model=NodePage)
    def profile_nodes(profile_id: UUID, current=Depends(actor),
                      limit: Annotated[int, Query(ge=1, le=100)] = 25,
                      cursor: Annotated[str | None, Query(max_length=512)] = None):
        return profiles.profile_nodes(current, str(profile_id), limit=limit, cursor=cursor)

    @app.get('/api/v1/nodes', response_model=AdminNodePage)
    def list_nodes(current=Depends(actor), limit: Annotated[int, Query(ge=1, le=100)] = 25,
                   cursor: Annotated[str | None, Query(max_length=8192)] = None,
                   search: Annotated[str | None, Query(max_length=128)] = None,
                   include_summary: bool = False, order: Literal['key', 'region'] = 'key'):
        result = nodes.list(current, limit=limit, cursor=cursor, search=search, order=order)
        if include_summary:
            for node in result['items']:
                node['overview'] = node_overview.get(current, node['key'])
        return result

    @app.get('/api/v1/nodes/creation-options')
    def node_creation_options(current=Depends(actor)):
        return installation_defaults.creation_options(current)

    @app.get('/api/v1/system/installation-defaults')
    def get_installation_defaults(response: Response, current=Depends(actor)):
        result = installation_defaults.get(current)
        response.headers['ETag'] = '"' + str(result['revision']) + '"'
        return result

    @app.put('/api/v1/system/installation-defaults')
    def put_installation_defaults(body: InstallationDefaultsInput, request: Request,
                                  response: Response, current=Depends(actor)):
        result = installation_defaults.update(current, body.model_dump(exclude_unset=True), revision_header(request))
        response.headers['ETag'] = '"' + str(result['revision']) + '"'
        return result

    @app.get('/api/v1/nodes/{node_key}', response_model=AdminNodeOutput)
    def get_node(node_key: str, response: Response, current=Depends(actor)):
        result = nodes.get(current, node_key)
        response.headers['ETag'] = '"' + str(result['desired_revision']) + '"'
        return result

    @app.get('/api/v1/nodes/{node_key}/overview', response_model=AdminNodeOverviewOutput)
    def get_node_overview(node_key: str, current=Depends(actor)):
        result = node_overview.get(current, node_key)
        result['traffic'] = TrafficService(db).node_summary(node_key)
        return result

    @app.get('/api/v1/nodes/{node_key}/services')
    def get_node_services(node_key: str, current=Depends(actor)):
        nodes.get(current, node_key)
        try:
            if node_driver is not None:
                return node_driver.inspect_node_services(node_key)
            from .driver_transport import GrpcIntentDriver, local_channel
            with local_channel('127.0.0.1:50051') as channel:
                return GrpcIntentDriver(channel).inspect_node_services(node_key)
        except Exception as failure:
            import grpc
            if isinstance(failure, grpc.RpcError) and failure.code() == grpc.StatusCode.FAILED_PRECONDITION:
                raise AccessDenied('node_agent_unconfigured', 409) from None
            raise AccessDenied('node_agent_unavailable', 503) from None

    @app.post('/api/v1/nodes/{node_key}/bootstrap', status_code=202)
    def bootstrap_node(node_key: str, body: NodeActionInput,
                       idempotency_key: str | None = Header(default=None), current=Depends(actor)):
        if body.action != 'bootstrap':
            raise AccessDenied('invalid_input', 422)
        return node_bootstraps.queue(current, node_key, body.revision, idempotency_key)

    @app.get('/api/v1/node-bootstraps/{identity}')
    def get_node_bootstrap(identity: UUID, current=Depends(actor)):
        return node_bootstraps.get(current, str(identity))

    @app.post('/api/v1/nodes/{node_key}/actions', status_code=202)
    def queue_node_action(node_key: str, body: NodeActionInput,
                          idempotency_key: str | None = Header(default=None), current=Depends(actor)):
        return node_jobs.queue(current, node_key, body.action, revision=body.revision, command_key=idempotency_key)

    @app.get('/api/v1/node-jobs/{job_id}')
    def get_node_job(job_id: str, current=Depends(actor)):
        return node_jobs.get(current, job_id)

    @app.post('/api/v1/node-jobs/{job_id}/resolve')
    def resolve_node_job(job_id: str, current=Depends(actor)):
        with maintenance_lock(current):
            try:
                if node_driver is not None:
                    return NodeOperations(db, node_driver).resolve(current, job_id)
                from .driver_transport import GrpcIntentDriver, local_channel
                with local_channel('127.0.0.1:50051') as channel:
                    return NodeOperations(db, GrpcIntentDriver(channel)).resolve(current, job_id)
            except AccessDenied:
                raise
            except Exception:
                raise AccessDenied('node_repair_unconfirmed', 409) from None

    @app.get('/api/v1/nodes/{node_key}/runtime', response_model=NodeRuntimeObservation)
    def inspect_node_runtime(node_key: str, current=Depends(actor)):
        node = nodes.get(current, node_key)
        try:
            if node_driver is None:
                from .driver_transport import GrpcIntentDriver, local_channel
                with local_channel('127.0.0.1:50051') as channel:
                    observation = GrpcIntentDriver(channel).inspect_node(node_key)
            else:
                observation = node_driver.inspect_node(node_key)
        except Exception as failure:
            import grpc
            if isinstance(failure, grpc.RpcError):
                if failure.code() == grpc.StatusCode.FAILED_PRECONDITION:
                    raise AccessDenied('node_agent_unconfigured', 409) from None
                if failure.code() in {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED}:
                    raise AccessDenied('node_agent_unavailable', 503) from None
            raise AccessDenied('driver_unavailable', 503) from None
        # Runtime facts prove only agent connectivity/config-file presence.
        # They cannot acknowledge the desired settings or enable the node.
        return {**observation, 'desired_revision': node['desired_revision'],
                'applied_revision': node['applied_revision'], 'settings_verified': False}

    @app.get('/api/v1/nodes/{node_key}/diagnostics', response_model=NodeDiagnosticsOutput)
    def get_node_diagnostics(node_key: str, current=Depends(actor)):
        nodes.get(current, node_key)
        try:
            if node_driver is None:
                from .driver_transport import GrpcIntentDriver, local_channel
                with local_channel('127.0.0.1:50051') as channel:
                    return GrpcIntentDriver(channel).diagnose_node(node_key)
            return node_driver.diagnose_node(node_key)
        except Exception as failure:
            import grpc
            from .driver_transport import AgentNotConfigured
            if isinstance(failure, AgentNotConfigured):
                raise AccessDenied('node_agent_unconfigured', 409) from None
            if isinstance(failure, grpc.RpcError):
                if failure.code() == grpc.StatusCode.FAILED_PRECONDITION:
                    raise AccessDenied('node_agent_unconfigured', 409) from None
                if failure.code() in {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED}:
                    raise AccessDenied('node_agent_unavailable', 503) from None
            raise AccessDenied('driver_unavailable', 503) from None

    @app.post('/api/v1/nodes', response_model=AdminNodeOutput, status_code=201)
    def create_node(body: NodeCreateInput, request: Request, response: Response,
                    command_key: Annotated[str, Header(alias='Idempotency-Key')], current=Depends(actor)):
        result = nodes.command(current, action='create', values=body.model_dump(exclude_unset=True),
                               command_key=header(request, 'Idempotency-Key'))
        response.headers['ETag'] = '"' + str(result['desired_revision']) + '"'
        return result

    @app.patch('/api/v1/nodes/{node_key}', response_model=AdminNodeOutput)
    def edit_node(node_key: str, body: NodeEditInput, request: Request, response: Response,
                  command_key: Annotated[str, Header(alias='Idempotency-Key')],
                  if_match: Annotated[str | None, Header(alias='If-Match')] = None, current=Depends(actor)):
        result = nodes.command(current, action='edit', node_key=node_key,
                               values=body.model_dump(exclude_unset=True),
                               revision=revision_header(request), command_key=header(request, 'Idempotency-Key'))
        response.headers['ETag'] = '"' + str(result['desired_revision']) + '"'
        return result

    @app.post('/api/v1/nodes/{node_key}/apply-settings', response_model=NodeSettingsTaskOutput, status_code=202)
    def apply_backend_node_settings(node_key: str, request: Request,
                                    command_key: Annotated[str, Header(alias='Idempotency-Key')],
                                    if_match: Annotated[str | None, Header(alias='If-Match')] = None,
                                    current=Depends(actor)):
        return node_settings.queue(current, node_key, revision=revision_header(request),
                                   command_key=header(request, 'Idempotency-Key'))

    @app.get('/api/v1/node-settings-operations/{task_id}', response_model=NodeSettingsTaskOutput)
    def get_node_settings_operation(task_id: UUID, current=Depends(actor)):
        return node_settings.get(current, str(task_id))

    @app.post('/api/v1/nodes/{node_key}/agent-rollouts',
              response_model=AgentRolloutOutput, status_code=202)
    def request_agent_rollout(node_key: str, body: AgentRolloutInput, request: Request,
                              command_key: Annotated[str, Header(alias='Idempotency-Key')],
                              current=Depends(actor)):
        return agent_rollouts.request(current, node_key, header(request, 'Idempotency-Key'),
            transport=body.transport, ssh_target=body.ssh_target, ssh_port=body.ssh_port, install_rust=body.install_rust)

    @app.get('/api/v1/agent-rollouts/{task_id}', response_model=AgentRolloutOutput)
    def get_agent_rollout(task_id: UUID, current=Depends(actor)):
        return agent_rollouts.get(current, str(task_id))

    @app.get('/api/v1/nodes/{node_key}/maintenance', response_model=NodeMaintenanceOutput)
    def get_node_maintenance(node_key: str, current=Depends(actor)):
        return lifecycle.overview(current, node_key)

    @app.post('/api/v1/nodes/{node_key}/bind-verification-target',
              response_model=VerificationTargetOutput)
    def bind_node_verification_target(node_key: str, body: VerificationTargetInput,
                                      current=Depends(actor)):
        from .removal_verifier import RemovalVerifier, RemovalVerificationError
        if body.transport == 'local':
            if body.ssh_target is not None or body.ssh_port != 22:
                raise AccessDenied('invalid_input', 422)
        elif not body.ssh_target or body.ssh_port != 22:
            raise AccessDenied('invalid_input', 422)
        try:
            verifier = RemovalVerifier(local=body.transport == 'local',
                ssh_target=body.ssh_target,
                ssh_identity_file=(os.environ.get('SSH_KEY') or SSH_KEY) if body.transport == 'ssh' else None,
                ssh_port=body.ssh_port)
        except ValueError:
            raise AccessDenied('invalid_input', 422) from None
        with maintenance_lock(current):
            try:
                return lifecycle.bind_verification_target(current, node_key, verifier)
            except RemovalVerificationError:
                raise AccessDenied('host_verification_failed', 409) from None

    @app.post('/api/v1/nodes/{node_key}/drain', response_model=DrainOutput)
    def drain_node(node_key: str, current=Depends(actor)):
        with maintenance_lock(current):
            with db.connect() as conn:
                bound = conn.execute('''SELECT 1 FROM backend_node_verification_targets
                    WHERE node_key = ?''', (node_key,)).fetchone()
            if bound is None:
                raise AccessDenied('verification_target_required', 409)
            return lifecycle.start_drain(current, node_key)

    @app.post('/api/v1/nodes/{node_key}/cleanup-step',
              response_model=CleanupStepOutput)
    def cleanup_node_step(node_key: str, body: CleanupStepInput,
                          current=Depends(actor)):
        from .driver_transport import GrpcIntentDriver, local_channel
        with maintenance_lock(current):
            try:
                with local_channel(os.environ.get('NODE_DRIVER_GRPC_TARGET',
                                               '127.0.0.1:50051')) as channel:
                    return lifecycle.cleanup(current, node_key,
                        GrpcIntentDriver(channel), expected_phase=body.expected_phase)
            except AccessDenied:
                raise
            except Exception:
                raise AccessDenied('node_cleanup_unavailable', 503) from None

    @app.post('/api/v1/nodes/{node_key}/verify-and-retire',
              response_model=VerifiedRetirementOutput)
    def verify_and_retire_node(node_key: str, current=Depends(actor)):
        from .removal_verifier import RemovalVerifier, RemovalVerificationError
        with maintenance_lock(current):
            with db.connect() as conn:
                bound = conn.execute('''SELECT target FROM backend_node_verification_targets
                    WHERE node_key = ?''', (node_key,)).fetchone()
            if bound is None:
                raise AccessDenied('verification_target_required', 409)
            bot_key_file = os.environ.get('NODE_PLANE_BOT_PUBLIC_KEY_FILE')
            if not bot_key_file and os.environ.get('SSH_KEY'):
                bot_key_file = os.environ['SSH_KEY'] + '.pub'
            if not bot_key_file:
                raise AccessDenied('verification_key_unavailable', 503)
            try:
                bot_key = Path(bot_key_file).read_text(encoding='utf-8').strip()
                if bound['target'] == 'local':
                    verifier = RemovalVerifier(local=True, bot_public_key=bot_key)
                else:
                    independent_key = os.environ.get('NODE_PLANE_REMOVAL_SSH_KEY')
                    if not independent_key or not Path(independent_key).is_file():
                        raise AccessDenied('independent_verification_key_required', 503)
                    bot_private_key = os.environ.get('SSH_KEY')
                    if bot_private_key and Path(bot_private_key).is_file() and os.path.samefile(
                            independent_key, bot_private_key):
                        raise AccessDenied('independent_verification_key_required', 503)
                    verifier = RemovalVerifier(ssh_target=bound['target'],
                        ssh_identity_file=independent_key, bot_public_key=bot_key)
                return lifecycle.retire_verified(current, node_key, verifier)
            except (OSError, ValueError, RemovalVerificationError):
                raise AccessDenied('host_verification_failed', 409) from None

    @app.post('/api/v1/nodes/{node_key}/retire-registry-only',
              response_model=RegistryRetirementOutput)
    def retire_node_registry_only(node_key: str, body: RegistryRetirementInput,
                                  current=Depends(actor)):
        with maintenance_lock(current):
            return lifecycle.retire_registry_only(current, node_key, body.reason)

    @app.post('/api/v1/nodes/{node_key}/remove-step')
    def remove_node_step(node_key: str, body: NodeRemovalInput, current=Depends(actor)):
        from .node_removal import NodeRemovalService
        return NodeRemovalService(db).request(current, node_key, retry=body.retry)

    @app.get('/api/v1/nodes/{node_key}/removal')
    def get_node_removal(node_key: str, current=Depends(actor)):
        from .node_removal import NodeRemovalService
        return NodeRemovalService(db).get(current, node_key)

    def revision_header(request):
        value = header(request, 'If-Match')
        if value is None:
            raise AccessDenied('revision_required', 428)
        if len(value) > 24 or not value.startswith('"') or not value.endswith('"') or not value[1:-1].isascii() or not value[1:-1].isdecimal():
            raise AccessDenied('invalid_revision', 422)
        revision = int(value[1:-1])
        if revision < 1:
            raise AccessDenied('invalid_revision', 422)
        return revision

    @app.get('/api/v1/accounts', response_model=AccountPage)
    def list_accounts(current=Depends(actor), limit: Annotated[int, Query(ge=1, le=100)] = 25,
                      cursor: Annotated[str | None, Query(max_length=512)] = None):
        return accounts.list(current, limit=limit, cursor=cursor)

    @app.get('/api/v1/accounts/{account_id}', response_model=AdminAccountOutput)
    def get_account(account_id: UUID, response: Response, current=Depends(actor)):
        result = accounts.get(current, str(account_id))
        response.headers['ETag'] = '"' + str(result['revision']) + '"'
        return result

    @app.patch('/api/v1/accounts/{account_id}', response_model=AdminAccountOutput)
    def update_account(account_id: UUID, body: AccountEditInput, request: Request, response: Response,
                       command_key: Annotated[str, Header(alias='Idempotency-Key')],
                       if_match: Annotated[str | None, Header(alias='If-Match')] = None, current=Depends(actor)):
        result = accounts.update(current, str(account_id), role=body.role, status=body.status,
                                 revision=revision_header(request), command_key=header(request, 'Idempotency-Key'))
        response.headers['ETag'] = '"' + str(result['revision']) + '"'
        return result

    def mutation_response(response, result):
        response.headers['ETag'] = '"' + str(result['profile']['desired_revision']) + '"'
        return result

    @app.get('/api/v1/regions')
    def grant_regions(current=Depends(actor), limit: Annotated[int, Query(ge=1, le=100)] = 100,
                      cursor: Annotated[str | None, Query(max_length=512)] = None):
        from .grant_policies import GrantPolicies
        return GrantPolicies(db).regions(current, limit=limit, cursor=cursor)

    @app.get('/api/v1/nodes/{node_key}/region-access-preview')
    def preview_region_access(node_key: str, region: Annotated[str, Query(min_length=1, max_length=128)],
                              current=Depends(actor)):
        from .grant_policies import GrantPolicies
        return GrantPolicies(db).preview_region(current, node_key, region)

    @app.get('/api/v1/profiles/{profile_id}/access-policy')
    def get_access_policy(profile_id: UUID, response: Response, current=Depends(actor)):
        from .grant_policies import GrantPolicies
        result = GrantPolicies(db).get(current, str(profile_id))
        response.headers['ETag'] = '"' + str(result['revision']) + '"'
        return result

    @app.put('/api/v1/profiles/{profile_id}/access-policy', response_model=ProfileCommandOutput)
    def replace_access_policy(profile_id: UUID, body: GrantPolicyInput, request: Request, response: Response,
                              command_key: Annotated[str, Header(alias='Idempotency-Key')],
                              if_match: Annotated[str | None, Header(alias='If-Match')] = None,
                              current=Depends(actor)):
        result = profile_commands.execute(current, header(request, 'Idempotency-Key'), action='policy',
            profile_id=str(profile_id), revision=revision_header(request), values=body.model_dump(mode='json'))
        return mutation_response(response, result)

    @app.post('/api/v1/profiles', response_model=ProfileCommandOutput, status_code=201)
    def create_profile(body: ProfileCreateInput, request: Request, response: Response,
                       command_key: Annotated[str, Header(alias='Idempotency-Key')], current=Depends(actor)):
        result = profile_commands.execute(current, header(request, 'Idempotency-Key'), action='create',
                                          values=body.model_dump(mode='json'))
        return mutation_response(response, result)

    @app.patch('/api/v1/profiles/{profile_id}', response_model=ProfileCommandOutput)
    def edit_profile(profile_id: UUID, body: ProfileEditInput, request: Request, response: Response,
                     command_key: Annotated[str, Header(alias='Idempotency-Key')],
                     if_match: Annotated[str | None, Header(alias='If-Match')] = None, current=Depends(actor)):
        result = profile_commands.execute(current, header(request, 'Idempotency-Key'), action='edit',
            profile_id=str(profile_id), revision=revision_header(request), values=body.model_dump(mode='json', exclude_unset=True))
        return mutation_response(response, result)

    @app.patch('/api/v1/profiles/{profile_id}/grants', response_model=ProfileCommandOutput)
    def replace_grants(profile_id: UUID, body: GrantsInput, request: Request, response: Response,
                       command_key: Annotated[str, Header(alias='Idempotency-Key')],
                       if_match: Annotated[str | None, Header(alias='If-Match')] = None, current=Depends(actor)):
        result = profile_commands.execute(current, header(request, 'Idempotency-Key'), action='grants',
            profile_id=str(profile_id), revision=revision_header(request), values=body.model_dump())
        return mutation_response(response, result)

    @app.delete('/api/v1/profiles/{profile_id}', response_model=ProfileCommandOutput)
    def delete_profile(profile_id: UUID, request: Request, response: Response,
                       command_key: Annotated[str, Header(alias='Idempotency-Key')],
                       if_match: Annotated[str | None, Header(alias='If-Match')] = None,
                       current=Depends(actor)):
        result = profile_commands.execute(current, header(request, 'Idempotency-Key'),
            action='delete', profile_id=str(profile_id), revision=revision_header(request))
        return mutation_response(response, result)

    @app.get('/api/v1/operations/{operation_id}', response_model=OperationOutput)
    def operation(operation_id: UUID, current=Depends(actor)):
        return operations.get(current, str(operation_id))

    @app.post('/api/v1/profiles/{profile_id}/config-issuances',
              response_model=ConfigIssuanceOutput, status_code=202)
    def request_config(profile_id: UUID, body: ConfigIssuanceInput, request: Request,
                       command_key: Annotated[str, Header(alias='Idempotency-Key')],
                       current=Depends(actor), wait: bool = False):
        queued = config_issuances.request(current, str(profile_id), body.node_key,
            body.protocol, body.transport, header(request, 'Idempotency-Key'),
            device_id=str(body.device_id) if body.device_id else None)
        if wait:
            # This endpoint runs in FastAPI's thread pool. These are read-only
            # live checks; mutations remain in the durable worker.
            config_issuances.run_one(queued['id'])
            return config_issuances.get(current, queued['id'])
        return queued

    @app.get('/api/v1/config-issuances/{issuance_id}', response_model=ConfigIssuanceOutput)
    def get_config_issuance(issuance_id: UUID, current=Depends(actor)):
        return config_issuances.get(current, str(issuance_id))

    @app.get('/api/v1/config-issuances/{issuance_id}/artifact', response_model=ConfigArtifactOutput)
    def get_config_artifact(issuance_id: UUID, current=Depends(actor)):
        return config_issuances.artifact(current, str(issuance_id))


    class TemporaryConfigInput(BaseModel):
        node_key: str
        protocol: str
        transport: str
        duration_seconds: int

    @app.get('/api/v1/system/temporary-configs')
    def list_temporary_configs(page: int = 0, page_size: int = 20,
                               node_key: str | None = None, current=Depends(actor)):
        return temporary_configs.list(current,node_key=node_key,page=page,page_size=page_size)

    @app.post('/api/v1/system/temporary-configs', status_code=202)
    def create_temporary_config(body: TemporaryConfigInput,
        command_key: Annotated[str, Header(alias='Idempotency-Key')], current=Depends(actor)):
        return temporary_configs.create(current,body.node_key,body.protocol,body.transport,
                                        body.duration_seconds,command_key)

    @app.get('/api/v1/system/temporary-configs/{config_id}')
    def get_temporary_config(config_id: UUID, current=Depends(actor)):
        return temporary_configs.get(current,str(config_id))

    @app.get('/api/v1/system/temporary-configs/{config_id}/artifact')
    def get_temporary_artifact(config_id: UUID, current=Depends(actor)):
        return temporary_configs.artifact(current,str(config_id))

    @app.post('/api/v1/system/temporary-configs/{config_id}/revoke', status_code=202)
    def revoke_temporary_config(config_id: UUID, current=Depends(actor)):
        return temporary_configs.revoke(current,str(config_id))

    class SshKeyOutput(BaseModel):
        public_key: str

    @app.get('/api/v1/system/ssh-key', response_model=SshKeyOutput)
    def get_system_ssh_key(current=Depends(actor)):
        import subprocess
        import os
        import pathlib
        require_permission(current, 'settings.manage')
        private_path = pathlib.Path(os.environ.get('SSH_KEY') or SSH_KEY)
        public_path = pathlib.Path(f"{private_path}.pub")
        private_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(private_path.parent, 0o700)
        
        if private_path.exists() and not public_path.exists():
            proc = subprocess.run(["ssh-keygen", "-y", "-f", str(private_path)], capture_output=True, text=True)
            if proc.returncode == 0:
                public_path.write_text((proc.stdout or "").strip() + "\n", encoding="utf-8")
                os.chmod(public_path, 0o644)
        
        if not private_path.exists():
            proc = subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "node-plane", "-f", str(private_path)], capture_output=True, text=True)
            if proc.returncode == 0:
                os.chmod(private_path, 0o600)
                if public_path.exists():
                    os.chmod(public_path, 0o644)

        if not public_path.exists():
            raise HTTPException(500, detail="Failed to ensure SSH keypair")
            
        return SshKeyOutput(public_key=public_path.read_text(encoding='utf-8').strip())


    @app.get('/api/v1/system/updates')
    def get_updates(current=Depends(actor)):
        require_permission(current, 'settings.manage')
        import app.services.updates as updater
        return {**updater.get_updates_overview(refresh_run=False), 'latest_job': update_service.latest(current),
                'dismissed_job_ids': update_service.dismissed_results(current)}

    @app.patch('/api/v1/system/updates/preferences')
    def edit_update_preferences(body: UpdatePreferencesInput, current=Depends(actor)):
        require_permission(current, 'settings.manage')
        values = body.model_dump(exclude_unset=True)
        if not values or any(value is None for value in values.values()):
            raise AccessDenied('invalid_input', 422)
        from app.services import app_settings, updates
        if 'branch' in values:
            app_settings.set_updates_branch(values['branch'])
        if 'dev_track' in values:
            app_settings.set_updates_dev_track(values['dev_track'])
        if 'auto_check_enabled' in values:
            app_settings.set_updates_auto_check_enabled(values['auto_check_enabled'])
        if 'auto_check_interval_minutes' in values:
            app_settings.set_updates_check_interval_minutes(values['auto_check_interval_minutes'])
        return updates.get_updates_overview(refresh_run=False)

    @app.post('/api/v1/system/updates/check')
    def check_updates(current=Depends(actor)):
        require_permission(current, 'settings.manage')
        import app.services.updates as updater
        return updater.check_for_updates()

    @app.post('/api/v1/system/updates/run', status_code=202)
    def run_update(body: UpdateRunInput, request: Request, current=Depends(actor)):
        with live_updates() as service:
            return service.queue(current, header(request, 'Idempotency-Key'), body.kind,
                                 target_ref=body.target_ref, branch=body.branch)

    @app.get('/api/v1/system/updates/versions')
    def update_versions(offset: int = Query(default=0, ge=0), current=Depends(actor)):
        return update_service.versions(current, offset)

    @app.get('/api/v1/system/updates/rollout')
    def update_rollout(current=Depends(actor)):
        with live_updates() as service:
            return service.rollout_overview(current, use_cache=True)

    @app.get('/api/v1/system/recovery/history')
    def recovery_history(offset: int = Query(default=0, ge=0, le=1000000), current=Depends(actor)):
        from .recovery import history
        return history(db, current, offset)

    @app.get('/api/v1/system/recovery/controller')
    def recovery_controller(current=Depends(actor)):
        require_permission(current, 'maintenance.manage')
        import importlib.util
        source = Path(__file__).resolve().parents[2] / 'scripts' / 'installation_diagnostics.py'
        try:
            spec = importlib.util.spec_from_file_location('controller_diagnostics', source)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.observe()
        except Exception:
            raise AccessDenied('diagnostics_unavailable', 503) from None

    @app.post('/api/v1/system/recovery/{kind}/{identity}/{action}')
    def recover_operation(kind: str, identity: UUID, action: str, current=Depends(actor)):
        from .recovery import act
        with maintenance_lock(current):
            if node_driver is not None:
                return act(db, current, node_driver, kind, str(identity), action)
            from .driver_transport import GrpcIntentDriver, local_channel
            with local_channel('127.0.0.1:50051') as channel:
                return act(db, current, GrpcIntentDriver(channel, timeout=10), kind, str(identity), action)

    @app.get('/api/v1/system/recovery', response_model=RecoveryOverviewOutput)
    def recovery_overview(offset: int = Query(default=0, ge=0, le=1000000), current=Depends(actor)):
        from .recovery import overview
        return overview(db, current, offset)

    @app.get('/api/v1/system/workstation-audit', response_model=WorkstationAuditPageOutput)
    def workstation_audit(offset: int = Query(default=0, ge=0, le=1000000),
                          errors_only: bool = False, current=Depends(actor)):
        from .workstation_audit import WorkstationAudit
        return WorkstationAudit(db).page(current, offset, errors_only)

    @app.get('/api/v1/system/updates/jobs/{job_id}')
    def update_job(job_id: UUID, current=Depends(actor)):
        return update_service.get(current, str(job_id))

    @app.post('/api/v1/system/updates/jobs/{job_id}/dismiss')
    def dismiss_update_result(job_id: UUID, current=Depends(actor)):
        return update_service.dismiss_result(current, str(job_id))

    @app.post('/api/v1/system/updates/jobs/{job_id}/cancel')
    def cancel_update(job_id: UUID, current=Depends(actor)):
        from .recovery import track
        with maintenance_lock(current), track(db,current,'update',str(job_id),'cancel'), live_updates() as service:
            return service.cancel(current, str(job_id))

    @app.post('/api/v1/system/updates/jobs/{job_id}/recheck')
    def recheck_update(job_id: UUID, current=Depends(actor)):
        from .recovery import track
        with maintenance_lock(current), track(db,current,'update',str(job_id),'recheck'), live_updates() as service:
            return service.recheck(current, str(job_id))

    @app.get('/api/v1/system/backups')
    def get_backups(current=Depends(actor)):
        return backup_service.overview(current)

    @app.get('/api/v1/system/backups/catalog')
    def backup_catalog(offset:int=Query(default=0,ge=0),current=Depends(actor)):
        return backup_service.catalog(current,offset)

    @app.get('/api/v1/system/backups/catalog/{backup_id}')
    def backup_detail(backup_id:UUID,current=Depends(actor)):
        return backup_service.detail(current,str(backup_id))

    @app.patch('/api/v1/system/backups/preferences')
    def backup_preferences(body:BackupPreferencesInput,current=Depends(actor)):
        return backup_service.preferences(current,body.model_dump(exclude_unset=True))

    @app.post('/api/v1/system/backups/jobs',status_code=202)
    def backup_command(body:BackupCommandInput,request:Request,current=Depends(actor)):
        return backup_service.queue(current,header(request,'Idempotency-Key'),body.action,
                                    str(body.backup_id) if body.backup_id else None,body.checksum)

    @app.get('/api/v1/system/backups/jobs/{job_id}')
    def backup_job(job_id:UUID,current=Depends(actor)):
        return backup_service.get(current,str(job_id))

    return app


def application():
    """Uvicorn factory; database configuration is loaded only at process startup."""
    from db import get_db
    return create_app(get_db())
