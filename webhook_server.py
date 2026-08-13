import logging
import os
from datetime import datetime
import httpx
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel  # <--- السطر اللي كان ناقص
import uvicorn
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import text

# ============ الإعدادات الأساسية ============
BOT_TOKEN = "8141024354:AAFGwx-UzRfQhZlOypUmRG_kTtPMIzDqllA"
WEBHOOK_SECRET = "TammSecret9"
ADMIN_IDS = [5811814277]
# هنا بنسحب رابط قاعدة البيانات من إعدادات Vercel اللي حطيناها
DATABASE_URL = os.environ.get("DATABASE_URL")
PORT = int(os.environ.get("PORT", 8000))

# تعديل الرابط ليتوافق مع SQLAlchemy Async
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

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

@app.post("/webhook")
async def receive_sms(payload: SmsPayload, x_api_key: str = Header(None)):
    if x_api_key != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    async with AsyncSessionLocal() as session:
        try:
            # البحث عن طلب معلق في Supabase
            result = await session.execute(text("""
                SELECT id, user_id, amount FROM recharges
                WHERE status = 'pending' AND amount <= :amount
                ORDER BY request_date ASC LIMIT 1
            """), {"amount": payload.amount})
            
            recharge = result.fetchone()
            
            if not recharge:
                return {"status": "no_match"}
            
            recharge_id, user_id, req_amount = recharge
            
            # تحديث الشحن وإضافة الرصيد في Supabase
            await session.execute(text("UPDATE recharges SET status = 'completed' WHERE id = :id"), {"id": recharge_id})
            await session.execute(text("UPDATE users SET balance = balance + :amt WHERE user_id = :uid"), {"amt": float(req_amount), "uid": user_id})
            await session.commit()
            
            # إشعار تيليجرام
            await send_telegram_message(user_id, f"✅ <b>تم شحن رصيدك تلقائياً!</b>\n\n💰 المبلغ: <code>{req_amount}</code> ج.م")
            
            for admin_id in ADMIN_IDS:
                await send_telegram_message(admin_id, f"🔔 شحن ناجح للمستخدم {user_id} بقيمة {req_amount} ج.م")

            return {"status": "success", "user_id": user_id}

        except Exception as e:
            await session.rollback()
            logger.error(f"Error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)