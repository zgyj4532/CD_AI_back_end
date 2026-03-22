#!/usr/bin/env python3
"""定时清理过期DDL任务"""

import os
import sys
from datetime import datetime, timedelta

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.database import get_connection
from loguru import logger


def cleanup_expired_ddl():
    """清理过期的DDL（截止日期的后一天）"""
    logger.info("开始执行DDL清理任务")
    
    conn = None
    cursor = None
    try:
        # 获取数据库连接
        conn = get_connection()
        cursor = conn.cursor()
        
        # 计算截止日期：当前日期的前一天（即截止日期的后一天）
        cutoff_date = datetime.now() - timedelta(days=1)
        cutoff_date_str = cutoff_date.strftime("%Y-%m-%d 23:59:59")
        
        logger.info(f"清理截止日期：{cutoff_date_str}")
        
        # 1. 查询过期的DDL
        cursor.execute(
            "SELECT ddlid, teacher_id, teacher_name, ddl_time FROM ddl_management WHERE ddl_time <= %s",
            (cutoff_date_str,)
        )
        expired_ddls = cursor.fetchall()
        
        if not expired_ddls:
            logger.info("没有过期的DDL需要清理")
            return
        
        logger.info(f"发现 {len(expired_ddls)} 个过期的DDL")
        
        # 2. 为每个过期的DDL执行清理
        for ddlid, teacher_id, teacher_name, ddl_time in expired_ddls:
            try:
                # 开始事务
                conn.begin()
                
                # 获取该DDL的截止时间字符串，用于匹配消息
                ddl_time_str = ddl_time.strftime('%Y-%m-%d %H:%M:%S') if ddl_time else None
                
                # a. 删除与该DDL相关的消息
                # 方式1：通过metadata中的ddlid匹配
                deleted_messages = 0
                if ddlid:
                    cursor.execute(
                        "DELETE FROM user_messages WHERE source = 'ddl' AND metadata LIKE %s",
                        (f'%\"ddlid\": {ddlid}%',)
                    )
                    deleted_messages = cursor.rowcount
                
                # 方式2：如果metadata方式没找到，尝试通过消息内容匹配
                if deleted_messages == 0 and ddl_time_str:
                    cursor.execute(
                        "DELETE FROM user_messages WHERE source = 'ddl' AND content LIKE %s",
                        (f'%{ddl_time_str}%',)
                    )
                    deleted_messages = cursor.rowcount
                
                # b. 删除DDL记录
                cursor.execute(
                    "DELETE FROM ddl_management WHERE ddlid = %s",
                    (ddlid,)
                )
                deleted_ddl = cursor.rowcount
                
                # 提交事务
                conn.commit()
                
                logger.info(f"成功清理DDL {ddlid}（教师：{teacher_name}，截止时间：{ddl_time}），删除了 {deleted_messages} 条消息")
                
            except Exception as e:
                # 回滚事务
                if conn:
                    conn.rollback()
                logger.error(f"清理DDL {ddlid} 失败：{str(e)}")
                continue
    
    except Exception as e:
        logger.error(f"执行DDL清理任务失败：{str(e)}")
    finally:
        # 关闭数据库连接
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    
    logger.info("DDL清理任务执行完成")


if __name__ == "__main__":
    cleanup_expired_ddl()
