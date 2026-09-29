from aiogram.fsm.state import State, StatesGroup

class ProfileDraftState(StatesGroup):
    waiting_for_name = State()

class NodeDraftState(StatesGroup):
    waiting_for_key = State()
    waiting_for_title = State()
    waiting_for_flag = State()
    waiting_for_region = State()
    waiting_for_transport = State()
    waiting_for_target = State()
    waiting_for_public_host = State()

class AgentDraftState(StatesGroup):
    waiting_for_ssh_target = State()

class NodeEditState(StatesGroup):
    waiting_for_value = State()

class MaintenanceState(StatesGroup):
    waiting_for_ssh_target = State()
