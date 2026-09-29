from aiogram.fsm.state import State, StatesGroup

class ProfileDraftState(StatesGroup):
    waiting_for_name = State()

class NodeDraftState(StatesGroup):
    waiting_for_node_base = State()
    waiting_for_protocol_choice = State()

class AgentDraftState(StatesGroup):
    waiting_for_ssh_target = State()

class NodeEditState(StatesGroup):
    waiting_for_value = State()

class MaintenanceState(StatesGroup):
    waiting_for_ssh_target = State()
