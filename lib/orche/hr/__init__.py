"""HR: the one module that changes units and projects (SPEC §14.12).

Every transition — create a division or feature agent, wake, sleep, retire, stage change, freeze —
runs here end to end: its rules (creation limits, awake limit, stage gate, retired/onboarding
refusals), its Orca steps and its HR record (event -> registry.json -> HR-log line -> status line ->
notification). registry.json is written only under lib/orche/hr/. Callers resolve names through the
Roster and keep argument parsing and output: a transition returns (data, lines) or raises
Refused(code, msg), `code` being the CLI exit code. HR prints, on stderr, only what happens during a
transition: `[dry-run]` lines, warnings, retire's progress lines.
"""
from .audit import run as run_audit
from .book import Refused, correct, log_path
from .create import add_division, create_feature, onboard_project, plan_divisions
from .models import set_model
from .limits import awake, set_frozen, stage_gate
from .onboarding import session_name, session_owner
from .panes import project_pane
from .refresh import Refresh, refresh_project, stamp_version
from .retire import retire, retire_gone
from .transitions import set_stage, sleep, wake

__all__ = ["Refresh", "Refused", "add_division", "awake", "correct", "create_feature", "log_path", "onboard_project",
           "plan_divisions", "project_pane", "refresh_project", "retire", "retire_gone", "run_audit",
           "session_name", "session_owner", "set_frozen", "set_model", "set_stage", "sleep", "stage_gate", "stamp_version",
           "wake"]

