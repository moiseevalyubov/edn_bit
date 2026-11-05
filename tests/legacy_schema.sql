-- Synthetic pre-CRM schema. No customer records or credentials.
CREATE TABLE portals (
    id INTEGER PRIMARY KEY,
    member_id VARCHAR NOT NULL UNIQUE,
    client_endpoint VARCHAR NOT NULL,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    token_expires_at TIMESTAMP,
    app_token VARCHAR,
    open_line_id VARCHAR,
    installed_at TIMESTAMP,
    uninstalled_at TIMESTAMP,
    payment_required_at TIMESTAMP
);
CREATE TABLE channels (
    id INTEGER PRIMARY KEY,
    portal_id INTEGER NOT NULL REFERENCES portals(id),
    name VARCHAR NOT NULL,
    api_key TEXT NOT NULL,
    sender VARCHAR NOT NULL,
    webhook_token VARCHAR UNIQUE,
    subject_id INTEGER,
    channel_type VARCHAR,
    connected_at TIMESTAMP,
    disconnected_at TIMESTAMP,
    is_active BOOLEAN
);
CREATE TABLE message_delivery_tasks (
    id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL REFERENCES channels(id),
    task_type VARCHAR NOT NULL,
    direction VARCHAR NOT NULL,
    payload TEXT NOT NULL,
    status VARCHAR NOT NULL,
    retry_count INTEGER NOT NULL,
    next_attempt_at TIMESTAMP NOT NULL,
    last_error TEXT,
    max_message_id VARCHAR,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
