"""Local trusted provisioning, unavailable through the public API.

Run with PYTHONPATH=app python -m backend.admin_cli ...
"""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path

from .authorization import AccessDenied, Actor, Principal, PrincipalKind, validate_telegram_id
from .credentials import ADAPTER_SCOPES, PERMISSIONS, CredentialService
from .identity_repository import SQLIdentityRepository
from .profiles import ProfileRepository
from .profile_commands import ProfileCommands
from .node_lifecycle import NodeLifecycle
from .access_requests import AccessRequestService
from .accounts import AccountService
from .nodes import NodeService
from .node_settings import NodeSettingsService


def bootstrap_admin(repository: SQLIdentityRepository, telegram_user_id: int):
    validate_telegram_id(telegram_user_id)
    account = repository.resolve_telegram(telegram_user_id)
    with repository.db.transaction() as conn:
        conn.execute('UPDATE backend_account_guard SET revision = revision + 1 WHERE id = 1')
        conn.execute("""UPDATE backend_accounts SET role = 'admin', status = 'approved',
            revision = revision + 1 WHERE id = ? AND (role != 'admin' OR status != 'approved')""", (account.id,))
    return repository.get_account(account.id)


def write_secret(path: Path, secret: str):
    # Exclusive creation prevents replacement/following an existing symlink.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(secret + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description='Local backend identity/credential administration')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init-schema')
    bootstrap = commands.add_parser('bootstrap-admin')
    bootstrap.add_argument('--telegram-id', type=int, required=True)
    issue = commands.add_parser('issue-token')
    issue.add_argument('--kind', choices=['account', 'adapter'], required=True)
    issue.add_argument('--account-id')
    issue.add_argument('--output', type=Path, required=True)
    issue.add_argument('--scope', action='append', help='Explicit allowed scope; repeat as needed')
    profile = commands.add_parser('create-profile')
    profile.add_argument('--runtime-name', required=True)
    profile.add_argument('--display-name', required=True)
    profile.add_argument('--owner-account-id')
    revoke = commands.add_parser('revoke-token')
    revoke.add_argument('--id', required=True)
    repair = commands.add_parser('resolve-blocked')
    repair.add_argument('--task-id', required=True)
    repair.add_argument('--admin-account-id', required=True)
    repair.add_argument('--lock-file', type=Path, required=True)
    repair.add_argument('--driver', default='127.0.0.1:50051')
    repair_settings = commands.add_parser('resolve-blocked-node-settings',
        help='After restarting the agent, retire an uncertain settings command and queue a fresh revision')
    repair_settings.add_argument('--task-id', required=True)
    repair_settings.add_argument('--admin-account-id', required=True)
    repair_settings.add_argument('--lock-file', type=Path, required=True)
    repair_settings.add_argument('--driver', default='127.0.0.1:50051')
    drain = commands.add_parser('drain-node', help='Disable grants and queue revocation; does not remove runtime or agent')
    drain.add_argument('--node-key', required=True)
    drain.add_argument('--admin-account-id', required=True)
    drain.add_argument('--lock-file', type=Path, required=True)
    drain_status = commands.add_parser('node-drain-status', help='Inspect revocation progress; no remote cleanup')
    drain_status.add_argument('--node-key', required=True)
    drain_status.add_argument('--admin-account-id', required=True)
    cleanup = commands.add_parser('cleanup-node-step', help='Advance one backend-owned node cleanup phase')
    cleanup.add_argument('--node-key', required=True)
    cleanup.add_argument('--admin-account-id', required=True)
    cleanup.add_argument('--lock-file', type=Path, required=True)
    cleanup.add_argument('--driver', default='127.0.0.1:50051')
    registry_only = commands.add_parser('remove-node-registry-only', help='Abandon unverified VPS state and remove backend node record')
    registry_only.add_argument('--node-key', required=True)
    registry_only.add_argument('--admin-account-id', required=True)
    registry_only.add_argument('--lock-file', type=Path, required=True)
    registry_only.add_argument('--reason', required=True)
    registry_only.add_argument('--accept-unverified-runtime', action='store_true', required=True)
    verified = commands.add_parser('verify-and-remove-node', help='Inspect host independently, then remove backend node record')
    verified.add_argument('--node-key', required=True)
    verified.add_argument('--admin-account-id', required=True)
    verified.add_argument('--lock-file', type=Path, required=True)
    verified.add_argument('--bot-public-key-file', type=Path, required=True)
    verification_target = verified.add_mutually_exclusive_group(required=True)
    verification_target.add_argument('--local', action='store_true')
    verification_target.add_argument('--ssh-target')
    verified.add_argument('--ssh-identity-file', type=Path)
    verified.add_argument('--ssh-port', type=int, default=22)
    bind = commands.add_parser('bind-node-verification-target', help='Bind local or SSH host before draining a node')
    bind.add_argument('--node-key', required=True)
    bind.add_argument('--admin-account-id', required=True)
    bind.add_argument('--lock-file', type=Path, required=True)
    bind_target = bind.add_mutually_exclusive_group(required=True)
    bind_target.add_argument('--local', action='store_true')
    bind_target.add_argument('--ssh-target')
    bind.add_argument('--ssh-identity-file', type=Path)
    bind.add_argument('--ssh-port', type=int, default=22)
    args = parser.parse_args()
    from db import get_db
    db = get_db()
    identities = SQLIdentityRepository(db)
    credentials = CredentialService(db)
    try:
        if args.command == 'init-schema':
            identities.initialize_schema()
            credentials.initialize_schema()
            ProfileRepository(db).initialize_schema()
            ProfileCommands(db).initialize_schema()
            AccessRequestService(db).initialize_schema()
            AccountService(db).initialize_schema()
            NodeService(db).initialize_schema()
            NodeSettingsService(db).initialize_schema()
            print('Backend identity schema initialized.')
        elif args.command == 'bootstrap-admin':
            account = bootstrap_admin(identities, args.telegram_id)
            print(f'Administrator account: {account.id}')
        elif args.command == 'issue-token':
            kind = PrincipalKind(args.kind)
            scopes = frozenset(args.scope or (ADAPTER_SCOPES if kind == PrincipalKind.ADAPTER else PERMISSIONS))
            token_id, token = credentials.issue(kind, scopes, account_id=args.account_id)
            try:
                write_secret(args.output, token)
            except OSError:
                credentials.revoke(token_id)
                raise
            print(f'Credential ID: {token_id}; secret saved to {args.output}')
        elif args.command == 'create-profile':
            profile_id = ProfileRepository(db).create_profile(runtime_name=args.runtime_name,
                display_name=args.display_name, owner_account_id=args.owner_account_id)
            print(f'Profile ID: {profile_id}')
        elif args.command == 'revoke-token':
            if not credentials.revoke(args.id):
                parser.error('credential ID not found')
            print('Credential revoked.')
        elif args.command == 'resolve-blocked':
            from uuid import UUID
            task_id = str(UUID(args.task_id))
            account = identities.get_account(str(UUID(args.admin_account_id)))
            if account is None or account.role != 'admin' or account.status != 'approved':
                parser.error('an approved backend administrator account is required')
            actor = Actor(Principal('local-repair', PrincipalKind.ACCOUNT,
                                    frozenset({'maintenance.manage'}), account.id), account)
            from .driver_transport import GrpcIntentDriver, local_channel
            from .executor import IntentExecutor
            fd = os.open(args.lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with local_channel(args.driver) as channel:
                    outcome = IntentExecutor(db, GrpcIntentDriver(channel)).resolve_blocked(actor, task_id)
            print(f"Repair queued as operation {outcome['operation_id']}; inspect and run backend.executor to apply the current desired state.")
        elif args.command == 'resolve-blocked-node-settings':
            from uuid import UUID
            from .node_settings import NodeSettingsExecutor
            from .driver_transport import GrpcIntentDriver, local_channel
            task_id = str(UUID(args.task_id))
            account = identities.get_account(str(UUID(args.admin_account_id)))
            if account is None or account.role != 'admin' or account.status != 'approved':
                parser.error('an approved backend administrator account is required')
            actor = Actor(Principal('local-settings-repair', PrincipalKind.ACCOUNT,
                                    frozenset({'maintenance.manage'}), account.id), account)
            fd = os.open(args.lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with local_channel(args.driver) as channel:
                    outcome = NodeSettingsExecutor(db, GrpcIntentDriver(channel)).resolve_blocked(actor, task_id)
            print(f"Interrupted settings task retired after live inspection; fresh revision {outcome['revision']} queued as {outcome['task_id']}. Run backend.executor with the same lock file.")
        elif args.command == 'drain-node':
            from uuid import UUID
            account = identities.get_account(str(UUID(args.admin_account_id)))
            if account is None or account.role != 'admin' or account.status != 'approved':
                parser.error('an approved backend administrator account is required')
            actor = Actor(Principal('local-drain', PrincipalKind.ACCOUNT,
                                    frozenset({'maintenance.manage'}), account.id), account)
            fd = os.open(args.lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                outcome = NodeLifecycle(db).start_drain(actor, args.node_key)
            print(f"Node {outcome['node_key']} is draining; {len(outcome['operation_ids'])} revocation operations queued. Run backend.executor with the same lock file. Runtime and agent remain installed pending verified cleanup.")
        elif args.command == 'node-drain-status':
            from uuid import UUID
            account = identities.get_account(str(UUID(args.admin_account_id)))
            if account is None or account.role != 'admin' or account.status != 'approved':
                parser.error('an approved backend administrator account is required')
            actor = Actor(Principal('local-drain-status', PrincipalKind.ACCOUNT,
                                    frozenset({'maintenance.manage'}), account.id), account)
            outcome = NodeLifecycle(db).drain_status(actor, args.node_key)
            print(f"Node {args.node_key}: {outcome['pending_tasks']} pending, {outcome['blocked_tasks']} blocked; revocations complete: {outcome['revocations_complete']}. Runtime and agent cleanup have not run.")
        elif args.command == 'cleanup-node-step':
            from uuid import UUID
            account = identities.get_account(str(UUID(args.admin_account_id)))
            if account is None or account.role != 'admin' or account.status != 'approved':
                parser.error('an approved backend administrator account is required')
            actor = Actor(Principal('local-cleanup', PrincipalKind.ACCOUNT,
                                    frozenset({'maintenance.manage'}), account.id), account)
            from .driver_transport import GrpcIntentDriver, local_channel
            fd = os.open(args.lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with local_channel(args.driver) as channel:
                    outcome = NodeLifecycle(db).cleanup(actor, args.node_key, GrpcIntentDriver(channel))
            print(f"Node {args.node_key}: cleanup phase {outcome['phase']}. The registry is retained pending independent verification of agent removal.")
        elif args.command == 'remove-node-registry-only':
            from uuid import UUID
            account = identities.get_account(str(UUID(args.admin_account_id)))
            if account is None or account.role != 'admin' or account.status != 'approved':
                parser.error('an approved backend administrator account is required')
            actor = Actor(Principal('local-registry-removal', PrincipalKind.ACCOUNT,
                                    frozenset({'maintenance.manage'}), account.id), account)
            fd = os.open(args.lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                outcome = NodeLifecycle(db).retire_registry_only(actor, args.node_key, args.reason)
            print(f"Node {args.node_key} removed from backend registry; {outcome['unfinished_tasks']} unfinished tasks retired. VPS runtime state was not verified.")
        elif args.command == 'verify-and-remove-node':
            from uuid import UUID
            account = identities.get_account(str(UUID(args.admin_account_id)))
            if account is None or account.role != 'admin' or account.status != 'approved':
                parser.error('an approved backend administrator account is required')
            actor = Actor(Principal('local-verified-removal', PrincipalKind.ACCOUNT,
                                    frozenset({'maintenance.manage'}), account.id), account)
            from .removal_verifier import RemovalVerifier
            verifier = RemovalVerifier(local=args.local, ssh_target=args.ssh_target,
                ssh_identity_file=args.ssh_identity_file, ssh_port=args.ssh_port,
                bot_public_key=args.bot_public_key_file.read_text().strip())
            fd = os.open(args.lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                outcome = NodeLifecycle(db).retire_verified(actor, args.node_key, verifier)
            print(f"Node {outcome['node_key']} verified absent and removed from backend registry.")
        elif args.command == 'bind-node-verification-target':
            from uuid import UUID
            account = identities.get_account(str(UUID(args.admin_account_id)))
            if account is None or account.role != 'admin' or account.status != 'approved':
                parser.error('an approved backend administrator account is required')
            actor = Actor(Principal('local-verification-binding', PrincipalKind.ACCOUNT,
                                    frozenset({'maintenance.manage'}), account.id), account)
            from .removal_verifier import RemovalVerifier
            verifier = RemovalVerifier(local=args.local, ssh_target=args.ssh_target,
                ssh_identity_file=args.ssh_identity_file, ssh_port=args.ssh_port)
            fd = os.open(args.lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                outcome = NodeLifecycle(db).bind_verification_target(actor, args.node_key, verifier)
            print(f"Node {outcome['node_key']} verification target bound: {outcome['target']} (active agent identity checked).")
    except (OSError, ValueError, AccessDenied) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
