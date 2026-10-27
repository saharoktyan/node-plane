"""Safe public errors for read-only agent inspection, without remote details."""
import logging

import grpc

from .authorization import AccessDenied
from .driver_transport import AgentNotConfigured


def inspection_error(node_key, failure, *, fallback='driver_unavailable'):
    code, status, hint = fallback, 503, 'timeout' if isinstance(failure, TimeoutError) else 'driver_error'
    rpc_status = 'non_rpc_error'
    if isinstance(failure, AgentNotConfigured):
        code, status, hint = 'node_agent_unconfigured', 409, 'missing_route'
    elif isinstance(failure, grpc.RpcError):
        rpc = failure.code()
        rpc_status = rpc.name
        # Details may contain arbitrary remote output. Classify known reasons;
        # never put the raw message (or credentials it might contain) in logs.
        detail = (failure.details() or '').lower()
        if rpc == grpc.StatusCode.FAILED_PRECONDITION:
            if detail in {'no node-agent target configured', 'agent not configured'}:
                code, status, hint = 'node_agent_unconfigured', 409, 'missing_route'
            elif detail == 'agent node identity mismatch':
                code, status, hint = 'node_agent_identity_mismatch', 409, 'wrong_node_key'
            else:
                code, status, hint = 'node_agent_inspection_failed', 502, 'runtime_inspection'
        elif rpc in {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED}:
            code = 'node_agent_unavailable'
            hint = ('timeout' if rpc == grpc.StatusCode.DEADLINE_EXCEEDED or 'timed out' in detail
                    else 'tls' if 'certificate' in detail or 'tls' in detail
                    else 'connection_refused' if 'connection refused' in detail
                    else 'dns' if 'dns' in detail or 'name resolution' in detail
                    else 'connection_unavailable')
        else:
            code, status, hint = 'node_agent_inspection_failed', 502, 'agent_rpc_error'
    logging.getLogger(__name__).warning('Node inspection failed node=%s rpc_status=%s cause=%s',
                                       node_key, rpc_status, hint)
    return AccessDenied(code, status)
