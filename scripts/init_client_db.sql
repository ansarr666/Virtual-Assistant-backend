-- =====================================================================
-- Omnichannel Conversational Lead Bot - Client Database Schema
-- Compatible with PostgreSQL, Supabase, and SQLite (Standard SQL)
-- =====================================================================

-- 1. Users table: tracks returning users, contact information, and active session
CREATE TABLE IF NOT EXISTS users (
    tenant_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(128) NOT NULL,
    phone_number VARCHAR(32),
    channel VARCHAR(32),
    name VARCHAR(128),
    email VARCHAR(256),
    active_conversation_id VARCHAR(128),
    first_seen_at VARCHAR(64),
    last_seen_at VARCHAR(64),
    status VARCHAR(32) DEFAULT 'active',
    metadata TEXT,
    PRIMARY KEY (tenant_id, user_id)
);

-- 2. Conversations table: tracks conversation lifecycle, stages, and status
CREATE TABLE IF NOT EXISTS conversations (
    tenant_id VARCHAR(64) NOT NULL,
    conversation_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128) NOT NULL,
    status VARCHAR(32) DEFAULT 'active',
    stage VARCHAR(64),
    started_at VARCHAR(64),
    updated_at VARCHAR(64),
    metadata TEXT,
    PRIMARY KEY (tenant_id, conversation_id)
);

-- 3. Messages table: audit trail of all inbound and outbound messages
CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    conversation_id VARCHAR(128) NOT NULL,
    message_id VARCHAR(128),
    sender VARCHAR(32) NOT NULL,
    message_text TEXT,
    timestamp VARCHAR(64),
    metadata TEXT
);

-- 4. Conversation State table: stores dynamic state graph position and collected slots
CREATE TABLE IF NOT EXISTS conversation_state (
    tenant_id VARCHAR(64) NOT NULL,
    conversation_id VARCHAR(128) NOT NULL,
    state_json TEXT NOT NULL,
    updated_at VARCHAR(64),
    PRIMARY KEY (tenant_id, conversation_id)
);

-- 5. Leads table: qualified sales enquiries with validated contact details
CREATE TABLE IF NOT EXISTS leads (
    tenant_id VARCHAR(64) NOT NULL,
    lead_id VARCHAR(128) NOT NULL,
    conversation_id VARCHAR(128) NOT NULL,
    user_id VARCHAR(128),
    service VARCHAR(128),
    requirements TEXT,
    timeline VARCHAR(128),
    budget VARCHAR(128),
    status VARCHAR(32) DEFAULT 'qualified',
    created_at VARCHAR(64),
    updated_at VARCHAR(64),
    metadata TEXT,
    PRIMARY KEY (tenant_id, lead_id)
);

-- 6. Idempotent Responses table: guarantees exactly-once processing for webhook retries
CREATE TABLE IF NOT EXISTS idempotent_responses (
    tenant_id VARCHAR(64) NOT NULL,
    conversation_id VARCHAR(128) NOT NULL,
    client_message_id VARCHAR(128) NOT NULL,
    response_json TEXT NOT NULL,
    created_at VARCHAR(64),
    PRIMARY KEY (tenant_id, conversation_id, client_message_id)
);

-- Indexes for fast query performance
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(tenant_id, conversation_id);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(tenant_id, user_id);
CREATE INDEX IF NOT EXISTS idx_leads_tenant ON leads(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_users_phone ON users(tenant_id, phone_number);
