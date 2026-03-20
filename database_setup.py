from __future__ import annotations

import os
from pathlib import Path
from typing import Dict
from urllib.parse import parse_qs, urlparse

import pymysql


def _load_dotenv(env_path: str = ".env") -> None:
    """Load key=value pairs from .env into environment if not already set."""
    path = Path(env_path)
    if not path.is_file():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def parse_mysql_url(url: str) -> Dict:
    parsed = urlparse(url)
    if parsed.scheme not in ("mysql", "mysql+pymysql"):
        raise ValueError("DATABASE_URL must start with mysql:// or mysql+pymysql://")

    user = parsed.username or "root"
    password = parsed.password or ""
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 3306
    db = parsed.path.lstrip("/") or None
    qs = parse_qs(parsed.query)
    charset = qs.get("charset", ["utf8mb4"])[0]

    return dict(host=host, port=port, user=user, password=password, database=db, charset=charset)


DEFAULT_DB_URL = os.getenv(
    "DATABASE_URL",
    "mysql+pymysql://root:sbtwsj1002@127.0.0.1:3306/cd_ai_db?charset=utf8mb4",
)

ACCOUNT_MAPPING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `account_mapping` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键ID',
    `virtual_account` VARCHAR(128) NOT NULL COMMENT '虚拟账号，用于映射真实账号',
    `real_user_id` BIGINT UNSIGNED NOT NULL COMMENT '真实用户ID',
    `real_user_type` ENUM('student','teacher','admin') NOT NULL COMMENT '真实用户类型',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uniq_virtual_account` (`virtual_account`),
    KEY `idx_real_user` (`real_user_id`, `real_user_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='虚拟账号映射表';
"""


SCHOOLS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `schools` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键ID',
    `school_id` BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '学校唯一标识ID（默认值0，插入时需显式赋值）',
    `school_name` VARCHAR(128) NOT NULL COMMENT '学校名称',
    `province` VARCHAR(64) DEFAULT NULL COMMENT '所属省份',
    `city` VARCHAR(64) DEFAULT NULL COMMENT '所属城市',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uniq_school_id` (`school_id`),
    KEY `idx_school_name` (`school_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='学校信息表';
"""


DEPARTMENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `departments` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键ID',
    `department_id` BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '院系唯一标识ID',
    `department_name` VARCHAR(128) NOT NULL COMMENT '院系名称',
    `school_id` BIGINT UNSIGNED NOT NULL COMMENT '所属学校ID',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uniq_department_id` (`department_id`),
    KEY `idx_department_name` (`department_name`),
    KEY `idx_department_school_id` (`school_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='院系信息表';
"""


STUDENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `students` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键ID',
    `student_id` VARCHAR(20) NOT NULL COMMENT '学号',
    `name` VARCHAR(128) NOT NULL COMMENT '姓名',
    `phone` VARCHAR(32) DEFAULT NULL COMMENT '联系电话',
    `email` VARCHAR(255) DEFAULT NULL COMMENT '邮箱',
    `password` VARCHAR(255) NOT NULL COMMENT '密码哈希',
    `school_id` BIGINT UNSIGNED NULL COMMENT '所属学校ID',
    `school_name` VARCHAR(128) NULL COMMENT '学校名称',
    `department_id` BIGINT UNSIGNED NULL COMMENT '所属院系ID',
    `department_name` VARCHAR(128) NULL COMMENT '院系名称',
    `group_id` BIGINT UNSIGNED NULL COMMENT '所属群组ID',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uniq_student_id` (`student_id`),
    KEY `idx_name` (`name`),
    KEY `idx_student_phone` (`phone`),
    KEY `idx_student_email` (`email`),
    KEY `idx_student_school_id` (`school_id`),
    KEY `idx_student_department_id` (`department_id`),
    KEY `idx_student_group_id` (`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='学生信息表';
"""

TEACHERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `teachers` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键ID',
    `teacher_id` VARCHAR(64) NOT NULL COMMENT '教师工号',
    `name` VARCHAR(128) NOT NULL COMMENT '姓名',
    `phone` VARCHAR(32) DEFAULT NULL COMMENT '联系电话',
    `email` VARCHAR(255) DEFAULT NULL COMMENT '邮箱',
    `password` VARCHAR(255) NOT NULL COMMENT '密码哈希',
    `school_id` BIGINT UNSIGNED NULL COMMENT '所属学校ID',
    `school_name` VARCHAR(128) NULL COMMENT '学校名称',
    `department_id` BIGINT UNSIGNED NULL COMMENT '所属院系ID',
    `department_name` VARCHAR(128) NULL COMMENT '院系名称',
    `group_id` BIGINT UNSIGNED NULL COMMENT '所属群组ID',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uniq_teacher_id` (`teacher_id`),
    KEY `idx_name` (`name`),
    KEY `idx_teacher_phone` (`phone`),
    KEY `idx_teacher_email` (`email`),
    KEY `idx_teacher_school_id` (`school_id`),
    KEY `idx_teacher_department_id` (`department_id`),
    KEY `idx_teacher_group_id` (`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='教师信息表';
"""

ADMINS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `admins` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键ID',
    `admin_id` VARCHAR(64) NOT NULL COMMENT '管理员账号ID',
    `name` VARCHAR(128) NOT NULL COMMENT '姓名',
    `phone` VARCHAR(32) DEFAULT NULL COMMENT '联系电话',
    `email` VARCHAR(255) DEFAULT NULL COMMENT '邮箱',
    `password` VARCHAR(255) NOT NULL COMMENT '密码哈希',
    `role` VARCHAR(64) NOT NULL DEFAULT 'admin' COMMENT '管理员角色',
    `school_id` BIGINT UNSIGNED NULL COMMENT '所属学校ID',
    `school_name` VARCHAR(128) NULL COMMENT '学校名称',
    `department_id` BIGINT UNSIGNED NULL COMMENT '所属院系ID',
    `department_name` VARCHAR(128) NULL COMMENT '院系名称',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uniq_admin_id` (`admin_id`),
    KEY `idx_name` (`name`),
    KEY `idx_admin_phone` (`phone`),
    KEY `idx_admin_email` (`email`),
    KEY `idx_role` (`role`),
    KEY `idx_admin_school_id` (`school_id`),
    KEY `idx_admin_department_id` (`department_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='管理员信息表';
"""


FILE_RECORDS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `file_records` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键ID',
    `uploader_id` VARCHAR(64) DEFAULT NULL COMMENT '上传者ID',
    `name` VARCHAR(128) NOT NULL COMMENT '上传者姓名',
    `filename` VARCHAR(255) NOT NULL COMMENT '文件名',
    `upload_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '上传时间',
    `storage_path` VARCHAR(500) NOT NULL COMMENT '文件存储地址',
    `file_type` ENUM('document', 'essay') NOT NULL DEFAULT 'document' COMMENT '文件类型：document(文档)或essay(文章)',
    `version` INT NOT NULL DEFAULT 1 COMMENT '版本号',
    `remark` TEXT COMMENT '备注',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间',
    PRIMARY KEY (`id`),
    KEY `idx_name` (`name`),
    KEY `idx_uploader_id` (`uploader_id`),
    KEY `idx_filename` (`filename`),
    KEY `idx_upload_time` (`upload_time`),
    KEY `idx_file_type` (`file_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='文件记录表';
"""


GROUPS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `groups` (
    `id` INT NOT NULL AUTO_INCREMENT COMMENT '自增ID',
    `group_id` VARCHAR(64) NOT NULL COMMENT '群组编号',
    `group_name` VARCHAR(255) NOT NULL COMMENT '群组名称',
    `teacher_id` VARCHAR(64) DEFAULT NULL COMMENT '教师工号（负责人）',
    `teacher_name` VARCHAR(128) DEFAULT NULL COMMENT '教师姓名（负责人）',
    `description` TEXT COMMENT '群组描述',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uniq_group_id` (`group_id`),
    KEY `idx_group_name` (`group_name`),
    KEY `idx_teacher_id` (`teacher_id`),
    KEY `idx_teacher_name` (`teacher_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='群组表';
"""


GROUP_MEMBERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `group_members` (
    `group_id` VARCHAR(64) NOT NULL COMMENT '群组编号',
    `member_id` BIGINT UNSIGNED NOT NULL COMMENT '成员ID',
    `member_type` ENUM('student', 'teacher', 'admin') NOT NULL COMMENT '成员类型',
    `joined_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '加入时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    `status` VARCHAR(32) NOT NULL DEFAULT 'active' COMMENT '状态（如uploaded, processing, completed等）',
    `operated_by` VARCHAR(64) DEFAULT NULL COMMENT '操作人',
    `operated_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '操作时间',
    `is_active` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否有效（用于软删除）',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
    PRIMARY KEY (`group_id`, `member_id`, `member_type`),
    KEY `idx_member_id` (`member_id`),
    KEY `idx_member_type` (`member_type`),
    KEY `idx_group_id` (`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='群组成员关系表';
"""


PAPERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `papers` (
    `id` INT NOT NULL AUTO_INCREMENT COMMENT '论文ID',
    `owner_id` INT NOT NULL COMMENT '所有者ID',
    `teacher_id` INT NOT NULL COMMENT '老师ID',
    `version` VARCHAR(20) NOT NULL COMMENT '当前版本号',
    `size` INT NOT NULL COMMENT '文件大小（字节）',
    `status` VARCHAR(32) NOT NULL COMMENT '状态（uploaded:已上传, processing:处理中, completed:完成, rejected:驳回）',
    `detail` TEXT COMMENT '状态描述',
    `ddl` DATETIME DEFAULT NULL COMMENT '截止时间',
    `oss_key` VARCHAR(128) NOT NULL COMMENT 'OSS存储键（docx文件）',
    `pdf_oss_key` VARCHAR(128) NOT NULL COMMENT 'OSS存储键（PDF文件）',
    `submitted_by_name` VARCHAR(128) DEFAULT NULL COMMENT '提交者姓名',
    `submitted_by_role` VARCHAR(64) DEFAULT NULL COMMENT '提交者角色',
    `operated_by` VARCHAR(64) DEFAULT NULL COMMENT '操作人',
    `operated_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '操作时间',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    KEY `idx_owner_id` (`owner_id`),
    KEY `idx_teacher_id` (`teacher_id`),
    KEY `idx_version` (`version`),
    KEY `idx_status` (`status`),
    KEY `idx_operated_time` (`operated_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='论文信息表';
"""


PAPERS_HISTORY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `papers_history` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '历史版本ID',
    `paper_id` INT NOT NULL COMMENT '论文ID',
    `version` VARCHAR(20) NOT NULL COMMENT '历史版本号',
    `size` INT NOT NULL COMMENT '文件大小（字节）',
    `status` VARCHAR(32) NOT NULL COMMENT '状态（如uploaded, processing, completed等）',
    `detail` TEXT COMMENT '状态描述',
    `oss_key` VARCHAR(255) NOT NULL COMMENT 'OSS存储键（docx文件）',
    `pdf_oss_key` VARCHAR(255) NOT NULL COMMENT 'OSS存储键（PDF文件）',
    `submitted_by_id` VARCHAR(64) DEFAULT NULL COMMENT '提交者ID',
    `submitted_by_name` VARCHAR(128) DEFAULT NULL COMMENT '提交者姓名',
    `submitted_by_role` VARCHAR(64) DEFAULT NULL COMMENT '提交者角色',
    `operated_by` VARCHAR(64) DEFAULT NULL COMMENT '操作人',
    `operated_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '操作时间',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间',
    PRIMARY KEY (`id`),
    KEY `idx_papers_history_paper_id` (`paper_id`),
    KEY `idx_papers_history_version` (`version`),
    KEY `idx_papers_history_status` (`status`),
    KEY `idx_papers_history_created_at` (`created_at`),
    CONSTRAINT `fk_papers_history_paper_id` FOREIGN KEY (`paper_id`) REFERENCES `papers` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='论文历史版本表';
"""


PAPER_REVIEWS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `paper_reviews` (
    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '审阅记录ID',
    paper_id INT NOT NULL COMMENT '论文ID',
    teacher_id INT NOT NULL COMMENT '教师ID',
    teacher_name VARCHAR(50) NOT NULL COMMENT '教师姓名',
    review_content TEXT NOT NULL COMMENT '审阅内容',
    review_time DATETIME NOT NULL COMMENT '审阅时间',
    updated_time DATETIME DEFAULT NULL COMMENT '更新时间',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间',
    INDEX idx_paper_id (paper_id),
    INDEX idx_teacher_id (teacher_id),
    INDEX idx_paper_teacher (paper_id, teacher_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='论文审阅内容表';
"""


ANNOTATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `annotations` (
    `id` INT NOT NULL AUTO_INCREMENT COMMENT '批注ID',
    `paper_id` INT NOT NULL COMMENT '所属论文ID',
    `author_id` INT NOT NULL COMMENT '批注作者ID',
    `author_name` VARCHAR(50) NOT NULL COMMENT '批注者姓名',
    `paragraph_id` VARCHAR(50) DEFAULT NULL COMMENT '段落ID（可选）',
    `coordinates` JSON DEFAULT NULL COMMENT '坐标信息（JSON格式）',
    `content` TEXT NOT NULL COMMENT '批注内容',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    KEY `idx_paper_id` (`paper_id`),
    KEY `idx_author_id` (`author_id`),
    CONSTRAINT `fk_annotations_paper_id` FOREIGN KEY (`paper_id`) REFERENCES `papers` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='批注表';
"""


DDL_MANAGEMENT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `ddl_management` (
    ddlid INT PRIMARY KEY AUTO_INCREMENT COMMENT 'DDL唯一ID',
    teacher_id INT NOT NULL COMMENT '教师ID）',
    teacher_name VARCHAR(50) NOT NULL COMMENT '教师姓名', 
    group_id INT NOT NULL COMMENT '群组ID',
    ddl_time DATETIME NOT NULL COMMENT '截止时间（精确到秒）',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    INDEX idx_teacher_id (teacher_id),
    INDEX idx_ddl_time (ddl_time),
    INDEX idx_teacher_name (teacher_name),
    INDEX idx_group_id (group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='论文DDL截止时间管理表';
"""


TEMPLATES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `templates` (
    `id` INT NOT NULL AUTO_INCREMENT COMMENT '模板ID',
    `template_id` VARCHAR(64) NOT NULL COMMENT '模板唯一标识',
    `oss_key` VARCHAR(255) NOT NULL COMMENT 'OSS存储键',
    `filename` VARCHAR(255) NOT NULL COMMENT '原始文件名',
    `content_type` VARCHAR(128) DEFAULT NULL COMMENT 'MIME类型',
    `uploader_id` VARCHAR(64) NOT NULL COMMENT '上传者ID',
    `upload_time` DATETIME NOT NULL COMMENT '上传时间',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uniq_template_id` (`template_id`),
    KEY `idx_template_id` (`template_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='模板表';
"""


OPERATION_LOGS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `operation_logs` (
    `id` INT NOT NULL AUTO_INCREMENT COMMENT '日志ID',
    `user_id` VARCHAR(64) NOT NULL COMMENT '操作用户ID',
    `username` VARCHAR(64) NOT NULL COMMENT '操作用户名',
    `operation_type` VARCHAR(32) NOT NULL COMMENT '操作类型（如create, update, delete）',
    `operation_path` VARCHAR(255) NOT NULL COMMENT '操作路径',
    `operation_params` JSON DEFAULT NULL COMMENT '操作参数（JSON格式）',
    `ip_address` VARCHAR(64) DEFAULT NULL COMMENT 'IP地址',
    `operation_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '操作发生时间',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间',
    `status` VARCHAR(16) NOT NULL DEFAULT 'success' COMMENT '操作状态（success/failure）',
    PRIMARY KEY (`id`),
    KEY `idx_user_id` (`user_id`),
    KEY `idx_operation_time` (`operation_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='操作日志表';
"""


USER_MESSAGES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS `user_messages` (
    `id` INT NOT NULL AUTO_INCREMENT COMMENT '消息ID',
    `user_id` VARCHAR(64) NOT NULL COMMENT '接收用户ID',
    `username` VARCHAR(64) DEFAULT NULL COMMENT '接收用户名',
    `title` VARCHAR(255) NOT NULL COMMENT '消息标题',
    `content` TEXT NOT NULL COMMENT '消息内容',
    `source` VARCHAR(64) DEFAULT NULL COMMENT '来源（系统/业务模块）',
    `status` VARCHAR(16) NOT NULL DEFAULT 'unread' COMMENT '状态（unread/read）',
    `received_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '接收时间',
    `metadata` JSON DEFAULT NULL COMMENT '扩展元数据',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间',
    PRIMARY KEY (`id`),
    KEY `idx_user_messages_user_id` (`user_id`),
    KEY `idx_user_messages_status` (`status`),
    KEY `idx_user_messages_received_time` (`received_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户信息记录表（记录用户接收到的消息）';
"""


def init_db(database_url: str | None = None) -> None:
    """Create base tables if missing (one-time use)."""
    url = database_url or DEFAULT_DB_URL
    params = parse_mysql_url(url)

    conn = pymysql.connect(
        host=params["host"],
        port=params["port"],
        user=params["user"],
        password=params["password"],
        database=params["database"],
        charset=params.get("charset", "utf8mb4"),
        autocommit=True,
    )

    try:
        with conn.cursor() as cur:
            for sql in (
                SCHOOLS_TABLE_SQL,
                DEPARTMENTS_TABLE_SQL,
                STUDENTS_TABLE_SQL,
                TEACHERS_TABLE_SQL,
                ADMINS_TABLE_SQL,
                FILE_RECORDS_TABLE_SQL,
                GROUPS_TABLE_SQL,
                GROUP_MEMBERS_TABLE_SQL,
                PAPERS_TABLE_SQL,
                PAPERS_HISTORY_TABLE_SQL,
                PAPER_REVIEWS_TABLE_SQL,
                ANNOTATIONS_TABLE_SQL,
                DDL_MANAGEMENT_TABLE_SQL,
                TEMPLATES_TABLE_SQL,
                USER_MESSAGES_TABLE_SQL,
                OPERATION_LOGS_TABLE_SQL,
            ):
                cur.execute(sql)
        print(
            "Tables ensured: schools, departments, students, teachers, admins, file_records, groups, group_members, "
            "papers, papers_history, paper_reviews, annotations, ddl_management, templates, "
            "user_messages, operation_logs"
        )
    finally:
        conn.close()


def _get_existing_columns(conn: pymysql.connections.Connection, db_name: str, table: str) -> set:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
            (db_name, table),
        )
        return {row[0] for row in cur.fetchall()}


def _get_existing_indexes(conn: pymysql.connections.Connection, db_name: str, table: str) -> set:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT INDEX_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
            (db_name, table),
        )
        return {row[0] for row in cur.fetchall()}


TABLE_COLUMN_DEFINITIONS = {
    "schools": {
        "id": "`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键ID'",
        "school_id": "`school_id` BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '学校唯一标识ID（默认值0，插入时需显式赋值）'",
        "school_name": "`school_name` VARCHAR(128) NOT NULL COMMENT '学校名称'",
        "province": "`province` VARCHAR(64) DEFAULT NULL COMMENT '所属省份'",
        "city": "`city` VARCHAR(64) DEFAULT NULL COMMENT '所属城市'",
        "created_at": "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间'",
        "updated_at": "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间'",
    },
    "departments": {
        "id": "`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键ID'",
        "department_id": "`department_id` BIGINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '院系唯一标识ID'",
        "department_name": "`department_name` VARCHAR(128) NOT NULL COMMENT '院系名称'",
        "school_id": "`school_id` BIGINT UNSIGNED NOT NULL COMMENT '所属学校ID'",
        "created_at": "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间'",
        "updated_at": "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间'",
    },
    "students": {
        "id": "`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键ID'",
        "student_id": "`student_id` VARCHAR(20) NOT NULL COMMENT '学号'",
        "name": "`name` VARCHAR(128) NOT NULL COMMENT '姓名'",
        "phone": "`phone` VARCHAR(32) DEFAULT NULL COMMENT '联系电话'",
        "email": "`email` VARCHAR(255) DEFAULT NULL COMMENT '邮箱'",
        "password": "`password` VARCHAR(255) NOT NULL COMMENT '密码哈希'",
        "school_id": "`school_id` BIGINT UNSIGNED NULL COMMENT '所属学校ID'",
        "school_name": "`school_name` VARCHAR(128) NULL COMMENT '学校名称'",
        "department_id": "`department_id` BIGINT UNSIGNED NULL COMMENT '所属院系ID'",
        "department_name": "`department_name` VARCHAR(128) NULL COMMENT '院系名称'",
        "group_id": "`group_id` BIGINT UNSIGNED NULL COMMENT '所属群组ID'",
        "created_at": "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间'",
        "updated_at": "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间'",
    },
    "teachers": {
        "id": "`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键ID'",
        "teacher_id": "`teacher_id` VARCHAR(64) NOT NULL COMMENT '教师工号'",
        "name": "`name` VARCHAR(128) NOT NULL COMMENT '姓名'",
        "phone": "`phone` VARCHAR(32) DEFAULT NULL COMMENT '联系电话'",
        "email": "`email` VARCHAR(255) DEFAULT NULL COMMENT '邮箱'",
        "password": "`password` VARCHAR(255) NOT NULL COMMENT '密码哈希'",
        "school_id": "`school_id` BIGINT UNSIGNED NULL COMMENT '所属学校ID'",
        "school_name": "`school_name` VARCHAR(128) NULL COMMENT '学校名称'",
        "department_id": "`department_id` BIGINT UNSIGNED NULL COMMENT '所属院系ID'",
        "department_name": "`department_name` VARCHAR(128) NULL COMMENT '院系名称'",
        "group_id": "`group_id` BIGINT UNSIGNED NULL COMMENT '所属群组ID'",
        "created_at": "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间'",
        "updated_at": "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间'",
    },
    "admins": {
        "id": "`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键ID'",
        "admin_id": "`admin_id` VARCHAR(64) NOT NULL COMMENT '管理员账号ID'",
        "name": "`name` VARCHAR(128) NOT NULL COMMENT '姓名'",
        "phone": "`phone` VARCHAR(32) DEFAULT NULL COMMENT '联系电话'",
        "email": "`email` VARCHAR(255) DEFAULT NULL COMMENT '邮箱'",
        "password": "`password` VARCHAR(255) NOT NULL COMMENT '密码哈希'",
        "role": "`role` VARCHAR(64) NOT NULL DEFAULT 'admin' COMMENT '管理员角色'",
        "school_id": "`school_id` BIGINT UNSIGNED NULL COMMENT '所属学校ID'",
        "school_name": "`school_name` VARCHAR(128) NULL COMMENT '学校名称'",
        "department_id": "`department_id` BIGINT UNSIGNED NULL COMMENT '所属院系ID'",
        "department_name": "`department_name` VARCHAR(128) NULL COMMENT '院系名称'",
        "created_at": "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间'",
        "updated_at": "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间'",
    },
    "file_records": {
        "id": "`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键ID'",
        "uploader_id": "`uploader_id` VARCHAR(64) DEFAULT NULL COMMENT '上传者ID'",
        "name": "`name` VARCHAR(128) NOT NULL COMMENT '上传者姓名'",
        "filename": "`filename` VARCHAR(255) NOT NULL COMMENT '文件名'",
        "upload_time": "`upload_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '上传时间'",
        "storage_path": "`storage_path` VARCHAR(500) NOT NULL COMMENT '文件存储地址'",
        "file_type": "`file_type` ENUM('document', 'essay') NOT NULL DEFAULT 'document' COMMENT '文件类型：document(文档)或essay(文章)'",
        "version": "`version` INT NOT NULL DEFAULT 1 COMMENT '版本号'",
        "remark": "`remark` TEXT COMMENT '备注'",
        "created_at": "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间'",
        "updated_at": "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间'",
    },
    "groups": {
        "id": "`id` INT NOT NULL AUTO_INCREMENT COMMENT '自增ID'",
        "group_id": "`group_id` VARCHAR(64) NOT NULL COMMENT '群组编号'",
        "group_name": "`group_name` VARCHAR(255) NOT NULL COMMENT '群组名称'",
        "teacher_id": "`teacher_id` VARCHAR(64) DEFAULT NULL COMMENT '教师工号（负责人）'",
        "teacher_name": "`teacher_name` VARCHAR(128) DEFAULT NULL COMMENT '教师姓名（负责人）'",
        "description": "`description` TEXT COMMENT '群组描述'",
        "created_at": "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'",
        "updated_at": "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'",
    },
    "group_members": {
        "group_id": "`group_id` VARCHAR(64) NOT NULL COMMENT '群组编号'",
        "member_id": "`member_id` BIGINT UNSIGNED NOT NULL COMMENT '成员ID'",
        "member_type": "`member_type` ENUM('student', 'teacher', 'admin') NOT NULL COMMENT '成员类型'",
        "joined_at": "`joined_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '加入时间'",
        "updated_at": "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'",
        "status": "`status` VARCHAR(32) NOT NULL DEFAULT 'active' COMMENT '状态（如uploaded, processing, completed等）'",
        "operated_by": "`operated_by` VARCHAR(64) DEFAULT NULL COMMENT '操作人'",
        "operated_time": "`operated_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '操作时间'",
        "is_active": "`is_active` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否有效（用于软删除）'",
        "created_at": "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间'",
    },
    "papers": {
        "id": "`id` INT NOT NULL AUTO_INCREMENT COMMENT '论文ID'",
        "owner_id": "`owner_id` INT NOT NULL COMMENT '所有者ID'",
        "teacher_id": "`teacher_id` INT NOT NULL COMMENT '老师ID'",
        "teacher_name": "`teacher_name` VARCHAR(128) NOT NULL COMMENT '老师姓名'",
        "version": "`version` VARCHAR(20) NOT NULL COMMENT '当前版本号'",
        "size": "`size` INT NOT NULL COMMENT '文件大小（字节）'",
        "status": "`status` VARCHAR(32) NOT NULL COMMENT '状态（uploaded:已上传, processing:处理中, completed:完成, rejected:驳回）'",
        "detail": "`detail` TEXT COMMENT '状态描述'",
        "ddl": "`ddl` DATETIME DEFAULT NULL COMMENT '截止时间'",
        "oss_key": "`oss_key` VARCHAR(128) NOT NULL COMMENT 'OSS存储键（docx文件）'",
        "pdf_oss_key": "`pdf_oss_key` VARCHAR(128) NOT NULL COMMENT 'OSS存储键（PDF文件）'",
        "submitted_by_name": "`submitted_by_name` VARCHAR(128) DEFAULT NULL COMMENT '提交者姓名'",
        "submitted_by_role": "`submitted_by_role` VARCHAR(64) DEFAULT NULL COMMENT '提交者角色'",
        "operated_by": "`operated_by` VARCHAR(64) DEFAULT NULL COMMENT '操作人'",
        "operated_time": "`operated_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '操作时间'",
        "created_at": "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'",
        "updated_at": "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'",
    },
    "papers_history": {
        "id": "`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '历史版本ID'",
        "paper_id": "`paper_id` INT NOT NULL COMMENT '论文ID'",
        "teacher_name": "`teacher_name` VARCHAR(128) NOT NULL COMMENT '老师姓名'",
        "version": "`version` VARCHAR(20) NOT NULL COMMENT '历史版本号'",
        "size": "`size` INT NOT NULL COMMENT '文件大小（字节）'",
        "status": "`status` VARCHAR(32) NOT NULL COMMENT '状态（如uploaded, processing, completed等）'",
        "detail": "`detail` TEXT COMMENT '状态描述'",
        "oss_key": "`oss_key` VARCHAR(255) NOT NULL COMMENT 'OSS存储键（docx文件）'",
        "pdf_oss_key": "`pdf_oss_key` VARCHAR(255) NOT NULL COMMENT 'OSS存储键（PDF文件）'",
        "submitted_by_id": "`submitted_by_id` VARCHAR(64) DEFAULT NULL COMMENT '提交者ID'",
        "submitted_by_name": "`submitted_by_name` VARCHAR(128) DEFAULT NULL COMMENT '提交者姓名'",
        "submitted_by_role": "`submitted_by_role` VARCHAR(64) DEFAULT NULL COMMENT '提交者角色'",
        "operated_by": "`operated_by` VARCHAR(64) DEFAULT NULL COMMENT '操作人'",
        "operated_time": "`operated_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '操作时间'",
        "created_at": "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间'",
        "updated_at": "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间'",
    },
    "paper_reviews": {
        "id": "`id` INT AUTO_INCREMENT COMMENT '审阅记录ID'",
        "paper_id": "`paper_id` INT NOT NULL COMMENT '论文ID'",
        "teacher_id": "`teacher_id` INT NOT NULL COMMENT '教师ID'",
        "teacher_name": "`teacher_name` VARCHAR(50) NOT NULL COMMENT '教师姓名'",
        "review_content": "`review_content` TEXT NOT NULL COMMENT '审阅内容'",
        "review_time": "`review_time` DATETIME NOT NULL COMMENT '审阅时间'",
        "updated_time": "`updated_time` DATETIME DEFAULT NULL COMMENT '更新时间'",
        "created_at": "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间'",
        "updated_at": "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间'",
    },
    "annotations": {
        "id": "`id` INT NOT NULL AUTO_INCREMENT COMMENT '批注ID'",
        "paper_id": "`paper_id` INT NOT NULL COMMENT '所属论文ID'",
        "author_id": "`author_id` INT NOT NULL COMMENT '批注作者ID'",
        "author_name": "`author_name` VARCHAR(50) NOT NULL COMMENT '批注者姓名'",
        "paragraph_id": "`paragraph_id` VARCHAR(50) DEFAULT NULL COMMENT '段落ID（可选）'",
        "coordinates": "`coordinates` JSON DEFAULT NULL COMMENT '坐标信息（JSON格式）'",
        "content": "`content` TEXT NOT NULL COMMENT '批注内容'",
        "created_at": "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'",
        "updated_at": "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'",
    },
    "ddl_management": {
        "ddlid": "`ddlid` INT AUTO_INCREMENT COMMENT 'DDL唯一ID'",
        "teacher_id": "`teacher_id` INT NOT NULL COMMENT '教师ID）'",
        "teacher_name": "`teacher_name` VARCHAR(50) NOT NULL COMMENT '教师姓名'",
        "group_id": "`group_id` INT NOT NULL COMMENT '群组ID'",
        "ddl_time": "`ddl_time` DATETIME NOT NULL COMMENT '截止时间（精确到秒）'",
        "created_at": "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'",
        "updated_at": "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间'",
    },
    "templates": {
        "id": "`id` INT NOT NULL AUTO_INCREMENT COMMENT '模板ID'",
        "template_id": "`template_id` VARCHAR(64) NOT NULL COMMENT '模板唯一标识'",
        "oss_key": "`oss_key` VARCHAR(255) NOT NULL COMMENT 'OSS存储键'",
        "filename": "`filename` VARCHAR(255) NOT NULL COMMENT '原始文件名'",
        "content_type": "`content_type` VARCHAR(128) DEFAULT NULL COMMENT 'MIME类型'",
        "uploader_id": "`uploader_id` VARCHAR(64) NOT NULL COMMENT '上传者ID'",
        "upload_time": "`upload_time` DATETIME NOT NULL COMMENT '上传时间'",
        "created_at": "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间'",
        "updated_at": "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间'",
    },
    "user_messages": {
        "id": "`id` INT NOT NULL AUTO_INCREMENT COMMENT '消息ID'",
        "user_id": "`user_id` VARCHAR(64) NOT NULL COMMENT '接收用户ID'",
        "username": "`username` VARCHAR(64) DEFAULT NULL COMMENT '接收用户名'",
        "title": "`title` VARCHAR(255) NOT NULL COMMENT '消息标题'",
        "content": "`content` TEXT NOT NULL COMMENT '消息内容'",
        "source": "`source` VARCHAR(64) DEFAULT NULL COMMENT '来源（系统/业务模块）'",
        "status": "`status` VARCHAR(16) NOT NULL DEFAULT 'unread' COMMENT '状态（unread/read）'",
        "received_time": "`received_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '接收时间'",
        "metadata": "`metadata` JSON DEFAULT NULL COMMENT '扩展元数据'",
        "created_at": "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间'",
        "updated_at": "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间'",
    },
    "operation_logs": {
        "id": "`id` INT NOT NULL AUTO_INCREMENT COMMENT '日志ID'",
        "user_id": "`user_id` VARCHAR(64) NOT NULL COMMENT '操作用户ID'",
        "username": "`username` VARCHAR(64) NOT NULL COMMENT '操作用户名'",
        "operation_type": "`operation_type` VARCHAR(32) NOT NULL COMMENT '操作类型（如create, update, delete）'",
        "operation_path": "`operation_path` VARCHAR(255) NOT NULL COMMENT '操作路径'",
        "operation_params": "`operation_params` JSON DEFAULT NULL COMMENT '操作参数（JSON格式）'",
        "ip_address": "`ip_address` VARCHAR(64) DEFAULT NULL COMMENT 'IP地址'",
        "operation_time": "`operation_time` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '操作发生时间'",
        "created_at": "`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间'",
        "updated_at": "`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '记录更新时间'",
        "status": "`status` VARCHAR(16) NOT NULL DEFAULT 'success' COMMENT '操作状态（success/failure）'",
    },
}

TABLE_INDEX_DEFINITIONS = {
    "schools": [
        "CREATE UNIQUE INDEX uniq_school_id ON `schools` (school_id)",
        "CREATE INDEX idx_school_name ON `schools` (school_name)"
    ],
    "departments": [
        "CREATE UNIQUE INDEX uniq_department_id ON `departments` (department_id)",
        "CREATE INDEX idx_department_name ON `departments` (department_name)",
        "CREATE INDEX idx_department_school_id ON `departments` (school_id)"
    ],
    "students": [
        "CREATE UNIQUE INDEX uniq_student_id ON `students` (student_id)",
        "CREATE INDEX idx_name ON `students` (name)",
        "CREATE INDEX idx_student_phone ON `students` (phone)",
        "CREATE INDEX idx_student_email ON `students` (email)",
        "CREATE INDEX idx_student_school_id ON `students` (school_id)",
        "CREATE INDEX idx_student_department_id ON `students` (department_id)",
        "CREATE INDEX idx_student_group_id ON `students` (group_id)"
    ],
    "teachers": [
        "CREATE UNIQUE INDEX uniq_teacher_id ON `teachers` (teacher_id)",
        "CREATE INDEX idx_name ON `teachers` (name)",
        "CREATE INDEX idx_teacher_phone ON `teachers` (phone)",
        "CREATE INDEX idx_teacher_email ON `teachers` (email)",
        "CREATE INDEX idx_teacher_school_id ON `teachers` (school_id)",
        "CREATE INDEX idx_teacher_department_id ON `teachers` (department_id)",
        "CREATE INDEX idx_teacher_group_id ON `teachers` (group_id)"
    ],
    "admins": [
        "CREATE UNIQUE INDEX uniq_admin_id ON `admins` (admin_id)",
        "CREATE INDEX idx_name ON `admins` (name)",
        "CREATE INDEX idx_admin_phone ON `admins` (phone)",
        "CREATE INDEX idx_admin_email ON `admins` (email)",
        "CREATE INDEX idx_role ON `admins` (role)",
        "CREATE INDEX idx_admin_school_id ON `admins` (school_id)",
        "CREATE INDEX idx_admin_department_id ON `admins` (department_id)"
    ],
    "file_records": [
        "CREATE INDEX idx_name ON `file_records` (name)",
        "CREATE INDEX idx_uploader_id ON `file_records` (uploader_id)",
        "CREATE INDEX idx_filename ON `file_records` (filename)",
        "CREATE INDEX idx_upload_time ON `file_records` (upload_time)",
        "CREATE INDEX idx_file_type ON `file_records` (file_type)"
    ],
    "groups": [
        "CREATE UNIQUE INDEX uniq_group_id ON `groups` (group_id)",
        "CREATE INDEX idx_group_name ON `groups` (group_name)",
        "CREATE INDEX idx_teacher_id ON `groups` (teacher_id)",
        "CREATE INDEX idx_teacher_name ON `groups` (teacher_name)"
    ],
    "group_members": [
        "CREATE INDEX idx_member_id ON `group_members` (member_id)",
        "CREATE INDEX idx_member_type ON `group_members` (member_type)",
        "CREATE INDEX idx_group_id ON `group_members` (group_id)"
    ],
    "papers": [
        "CREATE INDEX idx_owner_id ON `papers` (owner_id)",
        "CREATE INDEX idx_teacher_id ON `papers` (teacher_id)",
        "CREATE INDEX idx_teacher_name ON `papers` (teacher_name)",
        "CREATE INDEX idx_version ON `papers` (version)",
        "CREATE INDEX idx_status ON `papers` (status)",
        "CREATE INDEX idx_operated_time ON `papers` (operated_time)"
    ],
    "papers_history": [
        "CREATE INDEX idx_papers_history_paper_id ON `papers_history` (paper_id)",
        "CREATE INDEX idx_papers_history_teacher_name ON `papers_history` (teacher_name)",
        "CREATE INDEX idx_papers_history_version ON `papers_history` (version)",
        "CREATE INDEX idx_papers_history_status ON `papers_history` (status)",
        "CREATE INDEX idx_papers_history_created_at ON `papers_history` (created_at)"
    ],
    "paper_reviews": [
        "CREATE INDEX idx_paper_id ON `paper_reviews` (paper_id)",
        "CREATE INDEX idx_teacher_id ON `paper_reviews` (teacher_id)",
        "CREATE INDEX idx_paper_teacher ON `paper_reviews` (paper_id, teacher_id)"
    ],
    "annotations": [
        "CREATE INDEX idx_annotations_paper_id ON `annotations` (paper_id)",
        "CREATE INDEX idx_annotations_author_id ON `annotations` (author_id)"
    ],
    "ddl_management": [
        "CREATE INDEX idx_teacher_id ON `ddl_management` (teacher_id)",
        "CREATE INDEX idx_ddl_time ON `ddl_management` (ddl_time)",
        "CREATE INDEX idx_teacher_name ON `ddl_management` (teacher_name)",
        "CREATE INDEX idx_group_id ON `ddl_management` (group_id)"
    ],
    "templates": [
        "CREATE UNIQUE INDEX uniq_template_id ON `templates` (template_id)",
        "CREATE INDEX idx_template_id ON `templates` (template_id)"
    ],
    "user_messages": [
        "CREATE INDEX idx_user_messages_user_id ON `user_messages` (user_id)",
        "CREATE INDEX idx_user_messages_status ON `user_messages` (status)",
        "CREATE INDEX idx_user_messages_received_time ON `user_messages` (received_time)"
    ],
    "operation_logs": [
        "CREATE INDEX idx_operation_logs_user_id ON `operation_logs` (user_id)",
        "CREATE INDEX idx_operation_logs_time ON `operation_logs` (operation_time)"
    ],
}


def sync_schema(database_url: str | None = None) -> None:
    """Ensure tables exist and add missing columns/indexes dynamically."""
    url = database_url or DEFAULT_DB_URL
    params = parse_mysql_url(url)

    conn = pymysql.connect(
        host=params["host"],
        port=params["port"],
        user=params["user"],
        password=params["password"],
        database=params["database"],
        charset=params.get("charset", "utf8mb4"),
        autocommit=True,
    )

    try:
        # Create base tables if missing
        with conn.cursor() as cur:
            for sql in (
                SCHOOLS_TABLE_SQL,
                DEPARTMENTS_TABLE_SQL,
                STUDENTS_TABLE_SQL,
                TEACHERS_TABLE_SQL,
                ADMINS_TABLE_SQL,
                FILE_RECORDS_TABLE_SQL,
                GROUPS_TABLE_SQL,
                GROUP_MEMBERS_TABLE_SQL,
                PAPERS_TABLE_SQL,
                PAPERS_HISTORY_TABLE_SQL,
                PAPER_REVIEWS_TABLE_SQL,
                ANNOTATIONS_TABLE_SQL,
                DDL_MANAGEMENT_TABLE_SQL,
                TEMPLATES_TABLE_SQL,
                USER_MESSAGES_TABLE_SQL,
                OPERATION_LOGS_TABLE_SQL,
            ):
                cur.execute(sql)

        db_name = params["database"]

        # Add missing columns
        for table, cols in TABLE_COLUMN_DEFINITIONS.items():
            existing = _get_existing_columns(conn, db_name, table)
            for col_name, col_def in cols.items():
                if col_name not in existing:
                    stmt = f"ALTER TABLE `{table}` ADD COLUMN {col_def};"
                    with conn.cursor() as cur:
                        cur.execute(stmt)

        # Align group_members column definitions (including defaults/comments)
        for col_def in TABLE_COLUMN_DEFINITIONS.get("group_members", {}).values():
            with conn.cursor() as cur:
                cur.execute(f"ALTER TABLE `group_members` MODIFY COLUMN {col_def};")

        # Align papers and papers_history timestamp defaults
        for table in ("papers", "papers_history"):
            for col_name in ("created_at", "updated_at"):
                col_def = TABLE_COLUMN_DEFINITIONS.get(table, {}).get(col_name)
                if col_def:
                    with conn.cursor() as cur:
                        cur.execute(f"ALTER TABLE `{table}` MODIFY COLUMN {col_def};")

        # Ensure enum definition for group_members.role includes owner
        with conn.cursor() as cur:
            cur.execute(
                "SHOW COLUMNS FROM `group_members` LIKE 'role'"
            )
            row = cur.fetchone()
            role_type = None
            if row:
                # row can be tuple or dict
                if isinstance(row, dict):
                    role_type = row.get("Type") or row.get("type")
                else:
                    # SHOW COLUMNS returns: Field, Type, Null, Key, Default, Extra
                    role_type = row[1] if len(row) > 1 else None
            if role_type and "enum" in role_type.lower() and "owner" not in role_type.lower():
                cur.execute(
                    "ALTER TABLE `group_members` MODIFY COLUMN `role` ENUM('member','admin','owner') NOT NULL DEFAULT 'member'"
                )

        # Ensure indexes
        for table, idx_list in TABLE_INDEX_DEFINITIONS.items():
            existing_idx = _get_existing_indexes(conn, db_name, table)
            for idx_sql in idx_list:
                parts = idx_sql.split()
                idx_name = None
                try:
                    if "UNIQUE" in parts:
                        idx_name = parts[3]
                    else:
                        idx_name = parts[2]
                except Exception:
                    continue

                if idx_name and idx_name in existing_idx:
                    continue

                with conn.cursor() as cur:
                    try:
                        cur.execute(idx_sql)
                    except pymysql.err.InternalError:
                        cur.execute(idx_sql)

        print("Schema synchronized (added missing columns/indexes if any).")
    finally:
        conn.close()


if __name__ == "__main__":
    sync_schema()
