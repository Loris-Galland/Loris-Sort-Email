"""Email classification via Ollama (local LLM — no API key, no data leaves the machine).

Calls the Ollama REST API on localhost:11434 with batches of email metadata.
Writes category, confidence, and reason back to the SQLite database.
"""

# Phase 3 — implemented in the classification phase

# Valid categories — must match the prompt exactly
VALID_CATEGORIES = {
    "NEWSLETTER",
    "PROMO",
    "NOTIFICATION",
    "FACTURE",
    "PROFESSIONNEL",
    "PERSONNEL",
    "SPAM",
    "A_TRAITER",
}

# Rules enforced in post-processing (on top of the LLM prompt):
#   - importance == "high"    -> only A_TRAITER or PROFESSIONNEL
#   - has_attachments == True -> never NEWSLETTER or SPAM
