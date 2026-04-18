"""材料相关接口"""
from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
import pymysql
from app.database import get_db
from app.schemas.document import MaterialResponse
from app.services.oss import upload_attachment_to_storage, get_file_from_oss
import json
from datetime import datetime
from typing import Optional
import os
import sys
import tempfile
import shutil
import subprocess
import io
import zipfile
try:
    from docx2pdf import convert as docx2pdf_convert
except ImportError:
    docx2pdf_convert = None

router = APIRouter()

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


@router.post(
    "/upload",
    response_model=MaterialResponse,
    summary="上传材料",
    description="上传材料并存储到数据库"
)
async def upload_material(
    file: UploadFile = File(...),  
    name: str = Query(..., description="username"),
    paper_id: int = Query(..., description="关联的论文ID"),
    file_type: str = Query(
        "document", 
        description="文件类型，可选值：document(文档)、essay(文章)",
        enum=["document", "essay"] 
    ),
    version: int = Query(1, description="版本号，默认1，最小值1", ge=1),
    remark: str = Query(None, description="备注信息"),
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="提交者信息(JSON字符串，包含 sub/username/roles)"),
):
    # 解析当前用户信息
    current_user = _parse_current_user(current_user)
    login_username = current_user.get("username", "")
    # 基础参数校验
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")
    if not name:
        raise HTTPException(status_code=400, detail="作者/上传者姓名不能为空")
    # 校验登录用户名与传入的name一致
    if not login_username:
        raise HTTPException(status_code=401, detail="未获取到有效登录用户信息，请先登录")
    if login_username != name:
        raise HTTPException(
            status_code=403, 
            detail=f"无权限上传：登录用户名[{login_username}]与传入的username[{name}]不一致"
        )
    # 验证 paper_id 是否存在于 papers 表中
    cursor = None
    try:
        cursor = db.cursor()
        cursor.execute("SELECT id FROM papers WHERE id = %s", (paper_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail=f"指定的论文ID {paper_id} 不存在")
    finally:
        if cursor:
            cursor.close()
    # 读取文件内容
    try:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="上传的文件内容不能为空")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"读取文件失败：{str(e)}")
    
    # 处理docx文件自动转换为pdf（仅上传pdf，不上传原docx）
    storage_path = ""
    original_filename = file.filename
    is_docx = original_filename.lower().endswith(".docx")
    
    try:
        if is_docx:
            # 如果是docx文件，转换为pdf并只上传pdf
            pdf_content, pdf_filename = convert_docx_to_pdf(content, original_filename)
            storage_path = upload_attachment_to_storage(pdf_filename, pdf_content)
            # 更新文件名记录为pdf文件名
            original_filename = pdf_filename
        else:
            # 非docx文件直接上传原文件
            storage_path = upload_attachment_to_storage(original_filename, content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文件存储/转换失败：{str(e)}")
    
    # 数据库操作
    cursor = None
    try:
        # 创建游标
        cursor = db.cursor(pymysql.cursors.DictCursor)
        # 插入SQL（保持原有字段不变）
        insert_sql = """
            INSERT INTO file_records (
                name, filename, upload_time, storage_path, 
                file_type, version, paper_id, remark, created_at, updated_at
            )
            VALUES (%s, %s, NOW(), %s, %s, %s, %s, %s, NOW(), NOW())
        """
        # 执行插入
        cursor.execute(
            insert_sql,
            (
                name,
                original_filename,  # docx时记录为pdf文件名，非docx记录原文件名
                storage_path,       # docx时存储pdf路径，非docx存储原文件路径
                file_type,
                version,
                paper_id,
                remark,          
            )
        )
        # 获取新增记录ID
        material_id = cursor.lastrowid
        # 提交事务
        db.commit()
        # 查询新增记录并返回
        select_sql = """
            SELECT 
                id, name, filename, upload_time, storage_path, 
                file_type, version, paper_id, remark, created_at, updated_at 
            FROM file_records 
            WHERE id = %s
        """
        cursor.execute(select_sql, (material_id,))
        new_record = cursor.fetchone()
        if not new_record:
            raise HTTPException(status_code=500, detail="文件上传成功，但查询不到新增记录")
        # 将上传文件的 content_type 加入返回记录
        try:
            # docx转换后content_type改为pdf的类型
            new_record["content_type"] = "application/pdf" if is_docx else file.content_type
        except Exception:
            new_record["content_type"] = None
        # 返回结果
        return new_record
    except pymysql.MySQLError as e:
        # 数据库异常回滚事务
        db.rollback()
        raise HTTPException(status_code=500, detail=f"数据库操作失败：{str(e)}")
    finally:
        # 确保游标关闭
        if cursor:
            cursor.close()


@router.put(
    "/{material_id}",
    response_model=MaterialResponse,
    summary="更新材料",
    description="替换已有材料文件并更新记录"
)
async def update_material(
    material_id: int,
    file: UploadFile = File(...),
    name: str = Query(..., description="username"),
    paper_id: int = Query(None, description="关联的论文ID"),
    file_type: str = Query(None, description="文件类型：document(文档)或essay(文章)", enum=["document", "essay"]),
    version: int = Query(None, description="版本号", ge=1),
    remark: str = Query(None, description="备注"),
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="提交者信息(JSON字符串，包含 sub/username/roles)"),
):
    # 解析当前用户信息
    current_user = _parse_current_user(current_user)
    login_username = current_user.get("username", "")
    # 基础文件参数校验
    if not file.filename:
        raise HTTPException(status_code=400, detail="上传的文件必须包含文件名")
    if not name:
        raise HTTPException(status_code=400, detail="作者/上传者姓名不能为空")
    # 校验登录用户信息
    if not login_username:
        raise HTTPException(status_code=401, detail="未获取到有效登录用户信息，请先登录")
    # 校验传入的name与登录用户名一致
    if login_username != name:
        raise HTTPException(
            status_code=403, 
            detail=f"无权限更新：登录用户名[{login_username}]与传入的username[{name}]不一致"
        )
    # 验证 paper_id 是否存在于 papers 表中（如果提供了的话）
    if paper_id is not None:
        cursor = None
        try:
            cursor = db.cursor()
            cursor.execute("SELECT id FROM papers WHERE id = %s", (paper_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail=f"指定的论文ID {paper_id} 不存在")
        finally:
            if cursor:
                cursor.close()
    # 读取文件内容
    try:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="上传的文件内容不能为空")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"读取上传文件失败：{str(e)}")
    
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        # 检查指定ID的材料是否存在，并获取原有name
        cursor.execute("SELECT id, name FROM file_records WHERE id = %s", (material_id,))
        existing_record = cursor.fetchone()
        if not existing_record:
            raise HTTPException(status_code=404, detail=f"ID为{material_id}的材料不存在")
        # 校验传入的name必须与原记录name一致
        original_name = existing_record["name"]
        if name != original_name:
            raise HTTPException(
                status_code=400, 
                detail=f"传入的作者姓名与原记录不一致，原姓名：{original_name}，传入姓名：{name}"
            )
        # 构建动态更新SQL
        update_fields = []
        update_params = []
        # 必更新字段：文件名、上传时间、storage_path
        update_fields.append("filename = %s")
        update_params.append(file.filename)
        update_fields.append("upload_time = NOW()")
        update_fields.append("storage_path = %s")
        update_params.append(upload_attachment_to_storage(file.filename, content))
        # 可选更新字段
        update_fields.append("name = %s")
        update_params.append(name)
        
        if file_type is not None:
            update_fields.append("file_type = %s")
            update_params.append(file_type)
        if version is not None:
            update_fields.append("version = %s")
            update_params.append(version)
        if paper_id is not None:
            update_fields.append("paper_id = %s")
            update_params.append(paper_id)
        # 最后更新updated_at字段
        update_fields.append("updated_at = NOW()")
        # 拼接更新SQL
        update_sql = f"""
            UPDATE file_records
            SET {', '.join(update_fields)}
            WHERE id = %s
        """
        # 添加material_id到参数列表末尾
        update_params.append(material_id)
        # 执行更新
        cursor.execute(update_sql, tuple(update_params))
        db.commit()
        # 查询更新后的完整记录并返回
        cursor.execute(
            """
            SELECT 
                id, name, filename, upload_time, storage_path, 
                file_type, version, paper_id, remark, created_at, updated_at 
            FROM file_records WHERE id = %s
            """,
            (material_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=500, detail="更新成功但查询不到记录")
        # 将上传文件的 content_type 加入返回数据
        try:
            row["content_type"] = file.content_type
        except Exception:
            row["content_type"] = None
        return MaterialResponse(**row)
    except pymysql.MySQLError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"数据库错误：{str(e)}")
    finally:
        if cursor:
            cursor.close()


@router.delete(
    "/{material_id}",
    summary="删除材料",
    description="根据材料ID删除记录"
)
def delete_material(
    material_id: int,
    name: str = Query(..., description="username"),
    db: pymysql.connections.Connection = Depends(get_db),
    current_user: Optional[str] = Query(None, description="提交者信息(JSON字符串，包含 sub/username/roles)"),
):
    # 解析当前用户信息
    current_user = _parse_current_user(current_user)
    login_username = current_user.get("username", "")
    # 基础参数校验
    if not name:
        raise HTTPException(status_code=400, detail="作者/上传者姓名不能为空")
    # 登录状态和用户名校验
    if not login_username:
        raise HTTPException(status_code=401, detail="未获取到有效登录用户信息，请先登录")
    if login_username != name:
        raise HTTPException(
            status_code=403, 
            detail=f"无权限删除：登录用户名[{login_username}]与传入的username[{name}]不一致"
        )
    cursor = None
    try:
        # 创建游标
        cursor = db.cursor(pymysql.cursors.DictCursor)
        # 检查指定ID的材料是否存在，并获取原记录的name
        check_sql = "SELECT id, name FROM file_records WHERE id = %s"
        cursor.execute(check_sql, (material_id,))
        existing_record = cursor.fetchone()
        if not existing_record:
            raise HTTPException(status_code=404, detail=f"ID为{material_id}的材料不存在")
        # 校验传入的name与原记录的name一致
        original_name = existing_record["name"]
        if name != original_name:
            raise HTTPException(
                status_code=400, 
                detail=f"传入的作者姓名与原记录不一致，原姓名：{original_name}，传入姓名：{name}"
            )
        # 执行删除操作
        delete_sql = "DELETE FROM file_records WHERE id = %s"
        cursor.execute(delete_sql, (material_id,))
        # 提交事务
        db.commit()
        # 返回友好的删除成功响应
        return {
            "code": 200,
            "message": "删除成功",
            "material_id": material_id,
            "username": login_username,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    except pymysql.MySQLError as e:
        # 数据库异常时回滚事务
        db.rollback()
        raise HTTPException(status_code=500, detail=f"数据库错误：{str(e)}")
    finally:
        # 确保游标关闭，释放资源
        if cursor:
            cursor.close()


@router.get(
    "/names",
    summary="获取材料名称列表",
    description="通过论文ID查询与该论文绑定的所有材料"
)
def list_material_names(
    paper_id: int = Query(..., description="论文ID，用于筛选与该论文绑定的材料"),
    file_type: str = Query(None, description="按文件类型筛选：document/essay", enum=["document", "essay"]),
    keyword: str = Query(None, description="按文件名关键词模糊筛选"),
    db: pymysql.connections.Connection = Depends(get_db)
):
    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        # 构建基础查询SQL
        query_sql = """
            SELECT id, name, filename, file_type, upload_time, version, paper_id, storage_path 
            FROM file_records 
            WHERE paper_id = %s
        """
        query_params = [paper_id]
        # 动态添加筛选条件
        if file_type:
            query_sql += " AND file_type = %s"
            query_params.append(file_type)  # 文件类型精准匹配
        if keyword:
            query_sql += " AND filename LIKE %s"
            query_params.append(f"%{keyword}%")  # 文件名关键词模糊匹配
        # 按上传时间倒序排列
        query_sql += " ORDER BY upload_time DESC"
        # 执行查询
        cursor.execute(query_sql, tuple(query_params))
        records = cursor.fetchall()
        # 提取文件名列表
        file_list = [
            {
                "id": record["id"],
                "uploader_name": record["name"],
                "filename": record["filename"],
                "file_type": record["file_type"],
                "upload_time": record["upload_time"],
                "version": record["version"],
                "paper_id": record.get("paper_id"),
                "storage_path": record["storage_path"]  # 新增返回存储路径
            }
            for record in records
        ]
        return {
            "total": len(file_list),
            "filter_conditions": {
                "paper_id": paper_id,
                "file_type": file_type,
                "filename_keyword": keyword
            },
            "files": file_list
        }
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库查询错误：{str(e)}")
    finally:
        if cursor:
            cursor.close()


def _parse_file_ids(file_ids_str: str) -> list[int]:
    """解析文件ID列表"""
    file_ids = []
    for id_str in file_ids_str.split(","):
        id_str = id_str.strip()
        if id_str:
            try:
                file_ids.append(int(id_str))
            except ValueError:
                pass
    return file_ids


def _get_files_by_ids(cursor, file_ids: list[int]) -> list[dict]:
    """根据文件ID列表获取文件信息"""
    if not file_ids:
        return []
    
    # 构建SQL查询
    placeholders = ', '.join(['%s'] * len(file_ids))
    sql = f"""
    SELECT
        id as file_id,
        name as uploader_name,
        filename,
        storage_path
    FROM
        file_records
    WHERE
        id IN ({placeholders})
    ORDER BY
        upload_time DESC
    """
    
    cursor.execute(sql, file_ids)
    rows = cursor.fetchall()
    
    files = []
    for row in rows:
        files.append({
            "file_id": row.get('file_id'),
            "uploader_name": row.get('uploader_name'),
            "filename": row.get('filename'),
            "storage_path": row.get('storage_path')
        })
    
    return files


@router.post(
    "/download",
    summary="下载附件",
    description="支持全选和手选下载附件，打包为zip格式"
)
async def download_attachments(
    mode: str = Query(..., description="下载模式：all（全选）或selected（手选）", enum=["all", "selected"]),
    file_ids: Optional[str] = Query(None, description="附件ID列表，用英文逗号分隔，例如: 1,2,3,4,5（手选模式时必填）"),
    db: pymysql.connections.Connection = Depends(get_db)
):
    """下载附件的实现，支持全选和手选"""
    # 验证参数
    if mode == "selected" and not file_ids:
        raise HTTPException(status_code=400, detail="手选模式时必须提供附件ID列表")

    cursor = None
    try:
        cursor = db.cursor(pymysql.cursors.DictCursor)
        attachments = []
        
        if mode == "all":
            # 全选模式：获取所有附件
            sql = """
            SELECT
                id as file_id,
                name as uploader_name,
                filename,
                storage_path
            FROM
                file_records
            ORDER BY
                upload_time DESC
            """
            
            cursor.execute(sql)
            rows = cursor.fetchall()
            
            if not rows:
                raise HTTPException(status_code=404, detail="未找到附件")
            
            # 构建附件列表
            for row in rows:
                attachments.append({
                    "file_id": row.get('file_id'),
                    "uploader_name": row.get('uploader_name'),
                    "filename": row.get('filename'),
                    "storage_path": row.get('storage_path')
                })
        else:
            # 手选模式：通过file_ids获取指定附件
            file_id_list = _parse_file_ids(file_ids)
            if not file_id_list:
                raise HTTPException(status_code=400, detail="请提供有效的附件ID列表")
            
            # 获取附件信息
            attachments = _get_files_by_ids(cursor, file_id_list)
            if not attachments:
                raise HTTPException(status_code=404, detail="未找到指定的附件")
        
        # 创建内存中的 zip 文件
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for attachment in attachments:
                storage_path = attachment.get('storage_path')
                if storage_path:
                    try:
                        # 从 OSS 获取文件
                        filename, content = get_file_from_oss(storage_path)
                        # 构建文件路径，包含上传者信息
                        uploader_info = f"{attachment.get('uploader_name')}"
                        zip_file.writestr(f"{uploader_info}/{filename}", content)
                    except Exception as e:
                        # 跳过失败的文件，继续处理其他文件
                        pass
        
        # 重置文件指针到开始位置
        zip_buffer.seek(0)
        
        # 返回流式响应
        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename=attachments_{datetime.now().strftime('%Y%m%d%H%M%S')}.zip"
            }
        )
    except HTTPException:
        raise
    except pymysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"数据库错误：{str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"下载失败：{str(e)}")
    finally:
        if cursor:
            cursor.close()
