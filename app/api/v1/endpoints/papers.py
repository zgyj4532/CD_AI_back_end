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
from app.schemas.document import (
    PaperOut,
    PaperStatusOut,
    VersionOut,
    DDLOut, 
)
from app.services.oss import get_file_from_oss, upload_paper_to_storage
from datetime import datetime
from app.database import get_db
import pymysql
import json

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
    teacher_id: int = Query(..., description="关联的老师ID，必须传入且为有效正整数"),
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="提交者信息(JSON字符串，包含 sub/username/roles)"),
):
    current_user = _parse_current_user(current_user)
    submitter_id = current_user.get("sub", 0)  
    # 参数校验
    if not isinstance(owner_id, int) or owner_id <= 0:
        raise HTTPException(status_code=400, detail="owner_id必须是正整数")
    if not isinstance(teacher_id, int) or teacher_id <= 0:
        raise HTTPException(status_code=400, detail="teacher_id必须是正整数")
    if owner_id != submitter_id:
        raise HTTPException(
            status_code=403,
            detail="无权限上传：论文归属者ID必须与当前登录用户ID一致"
        )
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
        paper_sql = """
        INSERT INTO papers (
            owner_id, teacher_id, version, size, status, ddl, oss_key, pdf_oss_key,
            submitted_by_name, submitted_by_role, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(
            paper_sql,
            (
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
            ),
        )
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
        return PaperOut(id=paper_id, owner_id=paper_owner_id, teacher_id=teacher_id, latest_version=version, oss_key=oss_key)
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
        is_student = (login_user_id == student_id)
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
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        
        is_student = (login_user_id == student_id)
        is_teacher = (login_user_id == teacher_id)
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
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        if paper_teacher_id != login_user_id:
            raise HTTPException(
                status_code=403,
                detail=f"无权限提交审阅：论文ID {paper_id} 关联的教师ID为 {paper_teacher_id}，当前登录教师ID为 {login_user_id}"
            )
        cursor.execute(
            "SELECT id FROM paper_reviews WHERE paper_id = %s AND teacher_id = %s LIMIT 1",
            (paper_id, login_user_id)
        )
        existing_review = cursor.fetchone()
        if existing_review:
            raise HTTPException(
                status_code=400,
                detail=f"论文ID {paper_id} 已存在审阅记录（ID：{existing_review[0]}），如需修改请使用更新审阅接口"
            )
        now = datetime.now()
        review_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
        insert_sql = """
        INSERT INTO paper_reviews (
            paper_id, teacher_id, review_content, review_time, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """
        cursor.execute(
            insert_sql,
            (
                paper_id,
                login_user_id,
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
        if paper_teacher_id != login_user_id:
            raise HTTPException(
                status_code=403,
                detail=f"无权限更新审阅：论文ID {paper_id} 关联的教师ID为 {paper_teacher_id}，当前登录教师ID为 {login_user_id}"
            )
        cursor.execute(
            "SELECT id, review_content FROM paper_reviews WHERE paper_id = %s AND teacher_id = %s LIMIT 1",
            (paper_id, login_user_id)
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
                login_user_id
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
        if is_teacher and login_user_id == paper_teacher_id:
            permission_denied = False
        # 学生权限：角色是学生 + 登录ID匹配论文的owner_id
        if is_student and login_user_id == paper_owner_id:
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
        
        is_owner = (paper_owner_id == submitter_id)
        is_teacher = (paper_teacher_id == submitter_id)
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
        is_owner = (owner_id == login_user_id)
        is_teacher = (paper_teacher_id == login_user_id)
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
        if paper_owner_id != student_id:
            raise HTTPException(
                status_code=400,
                detail=f"传入的学生ID({student_id})与论文归属者ID({paper_owner_id})不一致"
            )
        is_student = (login_user_id == paper_owner_id)  
        is_teacher = (login_user_id == teacher_id)    
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
        
        # 检查群组是否已经有DDL
        if members:
            # 获取群组第一个成员的ID
            first_member_id = members[0][0]
            # 检查该成员是否有任何DDL消息（无论是否已读）
            cursor.execute(
                "SELECT id FROM user_messages WHERE user_id = %s AND source = 'ddl'", 
                (str(first_member_id),)
            )
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
    response_model=List[DDLOut],
    summary="查看DDL列表",
    description="教师可查看自己创建的所有DDL，管理员可查看所有DDL"
)
def list_ddl(
    teacher_id: int = Query(..., description="教师ID（查询该教师创建的DDL）"),
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
):
    current_user = _parse_current_user(current_user)
    login_user_id = current_user.get("sub", 0)
    login_user_roles = current_user.get("roles", [])
    # 基础校验
    if login_user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    if not isinstance(teacher_id, int) or teacher_id <= 0:
        raise HTTPException(status_code=400, detail="teacher_id必须是正整数")
    # 权限校验
    is_admin = "admin" in login_user_roles or "管理员" in login_user_roles
    if teacher_id != login_user_id and not is_admin:
        raise HTTPException(
            status_code=403,
            detail=f"无权限查看：仅可查看自己创建的DDL或管理员查看，传入的teacher_id({teacher_id})与登录用户ID({login_user_id})不一致"
        )
    # 数据库查询
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        query_sql = """
        SELECT ddlid, teacher_id, teacher_name, ddl_time, created_at, updated_at
        FROM ddl_management 
        WHERE teacher_id = %s 
        ORDER BY ddl_time DESC
        """
        cursor.execute(query_sql, (teacher_id,))
        ddl_records = cursor.fetchall()
        result = []
        for record in ddl_records:
            # 处理datetime对象转字符串
            ddl_time_str = record["ddl_time"].strftime("%Y-%m-%d %H:%M:%S") if isinstance(record["ddl_time"], datetime) else record["ddl_time"]
            created_at_str = record["created_at"].strftime("%Y-%m-%d %H:%M:%S") if isinstance(record["created_at"], datetime) else record["created_at"]
            
            result.append(DDLOut(
                ddlid=record["ddlid"],
                creator_id=record["teacher_id"],
                teacher_id=record["teacher_id"],
                ddl_time=ddl_time_str,
                created_at=created_at_str
            ))
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
        is_owner = ddl_teacher_id == login_user_id
        
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
            cursor.execute(delete_messages_sql, (f'%\"group_id\": {group_id}%',))
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
        is_owner = ddl_teacher_id == login_user_id
        
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
        # 仅查询指定字段（严格匹配你要求的列表）
        paper_sql = """
        SELECT 
            id, owner_id, teacher_id, version, size, status, detail, 
            DATE_FORMAT(ddl, '%%Y-%%m-%%d %%H:%%i:%%s') as ddl,
            oss_key, pdf_oss_key
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
        if submitter_id not in [paper_owner_id, paper_teacher_id]:
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
