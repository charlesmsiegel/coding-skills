"""Tests for the DRF detector.

The gate matters as much as the checks: a project with no DRF must produce
nothing, because firing DRF rules at a plain Django project is the same failure
as firing Django rules at Flask, one level down.
"""

from helpers import build_project, run_detector, severities, smells

MODELS = """\
from django.db import models


class Order(models.Model):
    reference = models.CharField(max_length=20)

    class Meta:
        ordering = ["pk"]

    def __str__(self):
        return self.reference
"""

# A settings module that closes the project-wide defaults, so the per-view
# findings can be tested in isolation from the project-default ones.
STRICT_SETTINGS = """\
import django

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.UserRateThrottle"],
    "PAGE_SIZE": 25,
}
"""


# ---- the gate ------------------------------------------------------------------ #

def test_a_django_project_without_drf_produces_nothing(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/views.py": "from django.views.generic import ListView\n\n\n"
                         "class OrderList(ListView):\n"
                         "    queryset = None\n",
    })
    assert run_detector("find_drf_issues.py", project) == []


# ---- serializers ---------------------------------------------------------------- #

def test_serializer_fields_all_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/serializers.py": "from rest_framework import serializers\n"
                               "from .models import Order\n\n\n"
                               "class OrderSerializer(serializers.ModelSerializer):\n"
                               "    class Meta:\n"
                               "        model = Order\n"
                               "        fields = '__all__'\n",
    })
    assert severities(run_detector("find_drf_issues.py", project),
                      "serializer_fields_all") == ["high"]


def test_an_explicit_serializer_field_list_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "config/settings.py": STRICT_SETTINGS,
        "shop/serializers.py": "from rest_framework import serializers\n"
                               "from .models import Order\n\n\n"
                               "class OrderSerializer(serializers.ModelSerializer):\n"
                               "    class Meta:\n"
                               "        model = Order\n"
                               "        fields = ['id', 'reference']\n",
    })
    assert run_detector("find_drf_issues.py", project) == []


def test_serializer_depth_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "config/settings.py": STRICT_SETTINGS,
        "shop/serializers.py": "from rest_framework import serializers\n"
                               "from .models import Order\n\n\n"
                               "class OrderSerializer(serializers.ModelSerializer):\n"
                               "    class Meta:\n"
                               "        model = Order\n"
                               "        fields = ['id']\n"
                               "        depth = 1\n",
    })
    assert "serializer_depth" in smells(run_detector("find_drf_issues.py", project))


def test_a_querying_method_field_is_an_n_plus_one(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "config/settings.py": STRICT_SETTINGS,
        "shop/serializers.py": "from rest_framework import serializers\n"
                               "from .models import Order\n\n\n"
                               "class OrderSerializer(serializers.ModelSerializer):\n"
                               "    item_count = serializers.SerializerMethodField()\n\n"
                               "    class Meta:\n"
                               "        model = Order\n"
                               "        fields = ['id', 'item_count']\n\n"
                               "    def get_item_count(self, obj):\n"
                               "        return obj.items.count()\n",
    })
    assert severities(run_detector("find_drf_issues.py", project),
                      "query_in_serializer_method_field") == ["high"]


def test_a_method_field_reading_an_annotation_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "config/settings.py": STRICT_SETTINGS,
        "shop/serializers.py": "from rest_framework import serializers\n"
                               "from .models import Order\n\n\n"
                               "class OrderSerializer(serializers.ModelSerializer):\n"
                               "    item_count = serializers.SerializerMethodField()\n\n"
                               "    class Meta:\n"
                               "        model = Order\n"
                               "        fields = ['id', 'item_count']\n\n"
                               "    def get_item_count(self, obj):\n"
                               "        return obj.annotated_item_count\n",
    })
    assert run_detector("find_drf_issues.py", project) == []


# ---- viewsets --------------------------------------------------------------------- #

def test_a_viewset_with_no_permissions_and_no_project_default_is_public(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/api.py": "from rest_framework.viewsets import ModelViewSet\n"
                       "from .models import Order\n\n\n"
                       "class OrderViewSet(ModelViewSet):\n"
                       "    def get_queryset(self):\n"
                       "        return Order.objects.filter(owner=self.request.user)\n",
    })
    assert severities(run_detector("find_drf_issues.py", project),
                      "viewset_default_permission") == ["high"]


def test_a_project_default_permission_closes_it(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "config/settings.py": STRICT_SETTINGS,
        "shop/api.py": "from rest_framework.viewsets import ModelViewSet\n"
                       "from .models import Order\n\n\n"
                       "class OrderViewSet(ModelViewSet):\n"
                       "    def get_queryset(self):\n"
                       "        return Order.objects.filter(owner=self.request.user)\n",
    })
    assert "viewset_default_permission" not in smells(run_detector("find_drf_issues.py", project))


def test_allow_any_is_reported_explicitly(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "config/settings.py": STRICT_SETTINGS,
        "shop/api.py": "from rest_framework.viewsets import ModelViewSet\n"
                       "from rest_framework.permissions import AllowAny\n"
                       "from .models import Order\n\n\n"
                       "class OrderViewSet(ModelViewSet):\n"
                       "    permission_classes = [AllowAny]\n\n"
                       "    def get_queryset(self):\n"
                       "        return Order.objects.filter(owner=self.request.user)\n",
    })
    assert "permission_allow_any" in smells(run_detector("find_drf_issues.py", project))


def test_a_class_queryset_with_no_scoping_is_high(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "config/settings.py": STRICT_SETTINGS,
        "shop/api.py": "from rest_framework.viewsets import ModelViewSet\n"
                       "from .models import Order\n\n\n"
                       "class OrderViewSet(ModelViewSet):\n"
                       "    queryset = Order.objects.all()\n",
    })
    assert severities(run_detector("find_drf_issues.py", project),
                      "unscoped_viewset_queryset") == ["high"]


def test_a_scoped_get_queryset_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "config/settings.py": STRICT_SETTINGS,
        "shop/api.py": "from rest_framework.viewsets import ModelViewSet\n"
                       "from .models import Order\n\n\n"
                       "class OrderViewSet(ModelViewSet):\n"
                       "    def get_queryset(self):\n"
                       "        return Order.objects.filter(owner=self.request.user)\n",
    })
    assert run_detector("find_drf_issues.py", project) == []


def test_a_list_view_without_pagination_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "config/settings.py": "import django\n\nREST_FRAMEWORK = {\n"
                              "    'DEFAULT_PERMISSION_CLASSES': ['x.IsAuthenticated'],\n"
                              "    'DEFAULT_THROTTLE_CLASSES': ['x.UserRateThrottle'],\n}\n",
        "shop/api.py": "from rest_framework.generics import ListAPIView\n"
                       "from .models import Order\n\n\n"
                       "class OrderList(ListAPIView):\n"
                       "    def get_queryset(self):\n"
                       "        return Order.objects.filter(owner=self.request.user)\n",
    })
    assert "missing_pagination" in smells(run_detector("find_drf_issues.py", project))


def test_an_api_view_function_without_permissions_is_reported(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "shop/api.py": "from rest_framework.decorators import api_view\n\n\n"
                       "@api_view(['POST'])\n"
                       "def charge(request):\n"
                       "    return None\n",
    })
    assert "viewset_default_permission" in smells(run_detector("find_drf_issues.py", project))


def test_an_api_view_with_permission_classes_is_fine(tmp_path):
    project = build_project(tmp_path / "p", {
        "shop/models.py": MODELS,
        "config/settings.py": STRICT_SETTINGS,
        "shop/api.py": "from rest_framework.decorators import api_view, permission_classes\n"
                       "from rest_framework.permissions import IsAuthenticated\n\n\n"
                       "@api_view(['POST'])\n"
                       "@permission_classes([IsAuthenticated])\n"
                       "def charge(request):\n"
                       "    return None\n",
    })
    assert run_detector("find_drf_issues.py", project) == []
