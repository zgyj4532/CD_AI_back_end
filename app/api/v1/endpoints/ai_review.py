from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from typing import Optional
import pymysql
from app.database import get_db
from app.services.oss import get_file_from_oss
from app.services.ai_adapter import submit_ai_review, submit_ai_review_file, get_ai_report_by_paper_id

router = APIRouter()


@router.post(
    "/{paper_id}/ai-review",
    summary="触发 AI 评审",
    description="提交评审任务到后台队列"
)
def trigger_ai_review(
    paper_id: int, 
    background_tasks: BackgroundTasks, 
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
    db: pymysql.connections.Connection = Depends(get_db)
):
    current_user = _parse_current_user(current_user)
    submitter_id = current_user.get("sub", 0)
    if submitter_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    
    # 检查用户是否存在
    username = current_user.get("username", "")
    roles = current_user.get("roles", [])
    if not _check_user_exists(submitter_id, username, roles, db):
        raise HTTPException(status_code=404, detail="用户不存在")
    
    # 检查用户是否有权限触发评审
    _check_permission(submitter_id, roles, paper_id, db)
    
    # 这里将任务交给后台/任务队列
    try:
        background_tasks.add_task(submit_ai_review, paper_id, current_user)
    except Exception:
        raise HTTPException(status_code=503, detail="AI 服务暂时不可用")
    return {"status": "排队中", "message": "任务已加入队列"}


def _parse_current_user(current_user: Optional[str]) -> dict:
    try:
        if not current_user:
            return {"sub": 0, "username": "", "roles": []}
        import urllib.parse
        raw = urllib.parse.unquote(current_user)
        if not raw.strip():
            return {"sub": 0, "username": "", "roles": []}
        # 不再直接将纯数字转换为用户，而是要求必须是有效的 JSON 格式
        import json
        data = json.loads(raw)
        if isinstance(data, dict):
            sub_value = data.get("sub", 0)
            if isinstance(sub_value, str) and sub_value.isdigit():
                data["sub"] = int(sub_value)
            elif isinstance(sub_value, int):
                data["sub"] = sub_value
            else:
                data["sub"] = 0
            
            # 验证用户名
            username = data.get("username", "")
            if not username:
                data["username"] = ""
            
            # 验证角色是否有效，只保留 student、teacher、admin
            roles = data.get("roles", [])
            if isinstance(roles, str):
                roles = [roles]
            valid_roles = ["student", "teacher", "admin"]
            filtered_roles = [role for role in roles if role in valid_roles]
            data["roles"] = filtered_roles
            return data
    except Exception:
        pass
    return {"sub": 0, "username": "", "roles": []}


def _check_user_exists(user_id: int, username: str, roles: list, db: pymysql.connections.Connection) -> bool:
    """检查用户是否存在"""
    cursor = None
    try:
        cursor = db.cursor()
        
        # 根据角色检查用户是否存在
        for role in roles:
            if role == "student":
                # 检查用户是否在学生表中，并且用户名匹配
                cursor.execute("SELECT id, student_id FROM students WHERE id = %s", (user_id,))
                row = cursor.fetchone()
                if row and (not username or row[1] == username):
                    return True
            elif role == "teacher":
                # 检查用户是否在教师表中，并且用户名匹配
                cursor.execute("SELECT id, teacher_id FROM teachers WHERE id = %s", (user_id,))
                row = cursor.fetchone()
                if row and (not username or row[1] == username):
                    return True
            elif role == "admin":
                # 检查用户是否在管理员表中，并且用户名匹配
                cursor.execute("SELECT id, admin_id FROM admins WHERE id = %s", (user_id,))
                row = cursor.fetchone()
                if row and (not username or row[1] == username):
                    return True
        
        # 如果没有指定角色，返回 False
        return False
    except pymysql.MySQLError as e:
        # 打印数据库错误信息，方便调试
        print(f"数据库错误: {e}")
        return False
    except Exception as e:
        # 打印其他错误信息，方便调试
        print(f"其他错误: {e}")
        return False
    finally:
        if cursor:
            cursor.close()


def _check_permission(user_id: int, roles: list, paper_id: int, db: pymysql.connections.Connection) -> bool:
    """检查用户是否有权限操作指定论文"""
    cursor = None
    try:
        cursor = db.cursor()
        cursor.execute(
            "SELECT owner_id, teacher_id FROM papers WHERE id = %s",
            (paper_id,)
        )
        paper_info = cursor.fetchone()
        if not paper_info:
            raise HTTPException(status_code=404, detail="论文不存在")
        
        owner_id, teacher_id = paper_info
        is_admin = "admin" in roles
        if user_id != owner_id and user_id != teacher_id and not is_admin:
            raise HTTPException(status_code=403, detail="无权限操作该论文")
        return True
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")
    finally:
        if cursor:
            cursor.close()


@router.post(
    "/{paper_id}/quick-audit",
    summary="快速 AI 评审",
    description="根据论文ID从papers表中获取论文并返回 AI 评审结果"
)
async def quick_audit(
    paper_id: int,
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
    db: pymysql.connections.Connection = Depends(get_db)
):
    current_user = _parse_current_user(current_user)
    submitter_id = current_user.get("sub", 0)
    if submitter_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    
    # 检查用户是否存在
    username = current_user.get("username", "")
    roles = current_user.get("roles", [])
    if not _check_user_exists(submitter_id, username, roles, db):
        raise HTTPException(status_code=404, detail="用户不存在")

    # 从数据库中查询论文信息
    cursor = None
    try:
        cursor = db.cursor()
        cursor.execute(
            "SELECT owner_id, teacher_id, oss_key, pdf_oss_key, version FROM papers WHERE id = %s",
            (paper_id,)
        )
        paper_info = cursor.fetchone()
        if not paper_info:
            raise HTTPException(status_code=404, detail="论文不存在")
        
        # 检查用户是否有权限进行评审
        _check_permission(submitter_id, roles, paper_id, db)
        
        owner_id, teacher_id, oss_key, pdf_oss_key, version = paper_info
        
        # 从OSS获取文件内容
        try:
            if oss_key:
                filename, contents = get_file_from_oss(oss_key)
            elif pdf_oss_key:
                filename, contents = get_file_from_oss(pdf_oss_key)
            else:
                raise HTTPException(status_code=404, detail="论文文件不存在")
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"获取论文文件失败：{str(e)}")
        
        if not contents:
            raise HTTPException(status_code=400, detail="文件内容不能为空")

        # 执行AI评审
        try:
            # 将 paper_id 添加到用户信息中，以便存储报告
            current_user["paper_id"] = paper_id
            report = submit_ai_review_file(contents, filename, current_user)
        except Exception:
            raise HTTPException(status_code=503, detail="AI 服务暂时不可用")

        return {"status": "完成", "paper_id": paper_id, "filename": filename, "report": report}
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库操作失败: {str(e)}")
    finally:
        if cursor:
            cursor.close()


@router.get(
    "/{paper_id}/ai-report",
    summary="获取 AI 报告",
    description="查询并返回指定论文的 AI 评审报告"
)
def get_ai_report(
    paper_id: int, 
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
    db: pymysql.connections.Connection = Depends(get_db)
):
    current_user = _parse_current_user(current_user)
    submitter_id = current_user.get("sub", 0)
    if submitter_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    
    # 检查用户是否存在
    username = current_user.get("username", "")
    roles = current_user.get("roles", [])
    if not _check_user_exists(submitter_id, username, roles, db):
        raise HTTPException(status_code=404, detail="用户不存在")
    
    # 检查用户是否有权限获取 AI 报告
    _check_permission(submitter_id, roles, paper_id, db)
    
    # TODO: 从 ai_reports 表读取结构化报告
    report = get_ai_report_by_paper_id(paper_id)
    if report.get("status") == "NOT_FOUND":
        raise HTTPException(status_code=404, detail="AI 报告未找到")

    return {
        "paper_id": paper_id,
        "report": report,
    }

