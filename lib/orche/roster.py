"""Who is this session, where is this cwd, who gets what (SPEC §14.9)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from . import registry, units

ENV = "ORCHE_SESSION"


def env_session(env=None) -> str | None:
    """$ORCHE_SESSION from `env` (a mapping; default os.environ at call time); None when unset or empty."""
    return (os.environ if env is None else env).get(ENV) or None


def actor(env=None) -> str:
    """Credit for events, hr-log, graveyard, audit rows, secret requests: env_session(env) or "user". Never the cwd."""
    return env_session(env) or "user"


@dataclass(frozen=True)
class Member:
    """A registered session: a project's Main, a division, or a feature agent.

    `==` ignores `entry` and `home`, so a live record and its retired twin compare equal; compare `.entry` with `is`.
    """
    session: str
    project: str
    kind: str
    name: str
    division: str | None
    entry: dict | None = field(default=None, compare=False)
    home: Path | None = field(default=None, compare=False)

    @property
    def role(self) -> str:
        """The word for messages: main, division or feature."""
        return {"div": "division", "feat": "feature"}.get(self.kind, self.kind)

    @property
    def owns_worktree(self) -> bool:
        """True for a division or feature agent."""
        return self.kind in ("div", "feat")

    @property
    def worktree(self) -> Path | None:
        """The recorded worktree (`repoId::` stripped, not resolved); None if unrecorded or main."""
        path = units.worktree_path((self.entry or {}).get("worktree", "")) if self.owns_worktree else ""
        return Path(path) if path else None

    @property
    def branch(self) -> str:
        """The record's branch, or ""."""
        return (self.entry or {}).get("branch") or ""

    @property
    def status(self) -> str:
        """The record's status, or ""."""
        return (self.entry or {}).get("status") or ""

    @property
    def event_unit(self) -> str | None:
        """events[].unit: "<division>" or "<division>/<feature>"; None for main."""
        if self.kind == "div":
            return self.name
        return f"{self.division}/{self.name}" if self.kind == "feat" else None

    @property
    def memory(self) -> Path:
        """The member's memory dir: <home>/.orche (Main), or <worktree>/.orche/<kind>/<name>."""
        if self.owns_worktree:
            return (self.worktree or Path(".")) / ".orche" / self.kind / self.name
        return (self.home or Path(".")) / ".orche"


class _Lazy:
    """Read-only attributes; one named field may be a zero-argument callable, resolved and cached on first read."""
    _fields: tuple[str, ...] = ()
    _lazy = ""

    def __init__(self, *values):
        for name, value in zip(self._fields, values, strict=True):
            object.__setattr__(self, "_" + name if name == self._lazy else name, value)

    def __getattr__(self, name):
        if name != self._lazy:
            raise AttributeError(name)
        value = self.__dict__["_" + name]
        value = value() if callable(value) else value
        object.__setattr__(self, name, value)
        return value

    def __setattr__(self, name, value):
        raise AttributeError(f"{type(self).__name__} is read-only")

    def _values(self):
        return tuple(getattr(self, name) for name in self._fields)

    def __eq__(self, other):
        return type(other) is type(self) and self._values() == other._values()

    def __hash__(self):
        return hash(self._values())

    def __repr__(self):
        return f"{type(self).__name__}({', '.join(f'{n}={v!r}' for n, v in zip(self._fields, self._values()))})"


class Place(_Lazy):
    """Where a cwd is: its project (D3) and the deepest unit worktree holding it.

    `project` (the `.orche/project` walk) is resolved on first read, so a unit-only lookup never walks.
    """
    project: str | None
    unit: Member | None
    _fields = ("project", "unit")
    _lazy = "project"


class Identity(_Lazy):
    """Who a session is: session None = unidentified; member None with a session = unregistered.

    `place` is resolved on first read, so an ORCHE_SESSION identity that never reads it touches no filesystem.
    """
    session: str | None
    member: Member | None
    place: Place
    _fields = ("session", "member", "place")
    _lazy = "place"


class Roster:
    """A read model over a validated registry dict; rebuild it after the registry changes.

    Do not change `reg` while a Roster is in use; build a new one.
    """

    def __init__(self, reg: dict):
        """Index members in registry order; realpath of project paths only."""
        self.reg = reg
        self._mains: dict[str, Member] = {}
        self._units: list[Member] = []
        heads: dict[str, Member] = {}
        for key, proj in reg.get("projects", {}).items():
            home = units.project_root(reg, key)
            if proj.get("main_session"):
                member = Member(proj["main_session"], key, "main", "", None, None, home)
                heads.setdefault(member.session, member)
                self._mains[key] = member
            for div_name, div in proj.get("divisions", {}).items():
                self._units.append(Member(div.get("session", ""), key, "div", div_name, div_name, div, home))
                for feat in div.get("features", []):
                    self._units.append(Member(feat.get("session", ""), key, "feat", feat.get("name", ""),
                                              div_name, feat, home))
        live: dict[str, Member] = {}
        retired: dict[str, Member] = {}
        for unit in self._units:
            if unit.session:
                (retired if unit.status == "retired" else live).setdefault(unit.session, unit)
        self._members = {**retired, **live, **heads}

    def member(self, session) -> Member | None:
        """Exact name; Main before units; live before retired."""
        return self._members.get(session) if session else None

    def main(self, project) -> Member | None:
        """The project's Main; None for an unknown key or "" main_session."""
        return self._mains.get(project) if project else None

    def sessions(self) -> list[str]:
        """Every registered name, sorted, retired included."""
        return sorted(self._members)

    @cached_property
    def _index(self) -> tuple[list[tuple[str, Member]], dict[str, str]]:
        """One pass: (unit worktree realpath, unit) in registry order, and realpath -> project key."""
        by_project: dict[str, list[Member]] = {}
        for unit in self._units:
            by_project.setdefault(unit.project, []).append(unit)
        worktrees: list[tuple[str, Member]] = []
        owners: dict[str, str] = {}
        for key, proj in self.reg.get("projects", {}).items():
            if proj.get("path"):
                owners.setdefault(units.real(proj["path"]), key)
            for unit in by_project.get(key, ()):
                if unit.worktree:
                    root = units.real(unit.worktree)
                    worktrees.append((root, unit))
                    owners.setdefault(root, key)
        return worktrees, owners

    @property
    def _worktrees(self) -> list[tuple[str, Member]]:
        return self._index[0]

    @property
    def _owners(self) -> dict[str, str]:
        return self._index[1]

    def place(self, cwd) -> Place:
        """D3: the cwd's project and deepest unit (live beats retired at equal depth)."""
        if not cwd:
            return Place(None, None)
        here = units.real(cwd)
        best, best_len = None, -1
        for root, unit in self._worktrees:
            if not (here == root or here.startswith(root.rstrip(os.sep) + os.sep)):
                continue
            if len(root) > best_len or (len(root) == best_len and best.status == "retired"
                                         and unit.status != "retired"):
                best, best_len = unit, len(root)
        return Place(lambda: self._project_of(Path(here)), best)

    def _project_of(self, here: Path) -> str | None:
        for directory in (here, *here.parents):
            key = self._owners.get(str(directory))
            if key:
                return key
            marker = directory / ".orche" / "project"
            if marker.is_file():
                try:
                    key = marker.read_text(encoding="utf-8").strip()
                except OSError:
                    return None
                return key if key in self.reg.get("projects", {}) else None
        return None

    def identify(self, cwd, env=None) -> Identity:
        """D2: a non-empty env is authoritative, else the place's unit when it has a session, else unidentified.

        Without ORCHE_SESSION the member is the record at the worktree (possibly a retired twin); callers use
        `who.member` and never look the session up again.
        """
        session = env_session(env)
        if session:
            return Identity(session, self.member(session), lambda: self.place(cwd))
        place = self.place(cwd)
        if place.unit is not None and place.unit.session:
            return Identity(place.unit.session, place.unit, place)
        return Identity(None, None, place)

    def recipient(self, session, sender=None) -> str | None:
        """`sender` (who sent the unit's brief) when it is a live Main or division of the same project other
        than `session`; else feature -> its division's session (Main if ""), else Main; None if unknown or no Main."""
        member = self.member(session)
        if member is None:
            return None
        frm = self.member(sender) if sender and sender != session else None
        if (frm is not None and frm.project == member.project and frm.kind in ("main", "div")
                and frm.status != "retired"):
            return frm.session
        if member.kind == "feat":
            div = self.reg["projects"][member.project].get("divisions", {}).get(member.division) or {}
            if div.get("session"):
                return div["session"]
        main = self.main(member.project)
        return main.session if main else None

    def resolve_to(self, text, home) -> Member | None:
        """D9: exact session, else a division of `home`, else a project key or alias -> its Main."""
        text = (text or "").strip()
        if not text:
            return None
        found = self.member(text)
        if found is not None:
            return found
        if home:
            proj = self.reg.get("projects", {}).get(home, {})
            if text in registry.divisions_of(proj):
                session = (proj.get("divisions", {}).get(text) or {}).get("session")
                if session:
                    return self.member(session)
        hit = registry.project(self.reg, text)
        return self.main(hit[0]) if hit else None
