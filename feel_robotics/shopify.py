# -*- coding: utf-8 -*-
import base64
import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.http import HttpResponse, HttpResponseForbidden
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from six import wraps

from dashboard.models.currency import Currency
from dashboard.models.customer_details import CustomerDetails
from dashboard.models.order import Order
from dashboard.models.order_row import OrderRow
from dashboard.models.order_tag import OrderTag, OrderTagLink
from dashboard.models.package import Package
from dashboard.models.shop import Shop
from dashboard.warehouse_utils import get_recommended_warehouse

logger = logging.getLogger("shopify")


def _hmac_is_valid(body, secret, hmac_to_verify):
    if not secret:
        # Skip validation if secret is absent (useful in development/testing)
        return True

    hash = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    hmac_calculated = base64.b64encode(hash.digest())
    return hmac.compare_digest(hmac_calculated, hmac_to_verify.encode("utf-8"))


def _signature_is_valid(webhook_hmac, webhook_domain, body):
    try:
        shop = Shop.objects.get(shopify_domain=webhook_domain)
    except ObjectDoesNotExist:
        return False
    return shop and _hmac_is_valid(body, shop.shopify_shared_secret, webhook_hmac)


def shopify_webhook(f):
    """Decorator for validating Shopify webhook requests."""

    @wraps(f)
    def wrapper(request, *args, **kwargs):
        if getattr(settings, "TEST", False):
            return f(request, *args, **kwargs)  # Skip signature validation in tests

        try:
            webhook_topic = request.META["HTTP_X_SHOPIFY_TOPIC"]
            webhook_hmac = request.META["HTTP_X_SHOPIFY_HMAC_SHA256"]
            webhook_domain = request.META["HTTP_X_SHOPIFY_SHOP_DOMAIN"]
            webhook_data = json.loads(request.body.decode("utf-8"))
        except Exception as e:
            logger.error(f"Malformed request: {e}")
            return HttpResponse(json.dumps({"error": "Malformed request"}), status=400)

        if not _signature_is_valid(webhook_hmac, webhook_domain, request.body):
            return HttpResponseForbidden("Invalid webhook signature")

        request.webhook_topic = webhook_topic
        request.webhook_data = webhook_data
        return f(request, *args, **kwargs)

    return wrapper


def get_unknown_package():
    """Fetch or create an 'Unknown' package for unidentified SKUs."""
    package, created = Package.objects.get_or_create(
        name="Unknown",
        defaults={"price": 0, "identifier": "unknown-package"}
    )
    return package


def extract_order_fields(data):
    """Extract order-related fields from webhook data."""
    return {
        "shopify_order_data": data,
        "shop_order_id": data["id"],
        "date": data["created_at"],
        "total": float(data["total_price"]),
        "subtotal_price": float(data["subtotal_price"]),
        "total_tax": float(data["total_tax"]),
        "total_discounts": float(data["total_discounts"]),
        "total_shipping": float(data["total_shipping_price_set"]["shop_money"]["amount"]),
        "total_shipping_tax": sum(
            sum(float(tax_line.get("price")) for tax_line in shipping_line.get("tax_lines", []))
            for shipping_line in data.get("shipping_lines", [])
        ),
        "currency": get_currency(data),
        "test_order": data["test"],
        "payment_method": ",".join(data.get("payment_gateway_names", [])),
        "shopify_order_id": data["order_number"],
    }


def extract_customer_details(data):
    """Extract customer details, handling both billing and shipping information."""
    customer = data.get("customer", {})
    billing = data.get("billing_address", {})
    shipping = data.get("shipping_address", {})

    phone = billing.get("phone") or shipping.get("phone") or data.get("phone")

    return {
        "email": customer.get("email", data["email"]),
        "phone": phone,
        "billing_country": billing.get("country_code"),
        "billing_state": billing.get("province_code"),
        "billing_first_name": billing.get("first_name", ""),
        "billing_last_name": billing.get("last_name", ""),
        "billing_company": billing.get("company"),
        "billing_address_1": billing.get("address1"),
        "billing_address_2": billing.get("address2"),
        "billing_postcode": billing.get("zip"),
        "billing_city": billing.get("city"),
        "shipping_first_name": shipping.get("first_name", ""),
        "shipping_last_name": shipping.get("last_name", ""),
        "shipping_company": shipping.get("company"),
        "shipping_address_1": shipping.get("address1"),
        "shipping_address_2": shipping.get("address2"),
        "shipping_postcode": shipping.get("zip") if shipping.get("country_code") != "HK" else "HKSAR",
        "shipping_city": shipping.get("city"),
        "shipping_state": shipping.get("province_code"),
        "shipping_country": shipping.get("country_code"),
    }


def create_order_products(order, data):
    """Create or update OrderRow objects for an order."""
    logger.info("Deleting current OrderRows for the order.")
    OrderRow.objects.filter(order=order).delete()

    for line_item in data.get("line_items", []):
        sku = line_item.get("sku")
        if not sku:
            logger.error("Unknown package SKU encountered.")
            return HttpResponse(json.dumps({"error": "Unknown package"}), status=200)

        package_object, _ = Package.objects.get_or_create(
            identifier=sku,
            defaults={"name": sku, "price": float(sku.split("-")[-1]), "is_physical": False}
        )

        tax = sum(float(line["price"]) for line in line_item.get("tax_lines", []))
        order_row = OrderRow(
            order=order,
            package=package_object,
            cnt=line_item.get("quantity", 1),
            total=float(line_item.get("pre_tax_price", 0)) + tax,
            subtotal=float(line_item.get("price", 0)),
            subtotal_tax=0,
            tax=tax,
            name=line_item.get("name", ""),
            product_id=line_item.get("product_id", 0),
        )
        order_row.save()
        logger.info(f"Saved new row: {order_row.id}")


def save_tags(data, order):
    """Save or update tags for an order."""
    tags = data.get("tags", "")
    OrderTagLink.objects.filter(order=order).delete()

    for tag_name in tags.split(","):
        tag_name = tag_name.strip()
        tag, _ = OrderTag.objects.get_or_create(name=tag_name)
        OrderTagLink.objects.create(order=order, tag=tag)


def get_currency(data):
    """Retrieve or create a Currency object based on order data."""
    currency, _ = Currency.objects.get_or_create(name=data["currency"].strip())
    return currency


@transaction.atomic
def upsert_order(data, shop, state):
    """Create or update an Order object based on webhook data."""
    logger.info(f"Upserting Shopify order: {shop} - {data['id']}")
    order_fields = extract_order_fields(data)

    orders = Order.objects.select_for_update().filter(
        shop_order_id=order_fields["shop_order_id"], shop=shop, test_order=False
    )
    if len(orders) > 1:
        logger.warning(f"Duplicate orders found for ID {order_fields['shop_order_id']}")
        order = orders.first()
        Order.objects.filter(id__in=[o.id for o in orders[1:]]).delete()
    elif orders:
        order = orders[0]
    else:
        order = Order(shop=shop, **order_fields)
        order.state = state or Order.STATE_PROCESSING
        order.save()

    for key, value in order_fields.items():
        setattr(order, key, value)

    if not order.customer_details:
        customer_data = extract_customer_details(data)
        customer = CustomerDetails.objects.create(**customer_data)
        order.customer_details = customer

    if state and order.state != state:
        order.state = state

    order.comments = data.get("note")
    order.manual_invoice_no = data.get("name")
    order.save()

    err_response = create_order_products(order, data)
    if err_response:
        return None, err_response

    save_tags(data, order)

    if not order.warehouse:
        order.warehouse = get_recommended_warehouse(order)
        order.save()
        logger.info(f"Order warehouse set: {order.warehouse}")

    if not order.payment_date and order_fields.get("total"):
        order.payment_date = timezone.now()
        order.save()
        logger.info(f"Order marked as paid: {order.payment_date}")

    return order, None


@csrf_exempt
@shopify_webhook
def order_webhook(request):
    data = request.webhook_data
    try:
        shop = Shop.objects.get(shopify_domain=request.META["HTTP_X_SHOPIFY_SHOP_DOMAIN"])
    except ObjectDoesNotExist:
        logger.error(f"Shop not found for domain: {request.META['HTTP_X_SHOPIFY_SHOP_DOMAIN']}")
        return HttpResponseForbidden("Shop not found")

    state = (
        Order.STATE_CANCELLED
        if data.get("cancelled_at")
        else Order.STATE_PROCESSING
        if data.get("closed_at")
        else None
    )

    order, err_response = upsert_order(data, shop, state)
    if err_response:
        return err_response

    logger.info(f"Processed order webhook successfully: {order.id}")
    return HttpResponse(status=200)
