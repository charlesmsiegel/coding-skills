"""True positives retained by the TypeScript false-positive filters."""

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / 'skills/typescript-code-doctor/scripts'


def records(tmp_path, run_script, script, source, smell, filename='sample.ts'):
    target = tmp_path / filename
    target.write_text(source, encoding='utf-8')
    result = run_script(SCRIPTS / script, target, '--format', 'json')
    import json
    return [r for r in json.loads(result.stdout) if r['smell_type'] == smell]


@pytest.mark.parametrize('replacement', [
    'const checkOutput = () => {}; checkOutput();',
    'function checkOutput() {} checkOutput();',
])
def test_shadowed_helper_does_not_borrow_outer_assertions(tmp_path, run_script, replacement):
    source = ('function checkOutput() { expect(1).toBe(1); }\n'
              'test("shadowed", () => { ' + replacement + ' });\n'
              'test("real helper", () => { checkOutput(); });\n')
    found = records(tmp_path, run_script, 'find_test_smells.py', source,
                    'test_without_assertion', 'sample.test.ts')
    assert [r['line'] for r in found] == [2]


def test_parameter_shadows_asserting_helper(tmp_path, run_script):
    source = ('function checkOutput() { expect(1).toBe(1); }\n'
              'function register(checkOutput: () => void) {\n'
              '  test("shadowed", () => { checkOutput(); });\n}\n')
    found = records(tmp_path, run_script, 'find_test_smells.py', source,
                    'test_without_assertion', 'sample.test.ts')
    assert [r['line'] for r in found] == [3]


@pytest.mark.parametrize('handler', ['undefined', 'null', 'false', '42', '"ignored"', '{}', '[]',
                                    'void 0', 'missingHandler'])
def test_noncallable_then_argument_does_not_handle_rejection(tmp_path, run_script, handler):
    source = ('const missingHandler = undefined;\n'
              'const pending = Promise.reject(new Error("lost"));\n'
              f'pending.then(undefined, {handler});\n')
    found = records(tmp_path, run_script, 'find_async_issues.py', source, 'floating_promise')
    assert [r['line'] for r in found] == [3]


def test_callable_then_argument_handles_rejection(tmp_path, run_script):
    source = ('function run(onErr: (e: unknown) => void) {\n'
              ' const pending = Promise.reject(new Error("handled"));\n'
              ' pending.then(undefined, onErr);\n'
              ' pending.then(undefined, () => report());\n}\n')
    assert not records(tmp_path, run_script, 'find_async_issues.py', source, 'floating_promise')


@pytest.mark.parametrize('with_test', [False, True])
def test_build_config_only_has_no_source_coverage_gap(tmp_path, run_script, with_test):
    (tmp_path / 'package.json').write_text('{}', encoding='utf-8')
    (tmp_path / 'vite.config.ts').write_text('export default {};\n', encoding='utf-8')
    if with_test:
        (tmp_path / 'sample.test.ts').write_text('test("works", () => { expect(1).toBe(1); });\n',
                                                 encoding='utf-8')
    result = run_script(SCRIPTS / 'find_untested_modules.py', tmp_path, '--format', 'json')
    import json
    assert json.loads(result.stdout) == []


@pytest.mark.parametrize(('declaration', 'callee'), [
    ('import * as files from "node:fs/promises";', 'files.open'),
    ('import disk from "node:fs/promises";', 'disk.open'),
    ('import { promises as files } from "node:fs";', 'files.open'),
    ('import { open as openFile } from "node:fs/promises";', 'openFile'),
])
def test_imported_fs_alias_still_requires_close(tmp_path, run_script, declaration, callee):
    source = (declaration + '\nexport async function read(path: string) {\n'
              f' const handle = await {callee}(path);\n return handle.readFile();\n}}\n')
    found = records(tmp_path, run_script, 'find_resource_leaks.py', source, 'unreleased_resource')
    assert [r['line'] for r in found] == [3]


@pytest.mark.parametrize('body', ['error;', 'void error;', '(error);', 'void (error);'])
def test_bare_exception_read_does_not_record_failure(tmp_path, run_script, body):
    source = 'try { work(); } catch (error) { ' + body + ' }\n'
    found = records(tmp_path, run_script, 'find_exception_issues.py', source, 'swallowed_error')
    assert len(found) == 1
    assert found[0]['kind'] == 'finding'


def test_error_given_to_handler_is_not_a_bare_read(tmp_path, run_script):
    source = 'try { work(); } catch (error) { handle(error) }\n'
    assert not records(tmp_path, run_script, 'find_exception_issues.py', source, 'swallowed_error')


@pytest.mark.parametrize(('declaration', 'callee'), [
    ('import { useEffect as effect } from "react";', 'effect'),
    ('import * as UI from "react";', 'UI.useEffect'),
    ('import UI from "react";', 'UI.useEffect'),
])
def test_real_react_effect_aliases_keep_cleanup_contract(tmp_path, run_script, declaration, callee):
    source = declaration + '\n' + callee + '(() => { if (ready) return () => clean(); return; });\n'
    assert not records(tmp_path, run_script, 'find_code_smells.py', source, 'inconsistent_returns')


@pytest.mark.parametrize('source', [
    'import { useEffect } from "./custom";\nuseEffect(() => { if (ready) return 42; return; });\n',
    'import { useEffect } from "react";\nfunction run(useEffect: Function) {\n'
    ' useEffect(() => { if (ready) return 42; return; });\n}\n',
    'import { useEffect } from "react";\nfunction run() {\n'
    ' const useEffect = custom;\n useEffect(() => { if (ready) return 42; return; });\n}\n',
    'import * as React from "react";\nfunction run(React: Custom) {\n'
    ' React.useEffect(() => { if (ready) return 42; return; });\n}\n',
])
def test_unrelated_or_shadowed_effect_does_not_hide_returns(tmp_path, run_script, source):
    found = records(tmp_path, run_script, 'find_code_smells.py', source, 'inconsistent_returns')
    assert len(found) == 1


@pytest.mark.parametrize('annotation, default', [
    ('Record<string, number>', '{}'),
    ('Map<string, Array<number>>', 'new Map()'),
    ('Map<string, Array<Set<number>>>', 'new Map()'),
])
def test_fused_generic_closer_and_default_keep_parameter_boundaries(load_module, annotation, default):
    parser = load_module(SCRIPTS, 'tsparse')
    file = parser.parse_source(f'function f(x: {annotation}={default}, y: number) {{}}')
    func, = file.functions
    assert [(p.name, p.type_text, p.has_default) for p in func.params] == [
        ('x', annotation, True), ('y', 'number', False),
    ]


def test_default_comparison_still_ends_at_parameter_comma(load_module):
    parser = load_module(SCRIPTS, 'tsparse')
    file = parser.parse_source('function f(x: boolean = a < b, y: number = 1) {}')
    func, = file.functions
    assert [p.name for p in func.params] == ['x', 'y']


@pytest.mark.parametrize('source', [
    'function run(onErr: (e: unknown) => void) {\n'
    ' const pending = Promise.reject(1);\n holder.onErr = undefined;\n'
    ' pending.then(undefined, onErr);\n}\n',
    'const onErr = undefined;\nfunction run(onErr: (e: unknown) => void) {\n'
    ' const pending = Promise.reject(1);\n pending.then(undefined, onErr);\n}\n',
    'const onErr = undefined;\nfunction run() {\n const onErr = () => report();\n'
    ' const pending = Promise.reject(1);\n pending.then(undefined, onErr);\n}\n',
    'const onErr = undefined;\nfunction run() {\n function onErr() {}\n'
    ' const pending = Promise.reject(1);\n pending.then(undefined, onErr);\n}\n',
])
def test_callable_handler_alias_uses_its_own_binding(tmp_path, run_script, source):
    assert not records(tmp_path, run_script, 'find_async_issues.py', source, 'floating_promise')


def test_catch_binding_does_not_inherit_react_import(tmp_path, run_script):
    source = ('import { useEffect } from "react";\n'
              'try {} catch (useEffect) {\n'
              ' useEffect(() => { if (ready) return 42; return; });\n}\n')
    found = records(tmp_path, run_script, 'find_code_smells.py', source, 'inconsistent_returns')
    assert len(found) == 1


def test_catch_binding_does_not_inherit_fs_import(tmp_path, run_script):
    source = ('import * as files from "node:fs/promises";\n'
              'try {} catch (files) { files.open("dialog"); }\n')
    assert not records(tmp_path, run_script, 'find_resource_leaks.py', source, 'unreleased_resource')


def test_catch_binding_does_not_borrow_assertions(tmp_path, run_script):
    source = ('function checkOutput() { expect(1).toBe(1); }\n'
              'test("no assertion", () => { try {} catch (checkOutput) { checkOutput(); } });\n')
    found = records(tmp_path, run_script, 'find_test_smells.py', source,
                    'test_without_assertion', 'sample.test.ts')
    assert len(found) == 1


def test_latest_simple_handler_assignment_may_handle_rejection(tmp_path, run_script):
    source = ('let onErr = undefined;\n onErr = e => report(e);\n'
              'const pending = Promise.reject(1);\n pending.then(undefined, onErr);\n')
    assert not records(tmp_path, run_script, 'find_async_issues.py', source, 'floating_promise')


def test_typed_noncallable_handler_does_not_handle_rejection(tmp_path, run_script):
    source = ('const missingHandler: undefined = undefined;\n'
              'const pending = Promise.reject(1);\n pending.then(undefined, missingHandler);\n')
    found = records(tmp_path, run_script, 'find_async_issues.py', source, 'floating_promise')
    assert [r['line'] for r in found] == [3]
