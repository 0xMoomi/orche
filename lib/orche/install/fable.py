"""Patch the Fable orchestration rules: `~/.claude/fable/fable.md` is the source and
`~/.claude/fable/active.md` is the copy CLAUDE.md includes.

`fable.md` gets a guarded ORCHE block and the `Sonnet 5` -> `Sonnet 5.5` / `Haiku 4.5` -> `Haiku 5.5`
wording fix on model-routing lines (outside code spans and paths). `active.md` is patched the
same way only when it is byte-identical to the pre-patch `fable.md`; `empty.md` is never touched.
"""
import re

from . import has_block, insert_block, read_text, remove_block, write_file

STEP = "fable"
START, END = "<!-- >>> orche >>> -->", "<!-- <<< orche <<< -->"
REPLACEMENTS = (
    (re.compile(r"(?<![\w/.\-])Sonnet 5(?!\w|\.\d)"), "Sonnet 5", "Sonnet 5.5"),
    (re.compile(r"(?<![\w/.\-])Haiku 4\.5(?!\w|\.\d)"), "Haiku 4.5", "Haiku 5.5"),
)
ROUTING_MARKERS = ("->", "→", "라우팅", "모델", "routing", "model")
CODE_SPAN = re.compile(r"(`[^`\n]*`)")


def block(plugin_root) -> str:
    principles = plugin_root / "docs" / "principles.md"
    return "\n".join([
        START,
        "## ORCHE",
        "",
        "ORCHE rules supersede the routing table above where they conflict.",
        "",
        f"- Principles: `{principles}` (system principles P1-P6 and the ten working principles).",
        "- Hand work to a unit by running `orche dispatch` with the blocks on stdin (heredoc) or `--file <path>`,",
        "  a `---` line between blocks: one `KEY value` line per key, continuation lines indented by two spaces;",
        "  refusals print at once, so fix and re-run in the same turn. A block written in the reply is not sent.",
        "- Required keys: TO GOAL SCOPE CONTEXT ACCEPT VERIFY TIMEBOX FORBID REPORT TIER (optional ROLLBACK);",
        "  TIER is mechanical, standard or hard; a plain line that is not a key continues the field above.",
        "- Corrections: `AMEND <unit> rev <N>`, `SUPERSEDES rev <N-1>: <what>`, then only the keys that change;",
        "  halts: `STOP <unit> [reason]` (no keys but an optional TO).",
        "- Hooks deliver briefs and reports; done means a ledger row, not a claim.",
        "- Main ends every reply with one closing line: `RUNNING: <what runs>`, `OWNER: <decision or step,",
        "  default>` or `DONE: <result>`; only OWNER and DONE notify the owner.",
        END,
        "",
    ])


def _routing_lines(lines) -> set:
    """Indexes of lines in paragraphs that talk about model routing, outside fences and our block."""
    out, para, in_fence, in_block = set(), [], False, False

    def flush():
        if any(m in lines[i].lower() for i in para for m in ROUTING_MARKERS):
            out.update(para)
        para.clear()

    for i, line in enumerate(lines):
        bare = line.strip()
        if bare.startswith(START):
            flush()
            in_block = True
        if in_block:
            in_block = not bare.startswith(END)
            continue
        if bare.startswith("```") or bare.startswith("~~~"):
            flush()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not bare:
            flush()
        else:
            para.append(i)
    flush()
    return out


def replace_models(text):
    """(new text, [(line number, old, new)]) for the model wording fix."""
    lines = text.splitlines(keepends=True)
    changes = []
    for i in sorted(_routing_lines(lines)):
        parts = CODE_SPAN.split(lines[i])
        for j in range(0, len(parts), 2):  # odd indexes are code spans
            for pattern, old, new in REPLACEMENTS:
                parts[j], n = pattern.subn(new, parts[j])
                changes += [(i + 1, old, new)] * n
        lines[i] = "".join(parts)
    return "".join(lines), changes


def patched(ctx, text):
    fixed, changes = replace_models(text)
    return insert_block(fixed, block(ctx.plugin_root), START, END), changes


def install(ctx) -> None:
    fable = ctx.claude / "fable" / "fable.md"
    active = ctx.claude / "fable" / "active.md"
    if not fable.is_file():
        ctx.note(STEP, "skip", fable, "not found")
        return
    pre = read_text(fable)
    new, changes = patched(ctx, pre)
    for lineno, old, repl in changes:
        ctx.note(STEP, "replace", None, f"fable.md:{lineno} {old} -> {repl}")
    write_file(ctx, STEP, fable, new, "ORCHE block" + (f", {len(changes)} wording fix(es)" if changes else ""))
    if not active.is_file():
        ctx.note(STEP, "skip", active, "not found")
    elif active.read_bytes() == pre.encode("utf-8"):
        write_file(ctx, STEP, active, new, "mirrors fable.md")
    elif read_text(active) == new:
        ctx.note(STEP, "ok", active, "unchanged")
    else:
        ctx.note(STEP, "skip", active, "differs from fable.md (fable off or edited); left untouched")


def uninstall(ctx) -> None:
    for name in ("fable.md", "active.md"):
        path = ctx.claude / "fable" / name
        if not path.is_file() or not has_block(read_text(path), START, END):
            ctx.note(STEP, "ok", path, "no ORCHE block")
            continue
        ctx.note(STEP, "remove", None, f"ORCHE block from {name}")
        write_file(ctx, STEP, path, remove_block(read_text(path), START, END), "ORCHE block removed")
