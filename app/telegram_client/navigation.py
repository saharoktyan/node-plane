"""Structural screen parents. Only read-only navigation callbacks belong here."""
from .i18n import tr
from .screens import BACK_LABELS, Breadcrumb


def parent_path(callback, locale, data):
    admin = (Breadcrumb(tr(locale, 'navigation.admin'), 'admin_menu'),)
    settings = (*admin, Breadcrumb(tr(locale, 'settings.admin.title'), 'admin_settings'))
    servers = (*admin, Breadcrumb(tr(locale, 'nodes.admin.title'), 'admin_nodes'))
    profiles = (*admin, Breadcrumb(tr(locale, 'profiles.admin.title'), 'admin_profiles'))
    if callback.startswith('u:'):
        # Member callbacks are opaque and owner-bound. Reuse that same router.
        from .routers.user import actions, button
        action = actions.get(callback[2:])
        if action is None:
            return ()
        def member(label, name, *args):
            return Breadcrumb(label, button(action.owner_id, label, name, *args).callback_data)
        menu = (member(tr(locale, 'navigation.menu'), 'home'),)
        name, args = action.name, action.args
        if name == 'home':
            return menu
        if name == 'admin_menu':
            return admin
        if name in {'profiles', 'profile'}:
            return (*menu, Breadcrumb(tr(locale, 'home.get_config'), callback))
        if name in {'account_info', 'account_profile'}:
            return (*menu, Breadcrumb(tr(locale, 'home.account'), callback))
        if name == 'member_settings':
            return (*menu, Breadcrumb(tr(locale, 'home.settings'), callback))
        if name in {'node', 'protocol', 'device_picker', 'device_picker_back'} and len(args) >= 2:
            profile, key = args[:2]
            path = (*menu, member(tr(locale, 'home.get_config'), 'profile', profile),
                    member(data.get('_navigation_nodes', {}).get(key, key), 'node', profile, key))
            if name == 'node':
                return (*path[:-1], Breadcrumb(path[-1].label, callback))
            if name in {'device_picker', 'device_picker_back'}:
                return (*path, member(tr(locale, 'protocol.awg'), 'protocol', profile, key, 'awg'),
                        member(tr(locale, 'devices.title'), 'device_picker', profile, key))
            if len(args) == 3 and args[2] in {'awg', 'xray'}:
                return (*path, Breadcrumb(tr(locale, 'protocol.' + args[2]), callback))
            return ()
        if name in {'device_profiles', 'device_list'}:
            return (*menu, Breadcrumb(tr(locale, 'devices.title'), callback))
        if name == 'device_card' and len(args) == 2:
            return (*menu, member(tr(locale, 'devices.title'), 'device_list', args[0]),
                    Breadcrumb(tr(locale, 'navigation.device'), callback))
        return ()
    roots = {'admin_menu': admin, 'admin_settings': settings,
             'admin_nodes': servers, 'admin_profiles': profiles,
             'accounts': (*admin, Breadcrumb(tr(locale, 'accounts.title'), 'accounts')),
             'requests': (*admin, Breadcrumb(tr(locale, 'requests.title'), 'requests')),
             'announce_menu': (*admin, Breadcrumb(tr(locale, 'announce.title'), 'announce_menu')),
             'admin_status': (*admin, Breadcrumb(tr(locale, 'admin.status.title'), 'admin_status'))}
    if callback in roots:
        return roots[callback]
    settings_pages = {
        'updates': 'updates.title', 'backups': 'backups.title',
        'temporary:list:0': 'temporary.title',
        'alerts': 'alerts.title', 'traffic': 'traffic.title',
        'bot_title_settings': 'bot_title.title', 'request_policy': 'request_policy.title',
        'idefault:open': 'defaults.title', 'idefault:main': 'defaults.title',
        'recpage:0': 'recovery.title',
        'system_cleanup': 'system_cleanup.title', 'ssh_key': 'settings.admin.ssh_key',
    }
    if callback in settings_pages:
        return (*settings, Breadcrumb(tr(locale, settings_pages[callback]), callback))
    if callback.startswith('temporary:'):
        root = (*settings, Breadcrumb(tr(locale, 'temporary.title'), 'temporary:list:0'))
        action = callback.split(':',1)[1]
        if action.startswith('list:'):
            return (*settings, Breadcrumb(tr(locale, 'temporary.title'), callback))
        if action.startswith(('card:', 'show:')):
            identity = action.split(':')[1]
            return (*root, Breadcrumb(tr(locale, 'temporary.show'), 'temporary:card:'+identity))
        if action.startswith('confirm_revoke:'):
            return (*root, Breadcrumb(tr(locale, 'temporary.revoke'), callback))
        return root
    if callback == 'idefault:advanced':
        return (*parent_path('idefault:main', locale, data),
                Breadcrumb(tr(locale, 'defaults.edit_advanced'), callback))
    if callback == 'idefault:back':
        return parent_path('idefault:advanced' if data.get('installation_defaults_view') == 'advanced'
                           else 'idefault:main', locale, data)
    if callback == 'backup_settings':
        return (*parent_path('backups', locale, data), Breadcrumb(tr(locale, 'backups.settings'), callback))
    if callback.startswith(('backup_list:', 'backup_detail:')):
        return (*parent_path('backups', locale, data), Breadcrumb(tr(locale,
            'backups.details' if callback.startswith('backup_detail:') else 'backups.restore'), callback))
    if callback.startswith('backup_job:'):
        return (*parent_path('backups', locale, data), Breadcrumb(tr(locale, 'backups.result'), callback))
    if callback == 'upd_act:auto_check':
        return (*parent_path('updates', locale, data), Breadcrumb(tr(locale, 'updates.auto_title'), callback))
    if callback.startswith('update_job:'):
        return (*parent_path('updates', locale, data), Breadcrumb(tr(locale, 'update_tools.result'), callback))
    if callback == 'ufleet':
        return (*parent_path('updates', locale, data), Breadcrumb(tr(locale, 'update_tools.fleet'), callback))
    if callback.startswith('request_page:'):
        return (*admin, Breadcrumb(tr(locale, 'requests.title'), callback))
    if callback.startswith('recpage:'):
        return (*settings, Breadcrumb(tr(locale, 'recovery.title'), callback))
    if callback.startswith(('wizard_back:', 'wizard_proto:back')):
        # Wizard steps are local state transitions, not structural destinations.
        return servers
    if callback in {'profile_draft_name', 'profile_draft_nodes'}:
        return profiles
    if callback.startswith('uv_page:'):
        return (*parent_path('updates', locale, data),
                Breadcrumb(tr(locale, 'update_tools.versions'), callback))
    if callback.startswith('wsaudit:'):
        return (*parent_path('recpage:0', locale, data),
                Breadcrumb(tr(locale, 'audit.title'), callback))
    pieces = callback.split(':')
    prefix = pieces[0]
    if prefix == 'node_job' and len(pieces) == 2:
        key = data.get('_navigation_node_jobs', {}).get(pieces[1])
        if key:
            return (*parent_path('admin_node:' + key, locale, data),
                    Breadcrumb(tr(locale, 'node_tools.last_operation'), callback))
        return servers
    if prefix == 'account' and len(pieces) == 2:
        return (*parent_path('accounts', locale, data),
                Breadcrumb(tr(locale, 'navigation.account'), callback))
    node_pages = {'admin_node': None, 'node_settings': 'nodes.card.settings',
                  'node_manage': 'nodes.rich.manage', 'node_technical': 'nodes.rich.technical',
                  'node_tools': 'nodes.rich.technical', 'bootstrap_menu': 'node_tools.install',
                  'node_maintenance': 'nodes.maintenance.title', 'node_protocols': 'nodes.draft.protocol_settings',
                  'node_connection': 'nodes.rich.connection', 'probe_node': 'nodes.card.probe'}
    if prefix in node_pages and len(pieces) == 2:
        key = pieces[1]
        label = data.get('_navigation_nodes', {}).get(key, key)
        path = (*servers, Breadcrumb(label, 'admin_node:' + key))
        if prefix == 'admin_node':
            return path
        if prefix in {'node_technical', 'node_maintenance', 'node_tools'}:
            path += (Breadcrumb(tr(locale, 'nodes.rich.manage'), 'node_manage:' + key),)
        if prefix == 'node_tools':
            # Both screens currently use the same Technical title.
            return (*path, Breadcrumb(tr(locale, 'nodes.rich.technical'), callback))
        return (*path, Breadcrumb(tr(locale, node_pages[prefix]), callback))
    if prefix == 'node_section' and len(pieces) == 3 and pieces[1] in {'general', 'connection', 'awg', 'xray'}:
        section, key = pieces[1:]
        title = tr(locale, 'nodes.rich.connection' if section == 'connection' else 'node_tools.' + section)
        return (*parent_path('node_settings:' + key, locale, data), Breadcrumb(title, callback))
    if prefix == 'node_view' and len(pieces) == 3 and pieces[1] in {'ports', 'runtime', 'repair', 'diagnostics', 'entropy'}:
        view, key = pieces[1:]
        parent = f'node_section:awg:{key}' if view == 'entropy' else 'node_tools:' + key
        return (*parent_path(parent, locale, data), Breadcrumb(tr(locale, 'node_tools.' + view), callback))
    if prefix in {'admin_profile', 'prof_manage', 'admin_profile_edit', 'grant_nodes',
                  'prof_tech', 'prof_role', 'prof_expiry'} and len(pieces) == 2:
        key = pieces[1]
        path = (*profiles, Breadcrumb(data.get('_navigation_profiles', {}).get(key,
            tr(locale, 'navigation.profile')), 'admin_profile:' + key))
        if prefix == 'admin_profile':
            return path
        title = {'prof_manage': 'profile.layout.management', 'admin_profile_edit': 'profile.layout.edit',
                 'grant_nodes': 'profile.layout.access', 'prof_tech': 'profile.layout.technical',
                 'prof_role': 'profile.role.title', 'prof_expiry': 'profile.layout.expiry'}[prefix]
        if prefix in {'prof_tech', 'prof_role'}:
            path += (Breadcrumb(tr(locale, 'profile.layout.management'), 'prof_manage:' + key),)
        if prefix == 'prof_expiry':
            path += (Breadcrumb(tr(locale, 'profile.layout.edit'), 'admin_profile_edit:' + key),)
        return (*path, Breadcrumb(tr(locale, title), callback))
    return ()


def screen_parents(screen, rows, locale, data):
    """Use explicit Back destinations, never arbitrary history or action buttons."""
    if screen.breadcrumbs or not rows:
        return screen.breadcrumbs
    row = rows[-1]
    candidates = [button for button in row if button.text in BACK_LABELS]
    if not candidates and screen.navigation and len(row) == 1:
        candidates = row
    for button in candidates:
        path = parent_path(button.callback_data or '', locale, data)
        if path:
            return path[:-1] if path[-1].label == screen.title else path
    return ()


async def remember_label(state, kind, key, label):
    field = '_navigation_' + kind
    values = dict((await state.get_data()).get(field, {}))
    values.pop(key, None)
    values[key] = label
    await state.update_data(**{field: dict(list(values.items())[-100:])})


async def remember_node(state, node):
    if node.get('key') and node.get('title'):
        from .screens import server_label
        await remember_label(state, 'nodes', node['key'], server_label(node))
