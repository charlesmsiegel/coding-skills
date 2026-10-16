"""A get_or_create call alone does not establish a duplicate-row race."""

import pytest

from helpers import build_project, run_detector


@pytest.mark.parametrize("constraint", [
    "        unique_together = ('user', 'scene')\n",
    "        constraints = [models.UniqueConstraint(fields=['user', 'scene'], name='read_once')]\n",
    "        pass\n",
])
def test_get_or_create_requires_database_constraint_evidence(tmp_path, constraint):
    project = build_project(tmp_path / "p", {
        "game/models.py": (
            "from django.db import models\n"
            "class UserSceneReadStatus(models.Model):\n"
            "    user = models.IntegerField()\n"
            "    scene = models.IntegerField()\n"
            "    class Meta:\n" + constraint
        ),
        "accounts/views.py": (
            "from game.models import UserSceneReadStatus\n"
            "def mark_read(user, scene):\n"
            "    return UserSceneReadStatus.objects.get_or_create(user=user, scene=scene)\n"
        ),
    })
    records = run_detector("find_transaction_issues.py", project)
    races = [r for r in records if r["smell_type"] == "get_or_create_race"]
    assert len(races) == 1
    assert races[0]["kind"] == "candidate"
    assert not races[0]["suggestion"]
    assert any("unique" in reason.lower() for reason in races[0]["also_caused_by"])


def test_unresolved_get_or_create_receiver_is_only_a_candidate(tmp_path):
    project = build_project(tmp_path / "p", {
        "app/views.py": "def create(manager, key):\n    return manager.get_or_create(key=key)\n",
    })
    record, = run_detector("find_transaction_issues.py", project)
    assert record["kind"] == "candidate"
    assert not record["suggestion"]
    assert record["also_caused_by"]
