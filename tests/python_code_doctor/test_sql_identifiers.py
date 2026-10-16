"""SQL interpolation needs input evidence before it is scored as injection."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'skills/python-code-doctor/scripts/find_security_issues.py'


def sql_records(tmp_path, source):
    sample = tmp_path / 'sample.py'
    sample.write_text(source)
    result = subprocess.run([sys.executable, str(SCRIPT), str(sample), '--format', 'json'],
                            capture_output=True, text=True, check=True)
    return [record for record in json.loads(result.stdout) if record['smell_type'] == 'sql_injection']


def test_quoted_migration_identifiers_are_unscored_candidates(tmp_path):
    records = sql_records(tmp_path, '''
TABLE = "game_userscenereadstatus"
def migrate(connection):
    q = connection.ops.quote_name
    table, read, marker = q(TABLE), q("read"), q("last_read_post_id")
    with connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {table} WHERE user_id IS NULL")
        cursor.execute(f"UPDATE {table} SET {read} = %s, {marker} = %s", [True, 42])
''')
    assert len(records) == 2
    assert all(record['kind'] == 'candidate' for record in records)
    assert all(record['also_caused_by'] and not record['suggestion'] for record in records)


@pytest.mark.parametrize('expression', ['f"SELECT * FROM {table}"',
    '"SELECT * FROM " + table', '"SELECT * FROM %s" % table',
    '"SELECT * FROM {}".format(table)'])
def test_unknown_identifier_origin_remains_a_finding(tmp_path, expression):
    records = sql_records(tmp_path, f'def rows(cursor, table):\n    cursor.execute({expression})\n')
    assert [r['kind'] for r in records] == ['finding']


@pytest.mark.parametrize('source', ['input()', 'request.GET["value"]',
    'request.args.get("value")', 'request.json["value"]', 'sys.argv[1]'])
def test_external_values_remain_findings_through_aliases(tmp_path, source):
    records = sql_records(tmp_path, f'''def rows(cursor, request):
    value = {source}
    alias = value
    cursor.execute(f"SELECT * FROM items WHERE value = '{{alias}}'")
''')
    assert [r['kind'] for r in records] == ['finding']


def test_quote_name_does_not_suppress_untrusted_value_interpolation(tmp_path):
    records = sql_records(tmp_path, '''def rows(cursor, connection):
    value = connection.ops.quote_name(input())
    cursor.execute(f"SELECT * FROM items WHERE value = '{value}'")
''')
    assert [r['kind'] for r in records] == ['finding']



def test_unknown_value_interpolation_remains_a_finding(tmp_path):
    records = sql_records(tmp_path, "def rows(cursor, value):\n    cursor.execute(f'SELECT * FROM items WHERE id = {value}')\n")
    assert [r['kind'] for r in records] == ['finding']


def test_quoted_identifiers_do_not_hide_untrusted_values(tmp_path):
    records = sql_records(tmp_path, """def rows(cursor, connection, value):
    table = connection.ops.quote_name('items')
    cursor.execute(f"SELECT * FROM {table} WHERE id = {value}")
""")
    assert [r['kind'] for r in records] == ['finding']


@pytest.mark.parametrize('expression', ['f"SELECT * FROM {table}"',
    '"SELECT * FROM " + table', '"SELECT * FROM %s" % table',
    '"SELECT * FROM {}".format(table)'])
def test_quoted_constant_identifier_formats_are_candidates(tmp_path, expression):
    records = sql_records(tmp_path, f'''def rows(cursor, connection):
    table = connection.ops.quote_name('items')
    cursor.execute({expression})
''')
    assert [r['kind'] for r in records] == ['candidate']


@pytest.mark.parametrize('setup', [
    "table = connection.ops.quote_name('items')\n    table = input()",
    "table = connection.ops.quote_name(value)",
])
def test_unknown_or_reassigned_quoted_identifier_remains_finding(tmp_path, setup):
    records = sql_records(tmp_path, f'''def rows(cursor, connection, value):
    {setup}
    cursor.execute(f"SELECT * FROM {{table}}")
''')
    assert [r['kind'] for r in records] == ['finding']


def test_parameter_shadows_module_identifier_constant(tmp_path):
    records = sql_records(tmp_path, '''TABLE = 'items'
def rows(cursor, connection, TABLE):
    table = connection.ops.quote_name(TABLE)
    cursor.execute(f"SELECT * FROM {table}")
''')
    assert [r['kind'] for r in records] == ['finding']


@pytest.mark.parametrize('overwrite', [
    'if (table := input()):\n        cursor.execute(f"SELECT * FROM {table}")',
    'for table in values:\n        cursor.execute(f"SELECT * FROM {table}")',
    'with context() as table:\n        cursor.execute(f"SELECT * FROM {table}")',
    'try:\n        operation()\n    except Exception as table:\n        cursor.execute(f"SELECT * FROM {table}")',
    'table += input()\n    cursor.execute(f"SELECT * FROM {table}")',
    'import table\n    cursor.execute(f"SELECT * FROM {table}")',
])
def test_other_binding_writes_invalidate_constant_identifier(tmp_path, overwrite):
    records = sql_records(tmp_path, f'''def rows(cursor, connection, values):
    table = connection.ops.quote_name('items')
    {overwrite}
''')
    assert [r['kind'] for r in records] == ['finding']
