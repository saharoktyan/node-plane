"""What the web cabinet's relay may reach.

The relay is a public server; the controller's link forwards its requests
to this API and marks every one with `X-Node-Plane-Via: relay` itself (a
copy sent by the relay is dropped first). The mark only takes power away:
through the relay the API accepts no adapter credentials, no acting for a
Telegram identity, no integration routes and no system operations. Those
stay with clients on the controller host (the bot, Workstation over SSH).
"""
import re

VIA_HEADER = 'x-node-plane-via'
DELEGATION_HEADER = 'x-node-plane-telegram-user-id'

CLOSED = re.compile(r'''^(
    /health(/.*)?
  | /api/v1/integrations(/.*)?
  | /api/v1/system/(updates|backups|cleanup|recovery|ssh-key|controller-address|workstation-audit)(/.*)?
  | /api/v1/nodes/[^/]+/(agent-enrollment|agent-rollouts|bootstrap|bind-verification-target
        |cleanup-step|drain|remove-step|retire-registry-only|verify-and-retire)
  | /api/v1/(agent-rollouts|node-bootstraps|relay)(/.*)?
)$''', re.VERBOSE)


def through_relay(headers):
    return headers.get(VIA_HEADER) is not None


def refusal(path, headers, principal_kind):
    """The error code when a relayed request may not proceed, else None."""
    if CLOSED.match(path):
        return 'not_available_through_relay'
    if headers.get(DELEGATION_HEADER) is not None or principal_kind == 'adapter':
        return 'not_available_through_relay'
    return None
