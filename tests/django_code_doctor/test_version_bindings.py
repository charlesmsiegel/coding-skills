"""Deprecated API matches follow Django imports, not coincidental spellings."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'skills/django-code-doctor/scripts/find_version_issues.py'


def version_records(tmp_path, source):
    (tmp_path / 'manage.py').write_text('import django\n')
    (tmp_path / 'sample.py').write_text(source)
    result = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path), '--from', '3.2',
                             '--target', '5.2', '--format', 'json'],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('source', [
    'class Actions:\n    def url(self, name): return name\n    def test(self): self.url("approve")\n',
    'from other.urls import url\nurl("approve")\n',
    'def url(name): return name\nurl("approve")\n',
    'from django.conf.urls import url\ndef f(url): url("approve")\n',
    'from django.conf.urls import url\nurl = custom\nurl("approve")\n',
])
def test_unrelated_url_calls_are_not_removed_django_api(tmp_path, source):
    records = version_records(tmp_path, source)
    assert not any('`url`' in record['description'] for record in records)


@pytest.mark.parametrize('source', [
    'from django.conf.urls import url\nurl("route", view)\n',
    'from django.conf.urls import url as old_url\nold_url("route", view)\n',
    'import django.conf.urls\ndjango.conf.urls.url("route", view)\n',
    'import django.conf.urls as urls\nurls.url("route", view)\n',
    'from django.conf import urls as routing\nrouting.url("route", view)\n',
    'def f():\n    from django.conf.urls import url as route\n    route("route", view)\n',
])
def test_real_deprecated_url_bindings_remain_findings(tmp_path, source):
    records = version_records(tmp_path, source)
    assert any('`url`' in record['description'] for record in records)


@pytest.mark.parametrize('source', [
    'from datetime import timezone\nvalue = timezone.utc\n',
    'import datetime\nvalue = datetime.timezone.utc\n',
    'from other import timezone\nvalue = timezone.utc\n',
    'from django.utils import timezone\ndef f(timezone): return timezone.utc\n',
])
def test_non_django_timezone_is_not_removed_api(tmp_path, source):
    records = version_records(tmp_path, source)
    assert not any('`timezone.utc`' in record['description'] for record in records)


@pytest.mark.parametrize('source', [
    'from django.utils import timezone\nvalue = timezone.utc\n',
    'from django.utils import timezone as tz\nvalue = tz.utc\n',
    'import django.utils.timezone as tz\nvalue = tz.utc\n',
    'import django.utils.timezone\nvalue = django.utils.timezone.utc\n',
    'from django.utils.timezone import utc as old_utc\nvalue = old_utc\n',
])
def test_real_django_timezone_bindings_remain_findings(tmp_path, source):
    records = version_records(tmp_path, source)
    assert any('`timezone.utc`' in record['description'] for record in records)


def test_class_method_name_does_not_shadow_import_used_inside_other_method(tmp_path):
    records = version_records(tmp_path, '''from django.conf.urls import url
class Routes:
    def url(self): pass
    def build(self): return url('route', view)
''')
    assert any('`url`' in record['description'] for record in records)


def test_local_import_does_not_leak_to_another_function(tmp_path):
    records = version_records(tmp_path, '''def configure():
    from django.conf.urls import url
def unrelated(url):
    url('approve')
''')
    assert not any('`url`' in record['description'] for record in records)


def test_function_default_uses_enclosing_import_even_when_parameter_shadows_it(tmp_path):
    records = version_records(tmp_path, '''from django.utils import timezone
def f(timezone=timezone.utc):
    return timezone
''')
    assert any('`timezone.utc`' in record['description'] for record in records)
