"""Challenge D — Loyalty Rollout tests.

API-only (behavior contract). The winning agent is graded on what the HTTP
surface does, not on internal function names, so any correct implementation
passes.

Contract:
  Tiers by cumulative NON-cancelled spend (case-insensitive customer):
    BRONZE  < $100      -> 0% discount
    SILVER  $100..$199  -> 5% discount
    GOLD    >= $200     -> 10% discount

  GET  /api/loyalty/<customer>
       -> {customer, total_spent, tier, discount_pct}

  POST /api/cart  {"items":[...], "customer": "<name>"}
       applies that customer's loyalty discount_pct to the subtotal and returns
       a "loyalty" object {customer, tier, total_spent, discount_pct}.
  POST /api/cart  without "customer" -> unchanged: total == subtotal, no "loyalty".

Customers seeded in ORDERS (cumulative non-cancelled spend):
  Alice 79.98 (BRONZE), Grace 139.98 (SILVER), Carol 119.97 (SILVER)
"""
import pytest


@pytest.fixture
def client():
    # import lazily so the module path works regardless of working dir
    import importlib
    m = importlib.import_module("app")
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        yield c


# ---- Tier classification via the public endpoint ----

def test_loyalty_endpoint_silver(client):
    d = client.get("/api/loyalty/Grace").get_json()
    assert d["tier"] == "SILVER"
    assert d["discount_pct"] == 5
    assert d["total_spent"] == pytest.approx(139.98)


def test_loyalty_endpoint_bronze(client):
    d = client.get("/api/loyalty/Alice").get_json()
    assert d["tier"] == "BRONZE"
    assert d["discount_pct"] == 0
    assert d["total_spent"] == pytest.approx(79.98)


def test_tier_separates_bracket(client):
    # Grace (139.98) is a step above Alice (79.98) and must map to a higher tier
    g = client.get("/api/loyalty/Grace").get_json()
    a = client.get("/api/loyalty/Alice").get_json()
    assert g["tier"] != a["tier"]
    assert g["discount_pct"] > a["discount_pct"]


def test_loyalty_unknown_customer_is_bronze(client):
    d = client.get("/api/loyalty/NoOneHere").get_json()
    assert d["tier"] == "BRONZE"
    assert d["discount_pct"] == 0
    assert d["total_spent"] == 0.0


# ---- Checkout applies the loyalty discount ----

def test_checkout_apply_silver_discount(client):
    # 1 x Wireless Mouse = 29.99; Grace SILVER 5% off -> 29.99 * 0.95
    d = client.post("/api/cart", json={
        "items": [{"product_id": 1, "quantity": 1}], "customer": "Grace"
    }).get_json()
    assert d["subtotal"] == pytest.approx(29.99)
    assert d["total"] == pytest.approx(round(29.99 * 0.95, 2))
    assert d.get("loyalty", {}).get("tier") == "SILVER"


def test_checkout_apply_bronze_no_discount(client):
    # BRONZE 0% off -> total == subtotal
    d = client.post("/api/cart", json={
        "items": [{"product_id": 1, "quantity": 2}], "customer": "Alice"
    }).get_json()
    assert d["total"] == d["subtotal"]


def test_checkout_multiple_items_loyalty(client):
    # Grace SILVER: 1 mouse + 1 keyboard = 119.98 -> 5% off
    d = client.post("/api/cart", json={
        "items": [
            {"product_id": 1, "quantity": 1},
            {"product_id": 2, "quantity": 1},
        ],
        "customer": "Grace",
    }).get_json()
    assert d["subtotal"] == pytest.approx(119.98)
    assert d["total"] == pytest.approx(round(119.98 * 0.95, 2))


def test_checkout_case_insensitive_customer(client):
    d = client.post("/api/cart", json={
        "items": [{"product_id": 2, "quantity": 1}], "customer": "grace"
    }).get_json()
    assert d["loyalty"]["tier"] == "SILVER"
    assert d["total"] == pytest.approx(round(89.99 * 0.95, 2))


# ---- No customer -> baseline behavior is preserved ----

def test_checkout_no_customer_unchanged(client):
    d = client.post("/api/cart", json={
        "items": [{"product_id": 1, "quantity": 1}],
    }).get_json()
    assert d["total"] == d["subtotal"]
    assert "loyalty" not in d


def test_existing_product_endpoint_intact(client):
    d = client.get("/api/products").get_json()
    assert d and len(d["products"]) > 0
