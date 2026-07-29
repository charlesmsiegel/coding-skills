"""Fixture for the django-simplifier evals. Deliberately bad."""
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.safestring import mark_safe

from .models import Order


class OrderService:
    def get(self, pk):
        return Order.objects.get(pk=pk)

    def delete(self, pk):
        Order.objects.filter(pk=pk).delete()


def order_detail(request, pk):
    order = get_object_or_404(Order, pk=pk)
    note = mark_safe("<b>" + order.note + "</b>")
    return render(request, "orders/detail.html", {"order": order, "note": note})


def order_dashboard(request):
    rows = []
    for order in Order.objects.all():
        rows.append({"customer": order.customer.name, "region": order.customer.region.name})
        order.viewed_count = order.viewed_count + 1
        order.save()
    return redirect("/orders/")
