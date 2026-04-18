import pymysql
from pymysql import MySQLError
from typing import Optional
from app.models.document import DocumentRecord


class DocumentService:
    def __init__(self, db: pymysql.connections.Connection):
        self.db = db

    def create(self, filename: str, content: bytes, content_type: Optional[str] = None) -> DocumentRecord:
        sql = """
        INSERT INTO `documents` (filename, content, content_type, created_at)
        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
        """
        with self.db.cursor() as cur:
            cur.execute(sql, (filename, content, content_type))
            doc_id = cur.lastrowid
            cur.execute("SELECT id, filename, content, content_type, created_at FROM documents WHERE id = %s", (doc_id,))
            row = cur.fetchone()
        try:
            self.db.commit()
        except MySQLError as exc:
            self.db.rollback()
            raise RuntimeError("Failed to commit document creation") from exc

        if not row:
            raise RuntimeError("Failed to create document")

        return DocumentRecord(id=row[0], filename=row[1], content=row[2], content_type=row[3], created_at=row[4])

    def get_by_id(self, document_id: int) -> Optional[DocumentRecord]:
        sql = "SELECT id, filename, content, content_type, created_at FROM documents WHERE id = %s"
        with self.db.cursor() as cur:
            cur.execute(sql, (document_id,))
            row = cur.fetchone()
        if not row:
            return None
        return DocumentRecord(id=row[0], filename=row[1], content=row[2], content_type=row[3], created_at=row[4])

