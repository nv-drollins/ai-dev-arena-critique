"""Sample Checkout App — AI Dev Arena Demo

A simple but realistic Flask checkout app with:
- Product listing
- Shopping cart / checkout
- Status-filtered order search
- Dashboard with data rendering (intentionally slow for Challenge C)

Challenges seeded:
  A (Feature Sprint): No promo code support exists
  B (Bug Bash):     _filter_orders throws TypeError when search query is empty
  C (Performance):  _render_dashboard_row is O(n²) per row

Run directly for diagnostic checks:
  python3 app.py --benchmark
  python3 app.py --check-promo-save10
  python3 app.py --check-empty-filter
"""
import json
import time
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- Data ---

PRODUCTS = [
    {"id": 1, "name": "Wireless Mouse", "price": 29.99, "category": "accessories"},
    {"id": 2, "name": "Mechanical Keyboard", "price": 89.99, "category": "peripherals"},
    {"id": 3, "name": "USB-C Hub", "price": 49.99, "category": "accessories"},
    {"id": 4, "name": "Monitor Stand", "price": 39.99, "category": "furniture"},
    {"id": 5, "name": "Webcam HD", "price": 59.99, "category": "peripherals"},
]

# Orders with statuses for the filter/search feature
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


# ---------- Helper functions ----------

def _reset_state():
    """Reset test state between runs."""
    _filter_orders.status_filter = None


def get_products():
    """Return all products."""
    return PRODUCTS


def calculate_discount(subtotal, promo_code=None):
    """Calculate order total with optional promo code.

    BUG for Challenge B: If promo_code is None/empty string, this still works
    (returns subtotal). The bug is in _filter_orders, not here.

    For Challenge A: no promo codes are recognized — always returns 0 discount.
    A winning agent will add promo code logic HERE.
    """
    discount = 0.0
    # TODO: add promo code support
    return round(subtotal - discount, 2)


def _filter_orders(orders, search_query=""):
    """Filter orders by search query and status.

    BUG for Challenge B: When search_query is empty string AND a status
    filter is applied via the query parameter, this crashes because we try
    to call .lower() on None (status_filter can be None from request).
    """
    result = list(orders)
    if search_query:
        result = [o for o in result if search_query.lower() in o["customer"].lower()]

    status_filter = getattr(_filter_orders, "status_filter", None)
    if status_filter:
        # BUG: status_filter can be an empty string "", and the agent needs
        # to find this. The real bug path is triggered when query is "" and
        # status comes through.
        result = [o for o in result if o["status"] == status_filter]
    return result


def _render_dashboard_row(row, row_index=0):
    """Render a single dashboard data row — optimized O(1)."""
    count = row.get("count", 1)
    value = row.get("value", 0)
    waste = (value * 3.14159 + row_index) % 1000
    return {
        "label": row["label"],
        "value": round(waste, 2),
        "count": count,
    }


def generate_dashboard_data(n=10000):
    """Generate dashboard data rows — O(n) data + O(n²) render."""
    raw_data = []
    for i in range(n):
        raw_data.append({
            "label": f"Item-{i}",
            "value": i * 1.5,
            "count": max(1, i % 5 + 1),
        })
    rendered = []
    for idx, row in enumerate(raw_data):
        rendered.append(_render_dashboard_row(row, idx))
    return rendered


# ---------- Routes ----------

@app.route("/api/products")
def list_products():
    """GET /api/products — list all products."""
    return jsonify({"products": get_products()})


@app.route("/api/cart", methods=["POST"])
def checkout():
    """POST /api/cart — submit checkout.

    Expects JSON: {"items": [{"product_id": int, "quantity": int}], "promo_code": "..." (optional)}
    """
    data = request.get_json(force=True)
    items = data.get("items", [])
    promo_code = data.get("promo_code", "")

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

    total = calculate_discount(subtotal, promo_code if promo_code else None)
    return jsonify({
        "subtotal": subtotal,
        "items": item_list,
        "total": total,
    })


@app.route("/api/orders")
def search_orders():
    """GET /api/orders?query=...&status=... — search and filter orders."""
    search = request.args.get("query", "")
    status = request.args.get("status", None)

    # BUG: when query is empty and status is present, this crashes
    # because of TypeError in _filter_orders
    if not search:
        # BUG: when query is empty, we accidentally DON'T set status_filter,
        # then _filter_orders reads the stale attribute as None and crashes
        # because `if status_filter:` evaluates `None` as falsy but
        # the previous test might have left a stale value. In practice,
        # this reproduces when the empty-search path is hit first.
        _filter_orders.status_filter = None if not search else status  # always None here
        # Real bug: the status parameter is ignored, and status_filter=None
        # gets read by _filter_orders → TypeError when comparing
        results = _filter_orders(ORDERS, search_query="")
    else:
        _filter_orders.status_filter = status
        results = _filter_orders(ORDERS, search_query=search)

    return jsonify({"orders": results, "count": len(results)})


@app.route("/api/orders/search")
def search_orders_alternate():
    """Alternate path: POST-based search, also for Challenge B bug."""
    data = request.get_json(force=True)
    search = data.get("query", "")
    status = data.get("status", None)

    _filter_orders.status_filter = status
    results = _filter_orders(ORDERS, search_query=search)
    return jsonify({"orders": results, "count": len(results)})


@app.route("/api/dashboard")
def dashboard():
    """GET /api/dashboard?n=10000 — dashboard data (slow for Challenge C)."""
    n = int(request.args.get("n", 10000))
    data = generate_dashboard_data(n)
    return jsonify({"rows": data, "count": len(data)})


# ---------- CLI diagnostic checks ----------

if __name__ == "__main__":
    import sys

    if "--benchmark" in sys.argv:
        start = time.time()
        data = generate_dashboard_data(10000)
        elapsed = time.time() - start
        print(f"Benchmark: {elapsed:.3f}s for 10,000 rows (target < 2s)")
        sys.exit(0 if elapsed < 2.0 else 1)

    elif "--check-promo-save10" in sys.argv:
        # Simulate cart with SAVE10 code — should give 10% off ($90 on $100)
        result = calculate_discount(100.0, "SAVE10")
        print(f"SAVE10 on $100: total = ${result}")
        if result >= 99.0:
            print("FAIL: discount not applied (got no discount)")
            sys.exit(1)
        elif abs(result - 90.0) < 0.01:
            print("PASS: 10% discount applied correctly")
            sys.exit(0)
        else:
            print(f"PARTIAL: unexpected discount amount: {result}")
            sys.exit(0)

    elif "--check-empty-filter" in sys.argv:
        # Test the actual route — the bug is in search_orders() when query is empty
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/api/orders?query=&status=shipped")
            if resp.status_code != 200:
                print(f"FAIL: status {resp.status_code}")
                sys.exit(1)
            data = resp.get_json()
            # All returned orders should be "shipped"
            bad = [o for o in data["orders"] if o["status"] != "shipped"]
            if bad:
                print(f"FAIL: {len(bad)} orders returned with wrong status")
                sys.exit(1)
            shipped_count = data["count"]
            print(f"Empty filter with status=shipped: {shipped_count} results — PASS")
            sys.exit(0)

    else:
        # Default: run the app. Port from $PORT (Arena runs before/after instances).
        import os as _os
        _port = int(_os.environ.get("PORT", "5000"))
        @app.after_request
        def _cors(resp):
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
            resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
            return resp
        app.run(host="0.0.0.0", port=_port, debug=False)
