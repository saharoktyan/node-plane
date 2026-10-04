"""Async HTTP transport; Telegram identities are delegated, never trusted as roles."""
from __future__ import annotations

from uuid import uuid4
from urllib.parse import quote, urlencode, urlsplit

import aiohttp
import logging


class BackendError(Exception):
    def __init__(self, code: str, status: int):
        self.code, self.status = code, status
        super().__init__(code)


class BackendClient:
    async def node_services(self, user_id, node_key):
        return await self.request('GET', f'/api/v1/nodes/{node_key}/services', telegram_user_id=user_id)

    async def node_action(self, user_id, node_key, action, revision, command_key):
        return await self.request('POST', f'/api/v1/nodes/{node_key}/actions',
            telegram_user_id=user_id, body={'action': action, 'revision': revision},
            command=True, command_key=command_key)

    async def node_job(self, user_id, job_id):
        return await self.request('GET', f'/api/v1/node-jobs/{job_id}', telegram_user_id=user_id)

    async def resolve_node_job(self, user_id, job_id):
        return await self.request('POST', f'/api/v1/node-jobs/{job_id}/resolve', telegram_user_id=user_id)

    async def remove_node_step(self, user_id, node_key, retry=False):
        return await self.request('POST', f'/api/v1/nodes/{node_key}/remove-step',
            telegram_user_id=user_id, body={'retry': retry}, timeout_seconds=180)

    async def updates_overview(self, telegram_user_id: int):
        return await self.request('GET', '/api/v1/system/updates', telegram_user_id=telegram_user_id)

    async def update_preferences(self, telegram_user_id: int, changes: dict):
        return await self.request('PATCH', '/api/v1/system/updates/preferences',
                                  telegram_user_id=telegram_user_id, body=changes)

    async def check_updates(self, telegram_user_id: int):
        return await self.request('POST', '/api/v1/system/updates/check', telegram_user_id=telegram_user_id)

    async def run_update(self, telegram_user_id: int, body: dict, command_key: str):
        return await self.request('POST', '/api/v1/system/updates/run', telegram_user_id=telegram_user_id,
                                  command=True, command_key=command_key, body=body)

    async def update_versions(self, user_id: int, offset=0):
        return await self.request('GET', f'/api/v1/system/updates/versions?offset={offset}',
                                  telegram_user_id=user_id, timeout_seconds=90)

    async def update_rollout(self, user_id: int):
        return await self.request('GET', '/api/v1/system/updates/rollout',
                                  telegram_user_id=user_id, timeout_seconds=180)

    async def update_job(self, user_id: int, job_id: str):
        return await self.request('GET', f'/api/v1/system/updates/jobs/{job_id}', telegram_user_id=user_id)

    async def cleanup_overview(self, telegram_user_id: int):
        return await self.request('GET', '/api/v1/system/cleanup', telegram_user_id=telegram_user_id)

    async def backups_overview(self, user_id):
        return await self.request('GET','/api/v1/system/backups',telegram_user_id=user_id)

    async def backup_catalog(self, user_id, offset=0):
        return await self.request('GET',f'/api/v1/system/backups/catalog?offset={offset}',telegram_user_id=user_id)

    async def backup_detail(self, user_id, backup_id):
        return await self.request('GET',f'/api/v1/system/backups/catalog/{backup_id}',telegram_user_id=user_id)

    async def backup_preferences(self, user_id, changes):
        return await self.request('PATCH','/api/v1/system/backups/preferences',telegram_user_id=user_id,body=changes)

    async def backup_command(self,user_id,body,key):
        return await self.request('POST','/api/v1/system/backups/jobs',telegram_user_id=user_id,
                                  body=body,command=True,command_key=key)

    async def backup_job(self,user_id,job_id):
        return await self.request('GET',f'/api/v1/system/backups/jobs/{job_id}',telegram_user_id=user_id)

    async def run_cleanup(self, telegram_user_id: int):
        return await self.request('POST', '/api/v1/system/cleanup/run', telegram_user_id=telegram_user_id, command=True)

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
                    logging.getLogger(__name__).warning('Backend request failed: %s %s status=%s code=%s request_id=%s',
                        method, path.split('?')[0], response.status, error.get('code', 'backend_unavailable'),
                        response.headers.get('X-Request-ID', 'unknown'))
                    raise BackendError(error.get('code', 'backend_unavailable'), response.status)
                return data
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise BackendError('backend_unavailable', 503) from exc

    async def resolve(self, telegram_user_id: int, *, username=None, first_name=None,
                      last_name=None, language_code=None) -> dict:
        return await self.request('POST', '/api/v1/integrations/telegram/identities/resolve',
            body={'telegram_user_id': telegram_user_id, 'username': username,
                  'first_name': first_name, 'last_name': last_name,
                  'language_code': language_code}, command=True)

    async def me(self, telegram_user_id: int) -> dict:
        return await self.request('GET', '/api/v1/me', telegram_user_id=telegram_user_id)

    async def set_announcement_silent(self, telegram_user_id, silent):
        return await self.request('PATCH','/api/v1/me/preferences',telegram_user_id=telegram_user_id,
                                  body={'announcement_silent':silent})

    async def announcement_preview(self, user_id, text):
        return await self.request('POST','/api/v1/announcements/preview',telegram_user_id=user_id,body={'text':text})

    async def announcement_latest(self, user_id):
        return await self.request('GET','/api/v1/announcements',telegram_user_id=user_id)

    async def alerts_overview(self,user_id):
        return await self.request('GET','/api/v1/system/alerts',telegram_user_id=user_id)

    async def alert_preferences(self,user_id,changes):
        return await self.request('PATCH','/api/v1/system/alerts/preferences',telegram_user_id=user_id,body=changes)

    async def alert_claim(self,key):
        return await self.request('POST','/api/v1/integrations/telegram/alerts/claim',command=True,command_key=key)

    async def alert_ack(self,delivery_id,key,status):
        return await self.request('POST',f'/api/v1/integrations/telegram/alerts/{delivery_id}/ack',body={'status':status},command=True,command_key=key)

    async def announcement_create(self, user_id, text, key):
        return await self.request('POST','/api/v1/announcements',telegram_user_id=user_id,body={'text':text},command=True,command_key=key)

    async def announcement_status(self, user_id, announcement_id):
        return await self.request('GET',f'/api/v1/announcements/{announcement_id}',telegram_user_id=user_id)

    async def announcement_claim(self, key):
        return await self.request('POST','/api/v1/integrations/telegram/announcements/claim',command=True,command_key=key)

    async def announcement_ack(self, delivery_id, key, status):
        return await self.request('POST',f'/api/v1/integrations/telegram/announcements/{delivery_id}/ack',
                                  body={'status':status},command=True,command_key=key)

    async def set_locale(self, telegram_user_id: int, locale: str) -> dict:
        return await self.request('PATCH', '/api/v1/me/preferences', telegram_user_id=telegram_user_id,
                                  body={'locale': locale})

    async def profiles(self, telegram_user_id: int) -> dict:
        return await self.request('GET', '/api/v1/me/profiles?limit=100', telegram_user_id=telegram_user_id)

    async def member_profile_summary(self, telegram_user_id: int, profile_id: str) -> dict:
        return await self.request('GET', f'/api/v1/me/profiles/{profile_id}/summary',
                                  telegram_user_id=telegram_user_id)

    async def nodes(self, telegram_user_id: int) -> dict:
        return await self.request('GET', '/api/v1/me/nodes?limit=100', telegram_user_id=telegram_user_id)

    async def profile_nodes(self, telegram_user_id: int, profile_id: str) -> dict:
        path = f'/api/v1/profiles/{profile_id}/nodes?limit=100'
        page = await self.request('GET', path, telegram_user_id=telegram_user_id)
        items = list(page['items'])
        seen = set()
        while cursor := page.get('next_cursor'):
            if cursor in seen:
                raise BackendError('invalid_node_pagination', 502)
            seen.add(cursor)
            page = await self.request('GET', path + '&cursor=' + quote(cursor, safe=''),
                                      telegram_user_id=telegram_user_id)
            items.extend(page['items'])
        return {'items': items, 'next_cursor': None}

    async def request_access(self, telegram_user_id: int) -> dict:
        return await self.request('POST', '/api/v1/me/access-requests',
                                  telegram_user_id=telegram_user_id, command=True)

    async def access_request_policy(self, telegram_user_id: int) -> dict:
        return await self.request('GET', '/api/v1/system/access-requests',
                                  telegram_user_id=telegram_user_id)

    async def update_access_request_policy(self, telegram_user_id: int, changes: dict) -> dict:
        return await self.request('PATCH', '/api/v1/system/access-requests',
                                  telegram_user_id=telegram_user_id, body=changes)

    async def bot_title(self, telegram_user_id: int) -> dict:
        return await self.request('GET', '/api/v1/system/bot-title',
                                  telegram_user_id=telegram_user_id)

    async def update_bot_title(self, telegram_user_id: int, title: str) -> dict:
        return await self.request('PATCH', '/api/v1/system/bot-title',
                                  telegram_user_id=telegram_user_id,
                                  body={'title': title})

    async def system_version(self, telegram_user_id: int) -> dict:
        return await self.request('GET', '/api/v1/system/version',
                                  telegram_user_id=telegram_user_id)

    async def pending_access_requests(self, telegram_user_id: int, *,
                                       cursor: str | None = None,
                                       search: str | None = None,
                                       limit: int = 10) -> dict:
        params = {'limit': limit}
        if cursor:
            params['cursor'] = cursor
        if search:
            params['search'] = search
        return await self.request('GET', '/api/v1/access-requests?' + urlencode(params),
                                  telegram_user_id=telegram_user_id)

    async def pending_access_request(self, telegram_user_id: int,
                                     request_id: str) -> dict:
        return await self.request('GET', f'/api/v1/access-requests/{request_id}',
                                  telegram_user_id=telegram_user_id)

    async def issue(self, telegram_user_id: int, profile_id: str, node_key: str,
                    protocol: str, transport: str) -> dict:
        return await self.request('POST', f'/api/v1/profiles/{profile_id}/config-issuances?wait=true',
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

    async def admin_profiles(self, telegram_user_id: int, *, cursor: str | None = None,
                             search: str | None = None, limit: int = 10) -> dict:
        from urllib.parse import urlencode
        query = {'limit': limit}
        if cursor:
            query['cursor'] = cursor
        if search:
            query['search'] = search
        return await self.request('GET', '/api/v1/profiles?' + urlencode(query),
                                  telegram_user_id=telegram_user_id)

    async def admin_nodes(self, telegram_user_id: int, *, cursor: str | None = None,
                          search: str | None = None, limit: int = 100) -> dict:
        params = {'limit': limit, 'include_summary': 'true'}
        if cursor:
            params['cursor'] = cursor
        if search:
            params['search'] = search
        return await self.request('GET', '/api/v1/nodes?' + urlencode(params),
                                  telegram_user_id=telegram_user_id)

    async def profile_grants(self, telegram_user_id: int, profile_id: str) -> dict:
        return await self.request('GET', f'/api/v1/profiles/{profile_id}/grants',
                                  telegram_user_id=telegram_user_id)

    async def profile_operation(self, telegram_user_id: int, profile_id: str) -> dict | None:
        return await self.request('GET', f'/api/v1/profiles/{profile_id}/operation',
                                  telegram_user_id=telegram_user_id)

    async def admin_overview(self, telegram_user_id: int) -> dict:
        return await self.request('GET', '/api/v1/admin/overview',
                                  telegram_user_id=telegram_user_id)

    async def create_profile(self, telegram_user_id: int, account_id: str, name: str,
                             command_key: str, grants: list[dict] | None = None) -> dict:
        return await self.request('POST', '/api/v1/profiles', telegram_user_id=telegram_user_id,
                                  command=True, command_key=command_key,
                                  body={'display_name': name, 'owner_account_id': account_id,
                                        'grants': grants or []})

    async def edit_profile(self, telegram_user_id: int, profile_id: str,
                           revision: int, changes: dict, *, command_key: str | None = None) -> dict:
        return await self.request('PATCH', f'/api/v1/profiles/{profile_id}',
            telegram_user_id=telegram_user_id, command=True, command_key=command_key,
            revision=revision, body=changes)

    async def replace_grants(self, telegram_user_id: int, profile_id: str,
                             revision: int, grants: list[dict]) -> dict:
        return await self.request('PATCH', f'/api/v1/profiles/{profile_id}/grants',
            telegram_user_id=telegram_user_id, command=True, revision=revision,
            body={'grants': grants})

    async def delete_profile(self, telegram_user_id: int, profile_id: str,
                             revision: int, command_key: str) -> dict:
        return await self.request('DELETE', f'/api/v1/profiles/{profile_id}',
            telegram_user_id=telegram_user_id, command=True,
            command_key=command_key, revision=revision)

    async def node_runtime(self, telegram_user_id: int, node_key: str) -> dict:
        return await self.request('GET', f'/api/v1/nodes/{node_key}/runtime',
                                  telegram_user_id=telegram_user_id)

    async def node_diagnostics(self, telegram_user_id: int, node_key: str) -> dict:
        return await self.request('GET', f'/api/v1/nodes/{node_key}/diagnostics',
                                  telegram_user_id=telegram_user_id)

    async def node_overview(self, telegram_user_id: int, node_key: str) -> dict:
        return await self.request('GET', f'/api/v1/nodes/{node_key}/overview',
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
                            command_key: str, install_rust: bool = False) -> dict:
        return await self.request('POST', f'/api/v1/nodes/{node_key}/agent-rollouts',
            telegram_user_id=telegram_user_id, command=True, command_key=command_key,
            body={'transport': transport, 'ssh_target': ssh_target, 'install_rust': install_rust})

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

    async def traffic_policy(self, telegram_user_id):
        return await self.request('GET', '/api/v1/system/traffic', telegram_user_id=telegram_user_id)

    async def system_cleanup_overview(self, telegram_user_id):
        return await self.request('GET', '/api/v1/system/cleanup', telegram_user_id=telegram_user_id)

    async def system_cleanup_plan(self, telegram_user_id, action, cleanup_nodes):
        return await self.request('POST', '/api/v1/system/cleanup/plans',
                                  telegram_user_id=telegram_user_id,
                                  body={'action': action, 'cleanup_nodes': cleanup_nodes})

    async def system_cleanup_command(self, telegram_user_id, plan_id, phrase, command_key):
        return await self.request('POST', '/api/v1/system/cleanup/jobs',
                                  telegram_user_id=telegram_user_id, command=True,
                                  command_key=command_key,
                                  body={'plan_id': plan_id, 'confirmation_phrase': phrase})

    async def system_cleanup_job(self, telegram_user_id, job_id):
        return await self.request('GET', f'/api/v1/system/cleanup/jobs/{job_id}', telegram_user_id=telegram_user_id)

    async def system_cleanup_action(self, telegram_user_id, job_id, action):
        if action not in {'retry','abort','shutdown-ack'}:
            raise ValueError('unsupported cleanup action')
        return await self.request('POST', f'/api/v1/system/cleanup/jobs/{job_id}/{action}', telegram_user_id=telegram_user_id)

    async def update_traffic_policy(self, telegram_user_id, enabled):
        return await self.request('PATCH', '/api/v1/system/traffic/preferences',
                                  telegram_user_id=telegram_user_id, body={'enabled': enabled})

    async def set_traffic_consent(self, telegram_user_id, consent):
        return await self.request('PATCH', '/api/v1/me/preferences',
                                  telegram_user_id=telegram_user_id, body={'traffic_consent': consent})
