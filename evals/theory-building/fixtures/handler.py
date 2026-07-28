"""Payment webhook handler. (Eval fixture.)"""
import json


def handle_webhook(request, db, notifier):
    event = json.loads(request.body)
    payment_id = event["data"]["payment_id"]
    status = event["data"]["status"]

    payment = db.payments.get(payment_id)
    if payment is None:
        return {"status": 404}

    payment.status = status
    db.payments.save(payment)
    if status == "succeeded":
        notifier.send_receipt(payment)
    elif status == "failed":
        notifier.send_failure_notice(payment)
    return {"status": 200}
