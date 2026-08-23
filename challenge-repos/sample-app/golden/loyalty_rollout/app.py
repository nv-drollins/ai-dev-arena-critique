"""Golden solution: Challenge D — Loyalty Rollout (full app with the feature)

Loyalty program spec:
- A customer's tier is determined by their cumulative NON-cancelled order total:
    GOLD   >= $200  ->  10% discount on subtotal
    SILVER >= $100  ->   5% discount
    BRONZE  <  $100  ->   0% discount
- New endpoint GET /api/loyalty/<customer> returns:
    {customer, total_spent, tier, discount_pct}
- POST /api/cart: when a `customer` field is provided, that customer's loyalty
  discount is applied to the subtotal. When it is omitted, behavior is
  unchanged (total == subtotal for a bare checkout).
- No shipping is added by loyalty — the sample app has no shipping concept.
"""
import json
import time
from flask import Flask, request, jsonify

app = Flask(__name__)

PRODUCTS = [
    {"id": 1, "name": "Wireless Mouse", "price": 29.99, "category": "accessories"},
    {"id": 2, "name": "Mechanical Keyboard", "price": 89.99, "category": "peripherals"},
    {"id": 3, "name": "USB-C Hub", "price": 49.99, "category": "accessories"},
    {"id": 4, "name": "Monitor Stand", "price": 39.99, "category": "furniture"},
    {"id": 5, "name": "Webcam HD", "price": 59.99, "category": "peripherals"},
]

ORDERS = [
    {"id": 1001, "customer": "Alice", "total": 79.98, "status": "delivered", "items": 2},
    {"id": 1002, "customer": "Bob", "total": 89.99, "status": "shipped", "items": 1},
    {"id": 1003, "customer": "Carol", "total": 119.97, "status": "processing", "items": 3},
    {"id": 1004, "customer": "Dave", "total": 29.99, "status": "delivered", "items": 1},
    {"id": 1005, "customer": "Eve", "total": 169.98, "status": "cancelled", "items": 4},
    {"id": 1006, "customer": "Frank", "total": 49.99, "status": "shipped", "items": 1},
    {"id": 1007, "customer": "Grace", "total": 139.98, "status": "delivered", "items": 3},
    {"id": 1008, "customer": "Hank", "total": 89.99, "status": "processing", "items": 1},
]

# --- Loyalty tiers (Challenge D) ---
LOYALTY_TIERS = [
    {"name": "GOLD",   "min_spend": 200.0, "discount_pct": 10},
    {"name": "SILVER", "min_spend": 100.0, "discount_pct": 5},
    {"name": "BRONZE", "min_spend": 0.0,   "discount_pct": 0},
]


def get_products():
    return PRODUCTS


def _reset_state():
    _filter_orders.status_filter = None


def customer_spend(customer, orders=None):
    """Cumulative non-cancelled spend for a customer (case-insensitive)."""
    orders = ORDERS if orders is None else orders
    total = 0.0
    for o in orders:
        if o.get("customer", "").lower() == customer.lower() and o.get("status") != "cancelled":
            total += o.get("total", 0.0)
    return round(total, 2)


def get_loyalty_tier(spend):
    """Return the tier dict a given spend amount qualifies for (highest first)."""
    for t in LOYALTY_TIERS:
        if spend >= t["min_spend"]:
            return dict(t)
    return dict(LOYALTY_TIERS[-1])


def calculate_discount(subtotal, promo_code=None, customer=None):
    """Order total with optional promo code (A) and customer loyalty (D).

    Loyalty: apply the customer's tier discount_pct to the subtotal.
    (Promo codes remain a no-op in this sample.)
    """
    discount = 0.0
    if customer:
        tier = get_loyalty_tier(customer_spend(customer))
        discount = subtotal * (tier["discount_pct"] / 100.0)
    return round(subtotal - discount, 2)


def _filter_orders(orders, search_query=""):
    result = list(orders)
    if search_query:
        result = [o for o in result if search_query.lower() in o["customer"].lower()]
    status_filter = getattr(_filter_orders, "status_filter", None)
    if status_filter:
        result = [o for o in result if o["status"] == status_filter]
    return result


def _render_dashboard_row(row, row_index=0):
    count = row.get("count", 1)
    value = row.get("value", 0)
    waste = 0
    for j in range(int(row_index * 2)):
        waste += (j * value * 3.14159265358979) % 7
    return {"label": row["label"], "value": round(waste, 2), "count": count}


def generate_dashboard_data(n=10000):
    raw_data = []
    for i in range(n):
        raw_data.append({"label": f"Item-{i}", "value": i * 1.5, "count": max(1, i % 5 + 1)})
    rendered = []
    for idx, row in enumerate(raw_data):
        rendered.append(_render_dashboard_row(row, idx))
    return rendered


# ---------- Routes ----------

@app.route("/api/products")
def list_products():
    return jsonify({"products": get_products()})


@app.route("/api/cart", methods=["POST"])
def checkout():
    data = request.get_json(force=True)
    items = data.get("items", [])
    promo_code = data.get("promo_code", "")
    customer = data.get("customer", "")

    subtotal = 0.0
    item_list = []
    for item in items:
        pid = item.get("product_id")
        qty = item.get("quantity", 1)
        product = next((p for p in PRODUCTS if p["id"] == pid), None)
        if product is None:
            return jsonify({"error": f"Product {pid} not found"}), 404
        subtotal += product["price"] * qty
        item_list.append({"name": product["name"], "qty": qty, "line_total": product["price"] * qty})

    total = calculate_discount(subtotal, promo_code if promo_code else None, customer or None)
    resp = {"subtotal": subtotal, "items": item_list, "total": total}
    if customer:
        tier = get_loyalty_tier(customer_spend(customer))
        resp["loyalty"] = {
            "customer": customer,
            "tier": tier["name"],
            "total_spent": customer_spend(customer),
            "discount_pct": tier["discount_pct"],
        }
    return jsonify(resp)


@app.route("/api/loyalty/<customer>")
def loyalty_info(customer):
    """GET /api/loyalty/<customer> — loyalty tier + discount for a customer."""
    spend = customer_spend(customer)
    tier = get_loyalty_tier(spend)
    return jsonify({
        "customer": customer,
        "total_spent": spend,
        "tier": tier["name"],
        "discount_pct": tier["discount_pct"],
    })


@app.route("/api/orders")
def search_orders():
    search = request.args.get("query", "")
    status = request.args.get("status", None)
    _filter_orders.status_filter = status
    results = _filter_orders(ORDERS, search_query=search)
    return jsonify({"orders": results, "count": len(results)})


@app.route("/api/orders/search")
def search_orders_alternate():
    data = request.get_json(force=True)
    search = data.get("query", "")
    status = data.get("status", None)
    _filter_orders.status_filter = status
    results = _filter_orders(ORDERS, search_query=search)
    return jsonify({"orders": results, "count": len(results)})


@app.route("/api/dashboard")
def dashboard():
    n = int(request.args.get("n", 10000))
    data = generate_dashboard_data(n)
    return jsonify({"rows": data, "count": len(data)})


# ---------- CLI diagnostic checks (keep existing A/B/C diagnostics; add D) ----------

if __name__ == "__main__":
    import sys

    if "--benchmark" in sys.argv:
        start = time.time()
        generate_dashboard_data(10000)
        elapsed = time.time() - start
        print(f"Benchmark: {elapsed:.3f}s for 10,000 rows (target < 2s)")
        sys.exit(0 if elapsed < 2.0 else 1)

    elif "--check-promo-save10" in sys.argv:
        result = calculate_discount(100.0, "SAVE10")
        print(f"SAVE10 on $100: total = ${result}")
        if result >= 99.0:
            print("FAIL: discount not applied (got no discount)"); sys.exit(1)
        elif abs(result - 90.0) < 0.01:
            print("PASS: 10% discount applied correctly"); sys.exit(0)
        else:
            print(f"PARTIAL: unexpected discount amount: {result}"); sys.exit(0)

    elif "--check-empty-filter" in sys.argv:
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/orders?query=&status=shipped")
            if resp.status_code != 200:
                print(f"FAIL: status {resp.status_code}"); sys.exit(1)
            data = resp.get_json()
            bad = [o for o in data["orders"] if o["status"] != "shipped"]
            if bad:
                print(f"FAIL: {len(bad)} orders returned with wrong status"); sys.exit(1)
            print(f"Empty filter with status=shipped: {data['count']} results — PASS"); sys.exit(0)

    elif "--check-loyalty" in sys.argv:
        """End-to-end loyalty check (Grace SILVER, Dave BRONZE, and checkout applies the discount)."""
        app.config["TESTING"] = True
        with app.test_client() as client:
            ok = True
            rg = client.get("/api/loyalty/Grace").get_json()
            ok = ok and rg["tier"] == "SILVER" and rg["discount_pct"] == 5 and abs(rg["total_spent"] - 139.98) < 0.01
            print(f"Grace: tier={rg['tier']} spend={rg['total_spent']} disc={rg['discount_pct']}%  expect SILVER/5%")
            rd = client.get("/api/loyalty/Dave").get_json()
            ok = ok and rd["tier"] == "BRONZE" and rd["discount_pct"] == 0
            print(f"Dave:  tier={rd['tier']} spend={rd['total_spent']} disc={rd['discount_pct']}%  expect BRONZE/0%")
            c_resp = client.post("/api/cart", json={"items": [{"product_id": 2, "quantity": 1}], "customer": "Grace"}).get_json()
            exp = round(89.99 * 0.95, 2)  # 85.49
            ok = ok and abs(c_resp["subtotal"] - 89.99) < 0.01 and abs(c_resp["total"] - exp) < 0.02
            ok = ok and c_resp.get("loyalty", {}).get("tier") == "SILVER"
            print(f"Grace checkout: subtotal={c_resp['subtotal']} total={c_resp['total']}  expect {c_resp['subtotal']} / {exp}")
            if ok:
                print("PASS: loyalty program working"); sys.exit(0)
            else:
                print("FAIL: loyalty program not working"); sys.exit(1)

    else:
        app.run(host="0.0.0.0", port=5000, debug=True)
