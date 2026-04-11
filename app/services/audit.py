from __future__ import annotations

import json
import os
from typing import Optional

import aiohttp
import pymysql
from fastapi import HTTPException


AGENT_API_BASE = "http://127.0.0.1:8651"


def _parse_agent_response(payload):
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            try:
                nested = json.loads(detail)
            except json.JSONDecodeError:
                return payload
            if isinstance(nested, dict):
                return nested
        return payload

    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return {"status": payload}
        if isinstance(parsed, dict):
            return parsed

    raise HTTPException(status_code=500, detail="智能体响应格式无效")


def _ensure_tasks_status_column(db: pymysql.connections.Connection) -> None:
    cursor = None
    try:
        cursor = db.cursor()
        cursor.execute("SHOW COLUMNS FROM tasks LIKE 'status'")
        if cursor.fetchone():
            return
        cursor.execute(
            "ALTER TABLE tasks ADD COLUMN status VARCHAR(32) NOT NULL DEFAULT 'pending' COMMENT '任务状态' AFTER task_id"
        )
        db.commit()
    except pymysql.MySQLError as e:
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail=f"任务表结构更新失败: {str(e)}")
    finally:
        if cursor:
            cursor.close()


async def submit_audit_task(
    db: pymysql.connections.Connection,
    *,
    file_content: Optional[bytes] = None,
    filename: Optional[str] = None,
    paper_id: Optional[int] = None,
    version: Optional[str] = None,
    oss_key: Optional[str] = None,
    audit_config: str = '{"checks": ["grammar", "plagiarism"]}',
) -> dict:
    try:
        config = json.loads(audit_config)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid audit_config format")

    if file_content is None or not filename:
        if paper_id is None or version is None:
            raise HTTPException(status_code=400, detail="必须提供文件内容或论文ID和版本号")

        cursor = None
        try:
            cursor = db.cursor()
            cursor.execute(
                "SELECT id, version, oss_key FROM papers WHERE id = %s AND version = %s",
                (paper_id, version),
            )
            paper = cursor.fetchone()
            if not paper:
                raise HTTPException(status_code=404, detail=f"论文ID {paper_id} 版本 {version} 不存在")
            oss_key = paper[2]
            filename = os.path.basename(oss_key)
            if not os.path.exists(oss_key):
                raise HTTPException(status_code=404, detail=f"论文文件不存在: {oss_key}")
            with open(oss_key, "rb") as f:
                file_content = f.read()
        finally:
            if cursor:
                cursor.close()

    if file_content is None or not filename:
        raise HTTPException(status_code=400, detail="文件内容不能为空")

    async with aiohttp.ClientSession() as session:
        form_data = aiohttp.FormData()
        form_data.add_field("file", file_content, filename=filename)
        form_data.add_field("audit_config", json.dumps(config))

        async with session.post(f"{AGENT_API_BASE}/api/v1/audit", data=form_data) as response:
            if not 200 <= response.status < 203:
                raise HTTPException(status_code=response.status, detail=await response.text())
            result = await response.json()

    result = _parse_agent_response(result)
    task_id = result.get("task_id")
    status = result.get("status", "pending")
    if not task_id:
        raise HTTPException(status_code=500, detail="Failed to get task_id from agent API")

    mysql_inserted = False
    if paper_id is not None and version is not None:
        if not oss_key:
            oss_key = filename

        _ensure_tasks_status_column(db)

        cursor = None
        try:
            cursor = db.cursor()
            task_sql = """
            INSERT INTO tasks (
                task_id, paper_id, version, oss_key, status
            )
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                paper_id = VALUES(paper_id),
                version = VALUES(version),
                oss_key = VALUES(oss_key),
                status = VALUES(status)
            """
            cursor.execute(
                task_sql,
                (
                    task_id,
                    paper_id,
                    version,
                    oss_key,
                    status,
                ),
            )
            db.commit()

            cursor.execute(
                "SELECT task_id FROM tasks WHERE task_id = %s AND paper_id = %s AND version = %s",
                (task_id, paper_id, version),
            )
            mysql_inserted = cursor.fetchone() is not None
            if not mysql_inserted:
                raise HTTPException(status_code=500, detail="任务记录未成功写入 MySQL")
        except pymysql.MySQLError as e:
            if db:
                db.rollback()
            raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")
        finally:
            if cursor:
                cursor.close()

    return {"task_id": task_id, "status": status, "mysql_inserted": mysql_inserted}
