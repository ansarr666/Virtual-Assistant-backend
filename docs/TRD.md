## 1. Technical Objective

Build a reusable, multi-tenant conversational lead-generation platform.
A prospect initiates a conversation through a supported channel.
The backend should:
Receive Message
      ↓
Identify Tenant
      ↓
Identify User / Conversation
      ↓
Load Conversation State
      ↓
Load Tenant Conversation Flow
      ↓
Process User Input
      ↓
Decide Next Action
      ↓
Respond
      ↓
Store Conversation + State

The same application should support multiple clients through configuration rather than separate codebases.

## 2. High-Level Architecture

Prospect
   ↓
Messaging Channel
   ↓
WhatsApp / Instagram / Web Chat / etc.
   ↓
Webhook / API
   ↓
Channel Adapter
   ↓
FastAPI Backend
   ↓
Conversation Orchestrator
   ↓
Flow Engine
   ├── Predefined Q&A
   ├── Intent Detection
   ├── Qualification
   ├── RAG
   ├── Booking
   └── Human Handoff
   ↓
Storage Layer
   ↓
Response
   ↓
Channel API
   ↓
Prospect

## 3. Backend Stack

Initial stack:
Language: Python
Framework: FastAPI
Development Storage: Google Sheets
Flow Definition: JSON
Authentication/Config: .env
API Testing: FastAPI Swagger

Later:
Production DB: PostgreSQL / Client DB
Cache/State: Redis if required
Vector DB: when RAG is introduced
Messaging APIs: WhatsApp / Meta / others
Calendar: Google Calendar / booking provider

## 4. Multi-Tenant Architecture

Every request should contain or resolve a:
tenant_id

Example:
client_001
client_002
client_003

Each tenant may have its own:
Platform credentials
Conversation flow
Business information
Qualification rules
RAG knowledge base
Calendar
Database
Branding / message templates

Core backend logic remains shared.
Same Application
      ↓
Tenant Resolver
      ↓
Tenant Configuration
      ↓
Tenant-specific execution

## 5. Channel Integration Layer

Each communication platform should have its own adapter.
WhatsAppAdapter
InstagramAdapter
FacebookAdapter
WebChatAdapter

Incoming platform messages should be converted into one internal format.
Example:
{
  "tenant_id": "client_001",
  "user_id": "user_123",
  "channel": "whatsapp",
  "message": "Hi"
}

The orchestrator should not need to know whether the message originally came from WhatsApp, Instagram, or Web Chat.

## 6. Conversation Orchestrator

The orchestrator is the central backend component.
Its job is:
What did the user say?
        ↓
Where is the user currently in the conversation?
        ↓
Which flow are they using?
        ↓
What should happen next?

Possible actions:
Show Options
Ask Question
Save Answer
Detect Intent
Call RAG
Qualify Prospect
Book Call
Human Handoff
Complete Conversation

## 7. Conversation Flow Engine

Conversation flows should not be hardcoded inside Python.
They should be configuration-driven.
Example:
flows/
    lead_enquiry.json

Example flow:
Welcome
   ↓
Choose Service
   ↓
Describe Requirement
   ↓
Timeline
   ↓
Qualification
   ↓
Book Call

Each flow step can define:
Step ID
Question
Options
Input Type
Next Step
Validation
Action

This allows business flows to change without rewriting the orchestrator.

## 8. Conversation State

The application must remember where each user currently is.
Example:
{
  "tenant_id": "client_001",
  "conversation_id": "conv_user_001",
  "current_flow": "lead_enquiry",
  "current_step": "timeline_asked",
  "intent": "AI_SERVICE",
  "status": "active"
}

This allows conversations to continue across multiple messages.

## 9. Storage Architecture

Business logic should not depend directly on Google Sheets or any particular database.
Use a repository layer:
Orchestrator
      ↓
LeadRepository
      ↓
Storage Implementation

Current:
GoogleSheetRepository

Future:
PostgresRepository
MySQLRepository
ClientDatabaseRepository

This lets each client eventually use their own database without rewriting the orchestration layer.

## 10. Core Data Entities

Current core entities:
tenants
users
conversations
messages
conversation_state

Future entities may include:
qualification
bookings
integrations
knowledge_sources
handoffs

## 11. AI Layer

AI should supplement the deterministic conversation flow.
Initial AI capabilities:
Intent Detection
Free-text Understanding
Option Mapping
Structured Output

Example:
User:
"I need someone to build a chatbot for our internal documents"

        ↓

AI

        ↓

{
  "intent": "AI_SERVICE",
  "confidence": 0.94
}

The orchestrator then decides the next action.
The LLM should not control the entire application flow by itself.

## 12. RAG

RAG will be introduced for questions requiring company-specific knowledge.
User Question
      ↓
Orchestrator
      ↓
RAG Service
      ↓
Retrieve Approved Company Data
      ↓
LLM Generates Grounded Answer
      ↓
Guardrail Check
      ↓
Return Answer
      ↓
Resume Conversation Flow

Each tenant should eventually have its own knowledge base.

## 13. Call Booking

User chooses Book a Call
       ↓
Booking Service
       ↓
Tenant Calendar
       ↓
Fetch Available Slots
       ↓
User Selects Slot
       ↓
Create Meeting
       ↓
Store Booking
       ↓
Send Confirmation

Calendar configuration must be tenant-specific.

## 14. Human Handoff

Human handoff should occur when:
User asks for a person
AI confidence is too low
Unsupported question
High-value enquiry needs manual handling
System failure

The conversation history should be provided to the human agent so the prospect does not need to repeat everything.

## 15. API Structure 

Example backend APIs:
POST /chat

POST /webhooks/whatsapp
POST /webhooks/instagram

GET /conversations/{id}

GET /flows/{id}

POST /rag/query

GET /calendar/slots
POST /bookings

POST /handoff

## 16. Error Handling 

System should handle:
Webhook failure
Platform API failure
Invalid conversation state
Invalid flow configuration
LLM failure
RAG failure
Calendar failure
Database failure
Duplicate webhook/message

A failure should not destroy the user's conversation state.

## 17. Logging & Monitoring

Track:
Incoming messages
Outgoing messages
Conversation steps
AI decisions
API failures
RAG failures
Booking failures
Human handoffs
Conversation drop-offs

These logs will later help improve both product behaviour and AI performance.

## 18. Current Development Status

Already implemented / started:
✓ FastAPI backend
✓ POST /chat
✓ Google Sheets connectivity
✓ Repository abstraction
✓ Multi-tenant tenant_id
✓ Conversation creation
✓ Message persistence
✓ Conversation state
✓ Basic orchestration flow
✓ JSON flow separation — in progress

Next technical milestones:
JSON Flow Engine
      ↓
LLM Intent Detection
      ↓
Real Conversation Flow
      ↓
First Messaging Channel
      ↓
Booking
      ↓
RAG

# 19. Core Engineering Principle

The architecture should maintain this separation:
Channel
   ≠
Conversation Logic
   ≠
AI
   ≠
Storage

Each should be replaceable independently.
That is what allows the same system to work for different clients without rebuilding it every time.