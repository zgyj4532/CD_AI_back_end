import time

# 内存存储，用于保存生成的AI报告
ai_reports = {}


def submit_ai_review(paper_id: int, user_payload: dict):
    """Stub: simulate submitting to AI service and storing a result.

    In real app: generate temporary OSS URL, call external AI (via requests or SDK),
    parse JSON, persist to DB and insert virtual annotations.
    """
    # simulate work / timeout behavior
    time.sleep(0.5)
    # Return a fake report (in real usage, persist to DB)
    report = {"paper_id": paper_id, "issues": []}
    # 存储报告到内存
    ai_reports[paper_id] = report
    return report


def submit_ai_review_file(file_bytes: bytes, filename: str, user_payload: dict):
    """Stub: 直接处理上传文件的 AI 评审。

    参数:
      file_bytes: 文件内容
      filename: 原始文件名
      user_payload: 当前用户信息
    """
    time.sleep(0.5)
    # 真实场景中：调用外部AI审查接口，返回结构化问题列表或评分
    report = {
        "filename": filename,
        "length": len(file_bytes),
        "issues": [],
        "message": "快速审稿完成（模拟数据）",
    }
    # 从用户信息中获取paper_id
    paper_id = user_payload.get("paper_id")
    if paper_id:
        # 存储报告到内存
        ai_reports[paper_id] = report
    return report


def get_ai_report_by_paper_id(paper_id: int):
    """Stub: 根据 paper_id 返回历史审查报告。"""
    # 检查内存中是否有报告
    if paper_id in ai_reports:
        return {
            "task_id": f"paper_{paper_id}",
            "status": "COMPLETED",
            "summary": {
                "total_issues": 0,
                "score": 85.0,
                "mode": "AI_REVIEW",
                "message": "AI 审查完成。",
            },
            "issues": [],
            **ai_reports[paper_id]
        }
    
    # 这里根据你的文件 JSON 生成一个固定返回值，使用时可扩展入 DB
    if paper_id == 18:
        return {
            "task_id": "18通信2_李良循_毕业论文.docx",
            "status": "COMPLETED",
            "summary": {
                "total_issues": 150,
                "score": 0.0,
                "mode": "EMERGENCY_AI_ONLY",
                "message": "当前处于快速 AI 审查模式，物理格式检查（字号/行距）已跳过。",
            },
            "issues": [
                {"issue_id": "PUNCTUATION", "type": "LOW", "severity": "LOW", "original_text": ",", "suggested_text": "，", "reason": "应使用全角标点: ',' 应为对应全角符号", "location_hint": "whole_document"},
                {"issue_id": "PUNCTUATION", "type": "LOW", "severity": "LOW", "original_text": "(", "suggested_text": "（", "reason": "应使用全角标点: '(' 应为 对应全角符号", "location_hint": "whole_document"},
                {"issue_id": "PUNCTUATION", "type": "LOW", "severity": "LOW", "original_text": ")", "suggested_text": "）", "reason": "应使用全角标点: ')' 应为对应全角符号", "location_hint": "whole_document"},
                {"issue_id": "STYLE", "type": "MEDIUM", "severity": "MEDIUM", "original_text": "很好", "suggested_text": "表现优异", "reason": "口语化表达: '很好' 建议改为 '表现优异'", "location_hint": "whole_document"},
                {"issue_id": "STYLE", "type": "MEDIUM", "severity": "MEDIUM", "original_text": "东西", "suggested_text": "组件", "reason": "口语化表达: '东西' 建议改为 '组件'", "location_hint": "whole_document"},
            ],
        }

    return {
        "task_id": f"paper_{paper_id}",
        "status": "NOT_FOUND",
        "summary": {
            "total_issues": 0,
            "score": 100.0,
            "mode": "UNKNOWN",
            "message": "未找到 AI 报告，请先触发审查。",
        },
        "issues": [],
    }
