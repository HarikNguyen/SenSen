"""Business logic invoked by app/pages.py route handlers (non-scan).

Scan processing lives in app/scanning.py — this file is for everything else
(currently just registration).
"""

import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.database import APIKey, User


def register_user(email: str, db: Session) -> tuple[User, APIKey]:
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(email=email)
    db.add(user)
    db.flush()  # populate user.id before creating the dependent APIKey row

    api_key = APIKey(key=uuid.uuid4().hex, user_id=user.id)
    db.add(api_key)
    db.commit()

    return user, api_key
