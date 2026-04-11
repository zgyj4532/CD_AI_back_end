from fastapi import APIRouter, Depends, HTTPException, Query
from app.schemas.annotation import AnnotationOut
import pymysql
import json
from app.database import get_db
from loguru import logger
from datetime import datetime
from typing import Optional, Dict, List
import urllib.parse
import re 

router = APIRouter()


def _parse_current_user(current_user: Optional[str]) -> dict:
    try:
        if not current_user:
            return {"sub": 0, "username": "", "roles": []}
        raw = urllib.parse.unquote(current_user)
        if not raw.strip():
            return {"sub": 0, "username": "", "roles": []}
        if raw.isdigit():
            return {"sub": int(raw), "username": f"user{raw}", "roles": ["student"]}
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"sub": 0, "username": "", "roles": []}


def _parse_coordinates(coord_str: Optional[str]) -> Optional[Dict[str, float]]:
    if not coord_str:
        return None
    try:
        return json.loads(coord_str)
    except Exception:
        logger.warning(f"解析坐标失败: {coord_str}")
        return None

@router.post(
    "/",
    response_model=AnnotationOut,
    summary="创建论文标注",
    description="为指定论文创建标注并校验坐标后入库"
)
def create_annotation(
    paper_id: int = Query(..., description="所属论文ID，必须传入且为有效整数"),
    teacher_id: int = Query(..., description="论文绑定的教师ID，必须传入且为有效正整数"),
    content: str = Query(..., description="标注文本内容，不能为空"),
    coordinates: Optional[str] = Query(None, description="坐标信息（必须为(x,y)格式，x和y为数字）"),
    paragraph_id: Optional[str] = Query(None, description="段落ID（可选参数）"),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
    db: pymysql.connections.Connection = Depends(get_db)
):
    current_user = _parse_current_user(current_user)
    login_user_id = current_user.get("sub", 0)
    if login_user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    
    if not isinstance(teacher_id, int) or teacher_id <= 0:
        raise HTTPException(status_code=400, detail="teacher_id必须是有效正整数")
    
    if not content.strip():
        raise HTTPException(status_code=400, detail="标注文本内容不能为空")
    cursor = None
    try:
        cursor = db.cursor()
        cursor.execute(
            """
            SELECT 1 FROM papers 
            WHERE id = %s AND teacher_id = %s
            """,
            (paper_id, teacher_id)
        )
        paper_exists = cursor.fetchone()
        if not paper_exists:
            raise HTTPException(
                status_code=404,
                detail=f"论文不存在或论文绑定的教师ID不匹配（传入teacher_id: {teacher_id}）"
            )
        if login_user_id != teacher_id:
            raise HTTPException(
                status_code=403,
                detail=f"无权限创建标注：登录用户ID（{login_user_id}）必须与论文绑定的教师ID（{teacher_id}）一致"
            )
    except pymysql.MySQLError as e:
        logger.error(f"论文信息校验数据库异常: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="论文信息校验失败，请稍后重试"
        )
    finally:
        if cursor:
            cursor.close()
    
    coord_json = None
    if coordinates:
        try:
            coord_pattern = re.compile(r'^\s*\(\s*(-?\d+(\.\d+)?)\s*,\s*(-?\d+(\.\d+)?)\s*\)\s*$')
            match = coord_pattern.match(coordinates)
            if not match:
                raise ValueError("坐标格式必须为(x,y)，其中x和y为数字（支持整数/浮点数），例如(1,2)、(3.5,4.8)")
            x = float(match.group(1))
            y = float(match.group(3))
            coord_data = {"x": x, "y": y}
            coord_json = json.dumps(coord_data)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"坐标格式不合法: {str(e)}")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"坐标解析失败: {str(e)}")
    try:
        cursor = db.cursor()
        insert_sql = """
        INSERT INTO annotations (
            paper_id, author_id, paragraph_id, coordinates, content, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        now = datetime.now()
        cursor.execute(
            insert_sql,
            (
                paper_id,
                login_user_id,
                paragraph_id,
                coord_json,
                content.strip(),
                now,
                now
            )
        )
        db.commit()
        
        annotation_id = cursor.lastrowid
        cursor.execute(
            """
            SELECT id, paper_id, author_id, paragraph_id, coordinates, content, created_at, updated_at
            FROM annotations WHERE id = %s
            """,
            (annotation_id,)
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=500, detail="创建标注后查询全量信息失败")

        logger.info(
            f"教师用户{login_user_id}为论文{paper_id}创建标注成功，标注ID: {annotation_id}"
        )

        return AnnotationOut(
            id=row[0],
            paper_id=row[1],
            author_id=row[2],
            paragraph_id=row[3],
            coordinates=_parse_coordinates(row[4]),
            content=row[5],
            created_at=row[6].strftime("%Y-%m-%dT%H:%M:%SZ"),
            updated_at=row[7].strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    except pymysql.MySQLError as e:
        db.rollback()
        logger.error(f"标注存储数据库异常: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="标注创建失败，请稍后重试"
        )
    finally:
        if cursor:
            cursor.close()


@router.put(
    "/{annotation_id}",
    response_model=AnnotationOut,
    summary="更新论文标注",
    description="更新指定ID的论文标注，仅标注所属论文绑定的教师可操作"
)
def update_annotation(
    annotation_id: int,
    paper_id: int = Query(..., description="所属论文ID，必须传入且为有效整数"),
    teacher_id: int = Query(..., description="论文绑定的教师ID，必须传入且为有效正整数"),
    content: Optional[str] = Query(None, description="标注文本内容（为空则不更新）"),
    coordinates: Optional[str] = Query(None, description="坐标信息（必须为(x,y)格式，x和y为数字，为空则不更新）"),
    paragraph_id: Optional[str] = Query(None, description="段落ID（可选参数，为空则不更新）"),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
    db: pymysql.connections.Connection = Depends(get_db)
):
    current_user = _parse_current_user(current_user)
    login_user_id = current_user.get("sub", 0)
    if login_user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    if not isinstance(teacher_id, int) or teacher_id <= 0:
        raise HTTPException(status_code=400, detail="teacher_id必须是有效正整数")
    if login_user_id != teacher_id:
        raise HTTPException(
            status_code=403,
            detail=f"无权限更新标注：登录用户ID（{login_user_id}）必须与论文绑定的教师ID（{teacher_id}）一致"
        )
    cursor = None
    try:
        cursor = db.cursor()
        cursor.execute(
            """
            SELECT 1 FROM papers 
            WHERE id = %s AND teacher_id = %s
            """,
            (paper_id, teacher_id)
        )
        paper_exists = cursor.fetchone()
        if not paper_exists:
            raise HTTPException(
                status_code=404,
                detail=f"论文不存在或论文绑定的教师ID不匹配（传入teacher_id: {teacher_id}）"
            )
        cursor.execute(
            """
            SELECT 1 FROM annotations 
            WHERE id = %s AND paper_id = %s
            """,
            (annotation_id, paper_id)
        )
        annotation_exists = cursor.fetchone()
        if not annotation_exists:
            raise HTTPException(
                status_code=404,
                detail=f"标注不存在：标注ID({annotation_id}) 不属于论文ID({paper_id})"
            )
    except pymysql.MySQLError as e:
        logger.error(f"标注/论文信息校验数据库异常: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="标注信息校验失败，请稍后重试"
        )
    finally:
        if cursor:
            cursor.close()
    coord_json = None
    if coordinates:
        try:
            coord_pattern = re.compile(r'^\s*\(\s*(-?\d+(\.\d+)?)\s*,\s*(-?\d+(\.\d+)?)\s*\)\s*$')
            match = coord_pattern.match(coordinates)
            if not match:
                raise ValueError("坐标格式必须为(x,y)，其中x和y为数字（支持整数/浮点数），例如(1,2)、(3.5,4.8)")
            x = float(match.group(1))
            y = float(match.group(3))
            coord_data = {"x": x, "y": y}
            coord_json = json.dumps(coord_data)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"坐标格式不合法: {str(e)}")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"坐标解析失败: {str(e)}")
    try:
        cursor = db.cursor()
        update_fields = []
        update_values = []
        if content is not None and content.strip():
            update_fields.append("content = %s")
            update_values.append(content.strip())
        if coord_json is not None:
            update_fields.append("coordinates = %s")
            update_values.append(coord_json)
        if paragraph_id is not None:
            update_fields.append("paragraph_id = %s")
            update_values.append(paragraph_id)
        update_fields.append("updated_at = %s") 
        update_values.append(datetime.now())
        if len(update_fields) == 1: 
            cursor.execute(
                """
                SELECT id, paper_id, author_id, paragraph_id, coordinates, content, created_at, updated_at 
                FROM annotations WHERE id = %s
                """,
                (annotation_id,)
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="标注不存在")
            return AnnotationOut(
                id=row[0],
                paper_id=row[1],
                author_id=row[2],
                paragraph_id=row[3],
                coordinates=_parse_coordinates(row[4]), 
                content=row[5],
                created_at=row[6].strftime("%Y-%m-%dT%H:%M:%SZ"),
                updated_at=row[7].strftime("%Y-%m-%dT%H:%M:%SZ")
            )
        update_sql = f"""
        UPDATE annotations 
        SET {', '.join(update_fields)} 
        WHERE id = %s AND paper_id = %s
        """
        update_values.extend([annotation_id, paper_id])
        cursor.execute(update_sql, tuple(update_values))
        db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="标注更新失败：标注不存在或无字段更新")
        cursor.execute(
            """
            SELECT id, paper_id, author_id, paragraph_id, coordinates, content, created_at, updated_at 
            FROM annotations WHERE id = %s
            """,
            (annotation_id,)
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="标注更新后查询失败")

        logger.info(
            f"教师用户{login_user_id}更新标注成功，标注ID: {annotation_id}"
        )
        return AnnotationOut(
            id=row[0],
            paper_id=row[1],
            author_id=row[2],
            paragraph_id=row[3],
            coordinates=_parse_coordinates(row[4]),
            content=row[5],
            created_at=row[6].strftime("%Y-%m-%dT%H:%M:%SZ"),
            updated_at=row[7].strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    except pymysql.MySQLError as e:
        db.rollback()
        logger.error(f"标注更新数据库异常: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="标注更新失败，请稍后重试"
        )
    finally:
        if cursor:
            cursor.close()


@router.get(
    "/paper",
    response_model=List[AnnotationOut],
    summary="获取论文标注",
    description="根据论文所属用户ID与论文ID查询该论文的所有批注"
)
def list_annotations_by_paper(
    owner_id: int = Query(..., description="论文所属用户ID（papers.owner_id）"),
    paper_id: int = Query(..., description="论文ID"),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
    db: pymysql.connections.Connection = Depends(get_db)
):
    _ = _parse_current_user(current_user)
    if not isinstance(owner_id, int) or owner_id <= 0:
        raise HTTPException(status_code=400, detail="owner_id必须是有效正整数")

    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            """
            SELECT 1 FROM papers
            WHERE id = %s AND owner_id = %s
            """,
            (paper_id, owner_id)
        )
        if not cursor.fetchone():
            raise HTTPException(
                status_code=404,
                detail="论文不存在或论文所属用户ID不匹配"
            )

        cursor.execute(
            """
            SELECT id, paper_id, author_id, paragraph_id, coordinates, content, created_at, updated_at
            FROM annotations
            WHERE paper_id = %s
            ORDER BY created_at ASC
            """,
            (paper_id,)
        )
        rows = cursor.fetchall() or []

        return [
            AnnotationOut(
                id=row["id"],
                paper_id=row["paper_id"],
                author_id=row["author_id"],
                paragraph_id=row.get("paragraph_id"),
                coordinates=_parse_coordinates(row.get("coordinates")), 
                content=row.get("content"),
                created_at=row["created_at"].strftime("%Y-%m-%dT%H:%M:%SZ") if row["created_at"] else None,
                updated_at=row["updated_at"].strftime("%Y-%m-%dT%H:%M:%SZ") if row["updated_at"] else None,
            )
            for row in rows
        ]
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        logger.error(f"查询标注数据库异常: {str(e)}")
        raise HTTPException(status_code=500, detail="标注查询失败，请稍后重试")
    finally:
        if cursor:
            cursor.close()


@router.delete(
    "/{annotation_id}",
    summary="删除论文标注",
    description="删除指定ID的论文标注，仅标注所属论文绑定的教师可操作"
)
def delete_annotation(
    annotation_id: int,
    paper_id: int = Query(..., description="所属论文ID，必须传入且为有效整数"),
    teacher_id: int = Query(..., description="论文绑定的教师ID，必须传入且为有效正整数"),
    current_user: Optional[str] = Query(None, description="登录用户信息(JSON字符串，包含 sub/username/roles)"),
    db: pymysql.connections.Connection = Depends(get_db)
):
    current_user = _parse_current_user(current_user)
    login_user_id = current_user.get("sub", 0)
    if login_user_id <= 0:
        raise HTTPException(status_code=401, detail="请先登录后再操作")
    if not isinstance(teacher_id, int) or teacher_id <= 0:
        raise HTTPException(status_code=400, detail="teacher_id必须是有效正整数")
    if login_user_id != teacher_id:
        raise HTTPException(
            status_code=403,
            detail=f"无权限删除标注：登录用户ID（{login_user_id}）必须与论文绑定的教师ID（{teacher_id}）一致"
        )
    cursor = None
    try:
        cursor = db.cursor()
        cursor.execute(
            """
            SELECT 1 FROM papers 
            WHERE id = %s AND teacher_id = %s
            """,
            (paper_id, teacher_id)
        )
        paper_exists = cursor.fetchone()
        if not paper_exists:
            raise HTTPException(
                status_code=404,
                detail=f"论文不存在或论文绑定的教师ID不匹配（传入teacher_id: {teacher_id}）"
            )
        cursor.execute(
            """
            SELECT id, paper_id, author_id, paragraph_id, coordinates, content, created_at, updated_at
            FROM annotations 
            WHERE id = %s AND paper_id = %s
            """,
            (annotation_id, paper_id)
        )
        del_row = cursor.fetchone()
        if not del_row:
            raise HTTPException(
                status_code=404,
                detail=f"标注不存在：标注ID({annotation_id}) 不属于论文ID({paper_id})"
            )
        delete_sql = """
        DELETE FROM annotations 
        WHERE id = %s AND paper_id = %s
        """
        cursor.execute(delete_sql, (annotation_id, paper_id))
        db.commit()

        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="标注删除失败：标注不存在")

        logger.info(
            f"教师用户{login_user_id}删除标注成功，标注ID: {annotation_id}，论文ID: {paper_id}"
        )

        return {
            "code": 200,
            "message": f"标注ID({annotation_id}) 删除成功",
            "data": {
                "annotation_id": del_row[0],
                "paper_id": del_row[1],
                "author_id": del_row[2],
                "paragraph_id": del_row[3],
                "coordinates": _parse_coordinates(del_row[4]),
                "content": del_row[5],
                "created_at": del_row[6].strftime("%Y-%m-%dT%H:%M:%SZ"),
                "updated_at": del_row[7].strftime("%Y-%m-%dT%H:%M:%SZ")
            }
        }
    except pymysql.MySQLError as e:
        db.rollback()
        logger.error(f"标注删除数据库异常: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="标注删除失败，请稍后重试"
        )
    finally:
        if cursor:
            cursor.close()
