"""What the web cabinet's relay may reach.

The relay is a public server; the controller's link forwards its requests
to this API and marks every one with `X-Node-Plane-Via: relay` itself (a
copy sent by the relay is dropped first). The mark only takes power away:
through the relay the API accepts no adapter token, no acting for a
Telegram identity except by the bot's signed requests, and no health or
integration routes. Those stay with clients on the controller host (the bot
there, Workstation over SSH) and with the bot on a node, which signs its
requests with the key the controller pinned. System operations are open to
cabinet sessions of staff with the permission for them, and the dangerous
ones ask the session to confirm itself again (see `confirmations`). A
Workstation linked through the relay has no session to confirm, so they stay
closed to it; over SSH on the controller host it reaches them as before.
"""
import re

VIA_HEADER = 'x-node-plane-via'
DELEGATION_HEADER = 'x-node-plane-telegram-user-id'

CLOSED = re.compile(r'''^(
    /health(/.*)?
  | /api/v1/integrations(/.*)?
)$''', re.VERBOSE)

SYSTEM = re.compile(r'''^(
    /api/v1/system/(updates|backups|cleanup|recovery|ssh-key|controller-address|workstation-audit)(/.*)?
  | /api/v1/nodes/[^/]+/(agent-enrollment|agent-rollouts|bootstrap|bind-verification-target
        |cleanup-step|drain|remove-step|retire-registry-only|verify-and-retire)
  | /api/v1/(agent-rollouts|node-bootstraps|relay|web-cabinet|telegram-bot)(/.*)?
)$''', re.VERBOSE)


def through_relay(headers):
    return headers.get(VIA_HEADER) is not None


def refusal(path, headers, principal_kind):
    """The error code when a relayed request may not proceed, else None.
    `principal_kind` is a token's kind (`account`, `adapter`) or a signed
    client key's (`workstation`, `bot`)."""
    if principal_kind == 'bot':
        return None
    if CLOSED.match(path) or (principal_kind == 'workstation' and SYSTEM.match(path)):
        return 'not_available_through_relay'
    if principal_kind == 'adapter':
        return 'not_available_through_relay'
    if headers.get(DELEGATION_HEADER) is not None and principal_kind != 'bot':
        return 'not_available_through_relay'
    return None
