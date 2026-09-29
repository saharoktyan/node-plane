from typing import Optional
from aiogram.filters.callback_data import CallbackData

class HomeCallback(CallbackData, prefix="home"):
    pass

class ProfilesCallback(CallbackData, prefix="profiles"):
    pass

class ProfileCallback(CallbackData, prefix="profile"):
    profile_id: str

class NodeCallback(CallbackData, prefix="node"):
    profile_id: str
    node_key: str

class IssueCallback(CallbackData, prefix="issue"):
    profile_id: str
    node_key: str
    protocol: str
    transport: str

class IssuanceStatusCallback(CallbackData, prefix="issuance_status"):
    issuance_id: str
    profile_id: str
    node_key: str
    protocol: str
    transport: str

class ShowQrCallback(CallbackData, prefix="show_qr"):
    issuance_id: str
    profile_id: str
    node_key: str

class RequestAccessCallback(CallbackData, prefix="request_access"):
    pass

class RequestsCallback(CallbackData, prefix="requests"):
    pass

class ReviewCallback(CallbackData, prefix="review"):
    request_id: str

class DecideCallback(CallbackData, prefix="decide"):
    request_id: str
    decision: str

class NotificationReviewCallback(CallbackData, prefix="notification_review"):
    request_id: str

class AccountsCallback(CallbackData, prefix="accounts"):
    pass

class AccountCallback(CallbackData, prefix="account"):
    account_id: str

class NewProfileCallback(CallbackData, prefix="new_profile"):
    account_id: str

class AdminProfilesCallback(CallbackData, prefix="admin_profiles"):
    pass

class AdminProfileCallback(CallbackData, prefix="admin_profile"):
    profile_id: str

class AdminNodesCallback(CallbackData, prefix="admin_nodes"):
    pass

class NewNodeCallback(CallbackData, prefix="new_node"):
    pass

class SubmitNodeCallback(CallbackData, prefix="submit_node"):
    choice: str

class AdminNodeCallback(CallbackData, prefix="admin_node"):
    node_key: str

class NodeSettingsCallback(CallbackData, prefix="node_settings"):
    node_key: str

class EditNodeFieldCallback(CallbackData, prefix="edit_node_field"):
    node_key: str
    field: str

class NodeProtocolsCallback(CallbackData, prefix="node_protocols"):
    node_key: str

class NodeMaintenanceCallback(CallbackData, prefix="node_maintenance"):
    node_key: str

class BindLocalCallback(CallbackData, prefix="bind_local"):
    node_key: str

class BindSshCallback(CallbackData, prefix="bind_ssh"):
    node_key: str

class ConfirmNodeDrainCallback(CallbackData, prefix="confirm_node_drain"):
    node_key: str

class DrainNodeCallback(CallbackData, prefix="drain_node"):
    node_key: str

class CleanupStepCallback(CallbackData, prefix="cleanup_step"):
    node_key: str
    expected_phase: str

class VerifyRetirementCallback(CallbackData, prefix="verify_retirement"):
    node_key: str

class ConfirmRegistryRemovalCallback(CallbackData, prefix="confirm_registry_removal"):
    node_key: str

class RetireRegistryCallback(CallbackData, prefix="retire_registry"):
    node_key: str

class UpdatesCallback(CallbackData, prefix="updates"):
    pass

class NodeUpdatesCallback(CallbackData, prefix="node_updates"):
    node_key: str

class RefreshRuntimeCallback(CallbackData, prefix="refresh_runtime"):
    node_key: str

class ToggleNodeProtocolCallback(CallbackData, prefix="toggle_node_protocol"):
    node_key: str
    kind: str

class ToggleNodeTransportCallback(CallbackData, prefix="toggle_node_transport"):
    node_key: str
    kind: str

class RolloutLocalCallback(CallbackData, prefix="rollout_local"):
    node_key: str

class RolloutSshCallback(CallbackData, prefix="rollout_ssh"):
    node_key: str

class RolloutStatusCallback(CallbackData, prefix="rollout_status"):
    task_id: str

class RetryRolloutCallback(CallbackData, prefix="retry_rollout"):
    pass

class ProbeNodeCallback(CallbackData, prefix="probe_node"):
    node_key: str

class ApplyNodeCallback(CallbackData, prefix="apply_node"):
    node_key: str

class NodeApplyStatusCallback(CallbackData, prefix="node_apply_status"):
    operation_id: str
    node_key: str

class GrantNodesCallback(CallbackData, prefix="grant_nodes"):
    profile_id: str

class GrantProtocolsCallback(CallbackData, prefix="grant_protocols"):
    profile_id: str
    node_key: str

class AddGrantCallback(CallbackData, prefix="add_grant"):
    profile_id: str
    node_key: str
    protocol: str

class RemoveGrantCallback(CallbackData, prefix="remove_grant"):
    profile_id: str
    node_key: str
    protocol: str

class ToggleFreezeCallback(CallbackData, prefix="toggle_freeze"):
    profile_id: str

class AdminSettingsCallback(CallbackData, prefix="admin_settings"):
    pass
