"""
操作日志记录工具
用于将所有操作记录到operation_logs表中
"""
import json
from app.database import get_db

def record_operation_log(user_id, username, operation_type, operation_path, 
                        operation_params, ip_address, status):
    """记录操作日志到数据库"""
    db = None
    cursor = None
    try:
        db = get_db()
        cursor = db.cursor()
        
        # 准备参数
        params_json = json.dumps(operation_params) if operation_params else None
        
        # 插入日志记录
        sql = """
        INSERT INTO operation_logs (
            user_id, username, operation_type, operation_path, 
            operation_params, ip_address, operation_time, status
        ) VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s)
        """
        
        cursor.execute(sql, (
            user_id, username, operation_type, operation_path, 
            params_json, ip_address, status
        ))
        db.commit()
    except Exception:
        # 日志记录失败不应影响主流程
        pass
    finally:
        if cursor:
            cursor.close()
        if db:
            db.close()