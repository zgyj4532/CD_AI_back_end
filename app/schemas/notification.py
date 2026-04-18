from pydantic import BaseModel
from typing import Optional, List


class NotificationPush(BaseModel):
    title: str
    content: str
    target_user_id: Optional[str] = None
    target_user_ids: Optional[List[str]] = None
    target_username: Optional[str] = None
    sender_id: Optional[str] = None
    sender_role: Optional[str] = None


class NotificationUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None


class NotificationItem(BaseModel):
    id: int
    user_id: Optional[str]
    username: Optional[str]
    title: str
    content: str
    target_user_id: Optional[str]
    target_username: Optional[str]
    operation_time: Optional[str]
    status: Optional[str]
    sender_id: Optional[str] = None


class NotificationQueryResponse(BaseModel):
    items: List[NotificationItem]
    page: int
    page_size: int
    total: int
    total_pages: int
