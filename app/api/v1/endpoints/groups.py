from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, Query, Header, Request, Body
from typing import Optional, List
from pydantic import BaseModel
from app.core.dependencies import get_current_user
from app.core.security import decode_access_token, create_access_token 
from app.models.document import DocumentRecord  
import io
import json
import pymysql
from datetime import datetime  
from loguru import logger  
from app.database import get_connection

router = APIRouter()


class CurrentUser(BaseModel):
    """当前用户信息"""
    sub: int
    username: str
    roles: list[str]


class RequestWithCurrentUser(BaseModel):
    """包含current_user的通用请求体"""
    current_user: CurrentUser


class GroupCreate(BaseModel):
    """创建群组请求体"""

    group_id: str | None = None
    group_name: str
    teacher_id: str | None = None
    description: str | None = None
    current_user: CurrentUser


class GroupMember(BaseModel):
    """群组成员增删请求体"""

    member_id: int | None = None
    member_type: str = "student"  # 学生 student / 教师 teacher / 管理员 admin
    action: str = "add"  # add: 添加成员, list_students: 获取教师负责的学生列表
    student_ids: list[int] | None = None  # 批量添加时的学生ID列表
    current_user: CurrentUser


class GroupUpdate(BaseModel):
    group_name: str | None = None
    teacher_id: str | None = None
    description: str | None = None


class GroupBind(BaseModel):
    """绑定群组请求体"""
    group_id: str
    group_name: str
    member_type: str  # 只能是 teacher 或 student
    member_id: int     # 用户内部 ID
    role: str = "member"  # 成员角色：member 或 admin
    current_user: CurrentUser


class BindRequest(BaseModel):
    """绑定群组请求包装器"""
    payload: GroupBind


def _parse_current_user(current_user: Optional[dict|str]) -> dict:
    """Normalize current_user input to dict with keys: sub, username, roles"""
    try:
        if isinstance(current_user, dict):
            return current_user
        if isinstance(current_user, str):
            import urllib.parse
            cu = urllib.parse.unquote(current_user)
            if cu.strip():
                current_user = json.loads(cu)
            else:
                current_user = None
        if not isinstance(current_user, dict):
            return {"sub": 0, "username": "", "roles": []}
        return current_user
    except Exception:
        return {"sub": 0, "username": "", "roles": []}


def _normalize_roles(roles: Optional[list]) -> set:
    if not roles:
        return set()
    out = set()
    for r in roles:
        try:
            s = str(r).strip().lower()
            # tolerate plural forms like 'teachers' -> 'teacher'
            if s.endswith('s'):
                s = s.rstrip('s')
            out.add(s)
        except Exception:
            continue
    return out


def member_exists(cursor, member_type: str, member_id: int) -> bool:
    table_map = {"student": "students", "teacher": "teachers", "admin": "admins"}
    if member_type not in table_map:
        return False
    table = table_map[member_type]
    cursor.execute(f"SELECT 1 FROM `{table}` WHERE `id` = %s", (member_id,))
    return bool(cursor.fetchone())


def _ensure_caller_identity(cursor, cu: dict) -> None:
    """Ensure current_user exists in DB according to one of their declared roles.

    Raises HTTPException(403) when no matching record found.
    """
    sub = cu.get("sub", 0)
    if not sub:
        raise HTTPException(status_code=403, detail="无效的调用者身份")
    roles = _normalize_roles(cu.get("roles", []))
    # If roles list is empty, still try to find user in any table
    if not roles:
        for t in ("students", "teachers", "admins"):
            cursor.execute(f"SELECT 1 FROM `{t}` WHERE `id` = %s", (sub,))
            if cursor.fetchone():
                return
        raise HTTPException(status_code=403, detail="当前用户在系统中不存在或无效")

    for r in roles:
        if r in ("teacher", "student", "admin"):
            if member_exists(cursor, r, sub):
                return

    raise HTTPException(status_code=403, detail="当前用户在系统中不存在或其身份与数据库不符")


def _validate_teacher_exists(cursor, teacher_id: int) -> None:
    cursor.execute("SELECT 1 FROM `teachers` WHERE `id` = %s", (teacher_id,))
    if not cursor.fetchone():
        raise HTTPException(status_code=404, detail=f"教师ID {teacher_id} 不存在")


@router.get(
    "/",
    summary="获取群组列表",
    description="分页查询群组列表，支持关键词与教师工号筛选"
)
def list_groups(
    keyword: str | None = Query(None, description="群组编号/名称关键词"),
    teacher_id: str | None = Query(None, description="按教师工号或教师内部ID筛选（管理员可指定；教师可空使用自身）"),
    page: int = Query(1, ge=1, description="页码（从1开始）"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数（1-100）"),
    current_user: Optional[str] = Header(None, alias="X-Current-User", description="当前登录用户信息(JSON字符串)，示例: {\"sub\":1,\"roles\":[\"admin\"],\"username\":\"admin\"}"),
):
    cu = _parse_current_user(current_user)
    roles_norm = _normalize_roles(cu.get("roles", []))
    # only teachers or admins can call this endpoint
    if not ("admin" in roles_norm or "teacher" in roles_norm):
        raise HTTPException(status_code=403, detail="仅管理员或教师可查询教师所属群组")

    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        # resolve teacher internal id: allow passing teacher.teacher_id (工号) or internal id
        teacher_internal_id = None
        if teacher_id:
            # try find by teacher.teacher_id first
            cursor.execute("SELECT id FROM teachers WHERE teacher_id = %s", (teacher_id,))
            r = cursor.fetchone()
            if r:
                teacher_internal_id = r["id"] if isinstance(r, dict) else r[0]
            else:
                # if numeric, try by internal id
                try:
                    tid = int(teacher_id)
                    cursor.execute("SELECT id FROM teachers WHERE id = %s", (tid,))
                    r2 = cursor.fetchone()
                    if r2:
                        teacher_internal_id = r2["id"] if isinstance(r2, dict) else r2[0]
                except Exception:
                    teacher_internal_id = None

        # if caller is teacher and didn't provide teacher_id, use their identity
        if not teacher_internal_id and "teacher" in roles_norm:
            teacher_internal_id = cu.get("sub", None)

        if not teacher_internal_id or teacher_internal_id == 0:
            raise HTTPException(status_code=400, detail="需要提供有效的教师ID或调用者必须是教师")

        # ensure the teacher exists
        cursor.execute("SELECT id, teacher_id FROM teachers WHERE id = %s", (teacher_internal_id,))
        trow = cursor.fetchone()
        if not trow:
            raise HTTPException(status_code=404, detail="指定教师不存在")

        # Query groups where this teacher is a (active) member
        # For each group compute: student_count, pending_papers (待审阅), reviewed_papers (已审阅)
        list_sql = """
        SELECT
            g.group_id,
            g.group_name,
            g.description,
            g.created_at,
            g.updated_at,
            (
                SELECT COUNT(*) FROM group_members gm WHERE gm.group_id = g.group_id AND gm.member_type='student' AND gm.is_active=1
            ) AS student_count,
            (SELECT COUNT(DISTINCT p.id)
                FROM papers p
                WHERE p.owner_id IN (
                    SELECT member_id FROM group_members WHERE group_id = g.group_id AND member_type='student' AND is_active=1
                ) AND p.status = '待审阅'
            ) AS pending_papers,
            (
                SELECT COUNT(DISTINCT p2.id)
                FROM papers p2
                WHERE p2.owner_id IN (
                    SELECT member_id FROM group_members WHERE group_id = g.group_id AND member_type='student' AND is_active=1
                ) AND p2.status = '已审阅'
            ) AS reviewed_papers
        FROM `groups` g
        WHERE EXISTS (
            SELECT 1 FROM group_members gm2 WHERE gm2.group_id = g.group_id AND gm2.member_type='teacher' AND gm2.member_id = %s AND gm2.is_active=1
        )
        AND (g.group_id LIKE %s OR g.group_name LIKE %s)
        ORDER BY g.created_at DESC
        LIMIT %s OFFSET %s
        """

        like_value = f"%{keyword}%" if keyword else "%"
        offset = (page - 1) * page_size
        cursor.execute(list_sql, (teacher_internal_id, like_value, like_value, page_size, offset))
        rows = cursor.fetchall()

        # count total matching groups for pagination
        count_sql = """
        SELECT COUNT(*) AS total
        FROM `groups` g
        WHERE EXISTS (
            SELECT 1 FROM group_members gm2 WHERE gm2.group_id = g.group_id AND gm2.member_type='teacher' AND gm2.member_id = %s AND gm2.is_active=1
        )
        AND (g.group_id LIKE %s OR g.group_name LIKE %s)
        """
        cursor.execute(count_sql, (teacher_internal_id, like_value, like_value))
        cnt_row = cursor.fetchone()
        total = cnt_row["total"] if cnt_row and isinstance(cnt_row, dict) else (cnt_row[0] if cnt_row else 0)

        items = []
        for row in rows:
            items.append({
                "group_id": row["group_id"],
                "group_name": row["group_name"],
                "description": row.get("description"),
                "student_count": int(row.get("student_count", 0) or 0),
                "pending_papers": int(row.get("pending_papers", 0) or 0),
                "reviewed_papers": int(row.get("reviewed_papers", 0) or 0),
                "created_at": row["created_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("created_at") else None,
                "updated_at": row["updated_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("updated_at") else None,
            })

        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        }
    except pymysql.MySQLError as e:
        logger.exception("群组列表查询数据库异常")
        raise HTTPException(status_code=500, detail=f"数据库错误：{str(e)}")
    finally:
        if cursor:
            cursor.close()
        conn.close()


@router.post(
    "/import",
    summary="导入群组与师生关系",
    description="上传 TSV/CSV 文件批量导入群组及师生关系"
)
async def import_groups(
    file: UploadFile = File(...),
    current_user: Optional[str] = Query(None),
):
   # 这里只做接收并返回模拟结果；实际应解析 Excel 并写入 db
    try:
        if isinstance(current_user, str):
            # 解码URL编码的字符串
            import urllib.parse
            current_user = urllib.parse.unquote(current_user)
            if current_user.strip():
                # 解析为字典
                current_user = json.loads(current_user)
            else:
                current_user = None
        if not isinstance(current_user, dict):
            current_user = {"sub": 0, "username": "", "roles": []}
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"解析current_user失败: {str(e)}")
        current_user = {"sub": 0, "username": "", "roles": []}

    # 权限校验
    required_roles = {"admin", "manager"}
    user_roles = set(current_user.get("roles", []))  
    if not required_roles & user_roles:
        logger.warning(f"用户{current_user['username']}无导入权限，当前角色: {user_roles}")
        raise HTTPException(status_code=403, detail="无批量导入师生群组权限，请联系管理员")

    # 基础文件格式校验
    supported_formats = ('.tsv', '.csv')
    if not file.filename.lower().endswith(supported_formats):
        logger.warning(f"用户{current_user['username']}上传非支持文件：{file.filename}，支持格式：{supported_formats}")
        raise HTTPException(
            status_code=400,
            detail=f"请上传文本表格文件（{', '.join(supported_formats)}）"
        )
    content = await file.read()
    if not content:
        logger.warning(f"用户{current_user['username']}上传空文件：{file.filename}")
        raise HTTPException(status_code=400, detail="上传文件为空，无有效数据")
    
    # 数据解析
    try:
        import_data = []
        required_cols = {"群组编号", "群组名称", "教师工号", "学生学号", "学生姓名"}
        delimiter = '\t' if file.filename.lower().endswith('.tsv') else ','  
        
        try:
            text_content = content.decode('utf-8-sig')  # 自动处理UTF-8 BOM
        except UnicodeDecodeError:
            try:
                text_content = content.decode('gbk')  # 尝试GBK编码
            except UnicodeDecodeError:
                raise Exception("文件编码不支持，请使用UTF-8或GBK编码保存文件")
        
        lines = [line.strip() for line in text_content.split('\n') if line.strip()]
        if not lines:
            raise Exception("文件无有效文本内容")
        
        headers = [h.strip() for h in lines[0].split(delimiter) if h.strip()]
        logger.info(f"解析到的表头: {headers}")
        missing_cols = required_cols - set(headers)
        if missing_cols:
            logger.error(f"用户{current_user['username']}上传文件缺少必填列：{missing_cols}")
            raise HTTPException(status_code=400, detail=f"文件缺少必填列：{', '.join(missing_cols)}")
        
        for line_num, line in enumerate(lines[1:], start=2):
            row_values = [v.strip() for v in line.split(delimiter) if v.strip()]

            row_len = len(row_values)
            header_len = len(headers)
            if row_len != header_len:
                logger.warning(f"第{line_num}行列数异常（表头{header_len}列，当前行{row_len}列），跳过该行")
                continue
            row_dict = dict(zip(headers, row_values))

            if all([row_dict.get(col) for col in required_cols]):
                import_data.append({
                    "group_id": row_dict["群组编号"],
                    "group_name": row_dict["群组名称"],
                    "teacher_id": row_dict["教师工号"],
                    "student_id": row_dict["学生学号"],
                    "student_name": row_dict["学生姓名"]
                })
        
        # 数据清洗结果校验
        if not import_data:
            logger.warning(f"用户{current_user['username']}上传文件无有效师生关系数据")
            raise HTTPException(status_code=400, detail="文件中无有效师生关系数据")
        
        # 数据存储
        imported_count = len(import_data)
        group_ids = set(item["group_id"] for item in import_data)

        conn = get_connection()
        cursor = conn.cursor()
        try:
            # 插入或更新群组
            for item in import_data:
                cursor.execute("""
                    INSERT INTO `groups` (`group_id`, `group_name`, `teacher_id`, `description`)
                    VALUES (%s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE `group_name`=VALUES(`group_name`), `teacher_id`=VALUES(`teacher_id`), `description`=VALUES(`description`)
                """, (item["group_id"], item["group_name"], item["teacher_id"], None))
                
                # 插入或更新教师
                cursor.execute("""
                    INSERT INTO `teachers` (`teacher_id`, `name`, `password`)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE `name`=VALUES(`name`)
                """, (item["teacher_id"], item["teacher_id"], ""))  # 假设name就是teacher_id，如果有更好数据可以改
                
                # 插入或更新学生
                cursor.execute("""
                    INSERT INTO `students` (`student_id`, `name`, `password`)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE `name`=VALUES(`name`)
                """, (item["student_id"], item["student_name"], ""))
                
                # 获取学生ID
                cursor.execute("SELECT `id` FROM `students` WHERE `student_id` = %s", (item["student_id"],))
                student_row = cursor.fetchone()
                if student_row:
                    student_id = student_row[0]
                    # 插入群组成员
                    cursor.execute("""
                        INSERT INTO `group_members` (`group_id`, `member_id`, `member_type`, `role`)
                        VALUES (%s, %s, 'student', 'member')
                        ON DUPLICATE KEY UPDATE `is_active`=1, `role`=VALUES(`role`)
                    """, (item["group_id"], student_id))
                
                # 获取教师ID并添加为成员
                cursor.execute("SELECT `id` FROM `teachers` WHERE `teacher_id` = %s", (item["teacher_id"],))
                teacher_row = cursor.fetchone()
                if teacher_row:
                    teacher_id = teacher_row[0]
                    cursor.execute("""
                        INSERT INTO `group_members` (`group_id`, `member_id`, `member_type`, `role`)
                        VALUES (%s, %s, 'teacher', 'admin')
                        ON DUPLICATE KEY UPDATE `is_active`=1, `role`=VALUES(`role`)
                    """, (item["group_id"], teacher_id))
            
            conn.commit()
            logger.info(f"成功导入{imported_count}条师生关系数据")
        except Exception as e:
            conn.rollback()
            logger.error(f"数据库操作失败: {str(e)}")
            raise HTTPException(status_code=500, detail=f"数据存储失败：{str(e)}")
        finally:
            cursor.close()
            conn.close()
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"用户{current_user['username']}导入失败：{str(e)}")
        raise HTTPException(status_code=500, detail=f"数据导入失败：{str(e)}")
    
    # 返回导入结果
    return {
        "imported": imported_count,
        "message": f"成功识别{imported_count}条有效师生关系，上传文件已存档",
        "operated_by": current_user["username"],
        "operated_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "uploaded_file": file.filename,
        "file_format": file.filename.lower().split('.')[-1],
    }


@router.post(
    "/create",
    summary="创建群组",
    description=(
        "新增单个群组记录。\n"
        "必填字段：group_name。\n"
        "可选字段：group_id（不传则自动生成），teacher_id, description。\n"
        "必填 current_user 字段：sub(数据库用户id), roles(包含 teacher 或 admin), username。\n"
        "示例 current_user: {\"sub\": 3, \"roles\": [\"teacher\"], \"username\": \"li\"}"
    )
)
async def create_group(
    payload: GroupCreate
):
    cu = _parse_current_user(payload.current_user.model_dump())
    group_name = payload.group_name
    group_id = payload.group_id
    teacher_id = payload.teacher_id
    description = payload.description
    # Only teachers or admins can create groups
    allowed = {"admin", "teacher"}

    conn = get_connection()
    try:
        cursor = conn.cursor()
        # normalize and verify caller roles and existence in DB
        roles_norm = _normalize_roles(cu.get("roles", []))
        if not allowed & roles_norm:
            raise HTTPException(status_code=403, detail="仅老师或管理员可创建群组")
        # ensure caller exists in the corresponding table
        _ensure_caller_identity(cursor, cu)

        group_id_value = (group_id or "").strip() or None
        if not group_id_value:
            cursor.execute(
                "SELECT MAX(CAST(`group_id` AS UNSIGNED)) FROM `groups` WHERE `group_id` REGEXP '^[0-9]+$'"
            )
            row = cursor.fetchone()
            if isinstance(row, dict):
                max_id = row.get("MAX(CAST(`group_id` AS UNSIGNED))")
            else:
                max_id = row[0] if row else None
            next_id = 1 if not max_id else int(max_id) + 1
            group_id_value = str(next_id)
        insert_sql = (
            "INSERT INTO `groups` (`group_id`, `group_name`, `teacher_id`, `description`) "
            "VALUES (%s, %s, %s, %s)"
        )
        cursor.execute(
            insert_sql,
            (
                group_id_value,
                group_name.strip(),
                teacher_id.strip() if teacher_id else None,
                description.strip() if description else None,
            ),
        )
        # create owner member record: creator becomes group owner
        creator_member_type = "admin" if "admin" in roles_norm else ("teacher" if "teacher" in roles_norm else "student")
        try:
            cursor.execute(
                "INSERT INTO `group_members` (`group_id`, `member_id`, `member_type`, `role`, `is_active`, `joined_at`) VALUES (%s, %s, %s, %s, 1, NOW()) ON DUPLICATE KEY UPDATE role=VALUES(role), is_active=1",
                (group_id_value, cu.get("sub", 0), creator_member_type, "owner"),
            )
        except Exception:
            # if owner insert fails, rollback group creation as atomic
            conn.rollback()
            raise
        conn.commit()
        return {
            "group_id": group_id_value,
            "group_name": group_name,
            "teacher_id": teacher_id,
            "description": description,
            "message": "群组创建成功",
        }
    except pymysql.err.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=400, detail="群组编号已存在")
    except pymysql.MySQLError as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"数据库错误：{str(e)}")
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


@router.post(
    "/bind",
    summary="绑定群组",
    description="将用户绑定到指定群组"
)
async def bind_group(
    payload: GroupBind
):
    """绑定用户到群组的实现"""
    cu = _parse_current_user(payload.current_user.model_dump())
    group_id = payload.group_id
    group_name = payload.group_name
    member_type = payload.member_type
    member_id = payload.member_id
    role = payload.role
    
    try:
        # 验证入群身份
        if member_type not in ["teacher", "student"]:
            raise HTTPException(status_code=400, detail="入群身份只能是教师或学生")
        
        # 验证角色
        if role not in ["member", "admin"]:
            raise HTTPException(status_code=400, detail="角色必须是 member 或 admin")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"请求参数错误：{str(e)}")
    
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        
        # 验证群组是否存在
        cursor.execute("SELECT 1 FROM `groups` WHERE `group_id` = %s", (group_id,))
        if not cursor.fetchone():
            # 群组不存在，创建新群组
            cursor.execute("""
                INSERT INTO `groups` (`group_id`, `group_name`, `description`)
                VALUES (%s, %s, %s)
            """, (group_id, group_name, None))
        
        # 验证用户是否存在
        table_map = {"teacher": "teachers", "student": "students"}
        cursor.execute(f"SELECT 1 FROM `{table_map[member_type]}` WHERE `id` = %s", (member_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail=f"用户 ID {member_id} 不存在")
        
        # 绑定用户到群组
        cursor.execute("""
            INSERT INTO `group_members` (`group_id`, `member_id`, `member_type`, `role`, `is_active`, `joined_at`)
            VALUES (%s, %s, %s, %s, 1, NOW())
            ON DUPLICATE KEY UPDATE `is_active` = 1, `role` = VALUES(`role`), `updated_at` = NOW()
        """, (group_id, member_id, member_type, role))
        
        conn.commit()
        return {
            "group_id": group_id,
            "group_name": group_name,
            "member_id": member_id,
            "member_type": member_type,
            "role": role,
            "message": "绑定成功"
        }
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"数据库错误：{str(e)}")
    finally:
        if cursor:
            cursor.close()
        conn.close()


class DeleteGroupRequest(BaseModel):
    """删除群组请求体"""
    current_user: CurrentUser


@router.delete(
    "/{group_id}",
    summary="删除群组",
    description="根据群组编号删除群组及其所有成员关系"
)
async def delete_group(group_id: str, payload: DeleteGroupRequest):
    cu = _parse_current_user(payload.current_user.model_dump())
    # Only group owner can delete (dissolve) the group

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT `id` FROM `groups` WHERE `group_id` = %s", (group_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="群组不存在")
        # 检查权限：教师或管理员可删除群组
        roles_norm = _normalize_roles(cu.get("roles", []))
        if "admin" in roles_norm:
            # 管理员拥有所有权限
            pass
        else:
            # 教师需要验证是否是该群组的成员
            cursor.execute(
                "SELECT 1 FROM `group_members` WHERE `group_id`=%s AND `member_id`=%s AND `member_type`='teacher' AND `is_active`=1",
                (group_id, cu.get("sub", 0)),
            )
            if not cursor.fetchone():
                raise HTTPException(status_code=403, detail="只有教师或管理员可解散群组")

        # 删除群组成员关系
        cursor.execute("DELETE FROM `group_members` WHERE `group_id` = %s", (group_id,))
        # 删除群组
        cursor.execute("DELETE FROM `groups` WHERE `group_id` = %s", (group_id,))
        conn.commit()
        return {"group_id": group_id, "message": "群组及其成员关系已删除"}
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"数据库错误：{str(e)}")
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


@router.put(
    "/{group_id}",
    summary="更新群组",
    description="更新群组信息（群名/教师/描述），仅群主或群组管理员可更新"
)
async def update_group(
    group_id: str, 
    payload: GroupUpdate,
    current_user: Optional[str] = Header(None, alias="X-Current-User", description="当前登录用户信息(JSON字符串)，示例: {\"sub\":1,\"roles\":[\"admin\"],\"username\":\"admin\"}")
):
    # 解析并验证当前用户身份
    cu = _parse_current_user(current_user)
    
    conn = get_connection()
    try:
        cursor = conn.cursor()
        # 确保调用者在数据库中存在
        _ensure_caller_identity(cursor, cu)

        # 验证群组是否存在
        cursor.execute("SELECT 1 FROM `groups` WHERE `group_id` = %s", (group_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="群组不存在")

        # 权限检查：教师或管理员可更新
        roles_norm = _normalize_roles(cu.get("roles", []))
        if "admin" in roles_norm:
            # 管理员拥有所有权限
            pass
        else:
            # 教师需要验证是否是该群组的成员
            cursor.execute(
                "SELECT 1 FROM `group_members` WHERE `group_id`=%s AND `member_id`=%s AND `member_type`='teacher' AND `is_active`=1",
                (group_id, cu.get("sub", 0)),
            )
            if not cursor.fetchone():
                raise HTTPException(status_code=403, detail="只有教师或管理员可更新群组信息")

        # 准备更新数据
        updates = []
        params = []
        if payload.group_name is not None:
            updates.append("`group_name` = %s")
            params.append(payload.group_name.strip())
        if payload.teacher_id is not None:
            teacher_id_stripped = payload.teacher_id.strip()
            if teacher_id_stripped and teacher_id_stripped != "string":
                # 确保教师存在
                cursor.execute("SELECT `id` FROM `teachers` WHERE `teacher_id` = %s", (teacher_id_stripped,))
                row = cursor.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="指定教师不存在")
                updates.append("`teacher_id` = %s")
                params.append(teacher_id_stripped)
        if payload.description is not None:
            updates.append("`description` = %s")
            params.append(payload.description.strip())

        if not updates:
            return {"group_id": group_id, "message": "无更新内容"}

        # 执行更新
        params.append(group_id)
        sql = f"UPDATE `groups` SET {', '.join(updates)} WHERE `group_id` = %s"
        cursor.execute(sql, tuple(params))
        conn.commit()
        return {"group_id": group_id, "message": "群组更新成功"}
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"数据库错误：{str(e)}")
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


@router.post(
    "/members",
    summary="添加群组成员或获取教师负责的学生列表",
    description="为指定群组添加成员（单个或批量），或获取教师负责的学生列表"
)
async def add_group_member(
    action: str = Query("add", description="操作类型: add(添加成员) 或 list_students(获取学生列表)", enum=["add", "list_students"]),
    group_id: str | None = Query(None, description="群组ID（add操作时必填）"),
    member_id: int | None = Query(None, description="单个成员ID"),
    student_ids: str | None = Query(None, description="批量添加学生ID列表，逗号分隔，例如: 1,2,3"),
    teacher_id: int | None = Query(None, description="教师ID（管理员查看特定教师学生时必填）"),
    current_user: str = Query(None, description="当前用户信息(JSON字符串)，示例: {\"sub\":1,\"roles\":[\"teacher\"],\"username\":\"teacher1\"}")
):
    # 验证参数：当action为list_students时，不需要填写成员相关参数
    if action == "list_students":
        if member_id is not None:
            raise HTTPException(status_code=400, detail="选择list_students操作时，不需要填写member_id参数")
        if student_ids is not None:
            raise HTTPException(status_code=400, detail="选择list_students操作时，不需要填写student_ids参数")
        if group_id is not None:
            raise HTTPException(status_code=400, detail="选择list_students操作时，不需要填写group_id参数")
    # 当action为add时，需要提供成员ID或学生ID列表，且group_id必填
    elif action == "add":
        if not group_id:
            raise HTTPException(status_code=400, detail="选择add操作时，必须填写group_id参数")
        if member_id is None and student_ids is None:
            raise HTTPException(status_code=400, detail="选择add操作时，必须提供member_id或student_ids参数")
    logger.info(f"请求: group_id={group_id}, action={action}")
    cu = _parse_current_user(current_user)
    roles_norm = _normalize_roles(cu.get("roles", []))
    
    conn = get_connection()
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        # ensure caller identity exists
        _ensure_caller_identity(cursor, cu)
        
        if action == "list_students":
            # 获取教师负责的学生列表
            if "admin" in roles_norm:
                # 管理员需要输入教师ID来查看特定教师的学生
                if not teacher_id:
                    raise HTTPException(status_code=400, detail="管理员查看学生列表时，必须填写teacher_id参数")
                # 验证教师是否存在
                cursor.execute("SELECT 1 FROM `teachers` WHERE `id` = %s", (teacher_id,))
                if not cursor.fetchone():
                    raise HTTPException(status_code=404, detail=f"教师ID {teacher_id} 不存在")
                # 查询该教师的学生
                cursor.execute(
                    """
                    SELECT DISTINCT s.id, s.student_id, s.name, s.phone, s.email, s.grade, s.class_name
                    FROM students s
                    INNER JOIN papers p ON s.id = p.owner_id
                    WHERE p.teacher_id = %s
                    ORDER BY s.name
                    """,
                    (teacher_id,)
                )
                students = cursor.fetchall()
                return {
                    "action": "list_students",
                    "teacher_id": teacher_id,
                    "students": students,
                    "total": len(students)
                }
            elif "teacher" in roles_norm or "教师" in roles_norm:
                # 教师只能返回自己的学生
                teacher_id = cu.get("sub", 0)
                # 通过 papers 表查询与该教师关联的学生
                cursor.execute(
                    """
                    SELECT DISTINCT s.id, s.student_id, s.name, s.phone, s.email, s.grade, s.class_name
                    FROM students s
                    INNER JOIN papers p ON s.id = p.owner_id
                    WHERE p.teacher_id = %s
                    ORDER BY s.name
                    """,
                    (teacher_id,)
                )
                students = cursor.fetchall()
                return {
                    "action": "list_students",
                    "teacher_id": teacher_id,
                    "students": students,
                    "total": len(students)
                }
            else:
                raise HTTPException(status_code=403, detail="只有教师或管理员可获取学生列表")
        
        # 以下是 add 操作
        cursor.execute("SELECT 1 FROM `groups` WHERE `group_id` = %s", (group_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="群组不存在")
        # 检查调用者是否有权限（教师或管理员）
        if "admin" in roles_norm:
            # 管理员拥有所有权限
            pass
        elif "teacher" in roles_norm or "教师" in roles_norm:
            # 教师可以管理群组，拉自己班级的学生
            pass
        else:
            raise HTTPException(status_code=403, detail="只有教师或管理员可添加成员")
        
        # 移除群主任命逻辑，不再需要群组管理员和群主设定
        
        # 批量添加或单个添加
        if student_ids:
            # 批量添加学生
            try:
                student_id_list = [int(s.strip()) for s in student_ids.split(",") if s.strip()]
            except ValueError:
                raise HTTPException(status_code=400, detail="student_ids 格式错误，请使用逗号分隔的数字，例如: 1,2,3")
            
            added_members = []
            for sid in student_id_list:
                # 检查学生是否存在
                cursor.execute("SELECT 1 FROM `students` WHERE `id` = %s", (sid,))
                if not cursor.fetchone():
                    logger.warning(f"学生ID {sid} 不存在，跳过")
                    continue
                
                # 检查师生关系：如果是教师操作，确保学生是该教师的学生
                if "teacher" in roles_norm or "教师" in roles_norm:
                    teacher_id = cu.get("sub", 0)
                    cursor.execute(
                        "SELECT 1 FROM `papers` WHERE `owner_id` = %s AND `teacher_id` = %s",
                        (sid, teacher_id)
                    )
                    if not cursor.fetchone():
                        logger.warning(f"学生ID {sid} 不是该教师的学生，跳过")
                        continue
                
                # 插入成员，所有成员默认为普通成员
                cursor.execute(
                    """
                    INSERT INTO `group_members` (`group_id`, `member_id`, `member_type`, `role`, `is_active`, `joined_at`)
                    VALUES (%s, %s, %s, %s, 1, NOW())
                    ON DUPLICATE KEY UPDATE `is_active` = 1, `role` = VALUES(`role`), `updated_at`=NOW()
                    """,
                    (group_id, sid, "student", "member"),
                )
                added_members.append({
                    "member_id": sid,
                    "member_type": "student",
                    "role": "member"
                })
            
            conn.commit()
            return {
                "group_id": group_id,
                "action": "batch_add",
                "added_members": added_members,
                "total_added": len(added_members),
                "message": f"成功添加 {len(added_members)} 名成员"
            }
        else:
            # 单个添加成员
            if member_id is None:
                raise HTTPException(status_code=400, detail="必须提供 member_id 或 student_ids")
            
            # 检查学生是否存在
            cursor.execute("SELECT 1 FROM `students` WHERE `id` = %s", (member_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail=f"学生ID {member_id} 不存在")
            
            # 检查师生关系：如果是教师操作，确保学生是该教师的学生
            if "teacher" in roles_norm or "教师" in roles_norm:
                teacher_id = cu.get("sub", 0)
                cursor.execute(
                    "SELECT 1 FROM `papers` WHERE `owner_id` = %s AND `teacher_id` = %s",
                    (member_id, teacher_id)
                )
                if not cursor.fetchone():
                    raise HTTPException(status_code=403, detail="只能添加自己班级的学生")
            
            # insert as active member，所有成员默认为普通成员
            cursor.execute(
                """
                INSERT INTO `group_members` (`group_id`, `member_id`, `member_type`, `role`, `is_active`, `joined_at`)
                VALUES (%s, %s, %s, %s, 1, NOW())
                ON DUPLICATE KEY UPDATE `is_active` = 1, `role` = VALUES(`role`), `updated_at`=NOW()
                """,
                (group_id, member_id, "student", "member"),
            )
            conn.commit()
            return {
                "group_id": group_id,
                "member_id": member_id,
                "member_type": "student",
                "role": "member",
                "message": "成员已添加/更新",
            }
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"数据库错误：{str(e)}")
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


@router.delete(
    "/{group_id}/members",
    summary="删除群组成员",
    description="从指定群组移除成员（软删除，设置 is_active=0）"
)
async def remove_group_member(group_id: str, payload: GroupMember):
    cu = _parse_current_user(payload.current_user.model_dump())
    # only owner or group admin can remove members

    if payload.member_type not in ["student", "teacher", "admin"]:
        raise HTTPException(status_code=400, detail="成员类型必须是student、teacher或admin")

    conn = get_connection()
    try:
        cursor = conn.cursor()
        # ensure caller identity exists
        _ensure_caller_identity(cursor, cu)

        cursor.execute("SELECT 1 FROM `groups` WHERE `group_id` = %s", (group_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="群组不存在")
        # 检查权限：教师或管理员可移除成员
        roles_norm = _normalize_roles(cu.get("roles", []))
        if "admin" in roles_norm:
            # 管理员拥有所有权限
            pass
        else:
            # 教师需要验证是否是该群组的成员
            cursor.execute(
                "SELECT 1 FROM `group_members` WHERE `group_id`=%s AND `member_id`=%s AND `member_type`='teacher' AND `is_active`=1",
                (group_id, cu.get("sub", 0)),
            )
            if not cursor.fetchone():
                raise HTTPException(status_code=403, detail="只有教师或管理员可移除成员")

        # check target member exists in group
        cursor.execute(
            "SELECT 1 FROM `group_members` WHERE `group_id`=%s AND `member_id`=%s AND `member_type`=%s AND `is_active`=1",
            (group_id, payload.member_id, payload.member_type),
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="成员不在该群组或已被移除")

        # 移除防止移除群主的逻辑，不再需要群主设定

        cursor.execute(
            "UPDATE `group_members` SET `is_active` = 0 WHERE `group_id` = %s AND `member_id` = %s AND `member_type` = %s",
            (group_id, payload.member_id, payload.member_type),
        )
        conn.commit()
        return {
            "group_id": group_id,
            "member_id": payload.member_id,
            "member_type": payload.member_type,
            "message": "成员已移除",
        }
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"数据库错误：{str(e)}")
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


@router.get(
    "/{group_id}/members",
    summary="获取群组成员信息",
    description="获取指定群组成员列表，可按成员类型筛选"
)
async def get_group_members(
    group_id: str,
    member_type: Optional[str] = Query(None, description="成员类型筛选：student/teacher/admin"),
    include_inactive: bool = Query(False, description="是否包含已移除成员"),
    current_user: str = Query('{"sub": 1, "roles": ["admin"], "username": "admin"}', description="当前登录用户信息(JSON字符串)，示例: {\"sub\":1,\"roles\":[\"admin\"],\"username\":\"admin\"}")
):
    cu = _parse_current_user(current_user)
    roles_norm = _normalize_roles(cu.get("roles", []))

    if member_type and member_type not in ["student", "teacher", "admin"]:
        raise HTTPException(status_code=400, detail="成员类型必须是student、teacher或admin")

    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        cursor.execute("SELECT 1 FROM `groups` WHERE `group_id` = %s", (group_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="群组不存在")

        if not ("admin" in roles_norm or "teacher" in roles_norm):
            cursor.execute(
                "SELECT 1 FROM `group_members` WHERE `group_id`=%s AND `member_id`=%s AND `is_active`=1",
                (group_id, cu.get("sub", 0)),
            )
            if not cursor.fetchone():
                raise HTTPException(status_code=403, detail="无权限查看该群组成员")

        active_clause = "" if include_inactive else " AND gm.is_active = 1"
        members: list[dict] = []

        def _fetch_students():
            sql = f"""
            SELECT
                gm.group_id,
                gm.member_id,
                gm.member_type,
                gm.role,
                gm.joined_at,
                gm.updated_at,
                gm.is_active,
                s.student_id AS account_id,
                s.name,
                s.phone,
                s.email,
                s.grade,
                s.class_name
            FROM group_members gm
            JOIN students s ON s.id = gm.member_id
            WHERE gm.group_id = %s AND gm.member_type = 'student'{active_clause}
            ORDER BY s.name ASC
            """
            cursor.execute(sql, (group_id,))
            return cursor.fetchall() or []

        def _fetch_teachers():
            sql = f"""
            SELECT
                gm.group_id,
                gm.member_id,
                gm.member_type,
                gm.role,
                gm.joined_at,
                gm.updated_at,
                gm.is_active,
                t.teacher_id AS account_id,
                t.name,
                t.phone,
                t.email,
                t.department,
                t.title
            FROM group_members gm
            JOIN teachers t ON t.id = gm.member_id
            WHERE gm.group_id = %s AND gm.member_type = 'teacher'{active_clause}
            ORDER BY t.name ASC
            """
            cursor.execute(sql, (group_id,))
            return cursor.fetchall() or []

        def _fetch_admins():
            sql = f"""
            SELECT
                gm.group_id,
                gm.member_id,
                gm.member_type,
                gm.role,
                gm.joined_at,
                gm.updated_at,
                gm.is_active,
                a.admin_id AS account_id,
                a.name,
                a.phone,
                a.email,
                a.role AS admin_role
            FROM group_members gm
            JOIN admins a ON a.id = gm.member_id
            WHERE gm.group_id = %s AND gm.member_type = 'admin'{active_clause}
            ORDER BY a.name ASC
            """
            cursor.execute(sql, (group_id,))
            return cursor.fetchall() or []

        if member_type == "student":
            members.extend(_fetch_students())
        elif member_type == "teacher":
            members.extend(_fetch_teachers())
        elif member_type == "admin":
            members.extend(_fetch_admins())
        else:
            members.extend(_fetch_students())
            members.extend(_fetch_teachers())
            members.extend(_fetch_admins())

        def _fmt_time(val):
            return val.strftime("%Y-%m-%d %H:%M:%S") if val else None

        return {
            "group_id": group_id,
            "member_type": member_type,
            "include_inactive": include_inactive,
            "total": len(members),
            "members": [
                {
                    "member_id": m.get("member_id"),
                    "member_type": m.get("member_type"),
                    "role": m.get("role"),
                    "is_active": int(m.get("is_active", 0)) if m.get("is_active") is not None else None,
                    "joined_at": _fmt_time(m.get("joined_at")),
                    "updated_at": _fmt_time(m.get("updated_at")),
                    "account_id": m.get("account_id"),
                    "name": m.get("name"),
                    "phone": m.get("phone"),
                    "email": m.get("email"),
                    "grade": m.get("grade"),
                    "class_name": m.get("class_name"),
                    "department": m.get("department"),
                    "title": m.get("title"),
                    "admin_role": m.get("admin_role"),
                }
                for m in members
            ],
        }
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库错误：{str(e)}")
    finally:
        if cursor:
            cursor.close()
        conn.close()


@router.get(
    "/{group_id}/students",
    summary="获取班级学生列表",
    description="获取指定班级的所有学生及其论文状态"
)
async def get_class_students(
    group_id: str,
    current_user: str = Query('{"sub": 1, "roles": ["admin"], "username": "admin"}', description="当前登录用户信息(JSON字符串)，示例: {\"sub\":1,\"roles\":[\"admin\"],\"username\":\"admin\"}")
):
    """获取班级学生列表的实现"""
    cu = _parse_current_user(current_user)
    roles_norm = _normalize_roles(cu.get("roles", []))
    
    # 验证权限：只有管理员或教师可以查看班级学生列表
    if not ("admin" in roles_norm or "teacher" in roles_norm):
        raise HTTPException(status_code=403, detail="仅管理员或教师可查看班级学生列表")

    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        
        # 验证群组是否存在
        cursor.execute("SELECT 1 FROM `groups` WHERE `group_id` = %s", (group_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="群组不存在")
        
        # 获取班级所有学生信息及论文状态
        sql = """
        SELECT
            s.id as student_id,
            s.name as student_name,
            s.student_id as student_number,
            p.id as paper_id,
            p.latest_version as paper_version,
            p.updated_at as paper_update_time,
            (SELECT COUNT(*) FROM annotations WHERE paper_id = p.id) as annotation_count
        FROM
            students s
        JOIN
            group_members gm ON s.id = gm.member_id AND gm.member_type = 'student' AND gm.is_active = 1
        LEFT JOIN
            papers p ON s.id = p.owner_id
        
        WHERE
            gm.group_id = %s
        ORDER BY
            s.name ASC,
            p.updated_at DESC
        """
        
        cursor.execute(sql, (group_id,))
        rows = cursor.fetchall()
        
        # 处理结果，按学生分组，只保留每个学生的最新版本论文
        students = {}
        paper_versions = {}
        
        for row in rows:
            student_id = row.get('student_id')
            paper_id = row.get('paper_id')
            
            if student_id not in students:
                students[student_id] = {
                    "student_id": student_id,
                    "student_name": row.get('student_name'),
                    "student_number": row.get('student_number'),
                    "papers": []
                }
            
            # 如果有论文信息，记录论文版本，只保留最新版本
            if paper_id:
                if paper_id not in paper_versions:
                    paper_versions[paper_id] = row
        
        # 为每个学生添加最新版本的论文
        for student_id, student_info in students.items():
            for paper_id, paper_info in paper_versions.items():
                if paper_info.get('student_id') == student_id:
                    student_info["papers"].append({
                        "paper_id": paper_id,
                        "paper_version": f"v{paper_info.get('paper_version', 1)}",
                        "paper_update_time": paper_info.get('paper_update_time').strftime("%Y-%m-%d %H:%M:%S") if paper_info.get('paper_update_time') else None,
                        "annotation_count": paper_info.get('annotation_count', 0)
                    })
        
        # 转换为列表格式
        result = list(students.values())
        
        return {
            "group_id": group_id,
            "students": result,
            "total": len(result)
        }
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库错误：{str(e)}")
    finally:
        if cursor:
            cursor.close()
        conn.close()


@router.get(
    "/papers",
    summary="查看群组论文列表",
    description="老师查看指定群组的所有成员提交的论文信息"
)
async def get_group_papers(
    teacher_id: str = Query(..., description="教师ID"),
    group_id: str = Query(..., description="群组ID"),
    current_user: str = Query('{"sub": 1, "roles": ["admin"], "username": "admin"}', description="当前登录用户信息(JSON字符串)，示例: {\"sub\":1,\"roles\":[\"admin\"],\"username\":\"admin\"}")
):
    """查看群组论文列表的实现"""
    cu = _parse_current_user(current_user)
    roles_norm = _normalize_roles(cu.get("roles", []))
    
    # 验证权限：只有管理员或教师可以查看群组论文列表
    if not ("admin" in roles_norm or "teacher" in roles_norm):
        raise HTTPException(status_code=403, detail="仅管理员或教师可查看群组论文列表")

    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        
        # 验证教师是否存在
        teacher_internal_id = None
        # 尝试通过教师工号查找
        cursor.execute("SELECT id FROM teachers WHERE teacher_id = %s", (teacher_id,))
        r = cursor.fetchone()
        if r:
            teacher_internal_id = r["id"] if isinstance(r, dict) else r[0]
        else:
            # 尝试通过内部ID查找
            try:
                tid = int(teacher_id)
                cursor.execute("SELECT id FROM teachers WHERE id = %s", (tid,))
                r2 = cursor.fetchone()
                if r2:
                    teacher_internal_id = r2["id"] if isinstance(r2, dict) else r2[0]
            except Exception:
                pass
        
        if not teacher_internal_id:
            raise HTTPException(status_code=404, detail="指定教师不存在")
        
        # 验证群组是否存在
        cursor.execute("SELECT 1 FROM `groups` WHERE `group_id` = %s", (group_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="群组不存在")
        
        # 验证教师是否是该群组的成员
        cursor.execute("""
            SELECT 1 FROM `group_members` 
            WHERE `group_id` = %s AND `member_id` = %s AND `member_type` = 'teacher' AND `is_active` = 1
        """, (group_id, teacher_internal_id))
        if not cursor.fetchone():
            raise HTTPException(status_code=403, detail="教师不是该群组的成员")
        
        # 获取群组所有学生的论文信息
        sql = """
        SELECT
            s.id as student_id,
            s.name as student_name,
            s.student_id as student_number,
            p.id as paper_id,
            p.latest_version as paper_version,
            p.updated_at as paper_update_time,
            p.oss_key as paper_oss_key,
            (SELECT COUNT(*) FROM annotations WHERE paper_id = p.id) as annotation_count
        FROM
            students s
        JOIN
            group_members gm ON s.id = gm.member_id AND gm.member_type = 'student' AND gm.is_active = 1
        LEFT JOIN
            papers p ON s.id = p.owner_id
        
        WHERE
            gm.group_id = %s
        ORDER BY
            s.name ASC,
            p.updated_at DESC
        """
        
        cursor.execute(sql, (group_id,))
        rows = cursor.fetchall()
        
        # 处理结果，按学生分组，只保留每个学生的最新版本论文
        papers = []
        paper_versions = {}
        
        for row in rows:
            paper_id = row.get('paper_id')
            
            if paper_id:
                if paper_id not in paper_versions:
                    paper_versions[paper_id] = row
        
        # 构建论文列表
        for paper_id, paper_info in paper_versions.items():
            papers.append({
                "paper_id": paper_id,
                "student_id": paper_info.get('student_id'),
                "student_name": paper_info.get('student_name'),
                "student_number": paper_info.get('student_number'),
                "paper_version": f"v{paper_info.get('paper_version', 1)}",
                "paper_update_time": paper_info.get('paper_update_time').strftime("%Y-%m-%d %H:%M:%S") if paper_info.get('paper_update_time') else None,
                "annotation_count": paper_info.get('annotation_count', 0),
                "oss_key": paper_info.get('paper_oss_key')
            })
        
        return {
            "group_id": group_id,
            "teacher_id": teacher_id,
            "papers": papers,
            "total": len(papers)
        }
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库错误：{str(e)}")
    finally:
        if cursor:
            cursor.close()
        conn.close()


class BatchDownloadRequest(BaseModel):
    """批量下载请求体"""
    group_id: str
    student_ids: List[int] | None = None
    format: str = "zip"
    current_user: CurrentUser


@router.post(
    "/download/batch",
    summary="批量下载群组论文",
    description="管理员或老师批量下载指定群组的学生论文，支持zip和原格式下载"
)
async def batch_download_papers(
    payload: BatchDownloadRequest
):
    """批量下载群组论文的实现"""
    cu = _parse_current_user(payload.current_user.model_dump())
    group_id = payload.group_id
    student_ids = payload.student_ids
    format = payload.format
    roles_norm = _normalize_roles(cu.get("roles", []))
    
    # 验证权限：只有管理员或教师可以批量下载论文
    if not ("admin" in roles_norm or "teacher" in roles_norm):
        raise HTTPException(status_code=403, detail="仅管理员或教师可批量下载论文")

    # 验证格式参数
    if format not in ["zip", "original"]:
        raise HTTPException(status_code=400, detail="下载格式只能是zip或original")

    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        
        # 验证群组是否存在
        cursor.execute("SELECT 1 FROM `groups` WHERE `group_id` = %s", (group_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="群组不存在")
        
        # 构建SQL查询条件
        where_clause = "gm.group_id = %s"
        params = [group_id]
        
        if student_ids:
            # 为每个学生ID创建占位符
            placeholders = ', '.join(['%s'] * len(student_ids))
            where_clause += f" AND s.id IN ({placeholders})"
            params.extend(student_ids)
        
        # 获取群组学生的论文信息
        sql = f"""
        SELECT
            s.id as student_id,
            s.name as student_name,
            s.student_id as student_number,
            p.id as paper_id,
            p.oss_key as oss_key,
            p.latest_version as paper_version
        FROM
            students s
        JOIN
            group_members gm ON s.id = gm.member_id AND gm.member_type = 'student' AND gm.is_active = 1
        LEFT JOIN
            papers p ON s.id = p.owner_id
        
        WHERE
            {where_clause}
        ORDER BY
            s.name ASC,
            p.updated_at DESC
        """
        
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        
        if not rows:
            raise HTTPException(status_code=404, detail="未找到指定学生的论文")
        
        # 处理论文下载
        # 这里需要实现具体的下载逻辑，包括：
        # 1. 从OSS获取论文文件
        # 2. 按格式打包或直接返回
        # 3. 返回StreamingResponse
        
        # 由于缺少具体的OSS实现，这里返回模拟响应
        papers_to_download = []
        for row in rows:
            paper_id = row.get('paper_id')
            if paper_id:
                papers_to_download.append({
                    "paper_id": paper_id,
                    "student_id": row.get('student_id'),
                    "student_name": row.get('student_name'),
                    "student_number": row.get('student_number'),
                    "paper_version": row.get('paper_version'),
                    "oss_key": row.get('oss_key')
                })
        
        return {
            "group_id": group_id,
            "format": format,
            "total_papers": len(papers_to_download),
            "papers": papers_to_download,
            "message": f"成功准备{len(papers_to_download)}篇论文，格式为{format}"
        }
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库错误：{str(e)}")
    finally:
        if cursor:
            cursor.close()
        conn.close()


# 移除设置群组管理员教师的功能，不再需要群组管理员设定


@router.get(
    "/paper/reviewed/count",
    summary="查看已审阅论文数",
    description="查询指定群组下已审阅论文数量（状态：已审阅、已更新、已定稿、待更新），仅群组群主/管理员教师可访问"
)
def get_reviewed_paper_count(
    group_id: str = Query(..., description="群组ID"),
    current_user: Optional[str] = Header(None, alias="X-Current-User", description="当前登录用户信息(JSON字符串)，示例: {\"sub\":1,\"roles\":[\"teacher\"],\"username\":\"teacher1\"}"),
):
    cu = _parse_current_user(current_user)
    caller_id = cu.get("sub", 0)
    roles_norm = _normalize_roles(cu.get("roles", []))
    
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        _ensure_caller_identity(cursor, cu)
        # 检查权限：教师或管理员可查看已审阅论文数
        roles_norm = _normalize_roles(cu.get("roles", []))
        if "admin" in roles_norm:
            # 管理员拥有所有权限
            pass
        else:
            # 教师需要验证是否是该群组的成员
            cursor.execute(
                "SELECT 1 FROM `group_members` WHERE `group_id`=%s AND `member_id`=%s AND `member_type`='teacher' AND `is_active`=1",
                (group_id, caller_id),
            )
            if not cursor.fetchone():
                raise HTTPException(status_code=403, detail="只有教师或管理员可查看已审阅论文数")
        count_sql = """
        SELECT COUNT(DISTINCT p.id) AS count
        FROM `papers` p
        WHERE p.owner_id IN (
            SELECT member_id FROM group_members 
            WHERE group_id = %s AND member_type='student' AND is_active=1
        ) 
        AND p.status IN ('已审阅', '已更新', '已定稿', '待更新')
        """
        cursor.execute(count_sql, (group_id,))
        count_row = cursor.fetchone()
        count = int(count_row["count"]) if count_row else 0
        
        return {
            "group_id": group_id,
            "reviewed_paper_count": count,
            "message": "已成功查询已审阅论文数"
        }
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库错误：{str(e)}")
    finally:
        if cursor:
            cursor.close()
        conn.close()


@router.get(
    "/paper/uploaded/count",
    summary="查看已上传论文数",
    description="查询指定群组下已上传论文数量（状态：已上传、待审阅），仅群组群主/管理员教师可访问"
)
def get_uploaded_paper_count(
    group_id: str = Query(..., description="群组ID"),
    current_user: Optional[str] = Header(None, alias="X-Current-User", description="当前登录用户信息(JSON字符串)，示例: {\"sub\":1,\"roles\":[\"teacher\"],\"username\":\"teacher1\"}"),
):
    cu = _parse_current_user(current_user)
    caller_id = cu.get("sub", 0)
    roles_norm = _normalize_roles(cu.get("roles", []))
    
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        _ensure_caller_identity(cursor, cu)
        # 检查权限：教师或管理员可查看已上传论文数
        roles_norm = _normalize_roles(cu.get("roles", []))
        if "admin" in roles_norm:
            # 管理员拥有所有权限
            pass
        else:
            # 教师需要验证是否是该群组的成员
            cursor.execute(
                "SELECT 1 FROM `group_members` WHERE `group_id`=%s AND `member_id`=%s AND `member_type`='teacher' AND `is_active`=1",
                (group_id, caller_id),
            )
            if not cursor.fetchone():
                raise HTTPException(status_code=403, detail="只有教师或管理员可查看已上传论文数")
        count_sql = """
        SELECT COUNT(DISTINCT p.id) AS count
        FROM `papers` p
        WHERE p.owner_id IN (
            SELECT member_id FROM group_members 
            WHERE group_id = %s AND member_type='student' AND is_active=1
        ) 
        AND p.status IN ('已上传', '待审阅')
        """
        cursor.execute(count_sql, (group_id,))
        count_row = cursor.fetchone()
        count = int(count_row["count"]) if count_row else 0
        
        return {
            "group_id": group_id,
            "uploaded_paper_count": count,
            "message": "已成功查询已上传论文数"
        }
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库错误：{str(e)}")
    finally:
        if cursor:
            cursor.close()
        conn.close()


@router.get(
    "/paper/unuploaded/members",
    summary="查看未上传论文的成员",
    description="查询指定群组下未上传论文的学生成员列表，仅群组群主/管理员教师可访问"
)
def get_unuploaded_paper_members(
    group_id: str = Query(..., description="群组ID"),
    current_user: Optional[str] = Header(None, alias="X-Current-User", description="当前登录用户信息(JSON字符串)，示例: {\"sub\":1,\"roles\":[\"teacher\"],\"username\":\"teacher1\"}"),
):
    cu = _parse_current_user(current_user)
    caller_id = cu.get("sub", 0)
    roles_norm = _normalize_roles(cu.get("roles", []))
    
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        _ensure_caller_identity(cursor, cu)
        # 检查权限：教师或管理员可查看未上传论文成员
        roles_norm = _normalize_roles(cu.get("roles", []))
        if "admin" in roles_norm:
            # 管理员拥有所有权限
            pass
        else:
            # 教师需要验证是否是该群组的成员
            cursor.execute(
                "SELECT 1 FROM `group_members` WHERE `group_id`=%s AND `member_id`=%s AND `member_type`='teacher' AND `is_active`=1",
                (group_id, caller_id),
            )
            if not cursor.fetchone():
                raise HTTPException(status_code=403, detail="只有教师或管理员可查看未上传论文成员")
        cursor.execute(
            """
            SELECT gm.member_id, s.student_id, s.name 
            FROM `group_members` gm
            LEFT JOIN `students` s ON gm.member_id = s.id
            WHERE gm.group_id = %s 
            AND gm.member_type = 'student' 
            AND gm.is_active = 1
            """,
            (group_id,)
        )
        all_students = cursor.fetchall()
        if not all_students:
            return {
                "group_id": group_id,
                "unuploaded_members": [],
                "message": "该群组暂无学生成员"
            }
        cursor.execute(
            """
            SELECT DISTINCT p.owner_id 
            FROM `papers` p
            WHERE p.owner_id IN (
                SELECT member_id FROM group_members 
                WHERE group_id = %s AND member_type='student' AND is_active=1
            ) 
            AND p.status IN ('已上传', '待审阅')
            """,
            (group_id,)
        )
        uploaded_student_ids = [row["owner_id"] for row in cursor.fetchall()]
        unuploaded_members = [
            {
                "student_internal_id": student["member_id"],
                "student_id": student["student_id"],  # 学生学号
                "student_name": student["name"]       # 学生姓名
            }
            for student in all_students
            if student["member_id"] not in uploaded_student_ids
        ]
        return {
            "group_id": group_id,
            "unuploaded_members_count": len(unuploaded_members),
            "unuploaded_members": unuploaded_members,
            "message": "已成功查询未上传论文的成员列表"
        }
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库错误：{str(e)}")
    finally:
        if cursor:
            cursor.close()
        conn.close()
