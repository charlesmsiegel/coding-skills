"""Behavioral CLI contract for bounded semantic evidence, including isolated installs."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
QUOTE = '''def wrap(name):
    return '"' + name.replace('"', '""') + '"'
'''


def scan(tmp_path, skill, detector, source, suffix='.py'):
    installed = tmp_path / 'installed' / skill
    if not installed.exists():
        shutil.copytree(ROOT / 'skills' / skill, installed, ignore=shutil.ignore_patterns('__pycache__'))
    project = tmp_path / 'foreign'
    project.mkdir(exist_ok=True)
    sample = project / ('sample' + suffix)
    sample.write_text(source)
    result = subprocess.run([sys.executable, str(installed / 'scripts' / (detector + '.py')),
                             str(sample), '--format', 'json'], cwd=project,
                            capture_output=True, text=True, check=True)
    assert not result.stderr, result.stderr
    return json.loads(result.stdout)


def sql(tmp_path, source):
    return [r for r in scan(tmp_path, 'python-code-doctor', 'find_security_issues', source)
            if r['smell_type'] == 'sql_injection']


@pytest.mark.parametrize('query', [
    "f'DELETE FROM {wrap(name)}'", "f'SELECT * FROM {table}'",
    "'DELETE FROM ' + wrap(name)", "'DELETE FROM %s' % wrap(name)",
    "'DELETE FROM {}'.format(wrap(name))",
])
def test_local_identifier_quote_candidate(tmp_path, query):
    records = sql(tmp_path, QUOTE + 'def run(conn, name):\n    table = wrap(name)\n    conn.execute(' + query + ')\n')
    assert len(records) == 1
    assert records[0]['kind'] == 'candidate'
    assert records[0]['also_caused_by'] and not records[0]['suggestion']


@pytest.mark.parametrize('body', [
    "return '\"' + name + '\"'", "return '\"' + name.replace(\"'\", \"''\") + '\"'",
    "return '\"' + name.replace('\"', '\"\"', 1) + '\"'",
    "return name.replace('\"', '\"\"')", "return '\"' + name.replace('\"', '') + '\"'",
])
def test_incorrect_quote_body_finding(tmp_path, body):
    assert sql(tmp_path, 'def wrap(name):\n    ' + body + '\nconn.execute(f\'DELETE FROM {wrap(name)}\')\n')[0]['kind'] == 'finding'


@pytest.mark.parametrize('setup,query', [
    ('', '''f"SELECT * FROM items WHERE x = '{wrap(name)}'"'''),
    ('', "f'DELETE FROM items -- {wrap(name)}'"),
    ('', "f'DELETE FROM items /* {wrap(name)} */'"),
    ('', "f'DELETE FROM {wrap(name)} WHERE x = {value}'"),
    ('', "f'DELETE FROM {wrap(name)!r}'"),
    ('', "f'DELETE FROM {wrap(name):.3}'"),
    ('', "'DELETE FROM %.3s' % wrap(name)"),
    ('', "'DELETE FROM {!r}'.format(wrap(name))"),
    ('', "'DELETE FROM {:.3}'.format(wrap(name))"),
    ('', "f'DELETE FROM {wrap(name).lower()}'"),
    ('wrap = other\n', "f'DELETE FROM {wrap(name)}'"),
    ('table = wrap(name)\ntable = value\n', "f'DELETE FROM {table}'"),
    ('table = wrap(name)\ntable += value\n', "f'DELETE FROM {table}'"),
])
def test_quote_near_misses_remain_findings(tmp_path, setup, query):
    assert sql(tmp_path, QUOTE + setup + 'conn.execute(' + query + ')\n')[0]['kind'] == 'finding'


@pytest.mark.parametrize('parameter', ['wrap', 'table'])
def test_shadowed_quote_binding(tmp_path, parameter):
    source = QUOTE + 'table = wrap(name)\ndef run(conn, ' + parameter + '):\n'
    query = 'wrap(name)' if parameter == 'wrap' else 'table'
    assert sql(tmp_path, source + "    conn.execute(f'DELETE FROM {" + query + "}')\n")[0]['kind'] == 'finding'


@pytest.mark.parametrize('first,second,unreachable', [
    ('Exception', 'BaseException', False), ('Exception', 'KeyboardInterrupt', False),
    ('Exception', 'SystemExit', False), ('Exception', 'GeneratorExit', False),
    ('Exception', 'asyncio.CancelledError', False),
    ('Exception', '(ValueError, KeyboardInterrupt)', False),
    ('Exception', '(ValueError, signals[0])', False),
    ('Exception', '(ValueError, factory())', False),
    ('Exception', '(ValueError, *signals)', False),
    ('Exception', 'UnknownError', False), ('UnknownError', 'ValueError', False),
    ('Exception', 'ValueError', True), ('BaseException', 'Exception', True),
    ('(ValueError, TypeError)', '(ValueError, TypeError)', True),
])
def test_exception_complete_coverage(tmp_path, first, second, unreachable):
    source = f'try:\n    work()\nexcept {first}:\n    handle()\nexcept {second}:\n    cleanup()\n    raise\n'
    records = scan(tmp_path, 'python-code-doctor', 'find_exception_issues', source)
    assert bool([r for r in records if r['smell_type'] == 'unreachable_except']) == unreachable


@pytest.mark.parametrize('source,expected', [
    ("test('a', () => { type Discovery = string; }); test('b', () => { type Discovery = number; });", None),
    ('function a() { type T = string; } function b() { type T = number; }', None),
    ('{ type T = string; } { type T = number; }', None),
    ('namespace N { type T = string; } namespace N { type T = number; }', None),
    ('namespace N { export type T = string; } namespace N { export type T = number; }', 'finding'),
    ('declare namespace N { type T = string; } declare namespace N { type T = number; }', 'finding'),
    ('function T() {} type T = string;', None),
    ('interface T { x: string; } interface T { y: number; }', None),
    ('enum E { A = 1 } enum E { B = 2 }', None),
    ('enum E { A = 1 } enum E { A = 2 }', 'finding'),
    ('const enum E { A = 1 } enum E { B = 2 }', 'finding'),
    ('enum E { A } enum E { B }', 'finding'),
    ('enum E { A = "x", B } enum E { C = 2 }', 'finding'),
    ('enum E { A = compute() } enum E { B = 2 }', 'candidate'),
    ('enum E { "\\u0041" = 1 } enum E { A = 2 }', 'finding'),
    ('type T = string; type T = number;', 'finding'),
    ('function f() {} function f() {}', 'finding'),
])
def test_typescript_lexical_bindings(tmp_path, source, expected):
    records = [r for r in scan(tmp_path, 'typescript-code-doctor', 'find_ai_scaffolding', source, '.ts')
               if r['smell_type'] == 'duplicate_definition']
    assert [r['kind'] for r in records] == ([] if expected is None else [expected])
    if 'type T' in source and records:
        assert 'runtime' not in records[0]['suggestion'].lower()


@pytest.mark.parametrize('source,expected', [
    ('namespace N { namespace P { export type T = string; } } namespace N { namespace P { export type T = number; } }', None),
    ('namespace N { export namespace P { export type T = string; } } namespace N { export namespace P { export type T = number; } }', 'finding'),
    ('namespace N { declare namespace P { type T = string; } } namespace N { declare namespace P { type T = number; } }', None),
    ('interface I { x: string; } interface I { x: number; }', 'finding'),
    ('interface I { f(): void; } interface I { f(x: string): void; }', 'candidate'),
    ('enum E { A = unknown, B } enum E { C = 2 }', 'candidate'),
    ('enum E { A = 1, A = 2 }', 'finding'),
    ('enum E { "\\x41" = 1 } enum E { A = 2 }', 'finding'),
])
def test_typescript_merge_evidence(tmp_path, source, expected):
    records = [r for r in scan(tmp_path, 'typescript-code-doctor', 'find_ai_scaffolding', source, '.ts')
               if r['smell_type'] == 'duplicate_definition']
    assert [r['kind'] for r in records] == ([] if expected is None else [expected])


def test_except_star_not_an_ordinary_handler_order(tmp_path):
    records = scan(tmp_path, 'python-code-doctor', 'find_exception_issues',
                   'try:\n    work()\nexcept* Exception:\n    handle()\nexcept* ValueError:\n    handle()\n')
    assert not [r for r in records if r['smell_type'] == 'unreachable_except']


@pytest.mark.parametrize('source', [
    QUOTE + 'def run(conn, wrap):\n    conn.execute(f"DELETE FROM {wrap(name)}")\n',
    QUOTE + 'def run(conn):\n    global wrap\n    conn.execute(f"DELETE FROM {wrap(name)}")\n',
    QUOTE + 'def run(conn):\n    wrap = other\n    conn.execute(f"DELETE FROM {wrap(name)}")\n',
    QUOTE + 'def run(conn):\n    table = wrap(name)\n    if flag:\n        table = other\n    conn.execute(f"DELETE FROM {table}")\n',
    QUOTE + 'def run(conn):\n    conn.execute(f"DELETE FROM {wrap(name)}")\n    wrap = other\n',
    'wrap = external\nconn.execute(f"DELETE FROM {wrap(name)}")\n',
])
def test_lexical_quote_evidence_is_required(tmp_path, source):
    assert sql(tmp_path, source)[0]['kind'] == 'finding'


@pytest.mark.parametrize('source', [
    'Exception = KeyboardInterrupt\ntry:\n    work()\nexcept Exception:\n    handle()\nexcept ValueError:\n    raise\n',
    'def run(ValueError):\n    try:\n        work()\n    except Exception:\n        handle()\n    except ValueError:\n        raise\n',
    'class ValueError(BaseException):\n    pass\ntry:\n    work()\nexcept Exception:\n    handle()\nexcept ValueError:\n    raise\n',
])
def test_rebound_builtin_exception_inheritance_is_unproven(tmp_path, source):
    records = scan(tmp_path, 'python-code-doctor', 'find_exception_issues', source)
    assert not [r for r in records if r['smell_type'] == 'unreachable_except']


@pytest.mark.parametrize('source,expected', [
    (r'enum E { \u0041 = 1 } enum E { A = 2 }', 'finding'),
    ('function outer() { function f() {} function f() {} }', 'finding'),
    ('namespace N { export function f() {} } namespace N { export function f() {} }', 'finding'),
    ('namespace N { function f() {} } namespace N { function f() {} }', None),
])
def test_typescript_nested_implementations_and_escaped_names(tmp_path, source, expected):
    records = [r for r in scan(tmp_path, 'typescript-code-doctor', 'find_ai_scaffolding', source, '.ts')
               if r['smell_type'] == 'duplicate_definition']
    assert [r['kind'] for r in records] == ([] if expected is None else [expected])


@pytest.mark.parametrize('query', [
    "f'SELECT $$ FROM {wrap(name)} $$'",
    "f'SELECT $tag$ FROM {wrap(name)} $tag$'",
    "f'SELECT 1 /* outer /* inner */ FROM {wrap(name)} */'",
    "f\"SELECT 'unclosed FROM {wrap(name)}\"",
    "f'SELECT 1 /* unclosed FROM {wrap(name)}'",
])
def test_unsupported_sql_lexing_never_proves_identifier_position(tmp_path, query):
    assert sql(tmp_path, QUOTE + 'conn.execute(' + query + ')\n')[0]['kind'] == 'finding'


def test_computed_enum_cannot_supply_an_implicit_initializer(tmp_path):
    records = scan(tmp_path, 'typescript-code-doctor', 'find_ai_scaffolding',
                   'enum E { A = compute(), B } enum E { C = 1 }', '.ts')
    assert [r['kind'] for r in records if r['smell_type'] == 'duplicate_definition'] == ['finding']


def test_dotted_and_nested_exported_namespaces_share_the_binding(tmp_path):
    records = scan(tmp_path, 'typescript-code-doctor', 'find_ai_scaffolding',
                   'namespace A.B { export type T = string; } '
                   'namespace A { export namespace B { export type T = number; } }', '.ts')
    assert [r['kind'] for r in records if r['smell_type'] == 'duplicate_definition'] == ['finding']
