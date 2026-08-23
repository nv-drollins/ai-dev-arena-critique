"""Tests for the sample checkout app."""
import pytest
from app import app, PRODUCTS, ORDERS, calculate_discount, _filter_orders, _reset_state

@pytest.fixture
def client():
    _reset_state()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def reset_filter():
    """Ensure _filter_orders.status_filter is reset after each test."""
    _filter_orders.status_filter = None
    yield
    _filter_orders.status_filter = None


# ---- Product tests ----

def test_list_products(client):
    resp = client.get("/api/products")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "products" in data
    assert len(data["products"]) == len(PRODUCTS)


def test_product_has_price(client):
    resp = client.get("/api/products")
    for p in resp.get_json()["products"]:
        assert "price" in p
        assert p["price"] > 0


# ---- Checkout tests ----

def test_checkout_single_item(client):
    resp = client.post("/api/cart", json={"items": [{"product_id": 1, "quantity": 1}]})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] > 0
    assert "subtotal" in data


def test_checkout_multiple_items(client):
    items = [
        {"product_id": 1, "quantity": 2},
        {"product_id": 2, "quantity": 1},
    ]
    resp = client.post("/api/cart", json={"items": items})
    assert resp.status_code == 200
    expected = PRODUCTS[0]["price"] * 2 + PRODUCTS[1]["price"]
    assert resp.get_json()["subtotal"] == pytest.approx(expected)


def test_checkout_invalid_product(client):
    resp = client.post("/api/cart", json={"items": [{"product_id": 999, "quantity": 1}]})
    assert resp.status_code == 404


# ---- Order search tests ----

def test_search_orders_by_customer(client, reset_filter):
    resp = client.get("/api/orders?query=Alice")
    data = resp.get_json()
    assert data["count"] == 1
    assert data["orders"][0]["customer"] == "Alice"


def test_search_orders_no_match(client, reset_filter):
    resp = client.get("/api/orders?query=ZzzNotHere")
    data = resp.get_json()
    assert data["count"] == 0


def test_filter_orders_by_status(client, reset_filter):
    resp = client.get("/api/orders?query=&status=shipped")
    assert resp.status_code == 200
    data = resp.get_json()
    # Should NOT error — this is the key test for Bug Bash
    for o in data["orders"]:
        assert o["status"] == "shipped"


def test_filter_orders_combined(client, reset_filter):
    resp = client.get("/api/orders?query=Bob&status=shipped")
    data = resp.get_json()
    assert data["count"] == 1


# ---- Dashboard tests ----

def test_dashboard_returns_data(client):
    resp = client.get("/api/dashboard?n=100")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 100


def test_dashboard_rows_have_required_fields(client):
    resp = client.get("/api/dashboard?n=10")
    for row in resp.get_json()["rows"]:
        assert "label" in row
        assert "value" in row
        assert "count" in row


# ---- Promo code tests (Challenge A) ----

def test_promo_save10(client):
    """SAVE10 should give 10% off."""
    resp = client.post("/api/cart", json={
        "items": [{"product_id": 1, "quantity": 1}],
        "promo_code": "SAVE10",
    })
    data = resp.get_json()
    # Subtotal should be $29.99, total should be $26.99 (10% off)
    assert data["total"] == pytest.approx(round(29.99 * 0.9, 2)), \
        f"Expected ~26.99 with SAVE10, got {data['total']}"


def test_promo_welcome20(client):
    """WELCOME20 should give 20% off."""
    resp = client.post("/api/cart", json={
        "items": [{"product_id": 1, "quantity": 1}],
        "promo_code": "WELCOME20",
    })
    data = resp.get_json()
    assert data["total"] == pytest.approx(round(29.99 * 0.8, 2)), \
        f"Expected ~23.99 with WELCOME20, got {data['total']}"


def test_promo_invalid(client):
    """Invalid promo code should be rejected."""
    resp = client.post("/api/cart", json={
        "items": [{"product_id": 1, "quantity": 1}],
        "promo_code": "FAKECODE",
    })
    data = resp.get_json()
    # Should either return error OR subtotal unchanged
    assert data.get("error") is not None or data["total"] == 29.99


def test_no_promo_unchanged(client):
    """Without promo code, total equals subtotal."""
    resp = client.post("/api/cart", json={
        "items": [{"product_id": 1, "quantity": 1}],
    })
    data = resp.get_json()
    assert data["total"] == data["subtotal"]
