"""Missing local owner syntax is a review lead, not proof of unauthorized access."""
import pytest

from test_detectors import build_project, run_detector


def scan(tmp_path, source):
    project = build_project(tmp_path / 'project', {'app/views.py': source})
    return run_detector('find_view_issues.py', project)


def assert_authorization_candidate(rows, smell):
    hits = [row for row in rows if row['smell_type'] == smell]
    assert len(hits) == 1
    assert hits[0]['kind'] == 'candidate'
    assert not hits[0].get('suggestion')
    assert hits[0]['also_caused_by']
    assert 'any authenticated user can' not in hits[0]['description']
    assert 'every authenticated caller sees every row' not in hits[0]['description']


def test_internal_error_redirect_does_not_prove_a_routed_data_leak(tmp_path):
    rows = scan(tmp_path, '''from django.shortcuts import get_object_or_404, redirect
def object_error_redirect(request, object_type, pk, exc):
    obj = get_object_or_404(OBJECT_MODEL_MAP[object_type], pk=pk)
    return redirect(obj.get_absolute_url())
''')
    assert_authorization_candidate(rows, 'missing_ownership_filter')


def test_custom_policy_checked_lookup_is_not_a_proven_ownership_gap(tmp_path):
    rows = scan(tmp_path, '''from django.shortcuts import get_object_or_404
def detail(request, pk):
    obj = get_object_or_404(Article, pk=pk)
    policy.require_read(request, obj)
    return render(request, 'detail.html', {'object': obj})
''')
    assert_authorization_candidate(rows, 'missing_ownership_filter')


@pytest.mark.parametrize('body', [
    '    queryset = Article.objects.all()\n',
    '    def get_queryset(self):\n        return super().get_queryset()\n',
    '    def get_queryset(self):\n        return policy.readable_articles(self.request)\n',
])
def test_mixin_or_policy_queryset_is_not_a_proven_access_gap(tmp_path, body):
    rows = scan(tmp_path, '''from django.views.generic import ListView
class Articles(CentralAuthorizationMixin, ListView):
''' + body)
    assert_authorization_candidate(rows, 'unscoped_get_queryset')


def test_public_reference_queryset_does_not_prove_private_data_exposure(tmp_path):
    rows = scan(tmp_path, '''from django.views.generic import ListView
class Countries(ListView):
    queryset = Country.objects.all()
''')
    assert_authorization_candidate(rows, 'unscoped_get_queryset')
