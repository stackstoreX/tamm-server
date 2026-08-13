import logging
import os
from datetime import datetime
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import uvicorn
import sqlite3

# ============ الإعدادات الأساسية ============
BOT_TOKEN = "8141024354:AAFGwx-UzRfQhZlOypUmRG_kTtPMIzDqllA"
WEBHOOK_SECRET = "TammSecret9"
ADMIN_IDS = [5811814277]
DB_PATH = "bot_database.db"
PORT = int(os.environ.get("PORT", 8000))

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ============ إرسال تيليجرام ============
async def send_telegram_message(chat_id: int, text: str):
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
            )
    except Exception as e:
        logger.error(f"Telegram error: {e}")

class SmsPayload(BaseModel):
    sender: str
    amount: float
    message_body: str
    timestamp: str
    app_version: str = "1.0"

app = FastAPI(title="Tamm Simple Webhook")

@app.get("/")
async def root():
    return {"status": "ok", "service": "Tamm Server Running"}

@app.post("/webhook")
async def receive_sms(payload: SmsPayload, x_api_key: str = Header(None)):
    if x_api_key != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    logger.info(f"📥 رسالة من: {payload.sender} بمبلغ: {payload.amount}")
    
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        # البحث عن طلب معلق
        cursor.execute("""
            SELECT id, user_id, amount FROM recharges
            WHERE status = 'pending' AND amount <= ?
            ORDER BY request_date ASC LIMIT 1
        """, (payload.amount,))
        
        recharge = cursor.fetchone()
        
        if not recharge:
            conn.close()
            return {"status": "no_match", "message": "لا توجد طلبات معلقة مطابقة"}
        
        recharge_id, user_id, req_amount = recharge
        
        # تحديث الشحن وإضافة الرصيد
        cursor.execute("UPDATE recharges SET status = 'completed' WHERE id = ?", (recharge_id,))
        cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (req_amount, user_id))
        conn.commit()
        conn.close()
        
        # إشعار تيليجرام
        await send_telegram_message(user_id, f"✅ <b>تم شحن رصيدك تلقائياً!</b>\n\n💰 المبلغ: <code>{req_amount}</code> ج.م")
        
        for admin_id in ADMIN_IDS:
            await send_telegram_message(admin_id, f"🔔 شحن ناجح للمستخدم {user_id} بقيمة {req_amount} ج.م")

        return {"status": "success", "user_id": user_id, "amount": req_amount}

    except Exception as e:
        conn.close()
        logger.error(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)