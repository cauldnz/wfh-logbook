"""Notifier: messaging-channel review interface (HANDOFF §6 Phase 7).

Layering (7.A): ``conversation.py`` and ``grammar.py`` are PURE — no HTTP,
no DB writes, no Telegram-specific types. ``telegram.py`` is the thin
transport adapter. ``service.py`` glues events to conversation logic and
persists the audit trail. ``webhook.py`` is the FastAPI ingress for
webhook mode; ``polling.py`` the getUpdates loop for polling mode.

Adjustments made through this channel are claimed hours: the grammar
parser and the conversation state machine carry the same coverage
discipline as the sessioniser (CLAUDE.md).
"""
