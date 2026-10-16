"""Validation lifecycle and uncertain caller context must not become defects."""
from test_detectors import build_project, run_detector


def scan(tmp_path, source):
    project = build_project(tmp_path / 'project', {'app/forms.py': source})
    return run_detector('find_form_issues.py', project)


def test_form_and_formset_clean_run_during_validation(tmp_path):
    rows = scan(tmp_path, '''from django import forms
from django.forms import BaseInlineFormSet
class RatingForm(forms.Form):
    def clean(self):
        return self.cleaned_data
class Ratings(BaseInlineFormSet):
    def clean(self):
        return [form.cleaned_data for form in self.forms]
''')
    assert not [r for r in rows if r['smell_type'] == 'cleaned_data_before_validation']


def test_short_circuit_getattr_guard_protects_direct_read(tmp_path):
    rows = scan(tmp_path, '''def _rated_forms(forms):
    return [form for form in forms
            if "rating" in getattr(form, "cleaned_data", {}) and "bg" in form.cleaned_data]
''')
    assert not rows


def test_helper_with_unknown_callers_is_only_a_candidate(tmp_path):
    rows = scan(tmp_path, '''def allocation_status(form):
    return form.cleaned_data['rating']
''')
    assert len(rows) == 1
    assert rows[0]['kind'] == 'candidate'
    assert rows[0]['also_caused_by']


def test_commit_false_does_not_prove_missing_m2m_write(tmp_path):
    rows = scan(tmp_path, '''from django import forms
from django.db import models
class Note(models.Model):
    text = models.CharField(max_length=100)
class NoteForm(forms.ModelForm):
    class Meta:
        model = Note
        fields = ['text']
def save(form):
    instance = form.save(commit=False)
    instance.save()
    return instance
''')
    assert len(rows) == 1
    assert rows[0]['smell_type'] == 'commit_false_without_save_m2m'
    assert rows[0]['kind'] == 'candidate'
    assert rows[0]['also_caused_by']


def test_unrelated_guard_does_not_hide_another_receiver(tmp_path):
    rows = scan(tmp_path, '''def helper(form, other):
    return "rating" in getattr(form, "cleaned_data", {}) and other.cleaned_data
''')
    assert len(rows) == 1
    assert rows[0]['kind'] == 'candidate'


def test_clean_named_helper_does_not_get_formset_lifecycle_exemption(tmp_path):
    rows = scan(tmp_path, '''class Helper:
    def clean(self):
        return [form.cleaned_data for form in self.forms]
''')
    assert len(rows) == 1
    assert rows[0]['kind'] == 'candidate'


def test_truthy_getattr_default_does_not_prove_attribute_exists(tmp_path):
    rows = scan(tmp_path, '''def helper(form):
    return getattr(form, "cleaned_data", True) and form.cleaned_data
''')
    assert len(rows) == 1
    assert rows[0]['kind'] == 'candidate'
