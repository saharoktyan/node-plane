"""Explicit driver transport, independent of legacy bot services."""
import grpc


class GrpcIntentDriver:
    def __init__(self, channel, timeout=60):
        from driver.v1 import provisioning_service_pb2_grpc, operation_service_pb2_grpc, runtime_service_pb2_grpc
        self.provisioning = provisioning_service_pb2_grpc.ProvisioningServiceStub(channel)
        self.operations = operation_service_pb2_grpc.OperationServiceStub(channel)
        self.runtime = runtime_service_pb2_grpc.RuntimeServiceStub(channel)
        self.timeout = timeout

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
