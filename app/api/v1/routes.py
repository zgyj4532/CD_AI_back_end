"""
API v1 路由汇总
"""
from fastapi import APIRouter
from app.api.v1.endpoints import (
	documents,
	groups,
	papers,
	ai_review,
	annotations,
	admin,
	notifications,
	users,
	agent_api,
)

api_router = APIRouter()

# 注册各个端点路由
api_router.include_router(documents.router, prefix="/materials", tags=["材料"])
api_router.include_router(groups.router, prefix="/groups", tags=["群组"])
api_router.include_router(papers.router, prefix="/papers", tags=["论文"])
api_router.include_router(ai_review.router, prefix="/papers", tags=["AI评审"])
api_router.include_router(annotations.router, prefix="/annotations", tags=["标注"])
api_router.include_router(admin.router, prefix="/admin", tags=["管理"])
api_router.include_router(notifications.router, prefix="/notifications", tags=["通知"])
api_router.include_router(users.router, prefix="/users", tags=["用户"])
api_router.include_router(agent_api.router, prefix="/agent", tags=["智能体"])


