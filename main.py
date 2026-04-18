"""
FastAPI 应用主入口：集中创建应用实例、配置中间件与路由。
"""

from datetime import datetime
import warnings

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.openapi.docs import (
	get_redoc_html,
	get_swagger_ui_html,
	get_swagger_ui_oauth2_redirect_html,
)

from app.api.v1.routes import api_router
from app.config import settings

from app.middleware import setup_middleware
from app.static_config import setup_static_files


warnings.filterwarnings(
	"ignore",
	message=r"Valid config keys have changed in V2:\s*\* 'from_attributes' has been renamed to 'from_attributes'",
	module=r"pydantic\._internal\._config",
)


openapi_tags = [
	{"name": "材料", "description": "材料上传与管理"},
	{"name": "群组", "description": "群组与师生关系导入"},
	{"name": "论文", "description": "论文上传与版本管理"},
	{"name": "AI评审", "description": "AI 自动评审与报告"},
	{"name": "标注", "description": "论文标注创建与查询"},
	{"name": "管理", "description": "后台管理、模板与审计"},
	{"name": "用户", "description": "用户创建、更新、导入与删除"},
	{"name": "智能体", "description": "智能体API调用"},
]


app = FastAPI(
	title=settings.PROJECT_NAME,
	version=settings.VERSION,
	description=settings.DESCRIPTION,
	docs_url="/docs",
	redoc_url="/redoc",
	openapi_url="/openapi.json",
)
app.openapi_tags = openapi_tags


def setup_middlewares(app: FastAPI) -> None:
	"""配置 CORS、GZip 及自定义中间件。"""
	app.add_middleware(
		CORSMiddleware,
		allow_origins=settings.CORS_ORIGINS,
		allow_credentials=True,
		allow_methods=["*"],
		allow_headers=["*"],
	)
	app.add_middleware(GZipMiddleware, minimum_size=1000)
	setup_middleware(app)


def register_routes(app: FastAPI) -> None:
	"""集中注册 API 路由。"""
	app.include_router(api_router, prefix="/api/v1")



setup_middlewares(app)
setup_static_files(app)
register_routes(app)


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html():
	return get_swagger_ui_html(
		openapi_url=app.openapi_url,
		title=f"{app.title} - Swagger UI",
		swagger_ui_parameters={"filter": True},
	)


@app.get(app.swagger_ui_oauth2_redirect_url, include_in_schema=False)
async def swagger_ui_redirect():
	return get_swagger_ui_oauth2_redirect_html()


@app.get("/redoc", include_in_schema=False)
async def redoc_html():
	return get_redoc_html(openapi_url=app.openapi_url, title=f"{app.title} - ReDoc")


@app.get("/", include_in_schema=False)
async def root():
	return {
		"message": "欢迎使用 CD AI 后端 API",
		"version": settings.VERSION,
		"timestamp": datetime.now().isoformat(),
		"docs": "/docs",
	}


if __name__ == "__main__":
	print(f"启动 {settings.PROJECT_NAME} API 服务...")
	print(f"API 文档地址: http://{settings.HOST}:{settings.PORT}/docs")
	print(f"运行模式: {'开发 (热重载)' if settings.RELOAD else '生产'}")
	uvicorn.run(
		"main:app",
		host=settings.HOST,
		port=settings.PORT,
		reload=settings.RELOAD,
		log_level="info",
		access_log=True,
	)