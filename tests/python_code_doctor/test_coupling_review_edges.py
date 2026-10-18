"""Receiver identity and extracted type evidence share lexical binding ownership."""

import ast
import math
import sys
import textwrap
import types
from fractions import Fraction
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills/python-code-doctor/scripts"


@pytest.fixture
def detector(load_module):
    return load_module(SCRIPTS, "find_coupling_issues")


def findings(detector, tmp_path, source, kind):
    sample = tmp_path / "app.py"
    sample.write_text(source, encoding="utf-8")
    return [row for row in detector.coupling_issues(ast.parse(source), str(sample))
            if row.issue_type == kind]


@pytest.mark.parametrize("builtin", ["str", "repr"])
@pytest.mark.parametrize("binding", [
    "original = self\nself = original",
    "original = self\nalias = original\nself = alias",
    "original = self\ndef restore():\n    nonlocal self\n    self = original\nrestore()",
    "original, unused = self, None\nself = original",
])
def test_grounded_alias_restore_keeps_representation_edge(detector, tmp_path, builtin, binding):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                BINDING
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """).replace("        BINDING", textwrap.indent(binding, "        "))
    namespace = {}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = "original"
    assert entry.save() == "original"
    [record] = findings(detector, tmp_path, source, "low_cohesion")
    assert record.metric_value == 2


@pytest.mark.parametrize("builtin", ["str", "repr"])
@pytest.mark.parametrize("binding", [
    "original = replacement\nself = original",
    "original = self\noriginal = replacement\nself = original",
    "original = self\nfor original in self:\n    pass\nself = original",
    "original, unused = self\nself = original",
    "original = alias\nalias = original\nself = alias",
    "original: object\nself = original",
])
def test_derived_foreign_extracted_or_ungrounded_alias_is_not_identity(
        detector, tmp_path, builtin, binding):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, replacement):
                BINDING
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """).replace("        BINDING", textwrap.indent(binding, "        "))
    assert findings(detector, tmp_path, source, "low_cohesion") == []


@pytest.mark.parametrize("builtin", ["str", "repr"])
def test_child_alias_cannot_join_the_original_payload_group(detector, tmp_path, builtin):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.child
            def stored(self):
                return self.payload
            def save(self):
                original = self.child
                self = original
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """)
    [record] = findings(detector, tmp_path, source, "low_cohesion")
    assert record.metric_value == 2


@pytest.mark.parametrize("annotation,pattern", [
    ("list[Payload]", "[item]"),
    ("'list[Payload]'", "[item]"),
    ("tuple[Payload, ...]", "[item]"),
    ("dict[str, Payload]", "{'key': item}"),
    ("list[Payload]", "[(_ as item)]"),
])
@pytest.mark.parametrize("external", [False, True])
def test_simple_pattern_extraction_preserves_annotated_element_type(
        detector, tmp_path, annotation, pattern, external):
    declaration = "from external import Payload" if external else "class Payload: pass"
    source = declaration + textwrap.dedent(f"""

        class Entry:
            def measure(self):
                return self.metrics
            def save(self, values: {annotation}):
                match values:
                    case {pattern}:
                        return item.first + item.second + item.third
    """)
    found = findings(detector, tmp_path, source, "feature_envy")
    assert bool(found) is not external


@pytest.mark.parametrize("annotation,pattern", [
    ("Payload", "Payload(item)"),
    ("list[Payload]", "[[item]]"),
    ("list[Payload]", "[Payload(item)]"),
    ("dict[str, Payload]", "{'key': {'nested': item}}"),
])
def test_external_container_does_not_prove_arbitrary_nested_or_class_fields(
        detector, tmp_path, annotation, pattern):
    source = "from external import Payload\n" + textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, values: {annotation}):
                match values:
                    case {pattern}:
                        return item.first + item.second + item.third
    """)
    [record] = findings(detector, tmp_path, source, "feature_envy")
    assert record.metric_value == 3


@pytest.mark.parametrize("intermediate", [False, True])
@pytest.mark.parametrize("imported", [False, True])
def test_nonlocal_import_metadata_follows_its_binding_owner(detector, tmp_path, intermediate, imported):
    write = "import math as item" if imported else "item = supplied"
    helper = "def update():\n    nonlocal item\n    " + write + "\nupdate()"
    if intermediate:
        helper = "def relay():\n    nonlocal item\n" + textwrap.indent(helper, "    ") + "\nrelay()"
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, supplied):
                item = self.payload
                HELPER
                return item.pi + item.e + item.tau
    """).replace("        HELPER", textwrap.indent(helper, "        "))
    namespace = {}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = object()
    supplied = type("Payload", (), {"pi": 1, "e": 2, "tau": 3})()
    actual = entry.save(supplied)
    if imported:
        assert actual == math.pi + math.e + math.tau
    else:
        assert actual == 6
    found = findings(detector, tmp_path, source, "feature_envy")
    assert bool(found) is not imported


@pytest.mark.parametrize("nonlocal_write", [False, True])
@pytest.mark.parametrize("write", [
    "import math as item\nitem = supplied",
    "if flag:\n    import math as item\nelse:\n    item = supplied",
])
def test_one_possible_import_cannot_hide_a_foreign_rebinding(detector, tmp_path, nonlocal_write, write):
    body = "def update():\n    nonlocal item\n" + textwrap.indent(write, "    ") + "\nupdate()" if nonlocal_write else write
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, supplied, flag=False):
                item = self.payload
                WRITE
                return item.pi + item.e + item.tau
    """).replace("        WRITE", textwrap.indent(body, "        "))
    namespace = {}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = object()
    supplied = type("Payload", (), {"pi": 1, "e": 2, "tau": 3})()
    assert entry.save(supplied) == 6
    [record] = findings(detector, tmp_path, source, "feature_envy")
    assert record.metric_value == 3


def test_flat_alias_chain_does_not_drop_unrelated_file_findings(detector, tmp_path):
    aliases = "alias0 = self\n" + "\n".join(f"alias{i} = alias{i - 1}" for i in range(1, 500))
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                ALIASES
                self = alias499
                return str(self)
            def __str__(self):
                return self.payload
            def deep(self):
                return self.adapter.remote.state.value
    """).replace("        ALIASES", textwrap.indent(aliases, "        "))
    namespace = {}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = "original"
    assert entry.save() == "original"
    assert findings(detector, tmp_path, source, "low_cohesion")
    [record] = findings(detector, tmp_path, source, "message_chain")
    assert "deep" in record.name


@pytest.mark.parametrize("expression,external", [
    ("match values:\n    case [_, item]:\n        return item.numerator + item.denominator + item.real", False),
    ("match values:\n    case [item, _]:\n        return item.numerator + item.denominator + item.real", True),
    ("match values:\n    case [item, *_]:\n        return item.numerator + item.denominator + item.real", True),
    ("match values:\n    case [*_, item]:\n        return item.numerator + item.denominator + item.real", False),
    ("item = values[1]\nreturn item.numerator + item.denominator + item.real", False),
    ("item = values[0]\nreturn item.numerator + item.denominator + item.real", True),
    ("item = values[-1]\nreturn item.numerator + item.denominator + item.real", False),
    ("item = values[-2]\nreturn item.numerator + item.denominator + item.real", True),
])
def test_heterogeneous_tuple_extraction_uses_the_selected_position(detector, tmp_path, expression, external):
    source = textwrap.dedent("""
        from fractions import Fraction
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, values: tuple[Fraction, Payload]):
                EXTRACT
    """).replace("        EXTRACT", textwrap.indent(expression, "        "))
    namespace = {}
    exec(source, namespace)
    values = (namespace["Fraction"](1, 2), namespace["Payload"]())
    assert namespace["Entry"]().save(values) == (namespace["Fraction"](7, 2) if external else 6)
    found = findings(detector, tmp_path, source, "feature_envy")
    assert bool(found) is not external


@pytest.mark.parametrize("declaration,pattern,value,expected,external", [
    ("", "int() as item", "3", 7, True),
    ("from fractions import Fraction", "Fraction() as item", "Fraction(3, 2)", 6.5, True),
    ("import fractions", "fractions.Fraction() as item", "fractions.Fraction(3, 2)", 6.5, True),
    ("from fractions import Fraction", "(Fraction() | int()) as item", "3", 7, True),
    ("from fractions import Fraction", "[Fraction() as item]", "[Fraction(3, 2)]", 6.5, True),
    ("from fractions import Fraction", "{'key': Fraction() as item}", "{'key': Fraction(3, 2)}", 6.5, True),
    ("", "Payload() as item", "Payload()", 6, False),
    ("int = Payload", "int() as item", "Payload()", 6, False),
    ("from fractions import Fraction", "(Fraction() | Payload()) as item", "Payload()", 6, False),
    ("from types import SimpleNamespace", "SimpleNamespace(child=item)",
     "SimpleNamespace(child=Payload())", 6, False),
])
def test_whole_class_pattern_capture_uses_only_its_proven_type(
        detector, tmp_path, declaration, pattern, value, expected, external):
    source = textwrap.dedent("""
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
    """) + declaration + "\n" + textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value):
                match value:
                    case {pattern}:
                        return item.numerator + item.denominator + item.real
    """)
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(eval(value, namespace)) == expected
    found = findings(detector, tmp_path, source, "feature_envy")
    assert bool(found) is not external


@pytest.mark.parametrize("shadow", ["parameter", "local"])
def test_shadowed_class_pattern_cannot_prove_an_external_type(detector, tmp_path, shadow):
    source = textwrap.dedent("""
        from fractions import Fraction
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value, Fraction=Payload):
                match value:
                    case Fraction() as item:
                        return item.numerator + item.denominator + item.real
    """)
    if shadow == "local":
        source = source.replace("value, Fraction=Payload", "value").replace(
            "        match value:", "        Fraction = Payload\n        match value:")
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(namespace["Payload"]()) == 6
    [record] = findings(detector, tmp_path, source, "feature_envy")
    assert record.metric_value == 3


def test_class_capture_followed_by_foreign_write_is_not_external(detector, tmp_path):
    source = textwrap.dedent("""
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value, supplied):
                match value:
                    case int() as item:
                        item = supplied
                        return item.numerator + item.denominator + item.real
    """)
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(3, namespace["Payload"]()) == 6
    [record] = findings(detector, tmp_path, source, "feature_envy")
    assert record.metric_value == 3


@pytest.mark.parametrize("binding", [
    "import math as item\nalias = item\nitem = alias",
    "import math as item\nalias = item\nsecond = alias\nitem = second",
    "import math as module\nitem = module",
    "import math as item\nalias = item\ndef restore():\n    nonlocal item\n    item = alias\nrestore()",
    "import math as item\nalias = item\nitem, unused = alias, None",
])
def test_grounded_module_alias_preserves_namespace_exemption(detector, tmp_path, binding):
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                BINDING
                return item.pi + item.e + item.tau
    """).replace("        BINDING", textwrap.indent(binding, "        "))
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save() == math.pi + math.e + math.tau
    assert findings(detector, tmp_path, source, "feature_envy") == []


@pytest.mark.parametrize("binding", [
    "import math as item\nalias = supplied\nitem = alias",
    "import math as item\nalias = item\nalias = supplied\nitem = alias",
    "import math as item\nalias = item\nif flag:\n    alias = supplied\nitem = alias",
    "import math as item\nalias: object\ndef replace():\n    nonlocal alias\n    alias = supplied\nreplace()\nitem = alias",
])
def test_possible_foreign_module_alias_write_retains_envy(detector, tmp_path, binding):
    source = textwrap.dedent("""
        class Payload:
            pi = 1
            e = 2
            tau = 3
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, supplied, flag=True):
                BINDING
                return item.pi + item.e + item.tau
    """).replace("        BINDING", textwrap.indent(binding, "        "))
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(namespace["Payload"]()) == 6
    [record] = findings(detector, tmp_path, source, "feature_envy")
    assert record.metric_value == 3


def test_foreign_grounded_alias_cycle_does_not_borrow_an_import(detector, tmp_path):
    source = textwrap.dedent("""
        class Payload:
            pi = 1
            e = 2
            tau = 3
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, supplied):
                import math as item
                alias = supplied
                other = alias
                alias = other
                item = alias
                return item.pi + item.e + item.tau
    """)
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(namespace["Payload"]()) == 6
    [record] = findings(detector, tmp_path, source, "feature_envy")
    assert record.metric_value == 3


@pytest.mark.parametrize("root,pattern,replacement", [
    ("Fraction", "Fraction() as item", "Payload"),
    ("fractions", "fractions.Fraction() as item", "Namespace"),
])
@pytest.mark.parametrize("binding", [
    "ROOT = REPLACEMENT",
    "if True:\n    ROOT = REPLACEMENT",
    "for ROOT in (REPLACEMENT,):\n    pass",
    "with nullcontext(REPLACEMENT) as ROOT:\n    pass",
])
def test_class_pattern_module_rebinding_retains_foreign_type(
        detector, tmp_path, root, pattern, replacement, binding):
    source = textwrap.dedent("""
        import fractions
        from fractions import Fraction
        from contextlib import nullcontext
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
        class Namespace:
            Fraction = Payload
    """) + binding.replace("ROOT", root).replace("REPLACEMENT", replacement) + "\n" + textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value):
                match value:
                    case {pattern}:
                        return item.numerator + item.denominator + item.real
    """)
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(namespace["Payload"]()) == 6
    [record] = findings(detector, tmp_path, source, "feature_envy")
    assert record.metric_value == 3


@pytest.mark.parametrize("root,pattern,replacement", [
    ("Fraction", "Fraction() as item", "Payload"),
    ("fractions", "fractions.Fraction() as item", "Namespace"),
])
@pytest.mark.parametrize("binding", ["parameter", "local"])
def test_class_pattern_enclosing_binding_overrides_module_import(
        detector, tmp_path, root, pattern, replacement, binding):
    source = textwrap.dedent(f"""
        import fractions
        from fractions import Fraction
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
        class Namespace:
            Fraction = Payload
        def make({root}={replacement}):
            class Entry:
                def measure(self):
                    return self.metrics
                def save(self, value):
                    match value:
                        case {pattern}:
                            return item.numerator + item.denominator + item.real
            return Entry
        Entry = make()
    """)
    if binding == "local":
        source = source.replace(f"def make({root}={replacement}):", f"def make():\n    {root} = {replacement}")
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(namespace["Payload"]()) == 6
    [record] = findings(detector, tmp_path, source, "feature_envy")
    assert record.metric_value == 3


@pytest.mark.parametrize("root,pattern", [
    ("Fraction", "Fraction() as item"),
    ("fractions", "fractions.Fraction() as item"),
])
@pytest.mark.parametrize("namespace_only", [False, True])
def test_class_pattern_unshadowed_import_ignores_class_namespace(
        detector, tmp_path, root, pattern, namespace_only):
    source = textwrap.dedent(f"""
        import fractions
        from fractions import Fraction
        class Entry:
            {root} = object()
            def measure(self):
                return self.metrics
            def save(self, value):
                match value:
                    case {pattern}:
                        return item.numerator + item.denominator + item.real
    """)
    if not namespace_only:
        source = source.replace(f"    {root} = object()\n", "")
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(namespace["Fraction"](3, 2)) == 6.5
    assert findings(detector, tmp_path, source, "feature_envy") == []


@pytest.mark.parametrize("scope", ["method", "enclosing"])
@pytest.mark.parametrize("qualified", [False, True])
@pytest.mark.parametrize("binding", ["external", "rebound", "first_party"])
def test_scoped_class_pattern_imports_use_the_nearest_proven_binding(
        detector, tmp_path, monkeypatch, scope, qualified, binding):
    payload = "class Payload:\n    numerator = 1\n    denominator = 2\n    real = 3\n"
    local = tmp_path / "cohesion_local_types.py"
    local.write_text(payload, encoding="utf-8")
    module = types.ModuleType("cohesion_local_types")
    exec(payload, module.__dict__)
    monkeypatch.setitem(sys.modules, "cohesion_local_types", module)
    package = "cohesion_local_types" if binding == "first_party" else "fractions"
    kind = "Payload" if binding == "first_party" else "Fraction"
    root = "lib" if qualified else "Kind"
    declaration = f"import {package} as lib" if qualified else f"from {package} import {kind} as Kind"
    pattern = f"lib.{kind}()" if qualified else "Kind()"
    if binding == "rebound":
        declaration += f"\n{root} = " + ("Namespace" if qualified else "Payload")
    body = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value):
                BINDING
                match value:
                    case {pattern} as item:
                        return item.numerator + item.denominator + item.real
    """)
    if scope == "method":
        body = body.replace("        BINDING", textwrap.indent(declaration, "        "))
    else:
        body = ("def make():\n" + textwrap.indent(declaration, "    ") + "\n"
                + textwrap.indent(body.replace("        BINDING\n", ""), "    ")
                + "    return Entry\nEntry = make()\n")
    source = payload + "class Namespace:\n    Fraction = Payload\n" + body
    namespace = {}
    exec(source, namespace)
    value = module.Payload() if binding == "first_party" else namespace["Payload"]()
    if binding == "external":
        value = Fraction(3, 2)
    assert namespace["Entry"]().save(value) == (6.5 if binding == "external" else 6)
    found = findings(detector, tmp_path, source, "feature_envy")
    # A method-local replacement also reads Namespace.Fraction: four distinct
    # foreign attributes, rather than only the matched value's three.
    metric = 4 if binding == "rebound" and qualified and scope == "method" else 3
    assert [row.metric_value for row in found] == ([] if binding == "external" else [metric])


@pytest.mark.parametrize("builtin", ["str", "repr"])
@pytest.mark.parametrize("local_cell", [False, True])
def test_delete_only_closure_owns_its_nonlocal_write(detector, tmp_path, builtin, local_cell):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                def middle():
                    def initialize():
                        nonlocal self
                        self = "replacement"
                    initialize()
                    DELETE
                middle()
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """).replace("DELETE", "del self" if local_cell else "pass")
    namespace = {}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = "original"
    assert entry.save() == ("original" if local_cell else repr("replacement") if builtin == "repr" else "replacement")
    found = findings(detector, tmp_path, source, "low_cohesion")
    assert [row.metric_value for row in found] == ([2] if local_cell else [])


@pytest.mark.parametrize("scope", ["module", "class"])
def test_namespace_delete_does_not_create_a_builtin_shadow(detector, tmp_path, scope):
    deletion = "try:\n    del str\nexcept NameError:\n    pass\n"
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                return str(self)
            def __str__(self):
                return self.payload
    """)
    source = deletion + source if scope == "module" else source.replace(
        "class Entry:", "class Entry:\n" + textwrap.indent(deletion, "    "))
    namespace = {}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = "original"
    assert entry.save() == "original"
    assert [row.metric_value for row in findings(detector, tmp_path, source, "low_cohesion")] == [2]


@pytest.mark.parametrize("binding,external", [
    ("import math as item", True),
    ("import math as item\ndef declaration():\n    global item\ndeclaration()", True),
    ("import math as item\nalias = item\nitem = alias", True),
    ("import math as item\ndef replace():\n    global item\n    item = supplied\nreplace()", False),
    ("item = supplied", False),
    ("pass", False),
])
def test_global_namespace_ownership_is_not_a_value_write(detector, tmp_path, binding, external):
    source = textwrap.dedent("""
        class Payload:
            pi = 1
            e = 2
            tau = 3
        item = Payload()
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, supplied):
                global item
                BINDING
                return item.pi + item.e + item.tau
    """).replace("        BINDING", textwrap.indent(binding, "        "))
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(namespace["Payload"]()) == (math.pi + math.e + math.tau if external else 6)
    found = findings(detector, tmp_path, source, "feature_envy")
    assert [row.metric_value for row in found] == ([] if external else [3])


def test_global_representation_lookup_skips_an_enclosing_converter(detector, tmp_path):
    source = textwrap.dedent("""
        def make():
            str = lambda value: "local converter"
            class Entry:
                def measure(self):
                    return self.metrics
                def save(self):
                    global str
                    return str(self)
                def __str__(self):
                    return self.payload
            return Entry
        Entry = make()
    """)
    namespace = {}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = "original"
    assert entry.save() == "original"
    assert [row.metric_value for row in findings(detector, tmp_path, source, "low_cohesion")] == [2]


@pytest.mark.parametrize("ownership", ["global", "nonlocal"])
def test_global_and_nonlocal_receiver_writes_have_different_owners(detector, tmp_path, ownership):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                def replace():
                    {ownership} self
                    self = "replacement"
                replace()
                return str(self)
            def __str__(self):
                return self.payload
    """)
    namespace = {}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = "original"
    assert entry.save() == ("original" if ownership == "global" else "replacement")
    found = findings(detector, tmp_path, source, "low_cohesion")
    assert [row.metric_value for row in found] == ([2] if ownership == "global" else [])


@pytest.mark.parametrize("export", ["instance", "submodule", "shadowed_submodule", "dynamic", "mixed"])
@pytest.mark.parametrize("aliased", [False, True])
def test_first_party_from_members_need_namespace_value_proof(detector, tmp_path, monkeypatch, export, aliased):
    package = tmp_path / "cohesion_exports"
    package.mkdir()
    payload = "class Payload:\n    first = 1\n    second = 2\n    third = 3\n"
    exports = payload
    member = "payload" if export in {"instance", "mixed"} else "sub"
    if export in {"instance", "shadowed_submodule", "mixed"}:
        exports += f"{member} = Payload()\n"
    elif export == "dynamic":
        exports += "def __getattr__(name):\n    return Payload()\n"
    (package / "__init__.py").write_text(exports, encoding="utf-8")
    (package / "sub.py").write_text("first = 1\nsecond = 2\nthird = 3\n", encoding="utf-8")
    module = types.ModuleType("cohesion_exports")
    module.__path__ = [str(package)]
    exec(exports, module.__dict__)
    monkeypatch.setitem(sys.modules, "cohesion_exports", module)
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                from cohesion_exports import {member} as imported
                ALIAS
                return item.first + item.second + item.third
    """).replace("        ALIAS", "        item = imported" if aliased else "        pass").replace(
        "return item.", "return imported." if not aliased else "return item.")
    if not aliased:
        source = source.replace(" + item.", " + imported.")
    if export == "mixed":
        source = source.replace("import payload as imported", "import payload as imported, sub as module_value")
        source = source.replace(".third\n", ".third + module_value.first\n")
    namespace = {}
    try:
        exec(source, namespace)
        assert namespace["Entry"]().save() == (7 if export == "mixed" else 6)
    finally:
        sys.modules.pop("cohesion_exports.sub", None)
    found = findings(detector, tmp_path, source, "feature_envy")
    assert [row.metric_value for row in found] == ([] if export == "submodule" else [3])


def test_external_imported_member_alias_keeps_its_existing_value_exemption(detector, tmp_path):
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                from sys import float_info as imported
                alias = imported
                return alias.max_exp + alias.min_exp + alias.mant_dig
    """)
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save() == sys.float_info.max_exp + sys.float_info.min_exp + sys.float_info.mant_dig
    assert findings(detector, tmp_path, source, "feature_envy") == []


@pytest.mark.parametrize("builtin", ["str", "repr"])
def test_free_representation_lookup_sees_a_helpers_actual_global_write(detector, tmp_path, builtin):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                def configure():
                    global {builtin}
                    {builtin} = lambda value: "converted"
                configure()
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """)
    namespace = {}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = "original"
    assert entry.save() == "converted"
    assert findings(detector, tmp_path, source, "low_cohesion") == []


@pytest.mark.parametrize("qualified", [False, True])
def test_helpers_global_write_does_not_replace_an_enclosing_pattern_cell(detector, tmp_path, qualified):
    declaration = "import fractions as library" if qualified else "from fractions import Fraction as Kind"
    root = "library" if qualified else "Kind"
    replacement = "Namespace" if qualified else "Payload"
    pattern = "library.Fraction()" if qualified else "Kind()"
    source = textwrap.dedent(f"""
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
        class Namespace:
            Fraction = Payload
        def make():
            {declaration}
            class Entry:
                def measure(self):
                    return self.metrics
                def save(self, value):
                    def configure():
                        global {root}
                        {root} = {replacement}
                    configure()
                    match value:
                        case {pattern} as item:
                            return item.numerator + item.denominator + item.real
            return Entry
        Entry = make()
    """)
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(Fraction(3, 2)) == 6.5
    assert namespace[root] is namespace[replacement]
    assert findings(detector, tmp_path, source, "feature_envy") == []


@pytest.mark.parametrize("builtin", ["str", "repr"])
def test_global_representation_reader_keeps_a_sibling_helpers_write(detector, tmp_path, builtin):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                def configure():
                    global {builtin}
                    {builtin} = lambda value: "converted"
                def render():
                    global {builtin}
                    return {builtin}(self)
                configure()
                return render()
            def __{builtin}__(self):
                return self.payload
    """)
    namespace = {}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = "original"
    assert entry.save() == "converted"
    assert findings(detector, tmp_path, source, "low_cohesion") == []


def test_enclosing_global_import_keeps_an_inner_helpers_actual_write(detector, tmp_path):
    source = textwrap.dedent("""
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
        def make():
            global Kind
            from fractions import Fraction as Kind
            class Entry:
                def measure(self):
                    return self.metrics
                def save(self, value):
                    def configure():
                        global Kind
                        Kind = Payload
                    configure()
                    match value:
                        case Kind() as item:
                            return item.numerator + item.denominator + item.real
            return Entry
        Entry = make()
    """)
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(namespace["Payload"]()) == 6
    assert namespace["Kind"] is namespace["Payload"]
    assert [row.metric_value for row in findings(detector, tmp_path, source, "feature_envy")] == [3]


@pytest.mark.parametrize("rebound", [False, True])
def test_conditional_global_import_keeps_sibling_writes_until_the_module(detector, tmp_path, rebound):
    source = textwrap.dedent(f"""
        from fractions import Fraction as Kind
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value):
                def configure():
                    global Kind
                    {"Kind = Payload" if rebound else "pass"}
                def render(flag):
                    global Kind
                    if flag:
                        from fractions import Fraction as Kind
                    match value:
                        case Kind() as item:
                            return item.numerator + item.denominator + item.real
                configure()
                return render(False)
    """)
    namespace = {}
    exec(source, namespace)
    value = namespace["Payload"]() if rebound else Fraction(3, 2)
    assert namespace["Entry"]().save(value) == (6 if rebound else 6.5)
    found = findings(detector, tmp_path, source, "feature_envy")
    assert [row.metric_value for row in found] == ([3] if rebound else [])


@pytest.mark.parametrize("kind", ["star", "mapping"])
@pytest.mark.parametrize("binding", ["fresh", "alias", "rebound", "conditional"])
def test_rest_capture_creates_a_builtin_container_until_rebound(detector, tmp_path, kind, binding):
    pattern = "[head, *rest]" if kind == "star" else '{"key": selected, **rest}'
    attributes = "append, extend, clear" if kind == "star" else "keys, values, items"
    declarations = "\n".join(f"    {name} = {index}" for index, name in enumerate(attributes.split(", "), 1))
    assignment = {"fresh": "pass", "alias": "item = rest", "rebound": "rest = replacement",
                  "conditional": "if should_rebind:\n    rest = replacement"}[binding]
    name = "item" if binding == "alias" else "rest"
    reads = ", ".join(f"{name}.{attribute}" for attribute in attributes.split(", "))
    source = f"class Payload:\n{declarations}\nlist = dict = Payload\n" + textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value, replacement, should_rebind):
                match value:
                    case {pattern}:
                        BINDING
                        return {name}, ({reads})
    """).replace("                BINDING", textwrap.indent(assignment, "                "))
    namespace = {}
    exec(source, namespace)
    replacement = namespace["Payload"]()
    value = [0, 1] if kind == "star" else {"key": 0, "tail": 1}
    rest, reads = namespace["Entry"]().save(value, replacement, True)
    rebound = binding in {"rebound", "conditional"}
    assert (rest is replacement) if rebound else (type(rest) is (list if kind == "star" else dict))
    assert reads == (1, 2, 3) if rebound else all(method.__self__ is rest for method in reads)
    assert [row.metric_value for row in findings(detector, tmp_path, source, "feature_envy")] == ([3] if rebound else [])


@pytest.mark.parametrize("pattern,value", [("True", True), ("False", False), ("None", None), ("True | False", True)])
@pytest.mark.parametrize("rebound", [False, True])
def test_singleton_capture_proves_identity_until_rebound(detector, tmp_path, pattern, value, rebound):
    attributes = ("__class__", "__repr__", "__str__") if value is None else ("numerator", "denominator", "real")
    reads = ", ".join(f"item.{attribute}" for attribute in attributes)
    source = textwrap.dedent(f"""
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value, replacement):
                match value:
                    case {pattern} as item:
                        {"item = replacement" if rebound else "pass"}
                        return item, ({reads})
    """)
    namespace = {}
    exec(source, namespace)
    replacement = namespace["Payload"]()
    item, reads = namespace["Entry"]().save(value, replacement)
    assert item is (replacement if rebound else value)
    assert len(reads) == 3
    assert [row.metric_value for row in findings(detector, tmp_path, source, "feature_envy")] == ([3] if rebound else [])


@pytest.mark.parametrize("literal", ["1", '"Realm"'])
def test_literal_equality_capture_keeps_the_original_first_party_instance(detector, tmp_path, literal):
    source = textwrap.dedent(f"""
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
            def __eq__(self, other):
                return other == {literal}
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value):
                match value:
                    case {literal} as item:
                        return item, item.numerator + item.denominator + item.real
    """)
    namespace = {}
    exec(source, namespace)
    value = namespace["Payload"]()
    item, total = namespace["Entry"]().save(value)
    assert item is value and total == 6
    assert [row.metric_value for row in findings(detector, tmp_path, source, "feature_envy")] == [3]


@pytest.mark.parametrize("initial,binding,writer,local_alias,expected", [
    ("payload", "conditional_alias", "none", False, 3),
    ("payload", "conditional_import", "none", False, 3),
    ("payload", "direct_import", "none", False, None),
    ("payload", "direct_alias", "none", False, None),
    ("module", "conditional_alias", "none", False, None),
    ("module", "conditional_import", "none", False, None),
    ("module", "conditional_alias", "top", False, 3),
    ("module", "direct_import", "top", False, 3),
    ("module", "conditional_import", "class", False, 3),
    ("module", "direct_import", "class", False, 3),
    ("payload", "conditional_alias", "none", True, 3),
])
def test_global_namespace_proof_includes_ambient_writes(
        detector, tmp_path, initial, binding, writer, local_alias, expected):
    assignment = {
        "conditional_alias": "if flag:\n    alias = imported_module",
        "conditional_import": "if flag:\n    import math as alias",
        "direct_import": "import math as alias",
        "direct_alias": "alias = imported_module",
    }[binding]
    source = textwrap.dedent("""
        class Payload:
            pi = 1
            e = 2
            tau = 3
        INITIAL
        WRITER
        class Entry:
            CLASS_WRITER
            def measure(self):
                return self.metrics
            def save(self, flag):
                global alias
                import math as imported_module
                ASSIGNMENT
                CALL_WRITER
                LOCAL_ALIAS
                return READ.pi + READ.e + READ.tau
    """).replace("INITIAL", "alias = Payload()" if initial == "payload" else "import math as alias")
    declaration = "def configure():\n    global alias\n    alias = Payload()"
    source = source.replace("WRITER\n", declaration + "\n", 1) if writer == "top" else source.replace("WRITER\n", "\n", 1)
    source = source.replace("    CLASS_WRITER", textwrap.indent("@staticmethod\n" + declaration, "    ")
                            if writer == "class" else "")
    source = source.replace("        ASSIGNMENT", textwrap.indent(assignment, "        "))
    source = source.replace("CALL_WRITER", "configure()" if writer == "top" else "Entry.configure()" if writer == "class" else "pass")
    source = source.replace("LOCAL_ALIAS", "local_alias = alias" if local_alias else "pass")
    source = source.replace("READ", "local_alias" if local_alias else "alias")
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(False) == (6 if expected else math.pi + math.e + math.tau)
    assert [row.metric_value for row in findings(detector, tmp_path, source, "feature_envy")] == ([] if expected is None else [expected])


@pytest.mark.parametrize("body,expected", [("pass", None), ("import math as alias", None), ("alias = Payload()", 3)])
def test_class_body_global_ownership_preserves_actual_write_evidence(detector, tmp_path, body, expected):
    source = textwrap.dedent(f"""
        import math as alias
        class Payload:
            pi = 1
            e = 2
            tau = 3
        class Configure:
            global alias
            {body}
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, flag):
                global alias
                if flag:
                    import math as alias
                return alias.pi + alias.e + alias.tau
    """)
    namespace = {}
    exec(source, namespace)
    assert namespace["Entry"]().save(False) == (6 if expected else math.pi + math.e + math.tau)
    assert [row.metric_value for row in findings(detector, tmp_path, source, "feature_envy")] == ([] if expected is None else [expected])


@pytest.mark.parametrize("binding", ["import", "assignment", "late_assignment"])
def test_namespace_overwrite_respects_statement_end_before_the_read(detector, tmp_path, binding):
    statements = {
        "import": "import math as alias; return alias.pi + alias.e + alias.tau",
        "assignment": "alias = imported_module; return alias.pi + alias.e + alias.tau",
        "late_assignment": "local_alias = alias; alias = imported_module; return local_alias.pi + local_alias.e + local_alias.tau",
    }
    source = textwrap.dedent(f"""
        class Payload:
            pi = 1
            e = 2
            tau = 3
        alias = Payload()
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                global alias
                import math as imported_module
                {statements[binding]}
    """)
    namespace = {}
    exec(source, namespace)
    late = binding == "late_assignment"
    assert namespace["Entry"]().save() == (6 if late else math.pi + math.e + math.tau)
    assert [row.metric_value for row in findings(detector, tmp_path, source, "feature_envy")] == ([3] if late else [])


@pytest.mark.parametrize("binding", ["class_rebound", "method_rebound", "readonly", "external", "local", "enclosing"])
def test_pattern_module_lookup_includes_indexed_global_writers_but_keeps_nearer_cells(detector, tmp_path, binding):
    class_write = ("Kind = Payload" if binding in {"class_rebound", "local", "enclosing"}
                   else "from fractions import Fraction as Kind" if binding == "external" else "pass")
    source = textwrap.dedent(f"""
        from fractions import Fraction as Kind
        class Payload:
            numerator = 1
            denominator = 2
            real = 3
        class Configure:
            global Kind
            {class_write}
    """)
    if binding == "method_rebound":
        source += "    @staticmethod\n    def configure():\n        global Kind\n        Kind = Payload\n"
    entry = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value):
                {"from fractions import Fraction as Kind" if binding == "local" else "pass"}
                {"Configure.configure()" if binding == "method_rebound" else "pass"}
                match value:
                    case Kind() as item:
                        return item.numerator + item.denominator + item.real
    """)
    if binding == "enclosing":
        entry = ("def make():\n    from fractions import Fraction as Kind\n" + textwrap.indent(entry, "    ")
                 + "    return Entry\nEntry = make()\n")
    source += entry
    namespace = {}
    exec(source, namespace)
    rebound = binding in {"class_rebound", "method_rebound"}
    value = namespace["Payload"]() if rebound else Fraction(3, 2)
    assert namespace["Entry"]().save(value) == (6 if rebound else 6.5)
    assert [row.metric_value for row in findings(detector, tmp_path, source, "feature_envy")] == ([3] if rebound else [])


@pytest.mark.parametrize("rebound", [False, True])
def test_representation_module_lookup_includes_class_writes_without_inventing_declaration_values(
        detector, tmp_path, rebound):
    source = textwrap.dedent(f"""
        class Configure:
            global str
            {"str = lambda value: 'converted'" if rebound else "pass"}
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                return str(self)
            def __str__(self):
                return self.payload
    """)
    namespace = {}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = "original"
    assert entry.save() == ("converted" if rebound else "original")
    assert [row.metric_value for row in findings(detector, tmp_path, source, "low_cohesion")] == ([] if rebound else [2])


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("binding", ["rebound", "foreign", "readonly", "external"])
def test_class_nonlocal_writes_reach_the_actual_enclosing_pattern_owner(detector, tmp_path, nested, binding):
    statement = {"rebound": "Kind = Payload", "foreign": "with nullcontext(Payload) as Kind:\n    pass",
                 "readonly": "pass", "external": "from fractions import Fraction as Kind"}[binding]
    entry = textwrap.dedent("""
        class Configure:
            nonlocal Kind
            BINDING
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value):
                match value:
                    case Kind() as item:
                        return item.numerator + item.denominator + item.real
        return Entry
    """).replace("    BINDING", textwrap.indent(statement, "    "))
    if nested:
        entry = "def middle():\n    nonlocal Kind\n" + textwrap.indent(entry, "    ") + "return middle()\n"
    source = ("from contextlib import nullcontext\nclass Payload:\n    numerator=1\n    denominator=2\n    real=3\n"
              + "def make():\n    from fractions import Fraction as Kind\n" + textwrap.indent(entry, "    ")
              + "Entry = make()\n")
    namespace = {}
    exec(source, namespace)
    replaced = binding in {"rebound", "foreign"}
    value = namespace["Payload"]() if replaced else Fraction(3, 2)
    assert namespace["Entry"]().save(value) == (6 if replaced else 6.5)
    assert [row.metric_value for row in findings(detector, tmp_path, source, "feature_envy")] == ([3] if replaced else [])


@pytest.mark.parametrize("builtin", ["str", "repr"])
@pytest.mark.parametrize("global_alias", [False, True])
def test_receiver_identity_does_not_borrow_a_partial_global_alias(detector, tmp_path, builtin, global_alias):
    binding = "global original\nif keep:\n    original = self" if global_alias else "original = self"
    source = textwrap.dedent(f"""
        class Replacement:
            def __{builtin}__(self):
                return "replacement"
        original = Replacement()
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, keep):
                BINDING
                self = original
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """).replace("        BINDING", textwrap.indent(binding, "        "))
    namespace = {}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = "original"
    assert entry.save(False) == ("replacement" if global_alias else "original")
    assert [row.metric_value for row in findings(detector, tmp_path, source, "low_cohesion")] == ([] if global_alias else [2])


@pytest.mark.parametrize("placement", ["after", "before", "conditional", "nested", "ambient", "loop"])
def test_namespace_read_view_keeps_unsafe_writers_but_not_own_straight_line_future(
        detector, tmp_path, placement):
    body = {
        "after": "import math as alias\nresult = alias.pi + alias.e + alias.tau\nalias = Payload()\nreturn result",
        "before": "import math as alias\nalias = Payload()\nreturn alias.pi + alias.e + alias.tau",
        "conditional": "import math as alias\nif replace:\n    alias = Payload()\nreturn alias.pi + alias.e + alias.tau",
        "nested": "import math as alias\ndef configure():\n    global alias\n    alias = Payload()\nconfigure()\nreturn alias.pi + alias.e + alias.tau",
        "ambient": "import math as alias\nconfigure()\nreturn alias.pi + alias.e + alias.tau",
        "loop": "import math as alias\nfor unused in range(2):\n    result = alias.pi + alias.e + alias.tau\n    alias = Payload()\nreturn result",
    }[placement]
    source = "class Payload:\n    pi=1\n    e=2\n    tau=3\nalias=Payload()\n"
    if placement == "ambient":
        source += "def configure():\n    global alias\n    alias=Payload()\n"
    source += textwrap.dedent("""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, replace=True):
                global alias
                BODY
    """).replace("        BODY", textwrap.indent(body, "        "))
    namespace = {}
    exec(source, namespace)
    entry = namespace["Entry"]()
    expected = math.pi + math.e + math.tau if placement == "after" else 6
    assert (entry.save(), entry.save()) == (expected, expected)
    assert [row.metric_value for row in findings(detector, tmp_path, source, "feature_envy")] == ([] if placement == "after" else [3])


@pytest.mark.parametrize("binding", ["single", "chain", "long", "attribute", "qualified", "local", "enclosing",
                                     "rebound", "mixed", "foreign_cycle", "class_shadow"])
def test_pattern_class_aliases_are_iteratively_grounded_in_their_actual_binding(detector, tmp_path, binding):
    aliases = "Kind = Fraction\n"
    pattern = "Kind()"
    if binding == "chain":
        aliases = "First = Fraction\nKind = First\n"
    elif binding == "long":
        aliases = "First0 = Fraction\n" + "".join(f"First{index} = First{index-1}\n" for index in range(1, 1000)) + "Kind = First999\n"
    elif binding == "attribute":
        aliases = "Kind = library.Fraction\n"
    elif binding == "qualified":
        aliases, pattern = "Kind = library\n", "Kind.Fraction()"
    elif binding in {"rebound", "mixed"}:
        aliases += "Kind = Payload\n" if binding == "rebound" else "if True:\n    Kind = Payload\n"
    elif binding == "foreign_cycle":
        aliases = "First = Payload\nKind = First\nFirst = Kind\n"
    elif binding == "class_shadow":
        aliases = "class Configure:\n    global Kind\n    Fraction = Payload\n    Kind = Fraction\n"
    entry = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return self.metrics
            def save(self, value):
                LOCAL
                match value:
                    case {pattern} as item:
                        return item.numerator + item.denominator + item.real
    """)
    if binding == "local":
        entry = entry.replace("        LOCAL", "        Kind = Fraction")
        aliases = ""
    else:
        entry = entry.replace("        LOCAL", "        pass")
    if binding == "enclosing":
        entry = "def make():\n" + textwrap.indent(aliases + entry, "    ") + "    return Entry\nEntry = make()\n"
        aliases = ""
    source = ("from fractions import Fraction\nimport fractions as library\nclass Payload:\n    numerator=1\n    denominator=2\n    real=3\n"
              + aliases + entry)
    namespace = {}
    exec(source, namespace)
    foreign = binding in {"rebound", "mixed", "foreign_cycle", "class_shadow"}
    value = namespace["Payload"]() if foreign else Fraction(3, 2)
    assert namespace["Entry"]().save(value) == (6 if foreign else 6.5)
    assert [row.metric_value for row in findings(detector, tmp_path, source, "feature_envy")] == ([3] if foreign else [])


@pytest.mark.parametrize("builtin", ["str", "repr"])
@pytest.mark.parametrize("replaced", [False, True])
def test_class_nonlocal_receiver_writes_reach_the_existing_identity_proof(detector, tmp_path, builtin, replaced):
    source = textwrap.dedent(f"""
        class Replacement:
            def __{builtin}__(self):
                return "replacement"
        class Entry:
            def measure(self):
                return self.metrics
            def save(self):
                class Configure:
                    nonlocal self
                    {"self = Replacement()" if replaced else "pass"}
                return {builtin}(self)
            def __{builtin}__(self):
                return self.payload
    """)
    namespace = {}
    exec(source, namespace)
    entry = namespace["Entry"]()
    entry.payload = "original"
    assert entry.save() == ("replacement" if replaced else "original")
    assert [row.metric_value for row in findings(detector, tmp_path, source, "low_cohesion")] == ([] if replaced else [2])
