"""
Authentication router for SmartCanteen AI.

POST /auth/token    — exchange college_id + password for a JWT
POST /auth/register — self-registration (student role only)
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.auth import create_access_token, hash_password, verify_password
from app.database import get_db
from app.models import User, UserRole

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    college_id: str
    password: str
    role: str = "student"

    @field_validator("college_id")
    @classmethod
    def college_id_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("college_id must not be empty")
        return v

    @field_validator("password")
    @classmethod
    def password_length(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("password must be at least 6 characters")
        return v


class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    role: str
    college_id: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/token", response_model=TokenResponse)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    """Exchange college_id (username field) + password for a Bearer JWT.

    Returns access_token, token_type, role and college_id on success.
    Raises 401 on invalid credentials.
    """
    user: User | None = (
        db.query(User).filter(User.college_id == form_data.username).first()
    )
    if user is None or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect college ID or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(
        data={"sub": user.college_id, "role": user.role.value}
    )
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        role=user.role.value,
        college_id=user.college_id,
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    """Self-register a new student account.

    Only the *student* role is allowed for self-registration.
    Staff and manager accounts must be created by an admin directly.
    """
    # Role restriction
    if payload.role.lower() != "student":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Self-registration is limited to the student role. "
                   "Contact an administrator for staff or manager accounts.",
        )

    # Duplicate check
    existing = db.query(User).filter(User.college_id == payload.college_id).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"College ID '{payload.college_id}' is already registered.",
        )

    user = User(
        college_id=payload.college_id,
        hashed_password=hash_password(payload.password),
        role=UserRole.student,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return {
        "message": "Registration successful",
        "college_id": user.college_id,
        "role": user.role.value,
    }
