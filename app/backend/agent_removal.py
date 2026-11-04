"""Removal of a remote node, checked by its agent instead of over SSH.

Before draining, the controller records the host's machine-id the agent
reports. The agent checks the host itself before it removes itself: the
runtime, its configs and its containers are gone. The controller retires
the node once that agent, reporting the same machine-id, stops answering.

The agent's own removal is not verified: an agent that stopped answering
for another reason looks the same. The retirement record says so.
"""
from datetime import datetime, timezone

import grpc

from .authorization import AccessDenied
from .removal_verifier import RemovalVerificationError, host_fingerprint

CONTAINERS = {'absent', 'docker_not_installed'}


class AgentStillRunning(RemovalVerificationError):
    """The agent has not stopped yet; its removal may still be on the way."""


def validate_check(check):
    """The agent's report, as the agent sends it; ValueError otherwise."""
    if (not isinstance(check, dict) or set(check) != {'host_id', 'absent_paths', 'containers'}
            or check['containers'] not in CONTAINERS
            or not isinstance(check['absent_paths'], list)
            or not 1 <= len(check['absent_paths']) <= 16
            or any(not isinstance(path, str) or not path.startswith('/') or len(path) > 4096
                   for path in check['absent_paths'])):
        raise ValueError('invalid agent host check')
    host_fingerprint(check['host_id'])
    return check


class AgentRemovalVerifier:
    method = 'agent'
    target = 'agent'

    def __init__(self, driver):
        self.driver = driver

    def capture_identity(self, node_key):
        host = self.driver.inspect_node(node_key).get('host_id') or ''
        if not host:
            # Agents before this check do not report their host.
            raise AccessDenied('agent_update_required', 409)
        return host_fingerprint(host)

    def capture_resources(self, node_key, expected_fingerprint):
        # The agent checks its own paths; nothing to record in advance.
        return None

    def verify(self, node_key, expected_fingerprint, check):
        validate_check(check)
        if host_fingerprint(check['host_id']) != expected_fingerprint:
            raise RemovalVerificationError('host identity changed')
        try:
            self.driver.inspect_node(node_key)
        except grpc.RpcError as error:
            if error.code() not in {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED}:
                raise
        else:
            raise AgentStillRunning('agent still answers')
        return {'method': 'agent', 'target': 'agent',
                'host_fingerprint': expected_fingerprint,
                'checked_at': datetime.now(timezone.utc).isoformat(),
                'agent_check': check,
                'agent_removal_verified': False,
                'result': 'runtime_absent_agent_stopped'}
