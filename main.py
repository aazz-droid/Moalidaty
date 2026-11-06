from fastapi import FastAPI, Request, HTTPException, Depends, Header
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import httpx
import base64
import os
import json
import uuid
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ==========================================
# 1. إعدادات قاعدة البيانات (Database)
# ==========================================
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./qicard.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class Merchant(Base):
    __tablename__ = "merchants"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    terminal_id = Column(String, unique=True, nullable=False)
    username = Column(String, nullable=False)
    password = Column(String, nullable=False)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True, index=True)
    merchant_id = Column(Integer, nullable=False)
    request_id = Column(String, unique=True, index=True)
    payment_id = Column(String, nullable=True)
    subscriber_name = Column(String, nullable=True)
    subscriber_phone = Column(String, nullable=True)
    amount = Column(Float, nullable=False)
    currency = Column(String, default="IQD")
    status = Column(String, default="CREATED")
    form_url = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class WebhookLog(Base):
    __tablename__ = "webhook_logs"
    id = Column(Integer, primary_key=True)
    raw_body = Column(Text, nullable=False)
    signature_valid = Column(Boolean, nullable=False)
    processed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ==========================================
# 2. إعدادات QiCard (API Client)
# ==========================================
QICARD_BASE = os.getenv("QICARD_BASE_URL", "https://uat-sandbox-3ds-api.qi.iq/api/v1")

class QiCardClient:
    def __init__(self, terminal_id: str, username: str, password: str):
        self.terminal_id = terminal_id
        self.auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.headers = {
            "X-Terminal-Id": terminal_id,
            "Authorization": f"Basic {self.auth}",
            "Content-Type": "application/json"
        }

    async def create_payment(self, request_id: str, amount: float, currency: str,
                             finish_url: str, notification_url: str,
                             customer_info: dict = None):
        payload = {
            "requestId": request_id,
            "amount": amount,
            "currency": currency,
            "finishPaymentUrl": finish_url,
            "notificationUrl": notification_url,
        }
        if customer_info:
            payload["customerInfo"] = customer_info

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{QICARD_BASE}/payment", json=payload, headers=self.headers)
            resp.raise_for_status()
            return resp.json()

    async def get_payment_status(self, payment_id: str):
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{QICARD_BASE}/payment/{payment_id}", headers=self.headers)
            resp.raise_for_status()
            return resp.json()

# ==========================================
# 3. تطبيق FastAPI والمسارات (Routes)
# ==========================================
app = FastAPI(title="QiCard Generator Server")

FINISH_URL = os.getenv("FINISH_URL", "https://your-app.com/finish")
NOTIFICATION_URL = os.getenv("NOTIFICATION_URL", "https://your-app.com/webhook/qicard")
QICARD_PUBLIC_KEY_PEM = os.getenv("QICARD_PUBLIC_KEY", "")

class MerchantCreate(BaseModel):
    name: str
    terminal_id: str
    username: str
    password: str

class PaymentCreate(BaseModel):
    merchant_id: int
    subscriber_name: str
    subscriber_phone: str
    amount: float

@app.post("/api/merchants")
def create_merchant(data: MerchantCreate, db: Session = Depends(get_db)):
    existing = db.query(Merchant).filter(Merchant.terminal_id == data.terminal_id).first()
    if existing:
        raise HTTPException(400, "Terminal ID already exists")
    merchant = Merchant(
        name=data.name,
        terminal_id=data.terminal_id,
        username=data.username,
        password=data.password
    )
    db.add(merchant)
    db.commit()
    db.refresh(merchant)
    return {"id": merchant.id, "name": merchant.name, "terminal_id": merchant.terminal_id}

@app.get("/api/merchants")
def list_merchants(db: Session = Depends(get_db)):
    merchants = db.query(Merchant).all()
    return [{"id": m.id, "name": m.name, "terminal_id": m.terminal_id, "active": m.active} for m in merchants]

@app.post("/api/payments")
async def create_payment(data: PaymentCreate, db: Session = Depends(get_db)):
    merchant = db.query(Merchant).filter(Merchant.id == data.merchant_id, Merchant.active == True).first()
    if not merchant:
        raise HTTPException(404, "Merchant not found")
    request_id = str(uuid.uuid4())
    client = QiCardClient(merchant.terminal_id, merchant.username, merchant.password)
    
    payment = Payment(
        merchant_id=merchant.id,
        request_id=request_id,
        subscriber_name=data.subscriber_name,
        subscriber_phone=data.subscriber_phone,
        amount=data.amount,
        currency="IQD",
        status="CREATED"
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    
    try:
        result = await client.create_payment(
            request_id=request_id,
            amount=data.amount,
            currency="IQD",
            finish_url=FINISH_URL,
            notification_url=NOTIFICATION_URL,
            customer_info={
                "firstName": data.subscriber_name,
                "phone": data.subscriber_phone,
            }
        )
        payment.payment_id = result.get("paymentId")
        payment.form_url = result.get("formUrl")
        db.commit()
        return {
            "payment_db_id": payment.id,
            "payment_id": payment.payment_id,
            "form_url": payment.form_url,
            "status": "CREATED"
        }
    except Exception as e:
        payment.status = "ERROR"
        db.commit()
        raise HTTPException(500, f"QiCard error: {str(e)}")

def verify_signature(payload: dict, signature_b64: str, public_key_pem: str) -> bool:
    if not public_key_pem or not signature_b64:
        return False
    try:
        data_str = "|".join([
            str(payload.get("paymentId", "-")),
            f"{payload.get('amount', 0)}.000",
            payload.get("currency", "IQD"),
            payload.get("creationDate", ""),
            payload.get("status", "")
        ])
        public_key = serialization.load_pem_public_key(public_key_pem.encode())
        signature = base64.b64decode(signature_b64)
        public_key.verify(signature, data_str.encode(), padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False

@app.post("/webhook/qicard")
async def qicard_webhook(request: Request, x_signature: Optional[str] = Header(None), db: Session = Depends(get_db)):
    body = await request.body()
    try:
        payload = json.loads(body)
    except:
        raise HTTPException(400, "Invalid JSON")
        
    valid = verify_signature(payload, x_signature or "", QICARD_PUBLIC_KEY_PEM)
    log = WebhookLog(raw_body=body.decode(), signature_valid=valid)
    db.add(log)
    db.commit()
    
    if not valid:
        raise HTTPException(400, "Invalid signature")
        
    request_id = payload.get("requestId")
    payment = db.query(Payment).filter(Payment.request_id == request_id).first()
    if not payment:
        raise HTTPException(404, "Payment not found")
        
    merchant = db.query(Merchant).filter(Merchant.id == payment.merchant_id).first()
    client = QiCardClient(merchant.terminal_id, merchant.username, merchant.password)
    
    try:
        authoritative = await client.get_payment_status(payload.get("paymentId"))
        payment.status = authoritative.get("status", payload.get("status"))
        payment.updated_at = datetime.utcnow()
        db.commit()
    except Exception:
        payment.status = payload.get("status")
        db.commit()
        
    log.processed = True
    db.commit()
    return {"status": "ok"}

@app.get("/api/payments/{merchant_id}")
def get_payments(merchant_id: int, since_id: int = 0, db: Session = Depends(get_db)):
    payments = db.query(Payment).filter(
        Payment.merchant_id == merchant_id,
        Payment.id > since_id
    ).order_by(Payment.id.asc()).all()
    
    return [{
        "id": p.id,
        "request_id": p.request_id,
        "payment_id": p.payment_id,
        "subscriber_name": p.subscriber_name,
        "subscriber_phone": p.subscriber_phone,
        "amount": p.amount,
        "currency": p.currency,
        "status": p.status,
        "created_at": p.created_at.isoformat(),
    } for p in payments]

@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}
