"""API key authentication dependency."""

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.database import APIKey, get_db


def verify_api_key(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> APIKey:
    api_key = db.query(APIKey).filter(APIKey.key == x_api_key).first()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key"
        )
    api_key.request_count += 1
    db.commit()
    return api_key
