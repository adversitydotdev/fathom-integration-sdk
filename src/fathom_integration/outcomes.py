"""What validate and check return."""

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Subject:
    """A failing account, host, or resource, shown on the result without opening the snapshot."""

    kind: str
    name: str
    external_id: str | None = None

    @classmethod
    def account(cls, name: str, external_id: str | None = None) -> "Subject":
        return cls("account", name, external_id)

    @classmethod
    def resource(cls, name: str, external_id: str | None = None) -> "Subject":
        return cls("resource", name, external_id)

    def to_json(self) -> dict[str, str]:
        out = {"kind": self.kind, "name": self.name}
        if self.external_id is not None:
            out["external_id"] = self.external_id
        return out


@dataclass(frozen=True)
class Outcome:
    outcome: Literal["pass", "fail", "error"]
    summary: str
    subjects: list[Subject] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "summary": self.summary[:2048],
            "subjects": [s.to_json() for s in self.subjects],
        }


def pass_(summary: str) -> Outcome:
    return Outcome("pass", summary)


def fail(summary: str, subjects: list[Subject] | None = None) -> Outcome:
    return Outcome("fail", summary, list(subjects or []))


def error(summary: str) -> Outcome:
    return Outcome("error", summary)


@dataclass(frozen=True)
class Identity:
    """What `connection.validate` reports: who the credential is, and what it may read."""

    identity: str
    granted_scopes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "granted_scopes": list(self.granted_scopes),
            "warnings": list(self.warnings),
        }
