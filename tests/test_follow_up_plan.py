"""回访计划服务的接口与规则测试。

覆盖：缺失入职日期、跨年阶段、单位变更、批量导入、重复生成，
以及跳过/改期/转交/无法联系的理由留痕、完成回访只推进相应阶段、指定日期重放。
"""

import unittest
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import get_db
from app.models import Base, DestinationType, FollowUpStage, FollowUpTaskStatus
from app.services import add_months, compute_graduate_plan
from main import app

API = "/api/v1"


class AddMonthsTests(unittest.TestCase):
    def test_cross_year_and_month_end_clamp(self):
        self.assertEqual(add_months(date(2025, 10, 15), 3), date(2026, 1, 15))
        self.assertEqual(add_months(date(2025, 11, 10), 3), date(2026, 2, 10))
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(add_months(date(2024, 1, 31), 1), date(2024, 2, 29))
        self.assertEqual(add_months(date(2026, 7, 1), 12), date(2027, 7, 1))


class ComputeGraduatePlanTests(unittest.TestCase):
    def test_follow_up_before_anchor_is_not_valid(self):
        plan = compute_graduate_plan(
            graduation_year=2025,
            destination_type=DestinationType.EMPLOYMENT,
            employment_start_date=date(2026, 1, 1),
            follow_up_dates=[date(2025, 12, 20)],
            existing_tasks=[],
            as_of=date(2026, 9, 1),
        )
        self.assertTrue(plan.eligible)
        self.assertEqual([c.stage for c in plan.creates], [FollowUpStage.MONTH_3, FollowUpStage.MONTH_6])
        self.assertEqual(plan.covered_stages, ())

    def test_latest_valid_follow_up_covers_early_stages(self):
        plan = compute_graduate_plan(
            graduation_year=2025,
            destination_type=DestinationType.EMPLOYMENT,
            employment_start_date=date(2026, 1, 1),
            follow_up_dates=[date(2026, 5, 1)],
            existing_tasks=[],
            as_of=date(2026, 9, 1),
        )
        self.assertEqual(plan.covered_stages, (FollowUpStage.MONTH_3,))
        self.assertEqual([c.stage for c in plan.creates], [FollowUpStage.MONTH_6])


class FollowUpPlanApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls.TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

    def setUp(self):
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)

        def override_get_db():
            db = self.TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        resp = self.client.post(f"{API}/colleges", json={"name": "测试学院", "code": "T"})
        self.assertEqual(resp.status_code, 200)
        self.college_id = resp.json()["id"]

    def tearDown(self):
        app.dependency_overrides.clear()

    # ---------- 测试辅助 ----------

    def create_graduate(self, student_id, **overrides):
        payload = {
            "student_id": student_id,
            "name": f"学生{student_id}",
            "major": "软件工程",
            "graduation_year": 2025,
            "college_id": self.college_id,
            "destination_status": "已落实",
            "destination_type": "就业",
        }
        payload.update(overrides)
        resp = self.client.post(f"{API}/graduates", json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["id"]

    def generate(self, as_of, graduate_ids=None, dry_run=False):
        payload = {"as_of": as_of, "dry_run": dry_run}
        if graduate_ids is not None:
            payload["graduate_ids"] = graduate_ids
        resp = self.client.post(f"{API}/follow-up-plans/generate", json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def list_tasks(self, **params):
        resp = self.client.get(f"{API}/follow-up-plans/tasks", params=params)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def list_decisions(self, task_id):
        resp = self.client.get(f"{API}/follow-up-plans/tasks/{task_id}/decisions")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    # ---------- 计划生成 ----------

    def test_generate_creates_only_due_stages(self):
        gid = self.create_graduate("S001", employment_start_date="2026-06-01", employer_name="甲公司")
        result = self.generate("2026-09-25")

        self.assertEqual(result["created_count"], 1)
        created = result["created"][0]
        self.assertEqual(created["graduate_id"], gid)
        self.assertEqual(created["stage"], "入职3个月")
        self.assertEqual(created["anchor_date"], "2026-06-01")
        self.assertEqual(created["due_date"], "2026-09-01")

        tasks = self.list_tasks(graduate_id=gid)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "待回访")

    def test_missing_employment_date_skips_graduate(self):
        gid = self.create_graduate("S002", employer_name="乙公司")
        result = self.generate("2026-09-25")

        self.assertEqual(result["created_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        skipped = result["skipped"][0]
        self.assertEqual(skipped["graduate_id"], gid)
        self.assertIn("缺失入职日期", skipped["reason"])
        self.assertEqual(self.list_tasks(graduate_id=gid), [])

    def test_cross_year_stage_due_dates(self):
        gid = self.create_graduate("S003", employment_start_date="2025-11-10")

        early = self.generate("2026-01-15")
        self.assertEqual(early["created_count"], 0, "入职3个月节点跨年到2026-02-10，此前不应生成")

        later = self.generate("2026-05-10")
        self.assertEqual(later["created_count"], 2)
        by_stage = {item["stage"]: item for item in later["created"]}
        self.assertEqual(by_stage["入职3个月"]["due_date"], "2026-02-10")
        self.assertEqual(by_stage["入职6个月"]["due_date"], "2026-05-10")

        tasks = self.list_tasks(graduate_id=gid)
        self.assertEqual([t["due_date"] for t in tasks], ["2026-02-10", "2026-05-10"])

    def test_scattered_follow_up_determines_next_needed_stage(self):
        gid = self.create_graduate("S004", employment_start_date="2026-01-01")
        resp = self.client.post(f"{API}/follow-ups", json={
            "graduate_id": gid,
            "follow_up_date": "2026-05-01",
            "visited_by": "张老师",
        })
        self.assertEqual(resp.status_code, 200, resp.text)

        result = self.generate("2026-09-01")
        self.assertEqual(result["created_count"], 1)
        self.assertEqual(result["created"][0]["stage"], "入职6个月")

    def test_future_graduation_year_is_skipped(self):
        self.create_graduate("S005", graduation_year=2027, employment_start_date="2026-01-01")
        result = self.generate("2026-09-01")
        self.assertEqual(result["created_count"], 0)
        self.assertIn("尚未毕业", result["skipped"][0]["reason"])

    def test_non_employment_destination_is_skipped(self):
        self.create_graduate("S006", destination_type="升学", employment_start_date="2026-01-01")
        result = self.generate("2026-09-01")
        self.assertEqual(result["created_count"], 0)
        self.assertIn("非就业去向", result["skipped"][0]["reason"])

    # ---------- 重复生成与批量导入 ----------

    def test_duplicate_generation_creates_no_duplicates(self):
        gid = self.create_graduate("S007", employment_start_date="2026-01-01")

        first = self.generate("2026-04-01")
        self.assertEqual(first["created_count"], 1)

        second = self.generate("2026-04-01")
        self.assertEqual(second["created_count"], 0)
        self.assertEqual(len(self.list_tasks(graduate_id=gid)), 1)

        third = self.generate("2026-07-05")
        self.assertEqual(third["created_count"], 1)
        self.assertEqual(third["created"][0]["stage"], "入职6个月")
        self.assertEqual(len(self.list_tasks(graduate_id=gid)), 2)

    def test_batch_import_and_regenerate_is_idempotent(self):
        students = [
            {
                "student_id": "B001",
                "name": "批量一",
                "major": "软件工程",
                "graduation_year": 2025,
                "college_id": self.college_id,
                "destination_status": "已落实",
                "destination_type": "就业",
                "employer_name": "甲公司",
                "employment_start_date": "2026-01-01",
            },
            {
                "student_id": "B002",
                "name": "批量二",
                "major": "软件工程",
                "graduation_year": 2025,
                "college_id": self.college_id,
                "destination_status": "已落实",
                "destination_type": "就业",
                "employer_name": "乙公司",
                "employment_start_date": "2026-02-01",
            },
            {
                "student_id": "B003",
                "name": "批量三",
                "major": "软件工程",
                "graduation_year": 2025,
                "college_id": self.college_id,
                "destination_status": "已落实",
                "destination_type": "就业",
            },
        ]

        first = self.client.post(f"{API}/graduates/batch-import", json={"students": students})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["created_count"], 3)

        gen_first = self.generate("2026-09-01")
        self.assertEqual(gen_first["created_count"], 4, "两名有入职日期的学生各生成两个阶段任务")
        self.assertEqual(gen_first["skipped_count"], 1)
        self.assertIn("缺失入职日期", gen_first["skipped"][0]["reason"])
        task_total = len(self.list_tasks())

        again = self.client.post(f"{API}/graduates/batch-import", json={"students": students})
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["created_count"], 0)
        self.assertEqual(again.json()["unchanged_count"], 3)

        gen_second = self.generate("2026-09-01")
        self.assertEqual(gen_second["created_count"], 0, "重复导入同一批学生不得重复造任务")
        self.assertEqual(len(self.list_tasks()), task_total)

    # ---------- 单位变更 ----------

    def test_employer_change_cancels_and_reanchors_plan(self):
        gid = self.create_graduate("S008", employment_start_date="2026-01-01", employer_name="旧公司")
        self.generate("2026-04-15")
        old_tasks = self.list_tasks(graduate_id=gid)
        self.assertEqual(len(old_tasks), 1)
        old_task_id = old_tasks[0]["id"]

        resp = self.client.post(f"{API}/follow-up-plans/graduates/{gid}/employer-change", json={
            "employer_name": "新公司",
            "employment_start_date": "2026-06-01",
            "reason": "学生离职后入职新单位",
            "operator": "李老师",
            "as_of": "2026-09-25",
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()

        self.assertEqual(len(body["cancelled"]), 1)
        self.assertEqual(body["cancelled"][0]["task_id"], old_task_id)
        self.assertEqual(len(body["created"]), 1)
        self.assertEqual(body["created"][0]["anchor_date"], "2026-06-01")
        self.assertEqual(body["created"][0]["due_date"], "2026-09-01")

        old_task = self.client.get(f"{API}/follow-up-plans/tasks/{old_task_id}").json()
        self.assertEqual(old_task["status"], "已取消")

        decisions = self.list_decisions(old_task_id)
        cancel_decisions = [d for d in decisions if d["action"] == "取消任务"]
        self.assertEqual(len(cancel_decisions), 1)
        self.assertIn("单位变更", cancel_decisions[0]["reason"])
        self.assertEqual(cancel_decisions[0]["operator"], "李老师")

        new_task = self.client.get(f"{API}/follow-up-plans/tasks/{body['created'][0]['task_id']}").json()
        self.assertEqual(new_task["status"], "待回访")

        regen = self.generate("2026-09-25")
        self.assertEqual(regen["created_count"], 0, "变更后重复生成不得再造任务")

    # ---------- 老师处置与理由留痕 ----------

    def test_teacher_actions_are_recorded_with_reasons(self):
        gid_a = self.create_graduate("S009", employment_start_date="2026-01-01")
        gid_b = self.create_graduate("S010", employment_start_date="2026-01-01")
        self.generate("2027-02-01")

        tasks_a = self.list_tasks(graduate_id=gid_a)
        self.assertEqual(len(tasks_a), 3)
        skip_id, reschedule_id, transfer_id = (t["id"] for t in tasks_a)
        unreachable_id = self.list_tasks(graduate_id=gid_b)[0]["id"]

        resp = self.client.post(f"{API}/follow-up-plans/tasks/{skip_id}/skip", json={
            "reason": "学生已出国，阶段回访无意义", "operator": "王老师",
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "已跳过")

        resp = self.client.post(f"{API}/follow-up-plans/tasks/{reschedule_id}/reschedule", json={
            "reason": "学生休假，延后两周", "operator": "王老师", "new_due_date": "2026-04-15",
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["due_date"], "2026-04-15")

        resp = self.client.post(f"{API}/follow-up-plans/tasks/{transfer_id}/transfer", json={
            "reason": "原负责老师调岗", "operator": "王老师", "new_assignee": "赵老师",
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["assignee"], "赵老师")

        resp = self.client.post(f"{API}/follow-up-plans/tasks/{unreachable_id}/unreachable", json={
            "reason": "电话停机、微信无回应", "operator": "赵老师",
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "无法联系")

        skip_decisions = self.list_decisions(skip_id)
        self.assertEqual(skip_decisions[-1]["action"], "跳过")
        self.assertEqual(skip_decisions[-1]["reason"], "学生已出国，阶段回访无意义")
        self.assertEqual(skip_decisions[-1]["operator"], "王老师")

        reschedule_decisions = self.list_decisions(reschedule_id)
        self.assertEqual(reschedule_decisions[-1]["action"], "改期")
        self.assertEqual(reschedule_decisions[-1]["new_due_date"], "2026-04-15")

        transfer_decisions = self.list_decisions(transfer_id)
        self.assertEqual(transfer_decisions[-1]["action"], "转交")
        self.assertEqual(transfer_decisions[-1]["new_assignee"], "赵老师")

        unreachable_decisions = self.list_decisions(unreachable_id)
        self.assertEqual(unreachable_decisions[-1]["action"], "标记无法联系")
        self.assertEqual(unreachable_decisions[-1]["reason"], "电话停机、微信无回应")

        resp = self.client.post(f"{API}/follow-up-plans/tasks/{skip_id}/reschedule", json={
            "reason": "已跳过的任务不应再改期", "operator": "王老师", "new_due_date": "2026-05-01",
        })
        self.assertEqual(resp.status_code, 409)

        resp = self.client.post(f"{API}/follow-up-plans/tasks/{transfer_id}/skip", json={
            "reason": "", "operator": "王老师",
        })
        self.assertEqual(resp.status_code, 422, "理由必填")

    # ---------- 完成回访只推进相应阶段 ----------

    def test_complete_advances_only_the_corresponding_stage(self):
        gid = self.create_graduate("S011", employment_start_date="2026-01-01")
        self.generate("2026-08-01")
        tasks = {t["stage"]: t for t in self.list_tasks(graduate_id=gid)}
        self.assertEqual(set(tasks), {"入职3个月", "入职6个月"})
        month6_id = tasks["入职6个月"]["id"]

        resp = self.client.post(f"{API}/follow-up-plans/tasks/{month6_id}/complete", json={
            "follow_up_date": "2026-07-05",
            "operator": "张老师",
            "satisfaction_score": 4,
            "employer_name": "甲公司",
            "remark": "工作稳定",
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        completed = resp.json()
        self.assertEqual(completed["status"], "已完成")
        self.assertIsNotNone(completed["follow_up_id"])

        month3 = self.client.get(f"{API}/follow-up-plans/tasks/{tasks['入职3个月']['id']}").json()
        self.assertEqual(month3["status"], "待回访", "完成6个月回访不得推进3个月阶段")

        follow_ups = self.client.get(f"{API}/follow-ups", params={"graduate_id": gid}).json()
        self.assertEqual(len(follow_ups), 1)
        self.assertEqual(follow_ups[0]["follow_up_date"], "2026-07-05")
        self.assertEqual(follow_ups[0]["visited_by"], "张老师")

        decisions = self.list_decisions(month6_id)
        self.assertEqual(decisions[-1]["action"], "完成回访")

        resp = self.client.post(f"{API}/follow-up-plans/tasks/{month6_id}/complete", json={
            "follow_up_date": "2026-07-06", "operator": "张老师",
        })
        self.assertEqual(resp.status_code, 409, "已完成任务不能重复完成")

    # ---------- 指定日期重放 ----------

    def test_generation_replayable_at_specified_date(self):
        gid = self.create_graduate("S012", employment_start_date="2026-01-01")

        preview = self.generate("2026-04-15", dry_run=True)
        self.assertEqual(preview["created_count"], 1)
        self.assertEqual(preview["created"][0]["due_date"], "2026-04-01")
        self.assertEqual(self.list_tasks(graduate_id=gid), [], "预演不得写入任务")

        real = self.generate("2026-04-15")
        self.assertEqual(real["created_count"], 1)
        self.assertEqual(
            [i["due_date"] for i in real["created"]],
            [i["due_date"] for i in preview["created"]],
            "同一计划日期重放结果必须一致",
        )

        replay = self.generate("2026-04-15", dry_run=True)
        self.assertEqual(replay["created_count"], 0, "已生成的日期重放不应再产生新任务")

        later = self.generate("2026-08-01", dry_run=True)
        self.assertEqual(later["created_count"], 1)
        self.assertEqual(later["created"][0]["stage"], "入职6个月")


if __name__ == "__main__":
    unittest.main()
