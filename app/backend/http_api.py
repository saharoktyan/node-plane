"""HTTP transport for identity services; constructing an app performs no DDL."""

from uuid import UUID, uuid4
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Query, Request, Response, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from .authorization import (
    AccessDenied, Principal, PrincipalKind, SELF_PERMISSIONS, APPROVED_PERMISSIONS,
    ADMIN_PERMISSIONS, require_permission, resolve_actor,
)
from .credentials import CredentialService
from .identity import IdentityService
from .identity_repository import SQLIdentityRepository
from .profiles import ProfileRepository, ProfileService
from .profile_commands import ProfileCommands
from .operations import OperationRepository
from .access_requests import AccessRequestService
from .accounts import AccountService
from .nodes import NodeService
from .node_settings import NodeSettingsService


class ResolveInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    telegram_user_id: Annotated[StrictInt, Field(gt=0, lt=2**63)]


class AccountOutput(BaseModel):
    id: str
    role: str
    status: str


class MeOutput(AccountOutput):
    permissions: list[str]


class AdminAccountOutput(AccountOutput):
    revision: int
    telegram_user_id: int | None


class AccountPage(BaseModel):
    items: list[AdminAccountOutput]
    next_cursor: str | None


class AccountEditInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Literal['member', 'admin'] | None = None
    status: Literal['pending', 'approved', 'rejected', 'disabled'] | None = None


class ProfileOutput(BaseModel):
    id: UUID
    display_name: str
    owner_account_id: UUID | None
    frozen: bool
    expires_at: str | None
    desired_revision: int


class ProfilePage(BaseModel):
    items: list[ProfileOutput]
    next_cursor: str | None


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


class NodeCreateInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: StrictStr
    title: StrictStr
    region: StrictStr
    flag: StrictStr = ''
    protocols: list[Literal['awg', 'xray']]
    xray_transports: list[Literal['tcp', 'xhttp']] = Field(default_factory=list)
    settings: NodeSettingsInput = Field(default_factory=NodeSettingsInput)


class NodeEditInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: StrictStr | None = None
    region: StrictStr | None = None
    flag: StrictStr | None = None
    protocols: list[Literal['awg', 'xray']] | None = None
    xray_transports: list[Literal['tcp', 'xhttp']] | None = None
    settings: NodeSettingsInput | None = None


class AdminNodeOutput(BaseModel):
    key: str
    title: str
    region: str
    flag: str
    enabled: bool
    protocols: list[str]
    xray_transports: list[str]
    desired_revision: int
    applied_revision: int
    settings: dict[str, str | int]


class AdminNodePage(BaseModel):
    items: list[AdminNodeOutput]
    next_cursor: str | None


class NodeRuntimeObservation(BaseModel):
    node_key: str
    health_state: str
    runtime_version: str
    runtime_commit: str
    xray_config_present: bool
    awg_config_present: bool
    desired_revision: int
    applied_revision: int
    settings_verified: bool


class NodeSettingsTaskOutput(BaseModel):
    id: UUID
    node_key: str
    revision: int
    status: str


class ProfileCreateInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    display_name: Annotated[str, Field(min_length=1, max_length=128)]
    owner_account_id: UUID | None = None


class ProfileEditInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    display_name: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    frozen: bool | None = Field(default=None, strict=True)
    expires_at: str | None = None


class GrantInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    node_key: Annotated[str, Field(min_length=1, max_length=64)]
    protocol: str


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


class AccessRequestPage(BaseModel):
    items: list[AccessRequestOutput]
    next_cursor: str | None


class AccessDecisionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: Literal['approve', 'reject']


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


class OperationOutput(BaseModel):
    id: UUID
    profile_id: UUID
    desired_revision: int
    status: str
    created_at: str
    tasks: list[OperationTaskOutput]


def create_app(db, *, node_driver=None) -> FastAPI:
    app = FastAPI(title='Node Plane Backend', version='1.0.0', docs_url=None, redoc_url=None)
    identities = SQLIdentityRepository(db)
    credentials = CredentialService(db)
    service = IdentityService(identities)
    profiles = ProfileService(ProfileRepository(db))
    profile_commands = ProfileCommands(db)
    operations = OperationRepository(db)
    access_requests = AccessRequestService(db)
    accounts = AccountService(db)
    nodes = NodeService(db)
    node_settings = NodeSettingsService(db)

    def error(request, code, status):
        headers = {'WWW-Authenticate': 'Bearer'} if status == 401 else None
        return JSONResponse({'error': {'code': code, 'message': code,
                            'request_id': request.state.request_id, 'details': {}}},
                            status_code=status, headers=headers)

    @app.middleware('http')
    async def request_context(request: Request, call_next):
        request.state.request_id = str(uuid4())
        try:
            response = await call_next(request)
        except Exception:
            # Do not serialize DB exceptions, request bodies or credentials.
            response = error(request, 'internal_error', 500)
        response.headers['X-Request-ID'] = request.state.request_id
        response.headers['Cache-Control'] = 'no-store'
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
            with db.connect() as conn:
                # Only readiness for this implemented slice, not node connectivity.
                conn.execute('SELECT id FROM backend_accounts LIMIT 1').fetchone()
                conn.execute('SELECT id FROM backend_credentials LIMIT 1').fetchone()
                conn.execute('SELECT account_id FROM backend_external_identities LIMIT 1').fetchone()
                conn.execute('SELECT account_id FROM backend_identity_commands LIMIT 1').fetchone()
                conn.execute('SELECT id FROM backend_profiles LIMIT 1').fetchone()
                conn.execute('SELECT key FROM backend_nodes LIMIT 1').fetchone()
                conn.execute('SELECT profile_id FROM backend_grants LIMIT 1').fetchone()
                conn.execute('SELECT command_key FROM backend_profile_commands LIMIT 1').fetchone()
                conn.execute('SELECT id FROM backend_operations LIMIT 1').fetchone()
                conn.execute('SELECT id FROM backend_operation_tasks LIMIT 1').fetchone()
                conn.execute('SELECT profile_id FROM backend_profile_identities LIMIT 1').fetchone()
                conn.execute('SELECT task_id FROM backend_repairs LIMIT 1').fetchone()
                conn.execute('SELECT node_key FROM backend_node_drains LIMIT 1').fetchone()
                conn.execute('SELECT node_key FROM backend_node_cleanup LIMIT 1').fetchone()
                conn.execute('SELECT node_key FROM backend_node_retirements LIMIT 1').fetchone()
                conn.execute('SELECT node_key FROM backend_node_verification_targets LIMIT 1').fetchone()
                conn.execute('SELECT fingerprint FROM backend_node_host_identities LIMIT 1').fetchone()
                conn.execute('SELECT id FROM backend_access_requests LIMIT 1').fetchone()
                conn.execute('SELECT actor_account_id FROM backend_account_commands LIMIT 1').fetchone()
                conn.execute('SELECT id FROM backend_account_guard LIMIT 1').fetchone()
                conn.execute('SELECT actor_account_id FROM backend_node_commands LIMIT 1').fetchone()
                conn.execute('SELECT id FROM backend_node_settings_tasks LIMIT 1').fetchone()
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
        return service.resolve_telegram(principal, body.telegram_user_id, command_key=key)

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
                        permissions=permissions)

    @app.post('/api/v1/me/access-requests', response_model=AccessRequestOutput, status_code=201)
    def create_access_request(request: Request,
                              command_key: Annotated[str, Header(alias='Idempotency-Key')], current=Depends(actor)):
        return access_requests.create(current, header(request, 'Idempotency-Key'))

    @app.get('/api/v1/me/access-requests', response_model=AccessRequestPage)
    def own_access_requests(current=Depends(actor), limit: Annotated[int, Query(ge=1, le=100)] = 25,
                            cursor: Annotated[str | None, Query(max_length=512)] = None):
        return access_requests.list_own(current, limit=limit, cursor=cursor)

    @app.get('/api/v1/access-requests', response_model=AccessRequestPage)
    def pending_access_requests(current=Depends(actor), limit: Annotated[int, Query(ge=1, le=100)] = 25,
                                cursor: Annotated[str | None, Query(max_length=512)] = None):
        return access_requests.list_pending(current, limit=limit, cursor=cursor)

    @app.post('/api/v1/access-requests/{request_id}/decision', response_model=AccessRequestOutput)
    def decide_access_request(request_id: UUID, body: AccessDecisionInput, request: Request,
                              command_key: Annotated[str, Header(alias='Idempotency-Key')], current=Depends(actor)):
        return access_requests.decide(current, str(request_id), body.decision, header(request, 'Idempotency-Key'))

    @app.get('/api/v1/me/profiles', response_model=ProfilePage)
    def own_profiles(current=Depends(actor), limit: Annotated[int, Query(ge=1, le=100)] = 25,
                     cursor: Annotated[str | None, Query(max_length=512)] = None):
        return profiles.list_owned(current, limit=limit, cursor=cursor)

    @app.get('/api/v1/profiles/{profile_id}', response_model=ProfileOutput)
    def profile(profile_id: UUID, response: Response, current=Depends(actor)):
        result = profiles.get(current, str(profile_id))
        response.headers['ETag'] = '"' + str(result['desired_revision']) + '"'
        return result

    @app.get('/api/v1/me/nodes', response_model=NodePage)
    def own_nodes(current=Depends(actor), limit: Annotated[int, Query(ge=1, le=100)] = 25,
                  cursor: Annotated[str | None, Query(max_length=512)] = None):
        return profiles.available_nodes(current, limit=limit, cursor=cursor)

    @app.get('/api/v1/nodes', response_model=AdminNodePage)
    def list_nodes(current=Depends(actor), limit: Annotated[int, Query(ge=1, le=100)] = 25,
                   cursor: Annotated[str | None, Query(max_length=512)] = None):
        return nodes.list(current, limit=limit, cursor=cursor)

    @app.get('/api/v1/nodes/{node_key}', response_model=AdminNodeOutput)
    def get_node(node_key: str, response: Response, current=Depends(actor)):
        result = nodes.get(current, node_key)
        response.headers['ETag'] = '"' + str(result['desired_revision']) + '"'
        return result

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
            profile_id=str(profile_id), revision=revision_header(request), values=body.model_dump(exclude_unset=True))
        return mutation_response(response, result)

    @app.patch('/api/v1/profiles/{profile_id}/grants', response_model=ProfileCommandOutput)
    def replace_grants(profile_id: UUID, body: GrantsInput, request: Request, response: Response,
                       command_key: Annotated[str, Header(alias='Idempotency-Key')],
                       if_match: Annotated[str | None, Header(alias='If-Match')] = None, current=Depends(actor)):
        result = profile_commands.execute(current, header(request, 'Idempotency-Key'), action='grants',
            profile_id=str(profile_id), revision=revision_header(request), values=body.model_dump())
        return mutation_response(response, result)

    @app.get('/api/v1/operations/{operation_id}', response_model=OperationOutput)
    def operation(operation_id: UUID, current=Depends(actor)):
        return operations.get(current, str(operation_id))

    return app


def application():
    """Uvicorn factory; database configuration is loaded only at process startup."""
    from db import get_db
    return create_app(get_db())
