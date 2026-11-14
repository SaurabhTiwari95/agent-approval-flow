"""Customers and products: the server's own price list. The agent never supplies a price.

The sample data is a made-up wholesaler, "Acme Traders", and its made-up customers.
"""

from __future__ import annotations

import csv
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from approval.db import Database

CENT = Decimal("0.01")
CACHE_SECONDS = 60.0  # prices change rarely; an import clears the cache at once

SAMPLE_CUSTOMERS: list[tuple[int, str, str]] = [
    (1, "Kestrel Kitchenware", "purchasing@kestrel.example.com"),
    (2, "Marigold & Finch Interiors", "orders@marigoldfinch.example.com"),
    (3, "Northgate Hardware Co.", "buyer@northgate.example.com"),
    (4, "Oakridge Cafe Supplies", "hello@oakridge.example.com"),
    (5, "Pinecrest Office Goods", "accounts@pinecrest.example.com"),
    (6, "Riverside Linen House", "orders@riverside.example.com"),
]

SAMPLE_PRODUCTS: list[tuple[int, str, str, str]] = [
    (1, "SKU-00001", "Steel Mixing Bowl 3L", "12.50"),
    (2, "SKU-00002", "Bamboo Chopping Board", "18.00"),
    (3, "SKU-00003", "Cast Iron Skillet 26cm", "34.90"),
    (4, "SKU-00004", "Linen Tea Towel (pack of 3)", "9.75"),
    (5, "SKU-00005", "Ceramic Dinner Plate", "4.20"),
    (6, "SKU-00006", "Glass Storage Jar 1L", "6.80"),
    (7, "SKU-00007", "Stainless Cutlery Set (24 pc)", "42.00"),
    (8, "SKU-00008", "Cotton Apron", "11.25"),
    (9, "SKU-00009", "Enamel Mug", "5.40"),
    (10, "SKU-00010", "Silicone Spatula", "3.95"),
    (11, "SKU-00011", "Wooden Serving Tray", "22.00"),
    (12, "SKU-00012", "Copper Kettle 1.7L", "58.00"),
]


@dataclass(frozen=True)
class Product:
    id: int
    sku: str
    name: str
    unit_price: Decimal


@dataclass(frozen=True)
class Customer:
    id: int
    name: str
    email: str


def money(value: Decimal) -> str:
    return str(value.quantize(CENT))


def seed(db: Database) -> tuple[int, int]:
    """Insert the sample customers and products if the tables are empty. Returns rows added."""
    with db.tx() as conn:
        added = (0, 0)
        if not conn.execute("SELECT 1 FROM customers LIMIT 1").fetchone():
            conn.executemany("INSERT INTO customers (id, name, email) VALUES (?, ?, ?)", SAMPLE_CUSTOMERS)
            added = (len(SAMPLE_CUSTOMERS), added[1])
        if not conn.execute("SELECT 1 FROM products LIMIT 1").fetchone():
            conn.executemany("INSERT INTO products (id, sku, name, unit_price) VALUES (?, ?, ?, ?)", SAMPLE_PRODUCTS)
            added = (added[0], len(SAMPLE_PRODUCTS))
    return added


def import_products_csv(db: Database, path: Path) -> int:
    """Upsert products from a CSV with columns id,sku,name,unit_price (for example exported from the
    system invoices are posted to, so both sides price the same way). Validates every row first."""
    rows: list[tuple[int, str, str, str]] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for n, row in enumerate(csv.DictReader(fh), start=2):
            try:
                pid, sku, name = int(row["id"]), row["sku"].strip(), row["name"].strip()
                price = Decimal(row["unit_price"])
            except (KeyError, ValueError, InvalidOperation, AttributeError):
                raise ValueError(f"line {n}: need id,sku,name,unit_price") from None
            if pid < 1 or not sku or not name or len(sku) > 40 or len(name) > 200 or not (0 <= price < 10**9):
                raise ValueError(f"line {n}: out of range")
            rows.append((pid, sku, name, money(price)))
    with db.tx() as conn:
        conn.executemany(
            "INSERT INTO products (id, sku, name, unit_price) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET sku = excluded.sku, name = excluded.name, unit_price = excluded.unit_price",
            rows,
        )
    return len(rows)


class Catalogue:
    """Read-through cache of customers and products (in-process, CACHE_SECONDS expiry)."""

    def __init__(self, db: Database, ttl: float = CACHE_SECONDS, clock=time.monotonic):
        self.db, self.ttl, self.clock = db, ttl, clock
        self._lock = threading.Lock()
        self._loaded_at: float | None = None
        self._products: dict[int, Product] = {}
        self._customers: dict[int, Customer] = {}
        self._by_sku: dict[str, Product] = {}
        self._by_name: dict[str, Product] = {}

    def clear(self) -> None:
        with self._lock:
            self._loaded_at = None

    def _load(self) -> None:
        with self._lock:
            if self._loaded_at is not None and self.clock() - self._loaded_at < self.ttl:
                return
            self._products = {
                r["id"]: Product(r["id"], r["sku"], r["name"], Decimal(r["unit_price"]))
                for r in self.db.all("SELECT id, sku, name, unit_price FROM products WHERE active = 1 ORDER BY id")
            }
            self._by_sku = {p.sku.upper(): p for p in self._products.values()}
            self._by_name = {p.name.lower(): p for p in self._products.values()}
            self._customers = {
                r["id"]: Customer(r["id"], r["name"], r["email"])
                for r in self.db.all("SELECT id, name, email FROM customers ORDER BY name")
            }
            self._loaded_at = self.clock()

    def products(self) -> list[Product]:
        self._load()
        return list(self._products.values())

    def customers(self) -> list[Customer]:
        self._load()
        return list(self._customers.values())

    def product(self, product_id: int) -> Product | None:
        self._load()
        return self._products.get(product_id)

    def customer(self, customer_id: int | None) -> Customer | None:
        self._load()
        return self._customers.get(customer_id) if customer_id is not None else None

    def product_by_sku(self, sku: str) -> Product | None:
        self._load()
        return self._by_sku.get(sku.strip().upper())

    def product_by_name(self, name: str) -> Product | None:
        self._load()
        return self._by_name.get(" ".join(name.lower().split()))

    def match_customer(self, email: str | None, name: str | None) -> Customer | None:
        """Email first (exact, case-insensitive), then the exact company name. Never a fuzzy guess:
        an unmatched customer is shown to the approver as a problem to resolve."""
        self._load()
        if email:
            e = email.strip().lower()
            hit = next((c for c in self._customers.values() if c.email.lower() == e), None)
            if hit:
                return hit
        if name:
            n = " ".join(name.lower().split())
            return next((c for c in self._customers.values() if c.name.lower() == n), None)
        return None
