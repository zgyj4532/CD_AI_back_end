from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form, Query
from typing import Optional
import aiohttp
import os
import sqlite3
from datetime import datetime
from urllib.parse import quote
import pymysql

from app.database import get_db
from app.static_config import UPLOADS_MOUNT_PATH
from app.services.audit import submit_audit_task

router = APIRouter()

# 智能体API基础URL
AGENT_API_BASE = "http://127.0.0.1:8651"

# 确保uploads目录存在
UPLOADS_DIR = "./uploads"
os.makedirs(UPLOADS_DIR, exist_ok=True)

# SQLite数据库路径
DB_PATH = "./agent_tasks.db"

# 初始化SQLite数据库
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_path TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        progress INTEGER DEFAULT 0,
        result_path TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL,
        current_stage TEXT,
        error_log TEXT,
        updated_at TEXT NOT NULL
    )
    ''')
    conn.commit()
    conn.close()

# 初始化数据库
init_db()

@router.post(
    "/audit",
    summary="提交文件审核",
    description="接收文件或论文ID和版本号，保存至./uploads/，创建任务并返回task_id"
)
async def submit_audit(
    request: Request,
    file: Optional[UploadFile] = File(None),
    paper_id: Optional[int] = None,
    version: Optional[str] = None,
    audit_config: str = Form('{"checks": ["grammar", "plagiarism"]}'),
    db: pymysql.connections.Connection = Depends(get_db)
):
    try:
        if request is not None:
            form_data = await request.form()
            if paper_id is None:
                raw_paper_id = form_data.get("paper_id") or request.query_params.get("paper_id")
                if raw_paper_id not in (None, ""):
                    try:
                        paper_id = int(raw_paper_id)
                    except (TypeError, ValueError):
                        raise HTTPException(status_code=400, detail="paper_id 必须是整数")
            if version is None:
                version = form_data.get("version") or request.query_params.get("version")

        paper_request = paper_id is not None or version is not None
        if paper_request and (paper_id is None or version is None):
            raise HTTPException(status_code=400, detail="论文审核必须同时提供 paper_id 和 version")

        if paper_id is not None and version is not None:
            result = await submit_audit_task(
                db,
                paper_id=paper_id,
                version=version,
                audit_config=audit_config,
            )
            task_id = result.get("task_id")
            status = result.get("status", "pending")
            mysql_inserted = result.get("mysql_inserted", False)
            cursor = db.cursor()
            cursor.execute("SELECT oss_key FROM papers WHERE id = %s AND version = %s", (paper_id, version))
            paper = cursor.fetchone()
            file_path = paper[0] if paper else ""
            cursor.close()
        elif file:
            content = await file.read()
            file_path = os.path.join(UPLOADS_DIR, file.filename)
            with open(file_path, "wb") as f:
                f.write(content)
            uploaded_file_url = f"{UPLOADS_MOUNT_PATH}/{quote(file.filename)}"
            result = await submit_audit_task(
                db,
                file_content=content,
                filename=file.filename,
                audit_config=audit_config,
            )
            task_id = result.get("task_id")
            status = result.get("status", "pending")
            mysql_inserted = result.get("mysql_inserted", False)
        else:
            raise HTTPException(status_code=400, detail="必须提供文件或论文ID和版本号")
        
        # 插入任务记录到SQLite
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            "INSERT OR REPLACE INTO tasks (id, file_path, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, file_path, status, now, now)
        )
        conn.commit()
        conn.close()
        return {
            "task_id": task_id,
            "status": status,
            "paper_id": paper_id,
            "version": version,
            "mysql_inserted": mysql_inserted,
            "file_url": f"{UPLOADS_MOUNT_PATH}/task_{task_id}.zip",
            "uploaded_file_url": uploaded_file_url if file else None,
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to submit audit: {str(e)}")

@router.get(
    "/tasks/by-paper",
    summary="通过论文ID和版本查询任务ID",
    description="根据论文ID和版本号查询对应的任务ID"
)
async def get_task_by_paper(
    paper_id: int = Query(..., description="论文ID"),
    version: str = Query(..., description="论文版本号"),
    db: pymysql.connections.Connection = Depends(get_db)
):
    try:
        cursor = db.cursor()
        # 查询任务
        cursor.execute("SELECT task_id FROM tasks WHERE paper_id = %s AND version = %s", (paper_id, version))
        task = cursor.fetchone()
        if not task:
            raise HTTPException(status_code=404, detail=f"未找到论文ID {paper_id} 版本 {version} 对应的任务")
        
        return {
            "task_id": task[0],
            "paper_id": paper_id,
            "version": version
        }
        
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库操作失败：{str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询任务失败：{str(e)}")


@router.get(
    "/tasks/{task_id}",
    summary="查询任务进度",
    description="查询SQLite中的任务进度"
)
async def get_task_status(task_id: int):
    try:
        # 调用智能体API获取任务状态
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{AGENT_API_BASE}/api/v1/tasks/{task_id}") as response:
                if response.status != 200:
                    raise HTTPException(status_code=response.status, detail=await response.text())
                result = await response.json()
        
        # 更新本地SQLite数据库
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            "UPDATE tasks SET status=?, progress=?, current_stage=?, updated_at=? WHERE id=?",
            (result.get("status"), result.get("progress", 0), result.get("current_stage"), now, task_id)
        )
        conn.commit()
        conn.close()
        
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get task status: {str(e)}")

@router.get(
    "/report/{task_id}",
    summary="获取JSON报告",
    description="获取智能体生成的JSON报告"
)
async def get_report(task_id: int):
    try:
        # 调用智能体API获取报告
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{AGENT_API_BASE}/api/v1/report/{task_id}") as response:
                if response.status != 200:
                    raise HTTPException(status_code=response.status, detail=await response.text())
                result = await response.json()
        
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get report: {str(e)}")

@router.get(
    "/download/{task_id}",
    summary="下载ZIP文件",
    description="下载包含Word批注版和PDF报告的ZIP文件"
)
async def download_report(task_id: int, type: str = "zip"):
    try:
        # 调用智能体API下载文件
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{AGENT_API_BASE}/api/v1/download/{task_id}?type={type}", allow_redirects=True) as response:
                if response.status != 200:
                    raise HTTPException(status_code=response.status, detail=await response.text())
                content = await response.read()
        
        # 保存文件到本地
        zip_path = os.path.join(UPLOADS_DIR, f"task_{task_id}.zip")
        with open(zip_path, "wb") as f:
            f.write(content)
        
        # 返回文件路径
        return {
            "message": "Download successful",
            "file_path": zip_path
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to download report: {str(e)}")