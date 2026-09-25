import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.core import init_db, SessionLocal, engine
from app.models import Base, ProvinceReferenceLine, Warning, AttributionRecord


# SQLite 不支持 IF NOT EXISTS 的 ADD COLUMN，需要先查现有列。
_GRADUATES_NEW_COLUMNS = (
    ("graduation_date", "DATE"),
    ("onboard_date", "DATE"),
    ("current_employer_name", "VARCHAR(200)"),
)


def _existing_columns(connection, table_name):
    rows = connection.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    return {row[1] for row in rows}


def migrate_graduates_columns():
    """为回访计划补齐 graduates 表新增字段(仅 SQLite 场景)。"""

    if not engine.url.get_backend_name().startswith("sqlite"):
        return 0
    with engine.begin() as connection:
        columns = _existing_columns(connection, "graduates")
        added = 0
        for name, column_type in _GRADUATES_NEW_COLUMNS:
            if name not in columns:
                connection.execute(
                    text(f"ALTER TABLE graduates ADD COLUMN {name} {column_type}")
                )
                added += 1
        return added


def migrate():
    print("=" * 60)
    print("正在执行数据库迁移...")
    print("=" * 60)

    print("\n1. 创建新表结构...")
    init_db()
    print("   表结构创建完成")

    added_columns = migrate_graduates_columns()
    print(f"\n2. graduates 表补充字段 {added_columns} 个")

    db = SessionLocal()
    try:
        print("\n3. 检查新表数据...")
        bench_count = db.query(ProvinceReferenceLine).count()
        warning_count = db.query(Warning).count()
        attribution_count = db.query(AttributionRecord).count()

        print(f"   省基准线表: {bench_count} 条记录")
        print(f"   预警表: {warning_count} 条记录")
        print(f"   归因记录表: {attribution_count} 条记录")

        if bench_count == 0:
            print("\n4. 省基准线表为空，请运行 python scripts/init_data.py 初始化数据")
        else:
            print("\n4. 数据库迁移完成！")

    except Exception as e:
        print(f"\n迁移失败: {e}")
        import traceback
        traceback.print_exc()
        db.rollback()
    finally:
        db.close()

    print("\n" + "=" * 60)


if __name__ == "__main__":
    migrate()
