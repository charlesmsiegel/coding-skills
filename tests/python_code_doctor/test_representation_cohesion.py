"""Presentation protocols neither create nor hide behavioral LCOM4 groups."""

import ast
import textwrap
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills/python-code-doctor/scripts"


@pytest.fixture
def detector(load_module):
    return load_module(SCRIPTS, "find_coupling_issues")


def cohesion(detector, tmp_path, source):
    sample = tmp_path / "app.py"
    sample.write_text(source, encoding="utf-8")
    return [record for record in detector.coupling_issues(ast.parse(source), str(sample))
            if record.issue_type == "low_cohesion"]


@pytest.mark.parametrize("presentation", ["__repr__", "__str__"])
def test_presentation_of_an_identity_field_does_not_split_cached_behavior(
        detector, tmp_path, presentation):
    source = textwrap.dedent(f"""
        class Entry:
            def __init__(self, ref, text):
                self.ref = ref
                self.text = text
                self._features = None

            def features(self):
                if self._features is None:
                    self._features = tuple(self.text.split())
                return self._features

            def {presentation}(self):
                return self.ref
    """)
    assert cohesion(detector, tmp_path, source) == []


@pytest.mark.parametrize("presentation", ["__repr__", "__str__"])
@pytest.mark.parametrize("render", [
    "str((self.left_value, self.right_value))",
    "str((self.left(), self.right()))",
])
def test_presentation_cannot_bridge_two_real_responsibilities(
        detector, tmp_path, presentation, render):
    source = textwrap.dedent(f"""
        class Split:
            def __init__(self):
                self.left_value = 'left'
                self.right_value = 'right'

            def left(self):
                return self.left_value

            def right(self):
                return self.right_value

            def {presentation}(self):
                return {render}
    """)
    [record] = cohesion(detector, tmp_path, source)
    assert record.kind == "finding" and record.metric_value == 2
    assert "2 stateful methods" in record.description
    assert "{left} and {right}" in record.description


def test_presentation_methods_cannot_bridge_through_each_other(detector, tmp_path):
    source = textwrap.dedent("""
        class Split:
            def left(self):
                return self.left_value

            def right(self):
                return self.right_value

            def __repr__(self):
                return str(self.left())

            def __str__(self):
                return self.__repr__() + str(self.right())
    """)
    [record] = cohesion(detector, tmp_path, source)
    assert record.metric_value == 2 and "{left} and {right}" in record.description


def test_two_presentation_views_alone_are_not_separate_responsibilities(detector, tmp_path):
    source = textwrap.dedent("""
        class Entry:
            def __repr__(self):
                return self.ref

            def __str__(self):
                return self.title
    """)
    assert cohesion(detector, tmp_path, source) == []


@pytest.mark.parametrize("protocol,result", [
    ("__len__", "len(self.secondary)"),
    ("__iter__", "iter(self.secondary)"),
    ("__call__", "self.secondary"),
])
def test_other_protocols_still_form_real_behavioral_groups(detector, tmp_path, protocol, result):
    source = textwrap.dedent(f"""
        class Split:
            def count_primary(self):
                return len(self.primary)

            def {protocol}(self):
                return {result}
    """)
    [record] = cohesion(detector, tmp_path, source)
    assert record.metric_value == 2
    assert "{count_primary} and {" + protocol + "}" in record.description


@pytest.mark.parametrize("protocol,result", [
    ("__len__", "len(self.left() + self.right())"),
    ("__iter__", "iter(self.left() + self.right())"),
    ("__call__", "self.left() + self.right()"),
])
def test_other_protocols_can_connect_behavior_they_coordinate(detector, tmp_path, protocol, result):
    source = textwrap.dedent(f"""
        class Joined:
            def left(self):
                return self.left_value

            def right(self):
                return self.right_value

            def {protocol}(self):
                return {result}
    """)
    assert cohesion(detector, tmp_path, source) == []


@pytest.mark.parametrize("presentation", ["__repr__", "__str__"])
def test_representation_still_participates_in_other_coupling_checks(detector, tmp_path, presentation):
    source = textwrap.dedent(f"""
        class View:
            def label(self):
                return self.ref

            def {presentation}(self):
                return str(self.adapter.remote.state.value)
    """)
    found = detector.coupling_issues(ast.parse(source), str(tmp_path / "app.py"))
    assert any(record.issue_type == "message_chain" for record in found)


@pytest.mark.parametrize("presentation", ["__repr__", "__str__"])
def test_behavior_reaching_separate_state_through_presentation_still_counts(
        detector, tmp_path, presentation):
    source = textwrap.dedent(f"""
        class Split:
            def measure(self):
                return len(self.metrics)

            def save(self):
                return self.{presentation}()

            def {presentation}(self):
                return self.payload
    """)
    [record] = cohesion(detector, tmp_path, source)
    assert record.metric_value == 2
    assert "{measure} and {save}" in record.description


@pytest.mark.parametrize("presentation", ["__repr__", "__str__"])
def test_behavior_reaching_shared_state_through_presentation_stays_connected(
        detector, tmp_path, presentation):
    source = textwrap.dedent(f"""
        class Entry:
            def measure(self):
                return len(self.payload)

            def save(self):
                return self.{presentation}()

            def {presentation}(self):
                return self.payload
    """)
    assert cohesion(detector, tmp_path, source) == []


def test_stateless_presentation_does_not_make_its_caller_stateful(detector, tmp_path):
    source = textwrap.dedent("""
        class Entry:
            def measure(self):
                return len(self.payload)

            def label(self):
                return self.__str__()

            def __str__(self):
                return 'Entry'
    """)
    assert cohesion(detector, tmp_path, source) == []


def test_presentation_cycle_preserves_its_callers_reachable_state(detector, tmp_path):
    source = textwrap.dedent("""
        class Split:
            def measure(self):
                return len(self.metrics)

            def save(self):
                return self.__str__()

            def __str__(self):
                return self.__repr__()

            def __repr__(self):
                return self.payload + self.__str__()
    """)
    [record] = cohesion(detector, tmp_path, source)
    assert record.metric_value == 2
    assert "{measure} and {save}" in record.description


def test_behavior_coordinating_groups_through_a_representation_can_join_them(detector, tmp_path):
    source = textwrap.dedent("""
        class Entry:
            def left(self):
                return self.left_value

            def right(self):
                return self.right_value

            def save(self):
                return self.__repr__()

            def __repr__(self):
                return str((self.left(), self.right()))
    """)
    assert cohesion(detector, tmp_path, source) == []
