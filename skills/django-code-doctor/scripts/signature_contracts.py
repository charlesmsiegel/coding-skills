"""Pure AST rules for signatures fixed by a caller or framework.

These classify signature structure and decorators without reading project files;
common.py keeps project walking, parse caches and copied-signature lookups. New
externally fixed signatures belong here when their supplied AST proves the contract.
"""

import ast


def _decorator_target(dec):
    """The callable a decorator names: `x` for `@x`, and for `@x(...)` too."""
    return dec.func if isinstance(dec, ast.Call) else dec


def is_cli_command(func) -> bool:
    """Whether `func` is a typer/click command: `@app.command()`, `@app.callback()`,
    `@click.command` or `@click.group`.

    Such a function's signature is the command line: one parameter per option,
    and its body runs only when that command is invoked.
    """
    for dec in func.decorator_list:
        target = _decorator_target(dec)
        if not isinstance(target, ast.Attribute):
            continue
        if target.attr in ("command", "callback"):
            return True
        if target.attr in ("command", "group") and isinstance(target.value, ast.Name) and target.value.id == "click":
            return True
    return False


def registers_config(func) -> bool:
    """An `__init__` under diffusers' `@register_to_config`.

    ConfigMixin serialises the model config from exactly those flat keyword
    arguments, so the usual fix for a long signature — group them into a
    dataclass — breaks from_pretrained/save_pretrained.
    """
    if func.name != "__init__":
        return False
    for dec in func.decorator_list:
        target = _decorator_target(dec)
        name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", None)
        if name == "register_to_config":
            return True
    return False


# Framework hooks whose parameter list the framework fixes: the method name,
# the package whose base class declares it, and its parameters after self/cls.
# Deliberately short. An nn.Module's forward() is also an override, but its
# parameters are the author's own design, so it is not here.
FRAMEWORK_HOOKS = (
    ("settings_customise_sources", "pydantic_settings",
     ("settings_cls", "init_settings", "env_settings", "dotenv_settings", "file_secret_settings")),
    ("__get_pydantic_core_schema__", "pydantic", ("source_type", "handler")),
    ("emit", "logging", ("record",)),            # logging.Handler
    ("default", "json", ("o",)),                 # json.JSONEncoder
    ("__call__", "argparse", ("parser", "namespace", "values", "option_string")),  # argparse.Action
)


def framework_hook_overrides(tree) -> frozenset:
    """id() of every method in ``tree`` that overrides a FRAMEWORK_HOOKS entry.

    Narrow on purpose, all three must hold: the method's class has a base
    imported from the hook's package, the name is the hook's, and the parameter
    list after self/cls is exactly the framework's.
    """
    imports: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name.split(".")[0]] = alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            for alias in node.names:
                imports[alias.asname or alias.name] = node.module.split(".")[0]
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        packages = set()
        for base in node.bases:
            root = base.value if isinstance(base, ast.Subscript) else base
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in imports:
                packages.add(imports[root.id])
        if not packages:
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = tuple(a.arg for a in [*item.args.posonlyargs, *item.args.args,
                                           *item.args.kwonlyargs])[1:]
            if any(item.name == name and package in packages and params == expected
                   for name, package, expected in FRAMEWORK_HOOKS):
                found.add(id(item))
    return frozenset(found)


def signature_is_a_contract(func, hooks: frozenset = frozenset()) -> bool:
    """Whether something other than `func` dictates its parameter list, so its length is not a design choice.

    A `@register_to_config` `__init__` (the flat kwargs are the serialised
    config), a CLI command (each parameter is one of its flags), and a method
    in ``hooks``: an override of a framework hook (framework_hook_overrides())
    or, in a test file, a copy of a production signature (copied_signatures()).
    The parameter-count detectors exempt these; nothing else about them is
    exempt.
    """
    return registers_config(func) or is_cli_command(func) or id(func) in hooks


# The signature size a function may reach before analyze_complexity reports it.
PARAMETER_LIMIT = 5


def count_parameters(func) -> int:
    """Every parameter a caller can bind, as written: positional-only,
    positional (`self` and `cls` included), keyword-only, `*args`, `**kwargs`."""
    args = func.args
    return (len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs)
            + (args.vararg is not None) + (args.kwarg is not None))


def parameter_severity(count: int, limit: int = PARAMETER_LIMIT) -> str:
    """How bad a signature of ``count`` parameters is: low past the limit,
    medium from half again over it, high from double.

    analyze_complexity's parameter_count and find_code_smells'
    long_parameter_list both grade with this, so one signature cannot be a
    medium in one report and a low in the other.
    """
    ratio = count / limit
    return "high" if ratio >= 2.0 else ("medium" if ratio >= 1.5 else "low")


def _parameter_names(func) -> frozenset:
    """The parameters a caller can pass by position or keyword, self and cls aside."""
    args = func.args
    return frozenset(a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]) - {"self", "cls"}
