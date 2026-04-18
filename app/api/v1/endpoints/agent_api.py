from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse
from typing import Optional
import aiohttp
import os
import sqlite3
from datetime import datetime
from urllib.parse import quote
import pymysql
import io
import json

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
        
        # 创建内存中的文件对象
        file_buffer = io.BytesIO(content)
        file_buffer.seek(0)
        
        # 返回流式响应
        return StreamingResponse(
            file_buffer,
            media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename=task_{task_id}.zip"
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to download report: {str(e)}")


@router.get(
    "/check-permission",
    summary="检查学生智能体使用权限",
    description="根据学生学号检查智能体使用权限状态"
)
async def check_agent_permission(
    student_id: str = Query(..., description="学生学号"),
    db: pymysql.connections.Connection = Depends(get_db)
):
    try:
        cursor = db.cursor()
        
        # 首先检查学生是否存在
        cursor.execute("SELECT student_id FROM students WHERE student_id = %s", (student_id,))
        student = cursor.fetchone()
        
        if not student:
            cursor.close()
            raise HTTPException(status_code=404, detail="学生不存在")
        
        # 直接从user_agent_permissions表中获取权限状态（使用学号作为查询条件）
        cursor.execute(
            "SELECT agent_permission FROM user_agent_permissions WHERE student_id = %s", 
            (student_id,)
        )
        permission = cursor.fetchone()
        cursor.close()
        
        # 如果没有权限记录，返回0（无权限）
        agent_permission = permission[0] if permission else 0
        
        # 返回权限状态（1-有权限，0-无权限）
        return {
            "student_id": student_id,
            "agent_permission": agent_permission
        }
        
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库操作失败：{str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"检查权限失败：{str(e)}")


@router.post(
    "/request-agent-permission",
    summary="请求智能体使用权限",
    description="学生向特定管理员发送消息请求获取智能体使用权限"
)
async def request_agent_permission(
    admin_id: str = Query(..., description="管理员ID（字符串格式，如 admin1）"),
    current_user: str = Query(..., description="当前用户信息(JSON字符串)，示例: {\"sub\":1,\"roles\":[\"student\"],\"username\":\"student1\"}"),
    db: pymysql.connections.Connection = Depends(get_db)
):
    try:
        # 解析当前用户信息
        try:
            import urllib.parse
            current_user = urllib.parse.unquote(current_user)
            user_info = json.loads(current_user)
        except urllib.error.URLError as e:
            raise HTTPException(status_code=400, detail=f"URL解码失败：{str(e)}")
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"JSON格式错误：{str(e)}")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"无效的用户信息格式：{str(e)}")
        
        # 验证用户角色
        roles = user_info.get("roles", [])
        user_id = user_info.get("sub")
        
        if not ("student" in roles or "学生" in roles):
            raise HTTPException(status_code=403, detail="仅学生可以申请智能体使用权限")
        
        # 获取学生信息
        cursor = db.cursor()
        cursor.execute("SELECT name, student_id FROM students WHERE id = %s", (user_id,))
        student = cursor.fetchone()
        if not student:
            raise HTTPException(status_code=404, detail="学生信息不存在")
        student_name, student_number = student
        
        # 构建消息内容
        message_title = "智能体使用权限申请"
        message_content = f"学生 {student_name} 请求获取智能体使用权限"
        
        # 生成消息ID
        message_id = f"agent_permission_{user_id}_{int(datetime.now().timestamp() * 1000)}"
        
        # 保存消息到user_messages表
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        metadata = {
            "sender_id": str(user_id),
            "sender_role": "student",
            "message_id": message_id,
            "request_type": "agent_permission"
        }
        metadata_json = json.dumps(metadata, ensure_ascii=False)
        
        # 验证管理员是否存在
        cursor.execute("SELECT admin_id FROM admins WHERE admin_id = %s", (admin_id,))
        admin = cursor.fetchone()
        
        if not admin:
            raise HTTPException(status_code=404, detail="指定的管理员不存在")
        
        # 向指定管理员发送消息
        cursor.execute(
            """
            INSERT INTO user_messages (
                user_id, username, title, content, source, status, 
                received_time, metadata, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                admin_id,  # 接收者ID（管理员）
                "",  # 接收者用户名（可为空）
                message_title,
                message_content,
                "system",  # 消息来源
                "unread",  # 状态
                now_str,
                metadata_json,
                now_str,
                now_str
            )
        )
        inserted_count = 1
        
        db.commit()
        cursor.close()
        
        return {
            "message": "权限申请已提交，等待管理员审批",
            "student_name": student_name,
            "student_number": student_number,
            "message_id": message_id
        }
        
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"数据库操作失败：{str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"申请权限失败：{str(e)}")


@router.post(
    "/handle-permission-request",
    summary="处理智能体使用权限请求",
    description="管理员处理学生的智能体使用权限请求，可选择同意或拒绝"
)
async def handle_permission_request(
    message_id: str = Query(..., description="消息ID，用于标识权限请求"),
    action: str = Query(..., description="操作：approve（同意）或 reject（拒绝）"),
    current_user: str = Query(..., description="当前用户信息(JSON字符串)，示例: {\"sub\":1,\"roles\":[\"admin\"],\"username\":\"admin1\"}"),
    db: pymysql.connections.Connection = Depends(get_db)
):
    try:
        # 解析当前用户信息
        try:
            import urllib.parse
            current_user = urllib.parse.unquote(current_user)
            user_info = json.loads(current_user)
        except urllib.error.URLError as e:
            raise HTTPException(status_code=400, detail=f"URL解码失败：{str(e)}")
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"JSON格式错误：{str(e)}")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"无效的用户信息格式：{str(e)}")
        
        # 验证用户角色
        roles = user_info.get("roles", [])
        if not ("admin" in roles or "管理员" in roles):
            raise HTTPException(status_code=403, detail="仅管理员可以处理权限请求")
        
        # 从current_user中获取管理员ID（自增ID）
        admin_id_num = user_info.get("sub")
        if not admin_id_num:
            raise HTTPException(status_code=400, detail="用户信息中缺少管理员ID")
        
        # 根据自增ID获取管理员的admin_id（字符串格式）
        cursor = db.cursor()
        cursor.execute("SELECT admin_id FROM admins WHERE id = %s", (admin_id_num,))
        admin = cursor.fetchone()
        if not admin:
            cursor.close()
            raise HTTPException(status_code=404, detail="管理员信息不存在")
        admin_id = admin[0]
        cursor.close()
        
        # 验证操作类型
        if action not in ["approve", "reject"]:
            raise HTTPException(status_code=400, detail="操作类型必须是 approve 或 reject")
        
        # 根据消息ID获取消息信息
        cursor = db.cursor()
        cursor.execute(
            "SELECT metadata, title, content FROM user_messages WHERE metadata LIKE %s",
            (f"%{message_id}%",)
        )
        message = cursor.fetchone()
        
        if not message:
            cursor.close()
            raise HTTPException(status_code=404, detail="权限请求消息不存在")
        
        metadata_json, message_title, message_content = message
        metadata = json.loads(metadata_json)
        
        # 从消息元数据中获取学生信息
        sender_id = metadata.get("sender_id")
        if not sender_id:
            cursor.close()
            raise HTTPException(status_code=400, detail="消息元数据中缺少发送者信息")
        
        # 获取学生学号
        cursor.execute("SELECT student_id, name FROM students WHERE id = %s", (sender_id,))
        student = cursor.fetchone()
        
        if not student:
            cursor.close()
            raise HTTPException(status_code=404, detail="学生信息不存在")
        
        student_id, student_name = student
        
        # 处理权限请求
        if action == "approve":
            # 同意权限请求，添加或更新权限记录
            cursor.execute(
                """
                INSERT INTO user_agent_permissions (student_id, admin_id, agent_permission, granted_at)
                VALUES (%s, %s, 1, NOW())
                ON DUPLICATE KEY UPDATE 
                    admin_id = %s, 
                    agent_permission = 1, 
                    granted_at = NOW(),
                    updated_at = NOW()
                """,
                (student_id, admin_id, admin_id)
            )
            action_message = "权限申请已批准，您现在可以使用智能体功能"
        else:
            # 拒绝权限请求，确保权限状态为0
            cursor.execute(
                """
                INSERT INTO user_agent_permissions (student_id, admin_id, agent_permission, granted_at)
                VALUES (%s, %s, 0, NOW())
                ON DUPLICATE KEY UPDATE 
                    admin_id = %s, 
                    agent_permission = 0, 
                    granted_at = NOW(),
                    updated_at = NOW()
                """,
                (student_id, admin_id, admin_id)
            )
            action_message = "权限申请已拒绝，您暂时无法使用智能体功能"
        
        # 标记消息为已读
        cursor.execute(
            "UPDATE user_messages SET status = 'read' WHERE metadata LIKE %s",
            (f"%{message_id}%",)
        )
        
        # 向学生发送反馈消息
        feedback_title = f"智能体使用权限申请{"批准" if action == "approve" else "拒绝"}"
        feedback_content = f"亲爱的 {student_name}：\n\n您的智能体使用权限申请已{"批准" if action == "approve" else "拒绝"}。\n\n{action_message}\n\n管理员"
        
        # 生成反馈消息ID
        feedback_message_id = f"permission_feedback_{sender_id}_{int(datetime.now().timestamp() * 1000)}"
        
        # 保存反馈消息
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        feedback_metadata = {
            "sender_id": admin_id,
            "sender_role": "admin",
            "message_id": feedback_message_id,
            "request_type": "permission_feedback",
            "original_message_id": message_id
        }
        feedback_metadata_json = json.dumps(feedback_metadata, ensure_ascii=False)
        
        cursor.execute(
            """
            INSERT INTO user_messages (
                user_id, username, title, content, source, status, 
                received_time, metadata, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                sender_id,  # 接收者ID（学生）
                "",  # 接收者用户名（可为空）
                feedback_title,
                feedback_content,
                "system",  # 消息来源
                "unread",  # 状态
                now_str,
                feedback_metadata_json,
                now_str,
                now_str
            )
        )
        
        db.commit()
        cursor.close()
        
        return {
            "message": f"权限请求已{"批准" if action == "approve" else "拒绝"}",
            "student_id": student_id,
            "student_name": student_name,
            "action": action,
            "admin_id": admin_id
        }
        
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"数据库操作失败：{str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"处理权限请求失败：{str(e)}")