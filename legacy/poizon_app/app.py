import os
import re
import io
import json
import sqlite3
import datetime
from contextlib import closing
import hashlib
import hmac
import html
import math
import secrets
import tempfile
import time
import xml.etree.ElementTree as ET
from urllib.parse import parse_qsl, urlsplit
import requests
from dotenv import load_dotenv

from fastapi import FastAPI, Depends, HTTPException, Request, Form, Header, status, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from apscheduler.schedulers.background import BackgroundScheduler

# Генерация PDF и QR-кодов
import qrcode
from reportlab.lib.pagesizes import A5
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from PIL import Image, ImageOps, UnidentifiedImageError

# Load local configuration before reading environment-backed settings.
load_dotenv()

# --- НАСТРОЙКИ СЕРВИСА ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://127.0.0.1:8001/")
ENABLE_MARKETING_MESSAGES = os.getenv("ENABLE_MARKETING_MESSAGES", "false").lower() == "true"
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
PICKUP_SETTINGS_URL = os.getenv("POIZON_PICKUP_SETTINGS_URL", "").strip()
BOT_SETTINGS_TOKEN = os.getenv("BOT_SETTINGS_TOKEN", "").strip()

ADMIN_LOGIN = os.getenv("ADMIN_LOGIN", "")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "")
DEMO_LOGIN = os.getenv("DEMO_LOGIN", "")
DEMO_PASS = os.getenv("DEMO_PASSWORD", "")

ACTIVE_SESSIONS = {}
LAST_ORDER_SUBMITS = {}
ADMIN_LOGIN_ATTEMPTS = {}

BACKUP_DIR = os.path.join(os.getcwd(), "backups")
UPLOAD_DIR = os.path.join(os.getcwd(), "static", "uploads")
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

def get_current_admin_role(request: Request) -> str:
    token = request.cookies.get("session_token")
    if not token or token not in ACTIVE_SESSIONS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    
    sess = ACTIVE_SESSIONS[token]
    if datetime.datetime.utcnow() > sess["expires"]:
        del ACTIVE_SESSIONS[token]
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    
    return sess["role"]

def send_telegram_message(chat_id: int, text: str, reply_markup=None) -> bool:
    if not BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(url, json=payload, timeout=5)
        return r.status_code == 200
    except Exception as e:
        print(f"Ошибка отправки TG: {e}")
        return False

def send_telegram_document(chat_id: int, file_bytes: bytes, filename: str, caption: str = ""):
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    files = {"document": (filename, file_bytes, "application/pdf")}
    data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    try:
        requests.post(url, data=data, files=files, timeout=15)
    except Exception as e:
        print(f"Ошибка отправки PDF в TG: {e}")

def send_telegram_photo(chat_id: int, photo_bytes: bytes, caption: str):
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    files = {"photo": ("photo.jpg", photo_bytes, "image/jpeg")}
    data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    try:
        requests.post(url, data=data, files=files, timeout=10)
    except Exception as e:
        print(f"Ошибка отправки фото TG: {e}")

def download_telegram_photo(file_id: str) -> str:
    try:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        info_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}"
        r = requests.get(info_url, timeout=10).json()
        if r.get("ok"):
            file_path = r["result"]["file_path"]
            download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            with requests.get(download_url, timeout=15, stream=True) as response:
                response.raise_for_status()
                chunks = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    chunks.extend(chunk)
                    if len(chunks) > MAX_IMAGE_BYTES:
                        return ""
            img_data = normalize_image(bytes(chunks))
            filename = f"rev_{secrets.token_hex(8)}.jpg"
            dest = os.path.join(UPLOAD_DIR, filename)
            with open(dest, "wb") as f:
                f.write(img_data)
            return f"/static/uploads/{filename}"
    except Exception as e:
        print(f"[TG Photo Download Error] {e}")
    return ""

CACHED_EUR_RATE = 102.5
LAST_EUR_FETCH = None

def get_cbr_eur_rate() -> float:
    global CACHED_EUR_RATE, LAST_EUR_FETCH
    now = datetime.datetime.utcnow()
    if LAST_EUR_FETCH and (now - LAST_EUR_FETCH).total_seconds() < 3600:
        return CACHED_EUR_RATE
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get("https://www.cbr.ru/scripts/XML_daily.asp", headers=headers, timeout=4)
        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            for valute in root.findall("Valute"):
                if valute.find("CharCode") is not None and valute.find("CharCode").text == "EUR":
                    val_el = valute.find("Value")
                    if val_el is not None and val_el.text:
                        CACHED_EUR_RATE = float(val_el.text.replace(",", "."))
                        LAST_EUR_FETCH = now
                        break
    except Exception:
        pass
    return CACHED_EUR_RATE

def format_dt(dt) -> str:
    if not dt:
        return "—"
    if isinstance(dt, datetime.datetime):
        return dt.strftime("%d.%m %H:%M")
    if isinstance(dt, str):
        try:
            parts = dt.split()
            date_p = parts[0].split("-")
            time_p = parts[1].split(":")
            return f"{date_p[2]}.{date_p[1]} {time_p[0]}:{time_p[1]}"
        except Exception:
            return dt[:16]
    return str(dt)

# --- МОДЕЛИ БАЗЫ ДАННЫХ (SQLITE) ---
DATABASE_URL = "sqlite:///./database.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(Integer, unique=True, index=True)
    username = Column(String, default="")
    first_name = Column(String, default="")
    phone = Column(String, default="")
    client_status = Column(String, default="Новый")
    total_spent = Column(Float, default=0.0)
    orders_count = Column(Integer, default=0)
    default_shoe_size = Column(String, default="")
    default_cloth_size = Column(String, default="")
    height_cm = Column(Integer, default=None)
    weight_kg = Column(Integer, default=None)
    cdek_pvz = Column(String, default="")
    pass_tier = Column(String, default=None)
    pass_slots = Column(Integer, default=0)
    pass_expires_at = Column(DateTime, default=None)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    welcome_used = Column(Boolean, default=False)
    awaiting_review_order_id = Column(Integer, default=None)

    notified_welcome_24h = Column(Boolean, default=False)
    notified_welcome_2h = Column(Boolean, default=False)
    abandoned_cart_json = Column(Text, default="[]")
    abandoned_cart_updated_at = Column(DateTime, default=None)
    notified_abandoned = Column(Boolean, default=False)

class PromoCode(Base):
    __tablename__ = "promocodes"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, index=True)
    discount_rub = Column(Float, default=300.0)
    expires_at = Column(DateTime)
    is_active = Column(Boolean, default=True)

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=True)
    telegram_id = Column(Integer)
    username = Column(String, default="")
    phone = Column(String, default="")
    cdek_pvz = Column(String, default="")
    items_json = Column(Text, default="[]")
    total_cny = Column(Float, default=0.0)
    total_rub = Column(Float, default=0.0)
    service_fee = Column(Float, default=0.0)
    customs_duty_rub = Column(Float, default=0.0)
    applied_promo = Column(String, default="")
    has_insurance = Column(Boolean, default=False)
    delivery_rub = Column(Float, default=0.0)
    cdek_track = Column(String, default="")
    status = Column(String, default="Новый")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    delivered_at = Column(DateTime, default=None)
    rating = Column(Integer, default=0)
    review_text = Column(Text, default="")
    has_reviewed = Column(Boolean, default=False)

class ReviewShowcase(Base):
    __tablename__ = "reviews_showcase"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, default="Анонимный клиент")
    orders_count = Column(Integer, default=1)
    rating = Column(Integer, default=5)
    photo_url = Column(String, default="")
    review_text = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    likes_count = Column(Integer, default=12)

class SystemSettings(Base):
    __tablename__ = "settings"
    id = Column(Integer, primary_key=True)
    cny_rate = Column(Float, default=13.8)
    buffer_rate = Column(Float, default=0.7)
    regular_threshold = Column(Integer, default=2)
    fee_new_shoes_percent = Column(Float, default=20.0)
    fee_new_shoes_min = Column(Float, default=1500.0)
    fee_new_clothes_percent = Column(Float, default=20.0)
    fee_new_clothes_min = Column(Float, default=1000.0)
    fee_regular_shoes_percent = Column(Float, default=15.0)
    fee_regular_shoes_min = Column(Float, default=1000.0)
    fee_regular_clothes_percent = Column(Float, default=15.0)
    fee_regular_clothes_min = Column(Float, default=700.0)
    pass_price_bronze = Column(Float, default=3500.0)
    pass_price_silver = Column(Float, default=5200.0)
    pass_price_gold = Column(Float, default=7900.0)
    pass_price_platinum = Column(Float, default=10900.0)
    welcome_bonus_enabled = Column(Boolean, default=True)
    welcome_bonus_rub = Column(Float, default=500.0)
    welcome_bonus_hours = Column(Integer, default=72)
    custom_delivery_days = Column(Integer, default=0)  # Ручная настройка срока доставки (0 = авто)

Base.metadata.create_all(bind=engine)

def auto_migrate_db():
    import sqlite3
    db_file = "database.db"
    if os.path.exists(db_file):
        try:
            con = sqlite3.connect(db_file)
            cursor = con.cursor()
            cursor.execute("PRAGMA table_info(orders);")
            columns = [row[1] for row in cursor.fetchall()]
            if "has_insurance" not in columns:
                cursor.execute("ALTER TABLE orders ADD COLUMN has_insurance BOOLEAN DEFAULT 0;")
            if "has_reviewed" not in columns:
                cursor.execute("ALTER TABLE orders ADD COLUMN has_reviewed BOOLEAN DEFAULT 0;")
            
            cursor.execute("PRAGMA table_info(settings);")
            settings_cols = [row[1] for row in cursor.fetchall()]
            if "custom_delivery_days" not in settings_cols:
                cursor.execute("ALTER TABLE settings ADD COLUMN custom_delivery_days INTEGER DEFAULT 0;")
            
            cursor.execute("PRAGMA table_info(users);")
            user_cols = [row[1] for row in cursor.fetchall()]
            new_u_cols = {
                "awaiting_review_order_id": "INTEGER DEFAULT NULL",
                "notified_welcome_24h": "BOOLEAN DEFAULT 0",
                "notified_welcome_2h": "BOOLEAN DEFAULT 0",
                "abandoned_cart_json": "TEXT DEFAULT '[]'",
                "abandoned_cart_updated_at": "DATETIME DEFAULT NULL",
                "notified_abandoned": "BOOLEAN DEFAULT 0"
            }
            for col, ddl in new_u_cols.items():
                if col not in user_cols:
                    cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl};")
            con.commit()
            con.close()
        except Exception as e:
            print(f"[Auto-Migrate Error] {e}")

auto_migrate_db()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- ФОНОВЫЕ ЗАДАЧИ ---
def auto_backup_database():
    db_source = "database.db"
    if os.path.exists(db_source):
        now_str = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        dest = os.path.join(BACKUP_DIR, f"backup_database_{now_str}.db")
        with closing(sqlite3.connect(db_source)) as source, closing(sqlite3.connect(dest)) as target:
            source.backup(target)
        backups = sorted(
            (os.path.join(BACKUP_DIR, name) for name in os.listdir(BACKUP_DIR) if name.startswith("backup_database_") and name.endswith(".db")),
            key=os.path.getmtime,
            reverse=True,
        )
        for stale in backups[30:]:
            os.remove(stale)

def background_marketing_triggers():
    db = SessionLocal()
    try:
        now = datetime.datetime.utcnow()
        settings = db.query(SystemSettings).first()
        if not settings:
            return
        wb_hours = settings.welcome_bonus_hours or 72

        users = db.query(User).all()
        for u in users:
            if settings.welcome_bonus_enabled and not u.welcome_used and (u.orders_count == 0):
                passed_hours = (now - u.created_at).total_seconds() / 3600.0
                remain_hours = wb_hours - passed_hours

                if remain_hours <= 24.0 and remain_hours > 2.0 and not u.notified_welcome_24h:
                    send_telegram_message(
                        u.telegram_id,
                        "⏳ <b>Твой приветственный бонус сгорит завтра.</b>\nСамое время забрать ту самую пару со скидкой в PoizonPickUP!",
                        reply_markup={"inline_keyboard": [[{"text": "🔥 Забрать со скидкой", "web_app": {"url": WEBAPP_URL}}]]}
                    )
                    u.notified_welcome_24h = True
                    db.commit()

                elif remain_hours <= 2.0 and remain_hours > 0 and not u.notified_welcome_2h:
                    send_telegram_message(
                        u.telegram_id,
                        "🚨 <b>Последний шанс применить бонус на комиссию!</b>\nЧерез 2 часа приветственная скидка будет аннулирована.",
                        reply_markup={"inline_keyboard": [[{"text": "⚡ Использовать сейчас", "web_app": {"url": WEBAPP_URL}}]]}
                    )
                    u.notified_welcome_2h = True
                    db.commit()

            if u.abandoned_cart_updated_at and not u.notified_abandoned:
                diff_hours = (now - u.abandoned_cart_updated_at).total_seconds() / 3600.0
                if 3.5 <= diff_hours <= 24.0:
                    try:
                        cart_data = json.loads(u.abandoned_cart_json)
                    except Exception:
                        cart_data = []

                    if cart_data:
                        ask_btn = {
                            "inline_keyboard": [
                                [{"text": "👟 Вернуться в корзину", "web_app": {"url": WEBAPP_URL}}],
                                [{"text": "💬 Задать вопрос менеджеру", "url": f"https://t.me/{ADMIN_USERNAME}"}]
                            ]
                        }
                        send_telegram_message(
                            u.telegram_id,
                            "👋 <b>Ты рассчитывал стоимость заказа, но не оформил.</b>\nНужна помощь с размером или возникли вопросы по доставке? Мы всегда на связи!",
                            reply_markup=ask_btn
                        )
                        u.notified_abandoned = True
                        db.commit()
    except Exception as e:
        print(f"[Marketing Trigger Error] {e}")
    finally:
        db.close()

scheduler = BackgroundScheduler()
scheduler.add_job(auto_backup_database, 'interval', hours=6)
if ENABLE_MARKETING_MESSAGES:
    scheduler.add_job(background_marketing_triggers, 'interval', minutes=10)
scheduler.start()

app = FastAPI(title="PoizonPickUP")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="static")


class RequestBodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == "/admin/backup/restore":
            limit = 100 * 1024 * 1024 + 128 * 1024
        elif path in {"/api/order/photo-order", "/admin/reviews/create"}:
            limit = MAX_IMAGE_BYTES + 128 * 1024
        else:
            limit = 1024 * 1024

        headers = dict(scope.get("headers", []))
        try:
            content_length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            content_length = 0
        if content_length > limit:
            response = JSONResponse({"detail": "Размер запроса превышает допустимый лимит"}, status_code=413)
            await response(scope, receive, send)
            return

        body_file = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
        total = 0
        try:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                total += len(chunk)
                if total > limit:
                    response = JSONResponse({"detail": "Размер запроса превышает допустимый лимит"}, status_code=413)
                    await response(scope, receive, send)
                    return
                body_file.write(chunk)
                if not message.get("more_body", False):
                    break

            body_file.seek(0)

            async def replay_body():
                chunk = body_file.read(64 * 1024)
                if chunk:
                    return {"type": "http.request", "body": chunk, "more_body": body_file.tell() < total}
                return {"type": "http.disconnect"}

            await self.app(scope, replay_body, send)
        finally:
            body_file.close()


app.add_middleware(RequestBodyLimitMiddleware)

def clean_dewu_url(raw_text: str) -> str:
    match = re.search(r'(https?://[^\s]+)', raw_text)
    return match.group(0).split('?')[0] if match else raw_text.strip()


MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000


def normalize_image(contents: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(contents)) as source:
            if source.format not in ("JPEG", "PNG", "WEBP"):
                raise HTTPException(status_code=400, detail="Допустимы только JPEG, PNG или WEBP")
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise HTTPException(status_code=400, detail="Слишком большое разрешение изображения")
            source.verify()
        with Image.open(io.BytesIO(contents)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=90, optimize=True)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(status_code=400, detail="Файл не является корректным изображением") from exc


async def read_image_upload(upload: UploadFile) -> bytes:
    contents = await upload.read(MAX_IMAGE_BYTES + 1)
    if len(contents) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Размер изображения не должен превышать 5 МБ")
    return normalize_image(contents)


def validate_telegram_init_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(status_code=503, detail="Telegram bot token is not configured")
    if not init_data or len(init_data) > 4096:
        raise HTTPException(status_code=401, detail="Откройте приложение из Telegram")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError("duplicate fields")
        supplied_hash = fields.pop("hash")
        auth_date = int(fields["auth_date"])
        user = json.loads(fields["user"])
        user_id = int(user["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="Некорректные данные Telegram") from exc
    now = int(time.time())
    if auth_date <= 0 or now - auth_date > 86_400 or auth_date > now + 30:
        raise HTTPException(status_code=401, detail="Данные Telegram устарели")
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash) or user_id <= 0:
        raise HTTPException(status_code=401, detail="Подпись Telegram недействительна")
    return user


def require_telegram_user(x_telegram_init_data: str | None = Header(default=None)) -> dict:
    return validate_telegram_init_data(x_telegram_init_data or "")

# --- ЛАЙКИ НА ОТЗЫВЫ ---
@app.post("/api/reviews/like")
async def like_showcase_review(review_id: int = Form(...), db: Session = Depends(get_db)):
    rev = db.query(ReviewShowcase).filter(ReviewShowcase.id == review_id).first()
    if rev:
        rev.likes_count = (rev.likes_count or 0) + 1
        db.commit()
        return JSONResponse({"status": "ok", "new_likes": rev.likes_count})
    return JSONResponse({"status": "error"}, status_code=404)

# --- ГЕНЕРАТОР PDF-ЧЕКА ---
def get_apple_fonts():
    font_regular, font_bold = "Helvetica", "Helvetica-Bold"
    candidate_fonts = [
        ("SFPro", "SF-Pro-Text-Regular.otf", "SFPro-Bold", "SF-Pro-Display-Bold.otf"),
        ("SFPro", "SFProText-Regular.ttf", "SFPro-Bold", "SFProDisplay-Bold.ttf"),
        ("C:\\Windows\\Fonts\\arial.ttf", "ArialRus", "C:\\Windows\\Fonts\\arialbd.ttf", "ArialRus-Bold")
    ]
    for p_reg, name_reg, p_b, name_b in candidate_fonts:
        if os.path.exists(p_reg) and os.path.exists(p_b):
            try:
                pdfmetrics.registerFont(TTFont(name_reg, p_reg))
                pdfmetrics.registerFont(TTFont(name_b, p_b))
                font_regular, font_bold = name_reg, name_b
                break
            except Exception:
                pass
    return font_regular, font_bold

def generate_order_receipt_pdf(order: Order, user: User) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A5, rightMargin=22, leftMargin=22, topMargin=22, bottomMargin=22)
    story = []
    font_regular, font_bold = get_apple_fonts()

    styles = getSampleStyleSheet()
    brand_style = ParagraphStyle('BrandHeading', fontName=font_bold, fontSize=20, leading=24, alignment=1)
    doc_type_style = ParagraphStyle('DocType', fontName=font_regular, fontSize=9, leading=12, textColor=colors.HexColor('#6E6E73'), alignment=1)
    cell_style = ParagraphStyle('ReceiptCell', fontName=font_regular, fontSize=9, leading=13, textColor=colors.HexColor('#1D1D1F'))
    cell_bold = ParagraphStyle('ReceiptCellBold', fontName=font_bold, fontSize=9, leading=13, textColor=colors.HexColor('#1D1D1F'))
    cell_right_bold = ParagraphStyle('ReceiptCellRightBold', fontName=font_bold, fontSize=9, leading=13, alignment=2, textColor=colors.HexColor('#1D1D1F'))
    cell_nowrap = ParagraphStyle('ReceiptNoWrap', fontName=font_bold, fontSize=9, leading=13, alignment=2, textColor=colors.HexColor('#1D1D1F'))

    story.append(Paragraph("<font color='#000000'>PoizonPick</font><font color='#00E5C9'>UP!</font>", brand_style))
    story.append(Spacer(1, 2))
    story.append(Paragraph("ЭЛЕКТРОННЫЙ ЧЕК ЗАКАЗА", doc_type_style))
    story.append(Spacer(1, 14))

    info_data = [
        [Paragraph(f"<b>Заказ №:</b> #{order.id}", cell_style), Paragraph(f"<b>Дата:</b> {format_dt(order.created_at)}", cell_style)],
        [Paragraph(f"<b>Клиент:</b> @{html.escape(str(order.username or ''), quote=False)}", cell_style), Paragraph(f"<b>Телефон:</b> {html.escape(str(order.phone or '—'), quote=False)}", cell_style)],
        [Paragraph(f"<b>ПВЗ СДЭК:</b> {html.escape(str(order.cdek_pvz or '—'), quote=False)}", cell_style), Paragraph(f"<b>Оплата:</b> Подтверждена", cell_style)]
    ]
    t_info = Table(info_data, colWidths=[185, 185])
    t_info.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('TOPPADDING', (0,0), (-1,-1), 2)
    ]))
    story.append(t_info)
    story.append(Spacer(1, 12))

    items = []
    try:
        items = json.loads(order.items_json) if order.items_json else []
    except Exception:
        pass

    item_rows = [[Paragraph("<b>Товар / Размер</b>", cell_bold), Paragraph("<b>Dewu (¥)</b>", cell_bold), Paragraph("<b>Сумма (₽)</b>", cell_right_bold)]]
    for idx, it in enumerate(items):
        item_rows.append([
            Paragraph(f"{idx+1}. {html.escape(str(it.get('category', 'Товар')), quote=False)} ({html.escape(str(it.get('size', '—')), quote=False)})", cell_style),
            Paragraph(f"{it.get('price_cny')} ¥", cell_style),
            Paragraph(f"{it.get('item_rub')} ₽", cell_right_bold)
        ])

    t_items = Table(item_rows, colWidths=[195, 75, 100])
    t_items.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#F5F5F7')),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E5E5EA')),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE')
    ]))
    story.append(t_items)
    story.append(Spacer(1, 12))

    summary_rows = [
        [Paragraph("Комиссия сервиса:", cell_style), Paragraph(f"{order.service_fee} ₽", cell_right_bold)]
    ]
    if order.has_insurance:
        summary_rows.append([Paragraph("Страховка посылки (2.5%):", cell_style), Paragraph("Включена", cell_right_bold)])
    if order.customs_duty_rub > 0:
        summary_rows.append([Paragraph("Таможенная пошлина (>200€):", cell_style), Paragraph(f"{order.customs_duty_rub} ₽", cell_right_bold)])
    if order.applied_promo:
        clean_promo = html.escape(str(order.applied_promo).strip(), quote=False)
        summary_rows.append([Paragraph("Промокод на скидку:", cell_style), Paragraph(f"<b>{clean_promo}</b>", cell_nowrap)])

    summary_rows.append([Paragraph("<b>ИТОГО К ОПЛАТЕ:</b>", cell_bold), Paragraph(f"<b>{order.total_rub} ₽</b>", cell_right_bold)])

    t_sum = Table(summary_rows, colWidths=[210, 160])
    t_sum.setStyle(TableStyle([
        ('ALIGN', (1,0), (1,-1), 'RIGHT'),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('LINEBELOW', (0,-1), (-1,-1), 1, colors.HexColor('#00E5C9'))
    ]))
    story.append(t_sum)
    story.append(Spacer(1, 14))

    qr_url = f"https://t.me/PPU_order_bot?start=track_{order.id}"
    qr = qrcode.QRCode(box_size=3, border=1)
    qr.add_data(qr_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")

    qr_buf = io.BytesIO()
    qr_img.save(qr_buf, format="PNG")
    qr_buf.seek(0)
    rl_qr = RLImage(qr_buf, width=60, height=60)

    qr_text = "<font size='8' color='#6E6E73'>Отсканируйте QR-код для мгновенного отслеживания этапов доставки в Telegram-боте PoizonPickUP!.</font>"
    qr_table = Table([[rl_qr, Paragraph(qr_text, cell_style)]], colWidths=[70, 300])
    qr_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('ALIGN', (0,0), (0,0), 'CENTER')
    ]))
    story.append(qr_table)

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()

def process_order_paid(order: Order, db: Session):
    order.status = "Оплачен"
    db.commit()

    user = db.query(User).filter(User.id == order.user_id).first()
    if user:
        settings = db.query(SystemSettings).first()
        if settings:
            paid_count = db.query(Order).filter(
                Order.telegram_id == user.telegram_id,
                Order.status.in_(["Оплачен", "Выкуплен", "Выдан"]),
            ).count()
            user.client_status = "Постоянный" if paid_count >= settings.regular_threshold else "Новый"
            db.commit()
    try:
        pdf_bytes = generate_order_receipt_pdf(order, user)
        filename = f"Receipt_PPU_Order_{order.id}.pdf"
        caption = (
            f"🧾 <b>Оплата заказа #{order.id} подтверждена!</b>\n\n"
            f"Ваш официальный электронный чек сформирован и прикреплён к этому сообщению. "
            f"Товары отправлены на выкуп на складе в Китае."
        )
        send_telegram_document(order.telegram_id, pdf_bytes, filename, caption)
    except Exception as e:
        print(f"Ошибка чека: {e}")
        send_telegram_message(order.telegram_id, f"✅ <b>Оплата заказа #{order.id} подтверждена!</b>")

# --- TELEGRAM WEBHOOK: ПРИЁМ ОЦЕНОК, ТЕКСТА И ФОТО-ОТЗЫВОВ ---
@app.post(f"/bot-webhook/{BOT_TOKEN}")
async def telegram_webhook(request: Request, db: Session = Depends(get_db)):
    data = await request.json()

    if "callback_query" in data:
        cq = data["callback_query"]
        cq_data = cq.get("data", "")
        chat_id = cq["message"]["chat"]["id"]

        if cq_data.startswith("rate:"):
            _, order_id_str, score_str = cq_data.split(":")
            order_id = int(order_id_str)
            score = int(score_str)

            order = db.query(Order).filter(Order.id == order_id).first()
            user = db.query(User).filter(User.telegram_id == chat_id).first()
            
            if order and order.status == "Выдан" and not order.has_reviewed:
                order.rating = score
                if user:
                    user.awaiting_review_order_id = order_id
                db.commit()

                send_telegram_message(
                    chat_id,
                    f"⭐ Спасибо за вашу оценку <b>{score}/5</b> по заказу #{order.id}!\n\n"
                    f"✍ Пожалуйста, напишите пару слов в ответном сообщении (<b>можно прикрепить фото вещей!</b>) — как вам качество и доставка? Ваш отзыв будет опубликован в сервисе."
                )
            else:
                send_telegram_message(chat_id, "ℹ️ Отзыв по данному заказу уже был сохранён ранее.")

        elif cq_data.startswith("adm_pay:"):
            order_id = int(cq_data.split(":")[1])
            order = db.query(Order).filter(Order.id == order_id).first()
            if order:
                process_order_paid(order, db)
                send_telegram_message(ADMIN_CHAT_ID, f"✅ Заказ #{order.id} оплачен. Чек отправлен клиенту.")

        elif cq_data.startswith("adm_cancel:"):
            order_id = int(cq_data.split(":")[1])
            order = db.query(Order).filter(Order.id == order_id).first()
            if order:
                order.status = "Отменен"
                user = db.query(User).filter(User.id == order.user_id).first() if order.user_id else None
                settings = db.query(SystemSettings).first()
                if user and settings:
                    paid_count = db.query(Order).filter(
                        Order.telegram_id == user.telegram_id,
                        Order.status.in_(["Оплачен", "Выкуплен", "Выдан"]),
                    ).count()
                    user.client_status = "Постоянный" if paid_count >= settings.regular_threshold else "Новый"
                db.commit()
                send_telegram_message(order.telegram_id, f"❌ Заказ #{order.id} отклонён менеджером.")
                send_telegram_message(ADMIN_CHAT_ID, f"🚫 Заказ #{order.id} отклонён.")

        return {"ok": True}

    if "message" in data:
        message = data["message"]
        chat_id = message["chat"]["id"]
        username = message["chat"].get("username", "")
        first_name = message["chat"].get("first_name", "Клиент")
        text = message.get("text") or message.get("caption") or ""

        user = db.query(User).filter(User.telegram_id == chat_id).first()
        if not user:
            user = User(telegram_id=chat_id, username=username, first_name=first_name)
            db.add(user)
            db.commit()
            db.refresh(user)

        settings = db.query(SystemSettings).first()
        wb_rub = int(settings.welcome_bonus_rub) if settings else 500

        # Обработка контакта
        if "contact" in message:
            user.phone = message["contact"]["phone_number"]
            db.commit()
            send_telegram_message(chat_id, "✅ <b>Номер успешно сохранен!</b>", reply_markup={"remove_keyboard": True})
            send_telegram_message(
                chat_id, 
                "Добро пожаловать в сервис доставки PoizonPickUP!\nНажмите кнопку ниже, чтобы открыть приложение:", 
                reply_markup={"inline_keyboard": [[{"text": "🚀 Открыть PPU App", "web_app": {"url": WEBAPP_URL}}]]}
            )
            return {"ok": True}

        # Обработка отзыва на полученный заказ
        if user.awaiting_review_order_id:
            raw_text = (message.get("text") or message.get("caption") or "").strip()
            
            if not raw_text.startswith("/"):
                order = db.query(Order).filter(Order.id == user.awaiting_review_order_id).first()
                if order and not order.has_reviewed:
                    review_content = raw_text if raw_text else "Заказ получен, все отлично!"
                    
                    order.review_text = review_content
                    order.has_reviewed = True
                    user.awaiting_review_order_id = None
                    
                    # Скачивание фото, если прикреплено
                    photo_url = ""
                    photos = message.get("photo")
                    if photos and isinstance(photos, list) and len(photos) > 0:
                        best_photo = photos[-1]
                        file_id = best_photo.get("file_id")
                        if file_id:
                            photo_url = download_telegram_photo(file_id)

                    if user.first_name:
                        disp_name = f"{user.first_name} {user.username[0].upper()}." if user.username else user.first_name
                    else:
                        disp_name = f"@{user.username}" if user.username else "Клиент PPU"

                    rev_show = ReviewShowcase(
                        username=disp_name,
                        orders_count=max(1, user.orders_count),
                        rating=order.rating or 5,
                        photo_url=photo_url,
                        review_text=review_content,
                        likes_count=18
                    )
                    db.add(rev_show)
                    db.commit()

                    send_telegram_message(chat_id, "🤝 <b>Большое спасибо за отзыв!</b> Он опубликован на витрине сервиса.")
                    return {"ok": True}
                else:
                    user.awaiting_review_order_id = None
                    db.commit()

        # Команда /start с уведомлением о конфиденциальности номера
        if text.startswith("/start"):
            user.awaiting_review_order_id = None
            db.commit()

            has_no_orders = (user.orders_count == 0)
            hours_passed = (datetime.datetime.utcnow() - user.created_at).total_seconds() / 3600.0
            bonus_applicable = (
                settings 
                and settings.welcome_bonus_enabled 
                and not user.welcome_used 
                and has_no_orders 
                and hours_passed < settings.welcome_bonus_hours
            )

            welcome_text = "👋 <b>Добро пожаловать в PoizonPickUP!</b>\n\n"
            if bonus_applicable:
                welcome_text += f"🎁 Вам начислен <b>Welcome-бонус {wb_rub} ₽</b> на комиссию первого заказа!\n\n"

            if not user.phone:
                welcome_text += "Не волнуйтесь, все данные конфиденциальны, а номер будет использован для таможенного оформления товара🥹\n\nДля продолжения подтвердите ваш номер кнопкой ниже:"
                send_telegram_message(
                    chat_id, 
                    welcome_text, 
                    reply_markup={"keyboard": [[{"text": "📱 Поделиться номером", "request_contact": True}]], "resize_keyboard": True, "one_time_keyboard": True}
                )
            else:
                send_telegram_message(
                    chat_id, 
                    welcome_text + "Нажмите кнопку ниже, чтобы запустить приложение:", 
                    reply_markup={"inline_keyboard": [[{"text": "🚀 Открыть PPU App", "web_app": {"url": WEBAPP_URL}}]]}
                )

    return {"ok": True}

# --- МАРШРУТЫ API ---
@app.get("/", response_class=HTMLResponse)
async def web_app_home(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/api/user/sync")
async def sync_user(
    telegram_user: dict = Depends(require_telegram_user),
    db: Session = Depends(get_db)
):
    telegram_id = int(telegram_user["id"])
    username = str(telegram_user.get("username") or "")[:64]
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        user = User(telegram_id=telegram_id, username=username.replace("@", ""))
        db.add(user)
        db.commit()
        db.refresh(user)

    if user.pass_expires_at and datetime.datetime.utcnow() > user.pass_expires_at:
        user.pass_slots = 0
        user.pass_tier = None
        db.commit()

    orders = db.query(Order).filter(Order.telegram_id == telegram_id).order_by(Order.id.desc()).all()
    active_orders, archive_orders = [], []
    for o in orders:
        items = json.loads(o.items_json) if o.items_json else []
        order_data = {
            "id": o.id, "status": o.status, "total_rub": o.total_rub, "customs_duty_rub": o.customs_duty_rub, 
            "items": items, "delivery_rub": o.delivery_rub, "cdek_track": o.cdek_track, "date": format_dt(o.created_at)
        }
        if o.status in ["Выдан", "Отменен"]:
            archive_orders.append(order_data)
        else:
            active_orders.append(order_data)

    settings = db.query(SystemSettings).first()
    if not settings:
        settings = SystemSettings()
        db.add(settings)
        db.commit()

    # Определение срока доставки: если админ задал вручную custom_delivery_days > 0, берем его
    if settings.custom_delivery_days and settings.custom_delivery_days > 0:
        delivery_days = settings.custom_delivery_days
    else:
        all_delivered = db.query(Order).filter(Order.status == "Выдан", Order.delivered_at != None, Order.created_at != None).all()
        if all_delivered:
            total_days = sum((o.delivered_at - o.created_at).days for o in all_delivered)
            delivery_days = max(10, round(total_days / len(all_delivered)))
        else:
            delivery_days = 13

    predicted_date = (datetime.datetime.utcnow() + datetime.timedelta(days=delivery_days)).strftime("%d.%m")

    welcome_active = False
    if settings.welcome_bonus_enabled and not user.welcome_used and (user.orders_count == 0):
        hours_passed = (datetime.datetime.utcnow() - user.created_at).total_seconds() / 3600.0
        if hours_passed < settings.welcome_bonus_hours:
            welcome_active = True

    return {
        "id": user.id, "telegram_id": user.telegram_id, "username": user.username, "phone": user.phone or "",
        "first_name": user.first_name or "",
        "client_status": user.client_status, "total_spent": user.total_spent or 0.0,
        "default_shoe_size": user.default_shoe_size or "", "default_cloth_size": user.default_cloth_size or "",
        "height_cm": user.height_cm or "", "weight_kg": user.weight_kg or "", "cdek_pvz": user.cdek_pvz or "",
        "pass_tier": user.pass_tier or "", "pass_slots": user.pass_slots or 0,
        "active_orders": active_orders, "archive_orders": archive_orders,
        "calc_rate": round(settings.cny_rate + settings.buffer_rate, 2), "eur_rate": round(get_cbr_eur_rate(), 2),
        "admin_username": ADMIN_USERNAME,
        "predicted_delivery_date": f"{predicted_date} (≈ {delivery_days} дн.)",
        "welcome_bonus": {"active": welcome_active, "rub": settings.welcome_bonus_rub},
        "pass_prices": {
            "Bronze": settings.pass_price_bronze, "Silver": settings.pass_price_silver, 
            "Gold": settings.pass_price_gold, "Platinum": settings.pass_price_platinum
        },
        "fee_rules": {
            "is_regular": user.client_status != "Новый", "new_shoes_p": settings.fee_new_shoes_percent, 
            "new_shoes_min": settings.fee_new_shoes_min, "new_clothes_p": settings.fee_new_clothes_percent, 
            "new_clothes_min": settings.fee_new_clothes_min, "reg_shoes_p": settings.fee_regular_shoes_percent, 
            "reg_shoes_min": settings.fee_regular_shoes_min, "reg_clothes_p": settings.fee_regular_clothes_percent, 
            "reg_clothes_min": settings.fee_regular_clothes_min
        }
    }

@app.post("/api/user/cart-sync")
async def sync_cart_state(
    telegram_user: dict = Depends(require_telegram_user),
    cart_json: str = Form("[]"),
    db: Session = Depends(get_db)
):
    telegram_id = int(telegram_user["id"])
    if len(cart_json) > 100_000:
        raise HTTPException(status_code=413, detail="Корзина слишком большая")
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if user:
        user.abandoned_cart_json = cart_json
        user.abandoned_cart_updated_at = datetime.datetime.utcnow()
        user.notified_abandoned = False
        db.commit()
    return JSONResponse({"status": "ok"})

@app.get("/api/reviews/list")
async def get_showcase_reviews(db: Session = Depends(get_db)):
    revs = db.query(ReviewShowcase).order_by(ReviewShowcase.created_at.desc()).all()
    data = []
    for r in revs:
        p_url = (r.photo_url or "").strip()
        if not re.fullmatch(r"/static/uploads/[A-Za-z0-9._-]+", p_url):
            p_url = ""
        text_content = (r.review_text or "").strip()
        if not text_content:
            text_content = "Все круто!"

        data.append({
            "id": r.id,
            "username": r.username or "Клиент",
            "orders_count": r.orders_count or 1,
            "rating": r.rating or 5,
            "photo_url": p_url,
            "review_text": text_content,
            "likes_count": r.likes_count or 0,
            "date": r.created_at.strftime("%d.%m.%Y") if r.created_at else "Недавно"
        })
    return JSONResponse(data)

@app.post("/api/user/save-profile")
async def save_user_profile(
    telegram_user: dict = Depends(require_telegram_user), default_shoe_size: str = Form("", max_length=32), default_cloth_size: str = Form("", max_length=32),
    height_cm: str = Form("", max_length=3), weight_kg: str = Form("", max_length=3), cdek_pvz: str = Form("", max_length=128), db: Session = Depends(get_db)
):
    telegram_id = int(telegram_user["id"])
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if user:
        user.default_shoe_size = default_shoe_size.strip()
        user.default_cloth_size = default_cloth_size.strip()
        user.height_cm = int(height_cm) if (height_cm and height_cm.isdigit()) else None
        user.weight_kg = int(weight_kg) if (weight_kg and weight_kg.isdigit()) else None
        user.cdek_pvz = cdek_pvz.strip().upper()
        db.commit()
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "error"}, status_code=404)

@app.post("/api/promo/check")
async def check_promo(code: str = Form(...), db: Session = Depends(get_db)):
    clean_code = code.strip().upper()
    promo = db.query(PromoCode).filter(PromoCode.code == clean_code, PromoCode.is_active == True).first()
    if not promo or (promo.expires_at and datetime.datetime.utcnow() > promo.expires_at):
        return JSONResponse({"valid": False, "message": "Промокод недействителен"})
    return JSONResponse({"valid": True, "discount_rub": promo.discount_rub, "code": promo.code})

@app.post("/api/order/submit")
async def submit_order_cart(
    telegram_user: dict = Depends(require_telegram_user), cart_json: str = Form(..., max_length=100_000), promo_code: str = Form("", max_length=64),
    cdek_pvz: str = Form("", max_length=128), has_insurance: str = Form("false"), db: Session = Depends(get_db)
):
    telegram_id = int(telegram_user["id"])
    now = datetime.datetime.utcnow().timestamp()
    if telegram_id in LAST_ORDER_SUBMITS:
        if now - LAST_ORDER_SUBMITS[telegram_id] < 4.0:
            return JSONResponse({"status": "ok", "message": "Заказ уже обрабатывается"})
    LAST_ORDER_SUBMITS[telegram_id] = now

    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    settings = db.query(SystemSettings).first()
    if not user or not settings:
        raise HTTPException(status_code=400, detail="Ошибка пользователя")

    try:
        items = json.loads(cart_json)
    except Exception:
        raise HTTPException(status_code=400, detail="Неверный формат корзины")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="Корзина пуста")

    if len(items) > 30:
        raise HTTPException(status_code=400, detail="В одном заказе может быть не больше 30 товаров")

    rate = round(settings.cny_rate + settings.buffer_rate, 2)
    eur_rate = get_cbr_eur_rate()
    if not math.isfinite(rate) or rate <= 0 or not math.isfinite(eur_rate) or eur_rate <= 0:
        raise HTTPException(status_code=503, detail="Курс валют не настроен")
    recalc_items = []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            raise HTTPException(status_code=400, detail="Некорректный товар в корзине")
        category = raw_item.get("category")
        if category not in ("Обувь", "Одежда"):
            raise HTTPException(status_code=400, detail="Недопустимая категория товара")
        try:
            price_cny = float(raw_item.get("price_cny"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Некорректная цена товара")
        if not math.isfinite(price_cny) or not 0 < price_cny <= 1_000_000:
            raise HTTPException(status_code=400, detail="Цена товара вне допустимого диапазона")
        size = str(raw_item.get("size") or "").strip()[:32]
        if not size:
            raise HTTPException(status_code=400, detail="Укажите размер каждого товара")
        url = clean_dewu_url(str(raw_item.get("url") or ""))[:2048]
        parsed_url = urlsplit(url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname:
            raise HTTPException(status_code=400, detail="Ссылка на товар должна быть http или https")

        base_rub = price_cny * rate
        if user.pass_slots > 0:
            fee = 0
        elif category == "Обувь" and user.client_status == "Новый":
            fee = max(settings.fee_new_shoes_min, base_rub * settings.fee_new_shoes_percent / 100)
        elif category == "Обувь":
            fee = max(settings.fee_regular_shoes_min, base_rub * settings.fee_regular_shoes_percent / 100)
        elif user.client_status == "Новый":
            fee = max(settings.fee_new_clothes_min, base_rub * settings.fee_new_clothes_percent / 100)
        else:
            fee = max(settings.fee_regular_clothes_min, base_rub * settings.fee_regular_clothes_percent / 100)
        rounded_fee = math.floor(fee + 0.5)
        item_rub = math.floor(base_rub + rounded_fee + 0.5)
        euro_value = base_rub / eur_rate
        duty = math.floor(((euro_value - 200) * 0.15 * eur_rate + 689) * 1.05 + 0.5) if euro_value > 200 else 0
        recalc_items.append({
            "category": category, "url": url, "size": size, "price_cny": price_cny,
            "fee": rounded_fee, "item_rub": item_rub, "duty": duty,
        })
    items = recalc_items

    insurance_enabled = (has_insurance.lower() == "true")
    total_cny = sum(i["price_cny"] for i in items)
    base_rub = sum(i["item_rub"] for i in items)
    total_fee = sum(i["fee"] for i in items)
    total_duty = sum(i.get("duty", 0.0) for i in items)
    
    insurance_fee = round(base_rub * 0.025) if insurance_enabled else 0
    total_rub = base_rub + insurance_fee

    discount = 0.0
    applied_promo = ""

    if settings.welcome_bonus_enabled and not user.welcome_used and (user.orders_count == 0):
        hours_passed = (datetime.datetime.utcnow() - user.created_at).total_seconds() / 3600.0
        if hours_passed < settings.welcome_bonus_hours:
            discount = min(total_fee, settings.welcome_bonus_rub)
            total_fee -= discount
            total_rub -= discount
            applied_promo = f"WELCOME_BONUS_{int(settings.welcome_bonus_rub)}"
            user.welcome_used = True

    if not applied_promo and promo_code:
        promo = db.query(PromoCode).filter(PromoCode.code == promo_code.strip().upper(), PromoCode.is_active == True).first()
        if promo and (not promo.expires_at or datetime.datetime.utcnow() <= promo.expires_at):
            discount = min(total_fee, promo.discount_rub)
            total_fee -= discount
            total_rub -= discount
            applied_promo = promo.code

    pvz_final = cdek_pvz.strip().upper() or user.cdek_pvz or "Не указан"

    new_order = Order(
        user_id=user.id, telegram_id=user.telegram_id, username=user.username, phone=user.phone,
        cdek_pvz=pvz_final, items_json=json.dumps(items, ensure_ascii=False), total_cny=total_cny,
        total_rub=total_rub, service_fee=total_fee, customs_duty_rub=total_duty,
        applied_promo=applied_promo, has_insurance=insurance_enabled, status="Новый"
    )
    db.add(new_order)
    user.orders_count += 1

    user.abandoned_cart_json = "[]"
    user.abandoned_cart_updated_at = None
    user.notified_abandoned = True

    db.commit()
    db.refresh(new_order)

    items_text = "".join([
        f"{idx+1}. <b>{html.escape(str(it.get('category')), quote=False)}</b> | Разм: {html.escape(str(it.get('size')), quote=False)} | {it.get('price_cny')} ¥ (≈ {it.get('item_rub')} ₽)\nURL: {html.escape(str(it.get('url')), quote=False)}\n"
        for idx, it in enumerate(items)
    ])
    
    admin_msg = (
        f"🔥 <b>Новый заказ #{new_order.id}!</b>\n"
        f"Клиент: @{html.escape(str(user.username or ''), quote=False)} (TG: <code>{user.telegram_id}</code>)\n"
        f"Телефон: <b>{html.escape(str(user.phone or 'Не указан'), quote=False)}</b>\n"
        f"Статус: <b>{html.escape(str(user.client_status), quote=False)}</b>\n"
        f"ПВЗ СДЭК: <b>{html.escape(str(pvz_final), quote=False)}</b>\n"
        f"Рост/Вес: {user.height_cm or '—'} см / {user.weight_kg or '—'} кг\n"
        f"🛡 Страховка (+2.5%): <b>{'Подключена (' + str(insurance_fee) + ' ₽)' if insurance_enabled else 'Отключена'}</b>\n\n"
        f"📦 <b>Товары:</b>\n{items_text}\n"
        f"💰 <b>ИТОГО: {total_rub} ₽</b>\n"
        f"📊 Комиссия: {total_fee} ₽\n"
    )
    if total_duty > 0:
        admin_msg += f"🚨 <b>Таможенная пошлина (>200€): ~{total_duty} ₽</b>\n"
    if applied_promo:
        admin_msg += f"🎟 Применено: <code>{html.escape(str(applied_promo), quote=False)}</code> (-{discount} ₽)\n"

    adm_keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Оплачен", "callback_data": f"adm_pay:{new_order.id}"},
            {"text": "🚫 Отклонить", "callback_data": f"adm_cancel:{new_order.id}"}
        ]]
    }

    send_telegram_message(ADMIN_CHAT_ID, admin_msg, reply_markup=adm_keyboard)
    return JSONResponse({"status": "ok"})

@app.post("/api/order/photo-order")
async def photo_order(
    telegram_user: dict = Depends(require_telegram_user),
    comment: str = Form("", max_length=2000),
    photo: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    telegram_id = int(telegram_user["id"])
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        raise HTTPException(status_code=400, detail="Пользователь не найден")

    contents = await read_image_upload(photo)
    caption = (
        f"📸 <b>НОВЫЙ ЗАКАЗ ПО ФОТО!</b>\n\n"
        f"Клиент: @{html.escape(str(user.username or ''), quote=False)} (TG: <code>{user.telegram_id}</code>)\n"
        f"Телефон: <b>{html.escape(str(user.phone or 'Не указан'), quote=False)}</b>\n"
        f"ПВЗ СДЭК: <b>{html.escape(str(user.cdek_pvz or 'Не указан'), quote=False)}</b>\n"
        f"Параметры: {user.height_cm or '—'} см / {user.weight_kg or '—'} кг\n"
        f"Размеры: Обувь {user.default_shoe_size or '—'} | Одежда {user.default_cloth_size or '—'}\n\n"
        f"📝 <b>Комментарий:</b>\n{html.escape(comment.strip() or 'Без комментария', quote=False)}"
    )
    send_telegram_photo(ADMIN_CHAT_ID, contents, caption)
    return JSONResponse({"status": "ok"})

# --- АВТОРИЗАЦИЯ АДМИНКИ ---
@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    token = request.cookies.get("session_token")
    if token and token in ACTIVE_SESSIONS:
        if datetime.datetime.utcnow() <= ACTIVE_SESSIONS[token]["expires"]:
            return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/admin/auth/login")
async def admin_login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    remember: str = Form("false")
):
    host = request.client.host if request.client else "unknown"
    now = time.monotonic()
    recent = [stamp for stamp in ADMIN_LOGIN_ATTEMPTS.get(host, []) if now - stamp < 300]
    if len(recent) >= 10:
        raise HTTPException(status_code=429, detail="Слишком много попыток входа. Попробуйте позже.")
    role = None
    if ADMIN_PASS and username == ADMIN_LOGIN and password == ADMIN_PASS:
        role = "admin"
    elif DEMO_PASS and username == DEMO_LOGIN and password == DEMO_PASS:
        role = "demo"

    if not role:
        recent.append(now)
        ADMIN_LOGIN_ATTEMPTS[host] = recent
        return JSONResponse({"status": "error", "message": "Неверный логин или пароль"}, status_code=400)
    ADMIN_LOGIN_ATTEMPTS.pop(host, None)

    token = secrets.token_hex(32)
    is_remember = (remember.lower() == "true")
    days = 30 if is_remember else 1
    expires = datetime.datetime.utcnow() + datetime.timedelta(days=days)
    max_age = days * 24 * 3600

    ACTIVE_SESSIONS[token] = {"role": role, "expires": expires}

    response = JSONResponse({"status": "ok", "role": role})
    response.set_cookie(
        key="session_token", value=token, max_age=max_age, expires=max_age,
        httponly=True, samesite="lax", secure=request.url.scheme == "https"
    )
    return response

@app.get("/admin/logout")
async def admin_logout(request: Request):
    token = request.cookies.get("session_token")
    if token and token in ACTIVE_SESSIONS:
        del ACTIVE_SESSIONS[token]
    response = RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(key="session_token")
    return response

# --- ДЕМО РЕЖИМ (SANDBOX) ---
def get_demo_dashboard_data():
    demo_settings = SystemSettings(
        cny_rate=13.5, buffer_rate=0.8, regular_threshold=3,
        fee_new_shoes_percent=20.0, fee_new_shoes_min=1500.0,
        fee_new_clothes_percent=20.0, fee_new_clothes_min=1000.0,
        fee_regular_shoes_percent=15.0, fee_regular_shoes_min=1000.0,
        fee_regular_clothes_percent=15.0, fee_regular_clothes_min=700.0,
        pass_price_bronze=3500.0, pass_price_silver=5200.0,
        pass_price_gold=7900.0, pass_price_platinum=10900.0,
        welcome_bonus_enabled=True, welcome_bonus_rub=500.0, welcome_bonus_hours=72,
        custom_delivery_days=14
    )
    demo_orders = [
        {"id": 901, "created_at": "18.10 14:20", "username": "demo_user_1", "phone": "", "cdek_pvz": "DEMO", "items": [{"category": "Обувь", "size": "42 EU", "url": ""}], "total_rub": 14200, "customs_duty_rub": 0, "status": "Выдан", "delivery_rub": 1850, "cdek_track": "DEMO-TRACK-001", "rating": 5, "review_text": "Отличная пара, доставка за 12 дней до Москвы!"},
        {"id": 902, "created_at": "17.10 18:45", "username": "demo_user_2", "phone": "", "cdek_pvz": "DEMO", "items": [{"category": "Одежда", "size": "L", "url": ""}], "total_rub": 8900, "customs_duty_rub": 0, "status": "СДЭК", "delivery_rub": 1200, "cdek_track": "DEMO-TRACK-002", "rating": 5, "review_text": "Худи приехало с бирками и сертификатом."},
        {"id": 903, "created_at": "17.10 11:10", "username": "demo_user_3", "phone": "", "cdek_pvz": "DEMO", "items": [{"category": "Обувь", "size": "37.5 EU", "url": ""}], "total_rub": 22400, "customs_duty_rub": 850, "status": "На таможне в России", "delivery_rub": 2100, "cdek_track": "", "rating": 0, "review_text": ""}
    ]
    demo_users = [
        User(id=1, telegram_id=0, username="demo_user_1", phone="", client_status="Постоянный", orders_count=4, total_spent=52300, pass_tier="Silver", pass_slots=3, cdek_pvz="DEMO"),
        User(id=2, telegram_id=0, username="demo_user_2", phone="", client_status="Новый", orders_count=1, total_spent=8900, pass_tier=None, pass_slots=0, cdek_pvz="DEMO")
    ]
    demo_promos = [{"code": "DEMO300", "discount_rub": 300.0, "expires_str": "31.12 23:59", "orders_count": 8}]
    chart_labels = ["12.10", "13.10", "14.10", "15.10", "16.10", "17.10", "18.10"]
    demo_analytics = {
        "rev": 290000, "prof": 58200, "avg_del": 13.4, "avg_check": 12600, "expenses": 231800, "margin": 20.1,
        "clients_total": 52, "current_month_rev": 290000, "prev_month_rev": 245000, "rev_growth_mom": 18.4,
        "current_month_orders": 23, "prev_month_orders": 19, "orders_growth_mom": 21.0, "count_day": 4, "count_week": 23,
        "count_month": 58, "count_year": 342, "chart_labels_json": json.dumps(chart_labels),
        "chart_rev_json": json.dumps([28400, 34200, 19800, 48900, 52100, 61400, 45200]),
        "chart_prof_thousands_json": json.dumps([5.6, 6.8, 4.1, 9.8, 10.4, 12.3, 9.1]),
        "new_cust_orders_json": json.dumps([2, 3, 1, 4, 3, 5, 2]), "repeat_cust_orders_json": json.dumps([1, 1, 2, 3, 4, 3, 4]),
        "status_labels_json": json.dumps(["Новый", "Оплачен", "Выкуплен", "СДЭК", "Выдан"]), "status_values_json": json.dumps([3, 5, 4, 4, 15]),
        "funnel_stages": [{"name": "Оформлен", "count": 28}, {"name": "Выдан клиенту", "count": 15}], "welcome_bonus_count": 11
    }
    return demo_settings, demo_orders, demo_users, demo_promos, demo_analytics

# --- АДМИНИСТРАТИВНАЯ ПАНЕЛЬ ---
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    try:
        auth_role = get_current_admin_role(request)
    except HTTPException:
        return RedirectResponse(url="/admin/login", status_code=status.HTTP_302_FOUND)

    is_demo = (auth_role == "demo")
    if is_demo:
        settings, orders_data, users, promos_data, analytics_dict = get_demo_dashboard_data()
        showcase_reviews = []
        return templates.TemplateResponse("admin.html", {
            "request": request, "settings": settings, "orders": orders_data, "users": users, "promos": promos_data,
            "eur_rate": round(get_cbr_eur_rate(), 2), "stats": analytics_dict, "is_demo": True, "showcase_reviews": showcase_reviews
        })

    settings = db.query(SystemSettings).first()
    if not settings:
        settings = SystemSettings()
        db.add(settings)
        db.commit()

    orders_raw = db.query(Order).order_by(Order.id.desc()).all()
    users = db.query(User).order_by(User.id.desc()).all()
    promos_raw = db.query(PromoCode).order_by(PromoCode.id.desc()).all()
    showcase_reviews = db.query(ReviewShowcase).order_by(ReviewShowcase.created_at.desc()).all()

    orders_data = []
    total_rev, total_prof, total_del_time, del_count = 0, 0, 0, 0
    now = datetime.datetime.utcnow().date()
    current_month_rev, prev_month_rev, current_month_orders, prev_month_orders = 0, 0, 0, 0

    first_day_curr_month = now.replace(day=1)
    prev_month_last_day = first_day_curr_month - datetime.timedelta(days=1)
    first_day_prev_month = prev_month_last_day.replace(day=1)

    status_counts = {"Новый": 0, "Оплачен": 0, "Выкуплен": 0, "Ожидается на складе": 0, "На складе в Китае": 0, "Отправлен": 0, "На таможне в России": 0, "СДЭК": 0, "Выдан": 0, "Отменен": 0}
    last_7_days = [now - datetime.timedelta(days=i) for i in range(6, -1, -1)]
    chart_labels = [d.strftime("%d.%m") for d in last_7_days]
    chart_rev = [0] * 7
    chart_prof = [0] * 7
    count_day, count_week, count_month, count_year = 0, 0, 0, 0

    promo_stats = {p.code: 0 for p in promos_raw}
    promo_stats["WELCOME_BONUS"] = 0
    new_cust_orders, repeat_cust_orders = [0] * 7, [0] * 7
    user_order_history_counts = {}

    for o in sorted(orders_raw, key=lambda x: x.created_at or datetime.datetime.min):
        st = o.status or "Новый"
        if st in status_counts:
            status_counts[st] += 1

        if o.applied_promo:
            if o.applied_promo.startswith("WELCOME_BONUS"):
                promo_stats["WELCOME_BONUS"] += 1
            elif o.applied_promo in promo_stats:
                promo_stats[o.applied_promo] += 1

        if o.status != "Отменен":
            total_rev += (o.total_rub or 0)
            total_prof += (o.service_fee or 0)

            if o.created_at:
                o_date = o.created_at.date()
                diff_days = (now - o_date).days
                if diff_days == 0: count_day += 1
                if diff_days <= 7: count_week += 1
                if diff_days <= 30: count_month += 1
                if diff_days <= 365: count_year += 1

                if o_date >= first_day_curr_month:
                    current_month_rev += (o.total_rub or 0)
                    current_month_orders += 1
                elif first_day_prev_month <= o_date <= prev_month_last_day:
                    prev_month_rev += (o.total_rub or 0)
                    prev_month_orders += 1

                if o_date in last_7_days:
                    idx = last_7_days.index(o_date)
                    chart_rev[idx] += int(o.total_rub or 0)
                    chart_prof[idx] += int(o.service_fee or 0)

                    uid = o.user_id or o.telegram_id
                    prev_cnt = user_order_history_counts.get(uid, 0)
                    if prev_cnt == 0:
                        new_cust_orders[idx] += 1
                    else:
                        repeat_cust_orders[idx] += 1
                    user_order_history_counts[uid] = prev_cnt + 1

            if o.status == "Выдан" and o.delivered_at and o.created_at:
                total_del_time += (o.delivered_at - o.created_at).days
                del_count += 1

        try:
            itms = json.loads(o.items_json) if o.items_json else []
        except Exception:
            itms = []

        rendered_items = []
        for item in itms:
            raw_url = str(item.get("url", ""))[:2048]
            parsed_url = urlsplit(raw_url)
            safe_url = raw_url if parsed_url.scheme in ("http", "https") and parsed_url.hostname else ""
            rendered_items.append({
                "category": str(item.get("category", "Товар"))[:64],
                "size": str(item.get("size", "—"))[:32],
                "url": safe_url,
            })

        orders_data.append({
            "id": o.id, "created_at": format_dt(o.created_at), "username": o.username or "—", "phone": o.phone or "—",
            "cdek_pvz": o.cdek_pvz or "—", "items": rendered_items, "total_rub": o.total_rub or 0,
            "customs_duty_rub": o.customs_duty_rub or 0, "status": o.status or "Новый",
            "delivery_rub": o.delivery_rub or 0, "cdek_track": o.cdek_track or "",
            "rating": o.rating or 0, "review_text": o.review_text or ""
        })

    avg_del = round(total_del_time / del_count, 1) if del_count > 0 else 0
    non_canceled_count = len([o for o in orders_raw if o.status != 'Отменен'])
    avg_check = round(total_rev / non_canceled_count) if non_canceled_count > 0 else 0
    estimated_expenses = round(total_rev - total_prof)
    margin_percent = round((total_prof / total_rev) * 100, 1) if total_rev > 0 else 0.0
    chart_prof_thousands = [round(val / 1000.0, 1) for val in chart_prof]

    rev_growth_mom = round(((current_month_rev - prev_month_rev) / prev_month_rev) * 100, 1) if prev_month_rev > 0 else 0.0
    orders_growth_mom = round(((current_month_orders - prev_month_orders) / prev_month_orders) * 100, 1) if prev_month_orders > 0 else 0.0

    funnel_stages = [
        {"name": "Оформлен", "count": status_counts["Новый"] + status_counts["Оплачен"]},
        {"name": "Выкуплен", "count": status_counts["Выкуплен"] + status_counts["Ожидается на складе"]},
        {"name": "Склад КНР", "count": status_counts["На складе в Китае"] + status_counts["Отправлен"]},
        {"name": "Таможня / СДЭК", "count": status_counts["На таможне в России"] + status_counts["СДЭК"]},
        {"name": "Выдан клиенту", "count": status_counts["Выдан"]}
    ]

    promos_data = [
        {"code": p.code, "discount_rub": p.discount_rub, "expires_str": format_dt(p.expires_at), "orders_count": promo_stats.get(p.code, 0)}
        for p in promos_raw
    ]

    analytics_dict = {
        "rev": int(total_rev), "prof": int(total_prof), "avg_del": avg_del, "avg_check": avg_check,
        "expenses": estimated_expenses, "margin": margin_percent, "clients_total": len(users),
        "current_month_rev": int(current_month_rev), "prev_month_rev": int(prev_month_rev),
        "rev_growth_mom": rev_growth_mom, "current_month_orders": current_month_orders,
        "prev_month_orders": prev_month_orders, "orders_growth_mom": orders_growth_mom,
        "count_day": count_day, "count_week": count_week, "count_month": count_month, "count_year": count_year,
        "chart_labels_json": json.dumps(chart_labels), "chart_rev_json": json.dumps(chart_rev),
        "chart_prof_thousands_json": json.dumps(chart_prof_thousands),
        "new_cust_orders_json": json.dumps(new_cust_orders), "repeat_cust_orders_json": json.dumps(repeat_cust_orders),
        "status_labels_json": json.dumps(list(status_counts.keys())), "status_values_json": json.dumps(list(status_counts.values())),
        "funnel_stages": funnel_stages, "welcome_bonus_count": promo_stats.get("WELCOME_BONUS", 0)
    }

    return templates.TemplateResponse("admin.html", {
        "request": request, "settings": settings, "orders": orders_data, "users": users, "promos": promos_data,
        "eur_rate": round(get_cbr_eur_rate(), 2), "stats": analytics_dict, "is_demo": False, "showcase_reviews": showcase_reviews
    })

# --- УПРАВЛЕНИЕ ОТЗЫВАМИ В АДМИНКЕ ---
@app.post("/admin/reviews/create")
async def create_showcase_review(
    username: str = Form(..., max_length=80),
    orders_count: int = Form(1, ge=1, le=10000),
    rating: int = Form(5, ge=1, le=5),
    review_text: str = Form(..., max_length=2000),
    custom_date: str = Form(""),
    photo: UploadFile = File(None),
    auth_role: str = Depends(get_current_admin_role),
    db: Session = Depends(get_db)
):
    if auth_role == "demo":
        return JSONResponse({"status": "ok", "message": "В демо-режиме добавление отзывов имитировано"})

    photo_url = ""
    if photo and photo.filename:
        contents = await read_image_upload(photo)
        filename = f"rev_{secrets.token_hex(8)}.jpg"
        dest_path = os.path.join(UPLOAD_DIR, filename)
        with open(dest_path, "wb") as buffer:
            buffer.write(contents)
        photo_url = f"/static/uploads/{filename}"

    created_dt = datetime.datetime.utcnow()
    if custom_date.strip():
        try:
            clean_d = custom_date.strip().replace("T", " ")
            if len(clean_d) == 10:
                clean_d += " 12:00:00"
            elif len(clean_d) == 16:
                clean_d += ":00"
            created_dt = datetime.datetime.fromisoformat(clean_d)
        except Exception:
            pass

    new_rev = ReviewShowcase(
        username=username.strip(),
        orders_count=orders_count,
        rating=rating,
        photo_url=photo_url,
        review_text=review_text.strip() or "Отличный сервис!",
        likes_count=18,
        created_at=created_dt
    )
    db.add(new_rev)
    db.commit()
    return JSONResponse({"status": "ok", "message": "Отзыв успешно добавлен на витрину"})

@app.post("/admin/reviews/update-date")
async def update_showcase_review_date(
    review_id: int = Form(...),
    new_date: str = Form(...),
    auth_role: str = Depends(get_current_admin_role),
    db: Session = Depends(get_db)
):
    if auth_role == "demo":
        return JSONResponse({"status": "ok", "message": "Дата изменена в демо"})
    rev = db.query(ReviewShowcase).filter(ReviewShowcase.id == review_id).first()
    if rev and new_date.strip():
        try:
            clean_d = new_date.strip().replace("T", " ")
            if len(clean_d) == 10: clean_d += " 12:00:00"
            elif len(clean_d) == 16: clean_d += ":00"
            rev.created_at = datetime.datetime.fromisoformat(clean_d)
            db.commit()
            return JSONResponse({"status": "ok", "message": "Дата обновлена"})
        except Exception:
            return JSONResponse({"status": "error", "message": "Неверный формат даты"}, status_code=400)
    return JSONResponse({"status": "error"}, status_code=404)

@app.post("/admin/reviews/delete")
async def delete_showcase_review(
    review_id: int = Form(...),
    auth_role: str = Depends(get_current_admin_role),
    db: Session = Depends(get_db)
):
    if auth_role == "demo":
        return JSONResponse({"status": "ok", "message": "Удаление отзыва в демо"})
    rev = db.query(ReviewShowcase).filter(ReviewShowcase.id == review_id).first()
    if rev:
        db.delete(rev)
        db.commit()
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "error"}, status_code=404)

# --- БЭКАПЫ ---
@app.get("/admin/backup/download")
async def download_backup(auth_role: str = Depends(get_current_admin_role)):
    if auth_role == "demo":
        raise HTTPException(status_code=403, detail="Функция недоступна в демо-режиме")
    db_path = "database.db"
    if os.path.exists(db_path):
        filename = f"backup_ppu_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        return FileResponse(db_path, filename=filename, media_type="application/octet-stream")
    raise HTTPException(status_code=404, detail="Файл базы данных не найден")

@app.post("/admin/backup/restore")
async def restore_backup(backup_file: UploadFile = File(...), auth_role: str = Depends(get_current_admin_role)):
    if auth_role == "demo":
        raise HTTPException(status_code=403, detail="Функция недоступна в демо-режиме")
    contents = await backup_file.read(100 * 1024 * 1024 + 1)
    if len(contents) > 100 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Размер базы не должен превышать 100 МБ")
    if not contents.startswith(b"SQLite format 3\x00"):
        raise HTTPException(status_code=400, detail="Файл не является базой SQLite")
    temp_path = "database.db.restore-tmp"
    try:
        with open(temp_path, "wb") as f:
            f.write(contents)
        with closing(sqlite3.connect(temp_path)) as candidate:
            check = candidate.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise HTTPException(status_code=400, detail="База данных повреждена")
        auto_backup_database()
        os.replace(temp_path, "database.db")
        engine.dispose()
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return JSONResponse({"status": "ok", "message": "База данных успешно восстановлена"})

# --- РАССЫЛКА ---
@app.post("/admin/broadcast")
async def broadcast_message(broadcast_text: str = Form(...), auth_role: str = Depends(get_current_admin_role), db: Session = Depends(get_db)):
    if auth_role == "demo":
        raise HTTPException(status_code=403, detail="Рассылка сообщений отключена в демо-аккаунте")
    users = db.query(User).all()
    success_count, fail_count = 0, 0
    for u in users:
        if u.telegram_id:
            ok = send_telegram_message(u.telegram_id, broadcast_text.strip())
            if ok:
                success_count += 1
            else:
                fail_count += 1
    return JSONResponse({"status": "ok", "message": f"Рассылка завершена. Доставлено: {success_count}, Ошибок: {fail_count}"})

# --- УПРАВЛЕНИЕ ЗАКАЗАМИ ---
@app.post("/admin/order/delete")
async def delete_order(order_id: int = Form(...), auth_role: str = Depends(get_current_admin_role), db: Session = Depends(get_db)):
    if auth_role == "demo":
        return JSONResponse({"status": "ok", "message": "Демо-заказ удален локально"})
    order = db.query(Order).filter(Order.id == order_id).first()
    if order:
        db.delete(order)
        db.commit()
        return JSONResponse({"status": "ok"})
    return JSONResponse({"status": "error", "message": "Заказ не найден"}, status_code=404)

@app.post("/admin/order/delete-batch")
async def delete_batch_orders(order_ids: str = Form(...), auth_role: str = Depends(get_current_admin_role), db: Session = Depends(get_db)):
    if auth_role == "demo":
        return JSONResponse({"status": "ok", "message": "Демо-заказы удалены локально"})
    try:
        ids = json.loads(order_ids)
        db.query(Order).filter(Order.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        return JSONResponse({"status": "ok"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)

STATUS_NOTIFICATIONS = {
    "Оплачен": "✅ <b>Заказ #{id} успешно оплачен!</b>\nТовары отправлены на выкуп нашими байерами.",
    "Выкуплен": "🛍 <b>Заказ #{id} успешно выкуплен!</b>\nПродавец готовит посылку к отправке на наш склад в Китае.",
    "На складе в Китае": "🇨🇳 <b>Заказ #{id} прибыл на склад в Китае!</b>\nТовары проходят проверку на оригинальность и взвешивание.",
    "На таможне в России": "🛃 <b>Заказ #{id} проходит таможенный контроль в РФ.</b>\nСкоро посылка будет передана в службу СДЭК.",
    "СДЭК": "🚚 <b>Заказ #{id} передан в доставку СДЭК!</b>\nВы можете отслеживать перемещение посылки по трек-номеру.",
    "Выдан": "🎉 <b>Заказ #{id} успешно вручен!</b>\nСпасибо, что выбрали PoizonPickUP!",
    "Отменен": "❌ <b>Заказ #{id} был отменен.</b>\nЕсли у вас возникли вопросы, свяжитесь с поддержкой."
}

@app.post("/admin/order/update-status")
async def admin_update_order_status(
    order_id: int = Form(...), status: str = Form(...), auth_role: str = Depends(get_current_admin_role), db: Session = Depends(get_db)
):
    if auth_role == "demo":
        return JSONResponse({"status": "ok", "message": "Статус изменен в демо-режиме"})
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        return JSONResponse({"status": "error"})
    old_status = order.status
    
    if status == "Оплачен" and old_status != "Оплачен":
        process_order_paid(order, db)
        return JSONResponse({"status": "ok"})

    order.status = status
    if order.user_id:
        db.flush()
        user = db.query(User).filter(User.id == order.user_id).first()
        settings = db.query(SystemSettings).first()
        if user and settings:
            paid_count = db.query(Order).filter(
                Order.telegram_id == user.telegram_id,
                Order.status.in_(["Оплачен", "Выкуплен", "Выдан"]),
            ).count()
            user.client_status = "Постоянный" if paid_count >= settings.regular_threshold else "Новый"
    
    if status == "Выдан" and old_status != "Выдан":
        order.delivered_at = datetime.datetime.utcnow()
        if order.user_id:
            user = db.query(User).filter(User.id == order.user_id).first()
            if user:
                user.total_spent = (user.total_spent or 0.0) + (order.total_rub or 0.0)

        kbd = {
            "inline_keyboard": [[
                {"text": "1 ⭐", "callback_data": f"rate:{order.id}:1"},
                {"text": "2 ⭐", "callback_data": f"rate:{order.id}:2"},
                {"text": "3 ⭐", "callback_data": f"rate:{order.id}:3"},
                {"text": "4 ⭐", "callback_data": f"rate:{order.id}:4"},
                {"text": "5 ⭐", "callback_data": f"rate:{order.id}:5"}
            ]]
        }
        send_telegram_message(
            order.telegram_id,
            f"🎉 <b>Заказ #{order.id} успешно вручен!</b>\n\nПожалуйста, оцените качество выкупа и скорость доставки сервиса PoizonPickUP кнопками ниже:",
            reply_markup=kbd
        )
    elif status != old_status and status in STATUS_NOTIFICATIONS:
        msg_template = STATUS_NOTIFICATIONS[status]
        send_telegram_message(order.telegram_id, msg_template.format(id=order.id))

    db.commit()
    return JSONResponse({"status": "ok"})

# --- УПРАВЛЕНИЕ КЛИЕНТАМИ ---
@app.post("/admin/user/manual-create")
async def admin_manual_create_user(
    telegram_id: int = Form(...), username: str = Form(""), client_status: str = Form("Постоянный"),
    orders_count: int = Form(0), total_spent: float = Form(0.0), auth_role: str = Depends(get_current_admin_role), db: Session = Depends(get_db)
):
    if auth_role == "demo":
        return JSONResponse({"status": "ok", "message": "Клиент добавлен в демо-список"})
    u = User(
        telegram_id=telegram_id, username=username.replace("@", "").strip(),
        client_status=client_status, orders_count=orders_count, total_spent=total_spent
    )
    db.add(u)
    db.commit()
    return JSONResponse({"status": "ok"})

@app.post("/admin/user/update-full")
async def admin_update_user_full(
    user_id: int = Form(...), username: str = Form(""), client_status: str = Form(...),
    orders_count: int = Form(0), total_spent: float = Form(...), pass_tier: str = Form("None"),
    pass_slots: int = Form(0), auth_role: str = Depends(get_current_admin_role), db: Session = Depends(get_db)
):
    if auth_role == "demo":
        return JSONResponse({"status": "ok", "message": "Демо-клиент обновлен"})
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Клиент не найден")

    user.username = username.replace("@", "").strip()
    user.client_status = client_status
    user.orders_count = orders_count
    user.total_spent = total_spent

    tier_defaults = {"Bronze": (3, 30), "Silver": (5, 45), "Gold": (8, 60), "Platinum": (12, 90)}
    if pass_tier in tier_defaults:
        def_slots, def_days = tier_defaults[pass_tier]
        user.pass_tier = pass_tier
        user.pass_slots = pass_slots if pass_slots > 0 else def_slots
        user.pass_expires_at = datetime.datetime.utcnow() + datetime.timedelta(days=def_days)
    elif pass_tier == "Reset":
        user.pass_tier = None
        user.pass_slots = 0
        user.pass_expires_at = None

    db.commit()
    return JSONResponse({"status": "ok"})

# --- НАСТРОЙКИ СЕРВИСА ---
@app.post("/admin/settings/save")
async def admin_save_settings(
    cny_rate: float = Form(...), buffer_rate: float = Form(...), regular_threshold: int = Form(...),
    fee_new_shoes_percent: float = Form(...), fee_new_shoes_min: float = Form(...),
    fee_new_clothes_percent: float = Form(...), fee_new_clothes_min: float = Form(...),
    fee_regular_shoes_percent: float = Form(...), fee_regular_shoes_min: float = Form(...),
    fee_regular_clothes_percent: float = Form(...), fee_regular_clothes_min: float = Form(...),
    pass_price_bronze: float = Form(...), pass_price_silver: float = Form(...),
    pass_price_gold: float = Form(...), pass_price_platinum: float = Form(...),
    welcome_bonus_enabled: str = Form("false"), welcome_bonus_rub: float = Form(...),
    welcome_bonus_hours: int = Form(...), custom_delivery_days: int = Form(0),
    auth_role: str = Depends(get_current_admin_role), db: Session = Depends(get_db)
):
    if auth_role == "demo":
        return JSONResponse({"status": "ok", "message": "В демо-режиме настройки изолированы"})

    numeric_values = [
        cny_rate, buffer_rate, fee_new_shoes_percent, fee_new_shoes_min,
        fee_new_clothes_percent, fee_new_clothes_min, fee_regular_shoes_percent,
        fee_regular_shoes_min, fee_regular_clothes_percent, fee_regular_clothes_min,
        pass_price_bronze, pass_price_silver, pass_price_gold, pass_price_platinum,
        welcome_bonus_rub,
    ]
    if any(not math.isfinite(value) for value in numeric_values):
        raise HTTPException(status_code=422, detail="Все суммы и проценты должны быть конечными числами")
    if not 0.01 <= cny_rate <= 100 or not 0 <= buffer_rate <= 10 or not 1 <= regular_threshold <= 100:
        raise HTTPException(status_code=422, detail="Проверьте курс, буфер и порог тарифа")
    if any(not 0 <= value <= 100 for value in (fee_new_shoes_percent, fee_new_clothes_percent, fee_regular_shoes_percent, fee_regular_clothes_percent)):
        raise HTTPException(status_code=422, detail="Процент комиссии должен быть от 0 до 100")
    if any(not 0 <= value <= 1_000_000 for value in (fee_new_shoes_min, fee_new_clothes_min, fee_regular_shoes_min, fee_regular_clothes_min)):
        raise HTTPException(status_code=422, detail="Минимальная комиссия вне допустимого диапазона")

    s = db.query(SystemSettings).first()
    if s:
        s.cny_rate, s.buffer_rate, s.regular_threshold = cny_rate, buffer_rate, regular_threshold
        s.fee_new_shoes_percent, s.fee_new_shoes_min = fee_new_shoes_percent, fee_new_shoes_min
        s.fee_new_clothes_percent, s.fee_new_clothes_min = fee_new_clothes_percent, fee_new_clothes_min
        s.fee_regular_shoes_percent, s.fee_regular_shoes_min = fee_regular_shoes_percent, fee_regular_shoes_min
        s.fee_regular_clothes_percent, s.fee_regular_clothes_min = fee_regular_clothes_percent, fee_regular_clothes_min
        s.pass_price_bronze, s.pass_price_silver = pass_price_bronze, pass_price_silver
        s.pass_price_gold, s.pass_price_platinum = pass_price_gold, pass_price_platinum
        s.welcome_bonus_enabled = (welcome_bonus_enabled == "true")
        s.welcome_bonus_rub, s.welcome_bonus_hours = welcome_bonus_rub, welcome_bonus_hours
        s.custom_delivery_days = custom_delivery_days
        db.commit()
    if PICKUP_SETTINGS_URL:
        if not BOT_SETTINGS_TOKEN:
            return JSONResponse({"status": "error", "message": "Настройки сохранены здесь, но синхронизация Mini App не настроена: отсутствует BOT_SETTINGS_TOKEN"}, status_code=503)
        sync_payload = {
            "pronto_rate": cny_rate,
            "buffer_rub": buffer_rate,
            "shoes_new_percent": fee_new_shoes_percent,
            "shoes_new_min_rub": fee_new_shoes_min,
            "shoes_regular_percent": fee_regular_shoes_percent,
            "shoes_regular_min_rub": fee_regular_shoes_min,
            "clothes_new_percent": fee_new_clothes_percent,
            "clothes_new_min_rub": fee_new_clothes_min,
            "clothes_regular_percent": fee_regular_clothes_percent,
            "clothes_regular_min_rub": fee_regular_clothes_min,
            "regular_after_paid_orders": regular_threshold,
        }
        try:
            response = requests.put(
                PICKUP_SETTINGS_URL,
                json=sync_payload,
                headers={"X-Bot-Settings-Token": BOT_SETTINGS_TOKEN},
                timeout=5,
            )
            response.raise_for_status()
        except requests.RequestException:
            return JSONResponse({"status": "error", "message": "Настройки сохранены в менеджерской панели, но не удалось передать их в активную Mini App"}, status_code=502)
    return JSONResponse({"status": "ok", "synced": bool(PICKUP_SETTINGS_URL)})

@app.post("/admin/promo/create")
async def create_promo(
    code: str = Form(...), discount_rub: float = Form(...), days: int = Form(30),
    auth_role: str = Depends(get_current_admin_role), db: Session = Depends(get_db)
):
    if auth_role == "demo":
        return JSONResponse({"status": "ok", "message": "Промокод добавлен в демо-список"})
    clean_code = code.strip().upper()
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(days=days)
    new_p = PromoCode(code=clean_code, discount_rub=discount_rub, expires_at=expires_at)
    db.add(new_p)
    db.commit()
    return JSONResponse({"status": "ok"})

@app.post("/admin/order/set-delivery")
async def admin_set_delivery(
    order_id: int = Form(...), delivery_rub: float = Form(...),
    auth_role: str = Depends(get_current_admin_role), db: Session = Depends(get_db)
):
    if auth_role == "demo":
        return JSONResponse({"status": "ok", "message": "Доставка рассчитана в демо"})
    order = db.query(Order).filter(Order.id == order_id).first()
    if order:
        order.delivery_rub = delivery_rub
        db.commit()
        pay_url = f"https://t.me/{ADMIN_USERNAME}?text=Оплата_доставки_заказ_{order.id}_сумма_{delivery_rub}руб"
        kbd = {"inline_keyboard": [[{"text": f"💳 Оплатить доставку ({delivery_rub} ₽)", "url": pay_url}]]}
        send_telegram_message(
            order.telegram_id, 
            f"⚖️ <b>Посылка по заказу #{order.id} взвешена на складе в Китае!</b>\n\n"
            f"Стоимость международной доставки: <b>{delivery_rub} ₽</b>.\n"
            f"Для отправки в РФ, пожалуйста, оплатите доставку менеджеру @{ADMIN_USERNAME}:",
            reply_markup=kbd
        )
    return JSONResponse({"status": "ok"})

@app.post("/admin/order/set-track")
async def admin_set_track(
    order_id: int = Form(...), track: str = Form(...),
    auth_role: str = Depends(get_current_admin_role), db: Session = Depends(get_db)
):
    if auth_role == "demo":
        return JSONResponse({"status": "ok", "message": "Трек сохранен в демо"})
    order = db.query(Order).filter(Order.id == order_id).first()
    if order:
        order.cdek_track = track.strip()
        order.status = "СДЭК"
        db.commit()
        send_telegram_message(order.telegram_id, f"🚚 Посылка по заказу #{order.id} отправлена СДЭК!\nТрек-номер: <code>{track.strip()}</code>\nОтслеживание: https://www.cdek.ru/ru/tracking?order_id={track.strip()}")
    return JSONResponse({"status": "ok"})
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8001, reload=False)
