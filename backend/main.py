
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


# ============================================================
# LOAD ENVIRONMENT VARIABLES
# ============================================================

load_dotenv()


# ============================================================
# FASTAPI APPLICATION
# ============================================================

app = FastAPI(
    title="Appa Connect API",
    version="1.0"
)


# ============================================================
# CORS
# ============================================================

# For development this allows your local frontend.
# We will restrict this to your Netlify URL after deployment.

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# VAPID CONFIGURATION
# ============================================================

VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")

VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")

VAPID_EMAIL = os.getenv(
    "VAPID_EMAIL",
    "mailto:sajrag05@gmail.com"
)


def get_vapid_private_key():
    """
    Returns the VAPID private key.

    Local development:
        VAPID_PRIVATE_KEY=vapid_private.pem

    Render/production:
        VAPID_PRIVATE_KEY contains the actual PEM key.
    """

    if not VAPID_PRIVATE_KEY:
        raise RuntimeError(
            "VAPID_PRIVATE_KEY is not configured."
        )

    # --------------------------------------------------------
    # CASE 1:
    # The environment variable contains the actual PEM key.
    # This is what we will use on Render.
    # --------------------------------------------------------

    if "BEGIN" in VAPID_PRIVATE_KEY:
        return VAPID_PRIVATE_KEY

    # --------------------------------------------------------
    # CASE 2:
    # The environment variable contains a filename.
    # This is what we currently use locally.
    # --------------------------------------------------------

    key_path = VAPID_PRIVATE_KEY

    if not os.path.isabs(key_path):
        key_path = os.path.join(
            os.path.dirname(__file__),
            key_path
        )

    if not os.path.exists(key_path):
        raise RuntimeError(
            f"VAPID private key file not found: {key_path}"
        )

    with open(key_path, "r", encoding="utf-8") as file:
        return file.read()


# ============================================================
# PYDANTIC MODELS
# ============================================================

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


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def root():
    return {
        "message": "Appa Connect backend is running ❤️"
    }


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
def health():
    return {
        "status": "ok"
    }


# ============================================================
# USERS
# ============================================================

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


# ============================================================
# VAPID PUBLIC KEY
# ============================================================

@app.get("/push/public-key")
def get_public_key():

    try:

        from py_vapid import Vapid

        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat
        )

        private_key = get_vapid_private_key()

        # ----------------------------------------------------
        # Load VAPID private key from PEM contents
        # ----------------------------------------------------

        vapid = Vapid.from_pem(
            private_key.encode("utf-8")
        )

        # ----------------------------------------------------
        # Get the uncompressed EC public key.
        #
        # Browser PushManager expects this format.
        # ----------------------------------------------------

        public_key_bytes = vapid.public_key.public_bytes(
            Encoding.X962,
            PublicFormat.UncompressedPoint
        )

        # ----------------------------------------------------
        # Convert to URL-safe Base64.
        # ----------------------------------------------------

        public_key_base64 = base64.urlsafe_b64encode(
            public_key_bytes
        ).rstrip(b"=").decode("utf-8")

        return {
            "publicKey": public_key_base64
        }

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=(
                "Could not generate VAPID public key: "
                + str(e)
            )
        )


# ============================================================
# SAVE PUSH SUBSCRIPTION
# ============================================================

@app.post("/push/subscribe")
def subscribe(
    subscription: SubscriptionCreate,
    db: Session = Depends(get_db)
):

    # --------------------------------------------------------
    # Check that the user exists.
    # --------------------------------------------------------

    user = db.query(User).filter(
        User.id == subscription.user_id
    ).first()

    if not user:

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    # --------------------------------------------------------
    # Check whether this device already exists.
    # --------------------------------------------------------

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


# ============================================================
# SEND PUSH NOTIFICATION
# ============================================================

@app.post("/push/send")
def send_notification(
    notification: NotificationRequest,
    db: Session = Depends(get_db)
):

    # --------------------------------------------------------
    # Get all devices belonging to this user.
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Notification payload.
    # --------------------------------------------------------

    payload = {
        "title": notification.title,
        "body": notification.body
    }

    results = []

    # --------------------------------------------------------
    # Get private VAPID key.
    # --------------------------------------------------------

    try:

        private_key = get_vapid_private_key()

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

    # --------------------------------------------------------
    # Send notification to every registered device.
    # --------------------------------------------------------

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

                vapid_private_key=private_key,

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

        except Exception as e:

            results.append({
                "success": False,
                "error": str(e)
            })

    return {
        "success": True,
        "results": results
    }


# ============================================================
# CREATE HEALTH REMINDER
# ============================================================

@app.post("/reminders")
def create_reminder(
    reminder: ReminderCreate,
    db: Session = Depends(get_db)
):

    # --------------------------------------------------------
    # Check that user exists.
    # --------------------------------------------------------

    user = db.query(User).filter(
        User.id == reminder.user_id
    ).first()

    if not user:

        raise HTTPException(
            status_code=404,
            detail="User not found"
        )

    # --------------------------------------------------------
    # Create reminder.
    # --------------------------------------------------------

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


# ============================================================
# GET HEALTH REMINDERS
# ============================================================

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

