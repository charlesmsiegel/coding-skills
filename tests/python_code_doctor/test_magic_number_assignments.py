"""Named attribute values follow the same contract as named local values."""

import json
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[2] / 'skills/python-code-doctor/scripts/find_code_smells.py'


def magic_numbers(tmp_path, source):
    path = tmp_path / 'sample.py'
    path.write_text(source)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(path), '--format', 'json'],
        capture_output=True, text=True, check=True,
    )
    return [f for f in json.loads(result.stdout) if f['smell_type'] == 'magic_number']


def test_direct_attribute_assignments_name_the_value(tmp_path):
    assert not magic_numbers(tmp_path, '''
def initialize(character, settings):
    character.freebies = 15
    character.balance: int = -30
    settings.retry_delay = +45
    character.profile.age = 501
''')


def test_attribute_exemption_does_not_hide_computation_or_thresholds(tmp_path):
    findings = magic_numbers(tmp_path, '''
def update(character, age):
    character.freebies = age * 15
    character.balance += 30
    character.score = calculate(45)
    return character.age > 501
''')
    assert {f['description'] for f in findings} == {
        'Magic number 15', 'Magic number 30', 'Magic number 45', 'Magic number 501',
    }
