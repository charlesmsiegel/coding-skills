"""Keep structural leads without mistaking local policy or outputs for defects."""

import json
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / 'skills/python-code-doctor/scripts'


def detect(run_script, tmp_path, detector, source, issue):
    sample = tmp_path / 'sample.py'
    sample.write_text(source, encoding='utf-8')
    rows = json.loads(run_script(SCRIPTS / detector, sample, '--format', 'json').stdout)
    return [row for row in rows if row.get('issue_type', row.get('smell_type')) == issue]


def test_message_accumulator_keeps_policy_as_a_wrapper_candidate(run_script, tmp_path):
    [row] = detect(run_script, tmp_path, 'find_overengineering.py', '''
class MessageUsage:
    def __init__(self):
        self._held = {}

    def note(self, message):
        held = self._held.setdefault(message.id, {})
        for name, count in message.usage.items():
            held[name] = max(held.get(name, 0), count)

    def take(self):
        total = Counter()
        for held in self._held.values():
            total.update(held)
        self._held = {}
        return dict(total)
''', 'thin_wrapper')
    assert row['kind'] == 'candidate'
    assert row['also_caused_by'] and not row['suggestion']
    assert '_held' in row['description']
    assert 'note' in row['description'] and 'take' in row['description']
    assert 'is a thin wrapper' not in row['description']


@pytest.mark.parametrize('body', [
    'return self.client.get(key)',
    '"""Read the key."""\n        return self.client.get(key)',
    'return await self.client.get(key)',
])
def test_transparent_delegator_keeps_a_finding(run_script, tmp_path, body):
    prefix = 'async ' if 'await ' in body else ''
    [row] = detect(run_script, tmp_path, 'find_overengineering.py',
                   'class Wrapper:\n'
                   '    def __init__(self, client):\n        self.client = client\n'
                   f'    {prefix}def get(self, key):\n        {body}\n'
                   '    def put(self, key, value):\n        return self.client.put(key, value)\n',
                   'thin_wrapper')
    assert row.get('kind', 'finding') == 'finding'
    assert row['suggestion'] and not row.get('also_caused_by')


@pytest.mark.parametrize('operation', [
    'def _get(self, key):\n        return self.client.get(normalize(key))',
    'def __getitem__(self, key):\n        return self.client.get(key) + 1',
    'def get(self, key):\n        if key is None:\n            raise ValueError(key)\n'
    '        return self.client.get(key)',
    'def get(self, first, second):\n        return self.client.get(second, first)',
])
def test_private_and_protocol_policy_is_not_transparent(run_script, tmp_path, operation):
    [row] = detect(run_script, tmp_path, 'find_overengineering.py',
                   'class Wrapper:\n'
                   '    def __init__(self, client):\n        self.client = client\n'
                   f'    {operation}\n'
                   '    def put(self, key, value):\n        return self.client.put(key, value)\n',
                   'thin_wrapper')
    assert row['kind'] == 'candidate'
    assert row['also_caused_by'] and not row['suggestion']


def test_published_result_keeps_single_writer_evidence_as_a_candidate(run_script, tmp_path):
    [row] = detect(run_script, tmp_path, 'find_design_smells.py', '''
class Runtime:
    def __init__(self):
        self.turns = None

    def _note(self, message):
        self.turns = message.num_turns
        observe(report(self.turns))

def completed(runtime):
    return runtime.turns
''', 'temporary_field')
    assert row['kind'] == 'candidate'
    assert row['also_caused_by'] and not row['suggestion']
    assert 'self.turns' in row['description'] and 'Runtime._note' in row['description']
    assert 'None except inside' not in row['description']


@pytest.mark.parametrize('declaration', ['self.result = None', 'self.result: object | None = None'])
@pytest.mark.parametrize('preparation', ['', '        prepare(task)\n'])
def test_result_cleared_at_operation_entry_keeps_candidate_evidence(run_script, tmp_path, declaration, preparation):
    [row] = detect(run_script, tmp_path, 'find_design_smells.py',
                   'class Strategy:\n'
                   f'    def __init__(self):\n        {declaration}\n'
                   '    def run(self, task):\n'
                   '        """Publish the last completed verification for the caller."""\n'
                   f'{preparation}'
                   f'        {declaration}\n'
                   '        for attempt in task.attempts:\n'
                   '            self.result = verify(attempt)\n'
                   '            if self.result.verified:\n'
                   '                return outcome(attempt)\n'
                   '\ndef receipt(strategy):\n    return strategy.result\n', 'temporary_field')
    assert row['kind'] == 'candidate'
    assert row['also_caused_by'] and not row['suggestion']
    assert 'self.result' in row['description'] and 'Strategy.run' in row['description']
    assert 'None except inside' not in row['description']


@pytest.mark.parametrize(('field', 'body'), [
    ('_scratch', '        self._scratch = None\n'
                 '        self._scratch = text.split()\n        log(self._scratch)\n'),
    ('scratch', '        self.scratch = None\n        self.scratch = text.split()\n'
                '        log(self.scratch)\n        self.scratch = None\n'),
    ('scratch', '        self.scratch = None; self.scratch = text.split(); '
                'log(self.scratch); self.scratch = None\n'),
    ('scratch', '        self.scratch = None\n        try:\n'
                '            self.scratch = text.split()\n            log(self.scratch)\n'
                '        finally:\n            self.scratch = None\n'),
    ('scratch', '        if text:\n            self.scratch = None\n'
                '        self.scratch = text.split()\n        log(self.scratch)\n'),
])
def test_entry_reset_does_not_hide_private_or_later_reset_scratch(run_script, tmp_path, field, body):
    [row] = detect(run_script, tmp_path, 'find_design_smells.py',
                   'class Parser:\n'
                   f'    def __init__(self):\n        self.{field} = None\n'
                   f'    def parse(self, text):\n{body}', 'temporary_field')
    assert row['kind'] == 'finding'
    assert row['suggestion'] and not row['also_caused_by']


@pytest.mark.parametrize('cleanup', [
    'del self.result',
    'self.result, text = None, text',
    'self.result, text = clear_result(text)',
    'setattr(self, "result", None)',
    'delattr(self, "result")',
    'object.__setattr__(self, "result", None)',
    'self.__delattr__("result")',
    'self.__dict__["result"] = None',
])
def test_initial_reset_does_not_hide_other_field_cleanup(run_script, tmp_path, cleanup):
    [row] = detect(run_script, tmp_path, 'find_design_smells.py',
                   'class Parser:\n'
                   '    def __init__(self):\n        self.result = None\n'
                   '    def parse(self, text):\n        self.result = None\n'
                   '        self.result = text.split()\n        log(self.result)\n'
                   f'        {cleanup}\n', 'temporary_field')
    assert row['kind'] == 'finding'
    assert row['suggestion'] and not row['also_caused_by']


@pytest.mark.parametrize(('field', 'reset'), [('_scratch', ''), ('scratch', '        self.scratch = None\n')])
def test_single_method_scratch_keeps_a_finding(run_script, tmp_path, field, reset):
    [row] = detect(run_script, tmp_path, 'find_design_smells.py',
                   'class Parser:\n'
                   f'    def __init__(self):\n        self.{field} = None\n'
                   f'    def parse(self, text):\n        self.{field} = text.split()\n'
                   f'        log(self.{field})\n{reset}', 'temporary_field')
    assert row['kind'] == 'finding'
    assert row['suggestion'] and not row['also_caused_by']


def test_nested_same_named_receiver_does_not_add_wrapper_policy(run_script, tmp_path):
    [row] = detect(run_script, tmp_path, 'find_overengineering.py', '''
class Wrapper:
    def __init__(self, client):
        self.client = client
    def get(self, key):
        def other(self):
            return self.client.get(key) + 1
        return self.client.get(key)
    def put(self, key, value):
        return self.client.put(key, value)
''', 'thin_wrapper')
    # The extra local declaration makes transparency unproven, but its own
    # receiver must not supply evidence about this wrapper's stored object.
    assert row['kind'] == 'candidate'


def test_wrapper_candidates_validate_in_the_shared_schema(run_script, tmp_path, load_module):
    [row] = detect(run_script, tmp_path, 'find_overengineering.py', '''
class Cache:
    def __init__(self):
        self.items = {}
    def get(self, key):
        return self.items.get(key, 0) + 1
    def clear(self):
        self.items.clear()
''', 'thin_wrapper')
    common = load_module(SCRIPTS, 'common')
    assert common.validate_record(row)['kind'] == 'candidate'


def test_wrapper_text_keeps_candidate_reasons_without_a_fix(run_script, tmp_path):
    sample = tmp_path / 'sample.py'
    sample.write_text('class Cache:\n'
                      '    def __init__(self):\n        self.items = {}\n'
                      '    def get(self, key):\n        return self.items.get(key, 0) + 1\n'
                      '    def clear(self):\n        self.items.clear()\n', encoding='utf-8')
    output = run_script(SCRIPTS / 'find_overengineering.py', sample).stdout
    assert '[CANDIDATE]' in output
    assert 'also caused by:' in output
    assert 'Found 0 over-engineering issue(s)' in output
    assert all(line.strip() != '→' for line in output.splitlines())


def test_nested_receiver_access_is_not_wrapper_evidence(run_script, tmp_path):
    rows = detect(run_script, tmp_path, 'find_overengineering.py', '''
class Worker:
    def __init__(self, client):
        self.client = client
    def one(self, value):
        def unrelated(self):
            return self.client.get(value)
        return value + 1
    def two(self, value):
        class Other:
            def get(self):
                return self.client.get(value)
        return value + 2
''', 'thin_wrapper')
    assert not rows


def test_transparent_variadic_delegator_keeps_a_finding(run_script, tmp_path):
    [row] = detect(run_script, tmp_path, 'find_overengineering.py', '''
class Wrapper:
    def __init__(self, client):
        self.client = client
    def get(self, key, *args, timeout=None, **kwargs):
        return self.client.get(key, *args, timeout=timeout, **kwargs)
    def put(self, key, value):
        self.client.put(key, value)
''', 'thin_wrapper')
    assert row.get('kind', 'finding') == 'finding'


@pytest.mark.parametrize('body', [
    '        try:\n            self.buffer = text.split()\n'
    '            return transform(self.buffer)\n'
    '        finally:\n            self.buffer = None\n',
    '        if not text:\n            return None\n'
    '        self.buffer = text.split()\n        log(self.buffer)\n'
    '        self.buffer = None\n',
    '        if self.buffer is not None:\n            raise RuntimeError("reentered")\n'
    '        self.buffer = text.split()\n        log(self.buffer)\n'
    '        self.buffer = None\n',
])
def test_reset_scratch_lifecycle_stays_a_finding(run_script, tmp_path, body):
    [row] = detect(run_script, tmp_path, 'find_design_smells.py',
                   'class Parser:\n'
                   '    def __init__(self):\n        self.buffer = None\n'
                   f'    def parse(self, text):\n{body}', 'temporary_field')
    assert row['kind'] == 'finding'


def test_read_before_write_without_reset_carries_persistent_state(run_script, tmp_path):
    rows = detect(run_script, tmp_path, 'find_design_smells.py', '''
class Runtime:
    def __init__(self):
        self.turns = None
    def note(self, turns):
        if self.turns is not None:
            turns += self.turns
        self.turns = turns
        log(self.turns)
''', 'temporary_field')
    assert not rows
