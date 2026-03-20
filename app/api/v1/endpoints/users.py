from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, Body
import csv
import io
import pymysql
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime
from app.schemas.user import (
    StudentCreate,
    TeacherCreate,
    AdminCreate,
    UserUpdate,
    UserOut,
    UserBindPhone,
    UserBindEmail,
    LoginRequest,
    LoginResponse,
)
from app.database import get_db
from app.core.dependencies import get_current_user
from app.core.security import create_access_token, get_password_hash, verify_password
from loguru import logger


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
        import json
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"sub": 0, "username": "", "roles": []}


class TeacherSubmitReviewRequest(BaseModel):
    """教师提交审阅请求"""
    paper_id: int
    review_content: str
    
    model_config = {
        "json_schema_extra": {
            "example": {
                "paper_id": 1,
                "review_content": "这篇论文写得很好，结构清晰，逻辑严谨。"
            }
        }
    }


class TeacherUpdateReviewRequest(BaseModel):
    """教师更新审阅请求"""
    paper_id: int
    status: str  # 已审阅、已通过或待更新
    review_content: Optional[str] = None
    
    model_config = {
        "json_schema_extra": {
            "example": {
                "paper_id": 1,
                "status": "已通过",
                "review_content": "论文已审阅通过，建议在结论部分再做一些补充。"
            }
        }
    }



class UserBindSchool(BaseModel):
    school_id: int
    school_name: Optional[str] = None 

class UserBindDepartment(BaseModel):
    department_id: int
    department_name: Optional[str] = None 

router = APIRouter()
SUPPORTED_IMPORT_EXTS = (".csv", ".tsv")

USER_TABLES = {
    "admin": {"table": "admins", "id_col": "admin_id", "role_col": "role"},
    "student": {"table": "students", "id_col": "student_id", "role": "student"},
    "teacher": {"table": "teachers", "id_col": "teacher_id", "role": "teacher"},
}


def _resolve_user_type_from_payload(payload: dict) -> str:
    user_type = (payload.get("user_type") or "").strip().lower()
    if user_type in USER_TABLES:
        return user_type
    roles = payload.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    role_set = {str(role).strip().lower() for role in roles}
    if "admin" in role_set or "管理员" in role_set:
        return "admin"
    if "teacher" in role_set or "教师" in role_set:
        return "teacher"
    if "student" in role_set or "学生" in role_set:
        return "student"
    raise HTTPException(status_code=400, detail="无法识别用户类型")


def _fetch_user_for_login(
    cursor: pymysql.cursors.Cursor,
    username: str,
    user_type: str,
) -> dict | None:
    user_type = _normalize_user_type(user_type)
    info = USER_TABLES[user_type]
    table = info["table"]
    id_col = info["id_col"]
    if user_type == "admin":
        cursor.execute(
            f"""
            SELECT id, {id_col} as username, name as full_name, phone, email, role,
                   password,
                   DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:%%i:%%s') as created_at,
                   DATE_FORMAT(updated_at, '%%Y-%%m-%%d %%H:%%i:%%s') as updated_at
            FROM {table} WHERE {id_col} = %s
            """,
            (username,),
        )
    else:
        cursor.execute(
            f"""
            SELECT id, {id_col} as username, name as full_name, phone, email,
                   password,
                   DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:%%i:%%s') as created_at,
                   DATE_FORMAT(updated_at, '%%Y-%%m-%%d %%H:%%i:%%s') as updated_at
            FROM {table} WHERE {id_col} = %s
            """,
            (username,),
        )
    row = cursor.fetchone()
    if not row:
        return None
    if user_type != "admin":
        row["role"] = user_type
    return row


def _normalize_user_type(user_type: str | None) -> str:
    value = (user_type or "admin").strip().lower()
    if value not in USER_TABLES:
        raise HTTPException(status_code=400, detail="user_type 必须为 student/teacher/admin")
    return value


def _fetch_user(cursor: pymysql.cursors.Cursor, user_id: int, user_type: str) -> dict | None:
    user_type = _normalize_user_type(user_type)
    info = USER_TABLES[user_type]
    table = info["table"]
    id_col = info["id_col"]
    if user_type == "admin":
        cursor.execute(
            f"""
            SELECT id, {id_col} as username, name as full_name, phone, email, role, created_at, updated_at
            FROM {table} WHERE id = %s
            """,
            (user_id,),
        )
    else:
        cursor.execute(
            f"""
            SELECT id, {id_col} as username, name as full_name, phone, email, created_at, updated_at
            FROM {table} WHERE id = %s
            """,
            (user_id,),
        )
    row = cursor.fetchone()
    if not row:
        return None
    if isinstance(row, dict):
        data = {
            "id": row["id"],
            "username": row["username"],
            "phone": row.get("phone"),
            "email": row.get("email"),
            "full_name": row.get("full_name"),
            "role": row.get("role") if user_type == "admin" else info["role"],
            "created_at": row["created_at"] if isinstance(row["created_at"], str) else row["created_at"].strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": row["updated_at"] if isinstance(row["updated_at"], str) else row["updated_at"].strftime("%Y-%m-%d %H:%M:%S"),
        }
        return data
    # fallback for tuple cursor
    if user_type == "admin":
        return {
            "id": row[0],
            "username": row[1],
            "phone": row[3],
            "email": row[4],
            "full_name": row[2],
            "role": row[5],
            "created_at": row[6] if isinstance(row[6], str) else row[6].strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": row[7] if isinstance(row[7], str) else row[7].strftime("%Y-%m-%d %H:%M:%S"),
        }
    return {
        "id": row[0],
        "username": row[1],
        "phone": row[3],
        "email": row[4],
        "full_name": row[2],
        "role": info["role"],
        "created_at": row[5] if isinstance(row[5], str) else row[5].strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": row[6] if isinstance(row[6], str) else row[6].strftime("%Y-%m-%d %H:%M:%S"),
    }

class SchoolCreateRequest(BaseModel):
    """录入学校请求"""
    school_name: str = Field(..., min_length=1, description="学校名称，不能为空")
    province: Optional[str] = Field(None, description="所属省份")
    city: Optional[str] = Field(None, description="所属城市")
    
    model_config = {
        "json_schema_extra": {
            "example": {
                "school_name": "清华大学",
                "province": "北京市",
                "city": "北京市"
            }
        }
    }

class DepartmentCreateRequest(BaseModel):
    """录入院系请求"""
    school_id: int = Field(..., gt=0, description="学校ID（关联schools表的school_id），必须大于0")
    department_name: str = Field(..., min_length=1, description="院系名称，不能为空")
    
    model_config = {
        "json_schema_extra": {
            "example": {
                "school_id": 1,
                "department_name": "计算机科学与技术系"
            }
        }
    }

class SchoolIdQueryRequest(BaseModel):
    """学校ID查询请求"""
    school_name: str = Field(..., min_length=1, description="学校名称，不能为空")
    
    model_config = {
        "json_schema_extra": {
            "example": {
                "school_name": "清华大学"
            }
        }
    }

class DepartmentIdQueryRequest(BaseModel):
    """院系ID查询请求"""
    school_id: int = Field(..., gt=0, description="学校ID，必须大于0")
    
    model_config = {
        "json_schema_extra": {
            "example": {
                "school_id": 1
            }
        }
    }

router = APIRouter()

def _get_next_business_id(
    cursor: pymysql.cursors.DictCursor, 
    table: str, 
    id_field: str
) -> int:
    """
    获取下一个业务唯一ID（基于当前最大值+1）
    :param cursor: 数据库游标
    :param table: 表名
    :param id_field: 业务ID字段名（如school_id/department_id）
    :return: 下一个可用的业务ID
    """
    cursor.execute(f"SELECT MAX({id_field}) as max_id FROM {table}")
    result = cursor.fetchone()
    max_id = result.get("max_id") or 0
    return max_id + 1

@router.post(
    "/schools",
    summary="录入学校（管理员）",
    description="管理员录入学校信息，自动生成业务唯一ID，仅管理员可用",
)
def create_school(
    payload: SchoolCreateRequest,
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="管理员信息(JSON字符串，包含 sub/username/roles)"),
):
    # 解析当前用户信息
    current_user_info = _parse_current_user(current_user)
    # 验证当前用户是管理员
    user_roles = current_user_info.get("roles", [])
    if "admin" not in user_roles and "管理员" not in user_roles:
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        school_name = payload.school_name.strip()
        province = payload.province.strip() if payload.province else None
        city = payload.city.strip() if payload.city else None
        # 检查学校名称是否已存在
        cursor.execute(
            "SELECT school_id FROM schools WHERE school_name = %s",
            (school_name,)
        )
        if cursor.fetchone():
            raise HTTPException(status_code=400, detail=f"学校「{school_name}」已存在")
        # 生成唯一的school_id
        new_school_id = _get_next_business_id(cursor, "schools", "school_id")
        cursor.execute(
            """
            INSERT INTO schools (school_id, school_name, province, city)
            VALUES (%s, %s, %s, %s)
            """,
            (new_school_id, school_name, province, city)
        )
        db.commit()
        return {
            "code": 200,
            "message": "学校录入成功",
            "data": {
                "school_id": new_school_id,
                "school_name": school_name,
                "province": province,
                "city": city
            }
        }
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        db.rollback()
        logger.error(f"录入学校数据库错误: {str(e)}")
        raise HTTPException(status_code=500, detail=f"学校录入失败：{str(e)}")
    finally:
        if cursor:
            cursor.close()

@router.post(
    "/departments",
    summary="录入院系（管理员）",
    description="管理员录入院系信息，关联学校ID，自动生成业务唯一ID，仅管理员可用",
)
def create_department(
    payload: DepartmentCreateRequest,
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="管理员信息(JSON字符串，包含 sub/username/roles)"),
):
    # 解析当前用户信息
    current_user_info = _parse_current_user(current_user)
    # 验证当前用户是管理员
    user_roles = current_user_info.get("roles", [])
    if "admin" not in user_roles and "管理员" not in user_roles:
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        school_id = payload.school_id
        department_name = payload.department_name.strip()
        # 检查学校是否存在
        cursor.execute("SELECT school_id FROM schools WHERE school_id = %s", (school_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail=f"学校ID {school_id} 不存在")
        # 检查该学校下院系名称是否已存在
        cursor.execute(
            "SELECT department_id FROM departments WHERE school_id = %s AND department_name = %s",
            (school_id, department_name)
        )
        if cursor.fetchone():
            raise HTTPException(status_code=400, detail=f"学校ID {school_id} 下已存在院系「{department_name}」")
        # 生成唯一的department_id
        new_department_id = _get_next_business_id(cursor, "departments", "department_id")
        cursor.execute(
            """
            INSERT INTO departments (department_id, school_id, department_name)
            VALUES (%s, %s, %s)
            """,
            (new_department_id, school_id, department_name)
        )
        db.commit()
        return {
            "code": 200,
            "message": "院系录入成功",
            "data": {
                "department_id": new_department_id,
                "school_id": school_id,
                "department_name": department_name
            }
        }
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        db.rollback()
        logger.error(f"录入院系数据库错误: {str(e)}")
        # 更精准的异常提示
        if "Duplicate entry" in str(e):
            raise HTTPException(status_code=400, detail="院系ID生成冲突，请重试")
        raise HTTPException(status_code=500, detail=f"院系录入失败：{str(e)}")
    finally:
        if cursor:
            cursor.close()


@router.post(
    "/schools/query-id",
    summary="查询学校ID（公开）",
    description="输入学校名称查询对应的学校ID，任何人可访问",
)
def query_school_id(
    payload: SchoolIdQueryRequest,
    db: pymysql.connections.Connection = Depends(get_db),
):
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        school_name = payload.school_name.strip()
        cursor.execute(
            "SELECT id as school_id, school_name FROM schools WHERE school_name = %s",
            (school_name,)
        )
        result = cursor.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail=f"未查询到学校「{school_name}」的ID") 
        return {
            "code": 200,
            "message": "查询成功",
            "data": result
        }
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        logger.error(f"查询学校ID数据库错误: {str(e)}")
        raise HTTPException(status_code=500, detail="学校ID查询失败")
    finally:
        if cursor:
            cursor.close()

@router.post(
    "/departments/query-by-school",
    summary="查询院系ID（公开）",
    description="输入学校ID查询该学校下所有院系及对应ID，任何人可访问",
)
def query_departments_by_school(
    payload: DepartmentIdQueryRequest,
    db: pymysql.connections.Connection = Depends(get_db),
):
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        school_id = payload.school_id
        # 先检查学校是否存在
        cursor.execute("SELECT id FROM schools WHERE id = %s", (school_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail=f"学校ID {school_id} 不存在")
        # 查询该学校下所有院系
        cursor.execute(
            """
            SELECT id as department_id, department_name, school_id
            FROM departments WHERE school_id = %s
            ORDER BY department_id ASC
            """,
            (school_id,)
        )
        results = cursor.fetchall()
        if not results:
            return {
                "code": 200,
                "message": f"学校ID {school_id} 下暂无院系信息",
                "data": []
            }
        return {
            "code": 200,
            "message": "查询成功",
            "data": results
        }
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        logger.error(f"查询院系ID数据库错误: {str(e)}")
        raise HTTPException(status_code=500, detail="院系ID查询失败")
    finally:
        if cursor:
            cursor.close()

def _validate_school_exists(cursor: pymysql.cursors.Cursor, school_id: int) -> bool:
    """校验学校ID是否存在"""
    cursor.execute("SELECT 1 FROM schools WHERE school_id = %s LIMIT 1", (school_id,))
    return bool(cursor.fetchone())

def _validate_department_exists(cursor: pymysql.cursors.Cursor, department_id: int) -> bool:
    """校验院系ID是否存在"""
    cursor.execute("SELECT 1 FROM departments WHERE department_id = %s LIMIT 1", (department_id,))
    return bool(cursor.fetchone())

def _get_school_name_by_id(cursor: pymysql.cursors.Cursor, school_id: int) -> str | None:
    """根据学校ID获取学校名称"""
    cursor.execute("SELECT school_name FROM schools WHERE school_id = %s LIMIT 1", (school_id,))
    row = cursor.fetchone()
    return row["school_name"] if row else None

def _get_department_name_by_id(cursor: pymysql.cursors.Cursor, department_id: int) -> str | None:
    """根据院系ID获取院系名称"""
    cursor.execute("SELECT department_name FROM departments WHERE department_id = %s LIMIT 1", (department_id,))
    row = cursor.fetchone()
    return row["department_name"] if row else None


@router.post(
    "/user/bind-school",
    summary="用户绑定学校",
    description="校验sub、role与current_user的一致性后，绑定学校信息（仅学生/教师角色）",
)
def user_bind_school(
    payload: UserBindSchool,
    sub: int = Query(..., description="用户ID，必须传入且为有效整数"),
    role: str = Query(..., description="用户角色，仅支持student/teacher"),
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="提交者信息(JSON字符串，包含 sub/username/roles)"),
):
    # 基础参数校验
    if sub <= 0:
        raise HTTPException(status_code=400, detail="无效的用户ID（必须为正整数）")

    role = role.strip().lower()
    if role not in ["student", "teacher"]:
        raise HTTPException(status_code=400, detail="角色仅支持student/teacher")

    # 解析current_user并校验一致性
    current_user_info = _parse_current_user(current_user)
    current_sub = current_user_info.get("sub", 0)
    current_roles = current_user_info.get("roles", [])

    # 校验sub一致性
    if current_sub != sub:
        raise HTTPException(status_code=403, detail=f"current_user中的sub({current_sub})与传入的sub({sub})不匹配")

    # 校验role一致性
    role_set = {r.strip().lower() for r in current_roles}
    if role not in role_set:
        raise HTTPException(status_code=403, detail=f"current_user中的角色({current_roles})与传入的role({role})不匹配")
    # 业务逻辑处理
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        school_id = payload.school_id

        # 验证学校存在性
        if not _validate_school_exists(cursor, school_id):
            raise HTTPException(status_code=404, detail=f"学校ID {school_id} 不存在")

        # 获取学校名称
        school_name = _get_school_name_by_id(cursor, school_id) or payload.school_name

        # 更新用户学校信息
        table = USER_TABLES[role]["table"]
        update_sql = f"""
            UPDATE {table} 
            SET school_id = %s, school_name = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """
        cursor.execute(update_sql, (school_id, school_name, sub))

        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"{role}用户ID {sub} 不存在")

        db.commit()
        return {
            "code": 200,
            "message": "学校绑定成功",
            "data": {
                "user_id": sub,
                "user_type": role,
                "school_id": school_id,
                "school_name": school_name
            }
        }
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        db.rollback()
        logger.error(f"绑定学校数据库错误: {str(e)}")
        raise HTTPException(status_code=500, detail="学校绑定失败")
    finally:
        if cursor:
            cursor.close()


@router.post(
    "/user/bind-department",
    summary="用户绑定院系",
    description="校验sub、role与current_user的一致性后，绑定院系信息（需先绑定学校）",
)
def user_bind_department(
    payload: UserBindDepartment,
    sub: int = Query(..., description="用户ID，必须传入且为有效整数"),
    role: str = Query(..., description="用户角色，仅支持student/teacher"),
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="提交者信息(JSON字符串，包含 sub/username/roles)"),
):
    # 基础参数校验
    if sub <= 0:
        raise HTTPException(status_code=400, detail="无效的用户ID（必须为正整数）")

    role = role.strip().lower()
    if role not in ["student", "teacher"]:
        raise HTTPException(status_code=400, detail="角色仅支持student/teacher")

    # 解析current_user并校验一致性
    current_user_info = _parse_current_user(current_user)
    current_sub = current_user_info.get("sub", 0)
    current_roles = current_user_info.get("roles", [])

    # 校验sub一致性
    if current_sub != sub:
        raise HTTPException(status_code=403, detail=f"current_user中的sub({current_sub})与传入的sub({sub})不匹配")

    # 校验role一致性
    role_set = {r.strip().lower() for r in current_roles}
    if role not in role_set:
        raise HTTPException(status_code=403, detail=f"current_user中的角色({current_roles})与传入的role({role})不匹配")
    # 业务逻辑处理
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        department_id = payload.department_id

        # 验证院系存在性
        if not _validate_department_exists(cursor, department_id):
            raise HTTPException(status_code=404, detail=f"院系ID {department_id} 不存在")

        # 获取院系名称（自动填充）
        department_name = _get_department_name_by_id(cursor, department_id) or payload.department_name

        # 检查用户是否已绑定学校
        table = USER_TABLES[role]["table"]
        cursor.execute(f"SELECT school_id FROM {table} WHERE id = %s", (sub,))
        user_school = cursor.fetchone()
        if not user_school or not user_school["school_id"]:
            raise HTTPException(status_code=400, detail="请先绑定学校信息，再绑定院系")
        # 更新用户院系信息
        update_sql = f"""
            UPDATE {table} 
            SET department_id = %s, department_name = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """
        cursor.execute(update_sql, (department_id, department_name, sub))

        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"{role}用户ID {sub} 不存在")

        db.commit()
        return {
            "code": 200,
            "message": "院系绑定成功",
            "data": {
                "user_id": sub,
                "user_type": role,
                "department_id": department_id,
                "department_name": department_name
            }
        }
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        db.rollback()
        logger.error(f"绑定院系数据库错误: {str(e)}")
        raise HTTPException(status_code=500, detail="院系绑定失败")
    finally:
        if cursor:
            cursor.close()


@router.get(
    "/me",
    summary="获取当前登录用户信息",
    description="根据当前登录用户信息返回用户表中的全部字段（不包含密码）",
)
def get_current_user_info(
    current_user: dict = Depends(get_current_user),
    db: pymysql.connections.Connection = Depends(get_db),
):
    cursor = None
    try:
        user_id = current_user.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="请先登录")
        user_type = _resolve_user_type_from_payload(current_user)
        info = USER_TABLES[user_type]
        table = info["table"]
        cursor = db.cursor(pymysql.cursors.DictCursor)
        cursor.execute(f"SELECT * FROM {table} WHERE id = %s", (user_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        row.pop("password", None)
        return {"user_type": user_type, **row}
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        logger.error(f"获取用户信息数据库错误: {str(e)}")
        raise HTTPException(status_code=500, detail="获取用户信息失败")
    finally:
        if cursor:
            cursor.close()


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="用户登录",
    description="统一账号密码登录，返回 JWT access token 和用户信息",
)
def login_user(payload: LoginRequest, db: pymysql.connections.Connection = Depends(get_db)):

    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        username = payload.username.strip()
        if not username:
            raise HTTPException(status_code=400, detail="username 不能为空")
        if not payload.password:
            raise HTTPException(status_code=400, detail="password 不能为空")

        # 账号映射逻辑：先查映射表
        mapping = None
        try:
            cursor.execute(
                "SELECT real_user_id, real_user_type FROM account_mapping WHERE virtual_account = %s",
                (username,)
            )
            mapping = cursor.fetchone()
        except pymysql.MySQLError as e:
            if getattr(e, "args", [None])[0] == 1146:
                logger.warning("account_mapping table missing, skip virtual account mapping")
            else:
                raise
        if mapping:
            real_user_id = mapping["real_user_id"]
            real_user_type = mapping["real_user_type"]
            # 查找真实账号信息
            info = USER_TABLES[real_user_type]
            table = info["table"]
            id_col = info["id_col"]
            cursor.execute(
                f"SELECT id, {id_col} as username, name as full_name, phone, email, role, password, DATE_FORMAT(created_at, '%Y-%m-%d %H:%i:%s') as created_at, DATE_FORMAT(updated_at, '%Y-%m-%d %H:%i:%s') as updated_at FROM {table} WHERE id = %s",
                (real_user_id,)
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="真实账号不存在")
            password_hash = row.get("password")
            if not password_hash or not verify_password(payload.password, password_hash):
                raise HTTPException(status_code=401, detail="用户名或密码错误")
            role = row.get("role") or real_user_type
            token_payload = {
                "sub": row["id"],
                "username": row["username"],
                "roles": [role],
                "user_type": real_user_type,
            }
            access_token = create_access_token(token_payload)
            row.pop("password", None)
            user_out = UserOut(**row)
            return LoginResponse(access_token=access_token, user=user_out)

        # 没有映射则走原有逻辑
        candidates: list[tuple[str, dict]] = []
        if payload.user_type:
            row = _fetch_user_for_login(cursor, username, payload.user_type)
            if row:
                candidates.append((payload.user_type, row))
        else:
            for user_type in ("admin", "teacher", "student"):
                row = _fetch_user_for_login(cursor, username, user_type)
                if row:
                    candidates.append((user_type, row))
        if not candidates:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        matched: list[tuple[str, dict]] = []
        for user_type, row in candidates:
            password_hash = row.get("password")
            if password_hash and verify_password(payload.password, password_hash):
                matched.append((user_type, row))
        if not matched:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        if len(matched) > 1:
            raise HTTPException(status_code=400, detail="账号在多个用户类型中匹配，请指定 user_type")
        user_type, row = matched[0]
        role = row.get("role") or user_type
        token_payload = {
            "sub": row["id"],
            "username": row["username"],
            "roles": [role],
            "user_type": user_type,
        }
        access_token = create_access_token(token_payload)
        row.pop("password", None)
        user_out = UserOut(**row)
        return LoginResponse(access_token=access_token, user=user_out)
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        logger.error(f"用户登录数据库错误: {str(e)}")
        raise HTTPException(status_code=500, detail="登录失败")
    finally:
        if cursor:
            cursor.close()


class ChangePasswordRequest(BaseModel):
    """修改密码请求"""
    old_password: str
    new_password: str
    
    model_config = {
        "json_schema_extra": {
            "example": {
                "old_password": "123456",
                "new_password": "654321Abc!"
            }
        }
    }

@router.put(
    "/change-password",
    summary="修改密码",
    description="验证原始密码后修改用户密码，学生/教师/管理员均可操作"
)
def change_password(
    payload: ChangePasswordRequest,
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
):
    current_user = _parse_current_user(current_user)
    user_id = current_user.get("sub")
    if not user_id or user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录")
    cursor = None
    try:
        # 解析用户类型
        user_type = _resolve_user_type_from_payload(current_user)
        info = USER_TABLES[user_type]
        table = info["table"]
        cursor = db.cursor(pymysql.cursors.DictCursor)
        # 查询用户信息并验证原始密码
        cursor.execute(
            f"SELECT id, password FROM {table} WHERE id = %s",
            (user_id,)
        )
        user_row = cursor.fetchone()
        if not user_row:
            raise HTTPException(status_code=404, detail="用户不存在")
        # 验证原始密码
        if not verify_password(payload.old_password, user_row["password"]):
            raise HTTPException(status_code=400, detail="原始密码错误")
        # 验证新密码
        if len(payload.new_password) < 6:
            raise HTTPException(status_code=400, detail="新密码长度不能少于6位")
        # 加密新密码并更新
        new_password_hash = get_password_hash(payload.new_password)
        cursor.execute(
            f"UPDATE {table} SET password = %s, updated_at = NOW() WHERE id = %s",
            (new_password_hash, user_id)
        )
        db.commit()
        return {"message": "密码修改成功"}
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        db.rollback()
        logger.error(f"修改密码数据库错误: {str(e)}")
        raise HTTPException(status_code=500, detail="密码修改失败")
    finally:
        if cursor:
            cursor.close()


class ResetPasswordRequest(BaseModel):
    """重置密码请求"""
    user_id: int
    user_type: str  # student/teacher/admin
    
    model_config = {
        "json_schema_extra": {
            "example": {
                "user_id": 1,
                "user_type": "student"
            }
        }
    }


@router.post(
    "/reset-password",
    summary="重置用户密码",
    description="管理员重置指定用户类型和ID的用户密码为123456"
)
def reset_user_password(
    payload: ResetPasswordRequest,
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="管理员信息(JSON字符串，包含 sub/username/roles)"),
):
    # 解析当前用户信息
    current_user_info = _parse_current_user(current_user)
    # 验证当前用户是管理员
    user_roles = current_user_info.get("roles", [])
    if "admin" not in user_roles and "管理员" not in user_roles:
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    cursor = None
    try:
        # 标准化用户类型
        user_type = _normalize_user_type(payload.user_type)
        info = USER_TABLES[user_type]
        table = info["table"]
        cursor = db.cursor(pymysql.cursors.DictCursor)
        # 检查用户是否存在
        cursor.execute(f"SELECT id FROM {table} WHERE id = %s", (payload.user_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail=f"{user_type}用户不存在")
        # 重置密码为123456
        default_password = "123456"
        password_hash = get_password_hash(default_password)
        cursor.execute(
            f"UPDATE {table} SET password = %s, updated_at = NOW() WHERE id = %s",
            (password_hash, payload.user_id)
        )
        db.commit()
        return {
            "message": f"{user_type}用户密码已重置为123456",
            "user_id": payload.user_id,
            "user_type": user_type
        }
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        db.rollback()
        logger.error(f"重置用户密码数据库错误: {str(e)}")
        raise HTTPException(status_code=500, detail="重置密码失败")
    finally:
        if cursor:
            cursor.close()


class UserInfoRequest(BaseModel):
    """获取用户完整信息请求"""
    sub: int = Field(..., gt=0, description="用户ID（自增主键），必须大于0")
    username: str = Field(..., description="用户名/学号/工号")
    roles: str | List[str] = Field(..., description="用户角色，如admin/teacher/student")
    
    model_config = {
        "json_schema_extra": {
            "example": {
                "sub": 1,
                "username": "2400305304",
                "roles": "student"
            }
        }
    }

@router.post(
    "/user/full-info",
    summary="获取用户完整信息",
    description="输入用户登录信息，返回对应角色表中的全部信息（排除密码）",
)
def get_user_full_info(
    payload: UserInfoRequest = Body(...),
    db: pymysql.connections.Connection = Depends(get_db),
):
    cursor = None
    try:
        # 解析用户类型
        user_payload = {
            "sub": payload.sub,
            "username": payload.username,
            "roles": payload.roles
        }
        user_type = _resolve_user_type_from_payload(user_payload)
        cursor = db.cursor(pymysql.cursors.DictCursor)
        # 根据用户类型构建查询SQL
        if user_type == "student":
            cursor.execute("""
                SELECT 
                    id, 
                    student_id, 
                    name, 
                    phone, 
                    email, 
                    school_id, 
                    school_name, 
                    department_id, 
                    department_name, 
                    group_id,
                    DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:%%i:%%s') as created_at,
                    DATE_FORMAT(updated_at, '%%Y-%%m-%%d %%H:%%i:%%s') as updated_at
                FROM students 
                WHERE id = %s
            """, (payload.sub,))
        elif user_type == "teacher":
            cursor.execute("""
                SELECT 
                    id, 
                    teacher_id, 
                    name, 
                    phone, 
                    email, 
                    school_id, 
                    school_name, 
                    department_id, 
                    department_name, 
                    group_id,
                    DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:%%i:%%s') as created_at,
                    DATE_FORMAT(updated_at, '%%Y-%%m-%%d %%H:%%i:%%s') as updated_at
                FROM teachers 
                WHERE id = %s
            """, (payload.sub,))
        elif user_type == "admin":
            cursor.execute("""
                SELECT 
                    id, 
                    admin_id, 
                    name, 
                    phone, 
                    email, 
                    role,
                    school_id, 
                    school_name, 
                    department_id, 
                    department_name,
                    DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:%%i:%%s') as created_at,
                    DATE_FORMAT(updated_at, '%%Y-%%m-%%d %%H:%%i:%%s') as updated_at
                FROM admins 
                WHERE id = %s
            """, (payload.sub,))
        else:
            raise HTTPException(status_code=400, detail=f"不支持的用户类型: {user_type}")
        user_info = cursor.fetchone()
        if not user_info:
            raise HTTPException(status_code=404, detail=f"{user_type}用户（ID: {payload.sub}）不存在")
        # 补充用户类型信息
        user_info["user_type"] = user_type
        return {
            "code": 200,
            "message": "获取用户完整信息成功",
            "data": user_info
        }
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        logger.error(f"获取用户完整信息数据库错误（用户ID: {payload.sub}）: {str(e)}")
        raise HTTPException(status_code=500, detail="获取用户信息失败：数据库操作异常")
    except Exception as e:
        logger.error(f"获取用户完整信息未知错误（用户ID: {payload.sub}）: {str(e)}")
        raise HTTPException(status_code=500, detail="获取用户信息失败：系统异常")
    finally:
        if cursor:
            cursor.close()


@router.post(
    "/students",
    response_model=UserOut,
    summary="创建学生",
    description="创建学生并返回用户信息"
)
def create_student(payload: StudentCreate, db: pymysql.connections.Connection = Depends(get_db)):
    cursor = None
    try:
        # 初始化字典游标
        cursor = db.cursor(pymysql.cursors.DictCursor)
        # 校验用户名非空
        username = payload.username.strip()
        if not username:
            raise HTTPException(status_code=400, detail="username 不能为空")
        # 处理默认值
        full_name = payload.full_name or username
        raw_password = payload.password or "123456"
        password_hash = get_password_hash(raw_password)
        # 插入学生信息到数据库
        cursor.execute(
            """
            INSERT INTO students (student_id, name, phone, email, password)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (username, full_name, payload.phone, payload.email, password_hash),
        )
        db.commit()
        # 查询刚创建的学生信息
        user_id = cursor.lastrowid
        cursor.execute(
            """
            SELECT id, student_id as username, name as full_name, phone, email,
                   DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:%%i:%%s') as created_at,
                   DATE_FORMAT(updated_at, '%%Y-%%m-%%d %%H:%%i:%%s') as updated_at
            FROM students WHERE id = %s
            """,
            (user_id,),
        )
        row = cursor.fetchone()
        # 校验查询结果
        if not row:
            raise HTTPException(status_code=500, detail="用户创建成功但查询失败")
        # 添加角色标识并返回
        row["role"] = "student" if isinstance(row, dict) else "student"
        return UserOut(**row)
    # 用户名重复异常处理
    except pymysql.err.IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="用户名已存在")
    # 数据库通用异常处理
    except pymysql.MySQLError as e:
        db.rollback()
        logger.error(f"用户创建数据库错误: {str(e)}")
        raise HTTPException(status_code=500, detail="用户创建失败")
    # 释放游标资源
    finally:
        if cursor:
            cursor.close()


@router.post(
    "/teachers",
    response_model=UserOut,
    summary="创建教师",
    description="创建教师并返回用户信息"
)
def create_teacher(payload: TeacherCreate, db: pymysql.connections.Connection = Depends(get_db)):
    cursor = None
    try:
        # 初始化字典游标
        cursor = db.cursor(pymysql.cursors.DictCursor)
        # 校验教师工号非空
        username = payload.username.strip()
        if not username:
            raise HTTPException(status_code=400, detail="username 不能为空")
        # 处理默认值
        full_name = payload.full_name or username
        raw_password = payload.password or "123456"
        password_hash = get_password_hash(raw_password)
        # 插入教师信息到数据库
        cursor.execute(
            """
            INSERT INTO teachers (teacher_id, name, phone, email, password)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (username, full_name, payload.phone, payload.email, password_hash),
        )
        db.commit()
        # 查询刚创建的教师信息
        user_id = cursor.lastrowid
        cursor.execute(
            """
            SELECT id, teacher_id as username, name as full_name, phone, email,
                   DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:%%i:%%s') as created_at,
                   DATE_FORMAT(updated_at, '%%Y-%%m-%%d %%H:%%i:%%s') as updated_at
            FROM teachers WHERE id = %s
            """,
            (user_id,),
        )
        row = cursor.fetchone()
        # 校验查询结果
        if not row:
            raise HTTPException(status_code=500, detail="用户创建成功但查询失败")
        # 添加角色标识并返回
        row["role"] = "teacher" if isinstance(row, dict) else "teacher"
        return UserOut(**row)
    # 教师工号重复异常处理
    except pymysql.err.IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="用户名已存在")
    # 数据库通用异常处理
    except pymysql.MySQLError as e:
        db.rollback()
        logger.error(f"用户创建数据库错误: {str(e)}")
        raise HTTPException(status_code=500, detail="用户创建失败")
    # 释放游标资源
    finally:
        if cursor:
            cursor.close()


@router.post(
    "/admins",
    response_model=UserOut,
    summary="创建管理员",
    description="创建管理员并返回用户信息"
)
def create_admin(payload: AdminCreate, db: pymysql.connections.Connection = Depends(get_db)):
    cursor = None
    try:
        # 初始化字典游标
        cursor = db.cursor(pymysql.cursors.DictCursor)
        # 校验管理员账号非空
        username = payload.username.strip()
        if not username:
            raise HTTPException(status_code=400, detail="username 不能为空")
        # 处理默认值
        full_name = payload.full_name or username
        raw_password = payload.password or "123456"
        password_hash = get_password_hash(raw_password)
        # 插入管理员信息到数据库
        cursor.execute(
            """
            INSERT INTO admins (admin_id, name, phone, email, role, password)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                username,
                full_name,
                payload.phone,
                payload.email,
                payload.role or "admin",
                password_hash,
            ),
        )
        db.commit()
        # 查询刚创建的管理员信息
        user_id = cursor.lastrowid
        cursor.execute(
            """
            SELECT id, admin_id as username, name as full_name, phone, email, role,
                   DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:%%i:%%s') as created_at,
                   DATE_FORMAT(updated_at, '%%Y-%%m-%%d %%H:%%i:%%s') as updated_at
            FROM admins WHERE id = %s
            """,
            (user_id,),
        )
        row = cursor.fetchone()
        # 校验查询结果
        if not row:
            raise HTTPException(status_code=500, detail="用户创建成功但查询失败")
        # 返回管理员信息
        return UserOut(**row)
    # 管理员账号重复异常处理
    except pymysql.err.IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="用户名已存在")
    # 数据库通用异常处理
    except pymysql.MySQLError as e:
        db.rollback()
        logger.error(f"用户创建数据库错误: {str(e)}")
        raise HTTPException(status_code=500, detail="用户创建失败")
    # 释放游标资源
    finally:
        if cursor:
            cursor.close()


@router.put(
    "/{user_id}",
    response_model=UserOut,
    summary="更新用户信息",
    description="按需更新邮箱、姓名、角色或密码"
)
def update_user(user_id: int, payload: UserUpdate, db: pymysql.connections.Connection = Depends(get_db)):
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        user_type = _normalize_user_type(payload.user_type)
        info = USER_TABLES[user_type]
        table = info["table"]
        cursor.execute(f"SELECT id FROM {table} WHERE id = %s", (user_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="用户不存在")

        fields = []
        params: List[str] = []
        if payload.phone is not None:
            fields.append("phone = %s")
            params.append(payload.phone)
        if payload.email is not None:
            fields.append("email = %s")
            params.append(payload.email)
        if payload.full_name is not None:
            fields.append("name = %s")
            params.append(payload.full_name)
        if payload.role is not None and user_type == "admin":
            fields.append("role = %s")
            params.append(payload.role)
        if payload.password is not None:
            fields.append("password = %s")
            params.append(get_password_hash(payload.password))

        if not fields:
            existing = _fetch_user(cursor, user_id, user_type)
            if not existing:
                raise HTTPException(status_code=404, detail="用户不存在")
            return UserOut(**existing)

        fields.append("updated_at = NOW()")
        sql = f"UPDATE {table} SET {', '.join(fields)} WHERE id = %s"
        params.append(user_id)
        cursor.execute(sql, tuple(params))
        db.commit()
        updated = _fetch_user(cursor, user_id, user_type)
        if not updated:
            raise HTTPException(status_code=500, detail="用户更新后查询失败")
        return UserOut(**updated)
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        db.rollback()
        logger.error(f"用户更新数据库错误: {str(e)}")
        raise HTTPException(status_code=500, detail="用户更新失败")
    finally:
        if cursor:
            cursor.close()


@router.delete(
    "/{user_id}",
    summary="删除用户",
    description="根据用户ID删除用户"
)
def delete_user(
    user_id: int,
    db: pymysql.connections.Connection = Depends(get_db),
    user_type: str = Query("admin"),
):
    cursor = None
    try:
        cursor = db.cursor()
        user_type = _normalize_user_type(user_type)
        info = USER_TABLES[user_type]
        table = info["table"]
        cursor.execute(f"SELECT 1 FROM {table} WHERE id = %s", (user_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="用户不存在")
        cursor.execute(f"DELETE FROM {table} WHERE id = %s", (user_id,))
        db.commit()
        return {"message": "删除成功", "user_id": user_id}
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        db.rollback()
        logger.error(f"用户删除数据库错误: {str(e)}")
        raise HTTPException(status_code=500, detail="用户删除失败")
    finally:
        if cursor:
            cursor.close()


@router.post(
    "/import",
    summary="一键导入用户",
    description="上传 CSV/TSV 文件批量导入用户（列：username,user_type,email,full_name,role,password 可选）"
)
async def import_users(file: UploadFile = File(...), db: pymysql.connections.Connection = Depends(get_db)):
    filename = file.filename or ""
    lower_name = filename.lower()
    if not lower_name.endswith(SUPPORTED_IMPORT_EXTS):
        raise HTTPException(status_code=400, detail="仅支持 .csv 或 .tsv 文件")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")

    delimiter = "\t" if lower_name.endswith(".tsv") else ","
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("gbk")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="文件编码仅支持 UTF-8 或 GBK")

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    required_col = "username"
    if required_col not in reader.fieldnames:
        raise HTTPException(status_code=400, detail="文件缺少 username 列")

    created, updated = 0, 0
    default_role = "admin"
    default_password = "123456"
    cursor = None
    created_items = []
    updated_items = []
    try:
        cursor = db.cursor()
        for row in reader:
            username = (row.get("username") or "").strip()
            if not username:
                continue
            user_type = _normalize_user_type(row.get("user_type") or "admin")
            info = USER_TABLES[user_type]
            table = info["table"]
            phone = (row.get("phone") or None) and row.get("phone").strip()
            email = (row.get("email") or None) and row.get("email").strip()
            full_name = (row.get("full_name") or None) and row.get("full_name").strip()
            role = (row.get("role") or default_role).strip() or default_role
            password = (row.get("password") or default_password).strip() or default_password
            password_hash = get_password_hash(password)
            if not full_name:
                full_name = username  # 默认使用username作为full_name
            if user_type == "admin":
                cursor.execute(
                    """
                    INSERT INTO admins (admin_id, name, phone, email, role, password)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        name = VALUES(name),
                        phone = VALUES(phone),
                        email = VALUES(email),
                        role = VALUES(role),
                        password = VALUES(password),
                        updated_at = NOW()
                    """,
                    (username, full_name, phone, email, role, password_hash),
                )
            elif user_type == "student":
                cursor.execute(
                    """
                    INSERT INTO students (student_id, name, phone, email, password)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        name = VALUES(name),
                        phone = VALUES(phone),
                        email = VALUES(email),
                        password = VALUES(password),
                        updated_at = NOW()
                    """,
                    (username, full_name, phone, email, password_hash),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO teachers (teacher_id, name, phone, email, password)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        name = VALUES(name),
                        phone = VALUES(phone),
                        email = VALUES(email),
                        password = VALUES(password),
                        updated_at = NOW()
                    """,
                    (username, full_name, phone, email, password_hash),
                )
            if cursor.rowcount == 1:
                created += 1
                # fetch id
                cursor.execute(f"SELECT id FROM {table} WHERE {info['id_col']} = %s", (username,))
                rid = cursor.fetchone()
                if rid:
                    if isinstance(rid, dict):
                        rec_id = rid.get('id')
                    else:
                        rec_id = rid[0]
                else:
                    rec_id = None
                created_items.append({"user_type": user_type, "username": username, "id": rec_id})
            else:
                updated += 1
                cursor.execute(f"SELECT id FROM {table} WHERE {info['id_col']} = %s", (username,))
                rid = cursor.fetchone()
                if rid:
                    if isinstance(rid, dict):
                        rec_id = rid.get('id')
                    else:
                        rec_id = rid[0]
                else:
                    rec_id = None
                updated_items.append({"user_type": user_type, "username": username, "id": rec_id})
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
        logger.error(f"用户导入数据库错误: {str(e)}")
        raise HTTPException(status_code=500, detail="用户导入失败")
    finally:
        if cursor:
            cursor.close()


@router.put(
    "/{user_id}/bind-phone",
    response_model=UserOut,
    summary="绑定手机号",
    description="为指定用户绑定/更新手机号"
)
def bind_phone(
    user_id: int,
    payload: UserBindPhone,
    db: pymysql.connections.Connection = Depends(get_db),
    user_type: str = Query("admin"),
):
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        user_type = _normalize_user_type(user_type)
        table = USER_TABLES[user_type]["table"]
        cursor.execute(f"SELECT id FROM {table} WHERE id = %s", (user_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="用户不存在")

        cursor.execute(
            f"UPDATE {table} SET phone = %s, updated_at = NOW() WHERE id = %s",
            (payload.phone.strip(), user_id),
        )
        db.commit()
        updated = _fetch_user(cursor, user_id, user_type)
        if not updated:
            raise HTTPException(status_code=500, detail="手机号绑定后查询失败")
        return UserOut(**updated)
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        db.rollback()
        logger.error(f"绑定手机号数据库错误: {str(e)}")
        raise HTTPException(status_code=500, detail="绑定手机号失败")
    finally:
        if cursor:
            cursor.close()


@router.put(
    "/{user_id}/bind-email",
    response_model=UserOut,
    summary="绑定邮箱",
    description="为指定用户绑定/更新邮箱"
)
def bind_email(
    user_id: int,
    payload: UserBindEmail,
    db: pymysql.connections.Connection = Depends(get_db),
    user_type: str = Query("admin"),
):
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        user_type = _normalize_user_type(user_type)
        table = USER_TABLES[user_type]["table"]
        cursor.execute(f"SELECT id FROM {table} WHERE id = %s", (user_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="用户不存在")

        cursor.execute(
            f"UPDATE {table} SET email = %s, updated_at = NOW() WHERE id = %s",
            (payload.email, user_id),
        )
        db.commit()
        updated = _fetch_user(cursor, user_id, user_type)
        if not updated:
            raise HTTPException(status_code=500, detail="邮箱绑定后查询失败")
        return UserOut(**updated)
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        db.rollback()
        logger.error(f"绑定邮箱数据库错误: {str(e)}")
        raise HTTPException(status_code=500, detail="绑定邮箱失败")
    finally:
        if cursor:
            cursor.close()





def _validate_school_exists(cursor: pymysql.cursors.Cursor, school_id: int) -> bool:
    """校验学校ID是否存在"""
    cursor.execute("SELECT 1 FROM schools WHERE school_id = %s LIMIT 1", (school_id,))
    return bool(cursor.fetchone())

def _validate_department_exists(cursor: pymysql.cursors.Cursor, department_id: int) -> bool:
    """校验院系ID是否存在"""
    cursor.execute("SELECT 1 FROM departments WHERE department_id = %s LIMIT 1", (department_id,))
    return bool(cursor.fetchone())

def _get_school_name_by_id(cursor: pymysql.cursors.Cursor, school_id: int) -> str | None:
    """根据学校ID获取学校名称"""
    cursor.execute("SELECT school_name FROM schools WHERE school_id = %s LIMIT 1", (school_id,))
    row = cursor.fetchone()
    return row["school_name"] if row else None

def _get_department_name_by_id(cursor: pymysql.cursors.Cursor, department_id: int) -> str | None:
    """根据院系ID获取院系名称"""
    cursor.execute("SELECT department_name FROM departments WHERE department_id = %s LIMIT 1", (department_id,))
    row = cursor.fetchone()
    return row["department_name"] if row else None

# ========== 绑定学校接口 ==========
@router.put(
    "/{user_id}/bind-school",
    response_model=UserOut,
    summary="绑定用户学校",
    description="为指定用户绑定/更新所属学校信息（仅管理员可操作）"
)
def bind_school(
    user_id: int,
    payload: UserBindSchool,
    db: pymysql.connections.Connection = Depends(get_db),
    user_type: str = Query("admin", description="用户类型：student/teacher/admin"),
    current_user: Optional[str] = Query(None, description="管理员信息(JSON字符串，包含 sub/username/roles)"),
):
    # 1. 校验管理员权限
    current_user_info = _parse_current_user(current_user)
    user_roles = current_user_info.get("roles", [])
    if "admin" not in user_roles and "管理员" not in user_roles:
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        # 2. 标准化用户类型 & 校验用户是否存在
        user_type = _normalize_user_type(user_type)
        table = USER_TABLES[user_type]["table"]
        cursor.execute(f"SELECT id FROM {table} WHERE id = %s LIMIT 1", (user_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail=f"{user_type}用户不存在")
        
        # 3. 校验学校ID是否存在
        if not _validate_school_exists(cursor, payload.school_id):
            raise HTTPException(status_code=404, detail="学校不存在")
        
        # 4. 强制从数据库获取学校名称（不再使用传入的名称）
        school_name = _get_school_name_by_id(cursor, payload.school_id)
        if not school_name:
            raise HTTPException(status_code=500, detail="无法获取学校名称")
        
        # 5. 构造更新语句
        update_fields = [
            "school_id = %s",
            "school_name = %s",
            "updated_at = NOW()"
        ]
        update_params = [payload.school_id, school_name, user_id]
        
        # 6. 执行更新
        cursor.execute(
            f"UPDATE {table} SET {', '.join(update_fields)} WHERE id = %s",
            tuple(update_params)
        )
        db.commit()
        
        # 7. 查询更新后用户信息并返回
        updated_user = _fetch_user(cursor, user_id, user_type)
        if not updated_user:
            raise HTTPException(status_code=500, detail="绑定学校后查询用户信息失败")
        return UserOut(**updated_user)
    
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        db.rollback()
        logger.error(f"绑定学校数据库错误: {str(e)}")
        raise HTTPException(status_code=500, detail="绑定学校失败")
    finally:
        if cursor:
            cursor.close()

# ========== 绑定院系接口 ==========
@router.put(
    "/{user_id}/bind-department",
    response_model=UserOut,
    summary="绑定用户院系",
    description="为指定用户绑定/更新所属院系信息（仅管理员可操作）"
)
def bind_department(
    user_id: int,
    payload: UserBindDepartment,
    db: pymysql.connections.Connection = Depends(get_db),
    user_type: str = Query("admin", description="用户类型：student/teacher/admin"),
    current_user: Optional[str] = Query(None, description="管理员信息(JSON字符串，包含 sub/username/roles)"),
):
    # 1. 校验管理员权限
    current_user_info = _parse_current_user(current_user)
    user_roles = current_user_info.get("roles", [])
    if "admin" not in user_roles and "管理员" not in user_roles:
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        # 2. 标准化用户类型 & 校验用户是否存在
        user_type = _normalize_user_type(user_type)
        table = USER_TABLES[user_type]["table"]
        cursor.execute(f"SELECT id FROM {table} WHERE id = %s LIMIT 1", (user_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail=f"{user_type}用户不存在")
        
        # 3. 校验院系ID是否存在
        if not _validate_department_exists(cursor, payload.department_id):
            raise HTTPException(status_code=404, detail="院系不存在")
        
        # 4. 强制从数据库获取院系名称（不再使用传入的名称）
        dept_name = _get_department_name_by_id(cursor, payload.department_id)
        if not dept_name:
            raise HTTPException(status_code=500, detail="无法获取院系名称")
        
        # 5. 构造更新语句（兼容教师表原有department字段）
        update_fields = [
            "department_id = %s",
            "department_name = %s",
            "updated_at = NOW()"
        ]
        update_params = [payload.department_id, dept_name]
        
        # 兼容教师表的department字段（同步更新）
        if user_type == "teacher":
            update_fields.append("department = %s")
            update_params.append(dept_name)
        
        update_params.append(user_id)
        
        # 6. 执行更新
        cursor.execute(
            f"UPDATE {table} SET {', '.join(update_fields)} WHERE id = %s",
            tuple(update_params)
        )
        db.commit()
        
        # 7. 查询更新后用户信息并返回
        updated_user = _fetch_user(cursor, user_id, user_type)
        if not updated_user:
            raise HTTPException(status_code=500, detail="绑定院系后查询用户信息失败")
        return UserOut(**updated_user)
    
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        db.rollback()
        logger.error(f"绑定院系数据库错误: {str(e)}")
        raise HTTPException(status_code=500, detail="绑定院系失败")
    finally:
        if cursor:
            cursor.close()


USER_TABLES_MAP: Dict[str, Dict[str, str]] = {
    "student": {
        "table": "students",
        "username_col": "student_id",
        "sub_col": "id"
    },
    "teacher": {
        "table": "teachers",
        "username_col": "teacher_id",
        "sub_col": "id"
    },
    "admin": {
        "table": "admins",
        "username_col": "admin_id",
        "sub_col": "id"
    }
}

class UsernameToSubRequest(BaseModel):
    """通过username查询sub的请求模型"""
    username: str = Field(..., min_length=1, description="用户名（学号/教师工号/管理员账号）")
    user_type: str = Field(..., pattern="^(student|teacher|admin)$", 
                           description="用户类型，只能是 student/teacher/admin")
    
    model_config = {
        "json_schema_extra": {
            "example": {
                "username": "2400305304",
                "user_type": "student"
            }
        }
    }


def get_sub_by_username(
    db: pymysql.connections.Connection,
    username: str,
    user_type: str
) -> Optional[int]:
    # 验证用户类型
    if user_type not in USER_TABLES_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"用户类型错误，仅支持 {list(USER_TABLES_MAP.keys())}"
        )
    
    # 获取表信息
    table_info = USER_TABLES_MAP[user_type]
    table_name = table_info["table"]
    username_col = table_info["username_col"]
    sub_col = table_info["sub_col"]
    
    cursor = None
    try:
        # 使用字典游标
        cursor = db.cursor(pymysql.cursors.DictCursor)
        
        # 执行查询
        query_sql = f"""
            SELECT {sub_col} FROM {table_name} 
            WHERE {username_col} = %s LIMIT 1
        """
        cursor.execute(query_sql, (username.strip(),))
        result = cursor.fetchone()
        
        if result:
            return int(result[sub_col])  # 返回sub（id）
        return None
    
    except pymysql.MySQLError as e:
        logger.error(f"查询sub失败：表={table_name}, username={username}, 错误={str(e)}")
        raise HTTPException(
            status_code=500,
            detail="数据库查询失败，请稍后重试"
        )
    finally:
        if cursor:
            cursor.close()


@router.post("/get-sub-by-username", summary="通过username查询sub",
             description="根据用户名（学号/工号）和用户类型查询对应的自增主键ID（sub）")
def api_get_sub_by_username(
    request: UsernameToSubRequest,
    db: pymysql.connections.Connection = Depends(get_db)
) -> Dict[str, Any]:
    """
    API接口：通过username查询sub
    
    Returns:
        {
            "code": 200,
            "message": "查询成功",
            "data": {
                "username": "2400305304",
                "user_type": "student",
                "sub": 123
            }
        }
    """
    # 调用核心查询函数
    sub = get_sub_by_username(db, request.username, request.user_type)
    
    if sub is None:
        return {
            "code": 404,
            "message": f"{request.user_type}类型用户 {request.username} 不存在",
            "data": None
        }
    
    return {
        "code": 200,
        "message": "查询成功",
        "data": {
            "username": request.username,
            "user_type": request.user_type,
            "sub": sub
        }
    }


@router.get("/get-sub-auto", summary="自动匹配用户类型查询sub",
            description="无需指定用户类型，自动查询学生/教师/管理员表获取sub")
def api_get_sub_auto(
    username: str = Query(..., min_length=1, description="用户名（学号/教师工号/管理员账号）"),
    db: pymysql.connections.Connection = Depends(get_db)
) -> Dict[str, Any]:
    """
    扩展接口：自动匹配用户类型查询sub（无需指定user_type）
    """
    # 依次查询学生、教师、管理员表
    for user_type in ["student", "teacher", "admin"]:
        sub = get_sub_by_username(db, username, user_type)
        if sub is not None:
            return {
                "code": 200,
                "message": "查询成功",
                "data": {
                    "username": username,
                    "user_type": user_type,
                    "sub": sub
                }
            }
    
    # 未找到
    return {
        "code": 404,
        "message": f"未找到用户名 {username} 对应的用户",
        "data": None
    }

class UserRoleChangeRequest(BaseModel):
    """用户身份转换请求（仅管理员可用）"""
    original_sub: int = Field(..., gt=0, description="原用户自增主键ID，必须大于0")
    original_role: str = Field(..., description="原用户角色：student/teacher/admin")
    new_role: str = Field(..., description="新用户角色：student/teacher/admin")
    new_business_id: str = Field(..., description="新角色对应的业务ID（如teacher_id/admin_id/student_id）")
    
    model_config = {
        "json_schema_extra": {
            "example": {
                "original_sub": 1,
                "original_role": "student",
                "new_role": "teacher",
                "new_business_id": "t777"
            }
        }
    }

@router.post(
    "/user/change-role",
    summary="用户角色转换（仅管理员）",
    description="管理员将指定用户从原角色转换为新角色，删除原表数据并在新表创建，仅管理员可操作",
)
def change_user_role(
    payload: UserRoleChangeRequest,
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="管理员信息(JSON字符串，包含 sub/username/roles)"),
):
    # 解析并验证当前操作用户
    current_user_info = _parse_current_user(current_user)
    login_user_id = current_user_info.get("sub", 0)
    user_roles = current_user_info.get("roles", [])
    if login_user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    if "admin" not in user_roles and "管理员" not in user_roles:
        raise HTTPException(status_code=403, detail="无权限执行角色转换：仅管理员可操作")
    # 验证请求参数合法性
    original_role = payload.original_role.strip().lower()
    new_role = payload.new_role.strip().lower()
    original_sub = payload.original_sub
    new_business_id = payload.new_business_id.strip()
    if not new_business_id:
        raise HTTPException(status_code=400, detail="新业务ID（teacher_id/admin_id等）不能为空")
    if original_role not in USER_TABLES:
        raise HTTPException(status_code=400, detail=f"原角色不合法，仅支持：{list(USER_TABLES.keys())}")
    if new_role not in USER_TABLES:
        raise HTTPException(status_code=400, detail=f"新角色不合法，仅支持：{list(USER_TABLES.keys())}")
    if original_role == new_role:
        raise HTTPException(status_code=400, detail="原角色与新角色一致，无需转换")
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        # 检查原用户是否存在
        original_user = _fetch_user(cursor, original_sub, original_role)
        if not original_user:
            raise HTTPException(status_code=404, detail=f"未找到ID为{original_sub}的{original_role}用户")
        # 查询原用户完整数据
        original_table_info = USER_TABLES[original_role]
        cursor.execute(
            f"SELECT * FROM {original_table_info['table']} WHERE id = %s",
            (original_sub,)
        )
        original_user_data = cursor.fetchone()
        if not original_user_data:
            raise HTTPException(status_code=404, detail=f"原{original_role}用户数据不存在")
        # 检查新业务ID是否已存在
        new_table_info = USER_TABLES[new_role]
        cursor.execute(
            f"SELECT 1 FROM {new_table_info['table']} WHERE {new_table_info['id_col']} = %s LIMIT 1",
            (new_business_id,)
        )
        if cursor.fetchone():
            raise HTTPException(status_code=400, detail=f"新{new_role}的业务ID {new_business_id} 已存在")
        # 插入新角色表
        new_table = new_table_info["table"]
        new_id_col = new_table_info["id_col"]
        # 构建插入字段和值
        insert_fields = [
            new_id_col, "name", "phone", "email", "password", 
            "school_id", "school_name", "department_id", "department_name", 
             "created_at", "updated_at"
        ]
        # 从原用户数据中取值，无则设为NULL
        insert_values = [
            new_business_id,
            original_user_data.get("name", ""),
            original_user_data.get("phone"),
            original_user_data.get("email"),
            original_user_data.get("password"),
            original_user_data.get("school_id"),
            original_user_data.get("school_name"),
            original_user_data.get("department_id"),
            original_user_data.get("department_name"),
            datetime.now(),  # 新的创建时间
            datetime.now()   # 新的更新时间
        ]
        # 针对admin表补充role字段
        if new_role == "admin":
            insert_fields.append("role")
            insert_values.append("admin")  # 默认admin角色
        # 执行插入
        insert_placeholders = ", ".join(["%s"] * len(insert_fields))
        cursor.execute(
            f"""
            INSERT INTO {new_table} ({', '.join(insert_fields)})
            VALUES ({insert_placeholders})
            """,
            insert_values
        )
        # 获取新插入记录的自增ID
        new_user_id = cursor.lastrowid
        # 删除原角色表中的数据
        cursor.execute(
            f"DELETE FROM {original_table_info['table']} WHERE id = %s",
            (original_sub,)
        )
        # 提交事务
        db.commit()
        return {
            "code": 200,
            "message": f"用户角色已从{original_role}成功转换为{new_role}",
            "data": {
                "original_sub": original_sub,
                "original_role": original_role,
                "new_role": new_role,
                "new_business_id": new_business_id,  # 新的teacher_id/admin_id等
                "new_sub": new_user_id  # 新表中的自增ID
            }
        }

    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        db.rollback()
        logger.error(f"角色转换数据库错误: {str(e)}")
        if "Duplicate entry" in str(e):
            raise HTTPException(status_code=400, detail=f"新{new_role}的业务ID {new_business_id} 已存在")
        raise HTTPException(status_code=500, detail=f"角色转换失败：{str(e)}")
    finally:
        if cursor:
            cursor.close()
