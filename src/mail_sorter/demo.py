"""Fake data and a fake Graph client, to try the dashboard without an Outlook account."""

import random
from datetime import datetime, timedelta

DEMO_DB_PATH = "./demo.db"

# (sender name, address, category, subjects)
_SENDERS = [
    (
        "Riot Games",
        "noreply@riotgames.com",
        "NOTIFICATION",
        ["Your weekly match recap", "New skins in the store"],
    ),
    (
        "League of Legends",
        "news@leagueoflegends.com",
        "NEWSLETTER",
        ["Patch notes 26.4", "Worlds is coming"],
    ),
    (
        "Steam",
        "noreply@steampowered.com",
        "PROMO",
        ["Summer sale: up to 90% off", "A game on your wishlist is on sale"],
    ),
    (
        "Amazon",
        "auto-confirm@amazon.fr",
        "FACTURE",
        ["Votre commande a été expédiée", "Votre facture Amazon"],
    ),
    (
        "Zalando",
        "info@zalando.fr",
        "PROMO",
        ["-30% sur toute la collection", "Nouveautés de la semaine"],
    ),
    (
        "LinkedIn",
        "messages-noreply@linkedin.com",
        "NOTIFICATION",
        ["Vous avez 3 nouvelles invitations", "Qui a consulté votre profil"],
    ),
    (
        "Le Monde",
        "newsletter@lemonde.fr",
        "NEWSLETTER",
        ["La matinale du Monde", "Les essentiels de la semaine"],
    ),
    ("Free Mobile", "facture@free-mobile.fr", "FACTURE", ["Votre facture est disponible"]),
    (
        "Université",
        "scolarite@univ-example.fr",
        "PROFESSIONNEL",
        ["Planning des examens", "Inscription pédagogique"],
    ),
    ("Thomas", "thomas.martin@gmail.com", "PERSONNEL", ["Ce week-end ?", "Photos de la soirée"]),
    ("Prize Center", "winner@lucky-prize.xyz", "SPAM", ["Vous avez gagné un iPhone"]),
    ("Banque", "contact@mabanque.fr", "A_TRAITER", ["Action requise sur votre compte"]),
]


def generate_emails(count: int = 1000, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    now = datetime(2026, 10, 1)
    weights = [14, 8, 12, 6, 12, 10, 8, 3, 4, 2, 3, 2]
    emails = []
    for i in range(count):
        name, address, category, subjects = rng.choices(_SENDERS, weights=weights)[0]
        emails.append(
            {
                "message_id": f"demo_{i:05d}",
                "subject": rng.choice(subjects),
                "sender_email": address,
                "sender_name": name,
                "received_at": now
                - timedelta(days=rng.randint(0, 900), minutes=rng.randint(0, 1440)),
                "is_read": rng.random() < 0.6,
                "has_attachments": category == "FACTURE" and rng.random() < 0.5,
                "importance": "high" if category == "A_TRAITER" else "normal",
                "size_bytes": rng.randint(4_000, 120_000),
                "category": category,
                "confidence": rng.randint(55, 99),
                "reason": "demo data",
                "status": "classified",
            }
        )
    return emails


class DemoGraph:
    """Stands in for GraphClient: every move succeeds and IDs never change."""

    def ensure_folder(self, path: str) -> str:
        return path

    def move_messages(self, message_ids, destination, on_progress=None):
        if on_progress:
            on_progress(len(message_ids))
        return {msg_id: msg_id for msg_id in message_ids}
