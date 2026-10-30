from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DATABASE_PATH", str(ROOT.parent / "data" / "orders.sqlite3")))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
BOT_SETTINGS_TOKEN = os.getenv("BOT_SETTINGS_TOKEN", "").strip()
ALLOW_ANONYMOUS_ORDERS = os.getenv("ALLOW_ANONYMOUS_ORDERS", "false").lower() == "true"
security = HTTPBasic()
app = FastAPI(title="Poizon Pickup", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
REQUESTS: dict[str, list[float]] = {}
REQUESTS_LOCK = threading.Lock()

RULES: dict[str, dict[str, Any]] = {
    "shoes": {
        "label": "Обувь",
        "delivery": "2 000–3 000 ₽",
    },
    "clothes": {
        "label": "Одежда и аксессуары",
        "delivery": "около 1 200 ₽",
    },
}


def money(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db() -> None:
    raw_rate = os.getenv("PRONTO_RATE", "").strip()
    raw_rate = raw_rate or "13.55"
    try:
        initial_rate = Decimal(raw_rate)
    except Exception as exc:
        raise RuntimeError("PRONTO_RATE must be a finite number between 0.01 and 100") from exc
    if not initial_rate.is_finite() or not Decimal("0.01") <= initial_rate <= Decimal("100"):
        raise RuntimeError("PRONTO_RATE must be a finite number between 0.01 and 100")
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
            CREATE TABLE IF NOT EXISTS settings (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        defaults = {
            "pronto_rate": str(initial_rate),
            "pronto_rate_is_demo": "false" if os.getenv("PRONTO_RATE", "").strip() else "true",
            "buffer_rub": "0.25",
            "shoes_new_percent": "20", "shoes_new_min_rub": "1500",
            "shoes_regular_percent": "15", "shoes_regular_min_rub": "1000",
            "clothes_new_percent": "20", "clothes_new_min_rub": "1000",
            "clothes_regular_percent": "15", "clothes_regular_min_rub": "700",
            "regular_after_paid_orders": "2",
        }
        con.executemany(
            "INSERT OR IGNORE INTO settings (name, value) VALUES (?, ?)", defaults.items()
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


def load_settings() -> dict[str, str]:
    with connect() as con:
        rows = con.execute("SELECT name, value FROM settings").fetchall()
    return {row["name"]: row["value"] for row in rows}


def rate_limit(key: str, *, limit: int, window_seconds: int) -> None:
    now = time.monotonic()
    with REQUESTS_LOCK:
        if len(REQUESTS) > 5000:
            for old_key in [name for name, stamps in REQUESTS.items() if not stamps or now - stamps[-1] >= window_seconds]:
                REQUESTS.pop(old_key, None)
        recent = [stamp for stamp in REQUESTS.get(key, []) if now - stamp < window_seconds]
        if len(recent) >= limit:
            raise HTTPException(status_code=429, detail="Слишком много запросов. Попробуйте позже.")
        recent.append(now)
        REQUESTS[key] = recent


def require_configured_admin_password() -> None:
    if len(ADMIN_PASSWORD) < 12 or ADMIN_PASSWORD == "local-demo-only":
        raise HTTPException(status_code=503, detail="Set an ADMIN_PASSWORD of at least 12 characters before using the admin panel")


def paid_order_count(con: sqlite3.Connection, telegram_id: str) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM orders WHERE telegram_id=? AND status IN ('Оплачена', 'Выкуплена', 'Выдана')",
        (telegram_id,),
    ).fetchone()[0]


def quote(category: str, price_cny: Decimal, client_type: str) -> dict[str, Any]:
    if category not in RULES:
        raise HTTPException(status_code=422, detail="Выберите категорию товара")
    if client_type not in ("new", "regular"):
        raise HTTPException(status_code=422, detail="Неизвестный статус клиента")
    settings = load_settings()
    base_rate = Decimal(settings["pronto_rate"])
    buffer_rub = Decimal(settings["buffer_rub"])
    purchase_rate = base_rate + buffer_rub
    item_rub = money(price_cny * purchase_rate)
    prefix = "shoes" if category == "shoes" else "clothes"
    tier = {
        "percent": Decimal(settings[f"{prefix}_{client_type}_percent"]) / 100,
        "minimum": Decimal(settings[f"{prefix}_{client_type}_min_rub"]),
    }
    commission = max(money(Decimal(item_rub) * tier["percent"]), money(tier["minimum"]))
    return {
        "pronto_rate": str(base_rate),
        "buffer_rub": str(buffer_rub),
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
    size_cm: Decimal = Field(ge=10, le=40)
    notes: str = Field(default="", max_length=1000)


class SettingsRequest(BaseModel):
    pronto_rate: Decimal
    buffer_rub: Decimal
    shoes_new_percent: Decimal
    shoes_new_min_rub: Decimal
    shoes_regular_percent: Decimal
    shoes_regular_min_rub: Decimal
    clothes_new_percent: Decimal
    clothes_new_min_rub: Decimal
    clothes_regular_percent: Decimal
    clothes_regular_min_rub: Decimal
    regular_after_paid_orders: int = Field(ge=1, le=100)


class OrderStatusRequest(BaseModel):
    status: str


def validate_settings(payload: SettingsRequest) -> dict[str, str]:
    values = payload.model_dump()
    for key, value in values.items():
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise HTTPException(status_code=422, detail=f"{key} must be finite")
            if key == "pronto_rate" and not Decimal("0.01") <= value <= Decimal("100"):
                raise HTTPException(status_code=422, detail="pronto_rate must be between 0.01 and 100")
            if key == "buffer_rub" and not Decimal("0") <= value <= Decimal("10"):
                raise HTTPException(status_code=422, detail="buffer_rub must be between 0 and 10")
            if "percent" in key and not Decimal("0") <= value <= Decimal("100"):
                raise HTTPException(status_code=422, detail=f"{key} must be between 0 and 100")
            if "min_rub" in key and not Decimal("0") <= value <= Decimal("1000000"):
                raise HTTPException(status_code=422, detail=f"{key} must be between 0 and 1000000")
    result = {key: str(value) for key, value in values.items()}
    result["pronto_rate_is_demo"] = "false"
    return result


def save_settings(values: dict[str, str]) -> None:
    with connect() as con:
        con.executemany(
            "INSERT INTO settings (name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value", values.items()
        )


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    settings = load_settings()
    base_rate = Decimal(settings["pronto_rate"])
    buffer_rub = Decimal(settings["buffer_rub"])
    return {
        "pronto_rate": str(base_rate),
        "rate_is_demo": settings.get("pronto_rate_is_demo") == "true",
        "buffer_rub": str(buffer_rub),
        "purchase_rate": str(base_rate + buffer_rub),
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
        count = paid_order_count(con, telegram_id)
    threshold = int(load_settings()["regular_after_paid_orders"])
    return {
        "telegram_linked": True,
        "client_type": "regular" if count >= threshold else "new",
        "previous_orders": count,
    }


@app.post("/api/orders")
def create_order(
    payload: OrderRequest,
    request: Request,
    x_telegram_init_data: str | None = Header(default=None),
) -> dict[str, Any]:
    telegram_id: str | None = None
    if x_telegram_init_data:
        user = validate_init_data(x_telegram_init_data)
        telegram_id = register_telegram_user(user)
    elif not ALLOW_ANONYMOUS_ORDERS or BOT_TOKEN:
        raise HTTPException(status_code=401, detail="Откройте приложение из Telegram")

    source = telegram_id or (request.client.host if request.client else "unknown")
    rate_limit(f"orders:{source}", limit=5, window_seconds=60)

    with connect() as con:
        prior_orders = 0
        if telegram_id:
            prior_orders = paid_order_count(con, telegram_id)
        threshold = int(load_settings()["regular_after_paid_orders"])
        client_type = "regular" if prior_orders >= threshold else "new"
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


def require_admin(request: Request, credentials: HTTPBasicCredentials = Depends(security)) -> str:
    require_configured_admin_password()
    correct_user = secrets.compare_digest(credentials.username.encode(), b"admin")
    correct_password = secrets.compare_digest(credentials.password.encode(), ADMIN_PASSWORD.encode())
    if not (correct_user and correct_password):
        host = request.client.host if request and request.client else "unknown"
        rate_limit(f"admin-auth:{host}", limit=10, window_seconds=300)
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
def admin_orders(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _: str = Depends(require_admin),
) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            """SELECT o.*,
                      (SELECT COUNT(*) FROM orders all_orders
                       WHERE all_orders.telegram_id=o.telegram_id
                         AND all_orders.status IN ('Оплачена', 'Выкуплена', 'Выдана')) AS order_count
               FROM orders o
               ORDER BY o.id DESC LIMIT ? OFFSET ?""",
            (limit, max(offset, 0)),
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/admin/settings")
def admin_get_settings(_: str = Depends(require_admin)) -> dict[str, str]:
    return load_settings()


@app.put("/api/admin/settings")
def admin_update_settings(payload: SettingsRequest, _: str = Depends(require_admin)) -> dict[str, str]:
    values = validate_settings(payload)
    save_settings(values)
    return load_settings()


@app.put("/api/bot/settings")
def bot_update_settings(
    payload: SettingsRequest,
    x_bot_settings_token: str | None = Header(default=None),
) -> dict[str, str]:
    if not BOT_SETTINGS_TOKEN:
        raise HTTPException(status_code=503, detail="Bot settings integration is not configured")
    if not x_bot_settings_token or not secrets.compare_digest(x_bot_settings_token, BOT_SETTINGS_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid bot settings token")
    save_settings(validate_settings(payload))
    return load_settings()


@app.patch("/api/admin/orders/{order_id}")
def admin_update_order(
    order_id: int,
    payload: OrderStatusRequest,
    _: str = Depends(require_admin),
) -> dict[str, Any]:
    allowed = {"Новая", "Оплачена", "Выкуплена", "Выдана", "Отменена"}
    if payload.status not in allowed:
        raise HTTPException(status_code=422, detail="Недопустимый статус заявки")
    with connect() as con:
        result = con.execute("UPDATE orders SET status=? WHERE id=?", (payload.status, order_id))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Заявка не найдена")
    return {"order_id": order_id, "status": payload.status}
