import zipfile
import urllib.parse
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Query,Body
from fastapi.responses import StreamingResponse
from typing import List, Optional, Dict
import os
import io
import sys
import shutil
import subprocess
import tempfile
from decimal import Decimal
from xml.sax.saxutils import escape
from app.schemas.document import (
    PaperOut,
    PaperStatusOut,
    VersionOut,
    DDLOut, 
)
from app.services.oss import get_file_from_oss, upload_paper_to_storage
from app.services.audit import submit_audit_task
from datetime import datetime
from app.database import get_db
import pymysql
import json
import csv

try:
    import pandas as pd
except ImportError:
    pd = None

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, Side
except ImportError:
    Workbook = None
    Alignment = None
    Border = None
    Font = None
    Side = None

try:
    from docx2pdf import convert as docx2pdf_convert
except ImportError:
    docx2pdf_convert = None

router = APIRouter()


def _parse_current_user(current_user: Optional[str]) -> dict:
    try:
        if not current_user:
            return {"sub": 0, "username": "", "roles": []}
        import urllib.parse
        raw = urllib.parse.unquote(current_user)
        if not raw.strip():
            return {"sub": 0, "username": "", "roles": []}
        if raw.isdigit():
            return {"sub": int(raw), "username": f"user{raw}", "roles": ["student"]}
        data = json.loads(raw)
        if isinstance(data, dict):
            sub_value = data.get("sub", 0)
            if isinstance(sub_value, str) and sub_value.isdigit():
                data["sub"] = int(sub_value)
            elif isinstance(sub_value, int):
                data["sub"] = sub_value
            else:
                data["sub"] = 0
            return data
    except Exception:
        pass
    return {"sub": 0, "username": "", "roles": []}

def _parse_version(version_str: str) -> tuple:
    try:
        version_clean = version_str.strip().lower().lstrip('v')
        major_str, minor_str = version_clean.split('.')
        major = int(major_str)
        minor = int(minor_str)
        if major < 0 or minor < 0:
            raise ValueError("版本号数字不能为负数")
        return (major, minor)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"版本号格式错误（示例：v2.0），要求为 v+数字.数字 格式，且数字为正整数：{str(e)}"
        )
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="版本号格式错误，必须符合 v+数字.数字 格式（如 v1.0、v2.1）"
        )


def _column_exists(cursor, table_name: str, column_name: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (table_name, column_name),
    )
    return cursor.fetchone() is not None


def _resolve_teacher_name(cursor, teacher_id: str) -> str:
    if not teacher_id:
        return ""
    cursor.execute("SELECT name FROM teachers WHERE teacher_id = %s", (teacher_id,))
    row = cursor.fetchone()
    if row and row[0]:
        return row[0]
    if teacher_id.isdigit():
        cursor.execute("SELECT name FROM teachers WHERE id = %s", (int(teacher_id),))
        row = cursor.fetchone()
        if row and row[0]:
            return row[0]
    return teacher_id or ""


def _normalize_group_id(group_id) -> str:
    if group_id is None:
        return ""
    return str(group_id).strip()


def _normalize_identifier(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _ids_match(left, right) -> bool:
    left_norm = _normalize_identifier(left)
    right_norm = _normalize_identifier(right)
    return bool(left_norm and right_norm and left_norm == right_norm)


def _find_soffice_binary() -> Optional[str]:
    for cmd in ("soffice", "libreoffice"):
        path = shutil.which(cmd)
        if path:
            return path
    return None

def convert_docx_to_pdf(docx_content: bytes, filename: str) -> tuple:
    pdf_filename = os.path.splitext(filename)[0] + '.pdf'
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            docx_path = os.path.join(tmpdir, os.path.basename(filename) or "input.docx")
            with open(docx_path, "wb") as temp_docx:
                temp_docx.write(docx_content)

            if sys.platform.startswith("linux"):
                soffice_bin = _find_soffice_binary()
                if not soffice_bin:
                    raise HTTPException(
                        status_code=500,
                        detail="DOCX转PDF失败：未找到LibreOffice（soffice/libreoffice）。请在Linux上安装LibreOffice后重试"
                    )
                cmd = [
                    soffice_bin,
                    "--headless",
                    "--nologo",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    tmpdir,
                    docx_path,
                ]
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if proc.returncode != 0:
                    raise HTTPException(
                        status_code=500,
                        detail=f"DOCX转PDF失败：LibreOffice 执行错误（code={proc.returncode}，stderr={proc.stderr.decode(errors='ignore').strip()[:400]} )"
                    )
                pdf_path = os.path.join(tmpdir, pdf_filename)
            else:
                if not docx2pdf_convert:
                    raise HTTPException(
                        status_code=500,
                        detail="DOCX转PDF失败：docx2pdf 未安装或不可用，请安装 docx2pdf 并确保本机有可用的 Word/LibreOffice"
                    )
                docx2pdf_convert(docx_path, tmpdir)
                pdf_path = os.path.join(tmpdir, pdf_filename)

            if not os.path.exists(pdf_path):
                raise HTTPException(
                    status_code=500,
                    detail="DOCX转PDF失败：未生成PDF文件，请检查转换工具安装情况"
                )

            with open(pdf_path, 'rb') as f:
                pdf_content = f.read()

        return pdf_content, pdf_filename
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"DOCX转PDF失败：{str(e)}。请确保已安装转换工具（Linux推荐安装LibreOffice）"
        )


@router.post(
    "/upload",
    response_model=PaperOut,
    summary="上传论文",
    description="上传 docx 生成论文记录与首个版本，并记录提交者信息"
)
async def upload_paper(
    file: UploadFile = File(...),
    owner_id: int = Query(..., description="论文归属者ID，必须传入且为有效整数"),
    teacher_id: str = Query(..., description="关联的老师ID，必须传入"),
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="提交者信息(JSON字符串，包含 sub/username/roles)"),
):
    current_user = _parse_current_user(current_user)
    submitter_id = current_user.get("sub", 0)  
    # 参数校验
    if not isinstance(owner_id, int) or owner_id <= 0:
        raise HTTPException(status_code=400, detail="owner_id必须是正整数")
    if not teacher_id or not teacher_id.strip():
        raise HTTPException(status_code=400, detail="teacher_id不能为空")
    if owner_id != submitter_id:
        raise HTTPException(
            status_code=403,
            detail="无权限上传：论文归属者ID必须与当前登录用户ID一致"
        )
    # 检查学生是否已经上传过论文
    cursor = None
    try:
        cursor = db.cursor()
        cursor.execute("SELECT id FROM papers WHERE owner_id = %s", (owner_id,))
        if cursor.fetchone():
            raise HTTPException(status_code=400, detail="每个学生只能上传一篇论文")
        teacher_name = _resolve_teacher_name(cursor, teacher_id)
        has_teacher_name = _column_exists(cursor, "papers", "teacher_name")
    finally:
        if cursor:
            cursor.close()
    # 验证文件扩展名
    if not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="仅支持 .docx 格式")
    contents = await file.read()
    size = len(contents)
    if size > 100 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件大小超过 100MB")

    # 本地存储论文到 doc/essay（返回路径作为 oss_key）
    oss_key = upload_paper_to_storage(file.filename, contents)
    
    # 转换docx到pdf并上传到OSS
    pdf_content, pdf_filename = convert_docx_to_pdf(contents, file.filename)
    pdf_oss_key = upload_paper_to_storage(pdf_filename, pdf_content)

    # 持久化到数据库：创建paper记录和初始版本v1.0
    cursor = None 
    try:
        cursor = db.cursor()
        submitter_name = current_user.get("username") or ""
        roles = current_user.get("roles") or []
        submitter_role = ",".join([str(r) for r in roles]) if isinstance(roles, list) else str(roles)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        version = "v1.0"
        if has_teacher_name:
            paper_sql = """
            INSERT INTO papers (
                owner_id, teacher_id, teacher_name, version, size, status, ddl, oss_key, pdf_oss_key,
                submitted_by_name, submitted_by_role, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            paper_values = (
                owner_id,
                teacher_id,
                teacher_name,
                version,
                size,
                "已上传",
                None,
                oss_key,
                pdf_oss_key,
                submitter_name,
                submitter_role,
                now,
                now,
            )
        else:
            paper_sql = """
            INSERT INTO papers (
                owner_id, teacher_id, version, size, status, ddl, oss_key, pdf_oss_key,
                submitted_by_name, submitted_by_role, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            paper_values = (
                owner_id,
                teacher_id,
                version,
                size,
                "已上传",
                None,
                oss_key,
                pdf_oss_key,
                submitter_name,
                submitter_role,
                now,
                now,
            )
        cursor.execute(paper_sql, paper_values)
        paper_id = cursor.lastrowid
        # 插入历史版本表
        history_sql = """
        INSERT INTO papers_history (
            paper_id, version, size, status, oss_key, pdf_oss_key,
            submitted_by_id, submitted_by_name, submitted_by_role,
            operated_by, operated_time, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(
            history_sql,
            (
                paper_id,
                version,
                size,
                "已上传",
                oss_key,
                pdf_oss_key, 
                str(submitter_id),  
                submitter_name,
                submitter_role,
                submitter_name or str(submitter_id), 
                now,
                now,
                now
            )
        )

        await submit_audit_task(
            db,
            file_content=contents,
            filename=file.filename,
            paper_id=paper_id,
            version=version,
            oss_key=oss_key,
            audit_config='{"checks": ["grammar", "plagiarism"]}',
        )
        db.commit()
    except pymysql.MySQLError as e:
        db.rollback() 
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")
    finally:
        if cursor: 
            cursor.close()

    return PaperOut(id=paper_id, owner_id=owner_id, teacher_id=teacher_id, latest_version=version, oss_key=oss_key, pdf_oss_key=pdf_oss_key)


@router.put(
    "/{paper_id}",
    response_model=PaperOut,
    summary="更新论文",
    description="上传新版本并更新论文的最新版本信息"
)
async def update_paper(
    paper_id: int,
    file: UploadFile = File(...),
    version: str = Query(..., description="新版本号（必填，格式如v2.0，必须大于当前最新版本）"),
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="提交者信息(JSON字符串，包含 sub/username/roles)"),
):
    current_user = _parse_current_user(current_user)
    submitter_id = current_user.get("sub", 0)
    # 文件校验
    if not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="仅支持 .docx 格式")
    contents = await file.read()
    size = len(contents)
    if size == 0:
        raise HTTPException(status_code=400, detail="文件为空")
    if size > 100 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件大小超过 100MB")

    cursor = None
    try:
        cursor = db.cursor()
        # 查询论文信息（仅查表中存在的字段）
        cursor.execute("SELECT owner_id, version, teacher_id FROM papers WHERE id = %s", (paper_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="论文不存在")
        paper_owner_id, current_version_str, teacher_id = row
        # 权限校验
        if paper_owner_id != submitter_id:
            raise HTTPException(status_code=403, detail="无权限更新该论文")
        # 版本号校验
        current_version = _parse_version(current_version_str)
        new_version = _parse_version(version)
        if new_version <= current_version:
            raise HTTPException(
                status_code=400,
                detail=f"新版本号必须大于当前最新版本号 {current_version_str}，当前提交的版本号 {version} 不符合要求"
            )
        
        # 上传文件
        oss_key = upload_paper_to_storage(file.filename, contents)
        pdf_content, pdf_filename = convert_docx_to_pdf(contents, file.filename)
        pdf_oss_key = upload_paper_to_storage(pdf_filename, pdf_content)

        # 数据库更新
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        submitter_name = current_user.get("username") or ""
        roles = current_user.get("roles") or []
        submitter_role = ",".join([str(r) for r in roles]) if isinstance(roles, list) else str(roles)

        cursor.execute(
            """
            UPDATE papers
            SET version = %s,
                size = %s,
                status = %s,
                submitted_by_name = %s,
                submitted_by_role = %s,
                oss_key = %s,
                pdf_oss_key = %s, 
                updated_at = %s,
                operated_by = %s,
                operated_time = %s
            WHERE id = %s
            """,
            (
                version,
                size,
                "已更新",
                submitter_name,
                submitter_role,
                oss_key,
                pdf_oss_key,
                now,
                submitter_name,
                now,
                paper_id,
            ),
        )
        # 插入历史版本
        history_sql = """
        INSERT INTO papers_history (
            paper_id, version, size, status, oss_key, pdf_oss_key,
            submitted_by_id, submitted_by_name, submitted_by_role,
            operated_by, operated_time, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(
            history_sql,
            (
                paper_id,
                version,
                size,
                "已更新",
                oss_key,
                pdf_oss_key, 
                str(submitter_id),
                submitter_name,
                submitter_role,
                submitter_name or str(submitter_id),
                now,
                now,
                now
            )
        )
        db.commit()
        return PaperOut(id=paper_id, owner_id=paper_owner_id, teacher_id=teacher_id, latest_version=version, oss_key=oss_key, pdf_oss_key=pdf_oss_key)
    except pymysql.MySQLError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")
    finally:
        if cursor:
            cursor.close()


@router.delete(
    "/{paper_id}",
    summary="删除论文",
    description="删除论文记录及其版本信息"
)
def delete_paper(
    paper_id: int,
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="提交者信息(JSON字符串，包含 sub/username/roles)"),
):
    current_user = _parse_current_user(current_user)
    current_id = current_user.get("sub", 0) 
    current_roles = current_user.get("roles", []) 
    if current_id == 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")

    cursor = None
    try:
        cursor = db.cursor()
        # 查询论文信息
        cursor.execute("SELECT owner_id, teacher_id FROM papers WHERE id = %s", (paper_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="论文不存在")
        paper_owner_id, teacher_id = row
        # 权限校验
        is_owner = (paper_owner_id == current_id)
        is_admin = ("admin" in current_roles) or ("管理员" in current_roles)
        if not is_owner and not is_admin:
            raise HTTPException(
                status_code=403,
                detail=f"无权限删除该论文：仅论文归属者（ID={paper_owner_id}）或管理员可删除，当前登录用户ID={current_id}，角色={current_roles}"
            )
        # 删除论文
        cursor.execute("DELETE FROM papers WHERE id = %s", (paper_id,))
        db.commit()
        delete_type = "归属者" if is_owner else "管理员"
        return {
            "message": f"论文及其所有版本信息删除成功（{delete_type}权限）",
            "paper_id": paper_id,
            "deleted_by": current_id,
            "deleted_by_role": current_roles,
            "paper_owner_id": paper_owner_id,
            "teacher_id": teacher_id
        }
    except pymysql.MySQLError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")
    finally:
        if cursor:
            cursor.close()


@router.post(
    "/{paper_id}/status",
    response_model=PaperStatusOut,
    summary="创建论文状态",
    description="为指定论文版本创建状态记录",
)
def create_paper_status(
    paper_id: int,
    status: str = Query(
        "待审阅",
        description="论文状态（仅支持待审阅，不可修改）",
        enum=["待审阅"],
        include_in_schema=False
    ),
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
):
    """Insert a status row for a paper if it does not exist."""
    current_user = _parse_current_user(current_user)
    login_user_id = int(current_user.get("sub", 0))
    if login_user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    status = "待审阅"
    cursor = None
    try:
        cursor = db.cursor()
        cursor.execute("SELECT owner_id, teacher_id, version, oss_key, pdf_oss_key, size FROM papers WHERE id = %s", (paper_id,))
        paper_info = cursor.fetchone()
        if not paper_info:
            raise HTTPException(status_code=404, detail="论文不存在")
        student_id, teacher_id, version, oss_key, pdf_oss_key, current_size = paper_info 
        cursor.execute(
            "SELECT status, size FROM papers WHERE id = %s",
            (paper_id,),
        )
        current_status_row = cursor.fetchone()
        if not current_status_row:
            raise HTTPException(status_code=404, detail="论文不存在")
        current_status, current_size = current_status_row
        if current_status != "已上传":
            raise HTTPException(status_code=400, detail=f"当前论文状态为【{current_status}】，仅状态为【已上传】时可创建待审阅状态")
        is_student = _ids_match(login_user_id, student_id)
        if not is_student:
            raise HTTPException(
                status_code=403,
                detail=f"仅该论文的学生（ID={student_id}）可创建待审阅状态，当前登录用户ID={login_user_id}"
            )
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        size = current_size or 0
        cursor.execute(
            """
            UPDATE papers
            SET status = %s,
                operated_by = %s,
                operated_time = %s,
                updated_at = %s
            WHERE id = %s
            """,
            (
                status,
                current_user.get("username") or str(login_user_id),
                now_str,
                now_str,
                paper_id,
            ),
        )
        history_sql = """
        INSERT INTO papers_history (
            paper_id, version, size, status, oss_key, pdf_oss_key,
            submitted_by_id, submitted_by_name, submitted_by_role,
            operated_by, operated_time, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute("SELECT submitted_by_name, submitted_by_role FROM papers WHERE id = %s", (paper_id,))
        origin_submit = cursor.fetchone()
        submitter_name, submitter_role = origin_submit if origin_submit else ("", "")
        cursor.execute(
            history_sql,
            (
                paper_id,
                version,
                size,
                status,
                oss_key,
                pdf_oss_key, 
                str(student_id), 
                submitter_name,
                submitter_role,
                current_user.get("username") or str(login_user_id),  # 本次操作人
                now_str,
                now_str,
                now_str
            )
        )
        db.commit()
        return PaperStatusOut(
            paper_id=paper_id,
            version=version,  
            status=status,
            size=size,
            updated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")
    finally:
        if cursor:
            cursor.close()


@router.put(
    "/{paper_id}/status",
    response_model=PaperStatusOut,
    summary="更新论文状态",
    description="更新指定论文版本的状态信息",
)
def update_paper_status(
    paper_id: int,
    status: str = Query(
        ...,
        description="论文状态（仅可选择：待审阅/已审阅/已更新/待更新/已定稿）",
        enum=["待审阅", "已审阅", "已更新", "待更新", "已定稿"]  
    ),
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
):
    """Update status for the latest version of an existing paper."""
    current_user = _parse_current_user(current_user)
    login_user_id = current_user.get("sub", 0)
    if login_user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")

    cursor = None
    try:
        cursor = db.cursor()
        cursor.execute(
            "SELECT owner_id, teacher_id, version, oss_key, pdf_oss_key, size FROM papers WHERE id = %s", 
            (paper_id,)
        )
        paper_info = cursor.fetchone()
        if not paper_info:
            raise HTTPException(status_code=404, detail="论文不存在")
        student_id, teacher_id, version, oss_key, pdf_oss_key, original_size = paper_info 
        cursor.execute(
            """
            SELECT size, status FROM papers
            WHERE id = %s
            """,
            (paper_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="论文不存在")
        original_size, current_status = row
        if not current_status:
            raise HTTPException(status_code=404, detail="该论文无有效状态记录，请先创建状态")
        
        is_student = _ids_match(login_user_id, student_id)
        is_teacher = _ids_match(login_user_id, teacher_id)
        status_rules = {
            "待审阅": {
                "student": ["待审阅"],     
                "teacher": ["已审阅", "已定稿"]  
            },
            "已审阅": {
                "student": ["已更新"],    
                "teacher": ["已审阅", "已定稿"]  
            },
            "已更新": {
                "student": ["已更新"],      
                "teacher": ["待更新", "已定稿"] 
            },
            "待更新": {
                "student": ["已更新"],
                "teacher": ["待更新", "已定稿"]
            },
            "已定稿": {
                "student": [],          
                "teacher": []            
            }
        }
        if not is_student and not is_teacher:
            raise HTTPException(
                status_code=403,
                detail=f"无权限更新状态：仅该论文的学生（ID={student_id}）或老师（ID={teacher_id}）可操作，当前登录用户ID={login_user_id}"
            )
        
        role_key = "student" if is_student else "teacher"
        allowed_target_status = status_rules.get(current_status, {}).get(role_key, [])
        if current_status == "已定稿":
            raise HTTPException(
                status_code=403,
                detail="论文最近有效状态为【已定稿】，不允许修改任何状态"
            )
        if status not in allowed_target_status:
            role_name = "学生" if is_student else "老师"
            raise HTTPException(
                status_code=400,
                detail=f"论文最近有效状态为【{current_status}】，{role_name}仅可选择状态：{allowed_target_status}，当前选择：{status}"
            )
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            """
            UPDATE papers
            SET status = %s,
                operated_by = %s,
                operated_time = %s,
                updated_at = %s
            WHERE id = %s
            """,
            (
                status,
                current_user.get("username") or str(login_user_id),
                now_str,
                now_str,
                paper_id,
            ),
        )
        history_sql = """
        INSERT INTO papers_history (
            paper_id, version, size, status, oss_key, pdf_oss_key,
            submitted_by_id, submitted_by_name, submitted_by_role,
            operated_by, operated_time, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute("SELECT submitted_by_name, submitted_by_role FROM papers WHERE id = %s", (paper_id,))
        origin_submit = cursor.fetchone()
        submitter_name, submitter_role = origin_submit if origin_submit else ("", "")
        cursor.execute(
            history_sql,
            (
                paper_id,
                version,
                original_size,
                status,
                oss_key,
                pdf_oss_key,
                str(student_id),
                submitter_name,
                submitter_role,
                current_user.get("username") or str(login_user_id),  # 本次状态更新操作人
                now_str,
                now_str,
                now_str
            )
        )
        db.commit()
        return PaperStatusOut(
            paper_id=paper_id,
            version=version, 
            status=status,
            size=original_size, 
            updated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")
    finally:
        if cursor:
            cursor.close()


@router.post(
    "/{paper_id}/review",
    summary="提交论文审阅",
    description="仅论文关联的教师可提交审阅内容，一个论文仅允许一条初始审阅记录（可通过更新接口修改）",
    response_model=dict
)
def submit_paper_review(
    paper_id: int,
    review_content: str = Body(..., description="审阅内容，非空字符串", min_length=1),
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
):
    current_user = _parse_current_user(current_user)
    login_user_id = current_user.get("sub", 0)
    login_user_roles = current_user.get("roles", [])
    if login_user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    if not ("teacher" in login_user_roles or "教师" in login_user_roles):
        raise HTTPException(status_code=403, detail="无权限提交审阅：仅教师角色可操作")
    
    cursor = None
    try:
        cursor = db.cursor()
        cursor.execute(
            "SELECT id, teacher_id FROM papers WHERE id = %s",
            (paper_id,)
        )
        paper_row = cursor.fetchone()
        if not paper_row:
            raise HTTPException(status_code=404, detail=f"论文ID {paper_id} 不存在")
        
        paper_db_id, paper_teacher_id = paper_row
        if not _ids_match(paper_teacher_id, login_user_id):
            raise HTTPException(
                status_code=403,
                detail=f"无权限提交审阅：论文ID {paper_id} 关联的教师ID为 {paper_teacher_id}，当前登录教师ID为 {login_user_id}"
            )
        cursor.execute(
            "SELECT id FROM paper_reviews WHERE paper_id = %s AND teacher_id = %s LIMIT 1",
            (paper_id, str(login_user_id))
        )
        existing_review = cursor.fetchone()
        if existing_review:
            raise HTTPException(
                status_code=400,
                detail=f"论文ID {paper_id} 已存在审阅记录（ID：{existing_review[0]}），如需修改请使用更新审阅接口"
            )
        
        # 获取教师姓名
        cursor.execute("SELECT name FROM teachers WHERE id = %s", (login_user_id,))
        teacher_row = cursor.fetchone()
        if not teacher_row:
            raise HTTPException(status_code=404, detail=f"教师ID {login_user_id} 不存在")
        teacher_name = teacher_row[0]
        
        now = datetime.now()
        review_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
        insert_sql = """
        INSERT INTO paper_reviews (
            paper_id, teacher_id, teacher_name, review_content, review_time, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(
            insert_sql,
            (
                paper_id,
                str(login_user_id),
                teacher_name,
                review_content,
                review_time_str,
                review_time_str,
                review_time_str
            )
        )
        review_id = cursor.lastrowid
        db.commit()
        
        return {
            "message": "审阅内容提交成功",
            "review_id": review_id,
            "paper_id": paper_id,
            "teacher_id": login_user_id,
            "review_time": review_time_str,
            "review_content": review_content
        }
    
    except pymysql.MySQLError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"提交审阅失败：数据库操作错误 - {str(e)}")
    finally:
        if cursor:
            cursor.close()


@router.put(
    "/{paper_id}/review",
    summary="更新论文审阅",
    description="仅论文关联的教师可更新自己提交的审阅内容",
    response_model=dict
)
def update_paper_review(
    paper_id: int,
    review_content: str = Body(..., description="更新后的审阅内容，非空字符串", min_length=1),
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
):
    current_user = _parse_current_user(current_user)
    login_user_id = current_user.get("sub", 0)
    login_user_roles = current_user.get("roles", [])
    if login_user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    if not ("teacher" in login_user_roles or "教师" in login_user_roles):
        raise HTTPException(status_code=403, detail="无权限更新审阅：仅教师角色可操作")
    
    cursor = None
    try:
        cursor = db.cursor()
        cursor.execute(
            "SELECT id, teacher_id FROM papers WHERE id = %s",
            (paper_id,)
        )
        paper_row = cursor.fetchone()
        if not paper_row:
            raise HTTPException(status_code=404, detail=f"论文ID {paper_id} 不存在")
        
        paper_db_id, paper_teacher_id = paper_row
        if not _ids_match(paper_teacher_id, login_user_id):
            raise HTTPException(
                status_code=403,
                detail=f"无权限更新审阅：论文ID {paper_id} 关联的教师ID为 {paper_teacher_id}，当前登录教师ID为 {login_user_id}"
            )
        cursor.execute(
            "SELECT id, review_content FROM paper_reviews WHERE paper_id = %s AND teacher_id = %s LIMIT 1",
            (paper_id, str(login_user_id))
        )
        review_row = cursor.fetchone()
        if not review_row:
            raise HTTPException(
                status_code=404,
                detail=f"论文ID {paper_id} 暂无审阅记录，无法更新（请先提交审阅）"
            )
        
        review_id, old_content = review_row
        now = datetime.now()
        update_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
        update_sql = """
        UPDATE paper_reviews 
        SET review_content = %s, updated_time = %s, updated_at = %s
        WHERE id = %s AND paper_id = %s AND teacher_id = %s
        """
        cursor.execute(
            update_sql,
            (
                review_content,
                update_time_str,
                update_time_str,
                review_id,
                paper_id,
                str(login_user_id)
            )
        )
        db.commit()
        
        return {
            "message": "审阅内容更新成功",
            "review_id": review_id,
            "paper_id": paper_id,
            "teacher_id": login_user_id,
            "old_review_content": old_content,
            "new_review_content": review_content,
            "updated_time": update_time_str
        }
    
    except pymysql.MySQLError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"更新审阅失败：数据库操作错误 - {str(e)}")
    finally:
        if cursor:
            cursor.close()


@router.get(
    "/{paper_id}/review",
    summary="查看论文审阅内容",
    description="仅论文关联的学生（owner_id）或教师（teacher_id）可查看对应的审阅内容",
    response_model=dict
)
def get_paper_review(
    paper_id: int,
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
):
    # 解析当前登录用户信息
    current_user = _parse_current_user(current_user)
    login_user_id = current_user.get("sub", 0)
    login_user_roles = current_user.get("roles", [])
    
    # 1. 基础登录校验
    if login_user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    
    cursor = None
    try:
        cursor = db.cursor()
        
        # 2. 查询论文基础信息（匹配papers表的实际字段：owner_id=学生ID，teacher_id=教师ID）
        cursor.execute(
            "SELECT id, owner_id, teacher_id FROM papers WHERE id = %s",
            (paper_id,)
        )
        paper_row = cursor.fetchone()
        if not paper_row:
            raise HTTPException(status_code=404, detail=f"论文ID {paper_id} 不存在")
        
        paper_db_id, paper_owner_id, paper_teacher_id = paper_row
        
        # 3. 权限校验：仅论文关联的教师/学生可查看
        is_teacher = "teacher" in login_user_roles or "教师" in login_user_roles
        is_student = "student" in login_user_roles or "学生" in login_user_roles
        
        permission_denied = True
        # 教师权限：角色是教师 + 登录ID匹配论文的teacher_id
        if is_teacher and _ids_match(login_user_id, paper_teacher_id):
            permission_denied = False
        # 学生权限：角色是学生 + 登录ID匹配论文的owner_id
        if is_student and _ids_match(login_user_id, paper_owner_id):
            permission_denied = False
        
        if permission_denied:
            raise HTTPException(
                status_code=403,
                detail=f"无权限查看审阅：仅论文ID {paper_id} 关联的教师(ID:{paper_teacher_id})或学生(ID:{paper_owner_id})可查看"
            )
        
        # 4. 查询审阅记录（移除teacher_name字段）
        cursor.execute(
            """
            SELECT id, paper_id, teacher_id, review_content, 
                   review_time, updated_time, created_at, updated_at 
            FROM paper_reviews 
            WHERE paper_id = %s LIMIT 1
            """,
            (paper_id,)
        )
        review_row = cursor.fetchone()
        if not review_row:
            raise HTTPException(status_code=404, detail=f"论文ID {paper_id} 暂无审阅记录")
        
        # 5. 构造返回数据（格式化时间字段，移除teacher_name）
        review_id, r_paper_id, r_teacher_id, r_review_content, \
        r_review_time, r_updated_time, r_created_at, r_updated_at = review_row
        
        # 格式化datetime对象为字符串，兼容NULL值
        def format_datetime(dt):
            if isinstance(dt, datetime):
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            return dt  # 若为NULL则直接返回
        
        return {
            "message": "审阅内容查询成功",
            "review_id": review_id,
            "paper_id": r_paper_id,
            "teacher_id": r_teacher_id,
            "review_content": r_review_content,
            "review_time": format_datetime(r_review_time),
            "updated_time": format_datetime(r_updated_time),
            "created_at": format_datetime(r_created_at),
            "updated_at": format_datetime(r_updated_at)
        }
    
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"查询审阅失败：数据库操作错误 - {str(e)}")
    finally:
        if cursor:
            cursor.close()


@router.get(
    "/{paper_id}/versions",
    response_model=List[VersionOut],
    summary="查询论文版本列表",
    description="按时间倒序返回指定论文的版本信息"
)
def list_versions(
    paper_id: int,
    # current_user=Depends(get_current_user),  # 保留验证代码，注释掉
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="提交者信息(JSON字符串，包含 sub/username/roles)"),
):
    current_user = _parse_current_user(current_user)
    submitter_id = current_user.get("sub", 0)
    current_roles = current_user.get("roles", [])
    if submitter_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再查看论文版本")
    
    # 实际业务逻辑：查询该paper_id对应的版本列表
    cursor = None
    try:
        cursor = db.cursor()
        check_owner_sql = "SELECT owner_id, teacher_id FROM papers WHERE id = %s"
        cursor.execute(check_owner_sql, (paper_id,))
        paper_info = cursor.fetchone()
        if not paper_info:
            raise HTTPException(status_code=404, detail="论文不存在")
        paper_owner_id, paper_teacher_id = paper_info
        
        is_owner = _ids_match(paper_owner_id, submitter_id)
        is_teacher = _ids_match(paper_teacher_id, submitter_id)
        is_admin = ("admin" in current_roles) or ("管理员" in current_roles)
        if not is_owner and not is_teacher and not is_admin:
            raise HTTPException(
                status_code=403,
                detail=f"无权限查看该论文版本：仅论文归属者（ID={paper_owner_id}）、关联老师（ID={paper_teacher_id}）或管理员可查看，当前登录用户ID={submitter_id}，角色={current_roles}"
            )
        
        # 查询历史版本表
        version_sql = """
        SELECT version, size, created_at, status
        FROM papers_history
        WHERE paper_id = %s
        ORDER BY created_at DESC
        """
        cursor.execute(version_sql, (paper_id,))
        versions = cursor.fetchall()
        # 组装返回数据
        result = []
        for version in versions:
            result.append(VersionOut(
                version=version[0],
                size=version[1],
                created_at=version[2].strftime("%Y-%m-%dT%H:%M:%SZ"),  # 格式化时间
                status=version[3]
            ))
        return result
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库查询失败: {str(e)}")
    finally:
        if cursor:
            cursor.close()
    return []


@router.get(
    "/list",
    response_model=List[PaperOut],
    summary="查询当前用户所有论文",
    description="输入学生ID，仅当与登录用户ID一致时返回该学生的所有论文基础信息"
)
async def list_student_papers(
    owner_id: int = Query(..., description="要查询的学生ID（论文所有者ID），必须传入且为有效整数"),
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
):
    current_user = _parse_current_user(current_user)
    login_user_id = current_user.get("sub", 0)  
    current_roles = current_user.get("roles", [])
    
    # 1. 参数校验
    if not isinstance(owner_id, int) or owner_id <= 0:
        raise HTTPException(status_code=400, detail="owner_id必须是正整数")
    
    # 2. 权限校验
    cursor_check = None
    try:
        cursor_check = db.cursor()
        # 查询该学生论文关联的教师ID（用于判断是否是指导老师）
        cursor_check.execute("SELECT teacher_id FROM papers WHERE owner_id = %s LIMIT 1", (owner_id,))
        paper_teacher_id = cursor_check.fetchone()
        paper_teacher_id = paper_teacher_id[0] if paper_teacher_id else 0
        
        # 权限判断：本人/指导老师/管理员
        is_owner = _ids_match(owner_id, login_user_id)
        is_teacher = _ids_match(paper_teacher_id, login_user_id)
        is_admin = ("admin" in current_roles) or ("管理员" in current_roles)
        
        if not is_owner and not is_teacher and not is_admin:
            raise HTTPException(
                status_code=403,
                detail=f"无权限查询：仅可查询本人论文、本人指导的学生论文或管理员查询，传入的owner_id({owner_id})与登录用户ID({login_user_id})不一致，且非该学生的指导老师/管理员"
            )
    finally:
        if cursor_check:
            cursor_check.close()
    
    # 3. 数据库查询（新增 pdf_oss_key，修正 latest_version 为 version）
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor) 
        query_sql = """
        SELECT id, owner_id, teacher_id, version, oss_key, pdf_oss_key, created_at, updated_at
        FROM papers 
        WHERE owner_id = %s 
        ORDER BY created_at DESC
        """
        cursor.execute(query_sql, (owner_id,))
        paper_records = cursor.fetchall()
        
        # 4. 构造返回结果（新增 pdf_oss_key 字段，version 映射为 latest_version）
        result = []
        for record in paper_records:
            result.append(
                PaperOut(
                    id=record["id"],
                    owner_id=record["owner_id"],
                    teacher_id=record["teacher_id"],
                    latest_version=record["version"],  # 数据库的 version 对应响应的 latest_version
                    oss_key=record["oss_key"],
                    pdf_oss_key=record["pdf_oss_key"]  # 新增返回 PDF 存储键
                )
            )
        return result
    
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库查询失败: {str(e)}")
    finally:
        if cursor:
            cursor.close()


@router.get(
    "/{paper_id}/download",
    summary="下载论文",
    description="下载论文最新版本文件"
)
def download_paper(
    paper_id: int,
    student_id: int = Query(..., description="待下载论文归属的学生ID"),
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
):
    current_user = _parse_current_user(current_user)
    login_user_id = current_user.get("sub", 0)
    login_user_roles = current_user.get("roles", [])
    if login_user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    cursor = None
    try:
        cursor = db.cursor()
        cursor.execute(
            "SELECT owner_id, teacher_id, version, oss_key FROM papers WHERE id = %s",
            (paper_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="论文不存在")
        paper_owner_id, teacher_id, latest_version, oss_key = row
        if not _ids_match(paper_owner_id, student_id):
            raise HTTPException(
                status_code=400,
                detail=f"传入的学生ID({student_id})与论文归属者ID({paper_owner_id})不一致"
            )
        is_student = _ids_match(login_user_id, paper_owner_id)
        is_teacher = _ids_match(login_user_id, teacher_id)
        is_admin = ("admin" in login_user_roles) or ("管理员" in login_user_roles)  # 管理员
        if not is_student and not is_teacher and not is_admin:
            raise HTTPException(
                status_code=403,
                detail=f"无权限下载该论文：仅论文归属学生(ID={paper_owner_id})、关联老师(ID={teacher_id})或管理员可下载，当前登录用户ID={login_user_id}"
            )
        if not oss_key:
            raise HTTPException(status_code=404, detail="论文文件不存在（无存储路径）")
        try:
            docx_filename, docx_content = get_file_from_oss(oss_key)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"获取论文文件失败：{str(e)}")
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zip_file:
            pure_docx_name = os.path.basename(docx_filename)
            safe_docx_name = pure_docx_name.replace(" ", "_").replace("/", "_").replace("\\", "_")
            zip_inner_filename = f"paper_{paper_id}_v{latest_version.lstrip('v')}_{safe_docx_name}"
            zip_file.writestr(zip_inner_filename, docx_content)
        zip_buffer.seek(0)
        chinese_zip_name = f"论文_{paper_id}_v{latest_version.lstrip('v')}_{datetime.now().strftime('%Y%m%d')}.zip"
        safe_zip_name = f"paper_{paper_id}_v{latest_version.lstrip('v')}_{datetime.now().strftime('%Y%m%d')}.zip"
        encoded_chinese_name = urllib.parse.quote(chinese_zip_name, encoding='utf-8')
        headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_chinese_name}; filename={safe_zip_name}",
            "Content-Type": "application/zip",
            "X-Content-Type-Options": "nosniff"  
        }
        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers=headers
        )
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库查询失败: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"下载论文失败：{str(e)}")
    finally:
        if cursor:
            cursor.close()


@router.post(
    "/ddl/create",
    response_model=DDLOut,
    summary="创建DDL截止时间",
    description="仅教师可创建，且登录用户ID必须与教师ID一致，截止时间需精确到年月日时分秒，需指定群组ID发送给该群组所有人"
)
def create_ddl(
    year: str = Query(
        ..., 
        description="DDL年份（可选值：2024-2100）",
        enum=[str(y) for y in range(2024, 2101)]
    ),
    month: str = Query(
        ..., 
        description="DDL月份（可选值：1-12）",
        enum=[str(m) for m in range(1, 13)]
    ),
    day: str = Query(
        ..., 
        description="DDL日期（可选值：1-31）",
        enum=[str(d) for d in range(1, 32)]
    ),
    hour: str = Query(
        ..., 
        description="DDL小时（可选值：0-23）",
        enum=[str(h) for h in range(0, 24)]
    ),
    minute: str = Query(
        ..., 
        description="DDL分钟（可选值：0-59）",
        enum=[str(m) for m in range(0, 60)]
    ),
    second: str = Query(
        ..., 
        description="DDL秒数（可选值：0-59）",
        enum=[str(s) for s in range(0, 60)]
    ),
    teacher_id: int = Query(..., description="教师ID（必须为正整数）"),
    group_id: str = Query(..., description="群组ID（对应groups表的group_id）"),
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
):
    current_user = _parse_current_user(current_user)
    login_user_id = current_user.get("sub", 0)
    login_user_roles = current_user.get("roles", [])
    teacher_name = current_user.get("username", "") 
    group_id = _normalize_group_id(group_id)
    # 基础校验
    if not teacher_name:
        raise HTTPException(status_code=400, detail="教师姓名不能为空")

    if login_user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    if "teacher" not in login_user_roles and "教师" not in login_user_roles:
        raise HTTPException(status_code=403, detail="无权限创建DDL：仅教师角色可操作")
    if not isinstance(teacher_id, int) or teacher_id <= 0:
        raise HTTPException(status_code=400, detail="teacher_id必须是正整数")
    if teacher_id != login_user_id:
        raise HTTPException(
            status_code=403,
            detail=f"无权限创建DDL：传入的教师ID({teacher_id})与登录用户ID({login_user_id})不一致"
        )
    # 验证group_id
    if not group_id:
        raise HTTPException(status_code=400, detail="group_id不能为空")
    # 时间参数转换与校验
    try:
        year_int = int(year)
        month_int = int(month)
        day_int = int(day)
        hour_int = int(hour)
        minute_int = int(minute)
        second_int = int(second)
    except ValueError:
        raise HTTPException(status_code=400, detail="时间参数格式错误，必须为数字")
    try:
        ddl_time = datetime(year_int, month_int, day_int, hour_int, minute_int, second_int)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"非法的日期时间组合：{str(e)}")
    now = datetime.now()
    if ddl_time < now:
        raise HTTPException(status_code=400, detail="DDL截止时间不能早于当前时间")
    # 数据库操作
    cursor = None
    try:
        cursor = db.cursor()
        # 验证group_id是否存在于groups表
        cursor.execute("SELECT group_id, group_name FROM `groups` WHERE group_id = %s", (group_id,))
        group_info = cursor.fetchone()
        if not group_info:
            raise HTTPException(status_code=404, detail=f"群组ID {group_id} 不存在")
        group_name = group_info[1]
        
        # 获取群组所有成员
        cursor.execute("SELECT member_id, member_type FROM group_members WHERE group_id = %s AND is_active = 1", (group_id,))
        members = cursor.fetchall()
        
        # 检查群组是否已经有DDL（在ddl_management表中查找）
        cursor.execute("SELECT ddlid FROM ddl_management WHERE group_id = %s", (group_id,))
        if cursor.fetchone():
            raise HTTPException(status_code=400, detail="已有DDL存在，无法创建新的DDL")
        
        # 创建DDL记录
        create_sql = """
        INSERT INTO ddl_management (teacher_id, teacher_name, group_id, ddl_time, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """
        create_time = now.strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            create_sql, 
            (teacher_id, teacher_name, group_id, ddl_time, create_time, create_time)
        )
        ddlid = cursor.lastrowid
        
        # 发送消息给群组所有成员
        for member_id, member_type in members:
            # 构造消息内容
            message_title = f"【DDL通知】{group_name}"
            message_content = f"尊敬的用户，您所在的群组 {group_name} 已设置新的DDL截止时间：{ddl_time.strftime('%Y-%m-%d %H:%M:%S')}，请及时完成任务。"
            
            # 插入消息记录
            message_sql = """
            INSERT INTO user_messages (user_id, username, title, content, source, status, received_time, created_at, updated_at, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            metadata = json.dumps({"ddlid": ddlid, "group_id": group_id, "group_name": group_name})
            cursor.execute(
                message_sql, 
                (str(member_id), f"{member_type}_{member_id}", message_title, message_content, "ddl", "unread", create_time, create_time, create_time, metadata)
            )
        
        db.commit()
        return DDLOut(
            ddlid=ddlid,
            creator_id=teacher_id,
            teacher_id=teacher_id,
            ddl_time=ddl_time.strftime("%Y-%m-%d %H:%M:%S"),
            created_at=create_time
        )
    except pymysql.MySQLError as e:
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail=f"创建DDL失败：{str(e)}")
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass

@router.get(
    "/ddl/list",
    summary="查看DDL列表",
    description="根据群组ID查询对应的DDL截止时间"
)
def list_ddl(
    group_id: str = Query(..., description="群组ID（查询该群组的DDL，字符串类型）"),
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
):
    current_user = _parse_current_user(current_user)
    login_user_id = current_user.get("sub", 0)
    login_user_roles = current_user.get("roles", [])
    group_id = _normalize_group_id(group_id)
    # 基础校验
    if login_user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    if not group_id:
        raise HTTPException(status_code=400, detail="group_id不能为空")
    # 数据库查询
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        query_sql = """
        SELECT ddlid, teacher_id, teacher_name, group_id, ddl_time, created_at, updated_at
        FROM ddl_management 
        WHERE group_id = %s 
        ORDER BY ddl_time DESC
        """
        cursor.execute(query_sql, (group_id,))
        ddl_records = cursor.fetchall()
        result = []
        for record in ddl_records:
            # 处理datetime对象转字符串
            ddl_time_str = record["ddl_time"].strftime("%Y-%m-%d %H:%M:%S") if isinstance(record["ddl_time"], datetime) else record["ddl_time"]
            created_at_str = record["created_at"].strftime("%Y-%m-%d %H:%M:%S") if isinstance(record["created_at"], datetime) else record["created_at"]
            
            result.append({
                "ddlid": record["ddlid"],
                "teacher_id": record["teacher_id"],
                "teacher_name": record["teacher_name"],
                "group_id": record["group_id"],
                "ddl_time": ddl_time_str,
                "created_at": created_at_str
            })
        return result
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"查询DDL失败：{str(e)}")
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass

@router.get(
    "/ddl/received",
    summary="查看收到的DDL列表",
    description="接收者查看自己收到的DDL消息列表"
)
def list_received_ddl(
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
):
    current_user = _parse_current_user(current_user)
    login_user_id = current_user.get("sub", 0)
    # 基础校验
    if login_user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    # 数据库查询
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        query_sql = """
        SELECT id, title, content, received_time, status
        FROM user_messages 
        WHERE user_id = %s AND source = 'ddl'
        ORDER BY received_time DESC
        """
        cursor.execute(query_sql, (str(login_user_id),))
        ddl_messages = cursor.fetchall()
        result = []
        for message in ddl_messages:
            # 处理datetime对象转字符串
            received_time_str = message["received_time"].strftime("%Y-%m-%d %H:%M:%S") if isinstance(message["received_time"], datetime) else message["received_time"]
            
            result.append({
                "message_id": message["id"],
                "title": message["title"],
                "content": message["content"],
                "received_time": received_time_str,
                "status": message["status"]
            })
        return result
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"查询收到的DDL失败：{str(e)}")
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass


@router.post(
    "/ddl/cleanup",
    response_model=Dict[str, str],
    summary="清理过期DDL",
    description="自动清理过期的DDL（截止日期的后一天）"
)
def cleanup_expired_ddl(
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
):
    current_user = _parse_current_user(current_user)
    login_user_id = current_user.get("sub", 0)
    login_user_roles = current_user.get("roles", [])
    
    # 基础校验
    if login_user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    
    # 权限校验：仅管理员可执行清理操作
    if "admin" not in login_user_roles and "管理员" not in login_user_roles:
        raise HTTPException(status_code=403, detail="无权限执行DDL清理操作：仅管理员可操作")
    
    # 数据库操作
    cursor = None
    try:
        cursor = db.cursor()
        
        # 计算截止日期：当前日期的前一天（即截止日期的后一天）
        from datetime import datetime, timedelta
        cutoff_date = datetime.now() - timedelta(days=1)
        cutoff_date_str = cutoff_date.strftime("%Y-%m-%d 23:59:59")
        
        # 1. 查询过期的DDL
        cursor.execute(
            "SELECT ddlid, teacher_id, teacher_name, ddl_time FROM ddl_management WHERE ddl_time <= %s",
            (cutoff_date_str,)
        )
        expired_ddls = cursor.fetchall()
        
        if not expired_ddls:
            return {"message": "没有过期的DDL需要清理"}
        
        # 2. 为每个过期的DDL执行清理
        deleted_ddl_count = 0
        deleted_message_count = 0
        
        for ddlid, teacher_id, teacher_name, ddl_time in expired_ddls:
            try:
                # 开始事务
                db.begin()
                
                # a. 删除与该DDL相关的用户消息
                # 由于DDL消息是发送给群组所有成员的，我们删除所有source为'ddl'的消息
                cursor.execute(
                    "DELETE FROM user_messages WHERE source = 'ddl'"
                )
                deleted_messages = cursor.rowcount
                deleted_message_count += deleted_messages
                
                # b. 删除DDL记录
                cursor.execute(
                    "DELETE FROM ddl_management WHERE ddlid = %s",
                    (ddlid,)
                )
                if cursor.rowcount > 0:
                    deleted_ddl_count += 1
                
                # 提交事务
                db.commit()
                
            except Exception:
                # 回滚事务
                if db:
                    db.rollback()
                continue
        
        return {
            "message": f"清理完成，共删除 {deleted_ddl_count} 个过期DDL，{deleted_message_count} 条相关消息",
            "deleted_ddl_count": deleted_ddl_count,
            "deleted_message_count": deleted_message_count
        }
    
    except pymysql.MySQLError as e:
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail=f"清理DDL失败：{str(e)}")
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass


@router.delete(
    "/ddl/{ddlid}",
    response_model=Dict[str, str],
    summary="删除DDL",
    description="仅创建DDL的教师或管理员可删除"
)
def delete_ddl(
    ddlid: int,
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
):
    current_user = _parse_current_user(current_user)
    login_user_id = current_user.get("sub", 0)
    login_user_roles = current_user.get("roles", [])
    # 基础校验
    if login_user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    if not isinstance(ddlid, int) or ddlid <= 0:
        raise HTTPException(status_code=400, detail="ddlid必须是正整数")
    # 数据库操作
    cursor = None
    try:
        cursor = db.cursor()
        # 校验DDL是否存在并获取创建者
        check_sql = "SELECT teacher_id, teacher_name FROM ddl_management WHERE ddlid = %s"
        cursor.execute(check_sql, (ddlid,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"DDL ID {ddlid} 不存在")
        ddl_teacher_id, ddl_teacher_name = row
        # 权限校验
        is_admin = "admin" in login_user_roles or "管理员" in login_user_roles
        is_owner = _ids_match(ddl_teacher_id, login_user_id)
        
        if not is_owner and not is_admin:
            raise HTTPException(
                status_code=403,
                detail=f"无权限删除：仅创建该DDL的教师（ID={ddl_teacher_id}）或管理员可删除，当前登录用户ID={login_user_id}"
            )
        
        # 获取group_id和ddl_time用于匹配消息
        group_id = None
        ddl_time_str = None
        try:
            cursor.execute("SELECT group_id, ddl_time FROM ddl_management WHERE ddlid = %s", (ddlid,))
            row = cursor.fetchone()
            if row:
                group_id = row[0]
                ddl_time = row[1]
                if ddl_time:
                    ddl_time_str = ddl_time.strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            pass
        
        # 删除操作
        # 先删除相关的消息记录
        deleted_messages_count = 0
        
        # 方式1：通过metadata中的ddlid匹配
        if ddlid:
            delete_messages_sql = "DELETE FROM user_messages WHERE source = 'ddl' AND metadata LIKE %s"
            cursor.execute(delete_messages_sql, (f'%\"ddlid\": {ddlid}%',))
            deleted_messages_count = cursor.rowcount
        
        # 方式2：如果metadata方式没找到，通过group_id匹配
        if deleted_messages_count == 0 and group_id:
            delete_messages_sql = "DELETE FROM user_messages WHERE source = 'ddl' AND metadata LIKE %s"
            group_id_fragment = json.dumps({"group_id": str(group_id)}, ensure_ascii=False)[1:-1]
            cursor.execute(delete_messages_sql, (f'%{group_id_fragment}%',))
            deleted_messages_count = cursor.rowcount
        
        # 方式3：如果metadata方式没找到，尝试通过消息内容匹配
        if deleted_messages_count == 0 and ddl_time_str:
            delete_messages_sql = "DELETE FROM user_messages WHERE source = 'ddl' AND content LIKE %s"
            cursor.execute(delete_messages_sql, (f'%{ddl_time_str}%',))
            deleted_messages_count = cursor.rowcount
        
        # 再删除DDL记录
        delete_sql = "DELETE FROM ddl_management WHERE ddlid = %s"
        cursor.execute(delete_sql, (ddlid,))
        db.commit()
        
        return {
            "message": f"DDL {ddlid} 删除成功",
            "ddlid": str(ddlid),
            "deleted_messages_count": str(deleted_messages_count),
            "deleted_by": str(login_user_id),
            "deleted_by_role": ",".join(login_user_roles) if login_user_roles else "",
            "deleted_teacher_info": f"教师ID:{ddl_teacher_id},教师姓名:{ddl_teacher_name}"
        }
    except pymysql.MySQLError as e:
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail=f"删除DDL失败：{str(e)}")
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass

@router.put(
    "/ddl/{ddlid}",
    response_model=DDLOut,
    summary="更新DDL截止时间",
    description="仅创建该DDL的教师可更新，或管理员可更新，新截止时间需晚于当前时间"
)
def update_ddl(
    ddlid: int,
    year: str = Query(
        ..., 
        description="新DDL年份（可选值：2024-2100）",
        enum=[str(y) for y in range(2024, 2101)]
    ),
    month: str = Query(
        ..., 
        description="新DDL月份（可选值：1-12）",
        enum=[str(m) for m in range(1, 13)]
    ),
    day: str = Query(
        ..., 
        description="新DDL日期（可选值：1-31）",
        enum=[str(d) for d in range(1, 32)]
    ),
    hour: str = Query(
        ..., 
        description="新DDL小时（可选值：0-23）",
        enum=[str(h) for h in range(0, 24)]
    ),
    minute: str = Query(
        ..., 
        description="新DDL分钟（可选值：0-59）",
        enum=[str(m) for m in range(0, 60)]
    ),
    second: str = Query(
        ..., 
        description="新DDL秒数（可选值：0-59）",
        enum=[str(s) for s in range(0, 60)]
    ),
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
):
    current_user = _parse_current_user(current_user)
    login_user_id = current_user.get("sub", 0)
    login_user_roles = current_user.get("roles", [])
    
    # 基础校验
    if login_user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    if not isinstance(ddlid, int) or ddlid <= 0:
        raise HTTPException(status_code=400, detail="ddlid必须是正整数")
    
    # 时间参数转换与校验
    try:
        year_int = int(year)
        month_int = int(month)
        day_int = int(day)
        hour_int = int(hour)
        minute_int = int(minute)
        second_int = int(second)
    except ValueError:
        raise HTTPException(status_code=400, detail="时间参数格式错误，必须为数字")
    
    try:
        new_ddl_time = datetime(year_int, month_int, day_int, hour_int, minute_int, second_int)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"非法的日期时间组合：{str(e)}")
    
    now = datetime.now()
    if new_ddl_time < now:
        raise HTTPException(status_code=400, detail="新DDL截止时间不能早于当前时间")
    
    # 数据库操作
    cursor = None
    try:
        cursor = db.cursor()
        # 1. 校验DDL是否存在并获取创建者
        check_sql = "SELECT teacher_id, teacher_name, created_at FROM ddl_management WHERE ddlid = %s"
        cursor.execute(check_sql, (ddlid,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"DDL ID {ddlid} 不存在")
        ddl_teacher_id, ddl_teacher_name, created_at = row
        
        # 2. 权限校验
        is_admin = "admin" in login_user_roles or "管理员" in login_user_roles
        is_owner = _ids_match(ddl_teacher_id, login_user_id)
        
        if not is_owner and not is_admin:
            raise HTTPException(
                status_code=403,
                detail=f"无权限更新：仅创建该DDL的教师（ID={ddl_teacher_id}）或管理员可更新，当前登录用户ID={login_user_id}"
            )
        
        # 3. 更新DDL时间
        update_time = now.strftime("%Y-%m-%d %H:%M:%S")
        update_sql = """
        UPDATE ddl_management 
        SET ddl_time = %s, updated_at = %s 
        WHERE ddlid = %s
        """
        cursor.execute(update_sql, (new_ddl_time, update_time, ddlid))
        
        # 4. 查询更新后的完整信息
        query_sql = "SELECT ddlid, teacher_id, teacher_name, ddl_time, created_at, updated_at FROM ddl_management WHERE ddlid = %s"
        cursor.execute(query_sql, (ddlid,))
        updated_row = cursor.fetchone()
        
        db.commit()
        
        # 5. 构造返回结果
        return DDLOut(
            ddlid=updated_row[0],
            creator_id=updated_row[1],
            teacher_id=updated_row[1],
            ddl_time=updated_row[3].strftime("%Y-%m-%d %H:%M:%S") if isinstance(updated_row[3], datetime) else updated_row[3],
            created_at=updated_row[4].strftime("%Y-%m-%d %H:%M:%S") if isinstance(updated_row[4], datetime) else updated_row[4]
        )
    except pymysql.MySQLError as e:
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail=f"更新DDL失败：{str(e)}")
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass


def _fetch_paper_student_basic_info(
    db: pymysql.connections.Connection,
    paper_id: Optional[int] = None,
    student_id: Optional[str] = None,
) -> Dict:
    cursor = None
    try:
        resolved_paper_id = paper_id
        row = None
        cursor = db.cursor(pymysql.cursors.DictCursor)

        if paper_id is not None:
            if not isinstance(paper_id, int) or paper_id <= 0:
                raise HTTPException(status_code=400, detail="paper_id必须是正整数")

            cursor.execute(
                """
                SELECT
                    p.id AS paper_id,
                    p.owner_id AS owner_id,
                    s.student_id AS student_number
                FROM papers p
                LEFT JOIN students s ON s.id = p.owner_id
                WHERE p.id = %s
                LIMIT 1
                """,
                (paper_id,),
            )
            paper_row = cursor.fetchone()
            if not paper_row:
                raise HTTPException(status_code=404, detail=f"论文ID {paper_id} 不存在")

            candidate_student_ids = []
            if paper_row.get("student_number"):
                candidate_student_ids.append(str(paper_row["student_number"]))
            if paper_row.get("owner_id") is not None:
                candidate_student_ids.append(str(paper_row["owner_id"]))

            for candidate_student_id in candidate_student_ids:
                cursor.execute(
                    """
                    SELECT
                        id,
                        college,
                        student_id,
                        student_name,
                        student_major,
                        class_name,
                        teacher_name,
                        teacher_title,
                        paper_title,
                        paper_keywords,
                        paper_source,
                        paper_type,
                        research_direction,
                        paper_language
                    FROM paper_basic_info
                    WHERE student_id = %s
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                    """,
                    (candidate_student_id,),
                )
                row = cursor.fetchone()
                if row:
                    break
        else:
            student_id_value = str(student_id).strip() if student_id is not None else ""
            if not student_id_value:
                raise HTTPException(status_code=400, detail="student_id不能为空")

            cursor.execute(
                """
                SELECT
                    id,
                    college,
                    student_id,
                    student_name,
                    student_major,
                    class_name,
                    teacher_name,
                    teacher_title,
                    paper_title,
                    paper_keywords,
                    paper_source,
                    paper_type,
                    research_direction,
                    paper_language
                FROM paper_basic_info
                WHERE student_id = %s
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (student_id_value,),
            )
            row = cursor.fetchone()

            if not row and student_id_value.isdigit():
                cursor.execute(
                    """
                    SELECT
                        pbi.id,
                        pbi.college,
                        pbi.student_id,
                        pbi.student_name,
                        pbi.student_major,
                        pbi.class_name,
                        pbi.teacher_name,
                        pbi.teacher_title,
                        pbi.paper_title,
                        pbi.paper_keywords,
                        pbi.paper_source,
                        pbi.paper_type,
                        pbi.research_direction,
                        pbi.paper_language
                    FROM paper_basic_info pbi
                    JOIN students s ON s.student_id = pbi.student_id
                    WHERE s.id = %s
                    ORDER BY pbi.updated_at DESC, pbi.id DESC
                    LIMIT 1
                    """,
                    (int(student_id_value),),
                )
                row = cursor.fetchone()

        if not row:
            if paper_id is not None:
                raise HTTPException(status_code=404, detail=f"论文ID {paper_id} 未找到对应论文基础信息")
            raise HTTPException(status_code=404, detail=f"学生ID/学号 {student_id} 未找到对应论文基础信息")

        return {
            "paper_id": resolved_paper_id,
            "college": row.get("college"),
            "student_id": row.get("student_id"),
            "student_name": row.get("student_name"),
            "student_major": row.get("student_major"),
            "class_name": row.get("class_name"),
            "teacher_name": row.get("teacher_name"),
            "teacher_title": row.get("teacher_title"),
            "paper_title": row.get("paper_title"),
            "paper_keywords": row.get("paper_keywords"),
            "paper_source": row.get("paper_source"),
            "paper_type": row.get("paper_type"),
            "research_direction": row.get("research_direction"),
            "paper_language": row.get("paper_language"),
        }
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")
    finally:
        if cursor:
            cursor.close()


def _score_to_float(value):
    return float(value) if value is not None else None


def _docx_escape(value) -> str:
    return escape("" if value is None else str(value))


def _score_to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f").rstrip("0").rstrip(".")
    try:
        numeric_value = float(value)
        return str(int(numeric_value)) if numeric_value.is_integer() else f"{numeric_value:.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value)


def _docx_run(text: str, *, bold: bool = False, underline: bool = False, size: int = 24) -> str:
    bold_xml = "<w:b/>" if bold else ""
    underline_xml = '<w:u w:val="single"/>' if underline else ""
    return (
        "<w:r>"
        "<w:rPr>"
        '<w:rFonts w:ascii="Times New Roman" w:eastAsia="宋体" w:hAnsi="Times New Roman"/>'
        f"{bold_xml}{underline_xml}<w:sz w:val=\"{size}\"/><w:szCs w:val=\"{size}\"/>"
        "</w:rPr>"
        f'<w:t xml:space="preserve">{_docx_escape(text)}</w:t>'
        "</w:r>"
    )


def _docx_paragraph(
    runs: list[str],
    *,
    align: str | None = None,
    spacing_after: int = 120,
    spacing_before: int = 0,
) -> str:
    jc_xml = f'<w:jc w:val="{align}"/>' if align else ""
    return (
        "<w:p>"
        f"<w:pPr>{jc_xml}<w:spacing w:before=\"{spacing_before}\" w:after=\"{spacing_after}\"/></w:pPr>"
        f"{''.join(runs)}"
        "</w:p>"
    )


def _docx_text_paragraph(
    text: str,
    *,
    align: str | None = None,
    bold: bool = False,
    size: int = 24,
    spacing_after: int = 120,
) -> str:
    parts = str(text).split("\n")
    runs = []
    for index, part in enumerate(parts):
        if index:
            runs.append("<w:r><w:br/></w:r>")
        runs.append(_docx_run(part, bold=bold, size=size))
    return _docx_paragraph(runs, align=align, spacing_after=spacing_after)


def _docx_cell(
    content: str,
    *,
    width: int,
    grid_span: int | None = None,
    v_merge: str | None = None,
    v_align: str = "center",
) -> str:
    span_xml = f'<w:gridSpan w:val="{grid_span}"/>' if grid_span else ""
    if v_merge == "restart":
        v_merge_xml = '<w:vMerge w:val="restart"/>'
    elif v_merge == "continue":
        v_merge_xml = "<w:vMerge/>"
    else:
        v_merge_xml = ""
    cell_content = content or _docx_text_paragraph("", spacing_after=0)
    return (
        "<w:tc>"
        f'<w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{span_xml}{v_merge_xml}<w:vAlign w:val="{v_align}"/></w:tcPr>'
        f"{cell_content}"
        "</w:tc>"
    )


def _docx_row(cells: list[str], *, height: int | None = None) -> str:
    height_xml = f'<w:trPr><w:trHeight w:val="{height}" w:hRule="atLeast"/></w:trPr>' if height else ""
    return f"<w:tr>{height_xml}{''.join(cells)}</w:tr>"


def _review_table_docx_xml(basic_info: dict, grades: dict) -> str:
    score_topic = _score_to_text(grades.get("topic_significance_score"))
    score_logic = _score_to_text(grades.get("logical_ability_score"))
    score_knowledge = _score_to_text(grades.get("knowledge_application_score"))
    score_problem = _score_to_text(grades.get("problem_analysis_solution_score"))
    score_academic = _score_to_text(grades.get("academic_norm_score"))
    score_total = _score_to_text(grades.get("teacher_total_score"))

    col_widths = [780, 1220, 5100, 820, 980]
    table_width = sum(col_widths)
    header_cells = [
        _docx_cell(_docx_text_paragraph("一级\n指标", align="center", bold=True, size=24, spacing_after=0), width=col_widths[0]),
        _docx_cell(_docx_text_paragraph("二级指标", align="center", bold=True, size=24, spacing_after=0), width=col_widths[1]),
        _docx_cell(_docx_text_paragraph("评阅要素", align="center", bold=True, size=24, spacing_after=0), width=col_widths[2]),
        _docx_cell(_docx_text_paragraph("分值", align="center", bold=True, size=24, spacing_after=0), width=col_widths[3]),
        _docx_cell(_docx_text_paragraph("得分", align="center", bold=True, size=24, spacing_after=0), width=col_widths[4]),
    ]
    rows = [_docx_row(header_cells, height=620)]
    rows.append(_docx_row([
        _docx_cell(_docx_text_paragraph("选题\n意义", align="center", size=24, spacing_after=0), width=col_widths[0]),
        _docx_cell(_docx_text_paragraph("选题目的\n和意义", align="center", size=24, spacing_after=0), width=col_widths[1]),
        _docx_cell(_docx_text_paragraph("符合专业培养目标，体现综合训练基本要求。\n面向所在专业领域学术问题或行业社会实际问题，有一定的理论或实用价值", size=23, spacing_after=0), width=col_widths[2], v_align="top"),
        _docx_cell(_docx_text_paragraph("10分", align="center", size=24, spacing_after=0), width=col_widths[3]),
        _docx_cell(_docx_text_paragraph(score_topic, align="center", size=24, spacing_after=0), width=col_widths[4]),
    ], height=1040))
    rows.append(_docx_row([
        _docx_cell(_docx_text_paragraph("逻辑\n能力", align="center", size=24, spacing_after=0), width=col_widths[0]),
        _docx_cell(_docx_text_paragraph("逻辑与层\n次体系", align="center", size=24, spacing_after=0), width=col_widths[1]),
        _docx_cell(_docx_text_paragraph("论点鲜明，论据确凿，论证充分，达到所在专业领域要求。体系完整，层次分明，重点突出", size=23, spacing_after=0), width=col_widths[2], v_align="top"),
        _docx_cell(_docx_text_paragraph("10分", align="center", size=24, spacing_after=0), width=col_widths[3]),
        _docx_cell(_docx_text_paragraph(score_logic, align="center", size=24, spacing_after=0), width=col_widths[4]),
    ], height=860))
    rows.append(_docx_row([
        _docx_cell(_docx_text_paragraph("专业\n水平", align="center", size=24, spacing_after=0), width=col_widths[0], v_merge="restart"),
        _docx_cell(_docx_text_paragraph("综合应用\n知识能力", align="center", size=24, spacing_after=0), width=col_widths[1]),
        _docx_cell(_docx_text_paragraph("综合运用工程基础知识、专业知识和技能，对信息与通信及相关领域的复杂工程或科学问题，系统分析各项指标，提出设计方案，实现满足特定需求的系统或单元，完成任务书的技术指标要求，在设计环节中体现创新", size=23, spacing_after=0), width=col_widths[2], v_align="top"),
        _docx_cell(_docx_text_paragraph("30分", align="center", size=24, spacing_after=0), width=col_widths[3]),
        _docx_cell(_docx_text_paragraph(score_knowledge, align="center", size=24, spacing_after=0), width=col_widths[4]),
    ], height=1460))
    rows.append(_docx_row([
        _docx_cell("", width=col_widths[0], v_merge="continue"),
        _docx_cell(_docx_text_paragraph("分析解决\n问题能力", align="center", size=24, spacing_after=0), width=col_widths[1]),
        _docx_cell(_docx_text_paragraph("针对毕业设计课题的需求，合理选择恰当的软件硬件平台、编程语言或设计仿真工具，并熟练运用这些现代工具进行设计开发、仿真分析、测量调试及预测模拟，得到有助于解决问题的有效结论", size=23, spacing_after=0), width=col_widths[2], v_align="top"),
        _docx_cell(_docx_text_paragraph("30分", align="center", size=24, spacing_after=0), width=col_widths[3]),
        _docx_cell(_docx_text_paragraph(score_problem, align="center", size=24, spacing_after=0), width=col_widths[4]),
    ], height=1460))
    rows.append(_docx_row([
        _docx_cell(_docx_text_paragraph("学术\n规范", align="center", size=24, spacing_after=0), width=col_widths[0]),
        _docx_cell(_docx_text_paragraph("行文和引\n用规范", align="center", size=24, spacing_after=0), width=col_widths[1]),
        _docx_cell(_docx_text_paragraph("文字表达、书写格式、图表（图纸）、公式符号、缩略词等方面符合规范。在资料引证、参考文献等方面符合通行学术规范和知识产权相关规定", size=23, spacing_after=0), width=col_widths[2], v_align="top"),
        _docx_cell(_docx_text_paragraph("20分", align="center", size=24, spacing_after=0), width=col_widths[3]),
        _docx_cell(_docx_text_paragraph(score_academic, align="center", size=24, spacing_after=0), width=col_widths[4]),
    ], height=1180))
    rows.append(_docx_row([
        _docx_cell(_docx_text_paragraph("总分", align="center", bold=True, size=24, spacing_after=0), width=col_widths[0]),
        _docx_cell(_docx_text_paragraph("", align="center", size=24, spacing_after=0), width=sum(col_widths[1:4]), grid_span=3),
        _docx_cell(_docx_text_paragraph(score_total, align="center", bold=True, size=24, spacing_after=0), width=col_widths[4]),
    ], height=660))
    opinion_content = (
        _docx_text_paragraph("", spacing_after=180)
        + _docx_text_paragraph("", spacing_after=180)
        + _docx_text_paragraph("", spacing_after=180)
        + _docx_text_paragraph("评阅教师签名：________________", align="right", bold=True, size=24, spacing_after=360)
        + _docx_text_paragraph("年      月      日", align="right", size=24, spacing_after=0)
    )
    rows.append(_docx_row([
        _docx_cell(_docx_text_paragraph("修改\n意见", align="center", bold=True, size=24, spacing_after=0), width=col_widths[0]),
        _docx_cell(opinion_content, width=sum(col_widths[1:]), grid_span=4, v_align="bottom"),
    ], height=2600))

    table = (
        "<w:tbl>"
        "<w:tblPr>"
        f'<w:tblW w:w="{table_width}" w:type="dxa"/>'
        '<w:tblLayout w:type="fixed"/>'
        '<w:tblBorders><w:top w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:left w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:bottom w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:right w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:insideH w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:insideV w:val="single" w:sz="8" w:space="0" w:color="000000"/></w:tblBorders>'
        "</w:tblPr>"
        "<w:tblGrid>"
        + "".join(f'<w:gridCol w:w="{width}"/>' for width in col_widths)
        + "</w:tblGrid>"
        + "".join(rows)
        + "</w:tbl>"
    )

    college = basic_info.get("college") or ""
    teacher_name = basic_info.get("teacher_name") or ""
    student_name = basic_info.get("student_name") or ""
    student_id = basic_info.get("student_id") or ""
    class_name = basic_info.get("class_name") or ""
    paper_title = basic_info.get("paper_title") or ""

    document_body = (
        _docx_text_paragraph("附件8    评阅表", size=22, spacing_after=80)
        + _docx_text_paragraph("中国计量大学毕业设计（论文）评阅表", align="center", bold=True, size=32, spacing_after=260)
        + _docx_paragraph([
            _docx_run("二  级  学  院：", size=24),
            _docx_run(f"{college:^18}", underline=True, size=24),
            _docx_run("  指 导 教 师：", size=24),
            _docx_run(f"{teacher_name:^18}", underline=True, size=24),
        ], spacing_after=240)
        + _docx_paragraph([
            _docx_run("姓  名：", size=24),
            _docx_run(f"{student_name:^14}", underline=True, size=24),
            _docx_run("  学  号：", size=24),
            _docx_run(f"{student_id:^14}", underline=True, size=24),
            _docx_run("班  级：", size=24),
            _docx_run(f"{class_name:^14}", underline=True, size=24),
        ], spacing_after=240)
        + _docx_paragraph([
            _docx_run("题  目：", size=24),
            _docx_run(f"{paper_title:<52}", underline=True, size=24),
        ], spacing_after=160)
        + table
        + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="720" w:right="1440" w:bottom="720" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{document_body}</w:body>"
        "</w:document>"
    )


def _build_review_table_docx(basic_info: dict, grades: dict) -> bytes:
    document_xml = _review_table_docx_xml(basic_info, grades)
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as docx_zip:
        docx_zip.writestr("[Content_Types].xml", content_types)
        docx_zip.writestr("_rels/.rels", rels_xml)
        docx_zip.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def _review_opinion_table_docx_xml(basic_info: dict, reviews: list[dict]) -> str:
    col_widths = [420, 1750, 3200, 2850, 700]
    table_width = sum(col_widths)

    rows = [
        _docx_row(
            [
                _docx_cell(_docx_text_paragraph("序\n号", align="center", bold=True, size=24, spacing_after=0), width=col_widths[0]),
                _docx_cell(_docx_text_paragraph("问题所在位置", align="center", bold=True, size=24, spacing_after=0), width=col_widths[1]),
                _docx_cell(_docx_text_paragraph("评审意见", align="center", bold=True, size=24, spacing_after=0), width=col_widths[2]),
                _docx_cell(_docx_text_paragraph("学生修改反馈", align="center", bold=True, size=24, spacing_after=0), width=col_widths[3]),
                _docx_cell(_docx_text_paragraph("备注", align="center", bold=True, size=24, spacing_after=0), width=col_widths[4]),
            ],
            height=720,
        )
    ]

    for index, review in enumerate(reviews, start=1):
        opinion_text = (review.get("review_content") or "").strip()
        rows.append(
            _docx_row(
                [
                    _docx_cell(_docx_text_paragraph(str(index), align="center", size=24, spacing_after=0), width=col_widths[0]),
                    _docx_cell(_docx_text_paragraph("", size=24, spacing_after=0), width=col_widths[1], v_align="top"),
                    _docx_cell(_docx_text_paragraph(opinion_text, size=24, spacing_after=0), width=col_widths[2], v_align="top"),
                    _docx_cell(_docx_text_paragraph("", size=24, spacing_after=0), width=col_widths[3], v_align="top"),
                    _docx_cell(_docx_text_paragraph("", size=24, spacing_after=0), width=col_widths[4], v_align="top"),
                ],
                height=760,
            )
        )

    table = (
        "<w:tbl>"
        "<w:tblPr>"
        f'<w:tblW w:w="{table_width}" w:type="dxa"/>'
        '<w:tblLayout w:type="fixed"/>'
        '<w:tblBorders><w:top w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:left w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:bottom w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:right w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:insideH w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:insideV w:val="single" w:sz="8" w:space="0" w:color="000000"/></w:tblBorders>'
        "</w:tblPr>"
        "<w:tblGrid>"
        + "".join(f'<w:gridCol w:w="{width}"/>' for width in col_widths)
        + "</w:tblGrid>"
        + "".join(rows)
        + "</w:tbl>"
    )

    teacher_name = basic_info.get("teacher_name") or ""
    student_name = basic_info.get("student_name") or ""
    student_id = basic_info.get("student_id") or ""
    class_name = basic_info.get("class_name") or ""
    paper_title = basic_info.get("paper_title") or ""

    document_body = (
        _docx_text_paragraph("中国计量大学信息工程学院毕业论文评审意见", align="center", bold=True, size=32, spacing_after=360)
        + _docx_paragraph(
            [
                _docx_run("指导教师：", size=24),
                _docx_run(f"{teacher_name:^12}", underline=True, size=24),
                _docx_run("  姓名：", size=24),
                _docx_run(f"{student_name:^12}", underline=True, size=24),
                _docx_run("  学号：", size=24),
                _docx_run(f"{student_id:^14}", underline=True, size=24),
                _docx_run("  班级：", size=24),
                _docx_run(f"{class_name:^12}", underline=True, size=24),
            ],
            spacing_after=240,
        )
        + _docx_paragraph(
            [
                _docx_run("题目：", size=24),
                _docx_run(f"{paper_title:<56}", underline=True, size=24),
            ],
            spacing_after=220,
        )
        + _docx_text_paragraph("评审情况记录：", bold=True, size=28, spacing_after=180)
        + table
        + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="900" w:right="1440" w:bottom="900" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{document_body}</w:body>"
        "</w:document>"
    )


def _build_review_opinion_table_docx(basic_info: dict, reviews: list[dict]) -> bytes:
    document_xml = _review_opinion_table_docx_xml(basic_info, reviews)
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    rels_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as docx_zip:
        docx_zip.writestr("[Content_Types].xml", content_types)
        docx_zip.writestr("_rels/.rels", rels_xml)
        docx_zip.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def _fetch_review_table_data(
    db: pymysql.connections.Connection,
    paper_id: Optional[int] = None,
    student_id: Optional[str] = None,
) -> tuple[dict, dict]:
    if paper_id is None and student_id is None:
        raise HTTPException(status_code=400, detail="paper_id和student_id必须传入一个")
    if paper_id is not None and student_id is not None:
        raise HTTPException(status_code=400, detail="paper_id和student_id只能传入一个")
    if paper_id is not None and paper_id <= 0:
        raise HTTPException(status_code=400, detail="paper_id必须是正整数")

    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        resolved_paper_id = paper_id
        student_internal_id = None
        student_number = None

        if paper_id is not None:
            cursor.execute(
                """
                SELECT p.id AS paper_id, p.owner_id, s.student_id, s.class_name
                FROM papers p
                LEFT JOIN students s ON s.id = p.owner_id
                WHERE p.id = %s
                LIMIT 1
                """,
                (paper_id,),
            )
            paper_row = cursor.fetchone()
            if not paper_row:
                raise HTTPException(status_code=404, detail=f"论文ID {paper_id} 不存在")
            student_internal_id = paper_row.get("owner_id")
            student_number = str(paper_row["student_id"]) if paper_row.get("student_id") else None
        else:
            student_id_value = str(student_id).strip() if student_id is not None else ""
            if not student_id_value:
                raise HTTPException(status_code=400, detail="student_id不能为空")
            student_number = student_id_value
            if student_id_value.isdigit():
                cursor.execute(
                    "SELECT id, student_id FROM students WHERE id = %s OR student_id = %s LIMIT 1",
                    (int(student_id_value), student_id_value),
                )
                student_row = cursor.fetchone()
                if student_row:
                    student_internal_id = student_row.get("id")
                    student_number = str(student_row["student_id"]) if student_row.get("student_id") else student_number
            cursor.execute(
                """
                SELECT id
                FROM papers
                WHERE owner_id = %s
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (student_internal_id,),
            )
            paper_row = cursor.fetchone() if student_internal_id else None
            if paper_row:
                resolved_paper_id = paper_row.get("id")

        basic_row = None
        for candidate_student_id in [student_number, str(student_internal_id) if student_internal_id is not None else None]:
            if not candidate_student_id:
                continue
            cursor.execute(
                """
                SELECT
                    pbi.college,
                    pbi.student_id,
                    pbi.student_name,
                    pbi.teacher_name,
                    pbi.teacher_title,
                    pbi.paper_title,
                    COALESCE(pbi.class_name, s.class_name) AS class_name
                FROM paper_basic_info pbi
                LEFT JOIN students s ON s.student_id = pbi.student_id
                WHERE pbi.student_id = %s
                ORDER BY pbi.updated_at DESC, pbi.id DESC
                LIMIT 1
                """,
                (candidate_student_id,),
            )
            basic_row = cursor.fetchone()
            if basic_row:
                break
        if not basic_row:
            raise HTTPException(status_code=404, detail="未找到对应论文基础信息")

        grade_row = None
        if resolved_paper_id is not None:
            cursor.execute(
                """
                SELECT
                    topic_significance_score,
                    logical_ability_score,
                    knowledge_application_score,
                    problem_analysis_solution_score,
                    academic_norm_score,
                    teacher_total_score
                FROM paper_grades
                WHERE paper_id = %s
                LIMIT 1
                """,
                (resolved_paper_id,),
            )
            grade_row = cursor.fetchone()
        if not grade_row and student_internal_id is not None:
            cursor.execute(
                """
                SELECT
                    topic_significance_score,
                    logical_ability_score,
                    knowledge_application_score,
                    problem_analysis_solution_score,
                    academic_norm_score,
                    teacher_total_score
                FROM paper_grades
                WHERE student_id = %s
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (student_internal_id,),
            )
            grade_row = cursor.fetchone()

        if not grade_row:
            grade_row = {}
        return basic_row, grade_row
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")
    finally:
        if cursor:
            cursor.close()


def _fetch_review_opinion_table_data(
    db: pymysql.connections.Connection,
    paper_id: Optional[int] = None,
    student_id: Optional[str] = None,
) -> tuple[dict, list[dict]]:
    if paper_id is None and student_id is None:
        raise HTTPException(status_code=400, detail="paper_id和student_id必须传入一个")
    if paper_id is not None and student_id is not None:
        raise HTTPException(status_code=400, detail="paper_id和student_id只能传入一个")
    if paper_id is not None and paper_id <= 0:
        raise HTTPException(status_code=400, detail="paper_id必须是正整数")

    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        resolved_paper_id = paper_id
        student_internal_id = None
        student_number = None
        basic_row = None

        if paper_id is not None:
            cursor.execute(
                """
                SELECT p.id AS paper_id, p.owner_id, s.student_id
                FROM papers p
                LEFT JOIN students s ON s.id = p.owner_id
                WHERE p.id = %s
                LIMIT 1
                """,
                (paper_id,),
            )
            paper_row = cursor.fetchone()
            if paper_row:
                student_internal_id = paper_row.get("owner_id")
                student_number = str(paper_row["student_id"]) if paper_row.get("student_id") else None
            else:
                # 兼容将 paper_basic_info.id 当作 paper_id 传入的情况
                cursor.execute(
                    """
                    SELECT
                        pbi.id,
                        pbi.college,
                        pbi.student_id,
                        pbi.student_name,
                        COALESCE(pbi.class_name, s.class_name) AS class_name,
                        pbi.teacher_name,
                        pbi.teacher_title,
                        pbi.paper_title,
                        s.id AS student_internal_id
                    FROM paper_basic_info pbi
                    LEFT JOIN students s ON s.student_id = pbi.student_id
                    WHERE pbi.id = %s
                    LIMIT 1
                    """,
                    (paper_id,),
                )
                basic_row = cursor.fetchone()
                if not basic_row:
                    raise HTTPException(status_code=404, detail=f"论文ID {paper_id} 不存在")
                student_number = str(basic_row["student_id"]) if basic_row.get("student_id") else None
                student_internal_id = basic_row.get("student_internal_id")
        else:
            student_id_value = str(student_id).strip() if student_id is not None else ""
            if not student_id_value:
                raise HTTPException(status_code=400, detail="student_id不能为空")

            cursor.execute(
                """
                SELECT
                    pbi.id,
                    pbi.college,
                    pbi.student_id,
                    pbi.student_name,
                    COALESCE(pbi.class_name, s.class_name) AS class_name,
                    pbi.teacher_name,
                    pbi.teacher_title,
                    pbi.paper_title,
                    s.id AS student_internal_id
                FROM paper_basic_info pbi
                LEFT JOIN students s ON s.student_id = pbi.student_id
                WHERE pbi.student_id = %s
                ORDER BY pbi.updated_at DESC, pbi.id DESC
                LIMIT 1
                """,
                (student_id_value,),
            )
            basic_row = cursor.fetchone()
            if basic_row:
                student_number = str(basic_row["student_id"]) if basic_row.get("student_id") else student_id_value
                student_internal_id = basic_row.get("student_internal_id")

            paper_row = None
            if student_id_value.isdigit():
                if basic_row is None:
                    cursor.execute(
                        """
                        SELECT
                            pbi.id,
                            pbi.college,
                            pbi.student_id,
                            pbi.student_name,
                            COALESCE(pbi.class_name, s.class_name) AS class_name,
                            pbi.teacher_name,
                            pbi.teacher_title,
                            pbi.paper_title,
                            s.id AS student_internal_id
                        FROM paper_basic_info pbi
                        JOIN students s ON s.student_id = pbi.student_id
                        WHERE s.id = %s
                        ORDER BY pbi.updated_at DESC, pbi.id DESC
                        LIMIT 1
                        """,
                        (int(student_id_value),),
                    )
                    basic_row = cursor.fetchone()
                    if basic_row:
                        student_number = str(basic_row["student_id"]) if basic_row.get("student_id") else student_id_value
                        student_internal_id = basic_row.get("student_internal_id")

                cursor.execute(
                    """
                    SELECT
                        p.id AS paper_id,
                        p.owner_id AS student_internal_id,
                        s.student_id
                    FROM papers p
                    LEFT JOIN students s ON s.id = p.owner_id
                    WHERE s.student_id = %s
                    ORDER BY p.updated_at DESC, p.id DESC
                    LIMIT 1
                    """,
                    (student_id_value,),
                )
                paper_row = cursor.fetchone()
                if not paper_row:
                    fallback_student_internal_id = (
                        student_internal_id if student_internal_id is not None else int(student_id_value)
                    )
                    cursor.execute(
                        """
                        SELECT
                            p.id AS paper_id,
                            p.owner_id AS student_internal_id,
                            s.student_id
                        FROM papers p
                        LEFT JOIN students s ON s.id = p.owner_id
                        WHERE p.owner_id = %s OR s.id = %s
                        ORDER BY p.updated_at DESC, p.id DESC
                        LIMIT 1
                        """,
                        (fallback_student_internal_id, fallback_student_internal_id),
                    )
                    paper_row = cursor.fetchone()
            else:
                cursor.execute(
                    """
                    SELECT
                        p.id AS paper_id,
                        p.owner_id AS student_internal_id,
                        s.student_id
                    FROM papers p
                    LEFT JOIN students s ON s.id = p.owner_id
                    WHERE s.student_id = %s
                    ORDER BY p.updated_at DESC, p.id DESC
                    LIMIT 1
                    """,
                    (student_id_value,),
                )
                paper_row = cursor.fetchone()
            if paper_row:
                resolved_paper_id = paper_row.get("paper_id")
                student_internal_id = paper_row.get("student_internal_id")
                student_number = str(paper_row["student_id"]) if paper_row.get("student_id") else student_number
            elif basic_row is None:
                raise HTTPException(status_code=404, detail=f"学生ID/学号 {student_id} 未找到对应论文")

        if basic_row is None:
            for candidate_student_id in [student_number, str(student_internal_id) if student_internal_id is not None else None]:
                if not candidate_student_id:
                    continue
                cursor.execute(
                    """
                    SELECT
                        pbi.college,
                        pbi.student_id,
                        pbi.student_name,
                        pbi.teacher_name,
                        pbi.teacher_title,
                        pbi.paper_title,
                        COALESCE(pbi.class_name, s.class_name) AS class_name
                    FROM paper_basic_info pbi
                    LEFT JOIN students s ON s.student_id = pbi.student_id
                    WHERE pbi.student_id = %s
                    ORDER BY pbi.updated_at DESC, pbi.id DESC
                    LIMIT 1
                    """,
                    (candidate_student_id,),
                )
                basic_row = cursor.fetchone()
                if basic_row:
                    break

        if not basic_row:
            raise HTTPException(status_code=404, detail="未找到对应论文基础信息")

        review_rows = []
        review_paper_ids = []
        if resolved_paper_id is not None:
            review_paper_ids.append(resolved_paper_id)
        if paper_id is not None and paper_id not in review_paper_ids:
            review_paper_ids.append(paper_id)

        for review_paper_id in review_paper_ids:
            cursor.execute(
                """
                SELECT
                    id,
                    paper_id,
                    teacher_id,
                    teacher_name,
                    review_content,
                    review_time,
                    updated_time,
                    created_at,
                    updated_at
                FROM paper_reviews
                WHERE paper_id = %s
                ORDER BY review_time ASC, created_at ASC, id ASC
                """,
                (review_paper_id,),
            )
            review_rows = cursor.fetchall() or []
            if review_rows:
                break

        return basic_row, review_rows
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")
    finally:
        if cursor:
            cursor.close()


@router.post(
    "/teacher-score",
    response_model=Dict,
    summary="教师论文评分",
    description="输入论文ID或学生ID，保存教师五项评分并自动计算教师评分总分"
)
def save_teacher_paper_score(
    paper_id: Optional[int] = Query(None, description="论文ID，与student_id二选一"),
    student_id: Optional[str] = Query(None, description="学生ID或学号，与paper_id二选一"),
    topic_significance_score: Optional[float] = Query(None, ge=0, le=999.99, description="选题意义评分"),
    logical_ability_score: Optional[float] = Query(None, ge=0, le=999.99, description="逻辑能力评分"),
    knowledge_application_score: Optional[float] = Query(None, ge=0, le=999.99, description="综合运用知识能力评分"),
    problem_analysis_solution_score: Optional[float] = Query(None, ge=0, le=999.99, description="分析解决问题能力评分"),
    academic_norm_score: Optional[float] = Query(None, ge=0, le=999.99, description="学术规范评分"),
    db: pymysql.connections.Connection = Depends(get_db),
):
    if paper_id is None and student_id is None:
        raise HTTPException(status_code=400, detail="paper_id和student_id必须传入一个")
    if paper_id is not None and student_id is not None:
        raise HTTPException(status_code=400, detail="paper_id和student_id只能传入一个")

    score_values = {
        "topic_significance_score": topic_significance_score,
        "logical_ability_score": logical_ability_score,
        "knowledge_application_score": knowledge_application_score,
        "problem_analysis_solution_score": problem_analysis_solution_score,
        "academic_norm_score": academic_norm_score,
    }
    provided_scores = {
        field: value
        for field, value in score_values.items()
        if value is not None
    }
    if not provided_scores:
        raise HTTPException(status_code=400, detail="至少需要输入一项评分")

    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        if paper_id is not None:
            if paper_id <= 0:
                raise HTTPException(status_code=400, detail="paper_id必须是正整数")
            cursor.execute(
                """
                SELECT
                    p.id AS paper_id,
                    p.owner_id AS student_internal_id,
                    p.teacher_id AS teacher_id
                FROM papers p
                WHERE p.id = %s
                LIMIT 1
                """,
                (paper_id,),
            )
        else:
            student_id_value = str(student_id).strip() if student_id is not None else ""
            if not student_id_value:
                raise HTTPException(status_code=400, detail="student_id不能为空")

            if student_id_value.isdigit():
                student_internal_id = int(student_id_value)
                cursor.execute(
                    """
                    SELECT
                        p.id AS paper_id,
                        p.owner_id AS student_internal_id,
                        p.teacher_id AS teacher_id
                    FROM papers p
                    LEFT JOIN students s ON s.id = p.owner_id
                    LEFT JOIN paper_grades pg ON pg.paper_id = p.id
                    WHERE
                        p.owner_id = %s
                        OR s.id = %s
                        OR s.student_id = %s
                        OR pg.student_id = %s
                    ORDER BY p.updated_at DESC, p.id DESC
                    LIMIT 1
                    """,
                    (
                        student_internal_id,
                        student_internal_id,
                        student_id_value,
                        student_internal_id,
                    ),
                )
            else:
                cursor.execute(
                    """
                    SELECT
                        p.id AS paper_id,
                        p.owner_id AS student_internal_id,
                        p.teacher_id AS teacher_id
                    FROM papers p
                    LEFT JOIN students s ON s.id = p.owner_id
                    WHERE s.student_id = %s
                    ORDER BY p.updated_at DESC, p.id DESC
                    LIMIT 1
                    """,
                    (student_id_value,),
                )

        paper_row = cursor.fetchone()
        if not paper_row:
            if paper_id is not None:
                raise HTTPException(status_code=404, detail=f"论文ID {paper_id} 不存在")
            raise HTTPException(status_code=404, detail=f"学生ID/学号 {student_id} 未找到对应论文")

        resolved_paper_id = paper_row["paper_id"]
        resolved_student_id = paper_row["student_internal_id"]

        cursor.execute(
            """
            INSERT INTO paper_grades (paper_id, student_id, paper_title, created_at, updated_at)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON DUPLICATE KEY UPDATE
                student_id = COALESCE(student_id, VALUES(student_id)),
                paper_title = paper_title
            """,
            (resolved_paper_id, resolved_student_id, ""),
        )

        set_clause = ", ".join(f"`{field}` = %s" for field in provided_scores)
        cursor.execute(
            f"""
            UPDATE paper_grades
            SET {set_clause},
                updated_at = CURRENT_TIMESTAMP
            WHERE paper_id = %s
            """,
            tuple(provided_scores.values()) + (resolved_paper_id,),
        )
        cursor.execute(
            """
            UPDATE paper_grades
            SET teacher_total_score =
                COALESCE(topic_significance_score, 0) +
                COALESCE(logical_ability_score, 0) +
                COALESCE(knowledge_application_score, 0) +
                COALESCE(problem_analysis_solution_score, 0) +
                COALESCE(academic_norm_score, 0),
                updated_at = CURRENT_TIMESTAMP
            WHERE paper_id = %s
            """,
            (resolved_paper_id,),
        )
        cursor.execute(
            """
            SELECT
                paper_id,
                student_id,
                topic_significance_score,
                logical_ability_score,
                knowledge_application_score,
                problem_analysis_solution_score,
                academic_norm_score,
                teacher_total_score
            FROM paper_grades
            WHERE paper_id = %s
            LIMIT 1
            """,
            (resolved_paper_id,),
        )
        grade_row = cursor.fetchone()
        db.commit()

        return {
            "message": "教师评分保存成功",
            "paper_id": grade_row.get("paper_id"),
            "student_id": grade_row.get("student_id"),
            "teacher_id": paper_row.get("teacher_id"),
            "updated_fields": list(provided_scores.keys()),
            "topic_significance_score": _score_to_float(grade_row.get("topic_significance_score")),
            "logical_ability_score": _score_to_float(grade_row.get("logical_ability_score")),
            "knowledge_application_score": _score_to_float(grade_row.get("knowledge_application_score")),
            "problem_analysis_solution_score": _score_to_float(grade_row.get("problem_analysis_solution_score")),
            "academic_norm_score": _score_to_float(grade_row.get("academic_norm_score")),
            "teacher_total_score": _score_to_float(grade_row.get("teacher_total_score")),
        }
    except HTTPException:
        if db:
            db.rollback()
        raise
    except pymysql.MySQLError as e:
        if db:
            db.rollback()
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")
    finally:
        if cursor:
            cursor.close()


@router.get(
    "/teacher-score",
    response_model=Dict,
    summary="获取教师论文评分",
    description="输入论文ID或学生ID，查询教师五项评分及教师评分总分"
)
def get_teacher_paper_score(
    paper_id: Optional[int] = Query(None, description="论文ID，与student_id二选一"),
    student_id: Optional[int] = Query(None, description="学生ID，与paper_id二选一"),
    db: pymysql.connections.Connection = Depends(get_db),
):
    if paper_id is None and student_id is None:
        raise HTTPException(status_code=400, detail="paper_id和student_id必须传入一个")
    if paper_id is not None and student_id is not None:
        raise HTTPException(status_code=400, detail="paper_id和student_id只能传入一个")
    if paper_id is not None and paper_id <= 0:
        raise HTTPException(status_code=400, detail="paper_id必须是正整数")
    if student_id is not None and student_id <= 0:
        raise HTTPException(status_code=400, detail="student_id必须是正整数")

    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        if paper_id is not None:
            cursor.execute(
                """
                SELECT
                    paper_id,
                    student_id,
                    topic_significance_score,
                    logical_ability_score,
                    knowledge_application_score,
                    problem_analysis_solution_score,
                    academic_norm_score,
                    teacher_total_score
                FROM paper_grades
                WHERE paper_id = %s
                LIMIT 1
                """,
                (paper_id,),
            )
        else:
            cursor.execute(
                """
                SELECT
                    paper_id,
                    student_id,
                    topic_significance_score,
                    logical_ability_score,
                    knowledge_application_score,
                    problem_analysis_solution_score,
                    academic_norm_score,
                    teacher_total_score
                FROM paper_grades
                WHERE student_id = %s
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (student_id,),
            )

        row = cursor.fetchone()
        if not row:
            if paper_id is not None:
                raise HTTPException(status_code=404, detail=f"论文ID {paper_id} 暂无教师评分")
            raise HTTPException(status_code=404, detail=f"学生ID {student_id} 暂无教师评分")

        return {
            "paper_id": row.get("paper_id"),
            "student_id": row.get("student_id"),
            "topic_significance_score": _score_to_float(row.get("topic_significance_score")),
            "logical_ability_score": _score_to_float(row.get("logical_ability_score")),
            "knowledge_application_score": _score_to_float(row.get("knowledge_application_score")),
            "problem_analysis_solution_score": _score_to_float(row.get("problem_analysis_solution_score")),
            "academic_norm_score": _score_to_float(row.get("academic_norm_score")),
            "teacher_total_score": _score_to_float(row.get("teacher_total_score")),
        }
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")
    finally:
        if cursor:
            cursor.close()


@router.get(
    "/student-basic-info",
    response_model=Dict,
    summary="获取论文基础信息",
    description="输入论文ID或学生ID，从论文基础信息汇总表查询学院、学生、导师和论文信息"
)
def get_paper_student_basic_info(
    paper_id: Optional[int] = Query(None, description="论文ID"),
    student_id: Optional[str] = Query(None, description="学生ID或学号"),
    db: pymysql.connections.Connection = Depends(get_db),
):
    if paper_id is None and student_id is None:
        raise HTTPException(status_code=400, detail="paper_id和student_id必须传入一个")
    if paper_id is not None and student_id is not None:
        raise HTTPException(status_code=400, detail="paper_id和student_id只能传入一个")

    return _fetch_paper_student_basic_info(db, paper_id=paper_id, student_id=student_id)


@router.get(
    "/review-table-download",
    summary="下载论文评阅表",
    description="输入论文ID或学生ID，自动填充论文基础信息和教师评分，并下载 .docx 格式评阅表",
    responses={
        200: {
            "content": {
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {}
            },
            "description": "论文评阅表 Word 文件",
        }
    },
)
def download_review_table_docx(
    paper_id: Optional[int] = Query(None, description="论文ID，与student_id二选一"),
    student_id: Optional[str] = Query(None, description="学生ID或学号，与paper_id二选一"),
    db: pymysql.connections.Connection = Depends(get_db),
):
    basic_info, grades = _fetch_review_table_data(db, paper_id=paper_id, student_id=student_id)
    docx_bytes = _build_review_table_docx(basic_info, grades)
    student_number = str(basic_info.get("student_id") or student_id or paper_id or "unknown")
    filename = f"论文评阅表_{student_number}.docx"
    encoded_filename = urllib.parse.quote(filename)

    return StreamingResponse(
        io.BytesIO(docx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": f"attachment; filename=review_table_{student_number}.docx; filename*=UTF-8''{encoded_filename}"
        },
    )


@router.get(
    "/review-opinion-table-download",
    summary="下载论文评审意见表",
    description="输入论文ID或学生ID（二选一），自动填充论文基础信息和评审意见，并下载 .docx 格式评审意见表",
    responses={
        200: {
            "content": {
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {}
            },
            "description": "论文评审意见表 Word 文件",
        }
    },
)
def download_review_opinion_table_docx(
    paper_id: Optional[int] = Query(None, description="论文ID，与student_id二选一"),
    student_id: Optional[str] = Query(None, description="学生ID或学号，与paper_id二选一"),
    db: pymysql.connections.Connection = Depends(get_db),
):
    basic_info, reviews = _fetch_review_opinion_table_data(db, paper_id=paper_id, student_id=student_id)
    docx_bytes = _build_review_opinion_table_docx(basic_info, reviews)
    student_number = str(basic_info.get("student_id") or student_id or paper_id or "unknown")
    filename = f"论文评审意见表_{student_number}.docx"
    encoded_filename = urllib.parse.quote(filename)

    return StreamingResponse(
        io.BytesIO(docx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": f"attachment; filename=review_opinion_table_{student_number}.docx; filename*=UTF-8''{encoded_filename}"
        },
    )


@router.get(
    "/score-summary-export",
    summary="导出成绩汇总表",
    description="按班级筛选论文基础信息，按学号从小到大排序，导出 .xlsx 成绩汇总表",
    responses={
        200: {
            "content": {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}
            },
            "description": "成绩汇总表 Excel 文件",
        }
    },
)
def export_score_summary_excel(
    class_name: str = Query(..., description="班级名称（按 paper_basic_info.class_name 精确筛选）"),
    db: pymysql.connections.Connection = Depends(get_db),
):
    if Workbook is None:
        raise HTTPException(status_code=500, detail="需要安装 openpyxl 库")

    class_name = str(class_name).strip()
    if not class_name:
        raise HTTPException(status_code=400, detail="class_name不能为空")

    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            """
            SELECT
                student_id,
                student_name,
                class_name,
                teacher_name,
                paper_title
            FROM paper_basic_info
            WHERE class_name = %s
            ORDER BY
                CASE
                    WHEN student_id REGEXP '^[0-9]+$' THEN 0
                    ELSE 1
                END ASC,
                CASE
                    WHEN student_id REGEXP '^[0-9]+$' THEN CAST(student_id AS UNSIGNED)
                    ELSE NULL
                END ASC,
                student_id ASC,
                id ASC
            """,
            (class_name,),
        )
        rows = cursor.fetchall()

        if not rows:
            raise HTTPException(status_code=404, detail=f"班级 {class_name} 未找到论文基础信息")

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "成绩汇总表"

        title = f"{class_name}开题成绩汇总表"
        headers = [
            "序号",
            "学号",
            "姓名",
            "班级",
            "导师姓名",
            "学生论文（设计）题目",
            "文献综述和论文翻译成绩（50%）",
            "开题报告成绩（25%）",
            "开题答辩成绩（25%）",
            "总评",
        ]

        thin_side = Side(style="thin", color="000000")
        thin_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
        title_font = Font(name="宋体", size=18, bold=True)
        header_font = Font(name="宋体", size=12, bold=True)
        body_font = Font(name="宋体", size=11)
        center_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        left_alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

        worksheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
        title_cell = worksheet.cell(row=1, column=1, value=title)
        title_cell.font = title_font
        title_cell.alignment = center_alignment
        worksheet.row_dimensions[1].height = 34

        for col_index, header in enumerate(headers, start=1):
            cell = worksheet.cell(row=2, column=col_index, value=header)
            cell.font = header_font
            cell.alignment = center_alignment
            cell.border = thin_border
        worksheet.row_dimensions[2].height = 42

        for row_index, row in enumerate(rows, start=3):
            values = [
                row_index - 2,
                row.get("student_id") or "",
                row.get("student_name") or "",
                row.get("class_name") or "",
                row.get("teacher_name") or "",
                row.get("paper_title") or "",
                "",
                "",
                "",
                "",
            ]
            for col_index, value in enumerate(values, start=1):
                cell = worksheet.cell(row=row_index, column=col_index, value=value)
                cell.font = body_font
                cell.border = thin_border
                cell.alignment = left_alignment if col_index == 6 else center_alignment
            worksheet.row_dimensions[row_index].height = 24

        column_widths = {
            "A": 8,
            "B": 16,
            "C": 12,
            "D": 14,
            "E": 14,
            "F": 48,
            "G": 18,
            "H": 14,
            "I": 14,
            "J": 10,
        }
        for column_name, width in column_widths.items():
            worksheet.column_dimensions[column_name].width = width

        worksheet.freeze_panes = "A3"

        buffer = io.BytesIO()
        workbook.save(buffer)
        buffer.seek(0)

        filename = f"{class_name}开题成绩汇总表.xlsx"
        encoded_filename = urllib.parse.quote(filename)

        return StreamingResponse(
            buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename=score_summary.xlsx; filename*=UTF-8''{encoded_filename}"
            },
        )
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"导出成绩汇总表失败：{str(e)}")
    finally:
        if cursor:
            cursor.close()


@router.get(
    "/{paper_id}",
    response_model=Dict,
    summary="查看论文所有信息",
    description="输入论文ID查询指定字段信息，仅论文归属学生或关联老师可访问"
)
async def get_paper_detail(
    paper_id: int,
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="提交者信息(JSON字符串，包含 sub/username/roles)"),
):
    # 解析当前用户信息
    current_user = _parse_current_user(current_user)
    submitter_id = current_user.get("sub", 0)  

    # 参数校验
    if not isinstance(paper_id, int) or paper_id <= 0:
        raise HTTPException(status_code=400, detail="paper_id必须是正整数")

    # 未登录校验
    if submitter_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再查看论文信息")

    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        # 仅查询指定字段
        paper_sql = """
        SELECT 
            id, owner_id, teacher_id, version, size, status, detail, 
            DATE_FORMAT(ddl, '%%Y-%%m-%%d %%H:%%i:%%s') as ddl,
            oss_key, pdf_oss_key,
            DATE_FORMAT(updated_at, '%%Y-%%m-%%d %%H:%%i:%%s') as updated_at  -- 新增更新时间字段，格式化输出
        FROM papers 
        WHERE id = %s
        """
        cursor.execute(paper_sql, (paper_id,))
        paper_detail = cursor.fetchone()

        # 校验论文是否存在
        if not paper_detail:
            raise HTTPException(status_code=404, detail=f"论文ID {paper_id} 不存在")

        # 权限校验：仅论文归属学生或关联老师可访问
        paper_owner_id = paper_detail["owner_id"]
        paper_teacher_id = paper_detail["teacher_id"]
        if not _ids_match(submitter_id, paper_owner_id) and not _ids_match(submitter_id, paper_teacher_id):
            raise HTTPException(
                status_code=403,
                detail=f"无权限查看：仅论文归属者（ID={paper_owner_id}）或关联老师（ID={paper_teacher_id}）可查看该论文信息"
            )

        return paper_detail

    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")
    finally:
        if cursor:
            cursor.close()

SUPPORTED_IMPORT_EXTS = (".csv", ".tsv", ".xlsx")


@router.post(
    "/basic-info/import",
    summary="导入论文基础信息",
    description="""上传 CSV/TSV/XLSX 文件批量导入论文基础信息到 paper_basic_info 表
    支持的Excel表头列：学号,姓名,学年,学期,年级,课题主管学院,学生学院,专业名称,班级,课题名称,课题类型,课题性质,课题来源,指导教师工号,指导教师姓名,指导教师职称,答辩记录上传状态,合成状态,录入状态,五级制总成绩,百分制总成绩,是否重修成绩,论文指导教师成绩,论文指导教师成绩比例,评阅老师成绩,评阅老师成绩比例,论文二次答辩成绩,论文二次答辩成绩比例,开题报告二次答辩成绩,开题报告二次答辩成绩比例,论文答辩成绩,论文答辩成绩比例,中期报告成绩,中期报告成绩比例,论文初稿成绩,论文初稿成绩比例,外文翻译成绩,外文翻译成绩比例,文献综述成绩,文献综述成绩比例,开题报告答辩成绩,开题报告答辩成绩比例
    
    映射规则（能填多少填多少）：
    - 学号 → student_id
    - 姓名 → student_name
    - 学生学院/课题主管学院 → college
    - 专业名称 → student_major
    - 班级 → class_name
    - 课题名称 → paper_title
    - 课题类型 → paper_type
    - 指导教师工号 → teacher_id
    - 指导教师姓名 → teacher_name
    - 指导教师职称 → teacher_title
    """
)
async def import_paper_basic_info(file: UploadFile = File(...), db: pymysql.connections.Connection = Depends(get_db)):
    if pd is None:
        raise HTTPException(status_code=500, detail="需要安装 pandas 库")
    
    filename = file.filename or ""
    lower_name = filename.lower()
    if not lower_name.endswith(SUPPORTED_IMPORT_EXTS):
        raise HTTPException(status_code=400, detail=f"仅支持 {', '.join(SUPPORTED_IMPORT_EXTS)} 文件")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")

    rows = []
    
    # 处理不同文件类型
    if lower_name.endswith(('.xlsx', '.xls')):
        # 读取时指定 dtype=str，避免科学计数法问题
        df = pd.read_excel(io.BytesIO(content), dtype=str)
        
        # 处理列名
        df.columns = [
            str(col).strip() if pd.notna(col) else "" 
            for col in df.columns
        ]
        
        # 检查必填列
        required_cols = ["学号", "姓名", "课题名称"]
        missing = [col for col in required_cols if col not in df.columns]
        if missing:
            raise HTTPException(status_code=400, detail=f"文件表头缺少必填列：{', '.join(missing)}。必填列：学号,姓名,课题名称")
        
        # 清理所有单元格的空值和空格
        df = df.fillna("").astype(str)
        for col in df.columns:
            df[col] = df[col].str.strip()
        
        rows = df.to_dict(orient="records")
    else:
        # 处理CSV/TSV文件
        delimiter = "\t" if lower_name.endswith(".tsv") else ","
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = content.decode("gbk")
            except UnicodeDecodeError:
                raise HTTPException(status_code=400, detail="文件编码仅支持 UTF-8 或 GBK")

        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        if reader.fieldnames is None:
            raise HTTPException(status_code=400, detail="CSV 文件缺少标题行或文件为空")
        required_cols = ["学号", "姓名", "课题名称"]
        missing = [col for col in required_cols if col not in reader.fieldnames]
        if missing:
            raise HTTPException(status_code=400, detail=f"文件表头缺少必填列：{', '.join(missing)}。必填列：学号,姓名,课题名称")
        rows = list(reader)

    cursor = None
    created = 0
    updated = 0
    created_items = []
    updated_items = []
    
    try:
        cursor = db.cursor()
        
        for row in rows:
            # 安全获取字符串值
            def safe_get_str(key):
                val = row.get(key)
                if val is None or (pd is not None and pd.isna(val)):
                    return ""
                s = str(val)
                return s.strip()
            
            # 获取数据
            student_id = safe_get_str("学号")
            student_name = safe_get_str("姓名")
            paper_title = safe_get_str("课题名称")
            
            # 必填字段校验
            if not student_id or not student_name or not paper_title:
                continue
            
            # 可选字段
            college = safe_get_str("学生学院") or safe_get_str("课题主管学院")
            student_major = safe_get_str("专业名称")
            class_name = safe_get_str("班级")
            paper_type = safe_get_str("课题类型")
            teacher_id = safe_get_str("指导教师工号")
            teacher_name = safe_get_str("指导教师姓名")
            teacher_title = safe_get_str("指导教师职称")
            
            # 执行插入或更新
            cursor.execute(
                """
                INSERT INTO paper_basic_info (
                    college, student_id, student_name, student_major, class_name,
                    teacher_id, teacher_name, teacher_title,
                    paper_title, paper_keywords, paper_type,
                    research_direction, paper_language
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    college = VALUES(college),
                    student_name = VALUES(student_name),
                    student_major = VALUES(student_major),
                    class_name = VALUES(class_name),
                    teacher_id = VALUES(teacher_id),
                    teacher_name = VALUES(teacher_name),
                    teacher_title = VALUES(teacher_title),
                    paper_keywords = VALUES(paper_keywords),
                    paper_type = VALUES(paper_type),
                    research_direction = VALUES(research_direction),
                    paper_language = VALUES(paper_language),
                    updated_at = NOW()
                """,
                (
                    college, student_id, student_name, student_major, class_name,
                    teacher_id, teacher_name, teacher_title,
                    paper_title, "", paper_type,
                    "", "中文"
                ),
            )
            
            # 获取记录ID
            cursor.execute("SELECT id FROM paper_basic_info WHERE student_id = %s AND paper_title = %s", (student_id, paper_title))
            rid = cursor.fetchone()
            rec_id = rid[0] if rid else None
            
            if cursor.rowcount == 1:
                created += 1
                created_items.append({
                    "id": rec_id,
                    "student_id": student_id,
                    "student_name": student_name,
                    "paper_title": paper_title
                })
            else:
                updated += 1
                updated_items.append({
                    "id": rec_id,
                    "student_id": student_id,
                    "student_name": student_name,
                    "paper_title": paper_title
                })
        
        db.commit()
        return {
            "message": "导入完成",
            "created": created,
            "updated": updated,
            "created_items": created_items,
            "updated_items": updated_items,
        }
    except pymysql.MySQLError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")
    finally:
        if cursor:
            cursor.close()
