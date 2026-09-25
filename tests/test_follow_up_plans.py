"""回访阶段计划服务的接口与规则测试。

覆盖：缺失入职日期、跨年阶段、单位变更、批量导入幂等、
重复生成幂等、指定日期重放、老师处置留痕、完成回访只推进一个阶段。
"""

import unittest
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def _build_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # 在导入应用模型之后再建表。
    from app.models import Base
    from app.core.database import get_db
    from main import app

    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    return client, testing_session_local


class FollowUpPlanApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client, cls.SessionLocal = _build_client()

    def setUp(self):
        from app.models import Base
        Base.metadata.drop_all(bind=self.SessionLocal.kw["bind"])
        Base.metadata.create_all(bind=self.SessionLocal.kw["bind"])

    def _create_college(self):
        resp = self.client.post("/api/v1/colleges", json={"name": "计算机学院", "code": "CS"})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["id"]

    def _create_graduate(self, college_id, **overrides):
        payload = {
            "student_id": "2024001",
            "name": "张三",
            "major": "软件工程",
            "graduation_year": 2024,
            "college_id": college_id,
        }
        payload.update(overrides)
        resp = self.client.post("/api/v1/graduates", json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["id"]

    def _generate(self, as_of, graduate_ids=None):
        body = {"as_of": as_of.isoformat(), "created_by": "王老师"}
        if graduate_ids is not None:
            body["graduate_ids"] = graduate_ids
        resp = self.client.post("/api/v1/follow-up-plans/generate", json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    # 1. 缺失入职日期 -------------------------------------------------
    def test_missing_onboard_date_generates_nothing(self):
        college_id = self._create_college()
        graduate_id = self._create_graduate(college_id, student_id="NOBOARD")
        run = self._generate(date(2025, 6, 1), [graduate_id])
        self.assertEqual(run["requested_count"], 1)
        self.assertEqual(run["created_count"], 0)
        self.assertEqual(run["missing_onboard_count"], 1)

        plans = self.client.get(
            "/api/v1/follow-up-plans", params={"graduate_id": graduate_id}
        ).json()
        self.assertEqual(plans, [])

        # 补录入职日期后重新生成，同一重放日期仍不重复造(幂等批次直接返回)，
        # 换到新的重放日期应能正常生成。
        onboard = date(2024, 8, 1)
        resp = self.client.put(
            f"/api/v1/graduates/{graduate_id}",
            json={"onboard_date": onboard.isoformat(), "current_employer_name": "甲公司"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        run2 = self._generate(date(2024, 12, 1), [graduate_id])
        self.assertEqual(run2["created_count"], 1)
        plans = self.client.get(
            "/api/v1/follow-up-plans", params={"graduate_id": graduate_id}
        ).json()
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["stage"], "入职后3个月")

    # 2. 指定日期重放 + 跨年阶段 --------------------------------------
    def test_replay_generates_only_due_stages_across_years(self):
        college_id = self._create_college()
        onboard = date(2024, 11, 20)  # 跨年：+3月在2025年
        graduate_id = self._create_graduate(
            college_id,
            student_id="CROSSYEAR",
            onboard_date=onboard.isoformat(),
            graduation_date=date(2024, 6, 30).isoformat(),
        )

        # 重放到入职后第2个月：无到期阶段。
        run = self._generate(date(2025, 1, 10), [graduate_id])
        self.assertEqual(run["created_count"], 0)

        # 重放到 2025-02-20（3个月阶段到期）。
        run = self._generate(date(2025, 2, 20), [graduate_id])
        self.assertEqual(run["created_count"], 1)

        # 重放到 2025-05-20（6个月阶段到期）。
        run = self._generate(date(2025, 5, 20), [graduate_id])
        self.assertEqual(run["created_count"], 1)

        # 重放到 2025-11-20（12个月阶段到期，跨到下半年）。
        run = self._generate(date(2025, 11, 20), [graduate_id])
        self.assertEqual(run["created_count"], 1)

        plans = self.client.get(
            "/api/v1/follow-up-plans",
            params={"graduate_id": graduate_id, "limit": 100},
        ).json()
        self.assertEqual(len(plans), 3)
        self.assertEqual(
            [p["due_date"] for p in plans],
            ["2025-02-20", "2025-05-20", "2025-11-20"],
        )
        self.assertEqual([p["employment_seq"] for p in plans], [1, 1, 1])

    # 3. 重复生成幂等 -------------------------------------------------
    def test_duplicate_generation_does_not_recreate_tasks(self):
        college_id = self._create_college()
        graduate_id = self._create_graduate(
            college_id,
            student_id="DUP",
            onboard_date=date(2024, 1, 1).isoformat(),
        )
        run1 = self._generate(date(2024, 8, 1), [graduate_id])
        self.assertEqual(run1["created_count"], 2)
        run2 = self._generate(date(2024, 8, 1), [graduate_id])
        # 同范围同日期视为同一批次，直接幂等返回原批次。
        self.assertEqual(run2["run_id"], run1["run_id"])

        # 即便换个批次发起人，同范围+同重放日期仍命中幂等键。
        body = {
            "as_of": date(2024, 8, 1).isoformat(),
            "graduate_ids": [graduate_id],
            "created_by": "李老师",
        }
        run3 = self.client.post("/api/v1/follow-up-plans/generate", json=body).json()
        self.assertEqual(run3["run_id"], run1["run_id"])

        plans = self.client.get(
            "/api/v1/follow-up-plans", params={"graduate_id": graduate_id}
        ).json()
        self.assertEqual(len(plans), 2)

    # 4. 历史零散回访用于补建已完成阶段 -------------------------------
    def test_scattered_prior_visits_drive_stage_completion(self):
        from app.models import EmployerFollowUp
        from app.core.database import get_db

        college_id = self._create_college()
        onboard = date(2024, 1, 10)
        graduate_id = self._create_graduate(
            college_id,
            student_id="VISIT",
            onboard_date=onboard.isoformat(),
        )
        # 已有一条第8个月的零散回访(落在6-12月之间)。
        db_gen = self.client.app.dependency_overrides[get_db]
        db = next(db_gen())
        db.add(EmployerFollowUp(
            graduate_id=graduate_id,
            follow_up_date=date(2024, 9, 10),
            is_still_employed=True,
        ))
        db.commit()
        db.close()

        self._generate(date(2025, 2, 1), [graduate_id])
        plans = self.client.get(
            "/api/v1/follow-up-plans",
            params={"graduate_id": graduate_id, "limit": 100},
        ).json()
        by_stage = {p["stage"]: p for p in plans}
        # 3个月与6个月阶段均已被该有效回访覆盖，12个月仍待回访。
        self.assertEqual(by_stage["入职后3个月"]["status"], "已完成")
        self.assertEqual(by_stage["入职后6个月"]["status"], "已完成")
        self.assertEqual(by_stage["入职后12个月"]["status"], "待回访")
        self.assertIsNotNone(by_stage["入职后3个月"]["follow_up_id"])

    # 5. 单位变更：旧周期作废，新周期重排 -----------------------------
    def test_employer_change_supersedes_open_tasks(self):
        college_id = self._create_college()
        graduate_id = self._create_graduate(
            college_id,
            student_id="JOBCHANGE",
            onboard_date=date(2024, 1, 1).isoformat(),
            current_employer_name="甲公司",
        )
        self._generate(date(2024, 8, 1), [graduate_id])
        plans = self.client.get(
            "/api/v1/follow-up-plans",
            params={"graduate_id": graduate_id, "limit": 100},
        ).json()
        self.assertEqual(len(plans), 2)

        # 毕业生换了单位并更新入职信息。
        resp = self.client.put(
            f"/api/v1/graduates/{graduate_id}",
            json={
                "onboard_date": date(2024, 7, 1).isoformat(),
                "current_employer_name": "乙公司",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        run = self._generate(date(2024, 12, 10), [graduate_id])
        self.assertGreaterEqual(run["superseded_count"], 1)

        plans = self.client.get(
            "/api/v1/follow-up-plans",
            params={"graduate_id": graduate_id, "limit": 100},
        ).json()
        seq1 = [p for p in plans if p["employment_seq"] == 1]
        seq2 = [p for p in plans if p["employment_seq"] == 2]
        self.assertTrue(seq1)
        self.assertTrue(all(p["status"] == "已作废" for p in seq1))
        self.assertTrue(any(p["stage"] == "入职后3个月" and p["status"] == "待回访" for p in seq2))
        self.assertTrue(all(p["employer_name"] == "乙公司" for p in seq2))

        # 旧任务的作废理由必须留痕。
        old_plan = seq1[0]
        actions = self.client.get(
            f"/api/v1/follow-up-plans/{old_plan['id']}/actions"
        ).json()
        self.assertTrue(any(a["action_type"] == "单位变更作废" and a["reason"] for a in actions))

    # 6. 批量导入 + 重复导入不重复造任务 -------------------------------
    def test_batch_import_is_idempotent(self):
        college_id = self._create_college()
        payload = {
            "batch_key": "batch-2024-fall",
            "graduates": [
                {
                    "student_id": "IMP001",
                    "name": "导入甲",
                    "major": "软件工程",
                    "graduation_year": 2024,
                    "college_id": college_id,
                    "onboard_date": date(2024, 1, 1).isoformat(),
                    "current_employer_name": "甲公司",
                },
                {
                    "student_id": "IMP002",
                    "name": "导入乙",
                    "major": "软件工程",
                    "graduation_year": 2024,
                    "college_id": college_id,
                    # 缺少入职日期
                },
            ],
        }
        resp = self.client.post("/api/v1/follow-up-plans/graduates/import", json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        result = resp.json()
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["missing_onboard"], 1)
        self.assertGreaterEqual(result["auto_generated_plans"], 1)

        # 重复导入同一批次：不新增、不重复造任务。
        resp2 = self.client.post("/api/v1/follow-up-plans/graduates/import", json=payload)
        self.assertEqual(resp2.status_code, 200, resp2.text)
        result2 = resp2.json()
        self.assertEqual(result2["inserted"], 2)  # 回显首次结果
        self.assertEqual(result2["auto_generated_plans"], 0)
        self.assertEqual(result2["plan_run"]["run_id"], result["plan_run"]["run_id"])

        # 同名单再次导入但换新批次键时，按学号识别为更新而非新建，任务仍不重复。
        payload3 = dict(payload)
        payload3["batch_key"] = "batch-2024-fall-rerun"
        resp3 = self.client.post("/api/v1/follow-up-plans/graduates/import", json=payload3)
        self.assertEqual(resp3.status_code, 200, resp3.text)
        result3 = resp3.json()
        self.assertEqual(result3["inserted"], 0)
        self.assertEqual(result3["updated"], 2)

    # 7. 老师处置动作与理由留痕 ---------------------------------------
    def test_dispositions_require_reason_and_record_history(self):
        college_id = self._create_college()
        graduate_id = self._create_graduate(
            college_id,
            student_id="DISP",
            onboard_date=date(2024, 1, 1).isoformat(),
        )
        self._generate(date(2024, 4, 1), [graduate_id])
        plan = self.client.get(
            "/api/v1/follow-up-plans", params={"graduate_id": graduate_id}
        ).json()[0]
        plan_id = plan["id"]

        # 理由为空应被拒绝。
        resp = self.client.post(
            f"/api/v1/follow-up-plans/{plan_id}/skip",
            json={"reason": "   ", "acted_by": "王老师"},
        )
        self.assertEqual(resp.status_code, 422)

        # 跳过。
        resp = self.client.post(
            f"/api/v1/follow-up-plans/{plan_id}/skip",
            json={"reason": "学生已升学，不再属于就业回访范围", "acted_by": "王老师"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "已跳过")

        # 终态任务不能再次处置。
        resp = self.client.post(
            f"/api/v1/follow-up-plans/{plan_id}/skip",
            json={"reason": "再次尝试", "acted_by": "王老师"},
        )
        self.assertEqual(resp.status_code, 400)

        actions = self.client.get(
            f"/api/v1/follow-up-plans/{plan_id}/actions"
        ).json()
        self.assertTrue(any(a["action_type"] == "跳过" for a in actions))
        skip_action = next(a for a in actions if a["action_type"] == "跳过")
        self.assertIn("升学", skip_action["reason"])
        self.assertEqual(skip_action["acted_by"], "王老师")

        # 改期需要新日期；转交需要新负责人。
        self._generate(date(2024, 7, 1), [graduate_id])
        six_month_plan = self.client.get(
            "/api/v1/follow-up-plans",
            params={"graduate_id": graduate_id, "stage": "入职后6个月"},
        ).json()[0]
        pid = six_month_plan["id"]

        resp = self.client.post(
            f"/api/v1/follow-up-plans/{pid}/reschedule",
            json={"reason": "学生出差", "acted_by": "王老师"},
        )
        self.assertEqual(resp.status_code, 422)
        resp = self.client.post(
            f"/api/v1/follow-up-plans/{pid}/reschedule",
            json={
                "reason": "学生出差，改到月底",
                "acted_by": "王老师",
                "new_scheduled_date": "2024-07-25",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["scheduled_date"], "2024-07-25")
        self.assertEqual(resp.json()["status"], "已改期")

        resp = self.client.post(
            f"/api/v1/follow-up-plans/{pid}/transfer",
            json={"reason": "按学院分工调整给就业办", "acted_by": "王老师", "new_assignee": "刘老师"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["assignee"], "刘老师")
        self.assertEqual(resp.json()["status"], "已转交")

        # 标记无法联系。
        resp = self.client.post(
            f"/api/v1/follow-up-plans/{pid}/unreachable",
            json={"reason": "电话停机、邮箱退信，已尝试三种渠道", "acted_by": "刘老师"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "无法联系")

    # 8. 完成回访只推进对应阶段 ---------------------------------------
    def test_completion_advances_only_matching_stage(self):
        college_id = self._create_college()
        graduate_id = self._create_graduate(
            college_id,
            student_id="DONE",
            onboard_date=date(2024, 1, 1).isoformat(),
        )
        self._generate(date(2024, 8, 1), [graduate_id])
        plans = self.client.get(
            "/api/v1/follow-up-plans",
            params={"graduate_id": graduate_id, "limit": 100},
        ).json()
        stage3 = next(p for p in plans if p["stage"] == "入职后3个月")
        stage6 = next(p for p in plans if p["stage"] == "入职后6个月")

        resp = self.client.post(
            f"/api/v1/follow-up-plans/{stage3['id']}/complete",
            json={
                "reason": "电话回访确认在岗",
                "acted_by": "王老师",
                "follow_up_date": "2024-04-05",
                "employer_name": "甲公司",
                "is_still_employed": True,
                "satisfaction_score": 4.5,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "已完成")
        self.assertIsNotNone(resp.json()["follow_up_id"])

        # 6个月阶段保持待回访。
        again = self.client.get(
            "/api/v1/follow-up-plans",
            params={"graduate_id": graduate_id, "limit": 100},
        ).json()
        stage6_reloaded = next(p for p in again if p["id"] == stage6["id"])
        self.assertEqual(stage6_reloaded["status"], "待回访")

        # 回访日期早于入职日期应被拒绝。
        resp = self.client.post(
            f"/api/v1/follow-up-plans/{stage6['id']}/complete",
            json={
                "reason": "误填日期",
                "acted_by": "王老师",
                "follow_up_date": "2023-12-01",
            },
        )
        self.assertEqual(resp.status_code, 400)

        # 可以关联已有的回访记录完成任务。
        fu = self.client.post(
            "/api/v1/follow-ups",
            json={
                "graduate_id": graduate_id,
                "follow_up_date": "2024-07-08",
                "is_still_employed": True,
            },
        ).json()
        resp = self.client.post(
            f"/api/v1/follow-up-plans/{stage6['id']}/complete",
            json={
                "reason": "线下走访已登记，回填计划",
                "acted_by": "王老师",
                "follow_up_date": "2024-07-08",
                "follow_up_id": fu["id"],
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["follow_up_id"], fu["id"])

    # 9. 批量(全员)生成与查询过滤 -------------------------------------
    def test_generate_for_all_graduates_and_filters(self):
        college_id = self._create_college()
        self._create_graduate(
            college_id, student_id="ALL1",
            onboard_date=date(2024, 1, 1).isoformat(),
        )
        self._create_graduate(
            college_id, student_id="ALL2",
            onboard_date=date(2024, 6, 1).isoformat(),
        )
        run = self._generate(date(2024, 10, 1))
        self.assertEqual(run["requested_count"], 2)
        # ALL1 到期3、6两个月；ALL2 到期3个月。
        self.assertEqual(run["created_count"], 3)

        pending = self.client.get(
            "/api/v1/follow-up-plans", params={"status": "待回访", "limit": 100}
        ).json()
        self.assertEqual(len(pending), 3)

        due = self.client.get(
            "/api/v1/follow-up-plans",
            params={"due_before": "2024-04-01", "limit": 100},
        ).json()
        self.assertEqual(len(due), 1)

    # 10. 规则纯函数：月末与阶段窗口 ----------------------------------
    def test_pure_rules_month_end_and_windows(self):
        from app.services.follow_up_plan_rules import (
            add_months,
            stage_index_for_visit,
            stage_due_date,
        )
        from app.models import FollowUpStage

        # 11月30日入职，1个月后取月末12月31日，跨年正确。
        self.assertEqual(add_months(date(2024, 11, 30), 1), date(2024, 12, 31))
        onboard = date(2024, 1, 31)
        self.assertEqual(stage_due_date(onboard, FollowUpStage.MONTH_3), date(2024, 4, 30))
        self.assertIsNone(stage_index_for_visit(onboard, date(2023, 12, 1)))
        self.assertIsNone(stage_index_for_visit(onboard, date(2024, 2, 1)))
        self.assertEqual(stage_index_for_visit(onboard, date(2024, 5, 1)), 0)
        self.assertEqual(stage_index_for_visit(onboard, date(2024, 9, 1)), 1)
        self.assertEqual(stage_index_for_visit(onboard, date(2025, 3, 1)), 2)


if __name__ == "__main__":
    unittest.main()
