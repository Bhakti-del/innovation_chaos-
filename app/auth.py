"""
Authentication and authorisation helpers for SmartCanteen AI.

- Passwords hashed with bcrypt (cost factor 12).
- Sessions use short-lived JWT (HS256, 60-minute expiry).
- Three role-check FastAPI dependencies: require_student, require_staff, require_manager.
"""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, UserRole

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SECRET_KEY = "smartcanteen-secret-key-change-in-production"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def hash_password(password: str) -> str:
    """Return a bcrypt hash of *password*."""
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches the stored *hashed* value."""
    return pwd_context.verify(plain, hashed)


# ---------------------------------------------------------------------------
# JWT tokens
# ---------------------------------------------------------------------------

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Encode *data* into a signed JWT with an expiry claim.

    Args:
        data: Arbitrary claims to embed (must include a ``sub`` key with the
              college_id string).
        expires_delta: Override the default expiry window.

    Returns:
        Encoded JWT string.
    """
    to_encode = data.copy()
    expire = datetime.utcnow() + (
        expires_delta if expires_delta else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and verify *token*, returning the claims dict.

    Raises:
        HTTPException 401 if the token is invalid or expired.
    """
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is invalid or has expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


# ---------------------------------------------------------------------------
# OAuth2 scheme
# ---------------------------------------------------------------------------

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")


# ---------------------------------------------------------------------------
# FastAPI dependency: authenticated user
# ---------------------------------------------------------------------------

def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """FastAPI dependency that resolves the Bearer token to a User row.

    Raises:
        HTTPException 401 if the token is invalid.
        HTTPException 401 if the user no longer exists in the database.
    """
    payload = decode_token(token)
    college_id: Optional[str] = payload.get("sub")
    if college_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token payload missing subject",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.query(User).filter(User.college_id == college_id).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


# ---------------------------------------------------------------------------
# Role-check dependencies
# ---------------------------------------------------------------------------

def _require_role(required_role: UserRole, user: User) -> User:
    """Raise 403 if *user* does not have *required_role*."""
    if user.role != required_role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access restricted to {required_role.value} role",
        )
    return user


def require_student(current_user: User = Depends(get_current_user)) -> User:
    """Dependency: caller must be a student."""
    return _require_role(UserRole.student, current_user)


def require_staff(current_user: User = Depends(get_current_user)) -> User:
    """Dependency: caller must be counter staff."""
    return _require_role(UserRole.staff, current_user)


def require_manager(current_user: User = Depends(get_current_user)) -> User:
    """Dependency: caller must be a manager."""
    return _require_role(UserRole.manager, current_user)
