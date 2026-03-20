from pydantic import BaseModel
from typing import Optional


class PaperCreate(BaseModel):
    title: str


class PaperOut(BaseModel):
    id: int
    owner_id: int
    latest_version: str
    oss_key: Optional[str]


class VersionOut(BaseModel):
    version: str
    size: int
    created_at: str
    status: str


class PaperStatusCreate(BaseModel):
    status: str
    size: int | None = None


class PaperStatusUpdate(BaseModel):
    status: str
    size: int | None = None


class PaperStatusOut(BaseModel):
    paper_id: int
    version: str
    status: str
    size: int
    updated_at: str
from datetime import datetime


class MaterialResponse(BaseModel):
    id: int
    filename: str
    content_type: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}

class DDLCreate(BaseModel):
    teacher_id: int
    ddl_time: str 

class DDLOut(BaseModel):
    ddlid: int
    creator_id: int
    teacher_id: int
    ddl_time: str
    created_at: Optional[str] = None

    model_config = {"from_attributes": True}
