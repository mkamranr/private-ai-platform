"""Per-document access (E56): who may retrieve a restricted document.

A document is **open** (everyone who can use its knowledge base can retrieve it, as before
E56) or **restricted** to listed users and teams. Access is decided for a *reader*, the
identity a retrieval runs as:

* a person in the console or API: themselves and their teams;
* an agent run: the person it runs for (for an ``agent:`` call through ``/v1``, the API
  client's owner);
* a workflow run started by someone: that person. A scheduled or webhook run has nobody
  behind it and reads **open documents only**.

Enforced twice. Each indexed chunk carries an ``access`` label list, and every search
filters on the reader's labels, so a restricted passage is never even a candidate. Then
the PostgreSQL rows the hits resolve to are checked again, so a label that failed to
update (a vector store hiccup while restricting) cannot leak a passage: PostgreSQL is the
source of truth, and the labels are what make filtering fast and top-k honest.

Points indexed before E56 carry no label and count as open, which is what every document
was. A restricted document's labels always include ``restricted``, so its list is never
empty and never mistaken for "no label".

Superusers read everything. Holders of ``knowledge.manage`` see restricted documents
listed (they manage them) but, like everyone else, retrieve only what they are listed for.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.models.knowledge import DocumentAccessPrincipal

PUBLIC = "public"
RESTRICTED = "restricted"


def _label(principal_type: str, principal_id: uuid.UUID) -> str:
    return f"{principal_type}:{principal_id}"


def access_labels(restricted: bool, grants: Iterable[tuple[str, uuid.UUID]]) -> list[str]:
    """The ``access`` payload for a document's chunks."""
    if not restricted:
        return [PUBLIC]
    return [RESTRICTED, *sorted(_label(kind, pid) for kind, pid in grants)]


@dataclass(frozen=True, slots=True)
class DocumentReader:
    """The identity a retrieval runs as."""

    user_id: uuid.UUID | None = None
    team_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    #: Superusers: no access filter at all.
    unrestricted: bool = False

    @classmethod
    def open_only(cls) -> DocumentReader:
        """Nobody in particular: a scheduled or webhook workflow run."""
        return cls()

    def labels(self) -> tuple[str, ...]:
        """What a chunk's ``access`` list must contain one of for this reader."""
        found = [PUBLIC]
        if self.user_id is not None:
            found.append(_label(DocumentAccessPrincipal.USER, self.user_id))
        found.extend(_label(DocumentAccessPrincipal.TEAM, t) for t in sorted(self.team_ids))
        return tuple(found)

    def may_read(self, restricted: bool, grants: Iterable[tuple[str, uuid.UUID]]) -> bool:
        if self.unrestricted or not restricted:
            return True
        for kind, principal in grants:
            if kind == DocumentAccessPrincipal.USER and principal == self.user_id:
                return True
            if kind == DocumentAccessPrincipal.TEAM and principal in self.team_ids:
                return True
        return False
