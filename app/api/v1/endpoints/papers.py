import zipfile
import urllib.parse
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, BackgroundTasks, Query,Body
from fastapi.responses import StreamingResponse
from typing import List, Optional
import os
import io
import sys
import shutil
import subprocess
import tempfile
from app.core.dependencies import get_current_user
from app.schemas.document import (
    PaperCreate,
    PaperOut,
    PaperStatusCreate,
    PaperStatusOut,
    PaperStatusUpdate,
    VersionOut,
    DDLOut, 
    DDLCreate, 
)
from app.services.oss import upload_file_to_oss, get_file_from_oss, upload_paper_to_storage
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
            owner_id, teacher_id, latest_version, version, size, status, oss_key, pdf_oss_key,
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
                version,
                size,
                "已上传",
                oss_key,
                pdf_oss_key,
                submitter_name,
                submitter_role,
                now,
                now,
            ),
        )
        paper_id = cursor.lastrowid
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

    return PaperOut(id=paper_id, owner_id=owner_id, teacher_id=teacher_id, latest_version=version, oss_key=oss_key)


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
        cursor.execute("SELECT owner_id, latest_version, teacher_id FROM papers WHERE id = %s", (paper_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="论文不存在")
        paper_owner_id, current_version_str, teacher_id = row
        if paper_owner_id != submitter_id:
            raise HTTPException(status_code=403, detail="无权限更新该论文")
        current_version = _parse_version(current_version_str)
        new_version = _parse_version(version)
        if new_version <= current_version:
            raise HTTPException(
                status_code=400,
                detail=f"新版本号必须大于当前最新版本号 {current_version_str}，当前提交的版本号 {version} 不符合要求"
            )
        
        # 上传docx文件
        oss_key = upload_paper_to_storage(file.filename, contents)
        
        # 转换docx到pdf并上传到OSS
        pdf_content, pdf_filename = convert_docx_to_pdf(contents, file.filename)
        pdf_oss_key = upload_paper_to_storage(pdf_filename, pdf_content)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        submitter_name = current_user.get("username") or ""
        roles = current_user.get("roles") or []
        submitter_role = ",".join([str(r) for r in roles]) if isinstance(roles, list) else str(roles)

        cursor.execute(
            """
            UPDATE papers
            SET latest_version = %s,
                version = %s,
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
        cursor.execute("SELECT owner_id, teacher_id FROM papers WHERE id = %s", (paper_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="论文不存在")
        paper_owner_id, teacher_id = row
        is_owner = (paper_owner_id == current_id)
        is_admin = ("admin" in current_roles) or ("管理员" in current_roles)
        if not is_owner and not is_admin:
            raise HTTPException(
                status_code=403,
                detail=f"无权限删除该论文：仅论文归属者（ID={paper_owner_id}）或管理员可删除，当前登录用户ID={current_id}，角色={current_roles}"
            )
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
        cursor.execute("SELECT owner_id, teacher_id, latest_version, oss_key, pdf_oss_key, size FROM papers WHERE id = %s", (paper_id,))
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
            "SELECT owner_id, teacher_id, latest_version, oss_key, pdf_oss_key, size FROM papers WHERE id = %s", 
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
                detail=f"论文最近有效状态为【已定稿】，不允许修改任何状态"
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
    if not isinstance(owner_id, int) or owner_id <= 0:
        raise HTTPException(status_code=400, detail="owner_id必须是正整数")
    
    cursor_check = None
    try:
        cursor_check = db.cursor()
        cursor_check.execute("SELECT teacher_id FROM papers WHERE owner_id = %s LIMIT 1", (owner_id,))
        paper_teacher_id = cursor_check.fetchone()
        paper_teacher_id = paper_teacher_id[0] if paper_teacher_id else 0
        
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
    
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor) 
        query_sql = """
        SELECT id, owner_id, teacher_id, latest_version, oss_key, created_at, updated_at
        FROM papers 
        WHERE owner_id = %s 
        ORDER BY created_at DESC
        """
        cursor.execute(query_sql, (owner_id,))
        paper_records = cursor.fetchall()
        
        result = []
        for record in paper_records:
            result.append(
                PaperOut(
                    id=record["id"],
                    owner_id=record["owner_id"],
                    teacher_id=record["teacher_id"],
                    latest_version=record["latest_version"],
                    oss_key=record["oss_key"]
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
            "SELECT owner_id, teacher_id, latest_version, oss_key FROM papers WHERE id = %s",
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
    description="仅教师可创建，且登录用户ID必须与教师ID一致，截止时间需精确到年月日时分秒"
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
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
):
    current_user = _parse_current_user(current_user)
    login_user_id = current_user.get("sub", 0)
    login_user_roles = current_user.get("roles", [])
    teacher_name = current_user.get("username", "") 
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
    cursor = None
    try:
        cursor = db.cursor()
        create_sql = """
        INSERT INTO ddl_management (creator_id, teacher_id, teacher_name, ddl_time, created_at)
        VALUES (%s, %s, %s, %s, %s)
        """
        create_time = now.strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(create_sql, (login_user_id, teacher_id, teacher_name, ddl_time, create_time))
        ddlid = cursor.lastrowid
        db.commit()
        return DDLOut(
            ddlid=ddlid,
            creator_id=login_user_id,
            teacher_id=teacher_id,
            teacher_name=teacher_name, 
            ddl_time=ddl_time.strftime("%Y-%m-%d %H:%M:%S"),
            created_at=create_time
        )
    except pymysql.MySQLError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"创建DDL失败：{str(e)}")
    finally:
        if cursor:
            cursor.close()

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
    if login_user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    if not isinstance(teacher_id, int) or teacher_id <= 0:
        raise HTTPException(status_code=400, detail="teacher_id必须是正整数")
    is_admin = "admin" in login_user_roles or "管理员" in login_user_roles
    if teacher_id != login_user_id and not is_admin:
        raise HTTPException(
            status_code=403,
            detail=f"无权限查看：仅可查看自己创建的DDL或管理员查看，传入的teacher_id({teacher_id})与登录用户ID({login_user_id})不一致"
        )
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        query_sql = """
        SELECT ddlid, creator_id, teacher_id, teacher_name, ddl_time, created_at
        FROM ddl_management 
        WHERE teacher_id = %s 
        ORDER BY ddl_time DESC
        """
        cursor.execute(query_sql, (teacher_id,))
        ddl_records = cursor.fetchall()
        result = []
        for record in ddl_records:
            result.append(DDLOut(
                ddlid=record["ddlid"],
                creator_id=record["creator_id"],
                teacher_id=record["teacher_id"],
                teacher_name=record["teacher_name"], 
                ddl_time=record["ddl_time"].strftime("%Y-%m-%d %H:%M:%S") if isinstance(record["ddl_time"], datetime) else record["ddl_time"],
                created_at=record["created_at"].strftime("%Y-%m-%d %H:%M:%S") if isinstance(record["created_at"], datetime) else record["created_at"]
            ))
        return result
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"查询DDL失败：{str(e)}")
    finally:
        if cursor:
            cursor.close()

@router.delete(
    "/ddl/{ddlid}",
    summary="删除DDL",
    description="仅创建该DDL的教师可删除，或管理员可删除"
)
def delete_ddl(
    ddlid: int,
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
):
    current_user = _parse_current_user(current_user)
    login_user_id = current_user.get("sub", 0)
    login_user_roles = current_user.get("roles", [])
    if login_user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    if not isinstance(ddlid, int) or ddlid <= 0:
        raise HTTPException(status_code=400, detail="ddlid必须是正整数")
    cursor = None
    try:
        cursor = db.cursor()
        check_sql = "SELECT teacher_id, teacher_name FROM ddl_management WHERE ddlid = %s"
        cursor.execute(check_sql, (ddlid,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"DDL ID {ddlid} 不存在")
        ddl_teacher_id = row[0]

        is_admin = "admin" in login_user_roles or "管理员" in login_user_roles
        is_owner = ddl_teacher_id == login_user_id
        
        if not is_owner and not is_admin:
            raise HTTPException(
                status_code=403,
                detail=f"无权限删除：仅创建该DDL的教师（ID={ddl_teacher_id}）或管理员可删除，当前登录用户ID={login_user_id}"
            )
        delete_sql = "DELETE FROM ddl_management WHERE ddlid = %s"
        cursor.execute(delete_sql, (ddlid,))
        db.commit()
        
        return {
            "message": f"DDL {ddlid} 删除成功",
            "ddlid": ddlid,
            "deleted_by": login_user_id,
            "deleted_by_role": login_user_roles
        }
    except pymysql.MySQLError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"删除DDL失败：{str(e)}")
    finally:
        if cursor:
            cursor.close()
