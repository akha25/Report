"""Roster: decides setter / closer / unclassified. Matching is by email first, then exact name or alias.
Report rows always keep the name exactly as it appears in the source (CSV or HubSpot)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .csv_parser import normalise_name
from .models import RosterMember, log_event

ROLES = ("setter", "closer", "unclassified")
SEED_FILE = Path(__file__).with_name("seed_roster.json")


def seed_roster(session: Session) -> int:
    if session.scalar(select(RosterMember.id).limit(1)) is not None:
        return 0
    data = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    for m in data["members"]:
        session.add(RosterMember(
            name=m["name"], email=(m.get("email") or "").lower() or None, role=m.get("role", "unclassified"),
            team=m.get("team", ""), aliases=m.get("aliases", []), notes=m.get("notes", ""),
            hubspot_owner_id=m.get("hubspot_owner_id", ""),
        ))
    log_event(session, "info", "roster", f"Roster seeded with {len(data['members'])} people.")
    return len(data["members"])


@dataclass
class RosterIndex:
    by_email: dict[str, RosterMember]
    by_name: dict[str, RosterMember]
    by_owner: dict[str, RosterMember]

    @classmethod
    def load(cls, session: Session) -> "RosterIndex":
        members = session.scalars(select(RosterMember)).all()
        by_email, by_name, by_owner = {}, {}, {}
        for m in members:
            if m.email:
                by_email[m.email.lower()] = m
            for n in [m.name, *(m.aliases or [])]:
                by_name.setdefault(normalise_name(n), m)
            if m.hubspot_owner_id:
                by_owner[str(m.hubspot_owner_id)] = m
        return cls(by_email, by_name, by_owner)

    def match(self, name: str | None, email: str | None = None, owner_id: str | None = None):
        """Return (member, method) or (None, None)."""
        if owner_id and str(owner_id) in self.by_owner:
            return self.by_owner[str(owner_id)], "hubspot_id"
        if email and email.lower() in self.by_email:
            return self.by_email[email.lower()], "email"
        if name:
            m = self.by_name.get(normalise_name(name))
            if m is not None:
                # Never let a name match override a different email: that would merge two people.
                if email and m.email and m.email.lower() != email.lower():
                    return None, None
                return m, "name"
        return None, None


def register_new_people(session: Session, rows: list[dict]) -> list[str]:
    """Add people seen in a CSV who aren't on the roster yet, as 'unclassified'."""
    index = RosterIndex.load(session)
    added = []
    for row in rows:
        member, _ = index.match(row["name"], row.get("email"))
        if member is None and row.get("email") and row["email"] not in index.by_email:
            m = RosterMember(name=row["name"], email=row["email"], role="unclassified",
                             notes="Added automatically from a FreJun CSV")
            session.add(m)
            index.by_email[row["email"]] = m
            added.append(row["name"])
    if added:
        log_event(session, "warning", "roster",
                  f"{len(added)} new people found in the CSV and added as unclassified: {', '.join(added)}",
                  people=added)
    return added
