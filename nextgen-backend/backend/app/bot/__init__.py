"""
NextGen Bot Package — WhatsApp ordering pipeline.

Modules:
  intent_router  — Rules-first keyword matching (no LLM cost)
  state_machine  — Conversation state + cart management
  prompt_builder — Constrained LLM prompt construction
  llm_parser     — Parse structured output from LLM
  responses      — Template responses (menu, hours, specials)
  order_creator  — Transactional order creation from cart
  whatsapp_sender— Meta Cloud API message sending
  usage_tracker  — Daily usage upsert + limit enforcement
  pipeline       — Full message processing orchestrator
  outbox_worker  — Background reliable delivery worker
"""
