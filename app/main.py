from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DATABASE_PATH", str(ROOT.parent / "data" / "orders.sqlite3")))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "local-demo-only")
BUFFER_RUB = Decimal("0.25")
security = HTTPBasic()
app = FastAPI(title="Poizon Pickup", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

RULES: dict[str, dict[str, Any]] = {
    "shoes": {
        "label": "Обувь",
        "delivery": "2 000–3 000 ₽",
        "tiers": {
            "new": {"percent": Decimal("0.20"), "minimum": Decimal("1500")},
            "regular": {"percent": Decimal("0.15"), "minimum": Decimal("1000")},
        },
    },
    "clothes": {
        "label": "Одежда и аксессуары",
        "delivery": "около 1 200 ₽",
        "tiers": {
            "new": {"percent": Decimal("0.20"), "minimum": Decimal("1000")},
            "regular": {"percent": Decimal("0.15"), "minimum": Decimal("700")},
        },
    },
}


def money(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def pronto_rate() -> Decimal:
    raw = os.getenv("PRONTO_RATE", "").strip() or "13.55"
    try:
        rate = Decimal(raw)
    except Exception as exc:
        raise RuntimeError("PRONTO_RATE must be a positive number") from exc
    if rate <= 0:
        raise RuntimeError("PRONTO_RATE must be a positive number")
    return rate


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db() -> None:
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id TEXT PRIMARY KEY,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id TEXT REFERENCES users(telegram_id),
                category TEXT NOT NULL,
                product_url TEXT NOT NULL,
                item_price_cny TEXT NOT NULL,
                size_cm TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                pronto_rate TEXT NOT NULL,
                purchase_rate TEXT NOT NULL,
                item_rub INTEGER NOT NULL,
                client_type TEXT NOT NULL,
                commission_rub INTEGER NOT NULL,
                upfront_total_rub INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'Новая',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS orders_telegram_id ON orders(telegram_id);
            """
        )


@app.on_event("startup")
def startup() -> None:
    init_db()


def validate_init_data(init_data: str) -> dict[str, Any]:
    if not BOT_TOKEN:
        raise HTTPException(status_code=503, detail="Telegram bot token is not configured")
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        supplied_hash = fields.pop("hash")
        auth_date = int(fields["auth_date"])
        user = json.loads(fields["user"])
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="Invalid Telegram launch data") from exc
    now = int(time.time())
    if now - auth_date > 86_400 or auth_date > now + 30:
        raise HTTPException(status_code=401, detail="Telegram launch data has expired")
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash):
        raise HTTPException(status_code=401, detail="Telegram launch signature is invalid")
    if not isinstance(user, dict) or not user.get("id"):
        raise HTTPException(status_code=401, detail="Telegram user is missing")
    return user


def register_telegram_user(user: dict[str, Any]) -> str:
    telegram_id = str(user["id"])
    with connect() as con:
        con.execute(
            """INSERT INTO users (telegram_id) VALUES (?)
               ON CONFLICT(telegram_id) DO UPDATE SET last_seen_at=CURRENT_TIMESTAMP""",
            (telegram_id,),
        )
    return telegram_id


def quote(category: str, price_cny: Decimal, client_type: str) -> dict[str, Any]:
    if category not in RULES:
        raise HTTPException(status_code=422, detail="Выберите категорию товара")
    if client_type not in ("new", "regular"):
        raise HTTPException(status_code=422, detail="Неизвестный статус клиента")
    base_rate = pronto_rate()
    purchase_rate = base_rate + BUFFER_RUB
    item_rub = money(price_cny * purchase_rate)
    tier = RULES[category]["tiers"][client_type]
    commission = max(money(Decimal(item_rub) * tier["percent"]), money(tier["minimum"]))
    return {
        "pronto_rate": str(base_rate),
        "buffer_rub": str(BUFFER_RUB),
        "purchase_rate": str(purchase_rate),
        "item_rub": item_rub,
        "commission_rub": commission,
        "upfront_total_rub": item_rub + commission,
        "commission_percent": int(tier["percent"] * 100),
        "commission_minimum_rub": money(tier["minimum"]),
        "delivery_estimate": RULES[category]["delivery"],
        "delivery_days": "11–14 дней после отправки со склада в Китае",
        "client_type": client_type,
        "category_label": RULES[category]["label"],
    }


class QuoteRequest(BaseModel):
    category: str
    item_price_cny: Decimal = Field(gt=0, le=1_000_000)
    client_type: str = "new"


class OrderRequest(BaseModel):
    category: str
    item_price_cny: Decimal = Field(gt=0, le=1_000_000)
    product_url: HttpUrl
    size_cm: Decimal = Field(gt=0, le=80)
    notes: str = Field(default="", max_length=1000)


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return {
        "pronto_rate": str(pronto_rate()),
        "rate_is_demo": not bool(os.getenv("PRONTO_RATE", "").strip()),
        "buffer_rub": str(BUFFER_RUB),
        "purchase_rate": str(pronto_rate() + BUFFER_RUB),
        "telegram_configured": bool(BOT_TOKEN),
        "telegram_sdk_url": "https://telegram.org/js/telegram-web-app.js",
        "rules": {
            category: {
                "label": data["label"],
                "delivery_estimate": data["delivery"],
            }
            for category, data in RULES.items()
        },
    }


@app.post("/api/quote")
def get_quote(payload: QuoteRequest) -> dict[str, Any]:
    return quote(payload.category, payload.item_price_cny, payload.client_type)


@app.post("/api/session")
def register_session(x_telegram_init_data: str | None = Header(default=None)) -> dict[str, Any]:
    if not x_telegram_init_data:
        return {"telegram_linked": False, "client_type": "new", "previous_orders": 0}
    user = validate_init_data(x_telegram_init_data)
    telegram_id = register_telegram_user(user)
    with connect() as con:
        count = con.execute(
            "SELECT COUNT(*) FROM orders WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()[0]
    return {
        "telegram_linked": True,
        "client_type": "regular" if count >= 2 else "new",
        "previous_orders": count,
    }


@app.post("/api/orders")
def create_order(payload: OrderRequest, x_telegram_init_data: str | None = Header(default=None)) -> dict[str, Any]:
    telegram_id: str | None = None
    if x_telegram_init_data:
        user = validate_init_data(x_telegram_init_data)
        telegram_id = register_telegram_user(user)

    with connect() as con:
        prior_orders = 0
        if telegram_id:
            prior_orders = con.execute(
                "SELECT COUNT(*) FROM orders WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()[0]
        client_type = "regular" if prior_orders >= 2 else "new"
        result = quote(payload.category, payload.item_price_cny, client_type)
        cur = con.execute(
            """INSERT INTO orders (
                telegram_id, category, product_url, item_price_cny, size_cm, notes,
                pronto_rate, purchase_rate, item_rub, client_type, commission_rub,
                upfront_total_rub
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                telegram_id, payload.category, str(payload.product_url), str(payload.item_price_cny),
                str(payload.size_cm), payload.notes.strip(), result["pronto_rate"],
                result["purchase_rate"], result["item_rub"], client_type,
                result["commission_rub"], result["upfront_total_rub"],
            ),
        )
        order_id = cur.lastrowid
    return {
        "order_id": order_id,
        "telegram_linked": telegram_id is not None,
        "client_type": client_type,
        "quote": result,
        "message": "Заявка сохранена. Доставка будет рассчитана отдельно после взвешивания.",
    }


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    correct_user = secrets.compare_digest(credentials.username.encode(), b"admin")
    correct_password = secrets.compare_digest(credentials.password.encode(), ADMIN_PASSWORD.encode())
    if not (correct_user and correct_password):
        raise HTTPException(
            status_code=401,
            detail="Нужна авторизация администратора",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@app.get("/admin", response_class=HTMLResponse)
def admin_page(_: str = Depends(require_admin)) -> FileResponse:
    return FileResponse(ROOT / "static" / "admin.html")


@app.get("/api/admin/orders")
def admin_orders(_: str = Depends(require_admin)) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            """SELECT o.*,
                      (SELECT COUNT(*) FROM orders all_orders
                       WHERE all_orders.telegram_id=o.telegram_id) AS order_count
               FROM orders o
               ORDER BY o.id DESC LIMIT 200"""
        ).fetchall()
    return [dict(row) for row in rows]
