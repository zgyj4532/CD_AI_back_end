from pydantic import BaseModel, EmailStr, Field
from typing import Optional, Literal


class BaseUserCreate(BaseModel):
    username: str
    password: Optional[str] = None
    phone: Optional[str] = Field(None, min_length=1, max_length=32)
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None


class StudentCreate(BaseUserCreate):
    pass


class TeacherCreate(BaseUserCreate):
    pass


class AdminCreate(BaseUserCreate):
    role: Optional[str] = "admin"


class UserUpdate(BaseModel):
    user_type: Optional[Literal["student", "teacher", "admin"]] = None
    phone: Optional[str] = Field(None, min_length=1, max_length=32)
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    role: Optional[str] = None
    password: Optional[str] = None


class UserOut(BaseModel):
    id: int
    username: str
    phone: Optional[str] = None
    email: Optional[str] = None
    full_name: Optional[str] = None
    role: Optional[str] = None
    created_at: str
    updated_at: str


class UserBindPhone(BaseModel):
    phone: str = Field(..., min_length=1, max_length=32)


class UserBindEmail(BaseModel):
    email: EmailStr


class LoginRequest(BaseModel):
    username: str
    password: str
    user_type: Optional[Literal["student", "teacher", "admin"]] = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
