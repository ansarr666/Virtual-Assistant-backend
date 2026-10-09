## 1. Product Overview

Lead Gen Orchestration is a **multi-tenant conversational lead-generation system**.

A potential prospect initiates a conversation through a supported messaging channel, typically by sending something like **“Hi”**.

The system then:

`User initiates conversation → predefined options → guided enquiry → qualification → answer / human handoff / book a call`

The goal is to automate the repetitive first stage of sales conversations while keeping the experience simple and structured.

The same product should be reusable for different clients by changing their configuration, credentials, conversation flows, business information, and integrations rather than changing the core codebase.

---

## 2. Problem Statement

Inbound prospects often contact businesses through different messaging platforms.

Today these conversations may:

- depend on someone manually responding,
- have inconsistent responses,
- take too long to qualify,
- repeatedly ask the same basic questions,
- lose potential prospects before a sales call is booked.

The product should automate this initial interaction and move interested users toward the appropriate next action.

---

## 3. Target Users & Channels

### External Users

Potential prospects who want to:

- understand the company's services,
- make an enquiry,
- discuss a requirement,
- ask basic business questions,
- book a call with the team.

### Internal Users

- Sales / Business Development
- Admins managing the system
- Marketing / business teams defining conversation flows

### Channels

The product focuses on **conversational channels**, such as:

- WhatsApp
- Instagram DM
- Facebook Messenger
- Website chat
- Other supported messaging integrations

A platform is relevant when a prospect can initiate an interactive conversation.

Website contact forms, Google Forms, etc. are **not part of the core product scope**.

---

## 4. Goals & Success Metrics

### Product Goals

- Respond instantly to inbound prospects.
- Give every prospect a consistent first interaction.
- Automate repetitive enquiry questions.
- Understand what the prospect is looking for.
- Collect useful information before involving sales.
- Move interested prospects toward booking a call.
- Allow human intervention when automation is insufficient.

### Success Metrics

Track:

- conversation completion rate,
- enquiry-to-call-booking rate,
- user drop-off rate,
- percentage of conversations handled automatically,
- human handoff rate,
- average response time,
- percentage of users successfully reaching their intended action.

---

## 5. Core User Experience

The basic experience begins when the user initiates a conversation.

Example:

`User: Hi`

System:

**Hi! How can we help you?**

Options could include:

- Explore our services
- Discuss a project
- Make an enquiry
- Partnership
- Book a call
- Other

The system then loads the relevant conversation flow.

Example:

`Hi`

→ `AI Services`

→ `What are you looking to build?`

→ `What timeline are you considering?`

→ `Would you like to book a call?`

→ `Available slots`

→ `Call booked`

The final conversation flows will be configurable and supplied by the business team.

---

## 6. Functional Requirements

The V1 system should support:

### Conversation Handling

- Detect a new incoming conversation.
- Send a predefined welcome message.
- Present predefined options.
- Support branching Q&A flows.
- Accept both predefined selections and free-text responses.
- Maintain conversation state.
- Resume an existing conversation.

### Lead / Prospect Handling

Capture relevant information such as:

- name,
- contact identifier,
- source/channel,
- service interest,
- requirement,
- timeline,
- qualification information,
- conversation history,
- current status.

### AI Capabilities

AI should assist with:

- understanding free-text responses,
- intent detection,
- mapping user language to predefined flows,
- answering supported business questions,
- deciding when clarification is required.

The AI should **support the predefined workflow rather than replace it**.

### RAG

RAG is an internal capability, not a lead source.

Example:

`User asks business question`

→ `Orchestrator`

→ `RAG searches approved company knowledge`

→ `Answer returned`

→ `Original enquiry flow continues`

### Booking

The system should be able to:

- offer call booking,
- fetch available calendar slots,
- let the prospect choose a slot,
- create the meeting,
- send confirmation.

### Human Handoff

The system should allow escalation when:

- the user explicitly asks for a person,
- the AI cannot answer reliably,
- the enquiry requires manual intervention,
- the predefined flow cannot handle the request.

---

## 7. Multi-Tenant Product Requirement

The product should be built once and reused for multiple clients.

Each tenant/client can have its own:

- company details,
- platform credentials,
- enabled channels,
- conversation flows,
- qualification rules,
- RAG knowledge base,
- calendar configuration,
- response templates,
- data-storage configuration.

Example:

`Same Core Application`

→ `Tenant A Config`

→ `Tenant B Config`

→ `Tenant C Config`

The core application should not require code changes when onboarding a normal new client.

For internal development, Google Sheets may be used as temporary storage.

Production clients may connect their own database or storage system.

---

## 8. Lead Qualification & Status

The system should gradually collect enough information to understand whether the person is a meaningful prospect.

Typical information may include:

- service of interest,
- requirement/problem,
- company,
- timeline,
- budget if required,
- willingness to speak with the team.

Possible lead lifecycle:

`NEW → ENGAGED → QUALIFYING → QUALIFIED → CALL_BOOKED → CONVERTED / CLOSED`

Additional states may include:

- HUMAN_HANDOFF
- EXISTING_CUSTOMER
- SPAM
- UNQUALIFIED

Qualification rules should remain configurable per tenant.

---

## 9. Guardrails & Edge Cases

The system should:

- never invent business information,
- never invent pricing or commitments,
- answer using approved information,
- ask for clarification when intent is unclear,
- preserve conversation state,
- allow users to restart a conversation,
- allow human handoff when needed.

Important scenarios to test:

- user sends something other than “Hi”,
- user ignores provided options,
- user types a natural-language answer instead of clicking an option,
- user changes topic midway,
- user directly asks to book a call,
- user stops responding,
- returning user starts another conversation,
- RAG cannot confidently answer,
- calendar integration fails,
- external messaging API fails.

---

## 10. Launch & Iteration

### Initial V1

Start with:

- one messaging channel,
- one tenant,
- basic predefined conversation flow,
- conversation-state tracking,
- simple intent detection,
- call-booking flow,
- internal storage.

### Live Monitoring

Monitor:

- where users abandon conversations,
- which options are used most,
- questions users ask outside predefined flows,
- AI misunderstandings,
- RAG failures,
- booking failures,
- human handoff cases.

### Iteration

Use real conversations to improve:

- conversation options,
- question sequence,
- prompts,
- intent detection,
- qualification rules,
- RAG content,
- handoff rules,
- booking experience.

The product should evolve based on **actual user conversations rather than assumptions made before launch**.