# app/handlers/admin.py
from .admin_commands import (
    add_cmd,
    addserver_cmd,
    bootstrapserver_cmd,
    collecttraffic_cmd,
    diag_cmd,
    del_cmd,
    list_cmd,
    probeserver_cmd,
    setxrayserver_cmd,
    syncxrayserver_cmd,
    sshkey_cmd,
    servers_cmd,
)
from .admin_server_wizard import (
    on_server_callback,
    server_wizard_text,
    serverconfig_cmd,
    serverwizard_cmd,
    setserverfield_cmd,
    syncnodeenv_cmd,
)
from .admin_wizard import changecfg_cmd, cfg_wizard_text, createcfg_cmd, on_cfg_callback
from .user_profile import admin_menu_text_router


def admin_text_router(update, context) -> None:
    cfg_wizard_text(update, context)
    server_wizard_text(update, context)
    admin_menu_text_router(update, context)

__all__ = [
    "add_cmd",
    "addserver_cmd",
    "bootstrapserver_cmd",
    "collecttraffic_cmd",
    "diag_cmd",
    "del_cmd",
    "list_cmd",
    "probeserver_cmd",
    "setxrayserver_cmd",
    "setserverfield_cmd",
    "sshkey_cmd",
    "syncxrayserver_cmd",
    "servers_cmd",
    "serverconfig_cmd",
    "serverwizard_cmd",
    "syncnodeenv_cmd",
    "createcfg_cmd",
    "changecfg_cmd",
    "admin_text_router",
    "on_cfg_callback",
    "on_server_callback",
]
