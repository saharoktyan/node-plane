"""Authorized, idempotent device mutations sharing profile/node fences."""
import hashlib
import json
import unicodedata
from datetime import datetime,timezone
from uuid import UUID,uuid4

from .authorization import AccessDenied,Account,Actor,require_permission
from .devices import DeviceRepository
from .operations import OperationRepository


def device_name(value):
    if not isinstance(value,str) or any(unicodedata.category(c).startswith('C') for c in value):
        raise AccessDenied('invalid_device_name',422)
    display = unicodedata.normalize('NFC',' '.join(value.split()))
    if not display or len(display)>64:
        raise AccessDenied('invalid_device_name',422)
    return display,unicodedata.normalize('NFKC',display).casefold()


class DeviceCommands:
    def __init__(self,db):
        self.db=db

    def execute(self,actor,key,*,action,profile_id,device_id=None,revision=None,display_name=None):
        try:
            key=str(UUID(key))
        except (ValueError,TypeError,AttributeError):
            raise AccessDenied('invalid_idempotency_key',422) from None
        if action not in {'create','rename','delete'}:
            raise AccessDenied('invalid_input',422)
        if action != 'create' and not device_id:
            raise AccessDenied('invalid_input',422)
        name,name_key = device_name(display_name) if action!='delete' else (None,None)
        fingerprint=hashlib.sha256(json.dumps(dict(action=action,profile_id=profile_id,
            device_id=device_id,revision=revision,display_name=display_name),sort_keys=True).encode()).hexdigest()
        identity=(actor.principal.id,actor.account.id,key)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            account=conn.execute('SELECT role,status FROM backend_accounts WHERE id=?',(actor.account.id,)).fetchone()
            if account is None:
                raise AccessDenied('permission_denied')
            current=Actor(actor.principal,Account(actor.account.id,account['role'],account['status']))
            profile=conn.execute('''UPDATE backend_profiles SET desired_revision=desired_revision
                WHERE id=? RETURNING *''',(profile_id,)).fetchone()
            if profile is None or (profile['owner_account_id']!=current.account.id and current.account.role!='admin'):
                raise AccessDenied('resource_not_found',404)
            require_permission(current,'configs.self.issue' if profile['owner_account_id']==current.account.id else 'profiles.manage')
            if conn.execute('SELECT 1 FROM backend_profile_deletions WHERE profile_id=?',(profile_id,)).fetchone():
                raise AccessDenied('profile_deleting',409)
            conn.execute('''INSERT INTO backend_device_commands(principal_id,actor_id,command_key,fingerprint)
                VALUES (?,?,?,?) ON CONFLICT(principal_id,actor_id,command_key) DO NOTHING''',(*identity,fingerprint))
            previous=conn.execute('''SELECT fingerprint,result_json FROM backend_device_commands
                WHERE principal_id=? AND actor_id=? AND command_key=?''',identity).fetchone()
            if previous['fingerprint']!=fingerprint:
                raise AccessDenied('idempotency_conflict',409)
            if previous['result_json']:
                return json.loads(previous['result_json'])
            if revision is None:
                raise AccessDenied('revision_required',428)
            if type(revision) is not int or revision<1:
                raise AccessDenied('invalid_revision',422)
            device=None
            if action=='create':
                if device_id:
                    raise AccessDenied('invalid_input',422)
                if profile['frozen']:
                    raise AccessDenied('profile_frozen')
                if profile['expires_at'] and datetime.fromisoformat(profile['expires_at'])<=datetime.now(timezone.utc):
                    raise AccessDenied('profile_expired')
                expected=profile['desired_revision']
            else:
                device=DeviceRepository.select_active(conn,profile_id,device_id)
                expected=device['revision']
            if expected!=revision:
                raise AccessDenied('revision_conflict',412)
            if name_key is not None:
                siblings=conn.execute("SELECT id,display_name FROM backend_devices WHERE profile_id=? AND status!='retired'",(profile_id,)).fetchall()
                if any(row['id']!=device_id and device_name(row['display_name'])[1]==name_key for row in siblings):
                    raise AccessDenied('device_name_conflict',409)
            operation=None
            if action=='create':
                device_id=str(uuid4())
                runtime_name='d_'+uuid4().hex
                while conn.execute('SELECT 1 FROM backend_profiles WHERE runtime_name=?',(runtime_name,)).fetchone():
                    runtime_name='d_'+uuid4().hex
                conn.execute('''INSERT INTO backend_devices(id,profile_id,display_name,runtime_name,status,created_at)
                    VALUES (?,?,?,?,'active',?)''',(device_id,profile_id,name,runtime_name,datetime.now(timezone.utc).isoformat()))
            elif action=='rename':
                conn.execute('UPDATE backend_devices SET display_name=?,revision=revision+1 WHERE id=?',(name,device_id))
            else:
                conn.execute("UPDATE backend_devices SET status='deleting',revision=revision+1 WHERE id=?",(device_id,))
            if action!='rename':
                conn.execute('UPDATE backend_profiles SET desired_revision=desired_revision+1 WHERE id=?',(profile_id,))
                operation=OperationRepository.record(conn,current,profile_id,OperationRepository.targets(conn,profile_id))
                DeviceRepository.settle_deletions(conn)
            row=conn.execute('SELECT * FROM backend_devices WHERE id=?',(device_id,)).fetchone()
            result={'device':DeviceRepository.public(row),
                'profile_revision':conn.execute('SELECT desired_revision FROM backend_profiles WHERE id=?',(profile_id,)).fetchone()['desired_revision'],
                'operation_id':operation['id'] if operation else None,
                'runtime_status':operation['status'] if operation else 'metadata_only'}
            conn.execute('''UPDATE backend_device_commands SET result_json=?
                WHERE principal_id=? AND actor_id=? AND command_key=?''',(json.dumps(result),*identity))
            return result
