# 微专业就业成效追踪系统

维护毕业生、学院、微专业、就业去向、企业跟进和预警记录，支持按届次与组织维度追踪就业成效并保留分析依据。

## 运行约定

服务端代码位于 `app` 目录，默认使用项目目录中的 SQLite 文件。配置通过环境变量提供，导入演示数据前请确认数据库位置可写。

## 测试

在项目根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

## 编译检查

在项目根目录执行：

```bash
python3 -m compileall -q app tests
```

## 启动服务

准备依赖后可执行 `uvicorn main:app --host 127.0.0.1 --port 8000`，根路径与 `/health` 返回服务状态，接口文档位于 `/docs`。

## 毕业生入职后回访计划

依据毕业时间、入职时间与最近一次有效回访，自动生成入职后 **3 / 6 / 12 个月**
三个阶段的回访任务（`app/services/follow_up_plan_rules.py` 为纯规则，
`follow_up_plan_service.py` 为落库服务）。

- **生成与重放**：`POST /api/v1/follow-up-plans/generate`，请求体可传 `as_of`
  在任意指定日期重放；只有该日期当天（含）已到期的阶段才会建任务。
  同范围 + 同重放日期由幂等键去重，重复生成不会重复造任务。
- **批量导入**：`POST /api/v1/follow-up-plans/graduates/import` 按 `batch_key`
  幂等导入，同一批重复导入不重复建档、不重复造任务；缺少入职日期的学生只计数、
  不排计划。
- **阶段处置**：任务支持 `/skip`、`/reschedule`、`/transfer`、`/unreachable`
  和 `/complete`，每次决定必须填写理由并留痕（`GET /{id}/actions`）。
- **完成只推进一个阶段**：`/complete` 登记或关联一条回访记录，仅该任务变为
  已完成，其他阶段不受影响。
- **单位变更**：更新入职时间或单位名称后再次生成，旧入职周期未完成的任务
  自动作废，新周期（`employment_seq` 递增）从 3 个月阶段重新排期。

已有数据库升级时执行 `python scripts/migrate_db.py`（会为 `graduates` 补齐
`graduation_date`、`onboard_date`、`current_employer_name` 字段并创建计划相关表）。
规则细节见 `docs/follow-up-plan.md`。
