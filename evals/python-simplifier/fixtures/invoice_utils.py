"""Billing helpers. (Eval fixture: intentionally poor code — do not fix in place.)"""
import csv
import io


class ConfigLoader:
    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = ConfigLoader()
        return cls._instance

    def __init__(self):
        self.currency = "USD"
        self.tax_rate = 0.0825
        self.rounding = 2


def compute_total(lines, discounts=[]):
    try:
        cfg = ConfigLoader.get_instance()
        total = 0
        for line in lines:
            total = total + line["qty"] * line["unit_price"]
        for d in discounts:
            total = total - d
        return round(total * (1 + cfg.tax_rate), cfg.rounding)
    except:
        return 0


def export_invoices_csv(invoices):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "customer", "total"])
    for inv in invoices:
        w.writerow([inv["id"], inv["customer"], inv["total"]])
    return buf.getvalue()


def export_customers_csv(customers):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "name", "email"])
    for c in customers:
        w.writerow([c["id"], c["name"], c["email"]])
    return buf.getvalue()


def export_payments_csv(payments):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "invoice_id", "amount"])
    for p in payments:
        w.writerow([p["id"], p["invoice_id"], p["amount"]])
    return buf.getvalue()
