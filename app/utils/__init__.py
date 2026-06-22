"""
工具函数模块
"""


class WhereBuilder:
    """安全的 WHERE 子句构建器
    
    所有 SQL 条件片段为代码常量，用户输入通过 %s 占位符传递，
    绝不通过 f-string 拼接用户输入。
    
    用法:
        wb = WhereBuilder(["is_deleted = 0"])
        wb.add("status = %s", status_value)
        wb.add("job_order LIKE %s", f"%{keyword}%")
        where_clause, params = wb.build()
        cursor.execute(f"SELECT * FROM t WHERE {where_clause}", params)
    """

    def __init__(self, initial_conditions=None):
        self._conditions = list(initial_conditions) if initial_conditions else []
        self._params = []

    def add(self, condition, *values):
        self._conditions.append(condition)
        self._params.extend(values)

    def build(self):
        return " AND ".join(self._conditions), self._params
