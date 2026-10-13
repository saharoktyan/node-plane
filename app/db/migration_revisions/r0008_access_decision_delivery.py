"""Durable edits of access-request notices after decisions in either client."""


def upgrade(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_access_decision_details (
        request_id TEXT PRIMARY KEY REFERENCES backend_access_requests(id) ON DELETE CASCADE,
        decider_json TEXT NOT NULL, surface TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS backend_access_request_messages (
        request_id TEXT NOT NULL REFERENCES backend_access_requests(id) ON DELETE CASCADE,
        chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL, locale TEXT NOT NULL,
        status TEXT NOT NULL, PRIMARY KEY(request_id,chat_id,message_id))''')
