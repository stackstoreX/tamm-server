import sqlite3
import logging
import os
import json
from datetime import datetime
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException, Header, Depends
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
import uvicorn

# ============ الإعدادات ============
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8141024354:AAFGwx-UzRfQhZlOypUmRG_kTtPMIzDqllA")
ADMIN_IDS = [5811814277]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "TammSecret9")
DB_PATH = os.environ.get("DB_PATH", "bot_database.db")
PORT = int(os.environ.get("PORT", 8000))

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============ Header Security ============
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def verify_api_key(x_api_key: str = Header(None)):
    if x_api_key != WEBHOOK_SECRET:
        logger.warning(f"⚠️ محاولة وصول غير مصرح بها من API Key: {x_api_key}")
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

# ============ Database Helpers ============
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance REAL DEFAULT 0,
            join_date TEXT
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS recharges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            phone_number TEXT,
            status TEXT DEFAULT 'pending',
            request_date TEXT,
            admin_notes TEXT DEFAULT '',
            sms_raw TEXT DEFAULT ''
        )
    ''')
    
    # جدول سجل العمليات للـ Audit
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS webhook_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT,
            amount REAL,
            message_body TEXT,
            status TEXT,
            processed_at TEXT,
            matched_user_id INTEGER
        )
    ''')
    
    conn.commit()
    conn.close()

# ============ Telegram Notification ============
async def send_telegram_message(chat_id: int, text: str, parse_mode: str = "HTML"):
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode
                }
            )
            if response.status_code != 200:
                logger.error(f"فشل إرسال رسالة لـ {chat_id}: {response.text}")
    except Exception as e:
        logger.error(f"خطأ في إرسال رسالة Telegram: {e}")

# ============ Models ============
class SmsPayload(BaseModel):
    sender: str
    amount: float
    message_body: str
    timestamp: str
    app_version: str = "1.0"

# ============ FastAPI App ============
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("🚀 سيرفر Webhook بدأ العمل")
    yield
    logger.info("🛑 سيرفر Webhook توقف")

app = FastAPI(
    title="Tamm SMS Webhook",
    version="1.0.0",
    lifespan=lifespan
)

@app.get("/")
async def root():
    return {"status": "ok", "service": "Tamm SMS Webhook", "time": datetime.now().isoformat()}

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.post("/webhook")
async def receive_sms(payload: SmsPayload, auth: bool = Depends(verify_api_key)):
    logger.info(f"📥 رسالة جديدة: {payload.sender} - {payload.amount} ج.م")
    
    # حفظ الرسالة في السجل أولاً (للـ Audit)
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            INSERT INTO webhook_logs (sender, amount, message_body, status, processed_at)
            VALUES (?, ?, ?, 'processing', ?)
        """, (payload.sender, payload.amount, payload.message_body, datetime.now().isoformat()))
        log_id = cursor.lastrowid
        conn.commit()
        
        # البحث عن طلبات شحن معلقة مطابقة
        cursor.execute("""
            SELECT id, user_id, amount, phone_number
            FROM recharges
            WHERE status = 'pending'
            AND (phone_number = ? OR ? LIKE '%' || phone_number || '%')
            AND amount <= ?
            ORDER BY request_date ASC
        """, (payload.sender, payload.message_body, payload.amount))
        
        recharges = cursor.fetchall()
        
        if not recharges:
            logger.info(f"ℹ️ لا توجد طلبات مطابقة لـ {payload.sender}")
            cursor.execute("""
                UPDATE webhook_logs SET status = 'no_match' WHERE id = ?
            """, (log_id,))
            conn.commit()
            conn.close()
            return {"status": "no_match", "message": "لا توجد طلبات شحن معلقة"}
        
        processed = 0
        total = 0.0
        
        for r in recharges:
            recharge_id, user_id, req_amount, phone = r
            
            if payload.amount >= req_amount:
                # تحديث حالة الشحن
                cursor.execute("""
                    UPDATE recharges 
                    SET status = 'completed', 
                        admin_notes = ?,
                        sms_raw = ?
                    WHERE id = ?
                """, (
                    f"شحن تلقائي SMS @ {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                    payload.message_body,
                    recharge_id
                ))
                
                # إضافة الرصيد
                cursor.execute("""
                    UPDATE users SET balance = balance + ? WHERE user_id = ?
                """, (req_amount, user_id))
                
                conn.commit()
                processed += 1
                total += req_amount
                
                logger.info(f"✅ شحن {user_id} بمبلغ {req_amount}")
                
                # إشعار المستخدم
                await send_telegram_message(user_id, f"""✅ <b>تم شحن رصيدك تلقائياً!</b>

💰 المبلغ: <code>{req_amount}</code> ج.م
📱 من رقم: <code>{payload.sender}</code>
🆔 رقم العملية: <code>#{recharge_id}</code>
⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}

شكراً لاستخدامك Tamm Store 🎉""")
                
                # إشعار الأدمن
                for admin_id in ADMIN_IDS:
                    await send_telegram_message(admin_id, f"""🔔 <b>شحن تلقائي جديد</b>

👤 المستخدم: <code>{user_id}</code>
💰 المبلغ: {req_amount} ج.م
📱 المرسل: {payload.sender}
🆔 العملية: #{recharge_id}""")
        
        # تحديث سجل الـ Audit
        cursor.execute("""
            UPDATE webhook_logs 
            SET status = 'success', matched_user_id = ?
            WHERE id = ?
        """, (recharges[0][1] if recharges else None, log_id))
        conn.commit()
        conn.close()
        
        return {
            "status": "success",
            "processed": processed,
            "total_amount": total
        }
        
    except Exception as e:
        conn.close()
        logger.error(f"❌ خطأ: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/stats")
async def stats(auth: bool = Depends(verify_api_key)):
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM webhook_logs WHERE status = 'success'")
    success = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM webhook_logs WHERE status = 'no_match'")
    no_match = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM recharges WHERE status = 'pending'")
    pending = cursor.fetchone()[0]
    
    conn.close()
    return {
        "successful_recharges": success,
        "no_match": no_match,
        "pending_recharges": pending
    }

# ============ تشغيل السيرفر ============
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
