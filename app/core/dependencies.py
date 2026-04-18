"""
依赖注入
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import pymysql
from app.database import get_db
from app.core.security import decode_access_token

security = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: pymysql.connections.Connection = Depends(get_db)
):
    """
    获取当前用户
    从JWT token中解析用户信息
    """
    token = credentials.credentials
    payload = decode_access_token(token)
    
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # 这里应该使用原生 SQL 通过 `db.cursor()` 查询用户信息并返回用户对象
    return payload  # 临时返回payload，实际使用时应该返回用户对象

