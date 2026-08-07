"""Tests for the async, transaction, and Django-test-smell detectors."""

from helpers import build_project, run_detector, severities, smells


# ---- async ---------------------------------------------------------------------- #

def test_a_sync_orm_call_in_an_async_view_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from .models import Order\n\n"
                         "async def detail(request, pk):\n"
                         "    return Order.objects.get(pk=pk)\n",
    })
    assert severities(run_detector("find_async_issues.py", project),
                      "sync_orm_in_async_view") == ["high"]


def test_the_awaited_async_twin_is_correct(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from .models import Order\n\n"
                         "async def detail(request, pk):\n"
                         "    return await Order.objects.aget(pk=pk)\n",
    })
    assert run_detector("find_async_issues.py", project) == []


def test_an_unawaited_async_orm_call_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from .models import Order\n\n"
                         "async def detail(request, pk):\n"
                         "    Order.objects.aget(pk=pk)\n"
                         "    return None\n",
    })
    assert severities(run_detector("find_async_issues.py", project),
                      "unawaited_async_orm_call") == ["high"]


def test_a_sync_orm_call_in_a_sync_view_is_not_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from .models import Order\n\n"
                         "def detail(request, pk):\n"
                         "    return Order.objects.get(pk=pk)\n",
    })
    assert run_detector("find_async_issues.py", project) == []


def test_orm_inside_a_nested_sync_function_is_not_reported(tmp_path):
    # A `def` nested in an `async def` runs synchronously — usually because it is
    # about to be handed to sync_to_async — so the ORM call there is correct.
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from asgiref.sync import sync_to_async\n"
                         "from .models import Order\n\n"
                         "async def detail(request, pk):\n"
                         "    def fetch():\n"
                         "        return Order.objects.get(pk=pk)\n"
                         "    return await sync_to_async(fetch, thread_sensitive=True)()\n",
    })
    assert "sync_orm_in_async_view" not in smells(run_detector("find_async_issues.py", project))


def test_time_sleep_in_an_async_view_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "import time\n\n"
                         "async def wait(request):\n"
                         "    time.sleep(1)\n"
                         "    return None\n",
    })
    assert "blocking_io_in_async" in smells(run_detector("find_async_issues.py", project))


def test_awaited_asyncio_sleep_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "import asyncio\nimport django\n\n"
                         "async def wait(request):\n"
                         "    await asyncio.sleep(1)\n"
                         "    return None\n",
    })
    assert "blocking_io_in_async" not in smells(run_detector("find_async_issues.py", project))


def test_a_task_taking_a_model_instance_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "import django\n"
                         "from .tasks import send_receipt\n\n"
                         "def checkout(order):\n"
                         "    send_receipt.delay(order)\n",
    })
    assert "task_takes_model_instance" in smells(run_detector("find_async_issues.py", project))


def test_a_task_taking_a_pk_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "import django\n"
                         "from .tasks import send_receipt\n\n"
                         "def checkout(order_id):\n"
                         "    send_receipt.delay(order_id)\n",
    })
    assert "task_takes_model_instance" not in smells(run_detector("find_async_issues.py", project))


def test_enqueueing_inside_atomic_without_on_commit_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from django.db import transaction\n"
                         "from .tasks import send_receipt\n\n"
                         "def checkout(order_id):\n"
                         "    with transaction.atomic():\n"
                         "        send_receipt.delay(order_id)\n",
    })
    assert severities(run_detector("find_async_issues.py", project),
                      "enqueue_without_on_commit") == ["high"]


def test_on_commit_is_the_correct_form(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from django.db import transaction\n"
                         "from .tasks import send_receipt\n\n"
                         "def checkout(order_id):\n"
                         "    with transaction.atomic():\n"
                         "        transaction.on_commit(lambda: send_receipt.delay(order_id))\n",
    })
    assert "enqueue_without_on_commit" not in smells(run_detector("find_async_issues.py", project))


# ---- transactions ----------------------------------------------------------------- #

def test_an_http_call_inside_atomic_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "import requests\n"
                         "from django.db import transaction\n\n"
                         "def pay(order):\n"
                         "    with transaction.atomic():\n"
                         "        requests.post('https://gateway.example/charge')\n"
                         "        order.save()\n",
    })
    assert severities(run_detector("find_transaction_issues.py", project),
                      "external_call_in_atomic") == ["high"]


def test_an_http_call_outside_atomic_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "import requests\n"
                         "from django.db import transaction\n\n"
                         "def pay(order):\n"
                         "    charge = requests.post('https://gateway.example/charge')\n"
                         "    with transaction.atomic():\n"
                         "        order.save()\n"
                         "    return charge\n",
    })
    assert "external_call_in_atomic" not in smells(
        run_detector("find_transaction_issues.py", project))


def test_select_for_update_outside_a_transaction_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "import django\nfrom .models import Order\n\n"
                         "def lock(pk):\n"
                         "    return Order.objects.select_for_update().get(pk=pk)\n",
    })
    assert severities(run_detector("find_transaction_issues.py", project),
                      "select_for_update_outside_atomic") == ["high"]


def test_select_for_update_inside_atomic_is_correct(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from django.db import transaction\n"
                         "from .models import Order\n\n"
                         "def lock(pk):\n"
                         "    with transaction.atomic():\n"
                         "        return Order.objects.select_for_update().get(pk=pk)\n",
    })
    assert "select_for_update_outside_atomic" not in smells(
        run_detector("find_transaction_issues.py", project))


def test_integrity_error_caught_inside_the_same_atomic_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from django.db import IntegrityError, transaction\n"
                         "from .models import Order\n\n"
                         "def make(pk):\n"
                         "    with transaction.atomic():\n"
                         "        try:\n"
                         "            Order.objects.create(pk=pk)\n"
                         "        except IntegrityError:\n"
                         "            return None\n",
    })
    assert severities(run_detector("find_transaction_issues.py", project),
                      "integrity_error_caught_in_atomic") == ["high"]


def test_a_savepoint_makes_the_catch_correct(tmp_path):
    # An inner atomic() is a savepoint, which is exactly how you catch
    # IntegrityError inside a transaction.
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from django.db import IntegrityError, transaction\n"
                         "from .models import Order\n\n"
                         "def make(pk):\n"
                         "    with transaction.atomic():\n"
                         "        try:\n"
                         "            with transaction.atomic():\n"
                         "                Order.objects.create(pk=pk)\n"
                         "        except IntegrityError:\n"
                         "            return None\n",
    })
    assert "integrity_error_caught_in_atomic" not in smells(
        run_detector("find_transaction_issues.py", project))


def test_a_loop_of_saves_inside_atomic_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/views.py": "from django.db import transaction\n\n"
                         "def bulk(rows):\n"
                         "    with transaction.atomic():\n"
                         "        for row in rows:\n"
                         "            row.save()\n",
    })
    assert "atomic_around_loop_of_saves" in smells(
        run_detector("find_transaction_issues.py", project))


# ---- Django test smells ------------------------------------------------------------ #

GOOD_TEST = """\
from django.test import TestCase

from .models import Order


class OrderTests(TestCase):
    def test_list_is_two_queries(self):
        with self.assertNumQueries(2):
            self.client.get("/orders/")
"""


def test_a_suite_with_no_query_count_assertions_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/tests.py": "from django.test import TestCase\n\n\n"
                         "class OrderTests(TestCase):\n"
                         "    def test_ok(self):\n"
                         "        self.assertTrue(True)\n",
    })
    assert "no_query_count_assertions" in smells(run_detector("find_test_issues.py", project))


def test_one_assert_num_queries_anywhere_satisfies_the_check(tmp_path):
    # Whole-suite on purpose: "this one test lacks it" is noise, "nothing in the
    # project ever asserts a query count" is a fact worth one finding.
    project = build_project(tmp_path / "p", {"shop/tests.py": GOOD_TEST})
    assert "no_query_count_assertions" not in smells(run_detector("find_test_issues.py", project))


def test_on_commit_under_testcase_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/tests.py": "from django.test import TestCase\n"
                         "from django.db import transaction\n\n\n"
                         "class T(TestCase):\n"
                         "    def test_receipt(self):\n"
                         "        with self.assertNumQueries(1):\n"
                         "            transaction.on_commit(lambda: None)\n",
    })
    assert severities(run_detector("find_test_issues.py", project),
                      "on_commit_needs_transaction_testcase") == ["high"]


def test_on_commit_under_transaction_testcase_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/tests.py": "from django.test import TransactionTestCase\n"
                         "from django.db import transaction\n\n\n"
                         "class T(TransactionTestCase):\n"
                         "    def test_receipt(self):\n"
                         "        with self.assertNumQueries(1):\n"
                         "            transaction.on_commit(lambda: None)\n",
    })
    assert "on_commit_needs_transaction_testcase" not in smells(
        run_detector("find_test_issues.py", project))


def test_mutating_a_setuptestdata_object_without_saving_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/tests.py": "from django.test import TestCase\n"
                         "from .models import Order\n\n\n"
                         "class T(TestCase):\n"
                         "    @classmethod\n"
                         "    def setUpTestData(cls):\n"
                         "        cls.order = Order.objects.create(total=10)\n\n"
                         "    def test_total(self):\n"
                         "        self.order.total = 0\n"
                         "        with self.assertNumQueries(1):\n"
                         "            self.client.get('/orders/')\n",
    })
    assert severities(run_detector("find_test_issues.py", project),
                      "setuptestdata_mutation") == ["high"]


def test_mutating_then_saving_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/tests.py": "from django.test import TestCase\n"
                         "from .models import Order\n\n\n"
                         "class T(TestCase):\n"
                         "    @classmethod\n"
                         "    def setUpTestData(cls):\n"
                         "        cls.order = Order.objects.create(total=10)\n\n"
                         "    def test_total(self):\n"
                         "        self.order.total = 0\n"
                         "        self.order.save()\n"
                         "        with self.assertNumQueries(1):\n"
                         "            self.client.get('/orders/')\n",
    })
    assert "setuptestdata_mutation" not in smells(run_detector("find_test_issues.py", project))


def test_client_login_is_reported_in_favour_of_force_login(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/tests.py": "from django.test import TestCase\n\n\n"
                         "class T(TestCase):\n"
                         "    def test_x(self):\n"
                         "        self.client.login(username='a', password='b')\n"
                         "        with self.assertNumQueries(1):\n"
                         "            self.client.get('/')\n",
    })
    assert "client_login_over_force_login" in smells(run_detector("find_test_issues.py", project))


def test_a_project_with_no_django_tests_produces_nothing(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": "from django.db import models\n\n\n"
                          "class Order(models.Model):\n    pass\n",
    })
    assert run_detector("find_test_issues.py", project) == []
