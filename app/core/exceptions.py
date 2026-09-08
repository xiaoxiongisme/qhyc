"""业务异常定义（M1 阶段）"""


class QHYCError(Exception):
    """平台基类异常"""


class IngestError(QHYCError):
    """采集错误（akshare/tqsdk 拉取失败）"""


class CalibrationError(QHYCError):
    """校准错误（tqsdk 补缺/比对失败）"""


class ImportError(QHYCError):
    """CSV/JSON 导入错误"""


class SchemaError(QHYCError):
    """数据库结构/schema 异常"""


__all__ = [
    "QHYCError",
    "IngestError",
    "CalibrationError",
    "ImportError",
    "SchemaError",
]