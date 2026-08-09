"""Read the command surface out of ``cogs/`` without importing anything.

The command sheet used to be a hand-written list of what the bot was *going* to
have. That was right for planning and wrong forever after: the code moved, the
sheet did not, and nothing told anyone. This module removes the possibility --
the sheet is now generated from the same source the bot runs.

**Nothing here imports discord.py, opens the database, or reads the token.** It
is pure ``ast`` over the files on disk, so it is safe to run against a checkout
whose bot is live in a server.

What it understands:

* ``@commands.command`` / ``group`` -- prefix-only by construction.
* ``@commands.hybrid_command`` / ``hybrid_group`` -- both surfaces, unless
  ``with_app_command=False`` demotes them to prefix-only.
* ``@<group>.command(...)`` -- a subcommand, resolved to ``parent child`` by
  matching the decorator's attribute name against the group function's name.
* The permission decorators in ``core/permissions.py``, plus discord.py's own
  ``has_permissions``.

Buttons and modals are detected by looking for the constructs in the function
body. That is a heuristic and is labelled as one on the sheet; everything else
here is read straight off the syntax tree.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

#: Cog class name -> the category the sheet groups it under. Two cogs share
#: "Leveling & Economy" and two share "Server Config", which is deliberate:
#: the split into separate cogs is an implementation decision, and the sheet is
#: read by people who think in features.
COG_CATEGORIES: dict[str, str] = {
    "Moderation": "Moderation",
    "Automod": "Automod",
    "AntiRaid": "Anti-raid & Verification",
    "Roles": "Roles & Reaction Roles",
    "Logging": "Logging",
    "Leveling": "Leveling & Economy",
    "Economy": "Leveling & Economy",
    "Utility": "Utility",
    "Fun": "Fun/Social",
    "Giveaways": "Giveaways & Events",
    "Tags": "Tags & Custom Commands",
    "ServerConfig": "Server Config",
    "Importer": "Server Config",
    "Owner": "Owner / Operator",
}

#: Permission decorator -> what to print. Ordered most restrictive first so a
#: command carrying two of them reports the one that actually gates it.
PERMISSION_LABELS: list[tuple[str, str]] = [
    ("root_only", "Root owner"),
    ("owner_only", "Bot owner"),
    ("admin_only", "Administrator (or an admin role)"),
    ("mod_only", "Moderator (or a mod role)"),
]

#: Decorators that say nothing about who may run a command.
IGNORED_DECORATORS = frozenset({
    "guild_only", "describe", "autocomplete", "cooldown", "max_concurrency",
    "rename", "choices", "before_invoke", "after_invoke",
})

_BUTTON_MARKERS = ("confirm(", "View(", "Paginator(", "discord.ui.Button",
                   "add_item(", "paginate_lines(")
_MODAL_MARKERS = ("send_modal(", "Modal(")


@dataclass(slots=True)
class Command:
    """One registered command, as the source declares it."""

    name: str
    parent: str = ""
    cog: str = ""
    category: str = ""
    aliases: list[str] = field(default_factory=list)
    hybrid: bool = False
    with_app_command: bool = True
    is_group: bool = False
    fallback: str = ""
    description: str = ""
    arguments: str = ""
    permission: str = ""
    buttons: bool = False
    modal: bool = False
    source_file: str = ""
    lineno: int = 0
    #: Every way a user can actually type this command, including parent
    #: aliases and a group's fallback. Filled in by :func:`collect`.
    spellings: set[str] = field(default_factory=set)

    @property
    def qualified(self) -> str:
        return f"{self.parent} {self.name}".strip()

    @property
    def display(self) -> str:
        return f"?{self.qualified}"

    @property
    def surface(self) -> str:
        """What the sheet's "Slash / Prefix / Both" column should say."""
        return "Both" if (self.hybrid and self.with_app_command) else "Prefix"


def _literal(node: ast.AST) -> object:
    """Best-effort constant folding. Returns ``None`` for anything dynamic."""
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return None


def _decorator_parts(node: ast.AST) -> tuple[str, str]:
    """Split a decorator into ``(owner, attribute)``.

    ``commands.hybrid_group(...)``  -> ``("commands", "hybrid_group")``
    ``permissions.admin_only()``    -> ``("permissions", "admin_only")``
    ``giveaway.command(...)``       -> ``("giveaway", "command")``
    """
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        owner = target.value.id if isinstance(target.value, ast.Name) else ""
        return owner, target.attr
    if isinstance(target, ast.Name):
        return "", target.id
    return "", ""


def _kwargs(node: ast.AST) -> dict[str, object]:
    if not isinstance(node, ast.Call):
        return {}
    return {kw.arg: _literal(kw.value) for kw in node.keywords if kw.arg}


def _docline(func: ast.AsyncFunctionDef | ast.FunctionDef) -> str:
    """The first paragraph of the docstring, as one line."""
    raw = ast.get_docstring(func) or ""
    if not raw:
        return ""
    first = raw.split("\n\n", 1)[0]
    return " ".join(first.split())


def _arguments(func: ast.AsyncFunctionDef | ast.FunctionDef) -> str:
    """Render the parameter list the way a user types it.

    ``<required>`` and ``[optional=default]``, skipping ``self`` and ``ctx``.
    Keyword-only parameters are discord.py's consume-rest, so they read exactly
    like a positional one to whoever is typing the command.
    """
    args = func.args
    positional = list(args.posonlyargs) + list(args.args)
    defaults: dict[str, ast.AST] = {}
    if args.defaults:
        for name, default in zip(positional[-len(args.defaults):], args.defaults):
            defaults[name.arg] = default
    for name, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is not None:
            defaults[name.arg] = default

    parts: list[str] = []
    for arg in positional + list(args.kwonlyargs):
        if arg.arg in ("self", "ctx"):
            continue
        if arg.arg in defaults:
            rendered = _literal(defaults[arg.arg])
            if rendered is None or rendered == "":
                parts.append(f"[{arg.arg}]")
            else:
                parts.append(f"[{arg.arg}={rendered}]")
        else:
            parts.append(f"<{arg.arg}>")
    return " ".join(parts) if parts else "-"


def _permission(decorators: list[ast.AST]) -> str:
    """The gate on a command, in words."""
    names = set()
    explicit: list[str] = []
    for node in decorators:
        owner, attr = _decorator_parts(node)
        names.add(attr)
        if attr in ("has_permissions", "has_guild_permissions"):
            for kw in getattr(node, "keywords", []):
                if kw.arg and _literal(kw.value) is True:
                    explicit.append(kw.arg.replace("_", " ").title())

    for key, label in PERMISSION_LABELS:
        if key in names:
            return label
    if explicit:
        return " + ".join(explicit)
    return "None"


def _body_flags(source: str, func: ast.AST) -> tuple[bool, bool]:
    """Whether the body builds buttons or opens a modal. Heuristic."""
    segment = ast.get_source_segment(source, func) or ""
    return (
        any(marker in segment for marker in _BUTTON_MARKERS),
        any(marker in segment for marker in _MODAL_MARKERS),
    )


def parse_cog(path: Path) -> list[Command]:
    """Every command declared in one cog file."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    commands: list[Command] = []
    # Function name -> the command name it registers, so a subcommand decorated
    # with @tiktok_group.command() resolves to its parent's *command* name
    # rather than the Python identifier.
    group_names: dict[str, str] = {}

    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        for func in cls.body:
            if not isinstance(func, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue

            registration = None
            owner_name = ""
            for node in func.decorator_list:
                owner, attr = _decorator_parts(node)
                if owner == "commands" and attr in (
                    "command", "group", "hybrid_command", "hybrid_group"
                ):
                    registration, owner_name = node, ""
                    break
                if owner in group_names and attr in ("command", "group"):
                    registration, owner_name = node, owner
                    break
            if registration is None:
                continue

            _, kind = _decorator_parts(registration)
            options = _kwargs(registration)
            name = str(options.get("name") or func.name)
            aliases = options.get("aliases") or []
            buttons, modal = _body_flags(source, func)

            command = Command(
                name=name,
                parent=group_names.get(owner_name, ""),
                cog=cls.name,
                category=COG_CATEGORIES.get(cls.name, cls.name),
                aliases=[str(a) for a in aliases] if isinstance(aliases, list) else [],
                hybrid=kind.startswith("hybrid"),
                with_app_command=options.get("with_app_command", True) is not False,
                is_group=kind.endswith("group"),
                fallback=str(options.get("fallback") or ""),
                description=_docline(func),
                arguments=_arguments(func),
                permission=_permission(func.decorator_list),
                buttons=buttons,
                modal=modal,
                source_file=path.name,
                lineno=func.lineno,
            )
            if command.is_group:
                group_names[func.name] = command.qualified
            commands.append(command)

    # Subcommands take their surface from the parent. ``@group.command()`` on a
    # HybridGroup produces a hybrid subcommand even though the decorator is
    # spelled ``command``, and conversely no subcommand of a prefix-only group
    # is ever registered as a slash command whatever its own decorator says.
    groups = {c.qualified: c for c in commands if c.is_group}
    for command in commands:
        parent = groups.get(command.parent)
        if parent is None:
            continue
        command.hybrid = parent.hybrid
        command.with_app_command = (
            parent.hybrid and parent.with_app_command and command.with_app_command
        )

    return commands


def _fill_spellings(commands: list[Command]) -> None:
    """Record every name a command actually answers to.

    Needed because the old design spec wrote commands the way a user types
    them, not the way the code declares them: ``?rr mode`` is the alias of
    ``?reactionrole mode``, and ``?giveaway list`` is what bare ``?giveaway``
    does via the group's fallback. Matching on the declared name alone reports
    both as missing, which is worse than useless -- it is a false alarm on a
    sheet whose whole job is to be trusted.
    """
    groups = {c.qualified: c for c in commands if c.is_group}

    for command in commands:
        names = [command.name, *command.aliases]
        parent = groups.get(command.parent)
        parents = [""] if parent is None else [parent.name, *parent.aliases]

        for prefix in parents:
            for name in names:
                # A top-level command has no parent, so there is no separating
                # space -- "?ban", never "? ban".
                command.spellings.add(f"?{prefix} {name}" if prefix else f"?{name}")

        # A group's fallback is reachable both as the bare group and spelled
        # out, and different bots' docs pick different halves of that.
        if command.is_group and command.fallback:
            for prefix in [command.name, *command.aliases]:
                command.spellings.add(f"?{prefix} {command.fallback}")


def collect(project_root: Path) -> list[Command]:
    """Every command in the project, sorted by category then name."""
    cogs_dir = project_root / "cogs"
    found: list[Command] = []
    for path in sorted(cogs_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        found.extend(parse_cog(path))

    _fill_spellings(found)

    order = list(COG_CATEGORIES.values())

    def sort_key(command: Command) -> tuple[int, str]:
        try:
            rank = order.index(command.category)
        except ValueError:
            rank = len(order)
        return rank, command.qualified

    return sorted(found, key=sort_key)


def summarise(commands: list[Command]) -> dict[str, int]:
    """Counts the sheet and the README both quote."""
    top_level_slash = {
        c.qualified for c in commands
        if not c.parent and c.hybrid and c.with_app_command
    }
    return {
        "total": len(commands),
        "groups": sum(1 for c in commands if c.is_group),
        "both": sum(1 for c in commands if c.surface == "Both"),
        "prefix_only": sum(1 for c in commands if c.surface == "Prefix"),
        "top_level_slash": len(top_level_slash),
    }


if __name__ == "__main__":  # pragma: no cover - a quick look from the shell
    root = Path(__file__).resolve().parent.parent
    everything = collect(root)
    for entry in everything:
        print(f"{entry.display:<34} {entry.surface:<7} {entry.permission:<34} "
              f"{entry.source_file}:{entry.lineno}")
    print()
    for key, value in summarise(everything).items():
        print(f"{key:>16}: {value}")
