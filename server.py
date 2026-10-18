from dotenv import load_dotenv
load_dotenv()

import os
import re
import csv
import io
import uuid
import asyncio
import logging
import ipaddress
from datetime import datetime, timezone, timedelta
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlparse

import bcrypt
import jwt
import httpx
from fastapi import FastAPI, APIRouter, HTTPException, Request, Depends, UploadFile, File
from fastapi.responses import StreamingResponse, Response
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field
from typing import List, Optional

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

app = FastAPI()
api_router = APIRouter(prefix="/api")

JWT_ALGORITHM = "HS256"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ---------- Email (Emergent managed Resend) ----------

EMAIL_BASE_URL = "https://integrations.emergentagent.com"
EMAIL_KEY = os.environ.get("EMERGENT_EMAIL_KEY", "")
EMAIL_FROM_NAME = os.environ.get("EMAIL_FROM_NAME", "Ravan Riders Vizag")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "")

_SHORTENERS = ("bit.ly", "tinyurl.com", "t.co", "is.gd", "cutt.ly", "goo.gl", "rebrand.ly")
_CRED_ASK = ("reply with your password", "reply with the code", "send your password", "cvv",
             "send us your password", "enter your password below", "confirm your card number",
             "your full card number", "seed phrase", "recovery phrase", "verify your card",
             "social security number", "confirm your bank details")
_HOSTISH = re.compile(r"\b(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})", re.I)

def _host_ok(host: str) -> bool:
    if not host or "xn--" in host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return not any(host == s or host.endswith("." + s) for s in _SHORTENERS)

def _same_site(shown: str, real: str) -> bool:
    return shown == real or real.endswith("." + shown) or shown.endswith("." + real)

class _EmailScan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.urls, self.anchors = set(), [], []
        self._href, self._text = None, []
    def handle_starttag(self, tag, attrs):
        self.tags.add(tag.lower())
        self.urls += [v for k, v in attrs if k.lower() in ("href", "src") and v]
        if tag.lower() == "a":
            self._href = dict((k.lower(), v) for k, v in attrs).get("href")
            self._text = []
    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)
    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.anchors.append((self._href, "".join(self._text)))
            self._href, self._text = None, []

def _assert_safe_email(subject: str, html: str) -> None:
    scan = _EmailScan(); scan.feed(html)
    if scan.tags & {"form", "input", "textarea", "select"}:
        raise ValueError("No forms or input fields in email (G2)")
    body = f"{subject}\n{html}".lower()
    for p in _CRED_ASK:
        if p in body:
            raise ValueError(f"Email asks the recipient for credentials: {p!r} (G2)")
    for url in scan.urls:
        low = url.strip().lower()
        if low.startswith(("mailto:", "tel:", "cid:", "#")):
            continue
        if not low.startswith("https://"):
            raise ValueError(f"Email links/assets must be absolute https: {url!r} (G3)")
        host = urlparse(low).hostname or ""
        if not _host_ok(host) or urlparse(low).username is not None:
            raise ValueError(f"Shortened, numeric-host or credential-bearing URL: {url!r} (G3)")
    for href, text in scan.anchors:
        real = urlparse(href.strip().lower()).hostname or ""
        if not real:
            continue
        for m in _HOSTISH.finditer(text):
            if not _same_site(m.group(1).lower(), real):
                raise ValueError(f"Anchor text {m.group(1)!r} != real link host {real!r} (G3)")

async def send_email(*, to: str, subject: str, html: str) -> Optional[str]:
    _assert_safe_email(subject, html)
    payload = {"to": [to], "subject": subject, "html": html, "from_name": EMAIL_FROM_NAME}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{EMAIL_BASE_URL}/api/v1/email/send",
            headers={"X-Email-Key": EMAIL_KEY},
            json=payload,
        )
    resp.raise_for_status()
    return resp.json().get("id")

async def notify_owner(booking: dict):
    if not EMAIL_KEY or not OWNER_EMAIL:
        logger.info("Owner email not configured; skipping booking alert")
        return
    phone_digits = "".join(c for c in booking["phone"] if c.isdigit())[-10:]
    rows = "".join([
        f'<tr><td style="padding:8px 12px;color:#888;font-size:13px">Name</td><td style="padding:8px 12px;font-size:13px"><strong>{escape(booking["name"])}</strong></td></tr>',
        f'<tr><td style="padding:8px 12px;color:#888;font-size:13px">Phone</td><td style="padding:8px 12px;font-size:13px"><a href="tel:+91{phone_digits}">{escape(booking["phone"])}</a></td></tr>',
        f'<tr><td style="padding:8px 12px;color:#888;font-size:13px">Vehicle</td><td style="padding:8px 12px;font-size:13px"><strong>{escape(booking["vehicle_name"])}</strong></td></tr>',
        f'<tr><td style="padding:8px 12px;color:#888;font-size:13px">Pickup</td><td style="padding:8px 12px;font-size:13px">{escape(booking["pickup_date"])} at {escape(booking.get("pickup_time", ""))}</td></tr>',
        f'<tr><td style="padding:8px 12px;color:#888;font-size:13px">Return</td><td style="padding:8px 12px;font-size:13px">{escape(booking["return_date"])} at {escape(booking.get("return_time", ""))}</td></tr>',
        f'<tr><td style="padding:8px 12px;color:#888;font-size:13px">Notes</td><td style="padding:8px 12px;font-size:13px">{escape(booking.get("notes", "") or "—")}</td></tr>',
    ])
    subject = f'New Booking: {booking["vehicle_name"]} ({booking["pickup_date"]})'
    html = (
        '<table role="presentation" width="100%" style="background:#f5f5f5;padding:24px"><tr><td>'
        '<table role="presentation" width="100%" style="max-width:520px;margin:0 auto;background:#ffffff;border-radius:8px;padding:24px;font-family:Arial,sans-serif">'
        f'<tr><td><h2 style="margin:0 0 4px;color:#E60000;font-family:Arial,sans-serif">New Booking Request</h2>'
        '<p style="margin:0 0 16px;color:#666;font-size:13px">Ravan Riders Vizag website</p>'
        f'<table role="presentation" width="100%" style="border-top:1px solid #eee">{rows}</table>'
        '<p style="font-size:12px;color:#888;margin-top:20px">Call the customer to confirm. Manage this booking in your owner dashboard.</p>'
        f'<p style="font-size:11px;color:#aaa">Sent by {escape(EMAIL_FROM_NAME)} booking system.</p>'
        '</td></tr></table></td></tr></table>'
    )
    try:
        email_id = await send_email(to=OWNER_EMAIL, subject=subject, html=html)
        logger.info(f"Owner booking alert sent: {email_id}")
    except Exception as e:
        logger.warning(f"Owner booking alert failed: {e}")


# ---------- WhatsApp owner alert (Twilio) ----------

TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WA_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")
OWNER_WHATSAPP = os.environ.get("OWNER_WHATSAPP", "")

async def notify_owner_whatsapp(booking: dict):
    if not (TWILIO_SID and TWILIO_TOKEN and TWILIO_WA_FROM and OWNER_WHATSAPP):
        logger.info("Twilio WhatsApp not configured; skipping WhatsApp alert")
        return
    body = (
        "New booking request - Ravan Riders\n\n"
        f"Customer: {booking['name']} ({booking['phone']})\n"
        f"Vehicle: {booking['vehicle_name']}\n"
        f"Pickup: {booking['pickup_date']} at {booking.get('pickup_time', '')}\n"
        f"Return: {booking['return_date']} at {booking.get('return_time', '')}\n"
        f"Notes: {booking.get('notes') or '-'}\n\n"
        "Open your owner dashboard to CONFIRM or CANCEL. Confirming marks the vehicle BOOKED on the website."
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
                auth=(TWILIO_SID, TWILIO_TOKEN),
                data={"From": f"whatsapp:{TWILIO_WA_FROM}", "To": f"whatsapp:{OWNER_WHATSAPP}", "Body": body},
            )
        if resp.status_code >= 300:
            logger.warning(f"WhatsApp alert failed: {resp.status_code} {resp.text[:200]}")
        else:
            logger.info("Owner WhatsApp alert sent")
    except Exception as e:
        logger.warning(f"WhatsApp alert error: {e}")


# ---------- WhatsApp owner alert (CallMeBot — free personal alerts) ----------

CALLMEBOT_PHONE = os.environ.get("CALLMEBOT_PHONE", "")
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY", "")

async def notify_owner_callmebot(booking: dict) -> bool:
    if not (CALLMEBOT_PHONE and CALLMEBOT_APIKEY):
        logger.info("CallMeBot not configured; skipping WhatsApp alert")
        return False
    body = (
        "New booking - Ravan Riders\n\n"
        f"Customer: {booking['name']} ({booking['phone']})\n"
        f"Vehicle: {booking['vehicle_name']}\n"
        f"Pickup: {booking['pickup_date']} at {booking.get('pickup_time', '')}\n"
        f"Return: {booking['return_date']} at {booking.get('return_time', '')}\n"
        f"Notes: {booking.get('notes') or '-'}\n\n"
        "Open your owner dashboard to CONFIRM or CANCEL. Confirming marks the vehicle BOOKED on the website."
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.callmebot.com/whatsapp.php",
                params={"phone": CALLMEBOT_PHONE, "text": body, "apikey": CALLMEBOT_APIKEY},
            )
        if resp.status_code == 200:
            logger.info("CallMeBot WhatsApp alert sent")
            return True
        logger.warning(f"CallMeBot returned HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"CallMeBot alert error: {e}")
    return False


# ---------- Photo storage (MongoDB — portable across hosts) ----------

APP_NAME = "ravan-riders"
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


# ---------- Auth ----------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))

def get_jwt_secret() -> str:
    return os.environ["JWT_SECRET"]

def create_access_token(user_id: str, email: str) -> str:
    payload = {"sub": user_id, "email": email, "exp": datetime.now(timezone.utc) + timedelta(hours=12), "type": "access"}
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)

async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = await db.users.find_one({"_id": payload["sub"]})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return {"id": user["_id"], "email": user["email"], "name": user.get("name", "Owner"), "role": user.get("role", "admin")}


class LoginInput(BaseModel):
    email: str
    password: str


@api_router.post("/auth/login")
async def login(input: LoginInput, request: Request):
    email = input.email.strip().lower()
    identifier = f"{request.client.host}:{email}"

    attempt = await db.login_attempts.find_one({"identifier": identifier})
    if attempt and attempt.get("count", 0) >= 5:
        locked_since = attempt.get("updated_at")
        if locked_since and datetime.now(timezone.utc) - locked_since < timedelta(minutes=15):
            raise HTTPException(status_code=429, detail="Too many failed attempts. Try again in 15 minutes.")

    user = await db.users.find_one({"email": email})
    if not user or not verify_password(input.password, user["password_hash"]):
        await db.login_attempts.update_one(
            {"identifier": identifier},
            {"$inc": {"count": 1}, "$set": {"updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        raise HTTPException(status_code=401, detail="Invalid email or password")

    await db.login_attempts.delete_one({"identifier": identifier})
    token = create_access_token(user["_id"], email)
    return {"token": token, "user": {"id": user["_id"], "email": email, "name": user.get("name", "Owner"), "role": user.get("role", "admin")}}


@api_router.get("/auth/me")
async def auth_me(user=Depends(get_current_user)):
    return user


@api_router.post("/admin/upload")
async def upload_photo(file: UploadFile = File(...), user=Depends(get_current_user)):
    content_type = file.content_type or ""
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Only JPG, PNG, WEBP or GIF images allowed")
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image too large (max 10 MB)")
    ext = (file.filename.rsplit(".", 1)[-1] if "." in file.filename else "jpg").lower()
    if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
        ext = "jpg"
    path = f"{APP_NAME}/uploads/fleet/{uuid.uuid4()}.{ext}"
    await db.files.insert_one({
        "id": str(uuid.uuid4()),
        "storage_path": path,
        "data": data,
        "original_filename": file.filename,
        "content_type": content_type,
        "size": len(data),
        "is_deleted": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"path": path}

@api_router.get("/files/{path:path}")
async def serve_file(path: str):
    record = await db.files.find_one({"storage_path": path, "is_deleted": False})
    if not record or record.get("data") is None:
        raise HTTPException(status_code=404, detail="File not found")
    return Response(content=bytes(record["data"]), media_type=record.get("content_type", "application/octet-stream"))


# ---------- Fleet ----------

class Vehicle(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    type: str  # "bike" | "car"
    category: str
    detail: str = ""
    image: str
    daily_rate: int
    weekly_rate: int
    monthly_rate: int
    badge: str = ""
    specs: List[str] = []
    available: bool = True

class VehicleInput(BaseModel):
    name: str
    type: str
    category: str
    detail: str = ""
    image: str
    daily_rate: int
    weekly_rate: int
    monthly_rate: int
    badge: str = ""
    specs: List[str] = []
    available: bool = True


@api_router.get("/fleet")
async def get_fleet():
    return await db.fleet.find({}, {"_id": 0}).to_list(200)

@api_router.post("/admin/fleet")
async def create_vehicle(input: VehicleInput, user=Depends(get_current_user)):
    vehicle = Vehicle(**input.model_dump())
    await db.fleet.insert_one(vehicle.model_dump())
    return vehicle

@api_router.put("/admin/fleet/{vehicle_id}")
async def update_vehicle(vehicle_id: str, input: VehicleInput, user=Depends(get_current_user)):
    result = await db.fleet.update_one({"id": vehicle_id}, {"$set": input.model_dump()})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return await db.fleet.find_one({"id": vehicle_id}, {"_id": 0})

@api_router.delete("/admin/fleet/{vehicle_id}")
async def delete_vehicle(vehicle_id: str, user=Depends(get_current_user)):
    result = await db.fleet.delete_one({"id": vehicle_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    return {"deleted": True}


# ---------- Slot availability ----------

ACTIVE_STATUSES = ("contacted", "confirmed")  # confirmed bookings block the website
ALL_STATUSES = ("new", "confirmed", "contacted", "closed", "cancelled")

def parse_slot_dt(date_str: str, time_str: Optional[str]) -> datetime:
    try:
        return datetime.fromisoformat(f"{date_str}T{(time_str or '10:00')}:00")
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid date or time slot")

def ranges_overlap(p1, r1, p2, r2) -> bool:
    return p1 < r2 and r1 > p2

def booking_expired(b: dict) -> bool:
    try:
        return parse_slot_dt(b["return_date"], b.get("return_time")) <= datetime.now()
    except HTTPException:
        return False

async def find_conflict(vehicle_id: str, p: datetime, r: datetime, exclude_id: Optional[str] = None):
    query = {"vehicle_id": vehicle_id, "status": {"$in": list(ACTIVE_STATUSES)}}
    if exclude_id:
        query["id"] = {"$ne": exclude_id}
    existing = await db.bookings.find(query, {"_id": 0}).to_list(200)
    for b in existing:
        if booking_expired(b):
            continue
        bp = parse_slot_dt(b["pickup_date"], b.get("pickup_time"))
        br = parse_slot_dt(b["return_date"], b.get("return_time"))
        if ranges_overlap(p, r, bp, br):
            return b
    return None

@api_router.get("/fleet/availability")
async def fleet_availability(pickup_date: str, return_date: str, pickup_time: str = "10:00", return_time: str = "10:00"):
    p = parse_slot_dt(pickup_date, pickup_time)
    r = parse_slot_dt(return_date, return_time)
    if r <= p:
        raise HTTPException(status_code=400, detail="Return slot must be after pickup slot")
    active = await db.bookings.find({"status": {"$in": list(ACTIVE_STATUSES)}}, {"_id": 0}).to_list(1000)
    conflicts = {}
    for b in active:
        if not b.get("vehicle_id") or booking_expired(b):
            continue
        bp = parse_slot_dt(b["pickup_date"], b.get("pickup_time"))
        br = parse_slot_dt(b["return_date"], b.get("return_time"))
        if ranges_overlap(p, r, bp, br):
            conflicts[b["vehicle_id"]] = conflicts.get(b["vehicle_id"], 0) + 1
    return {"conflicts": conflicts}


# ---------- Bookings ----------

class BookingInput(BaseModel):
    name: str
    phone: str
    vehicle_id: Optional[str] = None
    vehicle_name: str
    pickup_date: str
    return_date: str
    pickup_time: str = "10:00"
    return_time: str = "10:00"
    notes: str = ""
    price: Optional[int] = None
    advance: Optional[int] = None

def validate_slot_range(input) -> tuple:
    p = parse_slot_dt(input.pickup_date, input.pickup_time)
    r = parse_slot_dt(input.return_date, input.return_time)
    if r <= p:
        raise HTTPException(status_code=400, detail="Return slot must be after pickup slot")
    return p, r

CONFLICT_MSG = "Sorry — this vehicle just got booked for those slots. Please pick different dates or another vehicle."

@api_router.post("/bookings")
async def create_booking(input: BookingInput):
    p, r = validate_slot_range(input)
    if input.vehicle_id and await find_conflict(input.vehicle_id, p, r):
        raise HTTPException(status_code=409, detail=CONFLICT_MSG)
    booking = {
        "id": str(uuid.uuid4()),
        **input.model_dump(),
        "source": "online",
        "status": "new",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.bookings.insert_one(booking)
    booking.pop("_id", None)
    await notify_owner(booking)
    sent = await notify_owner_callmebot(booking)
    if not sent:
        await notify_owner_whatsapp(booking)
    return booking

@api_router.get("/admin/bookings")
async def get_bookings(user=Depends(get_current_user)):
    return await db.bookings.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)

@api_router.post("/admin/bookings")
async def create_offline_booking(input: BookingInput, user=Depends(get_current_user)):
    p, r = validate_slot_range(input)
    if input.vehicle_id and await find_conflict(input.vehicle_id, p, r):
        raise HTTPException(status_code=409, detail="This vehicle is already booked for those slots.")
    booking = {
        "id": str(uuid.uuid4()),
        **input.model_dump(),
        "source": "offline",
        "status": "confirmed",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.bookings.insert_one(booking)
    booking.pop("_id", None)
    return booking

@api_router.put("/admin/bookings/{booking_id}")
async def edit_booking(booking_id: str, input: BookingInput, request: Request, user=Depends(get_current_user)):
    existing = await db.bookings.find_one({"id": booking_id})
    if not existing:
        raise HTTPException(status_code=404, detail="Booking not found")
    p, r = validate_slot_range(input)
    if input.vehicle_id and await find_conflict(input.vehicle_id, p, r, exclude_id=booking_id):
        raise HTTPException(status_code=409, detail="This vehicle is already booked for those slots.")
    await db.bookings.update_one({"id": booking_id}, {"$set": input.model_dump()})
    return await db.bookings.find_one({"id": booking_id}, {"_id": 0})

@api_router.patch("/admin/bookings/{booking_id}")
async def update_booking(booking_id: str, request: Request, user=Depends(get_current_user)):
    body = await request.json()
    update = {}
    if "status" in body:
        if body["status"] not in ALL_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid status")
        update["status"] = body["status"]
    for field in ("price", "advance"):
        if field in body:
            value = body[field]
            if value is not None and (not isinstance(value, int) or value < 0):
                raise HTTPException(status_code=400, detail=f"Invalid {field}")
            update[field] = value
    if not update:
        raise HTTPException(status_code=400, detail="Nothing to update")
    result = await db.bookings.update_one({"id": booking_id}, {"$set": update})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Booking not found")
    return await db.bookings.find_one({"id": booking_id}, {"_id": 0})

@api_router.get("/admin/bookings/export")
async def export_bookings(user=Depends(get_current_user)):
    rows = await db.bookings.find({}, {"_id": 0}).sort("created_at", -1).to_list(5000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Booking ID", "Source", "Status", "Customer", "Phone", "Vehicle",
                "Pickup Date", "Pickup Slot", "Return Date", "Return Slot",
                "Agreed Price (INR)", "Advance Paid (INR)", "Balance (INR)", "Notes", "Created At"])
    for b in rows:
        price = b.get("price") or 0
        advance = b.get("advance") or 0
        w.writerow([
            b["id"], b.get("source", "online"), b["status"], b["name"], b["phone"], b["vehicle_name"],
            b["pickup_date"], b.get("pickup_time", ""), b["return_date"], b.get("return_time", ""),
            price, advance, max(price - advance, 0), b.get("notes", ""), b.get("created_at", ""),
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ravan-riders-bookings.csv"},
    )


# ---------- Insights ----------

@api_router.get("/admin/insights")
async def get_insights(user=Depends(get_current_user)):
    bookings = await db.bookings.find({}, {"_id": 0}).to_list(2000)
    now = datetime.now()
    today = now.date()
    week_start = today - timedelta(days=today.weekday())  # Monday
    month_start = today.replace(day=1)

    earned = [b for b in bookings if b.get("status") != "cancelled" and b.get("price")]

    def pickup_day(b):
        try:
            return datetime.fromisoformat(b["pickup_date"]).date()
        except (ValueError, KeyError):
            return None

    rev_today = rev_week = rev_month = rev_all = 0
    for b in earned:
        d = pickup_day(b)
        if not d:
            continue
        rev_all += b["price"]
        if d == today:
            rev_today += b["price"]
        if d >= week_start:
            rev_week += b["price"]
        if d >= month_start:
            rev_month += b["price"]

    weekly = []
    for i in range(7, -1, -1):
        ws = week_start - timedelta(weeks=i)
        we = ws + timedelta(days=7)
        total = sum(b["price"] for b in earned if (d := pickup_day(b)) and ws <= d < we)
        weekly.append({"label": ws.strftime("%d %b"), "revenue": total})

    monthly = []
    y, m = today.year, today.month
    months = []
    for _ in range(6):
        months.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    months.reverse()
    for (yy, mm) in months:
        total = sum(b["price"] for b in earned if (d := pickup_day(b)) and d.year == yy and d.month == mm)
        monthly.append({"label": datetime(yy, mm, 1).strftime("%b"), "revenue": total})

    per_vehicle = {}
    for b in earned:
        key = b.get("vehicle_name", "Unknown")
        entry = per_vehicle.setdefault(key, {"name": key, "earnings": 0, "bookings": 0})
        entry["earnings"] += b["price"]
        entry["bookings"] += 1
    vehicles = sorted(per_vehicle.values(), key=lambda x: x["earnings"], reverse=True)

    advance_collected = sum((b.get("advance") or 0) for b in bookings if b.get("status") != "cancelled")
    balance_pending = sum(max((b.get("price") or 0) - (b.get("advance") or 0), 0)
                          for b in bookings if b.get("status") not in ("cancelled", "closed") and b.get("price"))

    active_now = upcoming = 0
    due_today = []
    for b in bookings:
        if b.get("status") not in ACTIVE_STATUSES:
            continue
        try:
            p = parse_slot_dt(b["pickup_date"], b.get("pickup_time"))
            r = parse_slot_dt(b["return_date"], b.get("return_time"))
        except HTTPException:
            continue
        if p <= now <= r:
            active_now += 1
        elif p > now:
            upcoming += 1
        if b.get("return_date") == today.isoformat():
            due_today.append({
                "id": b["id"], "name": b["name"], "phone": b["phone"],
                "vehicle_name": b["vehicle_name"], "return_time": b.get("return_time", "10:00"),
            })

    return {
        "today": rev_today,
        "week": rev_week,
        "month": rev_month,
        "all_time": rev_all,
        "advance_collected": advance_collected,
        "balance_pending": balance_pending,
        "weekly": weekly,
        "monthly": monthly,
        "vehicles": vehicles,
        "active_now": active_now,
        "upcoming": upcoming,
        "due_today": due_today,
    }


@api_router.get("/admin/alert-status")
async def alert_status(user=Depends(get_current_user)):
    return {
        "whatsapp_active": bool(CALLMEBOT_PHONE and CALLMEBOT_APIKEY),
        "whatsapp_number": CALLMEBOT_PHONE,
        "email_active": bool(OWNER_EMAIL),
    }


@api_router.get("/")
async def root():
    return {"message": "Ravan Riders API"}


# ---------- Startup seed ----------

SAMPLE_FLEET = [
    {"name": "Royal Enfield Hunter 350", "type": "bike", "category": "Cruiser / Sport", "detail": "349cc", "image": "https://images.unsplash.com/photo-1645818702734-f4e522a3982e?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NTY2NzZ8MHwxfHNlYXJjaHwxfHxzcG9ydCUyMG1vdG9yY3ljbGUlMjBkYXJrJTIwbW9vZHklMjBibGFjayUyMHJlZHxlbnwwfHx8fDE3OTE2MzY3MzJ8MA&ixlib=rb-4.1.0&q=85", "daily_rate": 800, "weekly_rate": 4900, "monthly_rate": 16000, "badge": "Popular Cruiser", "specs": ["Self Start", "Disc Brakes", "Helmets Included", "Unlimited KMs"]},
    {"name": "KTM Duke 250 / RC 200", "type": "bike", "category": "Sport Bike", "detail": "249cc", "image": "https://images.unsplash.com/photo-1591259966748-4e4b9916858f?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NTY2NzZ8MHwxfHNlYXJjaHw0fHxzcG9ydCUyMG1vdG9yY3ljbGUlMjBkYXJrJTIwbW9vZHklMjBibGFjayUyMHJlZHxlbnwwfHx8fDE3OTE2MzY3MzJ8MA&ixlib=rb-4.1.0&q=85", "daily_rate": 1100, "weekly_rate": 6500, "monthly_rate": 21000, "badge": "High Performance", "specs": ["ABS Brakes", "Liquid Cooled", "2 Helmet Set", "Zero Deposit"]},
    {"name": "Yamaha R15 V4 / MT-15", "type": "bike", "category": "Aggressive Sport", "detail": "155cc VVA", "image": "https://images.unsplash.com/photo-1517846693594-1567da72af75?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NTY2NzZ8MHwxfHNlYXJjaHwzfHxzcG9ydCUyMG1vdG9yY3ljbGUlMjBkYXJrJTIwbW9vZHklMjBibGFjayUyMHJlZHxlbnwwfHx8fDE3OTE2MzY3MzJ8MA&ixlib=rb-4.1.0&q=85", "daily_rate": 950, "weekly_rate": 5800, "monthly_rate": 18500, "badge": "Vizag Favorite", "specs": ["VVA Engine", "Quickshifter", "Slipper Clutch", "Unlimited KMs"]},
    {"name": "Honda Activa 6G / Jupiter", "type": "bike", "category": "Automatic Scooter", "detail": "110cc", "image": "https://images.unsplash.com/photo-1763305438248-90d79a2194bb?crop=entropy&cs=srgb&fm=jpg&ixid=M3w4NTYxODl8MHwxfHNlYXJjaHszfHxzY29vdGVyJTIwbW9wZWRzJTIwdXJiYW4lMjBkYXJrfGVufDB8fHx8MTc5MTYzNjczOXww&ixlib=rb-4.1.0&q=85", "daily_rate": 450, "weekly_rate": 2800, "monthly_rate": 9500, "badge": "Budget Friendly", "specs": ["Gearless Comfort", "Underseat Storage", "2 Helmets", "No Security Deposit"]},
    {"name": "Hyundai Creta SX / Tucson", "type": "car", "category": "Premium SUV", "detail": "5 Seats • Automatic", "image": "https://images.unsplash.com/photo-1720556405618-a47614fd663f?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NDQ2Mzl8MHwxfHNlYXJjaHw0fHxsdXh1cnklMjBzdXYlMjBjYXIlMjBkYXJrJTIwYmFja2dyb3VuZCUyMHJlZCUyMGFjY2VudHxlbnwwfHx8fDE3OTE2MzY3MzJ8MA&ixlib=rb-4.1.0&q=85", "daily_rate": 2800, "weekly_rate": 17500, "monthly_rate": 55000, "badge": "Luxury SUV", "specs": ["Sunroof", "Touchscreen Audio", "Unlimited KMs", "Clean Sanitized"]},
    {"name": "Mahindra Thar 4x4 Hardtop", "type": "car", "category": "Off-Road SUV", "detail": "4 Seats • Manual/Auto", "image": "https://images.unsplash.com/photo-1664012624750-cac9fbaad939?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NDQ2Mzl8MHwxfHNlYXJjaHwyfHxsdXh1cnklMjBzdXYlMjBjYXIlMjBkYXJrJTIwYmFja2dyb3VuZCUyMHJlZCUyMGFjY2VudHxlbnwwfHx8fDE3OTE2MzY3MzJ8MA&ixlib=rb-4.1.0&q=85", "daily_rate": 3500, "weekly_rate": 22000, "monthly_rate": 68000, "badge": "Araku Special", "specs": ["4x4 Low-Range", "High Clearance", "Hardtop", "No Deposit"]},
    {"name": "Executive Sedan (Verna/City)", "type": "car", "category": "Luxury Sedan", "detail": "5 Seats • Automatic", "image": "https://images.unsplash.com/photo-1775989122164-3fd6ba68a747?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NTY2NzZ8MHwxfHNlYXJjaHwyfHxtb2Rlcm4lMjBzZWRhbiUyMGNhciUyMGRhcmslMjByZWQlMjBmcm9udCUyMGhlYWRsaWdodHN8ZW58MHx8fHwxNzkxNjM2NzM5fDA&ixlib=rb-4.1.0&q=85", "daily_rate": 2500, "weekly_rate": 15500, "monthly_rate": 48000, "badge": "VIP Experience", "specs": ["Leather Interior", "Ambient Lighting", "Full Insurance", "Unlimited KMs"]},
    {"name": "Swift / Baleno Hatchback", "type": "car", "category": "City Hatchback", "detail": "5 Seats • Manual", "image": "https://images.unsplash.com/photo-1708738066978-2b1b9c19c7c5?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NDQ2Mzl8MHwxfHNlYXJjaHwzfHxsdXh1cnklMjBzdXYlMjBjYXIlMjBkYXJrJTIwYmFja2dyb3VuZCUyMHJlZCUyMGFjY2VudHxlbnwwfHx8fDE3OTE2MzY3MzJ8MA&ixlib=rb-4.1.0&q=85", "daily_rate": 1500, "weekly_rate": 9500, "monthly_rate": 32000, "badge": "Top Mileage", "specs": ["AC & Bluetooth", "Easy Parking", "24/7 Roadside Assist", "No Deposit"]},
]


@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)
    await db.login_attempts.create_index("identifier")

    admin_email = os.environ.get("ADMIN_EMAIL", "admin@example.com").lower()
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")
    existing = await db.users.find_one({"email": admin_email})
    if existing is None:
        await db.users.insert_one({
            "_id": str(uuid.uuid4()),
            "email": admin_email,
            "password_hash": hash_password(admin_password),
            "name": "Ravan Riders Owner",
            "role": "admin",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        logger.info("Admin account seeded")
    elif not verify_password(admin_password, existing["password_hash"]):
        await db.users.update_one({"email": admin_email}, {"$set": {"password_hash": hash_password(admin_password)}})
        logger.info("Admin password synced")

    if await db.fleet.count_documents({}) == 0:
        await db.fleet.insert_many([Vehicle(**v).model_dump() for v in SAMPLE_FLEET])
        logger.info("Sample fleet seeded")


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
