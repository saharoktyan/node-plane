"""Explicit driver transport, independent of legacy bot services."""
import grpc


class GrpcIntentDriver:
    def __init__(self, channel, timeout=60):
        from driver.v1 import provisioning_service_pb2_grpc, operation_service_pb2_grpc, runtime_service_pb2_grpc, node_service_pb2_grpc
        self.provisioning = provisioning_service_pb2_grpc.ProvisioningServiceStub(channel)
        self.operations = operation_service_pb2_grpc.OperationServiceStub(channel)
        self.runtime = runtime_service_pb2_grpc.RuntimeServiceStub(channel)
        self.nodes = node_service_pb2_grpc.NodeServiceStub(channel)
        self.timeout = timeout

    def inspect_node(self, node_key):
        """Read facts from the agent via a driver path independent of legacy servers."""
        from driver.v1 import node_service_pb2
        response = self.nodes.InspectBackendNode(
            node_service_pb2.InspectBackendNodeRequest(node_key=node_key), timeout=self.timeout)
        if response.node_key != node_key:
            raise ValueError('driver returned a different node identity')
        return {'node_key': response.node_key, 'health_state': response.health_state,
                'runtime_version': response.runtime_version, 'runtime_commit': response.runtime_commit,
                'xray_config_present': response.xray_config_present,
                'awg_config_present': response.awg_config_present}

    def prepare_node(self, node_key):
        """Prepare an already installed, unmanaged agent for a first apply."""
        from driver.v1 import runtime_service_pb2
        import json
        response = self.runtime.PrepareBackendNode(
            runtime_service_pb2.PrepareBackendNodeRequest(node_key=node_key),
            timeout=max(self.timeout, 1200))
        if json.loads(response.result_json) != {'node_key': node_key, 'prepared': True}:
            raise ValueError('driver returned an invalid node preparation result')

    def read_xray_public(self, node_key):
        """Read the live Reality public identity; no legacy registry access."""
        from driver.v1 import runtime_service_pb2
        import json
        import re
        response = self.runtime.GetBackendXrayPublic(
            runtime_service_pb2.GetBackendXrayPublicRequest(node_key=node_key),
            timeout=self.timeout)
        if response.node_key != node_key:
            raise ValueError('driver returned a different node identity')
        value = json.loads(response.metadata_json)
        required = {'sni', 'public_key', 'short_id', 'tcp_port', 'xhttp_port',
                    'xhttp_path', 'flow', 'fingerprint'}
        if (set(value) != required or not re.fullmatch(r'[A-Za-z0-9_-]{43}', value['public_key'])
            or not re.fullmatch(r'[0-9a-fA-F]{16}', value['short_id'])
            or any(type(value[port]) is not int or not 1 <= value[port] <= 65535
                   for port in ('tcp_port', 'xhttp_port'))):
            raise ValueError('driver returned invalid Xray public metadata')
        return value

    def refresh_awg_config(self, node_key, wg_conf, profile_name):
        """Refresh a stored peer against the live agent configuration."""
        from driver.v1 import runtime_service_pb2
        response = self.runtime.RefreshAwgConfig(
            runtime_service_pb2.RefreshAwgConfigRequest(node_key=node_key,
                wg_conf=wg_conf, profile_name=profile_name), timeout=self.timeout)
        if not response.wg_conf.startswith('[Interface]') or not response.vpn_key.startswith('vpn://'):
            raise ValueError('driver returned an invalid AWG config')
        return {'wg_conf': response.wg_conf, 'vpn_key': response.vpn_key}

    @staticmethod
    def node_settings_request(intent):
        from driver.v1 import runtime_service_pb2
        import json
        return runtime_service_pb2.ApplyBackendNodeSettingsRequest(
            node_key=intent['node_key'], desired_revision=intent['revision'],
            protocols_json=json.dumps(intent['protocols'], sort_keys=True, separators=(',', ':')),
            settings_json=json.dumps(intent['settings'], sort_keys=True, separators=(',', ':')))

    def apply_node_settings(self, task_id, intent):
        from driver.v1 import operation_service_pb2
        started = self.runtime.ApplyBackendNodeSettings(self.node_settings_request(intent),
            metadata=(('x-node-plane-command-id', task_id),), timeout=max(self.timeout, 1200))
        operation = self.operations.GetOperation(operation_service_pb2.GetOperationRequest(
            operation_id=started.operation_id), timeout=self.timeout)
        if operation.kind != 'apply_backend_node_settings' or operation.node_key != intent['node_key']:
            raise ValueError('driver returned an unrelated node operation')
        if operation.status != 'SUCCEEDED':
            raise ValueError('node settings were not applied')
        return operation.result_json

    def recover_node_settings(self, task_id, intent):
        response = self.runtime.RecoverBackendNodeSettings(self.node_settings_request(intent),
            metadata=(('x-node-plane-command-id', task_id),), timeout=self.timeout)
        return response.result_json

    def resolve_node_settings(self, task_id, intent):
        import json
        response = self.runtime.ResolveBackendNodeSettings(self.node_settings_request(intent),
            metadata=(('x-node-plane-command-id', task_id),), timeout=self.timeout)
        observation = json.loads(response.result_json)
        if set(observation) != {'config_matches', 'containers_running'} or any(
                type(value) is not bool for value in observation.values()):
            raise ValueError('driver returned an invalid node settings observation')
        return observation

    def decommission(self, node_key, command_id, phase):
        from driver.v1 import runtime_service_pb2
        response = self.runtime.DecommissionNode(runtime_service_pb2.DecommissionNodeRequest(
            node_key=node_key, command_id=command_id, phase=phase), timeout=max(self.timeout, 240))
        if response.phase != phase:
            raise ValueError('driver returned the wrong decommission phase')
        return response.summary

    def lookup(self, task_id, intent):
        from driver.v1 import operation_service_pb2
        operation_id = None
        try:
            operation = self.operations.GetOperationByCommand(
                operation_service_pb2.GetOperationByCommandRequest(command_id=task_id), timeout=self.timeout)
            matched = (operation.kind == 'apply_profile_intent'
                       and operation.node_key == intent['node_key']
                       and operation.profile_name == intent['runtime_name'])
            if not matched:
                return {'succeeded': False, 'driver_operation_id': None, 'result_json': None}
            operation_id = operation.operation_id
            if operation.status == 'SUCCEEDED':
                return {'driver_operation_id': operation_id, 'succeeded': True, 'result_json': operation.result_json}
        except grpc.RpcError as error:
            if error.code() != grpc.StatusCode.NOT_FOUND:
                raise
        # Driver may have timed out before the agent durably completed. Read the
        # agent journal with the exact payload; do not resend a mutation.
        recovered = self.provisioning.RecoverProfileIntent(self.request(intent),
            metadata=(('x-node-plane-command-id', task_id),), timeout=self.timeout)
        return {'driver_operation_id': operation_id, 'succeeded': True,
                'result_json': recovered.payload_json}

    @staticmethod
    def request(intent):
        from driver.v1 import provisioning_service_pb2, types_pb2
        request = provisioning_service_pb2.ApplyProfileIntentRequest(
            node_key=intent['node_key'], runtime_name=intent['runtime_name'],
            protocol_kind=intent['protocol'], action=intent['action'],
            desired_revision=intent['desired_revision'])
        if 'xray' in intent:
            request.xray.CopyFrom(types_pb2.XraySpec(profile_name=intent['runtime_name'], **intent['xray']))
        return request

    def inspect(self, intent):
        observation = self.provisioning.InspectProfileIntent(self.request(intent), timeout=self.timeout)
        return {key: getattr(observation, key) for key in ("disk_present", "live_present", "identity_matches", "config_available")}

    def resolve(self, task_id, intent):
        """Retire a quiesced agent command; never execute its old mutation."""
        observation = self.provisioning.ResolveProfileIntent(self.request(intent),
            metadata=(('x-node-plane-command-id', task_id),), timeout=self.timeout)
        return {key: getattr(observation, key) for key in
                ('disk_present', 'live_present', 'identity_matches', 'config_available')}

    def execute(self, task_id, intent):
        from driver.v1 import operation_service_pb2
        request = self.request(intent)
        started = self.provisioning.ApplyProfileIntent(request,
            metadata=(('x-node-plane-command-id', task_id),), timeout=self.timeout)
        operation = self.operations.GetOperation(operation_service_pb2.GetOperationRequest(
            operation_id=started.operation_id), timeout=self.timeout)
        return {'driver_operation_id': started.operation_id,
                'succeeded': operation.status == 'SUCCEEDED',
                'result_json': operation.result_json if operation.status == 'SUCCEEDED' else None}


def local_channel(address):
    # The deployment must keep driver loopback-only, as with the current bot.
    if address not in {'127.0.0.1:50051', 'localhost:50051', '[::1]:50051'}:
        raise ValueError('backend worker requires a loopback driver endpoint')
    return grpc.insecure_channel(address)
