"""DSPy tool construction and guidance sections for Agent Skills."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ..guide import names_in
from ..utils.dspy import load_tool
from .registry import _Registry
from .sandbox import (
    DEFAULT_INTERPRETERS,
    DEFAULT_SCRIPT_TIMEOUT,
    SandboxName,
    ScriptSandbox,
)
from .skill import (
    DEFAULT_MAX_CATALOG_CHARS,
    DEFAULT_MAX_RESOURCE_CHARS,
    Skill,
    _catalog,
    _discover,
)

if TYPE_CHECKING:
    from dspy import Tool

_MODES = ("search", "code")

_GUIDANCE = """## Agent Skills

Skills are folders of instructions, scripts, and reference files. The catalog
below lists every installed skill; activate one before following it.

- `{list_skills}` — names, descriptions, bundled files, and any declared
  `allowed-tools`.
- `{activate_skill}` — load one skill's full SKILL.md instructions.
- `{read_skill_resource}` — read a file from a skill's references/ or assets/.
- `{run_skill_script}` — run a file from a skill's scripts/ in a sandbox."""

_GUIDANCE_NO_SCRIPTS = _GUIDANCE.rsplit("\n", 1)[0]

_HOST_RULES = (
    "A skill's instructions may also call for host commands (for example "
    "`npx skills find ...` or `gh skill search ...`): run them only with a "
    "shell or CLI tool you were given. If you were given none, name the exact "
    "command and tell the user to run it; never claim you ran a command and "
    "never invent its output, search results, install counts, or repository "
    "names."
)

_EXECUTION_NOTE = (
    "Run a skill through its tools; never simulate a result. `{run_skill_script}` "
    "starts one file from `scripts/` on the machine inside a sandbox, and "
    "nothing else bundled with a skill can be executed. " + _HOST_RULES
)

_EXECUTION_NOTE_NO_SCRIPTS = (
    "Run a skill through its tools; never simulate a result. Bundled scripts "
    "cannot be executed here: script running is disabled for this catalog, so "
    "a skill that needs a script can only be followed by hand. " + _HOST_RULES
)

_HOST_TOOLS_NOTE = "Host command tools available to you: {names}."
_NETWORK_TOOLS_NOTE = (
    "Tools available to read a URL a skill points at (no shell needed): {names}."
)

#: Families whose tools can run a command a skill's instructions call for.
_HOST_FAMILIES = frozenset({"shell", "commands"})
#: Families whose tools can read a page a skill points at.
_NETWORK_FAMILIES = frozenset({"web", "browser"})

_LIST_DESC = (
    "List the installed agent skills with their descriptions and bundled "
    "scripts. Call this first when a task might match a skill, then call "
    "activate_skill before following its instructions."
)
_ACTIVATE_DESC = (
    "Load the full SKILL.md instructions for one skill, plus the scripts, "
    "references, and assets it bundles. Call it as soon as the task matches a "
    "skill description; then follow the returned instructions."
)
_READ_DESC = (
    "Read one reference or asset file bundled with a skill. Use the relative "
    "path shown by activate_skill, for example 'references/forms.md'. Paths "
    "outside the skill folder are rejected."
)
_RUN_DESC = (
    "Run one script bundled in a skill's scripts/ folder. The script runs "
    "with an allowlisted interpreter inside the skill sandbox; arguments are "
    "passed as an argv list, never through a shell. Run it with ['--help'] "
    "first when its usage is unclear."
)
_CODE_LIST_DESC = (
    "List the installed agent skills as a list of {name, description, "
    "allowed_tools, scripts, references, assets} dicts, sorted by name."
)
_CODE_ACTIVATE_DESC = (
    "Load one skill's full SKILL.md instructions as a dict with name, "
    "description, path, instructions, allowed_tools, scripts, references, "
    "assets, license, compatibility, and metadata."
)
_CODE_READ_DESC = (
    "Read one reference or asset file from a skill and return {path, "
    "content, truncated, error}."
)
_CODE_RUN_DESC = (
    "Run one script from a skill's scripts/ folder and return {script, "
    "exit_code, timed_out, elapsed, output, truncated, sandbox}."
)


def _tool_names(prefix: str) -> dict[str, str]:
    """Tool names for one ``name_prefix``."""
    return {
        "list_skills": f"{prefix}list_skills",
        "activate_skill": f"{prefix}activate_skill",
        "read_skill_resource": f"{prefix}read_skill_resource",
        "run_skill_script": f"{prefix}run_skill_script",
    }


def _build_tools(
    registry: _Registry,
    *,
    mode: Literal["search", "code"],
    names: Mapping[str, str],
    scripts: bool,
    tool_cls: Any,
) -> list[Tool]:
    def list_skills() -> Any:
        """List the skills installed in the configured skill folders."""
        return registry.list_text() if mode == "search" else registry.list_records()

    def activate_skill(skill: str) -> Any:
        """Load one skill's full SKILL.md instructions."""
        return (
            registry.activate_text(skill)
            if mode == "search"
            else registry.activate_record(skill)
        )

    def read_skill_resource(skill: str, path: str) -> Any:
        """Read one reference or asset file bundled with a skill."""
        return (
            registry.read_text(skill, path)
            if mode == "search"
            else registry.read_record(skill, path)
        )

    def run_skill_script(skill: str, script: str, args: list[str] | None = None) -> Any:
        """Run one script from a skill's scripts/ folder."""
        return (
            registry.run_text(skill, script, args)
            if mode == "search"
            else registry.run_record(skill, script, args)
        )

    if mode == "search":
        list_desc, activate_desc, read_desc, run_desc = (
            _LIST_DESC,
            _ACTIVATE_DESC,
            _READ_DESC,
            _RUN_DESC,
        )
    else:
        list_desc, activate_desc, read_desc, run_desc = (
            _CODE_LIST_DESC,
            _CODE_ACTIVATE_DESC,
            _CODE_READ_DESC,
            _CODE_RUN_DESC,
        )

    tools: list[Tool] = [
        tool_cls(list_skills, name=names["list_skills"], desc=list_desc),
        tool_cls(activate_skill, name=names["activate_skill"], desc=activate_desc),
        tool_cls(
            read_skill_resource,
            name=names["read_skill_resource"],
            desc=read_desc,
        ),
    ]
    if scripts:
        tools.append(
            tool_cls(
                run_skill_script,
                name=names["run_skill_script"],
                desc=run_desc,
            )
        )
    return tools


class SkillTools(list):
    """List of ``dspy.Tool`` objects backed by one or more skill folders.

    Behaves like a normal list, so it can be passed as ``tools=`` or
    concatenated with other tools. ``skills`` holds the discovered
    :class:`Skill` objects and ``errors`` the skipped directories.

    Pass it to :func:`dspy_agent_tools.Patch` on an existing ``dspy.ReAct``,
    ``dspy.ReActV2``, or ``dspy.RLM`` agent to append these tools and inject
    the skill catalog into the instructions the model sees. It implements that
    function's ``section`` protocol: ``family`` names the tool family and
    :meth:`section` renders the guidance that replaces the family template.
    """

    #: Tool family used by :func:`dspy_agent_tools.Patch` (see guide.py).
    family = "skills"

    def __init__(
        self,
        tools: Sequence[Any],
        *,
        skills: Sequence[Skill],
        errors: Sequence[str],
        catalog: str,
        scripts: bool,
        names: Mapping[str, str] | None = None,
        mode: Literal["search", "code"] = "search",
    ) -> None:
        super().__init__(tools)
        self.skills = tuple(skills)
        self.errors = tuple(errors)
        self.scripts = scripts
        self._mode = mode
        self._catalog = catalog
        names = dict(names) if names is not None else _tool_names("")
        discovered = {tool.name for tool in tools}
        present = {key: value for key, value in names.items() if value in discovered}
        self._names = present or names

    def catalog(self) -> str:
        """The ``<available_skills>`` XML block injected by :meth:`Patch`."""
        return self._catalog

    def _guidance(self) -> str:
        block = _GUIDANCE if self.scripts else _GUIDANCE_NO_SCRIPTS
        return block.format(**self._names)

    def _execution_note(self, tools: Sequence[Any]) -> str:
        template = _EXECUTION_NOTE if self.scripts else _EXECUTION_NOTE_NO_SCRIPTS
        parts = [template.format(**self._names)]
        found = names_in(tools, _HOST_FAMILIES)
        if found:
            parts.append(
                _HOST_TOOLS_NOTE.format(names=", ".join(f"`{name}`" for name in found))
            )
        fetchers = names_in(tools, _NETWORK_FAMILIES)
        if fetchers:
            parts.append(
                _NETWORK_TOOLS_NOTE.format(
                    names=", ".join(f"`{name}`" for name in fetchers)
                )
            )
        return "\n\n".join(parts)

    def section(self, tools: Sequence[Any] = ()) -> str:
        """Guidance, execution rules, and the catalog for an agent with ``tools``.

        Pass the agent's full tool list so the model is told which tools it
        has that can run a command or read a page a skill points at. This is
        the ``skills`` section :func:`dspy_agent_tools.Patch` injects.
        """
        return "\n\n".join(
            [self._guidance(), self._execution_note(tools), self._catalog]
        )


def Skills(
    *roots: str | Path,
    mode: Literal["search", "code"] = "search",
    name_prefix: str = "",
    strict: bool = False,
    scripts: bool = True,
    sandbox: SandboxName = "auto",
    timeout: float = DEFAULT_SCRIPT_TIMEOUT,
    allow_network: bool = False,
    allow_write: bool = False,
    interpreters: Sequence[str] = DEFAULT_INTERPRETERS,
    env: Mapping[str, str] | None = None,
    max_output_chars: int = 0,
    max_resource_chars: int = DEFAULT_MAX_RESOURCE_CHARS,
    max_catalog_chars: int = DEFAULT_MAX_CATALOG_CHARS,
) -> SkillTools:
    """Load Agent Skills folders and expose them as DSPy tools.

    Each root is scanned for skill directories: a folder containing a
    ``SKILL.md`` with ``name`` and ``description`` frontmatter. A root that is
    itself a skill is accepted too. Invalid skills are skipped and recorded
    on ``SkillTools.errors`` unless ``strict=True``.

    Args:
        roots: One or more folders that house the skills, for example
            ``Skills("~/.agents/skills", ".agents/skills")``.
        mode: ``"search"`` (default) returns text for ``dspy.ReAct`` and
            ``dspy.ReActV2``; ``"code"`` returns dictionaries for
            ``dspy.RLM``.
        name_prefix: Prepended to every tool name, for example
            ``name_prefix="skill_"``. It must form a valid Python identifier,
            so ``"sk-"`` is rejected and ``"sk_"`` is not.
        strict: Raise ``ValueError`` for the first invalid skill instead of
            skipping it.
        scripts: Include ``run_skill_script``. When ``False``, no script is
            ever executed and the sandbox is not configured.
        sandbox: OS sandbox for scripts: ``"auto"`` (default) uses bwrap,
            firejail, or sandbox-exec when installed and falls back to
            process confinement; ``"required"`` refuses the fallback; a
            backend name forces one; ``"off"`` skips the OS layer.
        timeout: Script deadline in seconds (default 30). The whole process
            group is killed when it expires.
        allow_network: Let scripts reach the network. Off by default; the
            OS sandboxes deny it.
        allow_write: Let scripts write inside the skill folder. Off by
            default; a throwaway temp directory is always writable.
        interpreters: Interpreter families scripts may use. Defaults to
            python, bash, sh, node, deno, ruby, and perl.
        env: Extra environment variables for scripts. The environment is
            replaced, not extended, so ambient secrets are never inherited.
        max_output_chars: Cap one script result (``0`` keeps everything).
        max_resource_chars: Cap one ``read_skill_resource`` result
            (``0`` keeps everything).
        max_catalog_chars: Cap the ``<available_skills>`` block injected by
            ``Patch`` (``0`` keeps every skill).

    Returns:
        A :class:`SkillTools` list with ``list_skills``, ``activate_skill``,
        ``read_skill_resource``, and (unless ``scripts=False``)
        ``run_skill_script``.

    Examples:
        >>> from dspy_agent_tools import Skills
        >>> skills = Skills("~/.agents/skills")
        >>> agent = dspy.ReAct("question -> answer", tools=skills)

        >>> agent = dspy.ReAct("question -> answer", tools=[bash_exec])
        >>> dspy_agent_tools.Patch(agent, tools=Skills("./skills"))

        >>> rlm = dspy.RLM("task -> report", tools=Skills("./skills", mode="code"))
    """
    if mode not in _MODES:
        raise ValueError(f"Unknown mode {mode!r}: expected 'search' or 'code'")
    if not roots:
        raise ValueError("Skills() needs at least one skill folder")
    if max_resource_chars < 0:
        raise ValueError("max_resource_chars must be >= 0")
    if max_catalog_chars < 0:
        raise ValueError("max_catalog_chars must be >= 0")

    paths = [Path(root).expanduser() for root in roots]
    names = _tool_names(name_prefix)
    if scripts is False:
        names.pop("run_skill_script")
    invalid = sorted(name for name in names.values() if not name.isidentifier())
    if invalid:
        raise ValueError(
            "name_prefix must form valid Python identifiers for every tool name; "
            f"got {name_prefix!r} producing {invalid[0]!r}"
        )
    tool_cls = load_tool(
        "dspy_agent_tools.Skills requires DSPy. Install it with: uv add dspy"
    )
    discovered, errors = _discover(paths, strict=strict)
    sandbox_runner = (
        ScriptSandbox(
            sandbox=sandbox,
            timeout=timeout,
            allow_network=allow_network,
            allow_write=allow_write,
            interpreters=interpreters,
            env=env,
            max_output_chars=max_output_chars,
        )
        if scripts
        else None
    )
    registry = _Registry(
        discovered,
        errors,
        sandbox_runner,
        max_resource_chars=max_resource_chars,
    )
    catalog = _catalog(discovered, max_catalog_chars)
    tools = _build_tools(
        registry,
        mode=mode,
        names=names,
        scripts=scripts,
        tool_cls=tool_cls,
    )
    return SkillTools(
        tools,
        skills=discovered,
        errors=errors,
        catalog=catalog,
        scripts=scripts,
        names=names,
        mode=mode,
    )
