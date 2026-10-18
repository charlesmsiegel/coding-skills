"""Form saves and validation must refer to the same local form instance."""

import textwrap

import pytest

from helpers import build_project, run_detector, smells


@pytest.mark.parametrize(('body', 'reported'), [
    # The original false positive: model setup precedes form inspection.
    ('self.character.save()\nform = OrderForm(instance=self.character)', False),
    ('form = OrderForm()\nself.character.save()', False),
    ('form = OrderForm()\nform.save()', True),
    ('form = OrderForm()\nif form.is_valid():\n    form.save()', False),
    ('form = OrderForm()\nother.is_valid()\nform.save()', True),
    ('form = OrderForm()\nother = OrderForm()\nother.is_valid()\nform.save()', True),
    ('form = OrderForm()\nform.save()\nform.is_valid()', True),
    ('form = OrderForm()\nalias = form\nalias.is_valid()\nform.save()', False),
    ('form = OrderForm()\nalias = form\nalias.save()', True),
    ('form = OrderForm()\nform.is_valid()\nform = OrderForm()\nform.save()', True),
    ('form = OrderForm()\nform = self.character\nform.save()', False),
    ('self.form = OrderForm()\nself.form.save()', True),
    ('self.form = OrderForm()\nself.form.is_valid()\nself.form.save()', False),
    ('OrderForm().save()', True),
    ('form: OrderForm = OrderForm()\nform.save()', True),
    ('form = OrderForm()\ndef validate():\n    form.is_valid()\nform.save()', True),
    ('form = OrderForm()\ndef unrelated():\n    self.character.save()', False),
    ('def nested():\n    form = OrderForm()\n    form.save()', True),
    ('form = OrderForm()\nalias = form\nfor form in records:\n    form.save()\nalias.save()', True),
    ('form = OrderForm()\nform: OrderForm\nform.save()', True),
    ('form = OrderForm()\n(alias := form)\nalias.save()', True),
])
def test_form_save_receiver_tracking(tmp_path, body, reported):
    project = build_project(tmp_path / 'p', {
        'shop/forms.py': 'from django import forms\nclass OrderForm(forms.Form):\n    pass\n',
        'shop/views.py': 'from .forms import OrderForm\ndef create(self):\n'
                         + textwrap.indent(body, '    ') + '\n',
    })
    findings = run_detector('find_form_issues.py', project)
    assert ('unvalidated_form_use' in smells(findings)) is reported
    for finding in findings:
        if finding['smell_type'] == 'unvalidated_form_use':
            assert finding['kind'] == 'candidate'
            assert not finding['suggestion']
            assert finding['also_caused_by']
    if body.startswith('def nested'):
        assert sum(f['smell_type'] == 'unvalidated_form_use' for f in findings) == 1


@pytest.mark.parametrize('definition', [
    'class OrderForm(forms.ModelForm):\n    pass\n',
    'class ParentForm(forms.ModelForm):\n    pass\n'
    'class OrderForm(ParentForm):\n    pass\n',
    'class OrderForm(forms.Form):\n'
    '    def save(self):\n        self.full_clean()\n        return self.cleaned_data\n',
    'class OrderForm(forms.Form):\n'
    '    def save(self):\n        return self.instance\n',
])
def test_missing_explicit_validation_does_not_prove_a_bypass(tmp_path, definition):
    project = build_project(tmp_path / 'p', {
        'shop/forms.py': 'from django import forms\n' + definition,
        'shop/views.py': 'from .forms import OrderForm\n'
                         'def create(request):\n'
                         '    form = OrderForm(request.POST)\n'
                         '    return form.save()\n',
    })
    findings = [f for f in run_detector('find_form_issues.py', project)
                if f['smell_type'] == 'unvalidated_form_use']
    assert len(findings) == 1
    assert findings[0]['kind'] == 'candidate'
    assert not findings[0]['suggestion']
    assert 'full_clean()' in ' '.join(findings[0]['also_caused_by'])


@pytest.mark.parametrize('replacement', [
    'for form in records:\n    form.save()',
    'form, other = record, None\nform.save()',
    '(form, (other, third)) = record\nform.save()',
    'form, *others = records\nform.save()',
    'with context() as form:\n    form.save()',
    'try:\n    operation()\nexcept Exception as form:\n    form.save()',
    'def form():\n    pass\nform.save()',
    'class form:\n    pass\nform.save()',
    'del form\nform.save()',
    'import form\nform.save()',
    'from models import record as form\nform.save()',
    'form += record\nform.save()',
    '(form := record)\nform.save()',
])
def test_unsupported_binding_invalidates_form_receiver(tmp_path, replacement):
    project = build_project(tmp_path / 'p', {
        'shop/forms.py': 'from django import forms\nclass OrderForm(forms.Form):\n    pass\n',
        'shop/views.py': 'from .forms import OrderForm\ndef create(self):\n'
                         '    form = OrderForm()\n'
                         + textwrap.indent(replacement, '    ') + '\n',
    })
    assert 'unvalidated_form_use' not in smells(run_detector('find_form_issues.py', project))


@pytest.mark.parametrize('body', [
    'holder.form = OrderForm()\nholder = record\nholder.form.save()',
    'holder.inner.form = OrderForm()\nholder.inner = record\nholder.inner.form.save()',
    'form = OrderForm()\nmatch record:\n    case {"row": form}:\n        form.save()',
    'form = OrderForm()\nmatch record:\n    case {"row": row, **form}:\n        form.save()',
    'form = OrderForm()\nmatch record:\n    case [*form]:\n        form.save()',
    'form = OrderForm()\nmatch record:\n    case _ as form:\n        form.save()',
])
def test_descendant_and_pattern_bindings_forget_stale_receiver(tmp_path, body):
    project = build_project(tmp_path / 'p', {
        'shop/forms.py': 'from django import forms\nclass OrderForm(forms.Form):\n    pass\n',
        'shop/views.py': 'from .forms import OrderForm\ndef create(self):\n'
                         + textwrap.indent(body, '    ') + '\n',
    })
    assert 'unvalidated_form_use' not in smells(run_detector('find_form_issues.py', project))


@pytest.mark.parametrize('expression', [
    '[form for form in records]',
    '{form for form in records}',
    '{form: form for form in records}',
    '(form for form in records)',
    '[row for row, form in records if form]',
])
def test_comprehension_target_preserves_outer_form_receiver(tmp_path, expression):
    project = build_project(tmp_path / 'p', {
        'shop/forms.py': 'from django import forms\nclass OrderForm(forms.Form):\n    pass\n',
        'shop/views.py': 'from .forms import OrderForm\ndef create(self):\n'
                         '    form = OrderForm()\n'
                         f'    rows = {expression}\n'
                         '    form.save()\n',
    })
    assert 'unvalidated_form_use' in smells(run_detector('find_form_issues.py', project))


@pytest.mark.parametrize('expression', [
    '[form.save() for form in records]',
    '{form.save() for form in records}',
    '{form: form.save() for form in records}',
    '(form.save() for form in records)',
])
def test_comprehension_save_uses_its_local_target(tmp_path, expression):
    project = build_project(tmp_path / 'p', {
        'shop/forms.py': 'from django import forms\nclass OrderForm(forms.Form):\n    pass\n',
        'shop/views.py': 'from .forms import OrderForm\ndef create(self):\n'
                         '    form = OrderForm()\n'
                         f'    rows = {expression}\n',
    })
    assert 'unvalidated_form_use' not in smells(run_detector('find_form_issues.py', project))
