"""Relation writes do not become cheaper by prefetching related rows."""

import pytest

from helpers import build_project, run_detector

MODELS = '''\
from django.db import models

class Person(models.Model):
    pass

class Group(models.Model):
    members = models.ManyToManyField(Person, related_name="groups")
    owner = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="owned_groups")
    leader = models.OneToOneField(Person, on_delete=models.CASCADE, related_name="led_group")
'''


def query_hits(tmp_path, body, model="Group", models=MODELS):
    project = build_project(tmp_path / "p", {
        "shop/models.py": models,
        "shop/views.py": f"from .models import {model}\n\ndef go(other):\n"
                         f"    for obj in {model}.objects.filter(pk__gt=0):\n"
                         + "\n".join(f"        {line}" for line in body.splitlines()) + "\n",
    })
    return [f for f in run_detector("find_query_issues.py", project)
            if f["smell_type"] == "n_plus_one_query"]


@pytest.mark.parametrize("operation", ["add(other)", "remove(other)", "set([other])", "clear()"])
@pytest.mark.parametrize("relation,model", [
    ("members", "Group"), ("groups", "Person"), ("owned_groups", "Person"),
])
def test_multi_relation_mutations_do_not_suggest_prefetch(tmp_path, operation, relation, model):
    assert not query_hits(tmp_path, f"obj.{relation}.{operation}", model)


@pytest.mark.parametrize("expression", [
    "print(obj.members.all())", "print(obj.members.filter(pk=other.pk))",
    "print(obj.members.count())", "print(obj.members)",
    "obj.members.remove(obj.owner)", "obj.members.set(obj.members.all())",
    "obj.owner.remove(other)", "obj.led_group.clear()",
    "obj.members.remove(other)\nprint(obj.members.all())",
])
def test_reads_remain_visible_alongside_mutations(tmp_path, expression):
    assert query_hits(tmp_path, expression)


def test_ambiguous_relation_names_are_not_excluded(tmp_path):
    models = MODELS + '''\nclass Other(models.Model):
    members = models.ForeignKey(Person, on_delete=models.CASCADE)
'''
    assert query_hits(tmp_path, "obj.members.remove(other)", models=models)


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_model_class_cannot_hide_a_single_relation(tmp_path, reverse):
    files = {
        "a/models.py": "from django.db import models\n"
                       "class Item(models.Model):\n"
                       "    members = models.ManyToManyField('Person')\n",
        "b/models.py": "from django.db import models\n"
                       "class Item(models.Model):\n"
                       "    members = models.ForeignKey('Person', on_delete=models.CASCADE)\n",
        "b/views.py": "from .models import Item\n"
                      "for obj in Item.objects.all():\n"
                      "    obj.members.remove(other)\n",
    }
    if reverse:
        files["a/models.py"], files["b/models.py"] = files["b/models.py"], files["a/models.py"]
    project = build_project(tmp_path / "p", files)
    assert any(f["smell_type"] == "n_plus_one_query"
               for f in run_detector("find_query_issues.py", project))
