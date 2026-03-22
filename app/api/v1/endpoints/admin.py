from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pathlib import Path
from app.services.oss import upload_file_to_oss
import pymysql
from datetime import datetime
from app.database import get_db
import uuid
import json
from app.middleware.operation_logger import record_operation_log

router = APIRouter()


def admin_only(
    # 注释掉认证依赖，保留参数行
    # user=Depends(get_current_user)
):
    # 注释掉原有角色校验逻辑
    # if user.get("role") != "admin":
    #     raise HTTPException(status_code=403, detail="仅管理员可访问")
    
    # 模拟管理员用户
    mock_admin_user = {
        "id": "admin_001",
        "role": "admin",
        "username": "test_admin"
    }
    return mock_admin_user


@router.post(
    "/templates",
    summary="上传模板",
    description="上传模板文件并存储元数据"
)
async def upload_template(
    file: UploadFile = File(...),
    user=Depends(admin_only),
    db: pymysql.connections.Connection = Depends(get_db)
):
    content = await file.read()
    key = upload_file_to_oss(file.filename, content)
    template_id = f"tpl_{uuid.uuid4().hex[:8]}"  
    
    # 定义模板元数据
    template_metadata = {
        "template_id": template_id,
        "oss_key": key,
        "filename": file.filename,
        "content_type": file.content_type,
        "uploader_id": user.get("id"),  
        "upload_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    
    try:
        cursor = db.cursor()
        insert_sql = """
        INSERT INTO templates (template_id, oss_key, filename, content_type, uploader_id, upload_time)
        VALUES (%s, %s, %s, %s, %s, %s);
        """
        cursor.execute(
            insert_sql,
            (
                template_metadata["template_id"],
                template_metadata["oss_key"],
                template_metadata["filename"],
                template_metadata["content_type"],
                template_metadata["uploader_id"],
                template_metadata["upload_time"]
            )
        )
        db.commit()
    except pymysql.MySQLError as e:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"模板元数据存储失败：{str(e)}"
        )
    finally:
        cursor.close()
    return {"template_id": template_id, "oss_key": key, "storage_path": key}


@router.put(
    "/templates/{template_id}",
    summary="更新模板",
    description="重新上传模板并更新元数据"
)
async def update_template(
    template_id: str,
    file: UploadFile = File(...),
    user=Depends(admin_only),
    db: pymysql.connections.Connection = Depends(get_db)
):
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")
    key = upload_file_to_oss(file.filename, content)
    upload_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cursor = None
    old_key = None
    try:
        cursor = db.cursor()
        cursor.execute("SELECT id, oss_key FROM templates WHERE template_id = %s", (template_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="模板不存在")
        if isinstance(row, dict):
            old_key = row.get("oss_key")
        else:
            old_key = row[1] if len(row) > 1 else None

        update_sql = """
        UPDATE templates
        SET oss_key = %s,
            filename = %s,
            content_type = %s,
            uploader_id = %s,
            upload_time = %s
        WHERE template_id = %s;
        """
        cursor.execute(
            update_sql,
            (
                key,
                file.filename,
                file.content_type,
                user.get("id"),
                upload_time,
                template_id,
            ),
        )
        db.commit()
        if old_key:
            old_path = Path(old_key)
            if old_path.is_file():
                old_path.unlink(missing_ok=True)
        return {
            "template_id": template_id,
            "oss_key": key,
            "storage_path": key,
            "filename": file.filename,
            "content_type": file.content_type,
            "upload_time": upload_time,
        }
    except pymysql.MySQLError as e:
        db.rollback()
        new_path = Path(key)
        if new_path.is_file():
            new_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"模板更新失败：{str(e)}")
    finally:
        if cursor:
            cursor.close()


@router.delete(
    "/templates/{template_id}",
    summary="删除模板",
    description="根据模板ID删除记录"
)
def delete_template(
    template_id: str,
    user=Depends(admin_only),
    db: pymysql.connections.Connection = Depends(get_db)
):
    cursor = None
    file_path = None
    try:
        cursor = db.cursor()
        cursor.execute("SELECT id, oss_key FROM templates WHERE template_id = %s", (template_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="模板不存在")
        if isinstance(row, dict):
            file_path = row.get("oss_key")
        else:
            file_path = row[1] if len(row) > 1 else None

        cursor.execute("DELETE FROM templates WHERE template_id = %s", (template_id,))
        db.commit()
        if file_path:
            path = Path(file_path)
            if path.is_file():
                path.unlink(missing_ok=True)
        return {"message": "删除成功", "template_id": template_id}
    except pymysql.MySQLError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"模板删除失败：{str(e)}")
    finally:
        if cursor:
            cursor.close()


@router.get(
    "/templates/{template_id}/download",
    summary="导出模板",
    description="根据模板ID导出模板文件"
)
def download_template(
    template_id: str,
    user=Depends(admin_only),
    db: pymysql.connections.Connection = Depends(get_db),
):
    cursor = None
    try:
        cursor = db.cursor()
        cursor.execute(
            "SELECT oss_key, filename, content_type FROM templates WHERE template_id = %s",
            (template_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="模板不存在")

        if isinstance(row, dict):
            oss_key = row.get("oss_key")
            filename = row.get("filename") or "template"
            content_type = row.get("content_type") or "application/octet-stream"
        else:
            oss_key = row[0]
            filename = row[1] or "template"
            content_type = row[2] or "application/octet-stream"

        if not oss_key:
            raise HTTPException(status_code=500, detail="模板存储路径缺失")

        file_path = Path(oss_key)
        if not file_path.is_file():
            raise HTTPException(status_code=404, detail="模板文件不存在")

        return FileResponse(
            path=str(file_path),
            media_type=content_type,
            filename=filename,
        )
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"模板导出失败：{str(e)}")
    finally:
        if cursor:
            cursor.close()

@router.get(
    "/dashboard/stats",
    summary="仪表盘统计",
    description="按学院汇总论文数量并返回总数"
)
def dashboard_stats(
    user=Depends(admin_only),  
    db: pymysql.connections.Connection = Depends(get_db) 
):
    cursor = None
    try:
        cursor = db.cursor()
        # 聚合论文数据：按学院分组统计论文数量
        stats_sql = """
        SELECT p.owner_id, CASE WHEN t.id IS NOT NULL THEN COALESCE(t.department_name, '未知院系') WHEN s.id IS NOT NULL THEN COALESCE(s.department_name, '未知院系') ELSE '未知' END AS college
        FROM papers p
        LEFT JOIN students s ON p.owner_id = s.id
        LEFT JOIN teachers t ON p.owner_id = t.id;
        """
        cursor.execute(stats_sql)
        rows = cursor.fetchall()
        
        # 在 Python 中分组统计
        from collections import defaultdict
        college_count = defaultdict(int)
        for owner_id, college in rows:
            college_count[college] += 1
        
        college_stats = [(college, count) for college, count in college_count.items()]
        
        # 统计论文总数
        total_sql = "SELECT COUNT(*) FROM papers;"
        cursor.execute(total_sql)
        total_papers = cursor.fetchone()[0]
        
        # 格式化按学院分组的统计结果
        by_college = []
        for item in college_stats:
            by_college.append({
                "college": item[0] if item[0] else "未归属学院",
                "paper_count": item[1]
            })
        
        # 返回结构化统计数据
        return {
            "total_papers": total_papers,
            "by_college": by_college,
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    
    except pymysql.MySQLError as e:
        raise HTTPException(
            status_code=500,
            detail=f"统计数据查询失败：{str(e)}"
        )
    finally:
        if cursor:
            cursor.close()


@router.get(
    "/audit/logs",
    summary="审计日志查询",
    description="分页查询操作日志记录"
)
def audit_logs(
    user=Depends(admin_only),  
    page: int = 1,
    page_size: int = 50,
    db: pymysql.connections.Connection = Depends(get_db)  
):
    # 待办：查询操作日志表并返回分页结果
    # 待办：查询操作日志表并返回分页结果
    cursor = None
    try:
        # 校验分页参数合法性
        if page < 1:
            page = 1
        if page_size < 1 or page_size > 100:  # 限制单页最大条数，避免性能问题
            page_size = 50
        
        cursor = db.cursor()
        # 计算分页偏移量
        offset = (page - 1) * page_size
        
        # 查询分页数据（按操作时间倒序）
        select_sql = """
        SELECT id, user_id, username, operation_type, operation_path, 
               operation_params, ip_address, operation_time, status
        FROM operation_logs
        ORDER BY operation_time DESC
        LIMIT %s OFFSET %s;
        """
        cursor.execute(select_sql, (page_size, offset))
        log_items = cursor.fetchall()
        
        # 查询总条数（用于分页计算）
        count_sql = "SELECT COUNT(*) FROM operation_logs;"
        cursor.execute(count_sql)
        total = cursor.fetchone()[0]
        
        # 格式化返回数据（适配前端展示）
        items = []
        for log in log_items:
            items.append({
                "id": log[0],
                "user_id": log[1],
                "username": log[2],
                "operation_type": log[3],
                "operation_path": log[4],
                "operation_params": log[5],
                "ip_address": log[6],
                "operation_time": log[7].strftime("%Y-%m-%d %H:%M:%S") if log[7] else None,
                "status": log[8]
            })
        
        # 组装分页返回结果
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size  # 向上取整计算总页数
        }
    
    except pymysql.MySQLError as e:
        raise HTTPException(
            status_code=500,
            detail=f"查询操作日志失败：{str(e)}"
        )
    finally:
        if cursor:
            cursor.close()


@router.get(
    "/stats/students/total",
    summary="计算学生总数",
    description="统计学生信息表中的总记录数（仅管理员可访问）"
)
def calculate_total_students(
    user=Depends(admin_only),
    db: pymysql.connections.Connection = Depends(get_db)
):
    cursor = None
    try:
        cursor = db.cursor()
        count_sql = "SELECT COUNT(*) FROM students;"
        cursor.execute(count_sql)
        total_students = cursor.fetchone()[0]
        return {
            "total_students": total_students,
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": 200,
            "message": "学生总数统计成功"
        }
    except pymysql.MySQLError as e:
        raise HTTPException(
            status_code=500,
            detail=f"计算学生总数失败：{str(e)}"
        )
    finally:
        if cursor:
            cursor.close()


@router.get(
    "/stats/teachers/total",
    summary="计算教师总数",
    description="统计教师信息表中的总记录数（仅管理员可访问）"
)
def calculate_total_teachers(
    user=Depends(admin_only),
    db: pymysql.connections.Connection = Depends(get_db)
):
    cursor = None
    try:
        cursor = db.cursor()
        count_sql = "SELECT COUNT(*) FROM teachers;"
        cursor.execute(count_sql)
        total_teachers = cursor.fetchone()[0]
        return {
            "total_teachers": total_teachers,
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": 200,
            "message": "教师总数统计成功"
        }
    except pymysql.MySQLError as e:
        raise HTTPException(
            status_code=500,
            detail=f"计算教师总数失败：{str(e)}"
        )
    finally:
        if cursor:
            cursor.close()


@router.get(
    "/stats/papers/uploaded/total",
    summary="计算总已上传论文数",
    description="统计论文表中状态为「已上传」的论文总数（仅管理员可访问）"
)
def calculate_total_uploaded_papers(
    user=Depends(admin_only),
    db: pymysql.connections.Connection = Depends(get_db)
):
    cursor = None
    try:
        cursor = db.cursor()
        count_sql = "SELECT COUNT(*) FROM papers WHERE status = '已上传';"
        cursor.execute(count_sql)
        total_uploaded = cursor.fetchone()[0]
        return {
            "total_uploaded_papers": total_uploaded,
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": 200,
            "message": "已上传论文数统计成功"
        }
    except pymysql.MySQLError as e:
        raise HTTPException(
            status_code=500,
            detail=f"计算已上传论文数失败：{str(e)}"
        )
    finally:
        if cursor:
            cursor.close()


@router.get(
    "/stats/papers/unreviewed/total",
    summary="计算总未审阅论文数",
    description="统计论文表中状态为「未审阅」的论文总数（仅管理员可访问）"
)
def calculate_total_unreviewed_papers(
    user=Depends(admin_only),
    db: pymysql.connections.Connection = Depends(get_db)
):
    cursor = None
    try:
        cursor = db.cursor()
        count_sql = "SELECT COUNT(*) FROM papers WHERE status = '未审阅';"
        cursor.execute(count_sql)
        total_unreviewed = cursor.fetchone()[0]
        return {
            "total_unreviewed_papers": total_unreviewed,
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": 200,
            "message": "未审阅论文数统计成功"
        }
    except pymysql.MySQLError as e:
        raise HTTPException(
            status_code=500,
            detail=f"计算未审阅论文数失败：{str(e)}"
        )
    finally:
        if cursor:
            cursor.close()


@router.get(
    "/stats/papers/updated/total",
    summary="计算总已更新论文数",
    description="统计论文表中状态为「已更新」的论文总数（仅管理员可访问）"
)
def calculate_total_updated_papers(
    user=Depends(admin_only),
    db: pymysql.connections.Connection = Depends(get_db)
):
    cursor = None
    try:
        cursor = db.cursor()
        count_sql = "SELECT COUNT(*) FROM papers WHERE status = '已更新';"
        cursor.execute(count_sql)
        total_updated = cursor.fetchone()[0]
        return {
            "total_updated_papers": total_updated,
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "code": 200,
            "message": "已更新论文数统计成功"
        }
    except pymysql.MySQLError as e:
        raise HTTPException(
            status_code=500,
            detail=f"计算已更新论文数失败：{str(e)}"
        )
    finally:
        if cursor:
            cursor.close()


@router.post(
    "/audit/logs/record",
    summary="记录操作日志",
    description="记录操作日志到数据库"
)
async def record_log(
    request: Request,
    user=Depends(admin_only),
    db: pymysql.connections.Connection = Depends(get_db)
):
    """记录操作日志到数据库"""
    try:
        # 获取请求信息
        method = request.method
        path = request.url.path
        client_ip = request.client.host if request.client else 'Unknown'
        
        # 获取用户信息
        user_id = str(user.get("id", ""))
        username = user.get("username", "")
        
        # 获取请求参数
        try:
            if request.method in ["POST", "PUT", "PATCH"]:
                body = await request.body()
                try:
                    params = json.loads(body.decode())
                except Exception:
                    params = str(body.decode())
            else:
                params = dict(request.query_params)
        except Exception:
            params = {}
        
        # 记录操作日志
        record_operation_log(
            user_id=user_id,
            username=username,
            operation_type=method,
            operation_path=path,
            operation_params=params,
            ip_address=client_ip,
            status="success"
        )
        
        return {
            "message": "操作日志记录成功",
            "code": 200
        }
    except Exception as e:
        # 记录失败日志
        try:
            client_ip = request.client.host if request.client else 'Unknown'
            record_operation_log(
                user_id=str(user.get("id", "")) if user else "",
                username=user.get("username", "") if user else "",
                operation_type=request.method,
                operation_path=request.url.path,
                operation_params={},
                ip_address=client_ip,
                status="failure"
            )
        except Exception:
            pass
        raise HTTPException(
            status_code=500,
            detail=f"记录操作日志失败：{str(e)}"
        )