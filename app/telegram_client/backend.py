"""Async HTTP transport; Telegram identities are delegated, never trusted as roles."""
from __future__ import annotations

from uuid import uuid4
from urllib.parse import urlsplit

import aiohttp


class BackendError(Exception):
    def __init__(self, code: str, status: int):
        self.code, self.status = code, status
        super().__init__(code)


class BackendClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, adapter_token: str):
        parsed = urlsplit(base_url)
        if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'} or not parsed.port or parsed.path not in {'', '/'}:
            raise ValueError('Telegram adapter requires a loopback backend endpoint')
        self.session = session
        self.base_url = base_url.rstrip('/')
        self.adapter_token = adapter_token

    async def request(self, method: str, path: str, *, telegram_user_id: int | None = None,
                      body: dict | None = None, command: bool = False,
                      revision: int | None = None, command_key: str | None = None,
                      timeout_seconds: int | None = None) -> dict:
        headers = {'Authorization': f'Bearer {self.adapter_token}'}
        if telegram_user_id is not None:
            headers['X-Node-Plane-Telegram-User-ID'] = str(telegram_user_id)
        if command:
            headers['Idempotency-Key'] = command_key or str(uuid4())
        if revision is not None:
            headers['If-Match'] = f'"{revision}"'
        options = {'headers': headers, 'json': body}
        if timeout_seconds is not None:
            options['timeout'] = aiohttp.ClientTimeout(total=timeout_seconds)
        try:
            async with self.session.request(method, self.base_url + path,
                                            **options) as response:
                data = await response.json()
                if response.status >= 400:
                    error = data.get('error', {}) if isinstance(data, dict) else {}
                    raise BackendError(error.get('code', 'backend_unavailable'), response.status)
                return data
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise BackendError('backend_unavailable', 503) from exc

    async def resolve(self, telegram_user_id: int) -> dict:
        return await self.request('POST', '/api/v1/integrations/telegram/identities/resolve',
                                  body={'telegram_user_id': telegram_user_id}, command=True)

    async def me(self, telegram_user_id: int) -> dict:
        return await self.request('GET', '/api/v1/me', telegram_user_id=telegram_user_id)

    async def profiles(self, telegram_user_id: int) -> dict:
        return await self.request('GET', '/api/v1/me/profiles?limit=100', telegram_user_id=telegram_user_id)

    async def nodes(self, telegram_user_id: int) -> dict:
        return await self.request('GET', '/api/v1/me/nodes?limit=100', telegram_user_id=telegram_user_id)

    async def profile_nodes(self, telegram_user_id: int, profile_id: str) -> dict:
        return await self.request('GET', f'/api/v1/profiles/{profile_id}/nodes?limit=100',
                                  telegram_user_id=telegram_user_id)

    async def request_access(self, telegram_user_id: int) -> dict:
        return await self.request('POST', '/api/v1/me/access-requests',
                                  telegram_user_id=telegram_user_id, command=True)

    async def issue(self, telegram_user_id: int, profile_id: str, node_key: str,
                    protocol: str, transport: str) -> dict:
        return await self.request('POST', f'/api/v1/profiles/{profile_id}/config-issuances',
            telegram_user_id=telegram_user_id, command=True,
            body={'node_key': node_key, 'protocol': protocol, 'transport': transport})

    async def issuance(self, telegram_user_id: int, issuance_id: str) -> dict:
        return await self.request('GET', f'/api/v1/config-issuances/{issuance_id}',
                                  telegram_user_id=telegram_user_id)

    async def artifact(self, telegram_user_id: int, issuance_id: str) -> dict:
        return await self.request('GET', f'/api/v1/config-issuances/{issuance_id}/artifact',
                                  telegram_user_id=telegram_user_id)

    async def accounts(self, telegram_user_id: int) -> dict:
        return await self.request('GET', '/api/v1/accounts?limit=100',
                                  telegram_user_id=telegram_user_id)

    async def admin_profiles(self, telegram_user_id: int) -> dict:
        return await self.request('GET', '/api/v1/profiles?limit=100',
                                  telegram_user_id=telegram_user_id)

    async def admin_nodes(self, telegram_user_id: int) -> dict:
        return await self.request('GET', '/api/v1/nodes?limit=100',
                                  telegram_user_id=telegram_user_id)

    async def profile_grants(self, telegram_user_id: int, profile_id: str) -> dict:
        return await self.request('GET', f'/api/v1/profiles/{profile_id}/grants',
                                  telegram_user_id=telegram_user_id)

    async def create_profile(self, telegram_user_id: int, account_id: str, name: str,
                             command_key: str) -> dict:
        return await self.request('POST', '/api/v1/profiles', telegram_user_id=telegram_user_id,
                                  command=True, command_key=command_key,
                                  body={'display_name': name, 'owner_account_id': account_id})

    async def edit_profile(self, telegram_user_id: int, profile_id: str,
                           revision: int, changes: dict) -> dict:
        return await self.request('PATCH', f'/api/v1/profiles/{profile_id}',
            telegram_user_id=telegram_user_id, command=True, revision=revision, body=changes)

    async def replace_grants(self, telegram_user_id: int, profile_id: str,
                             revision: int, grants: list[dict]) -> dict:
        return await self.request('PATCH', f'/api/v1/profiles/{profile_id}/grants',
            telegram_user_id=telegram_user_id, command=True, revision=revision,
            body={'grants': grants})

    async def node_runtime(self, telegram_user_id: int, node_key: str) -> dict:
        return await self.request('GET', f'/api/v1/nodes/{node_key}/runtime',
                                  telegram_user_id=telegram_user_id)

    async def apply_node_settings(self, telegram_user_id: int, node_key: str,
                                  revision: int, command_key: str | None = None) -> dict:
        return await self.request('POST', f'/api/v1/nodes/{node_key}/apply-settings',
            telegram_user_id=telegram_user_id, command=True,
            command_key=command_key, revision=revision)

    async def node_settings_operation(self, telegram_user_id: int, operation_id: str) -> dict:
        return await self.request('GET', f'/api/v1/node-settings-operations/{operation_id}',
                                  telegram_user_id=telegram_user_id)

    async def create_node(self, telegram_user_id: int, body: dict,
                          command_key: str) -> dict:
        return await self.request('POST', '/api/v1/nodes', telegram_user_id=telegram_user_id,
            command=True, command_key=command_key, body=body)

    async def edit_node(self, telegram_user_id: int, node_key: str, revision: int,
                        body: dict, command_key: str) -> dict:
        return await self.request('PATCH', f'/api/v1/nodes/{node_key}',
            telegram_user_id=telegram_user_id, command=True, command_key=command_key,
            revision=revision, body=body)

    async def rollout_agent(self, telegram_user_id: int, node_key: str,
                            transport: str, *, ssh_target: str | None = None,
                            command_key: str) -> dict:
        return await self.request('POST', f'/api/v1/nodes/{node_key}/agent-rollouts',
            telegram_user_id=telegram_user_id, command=True, command_key=command_key,
            body={'transport': transport, 'ssh_target': ssh_target})

    async def agent_rollout(self, telegram_user_id: int, task_id: str) -> dict:
        return await self.request('GET', f'/api/v1/agent-rollouts/{task_id}',
                                  telegram_user_id=telegram_user_id)

    async def node_maintenance(self, telegram_user_id: int, node_key: str) -> dict:
        return await self.request('GET', f'/api/v1/nodes/{node_key}/maintenance',
                                  telegram_user_id=telegram_user_id)

    async def bind_verification_target(self, telegram_user_id: int, node_key: str,
                                       transport: str, ssh_target: str | None = None) -> dict:
        return await self.request('POST',
            f'/api/v1/nodes/{node_key}/bind-verification-target',
            telegram_user_id=telegram_user_id,
            body={'transport': transport, 'ssh_target': ssh_target},
            timeout_seconds=60)

    async def drain_node(self, telegram_user_id: int, node_key: str) -> dict:
        return await self.request('POST', f'/api/v1/nodes/{node_key}/drain',
                                  telegram_user_id=telegram_user_id)

    async def cleanup_node_step(self, telegram_user_id: int, node_key: str,
                                expected_phase: str) -> dict:
        return await self.request('POST', f'/api/v1/nodes/{node_key}/cleanup-step',
                                  telegram_user_id=telegram_user_id,
                                  body={'expected_phase': expected_phase},
                                  timeout_seconds=180)

    async def verify_and_retire_node(self, telegram_user_id: int, node_key: str) -> dict:
        return await self.request('POST',
            f'/api/v1/nodes/{node_key}/verify-and-retire',
            telegram_user_id=telegram_user_id, timeout_seconds=60)

    async def retire_node_registry_only(self, telegram_user_id: int,
                                        node_key: str) -> dict:
        return await self.request('POST',
            f'/api/v1/nodes/{node_key}/retire-registry-only',
            telegram_user_id=telegram_user_id,
            body={'accept_unverified_runtime': True,
                  'reason': 'Operator removed node from Telegram; remote runtime unverified.'})
