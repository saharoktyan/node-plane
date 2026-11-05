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
from .node_lifecycle import NodeLifecycle


def named_admin_account(repository: SQLIdentityRepository, name: str):
    """An administrator known only by name, for installations without Telegram.

    Running it again with the same name returns the same account.
    """
    if not isinstance(name, str) or not 0 < len(name.strip()) <= 128 or any(c < ' ' for c in name):
        raise AccessDenied('invalid_input', 422)
    name = name.strip()
    with repository.db.connect() as conn:
        row = conn.execute("""SELECT id FROM backend_accounts WHERE role = 'admin' AND display_name = ?
            ORDER BY id LIMIT 1""", (name,)).fetchone()
    if row is not None:
        return repository.get_account(row['id'])
    account = repository.create_account(status='approved')
    with repository.db.transaction() as conn:
        conn.execute('UPDATE backend_accounts SET display_name = ? WHERE id = ?', (name, account.id))
    return account


def bootstrap_admin(repository: SQLIdentityRepository, telegram_user_id: int | None = None, db=None, *,
                    name: str | None = None):
    """Make the first administrator: by Telegram ID, or by name with no Telegram at all."""
    if (telegram_user_id is None) == (name is None):
        raise AccessDenied('invalid_input', 422)
    if name is not None:
        account = named_admin_account(repository, name)
    else:
        validate_telegram_id(telegram_user_id)
        account = repository.resolve_telegram(telegram_user_id)
    with repository.db.transaction() as conn:
        conn.execute('UPDATE backend_account_guard SET revision = revision + 1 WHERE id = 1')
        conn.execute("""UPDATE backend_accounts SET role = 'admin', status = 'approved',
            revision = revision + 1 WHERE id = ? AND (role != 'admin' OR status != 'approved')""", (account.id,))
    
    # The CLI supplies the fully initialized application database. Callers
    # bootstrapping only the identity schema may omit it.
    if db is not None:
        pref = ProfileRepository(db)
        pref.ensure_account_profile(account.id)
        
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
    commands.add_parser('init-schema', help='Apply pending versioned database migrations')
    commands.add_parser('schema-status', help='Read database migration history and compatibility')
    bootstrap = commands.add_parser('bootstrap-admin',
        help='Make an administrator by Telegram ID, or by name without Telegram')
    admin_identity = bootstrap.add_mutually_exclusive_group(required=True)
    admin_identity.add_argument('--telegram-id', type=int)
    admin_identity.add_argument('--name', help='Administrator name; Workstation signs in to this account')
    create_account = commands.add_parser('create-account',
        help='Create a member account without Telegram identity')
    link_telegram = commands.add_parser('link-telegram',
        help='Attach a Telegram login to an existing backend account')
    link_telegram.add_argument('--account-id', required=True)
    link_telegram.add_argument('--telegram-id', type=int, required=True)
    issue = commands.add_parser('issue-token')
    issue.add_argument('--kind', choices=['account', 'adapter'], required=True)
    issue.add_argument('--account-id')
    issue.add_argument('--output', type=Path, required=True)
    issue.add_argument('--scope', action='append', help='Explicit allowed scope; repeat as needed')
    profile = commands.add_parser('create-profile')
    profile.add_argument('--runtime-name', required=True)
    profile.add_argument('--display-name', required=True)
    profile.add_argument('--owner-account-id')
    channels = commands.add_parser('channels', help='Show or change the channels this installation runs')
    channels.add_argument('--telegram', choices=['on', 'off'])
    bot = commands.add_parser('telegram-bot',
        help='Turn the Telegram bot on (token read from a private file) or off; accounts stay')
    bot_action = bot.add_mutually_exclusive_group(required=True)
    bot_action.add_argument('--enable-with-token-file', type=Path)
    bot_action.add_argument('--disable', action='store_true')
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
    verified = commands.add_parser('verify-and-remove-node', help='Check the host (this one, or by its agent), then remove backend node record')
    verified.add_argument('--node-key', required=True)
    verified.add_argument('--admin-account-id', required=True)
    verified.add_argument('--lock-file', type=Path, required=True)
    verified.add_argument('--driver', default='127.0.0.1:50051')
    bind = commands.add_parser('bind-node-verification-target', help='Bind the local host or the remote agent before draining a node')
    bind.add_argument('--node-key', required=True)
    bind.add_argument('--admin-account-id', required=True)
    bind.add_argument('--lock-file', type=Path, required=True)
    bind.add_argument('--driver', default='127.0.0.1:50051')
    bind_target = bind.add_mutually_exclusive_group(required=True)
    bind_target.add_argument('--local', action='store_true')
    bind_target.add_argument('--agent', action='store_true', help='a remote node, checked by its agent')
    args = parser.parse_args()
    from db import get_db
    from db.migrations import MigrationError
    db = get_db()
    identities = SQLIdentityRepository(db)
    credentials = CredentialService(db)
    try:
        if args.command not in {'init-schema', 'schema-status'}:
            from db.migrations import check_schema
            check_schema(db)
        if args.command == 'init-schema':
            from db.migrations import migrate
            result = migrate(db)
            print(f"Database revision: {result['current_revision']}; applied: {result['applied']}")
        elif args.command == 'schema-status':
            from db.migrations import schema_status
            import json
            print(json.dumps(schema_status(db)))
        elif args.command == 'bootstrap-admin':
            account = bootstrap_admin(identities, args.telegram_id, db, name=args.name)
            print(f'Administrator account: {account.id}')
        elif args.command == 'channels':
            import json
            from .account_channels import enabled, set_enabled
            with db.transaction() as conn:
                current = enabled(conn)
                if args.telegram is not None:
                    current = set_enabled(conn, current | {'telegram'} if args.telegram == 'on'
                                          else current - {'telegram'})
            print(json.dumps({'channels': sorted(current)}))
        elif args.command == 'telegram-bot':
            import config
            from . import telegram_bot_setup
            from .telegram_configuration import _env, _write, verify_token
            path = Path(config.SHARED_ROOT) / '.env'
            system = telegram_bot_setup.SystemdBot(config.INSTALL_ROOT, config.SHARED_ROOT)
            if args.disable:
                done = telegram_bot_setup.disable(db, path, read=_env, write=_write, system=system)
                print('Telegram bot is off' if done else 'Telegram bot is off; its service could not be removed')
            else:
                token = args.enable_with_token_file.read_text().strip()
                verify_token(token)
                done = telegram_bot_setup.enable(db, path, token, read=_env, write=_write, system=system)
                print('Telegram bot is on' if done else 'Telegram bot is configured; its service did not start')
            if not done:
                raise SystemExit(1)
        elif args.command == 'create-account':
            account = identities.create_account()
            ProfileRepository(db).ensure_account_profile(account.id)
            print(f'Account ID: {account.id}')
        elif args.command == 'link-telegram':
            from uuid import UUID
            account = identities.link_telegram(str(UUID(args.account_id)), args.telegram_id)
            print(f'Telegram identity linked to account {account.id}')
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
            print(f"Node {args.node_key}: cleanup phase {outcome['phase']}. The registry is retained until the host check passes.")
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
            from .driver_transport import GrpcIntentDriver, local_channel
            from .node_removal import NodeRemovalService
            with db.connect() as conn:
                bound = conn.execute('SELECT target FROM backend_node_verification_targets WHERE node_key = ?',
                                     (args.node_key,)).fetchone()
            if bound is None:
                parser.error('the node has no bound verification target')
            fd = os.open(args.lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with local_channel(args.driver) as channel:
                    verifier = NodeRemovalService(db, GrpcIntentDriver(channel)).verifier(bound['target'])
                    outcome = NodeLifecycle(db).retire_verified(actor, args.node_key, verifier)
            if bound['target'] == 'local':
                print(f"Node {outcome['node_key']} verified absent and removed from backend registry.")
            else:
                print(f"Node {outcome['node_key']} removed from backend registry: its agent checked the host "
                      "and stopped answering. The agent's own removal was not verified.")
        elif args.command == 'bind-node-verification-target':
            from uuid import UUID
            account = identities.get_account(str(UUID(args.admin_account_id)))
            if account is None or account.role != 'admin' or account.status != 'approved':
                parser.error('an approved backend administrator account is required')
            actor = Actor(Principal('local-verification-binding', PrincipalKind.ACCOUNT,
                                    frozenset({'maintenance.manage'}), account.id), account)
            from .driver_transport import GrpcIntentDriver, local_channel
            from .node_removal import NodeRemovalService
            fd = os.open(args.lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with local_channel(args.driver) as channel:
                    verifier = NodeRemovalService(db, GrpcIntentDriver(channel)).verifier(
                        'local' if args.local else 'agent')
                    outcome = NodeLifecycle(db).bind_verification_target(actor, args.node_key, verifier)
            print(f"Node {outcome['node_key']} verification target bound: {outcome['target']} (active agent identity checked).")
    except (OSError, ValueError, AccessDenied, MigrationError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
