from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from sqlalchemy.orm import Session

from pydantic import BaseModel

from dotenv import load_dotenv
import os
import json
import base64
from pywebpush import webpush, WebPushException

from database import (
    get_db,
    User,
    PushSubscription,
    HealthReminder
)


load_dotenv()


app = FastAPI(
    title="Appa Connect API",
    version="1.0"
)


# --------------------------------------------------
# CORS
# --------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------
# Configuration
# --------------------------------------------------

VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")
VAPID_EMAIL = os.getenv(
    "VAPID_EMAIL",
    "mailto:appa-connect@example.com"
)


# --------------------------------------------------
# Pydantic models
# --------------------------------------------------

class UserCreate(BaseModel):
    name: str
    role: str


class SubscriptionCreate(BaseModel):
    user_id: int
    endpoint: str
    p256dh: str
    auth: str


class ReminderCreate(BaseModel):
    user_id: int
    reminder_type: str
    time: str
    message: str


class NotificationRequest(BaseModel):
    user_id: int
    title: str
    body: str


# --------------------------------------------------
# Root
# --------------------------------------------------

@app.get("/")
def root():
    return {
        "message": "Appa Connect backend is running ❤️"
    }


# --------------------------------------------------
# Health check
# --------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok"
    }


# --------------------------------------------------
# Users
# --------------------------------------------------

@app.post("/users")
def create_user(
    user_data: UserCreate,
    db: Session = Depends(get_db)
):

    if user_data.role not in ["father", "daughter"]:
        raise HTTPException(
            status_code=400,
            detail="Role must be father or daughter"
        )

    user = User(
        name=user_data.name,
        role=user_data.role
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    return {
        "id": user.id,
        "name": user.name,
        "role": user.role
    }


@app.get("/users")
def get_users(
    db: Session = Depends(get_db)
):

    users = db.query(User).all()

    return [
        {
            "id": user.id,
            "name": user.name,
            "role": user.role
        }
        for user in users
    ]


# --------------------------------------------------
# VAPID public key
# --------------------------------------------------

@app.get("/push/public-key")
def get_public_key():
    if not VAPID_PRIVATE_KEY:
        raise HTTPException(
            status_code=500,
            detail="VAPID private key is not configured"
        )

    try:
        from py_vapid import Vapid
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat
        )

        vapid = Vapid.from_file(VAPID_PRIVATE_KEY)

        public_key_bytes = vapid.public_key.public_bytes(
            Encoding.X962,
            PublicFormat.UncompressedPoint
        )

        public_key_base64 = base64.urlsafe_b64encode(
            public_key_bytes
        ).rstrip(b"=").decode("utf-8")

        return {
            "publicKey": public_key_base64
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not generate VAPID public key: {str(e)}"
        )

# --------------------------------------------------
# Save push subscription
# --------------------------------------------------

@app.post("/push/subscribe")
def subscribe(
    subscription: SubscriptionCreate,
    db: Session = Depends(get_db)
):

    user = db.query(User).filter(
        User.id == subscription.user_id
    ).first()

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    existing = db.query(PushSubscription).filter(
        PushSubscription.endpoint == subscription.endpoint
    ).first()

    if existing:

        existing.user_id = subscription.user_id
        existing.p256dh = subscription.p256dh
        existing.auth = subscription.auth

    else:

        new_subscription = PushSubscription(
            user_id=subscription.user_id,
            endpoint=subscription.endpoint,
            p256dh=subscription.p256dh,
            auth=subscription.auth
        )

        db.add(new_subscription)

    db.commit()

    return {
        "success": True,
        "message": "Push subscription saved"
    }


# --------------------------------------------------
# Send notification
# --------------------------------------------------

@app.post("/push/send")
def send_notification(
    notification: NotificationRequest,
    db: Session = Depends(get_db)
):

    subscriptions = db.query(
        PushSubscription
    ).filter(
        PushSubscription.user_id == notification.user_id
    ).all()

    if not subscriptions:

        raise HTTPException(
            status_code=404,
            detail="No registered device for this user"
        )

    payload = {
        "title": notification.title,
        "body": notification.body
    }

    results = []

    for subscription in subscriptions:

        push_info = {
            "endpoint": subscription.endpoint,
            "keys": {
                "p256dh": subscription.p256dh,
                "auth": subscription.auth
            }
        }

        try:

            webpush(
                subscription_info=push_info,
                data=json.dumps(payload),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={
                    "sub": VAPID_EMAIL
                }
            )

            results.append({
                "success": True
            })

        except WebPushException as e:

            results.append({
                "success": False,
                "error": str(e)
            })

    return {
        "success": True,
        "results": results
    }


# --------------------------------------------------
# Health reminders
# --------------------------------------------------

@app.post("/reminders")
def create_reminder(
    reminder: ReminderCreate,
    db: Session = Depends(get_db)
):

    user = db.query(User).filter(
        User.id == reminder.user_id
    ).first()

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    new_reminder = HealthReminder(
        user_id=reminder.user_id,
        reminder_type=reminder.reminder_type,
        time=reminder.time,
        message=reminder.message,
        enabled=1
    )

    db.add(new_reminder)
    db.commit()
    db.refresh(new_reminder)

    return {
        "id": new_reminder.id,
        "message": "Reminder created"
    }


@app.get("/reminders/{user_id}")
def get_reminders(
    user_id: int,
    db: Session = Depends(get_db)
):

    reminders = db.query(
        HealthReminder
    ).filter(
        HealthReminder.user_id == user_id
    ).all()

    return [
        {
            "id": r.id,
            "type": r.reminder_type,
            "time": r.time,
            "message": r.message,
            "enabled": bool(r.enabled)
        }
        for r in reminders
    ]