import json
import hashlib
import os
import re
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import app


def sample_evaluation(task_type="0-1 代码生成"):
    dimension = {
        "score": 5,
        "description": "本轮逐项核对了题面约束并完成项目验收，功能与交付结果都有对应记录。",
    }
    return {
        "task_type": task_type,
        "task_difficulty": "困难",
        "language_framework": "Python, FastAPI, Docker",
        "environment_reproducibility": "已容器化，可一键起环境",
        "delivery": dict(dimension),
        "instruction_following": dict(dimension),
        "planning": dict(dimension),
        "reasoning": dict(dimension),
        "execution": dict(dimension),
        "other_issues": "",
    }


class ValidationTests(unittest.TestCase):
    def test_responses_schemas_do_not_use_unsupported_unique_items(self):
        def walk(value):
            if isinstance(value, dict):
                self.assertNotIn("uniqueItems", value)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(app.evaluation_split_dimension_schema("delivery"))
        walk(app.evaluation_score_cap_schema())
        walk(app.difficulty_contract_schema())

    def test_score_cap_calibration_is_downward_only_and_updates_v2_mirrors(self):
        evaluation = sample_evaluation()
        evaluation["score_stage_version"] = 2
        evaluation["scores"] = [5, 5, 5, 5, 5]
        evaluation["descriptions"] = [
            evaluation[key]["description"] for key in app.EVALUATION_DIMENSION_KEYS
        ]
        for field in app.EVALUATION_SCORE_STAGE_DETAIL_FIELDS:
            evaluation[field] = [
                f"{field}-{key}" for key in app.EVALUATION_DIMENSION_KEYS
            ]
        evaluation["processFindings"] = "评分版本 2；" + "；".join(
            f"{app.EVALUATION_DIMENSION_LABELS[key]}=5分；事实={key}.py:1"
            for key in app.EVALUATION_DIMENSION_KEYS
        )
        calibration = {"calibrated": True, "reason": "按真实边界事实校准"}
        for index, key in enumerate(app.EVALUATION_DIMENSION_KEYS):
            score = 5 if index == 0 else 4
            calibration[key] = {
                "score": score,
                "description": f"第 1 轮在 {key}.py 发现一项真实遗漏。该遗漏造成对应边界没有验证。",
                "when": f"第 1 轮第 2 步操作 {key}.py",
                "behavior": f"检查 {key}.py",
                "impact": "对应边界没有验证",
                "expected": "应补齐边界检查",
                "evidenceRefs": f"{key}.py:1",
                "processFinding": (
                    f"{app.EVALUATION_DIMENSION_LABELS[key]}={score}分；"
                    f"事实={key}.py:1"
                ),
            }

        adjusted = app.apply_evaluation_score_cap_calibration(
            evaluation, calibration
        )

        self.assertEqual(app.evaluation_total_score(evaluation), 25)
        self.assertEqual(app.evaluation_total_score(adjusted), 21)
        self.assertEqual(adjusted["scores"], [5, 4, 4, 4, 4])
        self.assertEqual(
            adjusted["_score_cap"]["original_scores"], [5, 5, 5, 5, 5]
        )
        self.assertEqual(
            adjusted["_score_cap"]["adjusted_dimensions"],
            list(app.EVALUATION_DIMENSION_KEYS[1:]),
        )
        self.assertEqual(
            adjusted["descriptions"][1],
            adjusted["instruction_following"]["description"],
        )

    def test_score_cap_calibration_rejects_over_reduction_and_score_increase(self):
        evaluation = sample_evaluation()
        calibration = {"calibrated": True, "reason": "错误校准"}
        for key in app.EVALUATION_DIMENSION_KEYS:
            calibration[key] = {
                "score": 4,
                "description": "第 1 轮存在真实遗漏。该遗漏造成边界没有验证。",
                "when": "第 1 轮第 2 步操作 app.py",
                "behavior": "检查 app.py",
                "impact": "边界没有验证",
                "expected": "应补齐边界检查",
                "evidenceRefs": "app.py:1",
                "processFinding": (
                    f"{app.EVALUATION_DIMENSION_LABELS[key]}=4分；事实=app.py:1"
                ),
            }
        with self.assertRaisesRegex(app.WorkflowError, "恰好收敛到 21 分"):
            app.apply_evaluation_score_cap_calibration(evaluation, calibration)

        evaluation["delivery"]["score"] = 4
        calibration["delivery"]["score"] = 5
        with self.assertRaisesRegex(app.WorkflowError, "只能保持或降低"):
            app.apply_evaluation_score_cap_calibration(evaluation, calibration)

    def test_score_cap_row_policy_requires_new_version_marker(self):
        evaluation = sample_evaluation()
        row = {"solo_qa_state": "not_submitted"}
        self.assertEqual(
            app.completed_turn_score_cap_issue(row, evaluation), ""
        )
        evaluation["_score_cap_policy_version"] = (
            app.EVALUATION_SCORE_CAP_POLICY_VERSION
        )
        self.assertIn(
            "最高允许 21 分",
            app.completed_turn_score_cap_issue(row, evaluation),
        )
        row["solo_qa_state"] = "qc_passed"
        self.assertEqual(
            app.completed_turn_score_cap_issue(row, evaluation), ""
        )

    def test_evaluation_descriptions_reject_template_phrases(self):
        evaluation = sample_evaluation()
        evaluation["planning"]["description"] = "阶段顺序清楚，最终产物可用。"

        with self.assertRaisesRegex(app.WorkflowError, "阶段顺序清楚"):
            app.normalize_evaluation(evaluation)

    def test_evaluation_descriptions_reject_heavy_review_tone(self):
        for phrase in (
            "无法支撑",
            "返工点未被发现",
            "执行阶段暴露",
            "核心场景只缩短了失败窗口",
            "本次只读隔离复核中",
            "本次复核中",
            "未据此扣分",
            "属于环境故障",
            "逐项响应",
            "核心流程",
            "未影响定档",
        ):
            evaluation = sample_evaluation()
            evaluation["reasoning"]["description"] = f"测试结果{phrase}，仍需检查。"
            with self.subTest(phrase=phrase), self.assertRaisesRegex(
                app.WorkflowError, phrase
            ):
                app.normalize_evaluation(evaluation)

    def test_evaluation_guidance_names_natural_writing_requirements(self):
        self.assertIn("自然的项目工作记录", app.EVALUATION_DESCRIPTION_GUIDANCE)
        self.assertIn(
            "做了什么—途中遇到什么—最后结果怎样",
            app.EVALUATION_DESCRIPTION_GUIDANCE,
        )
        self.assertIn("不要为了凑结构编造过程", app.EVALUATION_DESCRIPTION_GUIDANCE)
        self.assertIn("五个维度不要使用相同的开头", app.EVALUATION_DESCRIPTION_GUIDANCE)
        self.assertIn("执行写具体对象、操作结果和故障恢复", app.EVALUATION_DESCRIPTION_GUIDANCE)
        self.assertIn("最多选在最相关的两个维度出现", app.EVALUATION_DESCRIPTION_GUIDANCE)
        self.assertIn("自然写明问题发生在第几轮", app.EVALUATION_DESCRIPTION_GUIDANCE)
        self.assertIn(
            "至少一项客观证据",
            app.EVALUATION_DESCRIPTION_GUIDANCE,
        )
        self.assertIn("具体不足及其实际影响", app.EVALUATION_DESCRIPTION_GUIDANCE)
        self.assertIn("不要为了省事把五项机械地都评为 5 分", app.EVALUATION_SCORE_GUIDANCE)
        self.assertIn(
            "交付完整性、指令遵循、任务规划、推理能力、执行能力的固定顺序",
            app.EVALUATION_SCORE_GUIDANCE,
        )
        self.assertIn("面向使用者的实际完成声明", app.EVALUATION_FACT_ATTRIBUTION_GUIDANCE)
        self.assertIn("后续独立验收通过不能抹掉", app.EVALUATION_FACT_ATTRIBUTION_GUIDANCE)
        self.assertIn("不可见的内部思维", app.EVALUATION_FACT_ATTRIBUTION_GUIDANCE)
        self.assertIn("如果轨迹中找不到真实不足，应改评 5 分", app.EVALUATION_DESCRIPTION_GUIDANCE)
        self.assertIn("不直接抄写 `[0,2,1,1]`", app.EVALUATION_DESCRIPTION_GUIDANCE)
        self.assertIn("不出现 AI、AI 浏览器", app.EVALUATION_DESCRIPTION_GUIDANCE)
        self.assertIn("模型认为", app.EVALUATION_DESCRIPTION_GUIDANCE)
        self.assertIn("模型完成了", app.EVALUATION_DESCRIPTION_GUIDANCE)
        self.assertIn("把“未”写成“没有”或“还没”", app.EVALUATION_DESCRIPTION_GUIDANCE)
        self.assertIn("把“均”写成“都”", app.EVALUATION_DESCRIPTION_GUIDANCE)
        self.assertIn("把“包含”写成“有”", app.EVALUATION_DESCRIPTION_GUIDANCE)
        self.assertIn("不使用 Markdown 反引号", app.EVALUATION_DESCRIPTION_GUIDANCE)
        for phrase in app.EVALUATION_DISALLOWED_PHRASES:
            self.assertIn(phrase, app.EVALUATION_DESCRIPTION_GUIDANCE)
        for phrase in app.EVALUATION_HIGH_RISK_FRAGMENTS:
            self.assertIn(phrase, app.EVALUATION_DESCRIPTION_GUIDANCE)

    def test_evaluation_description_rewrites_generic_user_subject(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = (
            "逐项核对题面要求后，最终用户可以在用户界面查看已经生成的检查计划。"
        )

        normalized = app.normalize_evaluation(evaluation)

        self.assertEqual(
            normalized["delivery"]["description"],
            "逐项核对题面要求后，使用人员可以在页面查看已经生成的检查计划。",
        )

    def test_generated_evaluation_descriptions_are_made_more_conversational(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = (
            "三个场景均已验证完成，响应不包含旧结果，"
            "当前未发现残留记录，全部要求均已覆盖。"
        )

        normalized = app.normalize_evaluation(evaluation)

        self.assertEqual(
            normalized["delivery"]["description"],
            "三个场景都已经验证完成，响应没有旧结果，"
            "当前还没发现残留记录，全部要求都已经覆盖。",
        )

    def test_plain_wording_keeps_business_terms_and_evidence_unchanged(self):
        description = (
            '未来天数、均值和“未保存”保持原样，'
            'ConfigDict(extra="ignore") 未修改。'
        )

        self.assertEqual(
            app.naturalize_evaluation_description(description),
            '未来天数、均值和“未保存”保持原样，'
            'ConfigDict(extra="ignore") 没有修改。',
        )

    def test_plain_wording_does_not_duplicate_a_nested_negation(self):
        self.assertEqual(
            app.naturalize_evaluation_description(
                "代码复核确认没有未提交改动，也没有没有提交改动。"
            ),
            "代码复核确认没有未提交改动，也没有未提交改动。",
        )

    def test_manual_evaluation_keeps_the_users_wording(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = "三个场景均已验证，尚未发现问题。"

        normalized = app.normalize_manual_evaluation(evaluation)

        self.assertEqual(
            normalized["delivery"]["description"],
            "三个场景均已验证，尚未发现问题。",
        )

    def test_manual_evaluation_removes_markdown_backticks(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = (
            "人工核对 `app.py` 和 `normalize_evaluation()`，结果保持不变。"
        )

        normalized = app.normalize_manual_evaluation(evaluation)

        self.assertEqual(
            normalized["delivery"]["description"],
            "人工核对 “app.py” 和 “normalize_evaluation()”，结果保持不变。",
        )

    def test_evaluation_rejects_raw_number_arrays(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = (
            "零费用分配结果是 [0,2,1,1]，其余流程保持可用。"
        )

        with self.assertRaisesRegex(app.WorkflowError, "原始数字数组"):
            app.normalize_evaluation(evaluation)

        self.assertTrue(
            app.retryable_review_output_error(
                "自动检查的 delivery 描述包含不易理解的原始数字数组"
            )
        )

    def test_evaluation_descriptions_reject_ai_identity_tool_and_model_names(self):
        descriptions = (
            "AI 完成了页面修复。",
            "AI浏览器确认页面能够打开。",
            "AI Agent 修正了接口。",
            "AI模型完成了数据检查。",
            "Codex 补齐了测试。",
            "GPT-5.6 判断边界正确。",
            "Claude Code 完成了改动。",
            "模型认为当前结果正确。",
            "模型完成了页面交付。",
        )
        for description in descriptions:
            evaluation = sample_evaluation()
            evaluation["delivery"]["description"] = description
            with self.subTest(description=description), self.assertRaisesRegex(
                app.WorkflowError, "AI 身份、工具或模型名称"
            ):
                app.normalize_evaluation(evaluation)

        self.assertTrue(
            app.retryable_review_output_error(
                "自动检查的 delivery 描述不能出现 AI 身份、工具或模型名称：Codex"
            )
        )

    def test_evaluation_identity_check_does_not_match_business_model_or_filename(self):
        evaluation = sample_evaluation()
        evaluation["reasoning"]["description"] = (
            "main.py 中的状态模型保留原字段，旧记录仍可正常读取。"
        )

        normalized = app.normalize_evaluation(evaluation)

        self.assertEqual(
            normalized["reasoning"]["description"],
            evaluation["reasoning"]["description"],
        )

    def test_manual_evaluation_rejects_ai_identity_reference(self):
        evaluation = sample_evaluation()
        evaluation["execution"]["description"] = "模型完成了页面和接口检查。"

        with self.assertRaisesRegex(
            app.WorkflowError, "AI 身份、工具或模型名称"
        ):
            app.normalize_manual_evaluation(evaluation)

    def test_generation_nonfull_evaluation_description_requires_turn_number(self):
        evaluation = sample_evaluation()
        evaluation["planning"] = {
            "score": 4,
            "description": (
                "检查 app.py 时遗漏了容器健康状态。"
                "这导致正式容器验收没有完成。"
            ),
        }

        with self.assertRaisesRegex(app.WorkflowError, "未写明第 1 轮"):
            app.normalize_evaluation(evaluation, 1)

    def test_generation_nonfull_evaluation_description_requires_negative_marker(self):
        evaluation = sample_evaluation()
        evaluation["planning"] = {
            "score": 4,
            "description": (
                "第 1 轮检查了 app.py 和页面交互。"
                "这使得相关功能有了完整记录。"
            ),
        }

        with self.assertRaisesRegex(app.WorkflowError, "没有写出具体不足"):
            app.normalize_evaluation(evaluation, 1)

    def test_generation_nonfull_evaluation_description_requires_consequence_marker(self):
        evaluation = sample_evaluation()
        evaluation["planning"] = {
            "score": 4,
            "description": (
                "第 1 轮检查 app.py 时遗漏了容器健康状态。"
                "随后又查看了页面交互。"
            ),
        }

        with self.assertRaisesRegex(app.WorkflowError, "没有说明实际后果"):
            app.normalize_evaluation(evaluation, 1)

    def test_nonfull_evaluation_rejects_environment_or_network_deduction(self):
        evaluation = sample_evaluation()
        evaluation["execution"] = {
            "score": 4,
            "description": (
                "第 1 轮执行后端检查时发现系统解释器不可用。"
                "运行准备不足导致验证推迟到补齐环境后才完成。"
            ),
        }

        with self.assertRaisesRegex(app.WorkflowError, "环境或网络问题"):
            app.normalize_evaluation(evaluation, 1)

    def test_nonfull_execution_accepts_concrete_action_error_without_environment(self):
        evaluation = sample_evaluation()
        evaluation["execution"] = {
            "score": 4,
            "description": (
                "第 1 轮在 app.py 连续使用了 3 次不匹配的文本替换。"
                "这些重复操作造成返工，读取实际代码片段后才完成修改。"
            ),
        }

        normalized = app.normalize_evaluation(evaluation, 1)

        self.assertEqual(normalized["execution"]["score"], 4)

    def test_nonfull_evaluation_accepts_explicit_rework_as_actual_consequence(self):
        evaluation = sample_evaluation()
        evaluation["reasoning"] = {
            "score": 4,
            "description": (
                "第 1 轮在 app.py 的 test_power_in_range_accepted() 中遗漏了零值边界。"
                "首次检查返回 422，需要返工修正用例，之后复验已经通过。"
            ),
        }

        normalized = app.normalize_evaluation(evaluation, 1)

        self.assertEqual(normalized["reasoning"]["score"], 4)

    def test_nonfull_evaluation_file_count_requires_filename(self):
        evaluation = sample_evaluation()
        evaluation["planning"] = {
            "score": 4,
            "description": (
                "第 1 轮在收尾检查中发现 10 个文件需要整理。"
                "计划没有继续处理，导致缺少修正后的记录。"
            ),
        }

        with self.assertRaisesRegex(app.WorkflowError, "具体步骤、文件"):
            app.normalize_evaluation(evaluation, 1)

    def test_nonfull_evaluation_file_count_accepts_named_file(self):
        evaluation = sample_evaluation()
        evaluation["planning"] = {
            "score": 4,
            "description": (
                "第 1 轮发现 10 个文件需要整理，其中包括 app/main.py。"
                "计划没有继续处理，导致该文件缺少修正后的记录。"
            ),
        }

        normalized = app.normalize_evaluation(evaluation, 1)

        self.assertEqual(normalized["planning"]["score"], 4)

    def test_nonfull_evaluation_description_accepts_natural_evidence(self):
        evaluation = sample_evaluation()
        evaluation["planning"] = {
            "score": 4,
            "description": (
                "第 2 轮检查 app.py 时遗漏了容器健康状态。"
                "这个遗漏导致正式容器验收没有完成。"
            ),
        }

        normalized = app.normalize_evaluation(evaluation, 2)

        self.assertEqual(normalized["planning"]["score"], 4)

    def test_nonfull_evaluation_allows_evidence_and_impact_in_later_sentence(self):
        evaluation = sample_evaluation()
        evaluation["planning"] = {
            "score": 4,
            "description": (
                "第 2 轮的收尾安排遗漏了兼容路径。"
                "随后检查 App.tsx 时才补看旧入口，导致一次返工。"
            ),
        }

        normalized = app.normalize_evaluation(evaluation, 2)

        self.assertEqual(normalized["planning"]["score"], 4)

    def test_planning_nonfull_requires_location_on_the_planning_defect(self):
        evaluation = sample_evaluation()
        evaluation["planning"] = {
            "score": 3,
            "description": (
                "第 1 轮先查看前后端材料再连续修改，但没有建立分项计划。"
                "随后在 `api` 目录执行前端检查失败，造成验证路径反复。"
            ),
        }

        with self.assertRaisesRegex(app.WorkflowError, "具体步骤、文件"):
            app.normalize_evaluation(evaluation, 1)

    def test_nonfull_location_accepts_specific_replace_and_argument_mistakes(self):
        evaluation = sample_evaluation()
        evaluation["execution"] = {
            "score": 4,
            "description": (
                "产出合计边界用例的补写发生了两处可核对的操作失误。"
                "第 1 轮编辑 frontend/src/domain.test.ts 时，替换范围覆盖了相邻用例的"
                " “it” 声明；写入 backend/tests/test_api.py 时又把待匹配文本和新增内容"
                "放反，调用返回“String to replace not found in file”。"
                "两处失误增加了修正操作，但最终检查全部通过。"
            ),
        }

        normalized = app.normalize_evaluation(evaluation, 1)

        self.assertEqual(normalized["execution"]["score"], 4)

    def test_nonfull_location_accepts_a_clear_cross_sentence_change_reference(self):
        evaluation = sample_evaluation()
        evaluation["instruction_following"] = {
            "score": 4,
            "description": (
                "第 1 轮第 30 步把 api/app/main.py 的类型化 Body 参数改为直接读取"
                " Request。后续独立验收调用 app.openapi() 确认 requestBody 消失；"
                "这一具体改动造成交互式 API 文档无法展示请求结构，属于接口契约退化。"
            ),
        }

        normalized = app.normalize_evaluation(evaluation, 1)

        self.assertEqual(normalized["instruction_following"]["score"], 4)

    def test_full_score_requires_verification_basis(self):
        evaluation = sample_evaluation()
        evaluation["instruction_following"]["description"] = (
            "第 1 轮实现了创建与完成两个契约，主备编号归入同一箱体。"
            "未知、停用和重复编号都有明确反馈。"
        )

        with self.assertRaisesRegex(app.WorkflowError, "缺少实际核对或验收依据"):
            app.normalize_evaluation(evaluation, 1)

    def test_full_score_rejects_recovered_deficiency(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = (
            "第 1 轮完成暂停周期接口和持久化，最终验收记录成功。"
            "早期用例构造不足已经修复。"
        )

        with self.assertRaisesRegex(app.WorkflowError, "满分描述包含扣分点"):
            app.normalize_evaluation(evaluation, 1)

    def test_full_score_rejects_first_failure_followed_by_rework(self):
        cases = (
            (
                "planning",
                2,
                "第 2 轮先复现字符串压力、布尔采样间隔等五类宽松转换，再修改 "
                "backend/app/schemas.py，并把类型拒绝、合法整数反向用例和 verify "
                "验收断言依次补进测试。整数压力用例首次因尾部取整为零而失败后，"
                "定位到测试数据触发既有正值约束，随即调整构造数据并重新完成 35 项"
                "后端检查、verify 全项检查和 6 项页面场景，规划覆盖了复现、修复、"
                "专项回归与联调验收。",
            ),
            (
                "execution",
                1,
                "第 1 轮先完成 TypeScript 检查和 23 个 Vitest 用例，再运行原有 8 个"
                "页面用例；本地浏览器缺少共享库后，将所需 Debian arm64 包解压到临时"
                "目录并通过 LD_LIBRARY_PATH 恢复页面执行。新增场景最初出现 1 项窄屏"
                "失败，定位并修正上传控件后 4 项专项场景通过，合并后 npm run verify "
                "显示 12 个页面用例通过，npm run build 也正常生成 dist 资源。",
            ),
        )

        for dimension, turn_number, description in cases:
            evaluation = sample_evaluation()
            evaluation[dimension]["description"] = description
            with self.subTest(dimension=dimension), self.assertRaisesRegex(
                app.WorkflowError, "满分描述包含扣分点"
            ):
                app.normalize_evaluation(evaluation, turn_number)

    def test_full_score_recovered_rework_allows_expected_contract_feedback(self):
        evaluation = sample_evaluation()
        description = (
            "第 1 轮在 api.py 验证错误请求首次按题面预期失败并返回 422，"
            "调整为合法请求后检查通过。"
        )
        self.assertIsNotNone(
            app.EVALUATION_FULL_SCORE_RECOVERED_REWORK_RE.search(description)
        )
        evaluation["instruction_following"]["description"] = description

        normalized = app.normalize_evaluation(evaluation, 1)

        self.assertEqual(normalized["instruction_following"]["score"], 5)

    def test_full_score_recovered_rework_allows_historical_or_environment_failure(self):
        descriptions = (
            (
                "第 1 轮在 app.py 核对到历史基线的早期测试失败，随后修改旧夹具并"
                "重新检查通过；本轮 12 项检查全部通过。"
            ),
            (
                "第 1 轮在 App.tsx 复核时，系统运行库缺失使页面场景最初失败，"
                "补齐浏览器依赖后 4 项检查通过。"
            ),
            (
                "第 1 轮核对 App.tsx 后，页面场景最初因共享库缺失而失败，"
                "补齐动态库后 4 项检查通过。"
            ),
        )

        for description in descriptions:
            self.assertIsNotNone(
                app.EVALUATION_FULL_SCORE_RECOVERED_REWORK_RE.search(description)
            )
            evaluation = sample_evaluation()
            evaluation["execution"]["description"] = description
            with self.subTest(description=description):
                normalized = app.normalize_evaluation(evaluation, 1)
                self.assertEqual(normalized["execution"]["score"], 5)

    def test_full_score_environment_words_do_not_hide_an_explicit_mistake(self):
        evaluation = sample_evaluation()
        evaluation["execution"]["description"] = (
            "第 1 轮错误地下载 amd64 浏览器依赖到 arm64 环境，最初页面检查失败，"
            "修正后 4 项检查通过。"
        )

        with self.assertRaisesRegex(app.WorkflowError, "满分描述包含扣分点"):
            app.normalize_evaluation(evaluation, 1)

    def test_new_score_descriptions_do_not_hard_block_generic_openings(self):
        cases = (
            (
                "planning",
                "第 1 轮先确认空仓库与运行条件，再依次安排项目骨架、校验与布局逻辑、"
                "React 页面、样式、Vitest、真实浏览器场景和 Docker 交付。",
            ),
            (
                "delivery",
                "第 2 轮已补齐 src/main.tsx 页面入口，在 src/App.tsx 接入 JSON 粘贴、"
                "非法输入整批拒绝、时间轴展示与冲突联动。",
            ),
        )

        for dimension, description in cases:
            with self.subTest(dimension=dimension):
                app.validate_evaluation_description_novelty(
                    {dimension: {"score": 5, "description": description}},
                    {dimension: []},
                    require_distinct_opening=True,
                )

    def test_rewritten_b5_descriptions_use_project_specific_openings(self):
        cases = (
            (
                "planning",
                "空仓库里第一批落下的是文物应急卡的数据边界、A5 版式和本地中文字体，"
                "第 1 轮接着把安全区测量、打印状态与三组测试串起来。",
            ),
            (
                "delivery",
                "JSON 粘贴框在第 2 轮真正接到冲突检视器，合法提示按资源进入时间轴，"
                "非法批次会撤下旧结果，页面交互和容器服务都有对应验收记录。",
            ),
        )

        for dimension, description in cases:
            with self.subTest(dimension=dimension):
                app.validate_evaluation_description_novelty(
                    {dimension: {"score": 5, "description": description}},
                    {dimension: []},
                    require_distinct_opening=True,
                )

    def test_description_novelty_blocks_only_effectively_identical_history(self):
        previous = (
            "陶坯称重页面在第 1 轮接入批次核对和差异提示，src/App.tsx 保存筛选状态。"
            "验收记录显示 18 项检查完成，超差批次会在列表中标红并阻止确认。"
        )
        copied = previous.replace("18 项", "19 项")
        history = {
            "delivery": [{
                "reference": "SOLO-QA #7001",
                "description": previous,
                "source": "account_remote",
            }]
        }

        app.validate_evaluation_description_novelty(
            {"delivery": {"score": 5, "description": copied}},
            history,
            require_distinct_opening=False,
        )

        with self.assertRaisesRegex(app.WorkflowError, "SOLO-QA #7001 完全重复"):
            app.validate_evaluation_description_novelty(
                {"delivery": {"score": 5, "description": previous}},
                history,
                require_distinct_opening=False,
            )

        distinct = (
            "窑炉配方卡把升温区间和保温阶段放进独立时间轴，第 1 轮还增加越界温度提示。"
            "最终核对覆盖配方切换、异常恢复和打印摘要，三个页面状态都保留正确结果。"
        )
        app.validate_evaluation_description_novelty(
            {"delivery": {"score": 5, "description": distinct}},
            history,
            require_distinct_opening=False,
        )

    def test_description_novelty_soft_scan_requests_only_a_wording_rewrite(self):
        previous = (
            "换线排程的接口和页面都完成了，后续独立验收执行 docker compose build "
            "并确认真实服务场景通过，最后没有留下失败检查。"
        )
        candidate = (
            "脉冲配对已经接入接口，后续独立验收执行 docker compose build "
            "并核对窗口边界，最终服务可以正常使用。"
        )
        history = {
            "delivery": [{
                "reference": "SOLO-QA #7002",
                "description": previous,
                "source": "account_remote",
            }]
        }

        with self.assertRaisesRegex(
            app.WorkflowError, "SOLO-QA #7002 高度重复.*只重写该维度措辞"
        ):
            app.validate_evaluation_description_novelty(
                {"delivery": {"score": 5, "description": candidate}},
                history,
                require_distinct_opening=False,
                detect_shared_structure=True,
            )

        app.validate_evaluation_description_novelty(
            {"delivery": {"score": 5, "description": candidate}},
            history,
            require_distinct_opening=False,
        )

    def test_history_similarity_errors_are_retryable_without_changing_score(self):
        self.assertTrue(
            app.retryable_review_output_error(
                "自动检查的交付完整性描述与历史点评 SOLO-QA #7001 高度重复"
            )
        )

    def test_full_score_rejects_unfinished_verification(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = (
            "第 1 轮核对了 app.py 的接口结果，但尚未完成浏览器复验。"
        )

        with self.assertRaisesRegex(app.WorkflowError, "满分描述包含扣分点"):
            app.normalize_evaluation(evaluation, 1)

    def test_full_score_allows_expected_business_error_feedback(self):
        evaluation = sample_evaluation()
        evaluation["instruction_following"]["description"] = (
            "第 1 轮核对了未知编号返回 404、重复确认返回 409，"
            "最终 18 项接口检查通过。"
        )

        normalized = app.normalize_evaluation(evaluation, 1)

        self.assertEqual(normalized["instruction_following"]["score"], 5)

    def test_full_score_allows_validation_error_labels_and_refresh_recovery(self):
        evaluation = sample_evaluation()
        evaluation["instruction_following"]["description"] = (
            "第 1 轮执行 npm run verify 后 60 项检查全部通过；页面主流程验证了"
            "连续换位、校验错误随内容移动、刷新后按换位顺序恢复及合格打印。"
        )

        normalized = app.normalize_evaluation(evaluation, 1)

        self.assertEqual(normalized["instruction_following"]["score"], 5)

    def test_full_score_still_rejects_an_actual_error_then_fix(self):
        evaluation = sample_evaluation()
        evaluation["execution"]["description"] = (
            "第 1 轮错误修改 App.tsx 后又恢复原逻辑，最终 12 项检查通过。"
        )

        with self.assertRaisesRegex(app.WorkflowError, "满分描述包含扣分点"):
            app.normalize_evaluation(evaluation, 1)

    def test_trace_grounding_rejects_repeated_read_without_count(self):
        evaluation = sample_evaluation()
        evaluation["execution"] = {
            "score": 4,
            "description": (
                "第 1 轮重复读取 tests/e2e/example.spec.ts，造成额外操作。"
                "随后检查该文件并完成验证，因此增加了处理时间。"
            ),
        }
        trajectory = (
            'TOOL Read: {"path": "tests/e2e/example.spec.ts"}\n'
            'TOOL RESULT: source text'
        )

        with self.assertRaisesRegex(app.WorkflowError, "没有写明.*次数"):
            app.validate_evaluation_trace_grounding(evaluation, trajectory)

    def test_trace_grounding_rejects_invented_full_score_count(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = (
            "第 1 轮核对了报价接口和持久化结果，最终 999 项检查通过。"
        )

        with self.assertRaisesRegex(app.WorkflowError, "无法在本轮轨迹.*找到：999"):
            app.validate_evaluation_trace_grounding(
                evaluation,
                'TOOL RESULT: 18 passed',
            )

    def test_trace_grounding_accepts_full_score_count_from_verification(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = (
            "第 1 轮核对了报价接口和持久化结果，最终 18 项检查通过。"
        )

        app.validate_evaluation_trace_grounding(
            evaluation,
            "",
            [{"output": "18 passed", "exit_code": 0}],
        )

    def test_trace_grounding_does_not_treat_turn_or_step_ordinal_as_a_count(self):
        evaluation = sample_evaluation()
        evaluation["planning"] = {
            "score": 4,
            "description": (
                "“完成真实导出链路”在第 1 轮第 1 步规划时没有拆分提交前检查，"
                "导致最终回复前缺少阶段记录。保存结果显示“已经完成”，"
                "因此该遗漏只造成过程依据不完整，没有影响最终结果。"
            ),
        }
        trajectory = (
            "USER[1] promptId=prompt-export: 完成真实导出链路\n"
            "ASSISTANT FINAL: 已经完成。"
        )

        issues = app.evaluation_trace_grounding_issues(
            evaluation, trajectory, []
        )

        self.assertEqual(issues, [])

    def test_trace_grounding_accepts_anchor_from_review_evidence_field(self):
        evaluation = sample_evaluation()
        evaluation["reasoning"] = {
            "score": 4,
            "description": (
                "后续独立复核发现，第 1 轮边界复现没有及时返回，"
                "并记录“probe_timeout_after_2s”。"
                "该结果导致页面输入路径需要补充常数时间拦截。"
            ),
        }

        issues = app.evaluation_trace_grounding_issues(
            evaluation,
            "",
            supplemental_evidence="复现输出 probe_timeout_after_2s",
        )

        self.assertEqual(issues, [])

    def test_trace_grounding_requires_source_for_number_only_in_review_evidence(self):
        evaluation = sample_evaluation()
        evaluation["execution"] = {
            "score": 4,
            "description": (
                "第 1 轮提交时有约 3505 个 node_modules 文件，并缺少 "
                "@rollup/rollup-darwin-arm64。这个结果造成依赖目录无法直接复用。"
            ),
        }

        issues = app.evaluation_trace_grounding_issues(
            evaluation,
            "ASSISTANT FINAL: 已经完成。",
            supplemental_evidence=(
                "产物盘点：3505 个 node_modules 文件，"
                "缺少 @rollup/rollup-darwin-arm64"
            ),
        )

        self.assertTrue(
            any("执行能力描述引用后续独立复核证据但没有注明来源" in issue for issue in issues),
            issues,
        )

    def test_trace_grounding_accepts_attributed_review_only_number(self):
        evaluation = sample_evaluation()
        evaluation["execution"] = {
            "score": 4,
            "description": (
                "后续产物检查显示，第 1 轮提交中有约 3505 个 node_modules 文件，"
                "并缺少 @rollup/rollup-darwin-arm64。这个结果造成依赖目录无法直接复用。"
            ),
        }

        issues = app.evaluation_trace_grounding_issues(
            evaluation,
            "ASSISTANT FINAL: 已经完成。",
            supplemental_evidence=(
                "产物盘点：3505 个 node_modules 文件，"
                "缺少 @rollup/rollup-darwin-arm64"
            ),
        )

        self.assertEqual(issues, [])

    def test_trace_grounding_does_not_require_source_for_original_trace_number(self):
        evaluation = sample_evaluation()
        evaluation["execution"] = {
            "score": 4,
            "description": (
                "第 1 轮检查输出记录 2679 个文件，提交范围因此需要收窄。"
                "这个遗漏造成仓库体积增加。"
            ),
        }
        trajectory = (
            'TOOL Bash: {"command": "find .venv -type f"}\n'
            "TOOL RESULT: 2679 个文件\n"
        )

        issues = app.evaluation_trace_grounding_issues(
            evaluation,
            trajectory,
            supplemental_evidence="后续复核也记录 2679 个文件",
        )

        self.assertFalse(
            any("引用后续独立复核证据" in issue for issue in issues),
            issues,
        )

    def test_review_evidence_does_not_count_as_direct_tool_output(self):
        evaluation = sample_evaluation()
        evaluation["planning"] = {
            "score": 4,
            "description": (
                "第 1 轮修改 stack.spec.ts 时没有预先列出入口隔离检查。"
                "后续独立复核发现，场景因复用“phantom_probe”辅助函数而需要"
                "回头修正，造成返工。"
            ),
        }

        issues = app.evaluation_trace_grounding_issues(
            evaluation,
            'TOOL Read: {"path": "stack.spec.ts"}\nTOOL RESULT: source text',
            supplemental_evidence="复核证据记录 phantom_probe",
        )

        self.assertTrue(any("辅助函数因果判断缺少" in issue for issue in issues))

    def test_completed_turn_policy_accepts_count_from_saved_turn_result(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = (
            "第 1 轮核对了借阅接口和持久化结果，最终 72 项检查通过。"
        )
        row = {
            "turn_number": 1,
            "turn_trajectory_path": "",
            "run_trajectory_path": "",
            "turn_prompt": "实现借阅接口。",
            "turn_result": "修复完成，72 个测试全部通过。",
            "turn_verification": "[]",
        }

        app.validate_evaluation_trace_grounding(
            evaluation,
            "",
            {
                "prompt": row["turn_prompt"],
                "result": row["turn_result"],
                "verification": row["turn_verification"],
            },
        )

    def test_trace_grounding_rejects_repeated_read_count_not_in_trace(self):
        evaluation = sample_evaluation()
        evaluation["execution"] = {
            "score": 4,
            "description": (
                "第 1 轮连续 3 次读取 tests/e2e/example.spec.ts，造成额外操作。"
                "读取实际内容后才完成验证，因此增加了处理时间。"
            ),
        }
        trajectory = (
            'TOOL Read: {"path": "tests/e2e/example.spec.ts"}\n'
            'TOOL RESULT: source text\n'
            'TOOL Read: {"path": "tests/e2e/example.spec.ts"}\n'
            'TOOL RESULT: source text'
        )

        with self.assertRaisesRegex(app.WorkflowError, "只定位到 2 次调用"):
            app.validate_evaluation_trace_grounding(evaluation, trajectory)

    def test_trace_grounding_rejects_inferred_state_clear_without_output(self):
        evaluation = sample_evaluation()
        evaluation["reasoning"] = {
            "score": 4,
            "description": (
                "第 1 轮在 AssemblyVerify.tsx 的隔离场景中判断测试会导致状态被清空。"
                "页面断言显示实际为 0，因此需要重新定位。"
            ),
        }
        trajectory = (
            'TOOL Read: {"path": "AssemblyVerify.tsx"}\n'
            'TOOL RESULT: component source\n'
            'TOOL Bash: {"command": "run browser checks"}\n'
            'TOOL RESULT: result-row expected 1, received 0'
        )

        with self.assertRaisesRegex(app.WorkflowError, "状态因果判断缺少"):
            app.validate_evaluation_trace_grounding(evaluation, trajectory)

    def test_trace_grounding_rejects_helper_cause_without_direct_error(self):
        evaluation = sample_evaluation()
        evaluation["planning"] = {
            "score": 4,
            "description": (
                "第 1 轮修改 stack.spec.ts 时没有预先列出入口隔离检查。"
                "两条场景因复用 gotoAssembly 辅助函数而需要回头修正，造成一次返工。"
            ),
        }
        trajectory = (
            'TOOL Read: {"path": "stack.spec.ts"}\n'
            'TOOL RESULT: source contains gotoAssembly\n'
            'TOOL Bash: {"command": "run browser checks"}\n'
            'TOOL RESULT: 2 failed, 31 passed'
        )

        with self.assertRaisesRegex(app.WorkflowError, "辅助函数因果判断缺少"):
            app.validate_evaluation_trace_grounding(evaluation, trajectory)

    def test_trace_grounding_rejects_architecture_claim_without_direct_output(self):
        evaluation = sample_evaluation()
        evaluation["execution"] = {
            "score": 4,
            "description": (
                "第 1 轮下载了 amd64 包到 arm64 环境，导致一次无效尝试。"
                "随后重新选择依赖，因此增加了处理步骤。"
            ),
        }
        trajectory = (
            'TOOL Bash: {"command": "download package"}\n'
            'TOOL RESULT: download complete'
        )

        with self.assertRaisesRegex(app.WorkflowError, "架构判断缺少"):
            app.validate_evaluation_trace_grounding(evaluation, trajectory)

    def test_evaluation_descriptions_reject_high_risk_public_fragments(self):
        for phrase in app.EVALUATION_HIGH_RISK_FRAGMENTS:
            evaluation = sample_evaluation()
            evaluation["execution"]["description"] = f"处理完成，{phrase}。"
            with self.subTest(phrase=phrase), self.assertRaisesRegex(
                app.WorkflowError, "高风险公共片段"
            ):
                app.normalize_evaluation(evaluation)

    def test_evaluation_description_accepts_groundable_command_fragment(self):
        evaluation = sample_evaluation()
        evaluation["execution"]["description"] = (
            "组件测试通过，随后运行 `Docker-Compose CONFIG --quiet` 检查配置。"
        )

        normalized = app.normalize_evaluation(evaluation)

        self.assertIn("Docker-Compose CONFIG --quiet", normalized["execution"]["description"])
        self.assertNotIn("`", normalized["execution"]["description"])

    def test_normal_success_phrases_are_allowed_and_backticks_are_removed(self):
        evaluation = sample_evaluation()
        evaluation["execution"]["description"] = (
            "后续独立验收执行 `npm run build`，生产构建成功，43 项检查全部通过。"
        )

        normalized = app.normalize_evaluation(evaluation)

        description = normalized["execution"]["description"]
        self.assertIn("npm run build", description)
        self.assertIn("生产构建成功", description)
        self.assertIn("全部通过", description)
        self.assertNotIn("`", description)

    def test_effective_unsubmitted_evaluation_removes_legacy_backticks(self):
        evaluation = sample_evaluation()
        evaluation["reasoning"]["description"] = (
            "第 1 轮核对输入 `1e100000000`，边界结果有对应记录。"
        )
        row = {
            "turn_review_result": json.dumps(
                {"evaluation": evaluation}, ensure_ascii=False
            ),
            "turn_manual_evaluation": "",
            "solo_qa_remote_submission_id": "",
        }

        effective = app.turn_evaluation(row)

        self.assertIn("“1e100000000”", effective["reasoning"]["description"])
        self.assertNotIn("`", effective["reasoning"]["description"])

    def test_effective_submitted_evaluation_also_removes_backticks(self):
        evaluation = sample_evaluation()
        evaluation["reasoning"]["description"] = "核对了 `app.py` 的结果。"
        row = {
            "turn_review_result": json.dumps(
                {"evaluation": evaluation}, ensure_ascii=False
            ),
            "turn_manual_evaluation": "",
            "solo_qa_remote_submission_id": "6009",
        }

        effective = app.turn_evaluation(row)

        self.assertIn("“app.py”", effective["reasoning"]["description"])
        self.assertNotIn("`", effective["reasoning"]["description"])

    def test_completed_turn_policy_grounds_backticked_input_in_independent_review(self):
        evaluation = sample_evaluation()
        evaluation["instruction_following"] = {
            "score": 4,
            "description": "第 1 轮存在具体不足。该问题造成了实际影响。",
        }
        evaluation["reasoning"]["description"] = (
            "后续独立复核发现，第 1 轮输入 `1e100000000` 的边界结果有对应记录。"
        )
        with tempfile.TemporaryDirectory() as directory:
            trajectory_path = Path(directory) / "turn.jsonl"
            trajectory_path.write_text(
                "USER[1]: 检查金额边界\nASSISTANT FINAL: 已完成检查。\n",
                encoding="utf-8",
            )
            row = {
                "turn_number": 1,
                "turn_trajectory_path": str(trajectory_path),
                "turn_prompt": "检查金额边界",
                "turn_result": "已完成检查。",
                "turn_verification": "",
                "turn_prompt_id": "",
                "turn_review_result": json.dumps(
                    {
                        "bugs": [
                            {
                                "evidence": (
                                    "独立复核输入 1e100000000 后确认边界问题。"
                                )
                            }
                        ],
                        "evaluation": evaluation,
                    },
                    ensure_ascii=False,
                ),
            }

            issues = app.completed_turn_evaluation_policy_issues(row, evaluation)

        self.assertFalse(
            any("1e100000000" in issue for issue in issues),
            issues,
        )
        self.assertTrue(any("没有把不足定位" in issue for issue in issues), issues)

    def test_turn_review_grounding_evidence_excludes_evaluation(self):
        row = {
            "turn_review_result": json.dumps(
                {
                    "summary": "独立复核事实",
                    "bugs": [
                        {
                            "evidence": "probe_timeout_after_2s",
                            "expected": "expected_only_anchor",
                            "fix": "fix_only_anchor",
                        }
                    ],
                    "quality_gaps": [
                        {
                            "evidence": "gap_evidence_anchor",
                            "recommendation": "recommendation_only_anchor",
                        }
                    ],
                    "evaluation": {
                        "reasoning": {"description": "score_only_anchor"}
                    },
                    "evaluation_warning": "warning_only_anchor",
                },
                ensure_ascii=False,
            )
        }

        evidence = app.turn_review_grounding_evidence(row)
        encoded = json.dumps(evidence, ensure_ascii=False)

        self.assertIn("probe_timeout_after_2s", encoded)
        self.assertIn("gap_evidence_anchor", encoded)
        self.assertNotIn("独立复核事实", encoded)
        self.assertNotIn("expected_only_anchor", encoded)
        self.assertNotIn("fix_only_anchor", encoded)
        self.assertNotIn("recommendation_only_anchor", encoded)
        self.assertNotIn("score_only_anchor", encoded)
        self.assertNotIn("warning_only_anchor", encoded)

    def test_trajectory_evidence_keeps_multiline_tool_result(self):
        trajectory = (
            'TOOL Bash: {"command": "run browser checks"}\n'
            "TOOL RESULT: 1 failed\n"
            "    Expected: <= 725\n"
            "    Received: 792\n"
            "ASSISTANT: 修正后继续检查。\n"
        )

        tool_text, result_text, call_lines = app.trajectory_evaluation_evidence(
            trajectory
        )

        self.assertIn("Expected: <= 725", result_text)
        self.assertIn("Received: 792", result_text)
        self.assertNotIn("修正后继续检查", result_text)
        self.assertIn("Received: 792", tool_text)
        self.assertEqual(len(call_lines), 1)

    def test_trace_grounding_reads_numbers_from_multiline_tool_result(self):
        evaluation = sample_evaluation()
        evaluation["reasoning"]["description"] = (
            "页面断言失败，输出 Expected 725 和 Received 792。"
        )
        trajectory = (
            'TOOL Bash: {"command": "run browser checks"}\n'
            "TOOL RESULT: 1 failed\n"
            "    Expected: <= 725\n"
            "    Received: 792\n"
        )

        issues = app.evaluation_trace_grounding_issues(evaluation, trajectory)

        self.assertFalse(
            any("725" in issue or "792" in issue for issue in issues),
            issues,
        )

    def test_trace_grounding_still_rejects_number_only_inferred_from_output(self):
        evaluation = sample_evaluation()
        evaluation["reasoning"]["description"] = (
            "页面断言失败，输出 clientWidth 724 和 Received 792。"
        )
        trajectory = (
            'TOOL Bash: {"command": "run browser checks"}\n'
            "TOOL RESULT: 1 failed\n"
            "    Expected: <= 725\n"
            "    Received: 792\n"
        )

        issues = app.evaluation_trace_grounding_issues(evaluation, trajectory)

        self.assertTrue(any(issue.endswith("724") for issue in issues), issues)

    def test_solo_qa_digest_is_stable_when_remote_id_is_added_after_cleanup(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = "核对了 `app.py` 的交付结果。"
        row = {
            "turn_review_result": json.dumps(
                {"evaluation": evaluation}, ensure_ascii=False
            ),
            "turn_manual_evaluation": "",
            "turn_count": 1,
            "intent_type": "0-1 代码生成",
            "run_task_difficulty": "困难",
            "run_language_framework": "Python",
            "harness_version": "2.1.269",
            "turn_number": 1,
            "turn_trajectory_sha256": "a" * 64,
            "solo_qa_remote_submission_id": "",
        }

        before_submit = app.solo_qa_payload_sha256(row)
        row["solo_qa_remote_submission_id"] = "6009"
        after_submit = app.solo_qa_payload_sha256(row)

        self.assertEqual(before_submit, after_submit)

    def test_solo_qa_state_accepts_legacy_submitted_backtick_digest(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = "核对了 `app.py` 的交付结果。"
        row = {
            "turn_review_result": json.dumps(
                {"evaluation": evaluation}, ensure_ascii=False
            ),
            "turn_manual_evaluation": "",
            "turn_count": 1,
            "intent_type": "0-1 代码生成",
            "run_task_difficulty": "困难",
            "run_language_framework": "Python",
            "harness_version": "2.1.269",
            "turn_number": 1,
            "turn_trajectory_sha256": "a" * 64,
            "solo_qa_remote_submission_id": "6009",
            "solo_qa_state": "qc_pending",
        }
        row["solo_qa_payload_sha256"] = app.solo_qa_payload_sha256(
            row, clean_description_markup=False
        )

        summary = app.solo_qa_state_summary(row, True)

        self.assertEqual(summary["state"], "qc_pending")
        self.assertFalse(summary["payload_changed"])

    def test_evaluation_command_anchor_must_exist_in_trace_tool_calls(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = (
            "依赖安装后执行 `npm ci`，随后检查页面行为。"
        )
        trajectory = 'TOOL Bash: {"command": "npm install && npm test"}'

        with self.assertRaisesRegex(app.WorkflowError, "未执行的命令：npm ci"):
            app.validate_evaluation_trace_commands(evaluation, trajectory)

    def test_single_command_anchor_survives_backtick_cleanup(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = "执行 `pytest` 后核对结果。"
        normalized = app.normalize_evaluation(evaluation)

        with self.assertRaisesRegex(app.WorkflowError, "未执行的命令：pytest"):
            app.validate_evaluation_trace_commands(normalized, "")

        app.validate_evaluation_trace_commands(
            normalized,
            'TOOL Bash: {"command": "pytest"}',
        )

    def test_result_count_after_tool_name_is_not_treated_as_command(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = (
            "轨迹记录 Vitest 31 项与 Playwright 9 个场景通过。"
        )

        app.validate_evaluation_trace_commands(evaluation, "")

    def test_novelty_allows_shared_file_path_as_factual_anchor(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = (
            "本轮在 frontend/e2e/app.spec.ts 增加指数输入场景，并核对合法重量恢复。"
            "页面记录了成品第一笔的提示，测试结果完整。"
        )
        history = {
            "delivery": [{
                "reference": "旧记录",
                "description": (
                    "称重导入在 frontend/e2e/app.spec.ts 覆盖文件选择和取消。"
                    "这次检查针对 CSV 预览，业务目标与本轮不同。"
                ),
            }]
        }

        app.validate_evaluation_description_novelty(
            evaluation,
            history,
            require_distinct_opening=False,
        )

    def test_evaluation_command_anchor_accepts_executed_command_and_shorter_reference(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = (
            "先执行 `npm install`，最后用 `npm test` 检查改动。"
        )
        trajectory = (
            'TOOL Bash: {"command": "npm install"}\n'
            'CALL Bash: {"command": "npm test -- --run"}'
        )

        app.validate_evaluation_trace_commands(evaluation, trajectory)

    def test_evaluation_command_anchor_accepts_attributed_independent_check(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = (
            "后续独立验收执行 `make test`，接口场景全部通过。"
        )

        app.validate_evaluation_trace_commands(
            evaluation,
            "",
            [{"command": "make test", "exit_code": 0, "output": "ok"}],
        )

    def test_evaluation_command_anchor_requires_independent_check_attribution(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = "执行 `make test` 后接口场景全部通过。"

        with self.assertRaisesRegex(app.WorkflowError, "后续验收命令但没有注明来源"):
            app.validate_evaluation_trace_commands(
                evaluation,
                "",
                [{"command": "make test", "exit_code": 0, "output": "ok"}],
            )

    def test_skipped_verification_command_is_not_treated_as_executed(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = (
            "后续独立验收执行 `docker compose run --rm verify`，接口场景全部通过。"
        )
        verification = [
            {
                "command": "docker compose run --rm verify",
                "exit_code": -2,
                "output": "前置 docker compose build 未成功；本条验收未执行。",
                "skipped": True,
            }
        ]

        with self.assertRaisesRegex(
            app.WorkflowError,
            "未执行的命令：docker compose run --rm verify",
        ):
            app.validate_evaluation_trace_commands(evaluation, "", verification)

    def test_false_success_requires_user_facing_completion_claim(self):
        evaluation = sample_evaluation()
        evaluation["delivery"] = {
            "score": 3,
            "description": (
                "第 1 轮在 app.py 中形成虚假成功，回复宣称已经修复，"
                "但实际接口仍返回 500。这个差异导致请求不能完成。"
            ),
        }

        with self.assertRaisesRegex(app.WorkflowError, "没有面向使用者的实际完成声明"):
            app.validate_false_success_claim(
                evaluation,
                'TOOL Bash: {"command": "python -m pytest"}\nTOOL RESULT: 1 failed',
            )

    def test_false_success_accepts_completion_claim_compared_with_output(self):
        evaluation = sample_evaluation()
        evaluation["delivery"] = {
            "score": 3,
            "description": (
                "第 1 轮在 app.py 中形成虚假成功，回复宣称“已经修复”，"
                "但实际接口仍返回 500。这个差异导致请求不能完成。"
            ),
        }
        trajectory = (
            "ASSISTANT FINAL: 已经修复并完成交付\n"
            'TOOL Bash: {"command": "python -m pytest"}\n'
            "TOOL RESULT: 1 failed"
        )

        app.validate_false_success_claim(evaluation, trajectory)

    def test_full_score_accepts_expected_error_rejection(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = (
            "检查 `/orders` 的错误输入返回 422，接口按题面约束拒绝保存；"
            "正常输入的创建结果也验证通过。"
        )

        normalized = app.normalize_evaluation(evaluation)

        self.assertEqual(normalized["delivery"]["score"], 5)

    def test_full_score_uses_contract_context_instead_of_negative_keyword(self):
        evaluation = sample_evaluation()
        evaluation["reasoning"]["description"] = (
            "检查 `normalize_input()` 时，错误输入会按题面约束在保存前修复为规范格式；"
            "正常输入也验证通过。"
        )

        normalized = app.normalize_evaluation(evaluation)

        self.assertEqual(normalized["reasoning"]["score"], 5)

    def test_final_verification_facts_keep_latest_result_per_suite(self):
        trajectory = (
            'TOOL Bash: {"command": "python -m pytest -q"}\n'
            'TOOL RESULT: 1 failed, 40 passed in 2.1s\n'
            'TOOL Bash: {"command": "python -m pytest -q && npm test"}\n'
            'TOOL RESULT: 41 passed in 2.0s\n'
            ' Test Files  1 passed (1)\n'
            '      Tests  22 passed (22)\n'
            'TOOL Bash: {"command": "npx playwright test"}\n'
            'TOOL RESULT: 3 failed\n'
            '10 passed (20.0s)\n'
            'TOOL Bash: {"command": "npx playwright test"}\n'
            'TOOL RESULT: 13 passed (11.0s)'
        )

        facts = {
            fact["scope"]: fact
            for fact in app.trajectory_final_verification_facts(trajectory)
        }

        self.assertEqual(
            (facts["backend"]["passed"], facts["backend"]["failed"]),
            (41, 0),
        )
        self.assertTrue(facts["backend"]["had_earlier_failure"])
        self.assertEqual(
            (facts["frontend"]["passed"], facts["frontend"]["failed"]),
            (22, 0),
        )
        self.assertEqual(
            (facts["browser"]["passed"], facts["browser"]["failed"]),
            (13, 0),
        )
        self.assertTrue(facts["browser"]["had_earlier_failure"])

    def test_evaluation_rejects_failure_claim_superseded_by_later_pass(self):
        evaluation = sample_evaluation()
        evaluation["planning"] = {
            "score": 4,
            "description": (
                "第 1 轮的 backend/tests/test_api.py 最终仍有 1 项失败。"
                "这导致后端缺少修正后的复验结果。"
            ),
        }
        trajectory = (
            'TOOL Bash: {"command": "python -m pytest -q"}\n'
            'TOOL RESULT: 1 failed, 40 passed in 2.1s\n'
            'TOOL Bash: {"command": "python -m pytest -q"}\n'
            'TOOL RESULT: 41 passed in 2.0s'
        )

        with self.assertRaisesRegex(
            app.WorkflowError, "与本轮最后一次检查结果矛盾"
        ):
            app.validate_evaluation_final_verification_consistency(
                evaluation, trajectory
            )

    def test_evaluation_allows_negated_final_failure_after_passing_checks(self):
        evaluation = sample_evaluation()
        evaluation["execution"] = {
            "score": 4,
            "description": (
                "第 1 轮在 backend/tests/test_api.py 修正参数后完成检查。"
                "最终记录后端 45 项、前端 37 项全部通过，未造成最终检查失败。"
            ),
        }
        trajectory = (
            'TOOL Bash: {"command": "python -m pytest"}\n'
            "TOOL RESULT: 45 passed\n"
            'TOOL Bash: {"command": "npm test"}\n'
            "TOOL RESULT: 37 passed\n"
        )

        app.validate_evaluation_final_verification_consistency(
            evaluation, trajectory
        )

    def test_evaluation_accepts_recovered_failure_and_real_final_failure(self):
        recovered = sample_evaluation()
        recovered["execution"] = {
            "score": 4,
            "description": (
                "第 1 轮调整 backend/tests/test_api.py 后仍有 1 项失败。"
                "最后一次才完成 41 项检查，因此该问题已经恢复。"
            ),
        }
        recovered_trajectory = (
            'TOOL Bash: {"command": "python -m pytest -q"}\n'
            'TOOL RESULT: 1 failed, 40 passed in 2.1s\n'
            'TOOL Bash: {"command": "python -m pytest -q"}\n'
            'TOOL RESULT: 41 passed in 2.0s'
        )
        app.validate_evaluation_final_verification_consistency(
            recovered, recovered_trajectory
        )

        still_failing = sample_evaluation()
        still_failing["execution"] = {
            "score": 4,
            "description": (
                "第 1 轮的 frontend/tests/App.test.tsx 最终仍有 1 项失败。"
                "这导致页面行为没有得到完整复验。"
            ),
        }
        failing_trajectory = (
            'TOOL Bash: {"command": "npm test"}\n'
            'TOOL RESULT: Test Files  1 failed (1)\n'
            'Tests  1 failed | 9 passed (10)'
        )
        app.validate_evaluation_final_verification_consistency(
            still_failing, failing_trajectory
        )

    def test_final_verification_contradiction_is_retryable(self):
        self.assertTrue(
            app.retryable_review_output_error(
                "自动检查的执行能力描述与本轮最后一次检查结果矛盾"
            )
        )

    def test_review_output_keeps_attributed_independent_command(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = (
            "后续独立验收执行 `make test` 后确认接口用例通过。"
        )
        trajectory = 'TOOL Bash: {"command": "python -m pytest"}'

        app.validate_evaluation_trace_commands(
            evaluation,
            trajectory,
            [{"command": "make test", "exit_code": 0, "output": "ok"}],
        )

        self.assertIn("`make test`", evaluation["delivery"]["description"])

    def test_execution_description_accepts_command_when_present_in_trace(self):
        evaluation = sample_evaluation()
        evaluation["execution"]["description"] = "最后执行 `make test`，检查了交接流程。"

        normalized = app.normalize_evaluation(evaluation)
        app.validate_evaluation_trace_commands(
            normalized,
            'TOOL Bash: {"command": "make test"}',
        )

    def test_delivery_copy_uses_the_exact_requested_fields_in_order(self):
        source = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        block = source.split("function turnDeliveryRows", 1)[1].split(
            "function buildDeliveryText", 1
        )[0]
        labels = re.findall(r'^\s*\["([^"]+)",', block, re.MULTILINE)
        self.assertEqual(
            labels,
            [
                "User Prompt",
                "SessionID",
                "TurnID/PromptID",
                "当前对话轮次排序",
                "本轮 Git Commit",
                "初始环境快照",
                "轨迹文件",
                "环境可复现等级",
                "Harness",
                "Harness 版本",
                "操作系统",
                "任务类型",
                "任务难度",
                "语言/框架",
                "交付完整性",
                "交付完整性 - 描述",
                "指令遵循",
                "指令遵循 - 描述",
                "任务规划",
                "任务规划 - 描述",
                "推理能力",
                "推理能力 - 描述",
                "执行能力",
                "执行能力 - 描述",
                "其他问题",
                "提交人",
            ],
        )
        self.assertEqual(app.DELIVERY_EXPORT_COLUMNS[:2], ("编号", "项目 / 仓库"))
        self.assertEqual(tuple(labels), app.DELIVERY_EXPORT_COLUMNS[2:])

    def test_export_page_only_syncs_solo_qa_history_on_manual_request(self):
        source = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        bridge_ready = source.split(
            'if (message.type === "SOLO_QA_BRIDGE_READY")', 1
        )[1].split(
            'if (!["SOLO_QA_BRIDGE_RESULT"', 1
        )[0]

        self.assertNotIn("syncSoloQa", bridge_ready)
        self.assertIn("同步只读取北京时间今天的提交", source)
        self.assertIn("autoRepairSyncedSoloQaReturns", source)
        self.assertIn("retry_failed: true", source)
        self.assertIn("的远端提交", source)
        self.assertIn("当天数据超过 500 条", source)

    def test_run_list_exposes_filters_delete_and_export_routes(self):
        html = (app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        for control in (
            'id="run-filter-query"',
            'id="run-filter-task-type"',
            'id="run-filter-category"',
            'id="run-filter-status"',
            'id="open-export-page"',
            'id="export-view"',
        ):
            self.assertIn(control, html)
        self.assertIn('method: "DELETE"', javascript)
        self.assertIn('/api/exports/turns.xlsx', javascript)
        self.assertIn('/api/runs/background-jobs', javascript)
        self.assertIn('run.background_generation', javascript)
        self.assertIn('background-job-stage', javascript)

    def test_run_and_export_lists_have_twenty_item_dual_pagination(self):
        html = (app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        styles = (app.STATIC_DIR / "styles.css").read_text(encoding="utf-8")

        self.assertIn("const TABLE_PAGE_SIZE = 20", javascript)
        self.assertEqual(html.count('data-table-pagination="runs"'), 2)
        self.assertEqual(html.count('data-table-pagination="exports"'), 2)
        self.assertIn("function paginateItems", javascript)
        self.assertIn("pagination.items.map((run)", javascript)
        self.assertIn("pagination.items.map((turn)", javascript)
        self.assertIn("state.runPage = 1", javascript)
        self.assertIn("state.exportPage = 1", javascript)
        self.assertIn(".table-pagination-top", styles)
        self.assertIn(".table-pagination-bottom", styles)

    def test_frontend_and_backend_versions_stay_in_sync(self):
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        match = re.search(r'^const UI_VERSION = "([^"]+)";', javascript)

        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), app.APP_VERSION)

    def test_run_list_shows_start_time_without_framework_or_model_columns(self):
        html = (app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        table = html.split('<table class="records-table">', 1)[1].split(
            "</table>", 1
        )[0]
        renderer = javascript.split("function renderRunList()", 1)[1].split(
            "function progressState", 1
        )[0]

        self.assertIn('data-sort-key="created_at"', table)
        self.assertIn("开始时间", table)
        self.assertNotIn("<th>语言 / 框架</th>", table)
        self.assertNotIn("<th>模型</th>", table)
        self.assertIn('data-label="开始时间"', renderer)
        self.assertIn("run.created_at", renderer)
        self.assertNotIn('data-label="语言 / 框架"', renderer)
        self.assertNotIn('data-label="模型"', renderer)
        self.assertIn('colspan="8"', table)
        self.assertIn('colspan="8"', renderer)

    def test_run_and_export_times_use_high_contrast_date_and_clock_blocks(self):
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        styles = (app.STATIC_DIR / "styles.css").read_text(encoding="utf-8")

        self.assertIn("function renderTableTimestamp", javascript)
        self.assertIn('renderTableTimestamp(run.created_at, "started")', javascript)
        self.assertIn('renderTableTimestamp(run.updated_at || run.created_at, "updated")', javascript)
        self.assertIn('renderTableTimestamp(turn.completed_at, "completed")', javascript)
        self.assertIn(".table-timestamp .timestamp-clock", styles)
        self.assertIn("font-size: 14px", styles)
        self.assertIn(".table-timestamp.updated", styles)
        self.assertIn(".table-timestamp.completed", styles)

    def test_primary_navigation_has_three_tabs_and_hourly_analytics(self):
        html = (app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        styles = (app.STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        self.assertIn('role="tablist"', html)
        for control in (
            'id="module-tab-runs"',
            'id="open-export-page"',
            'id="open-analytics-page"',
            'id="analytics-view"',
            'id="analytics-date"',
            'id="analytics-chart-bar"',
            'id="analytics-chart-line"',
            'id="analytics-type-baseline-count"',
            'id="analytics-type-feature-count"',
            'id="analytics-type-bugfix-count"',
            'id="analytics-type-other-count"',
            'id="hourly-output-chart"',
            'id="hourly-chart-tooltip"',
            'id="hourly-output-list"',
            'id="difficulty-reassessment-date"',
            'id="difficulty-reassessment-low-only"',
            'id="difficulty-reassessment-start"',
            'id="difficulty-reassessment-list"',
            'id="difficulty-reassessment-apply"',
        ):
            self.assertIn(control, html)
        self.assertIn("#analytics", javascript)
        self.assertIn("/api/analytics/hourly-output", javascript)
        self.assertIn("renderHourlyAnalytics", javascript)
        self.assertIn("analyticsTaskTypes.forEach", javascript)
        self.assertIn("renderHourlyChart", javascript)
        self.assertIn("showHourlyChartTooltip", javascript)
        self.assertIn("/api/analytics/difficulty-reassessment", javascript)
        self.assertIn("startDifficultyReassessment", javascript)
        self.assertIn("applySelectedDifficultyReassessment", javascript)
        self.assertIn(".module-tab.active", styles)
        self.assertIn(".hourly-chart", styles)
        self.assertIn(".analytics-line-chart", styles)
        self.assertIn(".analytics-tooltip", styles)
        self.assertIn(".difficulty-reassessment-card", styles)

    def test_run_list_exposes_imported_baseline_dialog(self):
        html = (app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        for control in (
            'id="open-import-baseline"',
            'id="import-baseline-dialog"',
            'id="import-project-numbers"',
            'id="import-project-directory"',
        ):
            self.assertIn(control, html)
        self.assertIn('/api/runs/import-baselines-by-number', javascript)
        self.assertIn('run.imported_baseline', javascript)

    def test_export_page_exposes_filters_and_soft_delete_actions(self):
        html = (app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        for control in (
            'id="export-filter-query"',
            'id="export-filter-task-type"',
            'id="export-filter-difficulty"',
            'id="export-filter-readiness"',
            'id="export-filter-solo-qa"',
            'id="export-filter-date-from"',
            'id="export-filter-date-to"',
            'id="delete-selected-export-turns"',
        ):
            self.assertIn(control, html)
        self.assertIn("filteredCompletedTurns", javascript)
        self.assertIn("deleteExportTurns", javascript)
        self.assertIn("/api/exports/turns/delete", javascript)

    def test_export_page_can_expand_each_turn_prompt(self):
        html = (app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        styles = (app.STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        self.assertIn("<th><span class=\"sr-only\">展开题面与评分</span></th>", html)
        self.assertIn("expandedExportPrompts: new Set()", javascript)
        self.assertIn('data-export-prompt-key="${escapeHtml(turn.key)}"', javascript)
        self.assertIn('class="export-prompt-row"', javascript)
        self.assertIn("escapeHtml(turn.prompt", javascript)
        self.assertIn("exportEvaluationEditorHtml(turn)", javascript)
        self.assertIn("/api/exports/turns/evaluation", javascript)
        self.assertIn("保存后，Excel 导出和 SOLO-QA 提交均使用这里的内容", javascript)
        self.assertIn(".export-prompt-toggle", styles)
        self.assertIn(".export-prompt-content", styles)
        self.assertIn(".export-evaluation-editor", styles)
        self.assertIn("const EXPORT_REFRESH_INTERVAL_MS = 60 * 1000", javascript)
        self.assertIn(
            "async function loadCompletedTurns({ autoRepair = true, force = true } = {})",
            javascript,
        )
        self.assertIn("await loadCompletedTurns({ force: false })", javascript)
        self.assertIn(
            "Date.now() - state.exportLastLoadedAt >= EXPORT_REFRESH_INTERVAL_MS",
            javascript,
        )
        self.assertIn('<textarea rows="6"', javascript)
        self.assertIn("min-height: 132px", styles)

    def test_export_page_exposes_solo_qa_bridge_controls(self):
        html = (app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        manifest = json.loads(
            (app.SOLO_QA_EXTENSION_DIR / "manifest.json").read_text(encoding="utf-8")
        )
        for control in (
            'id="solo-qa-bridge-status"',
            'id="solo-qa-sync"',
            'id="solo-qa-submit"',
            'id="solo-qa-helper-path"',
        ):
            self.assertIn(control, html)
        self.assertIn("SOLO_QA_BRIDGE_READY", javascript)
        self.assertIn("submitSelectedToSoloQa", javascript)
        self.assertIn("同步并自动返修", html)
        self.assertIn(
            ".sort((left, right) => Number(left.turn_number || 0) - Number(right.turn_number || 0))",
            javascript,
        )
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(
            manifest["host_permissions"],
            ["http://127.0.0.1:8765/*", "https://solo2.jzxhnh.com/*"],
        )

    def test_export_page_exposes_deep_preflight_controls(self):
        html = (app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        styles = (app.STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        for control in (
            'id="preflight-export"',
            'id="select-preflight-passed"',
            'id="export-preflight-panel"',
        ):
            self.assertIn(control, html)
        self.assertIn('/api/exports/preflight', javascript)
        self.assertIn('/api/exports/evaluation-repairs', javascript)
        self.assertIn('watchAutomaticEvaluationRepairs', javascript)
        self.assertIn('评分文字自动修复中', javascript)
        self.assertIn('selectedTurnsPassPreflight', javascript)
        self.assertIn('.preflight-result.failed', styles)
        self.assertIn('.export-readiness.repairing', styles)

    def test_run_list_uses_prominent_semantic_status_badges(self):
        html = (app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        styles = (app.STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        self.assertIn("<th>当前状态</th>", html)
        self.assertIn('data-label="当前状态"', javascript)
        self.assertIn("run.status_detail || label", javascript)
        self.assertIn("function runNeedsAttention(run)", javascript)
        self.assertIn('includes("等待人工确认")', javascript)
        self.assertIn("attention-detail", javascript)
        self.assertIn(".background-job-stage.attention-detail", styles)
        for tone in ("running", "ready", "complete", "failed", "warning"):
            self.assertIn(f".table-phase.{tone}", styles)
        self.assertIn("@keyframes status-pulse", styles)

    def test_detail_page_is_compact_and_preserves_text_selection_during_refresh(self):
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        styles = (app.STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        for target in (
            'data-detail-target="detail-task"',
            'data-detail-target="detail-session"',
            'data-detail-target="detail-turns"',
            'data-detail-target="detail-events"',
            'data-detail-key="task"',
            'data-detail-key="session"',
            'data-detail-key="turns"',
            'data-detail-key="events"',
        ):
            self.assertIn(target, javascript)
        self.assertIn("detailInteractionInProgress()", javascript)
        self.assertIn("showDetailRefreshHeld()", javascript)
        self.assertIn("captureDetailDisclosureState(run.id)", javascript)
        self.assertIn('detailOpenAttribute(run.id, "task", true)', javascript)
        turn_block = javascript.split("function turnHistoryHtml", 1)[1].split(
            "function renderDetail", 1
        )[0]
        self.assertLess(
            turn_block.index('class="turn-block turn-prompt-block"'),
            turn_block.index('class="meta-grid turn-meta-grid"'),
        )
        self.assertIn(
            'detailOpenAttribute(runId, `turn-${turn.turn_number}-prompt`, true)',
            turn_block,
        )
        self.assertIn(".detail-quickbar", styles)
        self.assertIn(".detail-section-summary", styles)

    def test_auto_refill_notice_is_a_separate_full_width_row(self):
        html = (app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        styles = (app.STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        self.assertIn('class="auto-refill-row" id="auto-refill-row"', html)
        self.assertGreater(html.index('id="auto-refill-row"'), html.index('class="quick-create"'))
        self.assertIn(".auto-refill-row { grid-column: 1 / -1;", styles)
        self.assertIn(".auto-refill-row.failed", styles)
        self.assertIn('refillRow.classList.toggle("failed", Boolean(refill.error))', javascript)

    def test_new_run_button_tracks_durable_background_generation(self):
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('generation_queued: ["题目生成排队中"', javascript)
        self.assertIn('generation_running: ["题目生成中"', javascript)
        self.assertIn("function renderNewRunButtonState()", javascript)
        self.assertIn('navigateTo("#runs")', javascript)

    def test_iteration_button_keeps_background_job_state_across_detail_refreshes(self):
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("iterationJobs: {}", javascript)
        self.assertIn("function setAutomaticIterationJob", javascript)
        self.assertIn("function watchAutomaticIteration", javascript)
        self.assertIn("await loadAutomaticIterationStatus(id)", javascript)
        self.assertIn('["Feature 迭代", "0-1 代码生成", "Bug 修复"].map', javascript)
        self.assertNotIn("const button = event.currentTarget", javascript)

    def test_valid_repo_name(self):
        self.assertEqual(app.validate_repo_name("api-change-radar"), "api-change-radar")

    def test_invalid_repo_names(self):
        for value in ("", "../escape", "has spaces", "/absolute"):
            with self.subTest(value=value), self.assertRaises(app.WorkflowError):
                app.validate_repo_name(value)

    def test_commands_are_split_by_line(self):
        self.assertEqual(
            app.normalize_commands("cd backend && pytest -q\n\nnpm run build"),
            ["cd backend && pytest -q", "npm run build"],
        )

    def test_model_name_validation(self):
        self.assertEqual(app.validate_model("ark/urm-01"), "ark/urm-01")
        self.assertEqual(app.validate_model("provider:model-v2"), "provider:model-v2")
        for value in ("", "has spaces", "../bad"):
            with self.subTest(value=value), self.assertRaises(app.WorkflowError):
                app.validate_model(value)

    def test_available_models_merge_current_local_and_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "settings.json").write_text(
                json.dumps(
                    {
                        "model": "auto_model/urm",
                        "env": {"ANTHROPIC_CUSTOM_MODEL_OPTION": "gateway/coder-v2"},
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "CLAUDE_DIR", root), mock.patch.dict(
                app.os.environ,
                {"CLAUDE_EVAL_MODELS": "auto_model/urm,model_hub/glm-coding"},
            ):
                app.initialize_database()
                app.set_global_model("auto_model/urm")
                self.assertEqual(
                    app.available_models(),
                    [
                        "default",
                        "opus[1m]",
                        "sonnet",
                        "sonnet[1m]",
                        "haiku",
                        "auto_model/urm",
                        "model_hub/glm-coding",
                        "gateway/coder-v2",
                    ],
                )
                self.assertEqual(
                    app.available_model_options()[0],
                    {"value": "default", "label": "Default（推荐）"},
                )
                self.assertEqual(
                    app.available_model_options()[5],
                    {"value": "auto_model/urm", "label": "auto_model/urm（自定义网关）"},
                )

    def test_docker_key_source_uses_local_claude_settings_without_returning_the_key(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text(
                json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "secret-test-value"}}),
                encoding="utf-8",
            )
            with mock.patch.object(app, "CLAUDE_SETTINGS_PATH", settings), mock.patch.dict(
                app.os.environ, {}, clear=True
            ):
                source = app.docker_api_key_source()

        self.assertEqual(source, "Claude 本机配置")
        self.assertNotIn("secret-test-value", source)

    def test_long_context_model_alias_is_valid(self):
        self.assertEqual(app.validate_model("opus[1m]"), "opus[1m]")
        self.assertEqual(app.validate_model("sonnet[1m]"), "sonnet[1m]")

    def test_run_metadata_can_be_inferred_from_prompt(self):
        task_type, framework = app.infer_run_metadata(
            "请从零完成系统，后端使用 Python、FastAPI 和 SQLite，前端采用 React 与 TypeScript。"
        )
        self.assertEqual(task_type, "0-1 代码生成")
        self.assertEqual(framework, "TypeScript、FastAPI、SQLite、React、Python")

    def test_project_directory_must_stay_inside_projects_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "DEFAULT_PROJECT_DIRECTORY", "zzzz"
            ):
                relative, resolved = app.resolve_project_directory("zzzz/team-a")
                self.assertEqual(relative, "zzzz/team-a")
                self.assertEqual(resolved, root / "zzzz" / "team-a")
                absolute_relative, _ = app.resolve_project_directory(str(root / "selected"))
                self.assertEqual(absolute_relative, "selected")
                with self.assertRaisesRegex(app.WorkflowError, "必须位于"):
                    app.resolve_project_directory("../outside")

    def test_context_preflight_requires_one_million_support(self):
        supported = subprocess.CompletedProcess([], 0, "--autocompact <auto|tokens> 100k–1M tokens", "")
        unsupported = subprocess.CompletedProcess([], 0, "Claude help", "")
        with mock.patch.object(app, "run_command", return_value=supported):
            app.ensure_claude_context_support()
        with mock.patch.object(app, "run_command", return_value=unsupported), self.assertRaisesRegex(
            app.WorkflowError, "1000000"
        ):
            app.ensure_claude_context_support()

    def test_compose_build_uses_a_longer_verification_timeout(self):
        self.assertEqual(
            app.verification_command_action("docker compose -f ci.yml build"),
            "build",
        )
        self.assertEqual(
            app.verification_command_action(
                "docker compose -p isolated run --rm verify"
            ),
            "run",
        )
        self.assertEqual(
            app.verification_command_action("docker compose --profile build up"),
            "up",
        )
        self.assertEqual(
            app.verification_command_action("docker compose -f build up"),
            "up",
        )
        self.assertEqual(
            app.verification_command_action(
                "docker compose --project-name run build"
            ),
            "build",
        )
        self.assertGreater(
            app.verification_command_timeout("docker compose build"),
            app.verification_command_timeout("docker compose config --quiet"),
        )
        self.assertEqual(
            app.verification_command_timeout("docker compose run --rm verify"),
            app.VERIFICATION_COMMAND_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            app.verification_command_timeout("docker compose run --build --rm verify"),
            app.VERIFICATION_COMPOSE_BUILD_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            app.verification_command_timeout("docker compose up --build"),
            app.VERIFICATION_COMPOSE_BUILD_TIMEOUT_SECONDS,
        )

    def test_compose_build_timeout_skips_dependent_run_and_keeps_partial_output(self):
        commands = [
            "docker compose config --quiet",
            "docker compose build",
            "docker compose run --rm verify",
        ]
        runner_calls = []

        def runner(args, cwd, environment, timeout, **kwargs):
            runner_calls.append((args, timeout, kwargs))
            if args[-1] == "docker compose build":
                kwargs["progress_callback"]("#7 downloading playwright 72%", 31)
                raise subprocess.TimeoutExpired(
                    args,
                    timeout,
                    output="#7 downloading playwright 72%",
                )
            return subprocess.CompletedProcess(args, 0, "", "")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app, "DATA_DIR", Path(directory)
        ), mock.patch.object(
            app,
            "verification_environment",
            return_value={"COMPOSE_PROJECT_NAME": "claude_eval_test"},
        ), mock.patch.object(
            app, "run_cancellable_subprocess", side_effect=runner
        ), mock.patch.object(
            app, "update_run"
        ) as update_run, mock.patch.object(
            app, "add_event"
        ) as add_event, mock.patch.object(
            app.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as cleanup:
            results = app.verification_results(commands, Path(directory), "run-test")

        self.assertEqual(len(runner_calls), 2)
        self.assertEqual(
            runner_calls[1][1], app.VERIFICATION_COMPOSE_BUILD_TIMEOUT_SECONDS
        )
        self.assertEqual(len(results), 3)
        self.assertTrue(results[1]["timed_out"])
        self.assertIn("downloading playwright 72%", results[1]["output"])
        self.assertEqual(
            results[1]["environment_overrides"],
            {"COMPOSE_PROFILES": "*"},
        )
        self.assertTrue(results[2]["skipped"])
        self.assertEqual(results[2]["exit_code"], -2)
        self.assertEqual(results[2]["failure_kind"], "environment")
        self.assertEqual(results[2]["blocked_by"], "docker compose build")
        self.assertTrue(
            any(
                "downloading playwright 72%" in str(call.kwargs.get("status_detail", ""))
                for call in update_run.call_args_list
            )
        )
        self.assertTrue(
            any("避免重复构建" in str(call.args) for call in add_event.call_args_list)
        )
        cleanup.assert_called_once()
        self.assertNotIn("--rmi", cleanup.call_args.args[0])

    def test_compose_build_failure_skips_run_but_keeps_later_non_compose_check(self):
        commands = [
            "docker compose build",
            "docker compose run --rm verify",
            "python3 -m unittest",
        ]
        runner = mock.Mock(
            side_effect=[
                subprocess.CompletedProcess([], 1, "Dockerfile syntax error", ""),
                subprocess.CompletedProcess([], 0, "tests passed", ""),
            ]
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app, "DATA_DIR", Path(directory)
        ), mock.patch.object(
            app,
            "verification_environment",
            return_value={"COMPOSE_PROJECT_NAME": "claude_eval_test"},
        ), mock.patch.object(
            app, "run_cancellable_subprocess", runner
        ), mock.patch.object(
            app, "update_run"
        ), mock.patch.object(
            app, "add_event"
        ), mock.patch.object(
            app.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ):
            results = app.verification_results(commands, Path(directory), "run-test")

        self.assertEqual(runner.call_count, 2)
        self.assertEqual(results[0]["failure_kind"], "product")
        self.assertTrue(results[1]["skipped"])
        self.assertEqual(results[1]["failure_kind"], "product")
        self.assertEqual(results[2]["exit_code"], 0)
        self.assertEqual(runner.call_args_list[1].args[0][-1], "python3 -m unittest")

    def test_compose_run_without_an_explicit_build_still_executes(self):
        runner = mock.Mock(
            return_value=subprocess.CompletedProcess([], 0, "verify passed", "")
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app, "DATA_DIR", Path(directory)
        ), mock.patch.object(
            app,
            "verification_environment",
            return_value={"COMPOSE_PROJECT_NAME": "claude_eval_test"},
        ), mock.patch.object(
            app, "run_cancellable_subprocess", runner
        ), mock.patch.object(
            app, "update_run"
        ), mock.patch.object(
            app, "add_event"
        ), mock.patch.object(
            app.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ):
            results = app.verification_results(
                ["docker compose run --rm verify"],
                Path(directory),
                "run-test",
            )

        self.assertEqual(runner.call_count, 1)
        self.assertEqual(results[0]["exit_code"], 0)
        self.assertNotIn("skipped", results[0])

    def test_compose_build_activates_profiles_without_leaking_to_other_commands(self):
        seen = []

        def runner(args, cwd, environment, timeout, **kwargs):
            seen.append((args[-1], dict(environment), timeout))
            return subprocess.CompletedProcess(args, 0, "ok", "")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app, "DATA_DIR", Path(directory)
        ), mock.patch.object(
            app,
            "verification_environment",
            return_value={"COMPOSE_PROJECT_NAME": "claude_eval_test"},
        ), mock.patch.object(
            app, "run_cancellable_subprocess", side_effect=runner
        ), mock.patch.object(
            app, "update_run"
        ), mock.patch.object(
            app, "add_event"
        ), mock.patch.object(
            app.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as cleanup:
            results = app.verification_results(
                [
                    "docker compose config --quiet",
                    "docker compose build",
                    "docker compose run --rm verify",
                ],
                Path(directory),
                "run-test",
            )

        self.assertNotIn("COMPOSE_PROFILES", seen[0][1])
        self.assertEqual(seen[1][1]["COMPOSE_PROFILES"], "*")
        self.assertNotIn("COMPOSE_PROFILES", seen[2][1])
        self.assertEqual(results[1]["command"], "docker compose build")
        self.assertEqual(
            results[1]["environment_overrides"],
            {"COMPOSE_PROFILES": "*"},
        )
        self.assertNotIn("COMPOSE_PROFILES", cleanup.call_args.kwargs["env"])

    def test_explicit_compose_profile_is_not_overridden(self):
        seen_environment = []

        def runner(args, cwd, environment, timeout, **kwargs):
            seen_environment.append(dict(environment))
            return subprocess.CompletedProcess(args, 0, "ok", "")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app, "DATA_DIR", Path(directory)
        ), mock.patch.object(
            app,
            "verification_environment",
            return_value={"COMPOSE_PROJECT_NAME": "claude_eval_test"},
        ), mock.patch.object(
            app, "run_cancellable_subprocess", side_effect=runner
        ), mock.patch.object(
            app, "update_run"
        ), mock.patch.object(
            app, "add_event"
        ), mock.patch.object(
            app.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ):
            results = app.verification_results(
                ["docker compose --profile verify build"],
                Path(directory),
                "run-test",
            )

        self.assertNotIn("COMPOSE_PROFILES", seen_environment[0])
        self.assertNotIn("environment_overrides", results[0])

    def test_cancellable_subprocess_streams_progress_and_retains_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path = root / "verification.log"
            progress = []
            completed = app.run_cancellable_subprocess(
                ["/bin/sh", "-lc", "printf 'layer one\\n'; sleep 0.12; printf 'done\\n'"],
                root,
                app.os.environ.copy(),
                2,
                progress_callback=lambda output, elapsed: progress.append((output, elapsed)),
                progress_interval=0.05,
                output_path=log_path,
            )

            self.assertEqual(completed.returncode, 0)
            self.assertIn("layer one", completed.stdout)
            self.assertIn("done", completed.stdout)
            self.assertTrue(progress)
            self.assertIn("layer one", progress[-1][0])
            self.assertEqual(log_path.read_text(encoding="utf-8"), "layer one\ndone\n")

    def test_cancellable_subprocess_timeout_returns_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path = root / "verification.log"
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                app.run_cancellable_subprocess(
                    ["/bin/sh", "-lc", "printf 'pulling base image\\n'; sleep 1"],
                    root,
                    app.os.environ.copy(),
                    0.05,
                    progress_interval=0.01,
                    output_path=log_path,
                )

            self.assertIn("pulling base image", str(raised.exception.output))
            self.assertIn("pulling base image", log_path.read_text(encoding="utf-8"))

    def test_validation_ui_labels_skipped_commands(self):
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        styles = (app.STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        self.assertIn('item.skipped ? "SKIPPED"', javascript)
        self.assertIn(".validation-item .skip", styles)


class ParsingTests(unittest.TestCase):
    def test_parse_background_id(self):
        text = "Starting background service…\nbackgrounded · 6c3fd7ce\n"
        self.assertEqual(app.BACKGROUND_ID_RE.search(text).group(1), "6c3fd7ce")

    def test_extract_prompt_id_ignores_tool_results(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "user", "promptId": "p1", "message": {"content": "完整需求"}}),
                        json.dumps({"type": "user", "promptId": "p1", "message": {"content": [{"type": "tool_result"}]}}),
                        json.dumps({"type": "user", "promptId": "p2", "message": {"content": "修复问题"}}),
                    ]
                ),
                encoding="utf-8",
            )
            with mock.patch.object(app, "find_transcript", return_value=transcript):
                self.assertEqual(app.extract_prompt_id("session", "完整需求"), "p1")
                self.assertEqual(app.extract_prompt_id("session", "修复问题"), "p2")

    def test_extract_prompt_id_recovers_legacy_multiline_terminal_paste(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    json.dumps(event, ensure_ascii=False)
                    for event in [
                        {
                            "type": "user",
                            "sessionId": "session",
                            "timestamp": "2026-09-10T09:14:52.500Z",
                            "promptId": "p-split",
                            "message": {"content": "修复第一个问题"},
                        },
                        {
                            "type": "queue-operation",
                            "operation": "enqueue",
                            "sessionId": "session",
                            "timestamp": "2026-09-10T09:14:53.000Z",
                            "content": "修复第二个问题",
                        },
                    ]
                ),
                encoding="utf-8",
            )
            with mock.patch.object(app, "find_transcript", return_value=transcript):
                prompt_id = app.extract_prompt_id(
                    "session", "修复第一个问题\n修复第二个问题"
                )

        self.assertEqual(prompt_id, "p-split")

    def test_trace_human_prompt_text_ignores_internal_task_notifications(self):
        event = {
            "type": "user",
            "promptId": "internal",
            "message": {"content": "<task-notification>\ncompleted\n</task-notification>"},
        }

        self.assertIsNone(app.trace_human_prompt_text(event))

    def test_trace_human_prompt_text_only_ignores_marked_api_resume(self):
        for content in ("继续", " 继续。 ", [{"type": "text", "text": "继续"}]):
            event = {
                "type": "user",
                "promptId": "resume-prompt",
                "message": {"content": content},
            }
            with self.subTest(content=content):
                self.assertEqual(app.trace_human_prompt_text(event).strip(" 。"), "继续")
                self.assertIsNone(
                    app.trace_human_prompt_text(event, automatic_api_resume=True)
                )

    def test_trace_automatic_api_resume_indexes_distinguishes_manual_continue(self):
        events = [
            {
                "type": "assistant",
                "isApiErrorMessage": True,
                "message": {"content": [{"type": "text", "text": "API Error: 504"}]},
            },
            {"type": "user", "message": {"content": "继续"}},
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "已恢复"}],
                    "stop_reason": "end_turn",
                },
            },
            {"type": "user", "message": {"content": "继续"}},
        ]

        self.assertEqual(app.trace_automatic_api_resume_indexes(events), {1})

    def test_trace_automatic_resume_recognizes_turn_ending_without_final_reply(self):
        events = [
            {
                "type": "user",
                "promptId": "task-prompt",
                "message": {"content": "完成这个项目"},
            },
            {
                "type": "assistant",
                "message": {
                    "stop_reason": None,
                    "content": [{"type": "text", "text": "准备补装依赖："}],
                },
            },
            {"type": "system", "subtype": "turn_duration"},
            {
                "type": "user",
                "promptId": "resume-prompt",
                "message": {"content": "继续"},
            },
            {
                "type": "assistant",
                "message": {
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "name": "Bash"}],
                },
            },
        ]

        self.assertEqual(app.trace_automatic_api_resume_indexes(events), {3})

    def test_trace_automatic_resume_ignores_last_prompt_during_tool_work(self):
        events = [
            {
                "type": "user",
                "promptId": "task-prompt",
                "message": {"content": "完成这个项目"},
            },
            {
                "type": "assistant",
                "message": {
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "name": "Bash"}],
                },
            },
            {"type": "last-prompt"},
            {
                "type": "user",
                "promptId": "manual-prompt",
                "message": {"content": "继续"},
            },
        ]

        self.assertEqual(app.trace_automatic_api_resume_indexes(events), set())

    def test_trace_human_prompt_text_ignores_cli_interruption_markers(self):
        for content in (
            "[Request interrupted by user]",
            [{"type": "text", "text": "[Request interrupted by user for tool use]"}],
        ):
            event = {
                "type": "user",
                "promptId": "interruption-marker",
                "message": {"content": content},
            }
            with self.subTest(content=content):
                self.assertIsNone(app.trace_human_prompt_text(event))

    def test_parse_agents_json_with_prefix(self):
        value = app.parse_json_output('warning\n[{"id":"abc","status":"busy"}]')
        self.assertEqual(value[0]["id"], "abc")

    def test_monitor_fails_fast_on_model_api_error(self):
        row = {"phase": "first_running"}
        agent = {"id": "agent-1", "state": "blocked", "status": "idle"}
        timeline = {"detail": "API Error: 403 model unavailable"}
        with mock.patch.object(app, "run_row", return_value=row), mock.patch.object(
            app, "list_agents", return_value=[agent]
        ), mock.patch.object(app, "read_timeline", return_value=timeline):
            with self.assertRaisesRegex(app.WorkflowError, "403 model unavailable"):
                app.monitor_claude("run-id", 1, "agent-1", None)

    def test_launch_claude_passes_prompt_unchanged(self):
        prompt = "  第一行\n第二行  "
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="backgrounded · abc12345\n", stderr=""
        )
        agents = [{"id": "abc12345", "sessionId": "session-1", "startedAt": 1}]
        with mock.patch.object(app, "run_command", return_value=completed) as command, mock.patch.object(
            app, "list_agents", side_effect=[[], agents]
        ):
            agent_id, session_id = app.launch_claude(
                Path("/tmp/project"), prompt, "ark/next-model"
            )
        self.assertEqual(agent_id, "abc12345")
        self.assertEqual(session_id, "session-1")
        self.assertEqual(command.call_args.args[0][-1], prompt)
        self.assertIn("ark/next-model", command.call_args.args[0])
        self.assertIn("--autocompact", command.call_args.args[0])
        self.assertIn("1m", command.call_args.args[0])

    def test_container_trace_detects_prompt_id_session_and_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_root = Path(directory)
            transcript = trace_root / "project" / "session-new.jsonl"
            transcript.parent.mkdir()
            events = [
                {"type": "user", "promptId": "prompt-new", "message": {"content": "修复这个问题\n"}},
                {
                    "type": "assistant",
                    "message": {
                        "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "name": "Edit", "input": {}}],
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "stop_reason": "stop_sequence",
                        "content": [{"type": "text", "text": "修复完成，测试已通过。"}],
                    },
                },
                {"type": "last-prompt"},
            ]
            transcript.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
                encoding="utf-8",
            )

            state = app.trace_turn_state(trace_root, "修复这个问题")

        self.assertEqual(state["session_id"], "session-new")
        self.assertEqual(state["prompt_id"], "prompt-new")
        self.assertEqual(state["result"], "修复完成，测试已通过。")
        self.assertTrue(state["complete"])
        self.assertEqual(state["api_error"], "")

    def test_container_trace_accepts_turn_duration_as_completion_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_root = Path(directory)
            transcript = trace_root / "project" / "session-duration.jsonl"
            transcript.parent.mkdir()
            events = [
                {
                    "type": "user",
                    "promptId": "prompt-duration",
                    "message": {"content": "完成这个项目"},
                },
                {
                    "type": "assistant",
                    "message": {
                        "stop_reason": "stop_sequence",
                        "content": [{"type": "text", "text": "实现和测试均已完成。"}],
                    },
                },
                {"type": "system", "subtype": "turn_duration"},
            ]
            transcript.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
                encoding="utf-8",
            )

            state = app.trace_turn_state(trace_root, "完成这个项目")

        self.assertEqual(state["session_id"], "session-duration")
        self.assertEqual(state["prompt_id"], "prompt-duration")
        self.assertEqual(state["result"], "实现和测试均已完成。")
        self.assertTrue(state["complete"])

    def test_container_trace_tolerates_terminal_outer_prompt_whitespace(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_root = Path(directory)
            transcript = trace_root / "project" / "session-spaced.jsonl"
            transcript.parent.mkdir()
            events = [
                {
                    "type": "user",
                    "promptId": "prompt-spaced",
                    "message": {"content": " 完成这个项目\n"},
                },
                {
                    "type": "assistant",
                    "message": {
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "项目已经完成。"}],
                    },
                },
                {"type": "system", "subtype": "turn_duration"},
            ]
            transcript.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
                encoding="utf-8",
            )

            state = app.trace_turn_state(trace_root, "完成这个项目")

        self.assertIsNotNone(state)
        self.assertEqual(state["session_id"], "session-spaced")
        self.assertEqual(state["prompt_id"], "prompt-spaced")
        self.assertTrue(state["complete"])

    def test_trace_activity_ignores_mtime_only_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text('{"type":"user"}\n', encoding="utf-8")
            initial = app.trace_activity_signature(Path(directory))
            stat = transcript.stat()
            os.utime(
                transcript,
                ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000),
            )

            touched = app.trace_activity_signature(Path(directory))

        self.assertEqual(touched, initial)

    def test_container_trace_detects_incomplete_end_and_waits_during_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_root = Path(directory)
            transcript = trace_root / "project" / "session-incomplete.jsonl"
            transcript.parent.mkdir()
            events = [
                {
                    "type": "user",
                    "promptId": "prompt-incomplete",
                    "message": {"content": "完成这个项目"},
                },
                {
                    "type": "assistant",
                    "message": {
                        "stop_reason": None,
                        "content": [{"type": "text", "text": "准备补装依赖："}],
                    },
                },
                {"type": "system", "subtype": "turn_duration"},
            ]
            transcript.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
                encoding="utf-8",
            )

            incomplete = app.trace_turn_state(trace_root, "完成这个项目")

            events.extend(
                [
                    {
                        "type": "user",
                        "promptId": "prompt-resume",
                        "message": {"content": "继续"},
                    },
                    {
                        "type": "assistant",
                        "message": {
                            "stop_reason": "tool_use",
                            "content": [{"type": "tool_use", "name": "Bash"}],
                        },
                    },
                ]
            )
            transcript.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
                encoding="utf-8",
            )
            resuming = app.trace_turn_state(trace_root, "完成这个项目")

        self.assertFalse(incomplete["complete"])
        self.assertTrue(incomplete["incomplete_turn"])
        self.assertEqual(incomplete["api_error"], "")
        self.assertFalse(resuming["complete"])
        self.assertFalse(resuming["incomplete_turn"])

    def test_container_trace_recovers_bare_duration_without_assistant_event(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_root = Path(directory)
            transcript = trace_root / "project" / "session-bare-duration.jsonl"
            transcript.parent.mkdir()
            events = [
                {
                    "type": "user",
                    "promptId": "prompt-bare-duration",
                    "message": {"content": "完成这个项目"},
                },
                {"type": "system", "subtype": "turn_duration"},
            ]
            transcript.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
                encoding="utf-8",
            )

            incomplete = app.trace_turn_state(trace_root, "完成这个项目")

            events.append(
                {
                    "type": "user",
                    "promptId": "prompt-resume",
                    "message": {"content": "继续"},
                }
            )
            transcript.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
                encoding="utf-8",
            )
            resuming = app.trace_turn_state(trace_root, "完成这个项目")

        self.assertTrue(incomplete["incomplete_turn"])
        self.assertEqual(app.trace_automatic_api_resume_indexes(events), {2})
        self.assertFalse(resuming["incomplete_turn"])

    def test_container_trace_keeps_structured_403_out_of_generic_resume(self):
        for content in ("API Error: 403 model unavailable", None):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                trace_root = Path(directory)
                transcript = trace_root / "project" / "session-403.jsonl"
                transcript.parent.mkdir()
                message = {"stop_reason": "stop_sequence"}
                if content is not None:
                    message["content"] = content
                events = [
                    {
                        "type": "user",
                        "promptId": "prompt-403",
                        "message": {"content": "完成这个项目"},
                    },
                    {
                        "type": "assistant",
                        "isApiErrorMessage": True,
                        "apiErrorStatus": 403,
                        "message": message,
                    },
                    {"type": "system", "subtype": "turn_duration"},
                ]
                transcript.write_text(
                    "\n".join(
                        json.dumps(event, ensure_ascii=False) for event in events
                    ),
                    encoding="utf-8",
                )

                state = app.trace_turn_state(trace_root, "完成这个项目")

            self.assertEqual(state["api_error"].split()[:3], ["API", "Error:", "403"])
            self.assertFalse(state["incomplete_turn"])
            self.assertFalse(app.retryable_api_error(state["api_error"]))

    def test_container_trace_does_not_recover_an_existing_terminal_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_root = Path(directory)
            transcript = trace_root / "project" / "session-terminal-stop.jsonl"
            transcript.parent.mkdir()
            events = [
                {
                    "type": "user",
                    "promptId": "prompt-terminal-stop",
                    "message": {"content": "完成这个项目"},
                },
                {
                    "type": "assistant",
                    "message": {
                        "stop_reason": "end_turn",
                        "content": [{"type": "thinking", "thinking": "done"}],
                    },
                },
                {"type": "system", "subtype": "turn_duration"},
                {
                    "type": "user",
                    "promptId": "manual-continue",
                    "message": {"content": "继续"},
                },
            ]
            transcript.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
                encoding="utf-8",
            )

            state = app.trace_turn_state(trace_root, "完成这个项目")

        self.assertFalse(state["complete"])
        self.assertFalse(state["incomplete_turn"])
        self.assertEqual(app.trace_automatic_api_resume_indexes(events), set())

    def test_container_trace_completes_legacy_multiline_terminal_paste(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_root = Path(directory)
            transcript = trace_root / "project" / "session-split.jsonl"
            transcript.parent.mkdir()
            events = [
                {
                    "type": "user",
                    "sessionId": "session-split",
                    "timestamp": "2026-09-10T09:14:52.500Z",
                    "promptId": "prompt-split",
                    "message": {"content": "修复第一个问题"},
                },
                {
                    "type": "queue-operation",
                    "operation": "enqueue",
                    "sessionId": "session-split",
                    "timestamp": "2026-09-10T09:14:53.000Z",
                    "content": "修复第二个问题",
                },
                {
                    "type": "assistant",
                    "message": {
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "两个问题都已修复。"}],
                    },
                },
                {"type": "system", "subtype": "turn_duration"},
            ]
            transcript.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
                encoding="utf-8",
            )

            state = app.trace_turn_state(
                trace_root, "修复第一个问题\n修复第二个问题"
            )

        self.assertEqual(state["prompt_id"], "prompt-split")
        self.assertEqual(state["result"], "两个问题都已修复。")
        self.assertTrue(state["complete"])

    def test_completed_docker_turn_becomes_idle_while_container_is_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker"):
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "idle-session-demo",
                    "project_directory": ".",
                    "first_prompt": "完成这个项目",
                    "verification_commands": ["make test"],
                    "_defer_start": True,
                })
                workspace = Path(created["repo_path"])
                workspace.mkdir(parents=True)
                app.update_run(created["id"], phase="first_running")
                app.update_turn(created["id"], 1, status="running")
                trace_path = root / "session-idle.jsonl"
                trace_state = {
                    "session_id": "session-idle",
                    "prompt_id": "prompt-idle",
                    "result": "本轮完成",
                    "complete": True,
                    "api_error": "",
                    "path": trace_path,
                }
                phase_during_verification = []
                checkpoint_order = []

                def verify(_commands, _workspace, run_id):
                    phase_during_verification.append(app.run_row(run_id)["phase"])
                    return [{"command": "make test", "returncode": 0}]

                with mock.patch.object(
                    app, "refresh_trace_snapshot", return_value=(root, trace_state)
                ), mock.patch.object(
                    app,
                    "schedule_worker",
                    side_effect=lambda *_args: checkpoint_order.append("review"),
                ) as scheduler, mock.patch.object(
                    app, "close_container_conversation"
                ) as close, mock.patch.object(
                    app, "verification_results", side_effect=verify
                ), mock.patch.object(
                    app,
                    "checkpoint_completed_work",
                    side_effect=lambda *_args: checkpoint_order.append("git") or "a" * 40,
                ) as checkpoint, mock.patch.object(
                    app,
                    "export_turn_checkpoint",
                    side_effect=lambda *_args: checkpoint_order.append("trajectory")
                    or root / "turn-01.jsonl",
                ) as export_turn:
                    app.monitor_docker_turn(created["id"], 1)

                stored = app.serialize_run(app.run_row(created["id"]))

        self.assertEqual(stored["phase"], "review_queued")
        self.assertEqual(stored["turns"][0]["status"], "reviewing")
        self.assertFalse(stored["trajectory_path"])
        self.assertIn("已提交、推送并导出轨迹", stored["status_detail"])
        self.assertEqual(phase_during_verification, ["first_idle"])
        checkpoint.assert_called_once_with(created["id"], 1)
        export_turn.assert_called_once_with(created["id"], 1)
        scheduler.assert_called_once_with(created["id"], "review_queued", app.review_worker)
        self.assertEqual(checkpoint_order, ["git", "trajectory", "review"])
        close.assert_not_called()

    def test_container_trace_detects_unresolved_api_error(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_root = Path(directory)
            transcript = trace_root / "project" / "session-error.jsonl"
            transcript.parent.mkdir()
            events = [
                {"type": "user", "promptId": "prompt-error", "message": {"content": "完成这个项目"}},
                {
                    "type": "assistant",
                    "isApiErrorMessage": True,
                    "apiErrorStatus": 504,
                    "message": {
                        "stop_reason": "stop_sequence",
                        "content": [{"type": "text", "text": "API Error: 504 Gateway Time-out"}],
                    },
                },
                {"type": "system", "subtype": "turn_duration"},
            ]
            transcript.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
                encoding="utf-8",
            )

            state = app.trace_turn_state(trace_root, "完成这个项目")

        self.assertEqual(state["session_id"], "session-error")
        self.assertEqual(state["prompt_id"], "prompt-error")
        self.assertFalse(state["complete"])
        self.assertEqual(state["result"], "")
        self.assertEqual(state["api_error"], "API Error: 504 Gateway Time-out")

    def test_container_trace_treats_continue_after_api_error_as_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_root = Path(directory)
            transcript = trace_root / "project" / "session-error.jsonl"
            transcript.parent.mkdir()
            events = [
                {"type": "user", "promptId": "prompt-error", "message": {"content": "完成这个项目"}},
                {
                    "type": "assistant",
                    "isApiErrorMessage": True,
                    "apiErrorStatus": 504,
                    "message": {"content": [{"type": "text", "text": "API Error: 504 Gateway Time-out"}]},
                },
                {"type": "user", "promptId": "prompt-resume", "message": {"content": "继续"}},
                {
                    "type": "assistant",
                    "message": {"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": "Bash"}]},
                },
            ]
            transcript.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
                encoding="utf-8",
            )

            state = app.trace_turn_state(trace_root, "完成这个项目")

        self.assertFalse(state["complete"])
        self.assertEqual(state["api_error"], "")

    def test_container_trace_detects_second_api_error_after_continue(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_root = Path(directory)
            transcript = trace_root / "project" / "session-error.jsonl"
            transcript.parent.mkdir()
            events = [
                {"type": "user", "promptId": "prompt-error", "message": {"content": "完成这个项目"}},
                {
                    "type": "assistant",
                    "isApiErrorMessage": True,
                    "apiErrorStatus": 504,
                    "message": {"content": [{"type": "text", "text": "API Error: 504 first"}]},
                },
                {"type": "user", "promptId": "prompt-resume", "message": {"content": "继续"}},
                {
                    "type": "assistant",
                    "isApiErrorMessage": True,
                    "apiErrorStatus": 504,
                    "message": {"content": [{"type": "text", "text": "API Error: 504 second"}]},
                },
            ]
            transcript.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
                encoding="utf-8",
            )

            state = app.trace_turn_state(trace_root, "完成这个项目")

        self.assertEqual(state["api_error"], "API Error: 504 second")

    def test_container_trace_detects_unresolved_user_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_root = Path(directory)
            transcript = trace_root / "project" / "session-interrupted.jsonl"
            transcript.parent.mkdir()
            events = [
                {
                    "type": "user",
                    "promptId": "prompt-interrupted",
                    "message": {"content": "完成这个项目"},
                },
                {
                    "type": "assistant",
                    "message": {
                        "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "name": "Bash", "input": {}}],
                    },
                },
                {
                    "type": "user",
                    "interruptedMessageId": "assistant-tool-call",
                    "message": {
                        "content": [
                            {"type": "text", "text": "[Request interrupted by user for tool use]"}
                        ]
                    },
                },
            ]
            transcript.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
                encoding="utf-8",
            )

            state = app.trace_turn_state(trace_root, "完成这个项目")

        self.assertFalse(state["complete"])
        self.assertTrue(state["interrupted"])
        self.assertIn("等待新的输入", state["interruption_reason"])

    def test_container_trace_does_not_flag_an_interruption_after_resume(self):
        events = [
            {"type": "user", "message": {"content": "完成这个项目"}},
            {
                "type": "user",
                "interruptedMessageId": "assistant-tool-call",
                "message": {"content": "[Request interrupted by user for tool use]"},
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "继续处理"}]},
            },
        ]

        self.assertEqual(app.trace_user_interruption(events, 0), "")

    def test_docker_monitor_preserves_api_error_as_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker"):
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "api-error-demo",
                    "project_directory": ".",
                    "first_prompt": "完成这个项目",
                    "_defer_start": True,
                })
                app.update_run(created["id"], phase="first_running")
                app.update_turn(created["id"], 1, status="running")
                app.add_event(
                    created["id"], app.api_resume_event_message(1), "warning"
                )
                trace_state = {
                    "session_id": "session-error",
                    "prompt_id": "prompt-error",
                    "result": "",
                    "complete": False,
                    "api_error": "API Error: 504 Gateway Time-out",
                    "path": root / "session-error.jsonl",
                }
                with mock.patch.object(
                    app, "refresh_trace_snapshot", return_value=(root, trace_state)
                ), mock.patch.object(app, "export_and_remove_container") as export:
                    with mock.patch.object(app, "schedule_automatic_api_retry") as auto_retry:
                        app.monitor_docker_turn(created["id"], 1)

                stored = app.serialize_run(app.run_row(created["id"]))

        self.assertEqual(stored["phase"], "interrupted")
        self.assertEqual(stored["turns"][0]["status"], "interrupted")
        self.assertIn("504 Gateway Time-out", stored["error"])
        export.assert_called_once_with(created["id"], force=True)
        auto_retry.assert_called_once_with(created["id"])

    def test_docker_monitor_sends_continue_before_fresh_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker"):
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "api-resume-demo",
                    "project_directory": ".",
                    "first_prompt": "完成这个项目",
                    "_defer_start": True,
                })
                app.update_run(created["id"], phase="first_running")
                app.update_turn(created["id"], 1, status="running")
                trace_state = {
                    "session_id": "session-error",
                    "prompt_id": "prompt-error",
                    "result": "",
                    "complete": False,
                    "api_error": "API Error: 504 Gateway Time-out",
                    "path": root / "session-error.jsonl",
                }

                def resume_once(run_id, *_args):
                    app.update_run(run_id, phase="stopped")
                    return True

                with mock.patch.object(
                    app, "refresh_trace_snapshot", return_value=(root, trace_state)
                ), mock.patch.object(
                    app, "resume_after_api_error", side_effect=resume_once
                ) as resume, mock.patch.object(
                    app, "preserve_interrupted_docker_turn"
                ) as preserve, mock.patch.object(
                    app, "schedule_automatic_api_retry"
                ) as auto_retry, mock.patch.object(app.time, "sleep"):
                    app.monitor_docker_turn(created["id"], 1)

        resume.assert_called_once_with(
            created["id"], 1, str(created["screen_name"] or "")
        )
        preserve.assert_not_called()
        auto_retry.assert_not_called()

    def test_docker_monitor_resumes_turn_that_ended_without_final_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker"):
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "incomplete-resume-demo",
                    "project_directory": ".",
                    "first_prompt": "完成这个项目",
                    "_defer_start": True,
                })
                app.update_run(created["id"], phase="first_running")
                app.update_turn(created["id"], 1, status="running")
                trace_state = {
                    "session_id": "session-incomplete",
                    "prompt_id": "prompt-incomplete",
                    "result": "",
                    "complete": False,
                    "api_error": "",
                    "incomplete_turn": True,
                    "path": root / "session-incomplete.jsonl",
                }

                def resume_once(run_id, *_args):
                    app.update_run(run_id, phase="stopped")
                    return True

                with mock.patch.object(
                    app, "refresh_trace_snapshot", return_value=(root, trace_state)
                ), mock.patch.object(
                    app, "resume_after_api_error", side_effect=resume_once
                ) as resume, mock.patch.object(
                    app, "preserve_interrupted_docker_turn"
                ) as preserve, mock.patch.object(
                    app, "schedule_automatic_api_retry"
                ) as auto_retry, mock.patch.object(app.time, "sleep"):
                    app.monitor_docker_turn(created["id"], 1)

        resume.assert_called_once_with(
            created["id"], 1, str(created["screen_name"] or "")
        )
        preserve.assert_not_called()
        auto_retry.assert_not_called()

    def test_docker_monitor_second_incomplete_turn_falls_back_to_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker"):
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "incomplete-retry-demo",
                    "project_directory": ".",
                    "first_prompt": "完成这个项目",
                    "_defer_start": True,
                })
                app.update_run(created["id"], phase="first_running")
                app.update_turn(created["id"], 1, status="running")
                app.add_event(
                    created["id"], app.api_resume_event_message(1), "warning"
                )
                trace_state = {
                    "session_id": "session-incomplete",
                    "prompt_id": "prompt-incomplete",
                    "result": "",
                    "complete": False,
                    "api_error": "",
                    "incomplete_turn": True,
                    "interrupted": False,
                    "interruption_reason": "",
                    "path": root / "session-incomplete.jsonl",
                }
                with mock.patch.object(
                    app, "refresh_trace_snapshot", return_value=(root, trace_state)
                ), mock.patch.object(
                    app, "send_api_resume_to_screen"
                ) as resume, mock.patch.object(
                    app, "export_and_remove_container"
                ) as export, mock.patch.object(
                    app, "schedule_automatic_api_retry"
                ) as auto_retry, mock.patch.object(app.time, "sleep"):
                    app.monitor_docker_turn(created["id"], 1)

                stored = app.serialize_run(app.run_row(created["id"]))

        self.assertEqual(stored["phase"], "interrupted")
        self.assertEqual(stored["turns"][0]["status"], "interrupted")
        self.assertIn("没有生成最终回复", stored["error"])
        resume.assert_not_called()
        export.assert_called_once_with(created["id"], force=True)
        auto_retry.assert_called_once_with(created["id"])

    def test_api_resume_is_persisted_and_sent_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker"), mock.patch.object(
                app, "screen_session_running", return_value=True
            ), mock.patch.object(app, "run_command") as command, mock.patch.object(
                app.time, "sleep"
            ):
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "api-resume-once-demo",
                    "project_directory": ".",
                    "first_prompt": "完成这个项目",
                    "_defer_start": True,
                })

                first = app.resume_after_api_error(
                    created["id"], 1, "claude-eval-demo"
                )
                second = app.resume_after_api_error(
                    created["id"], 1, "claude-eval-demo"
                )
                stored = app.run_row(created["id"])
                with app.db_connection() as database:
                    count = database.execute(
                        "SELECT COUNT(*) FROM events WHERE run_id = ? AND message = ?",
                        (created["id"], app.api_resume_event_message(1)),
                    ).fetchone()[0]

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(count, 1)
        self.assertGreater(int(stored["retry_not_before_epoch"] or 0), int(time.time()))
        self.assertIn("等待原会话恢复", stored["status_detail"])
        self.assertEqual(command.call_count, 3)

    def test_docker_monitor_preserves_user_interruption_without_reusing_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker"):
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "user-interrupted-demo",
                    "project_directory": ".",
                    "first_prompt": "完成这个项目",
                    "_defer_start": True,
                })
                app.update_run(created["id"], phase="first_running")
                app.update_turn(created["id"], 1, status="running")
                trace_state = {
                    "session_id": "session-interrupted",
                    "prompt_id": "prompt-interrupted",
                    "result": "",
                    "complete": False,
                    "api_error": "",
                    "interrupted": True,
                    "interruption_reason": "Claude 操作被用户中断，当前会话正在等待新的输入",
                    "path": root / "session-interrupted.jsonl",
                }
                with mock.patch.object(
                    app, "refresh_trace_snapshot", return_value=(root, trace_state)
                ), mock.patch.object(app, "export_and_remove_container") as export:
                    app.monitor_docker_turn(created["id"], 1)

                stored = app.serialize_run(app.run_row(created["id"]))

        self.assertEqual(stored["phase"], "interrupted")
        self.assertEqual(stored["turns"][0]["status"], "interrupted")
        self.assertIn("等待新的输入", stored["error"])
        export.assert_called_once_with(created["id"], force=True)

    def test_preserving_an_interrupted_turn_is_idempotent(self):
        row = {"phase": "interrupted"}
        with mock.patch.object(app, "run_row", return_value=row), mock.patch.object(
            app, "export_and_remove_container"
        ) as export, mock.patch.object(app, "update_run") as update:
            app.preserve_interrupted_docker_turn(
                "run-id", 1, "Claude 容器在本轮完成前已退出"
            )

        export.assert_not_called()
        update.assert_not_called()

    def test_bind_http_server_waits_without_running_recovery_side_effects(self):
        address_in_use = OSError(app.errno.EADDRINUSE, "Address already in use")
        server = object()
        with mock.patch.object(
            app, "ThreadingHTTPServer", side_effect=[address_in_use, server]
        ) as constructor, mock.patch.object(app.time, "sleep") as sleep:
            bound = app.bind_http_server("127.0.0.1", 8765)

        self.assertIs(bound, server)
        self.assertEqual(constructor.call_count, 2)
        sleep.assert_called_once_with(app.POLL_SECONDS)

    def test_auth_api_error_is_not_automatically_retried(self):
        self.assertFalse(app.retryable_api_error("API Error: 403 model unavailable"))
        self.assertTrue(app.retryable_api_error("API Error: 504 Gateway Time-out"))
        self.assertTrue(app.retryable_api_error("API Error: 429 rate limited"))
        self.assertTrue(
            app.retryable_api_error(
                "API Error: Unable to connect to API "
                "(UNKNOWN_CERTIFICATE_VERIFICATION_ERROR)"
            )
        )
        self.assertTrue(
            app.retryable_api_error(
                "API Error: Request rejected (429) · litellm.RateLimitError"
            )
        )
        self.assertTrue(
            app.retryable_api_error("API 错误：请求被拒绝（429）· 超出速率限制")
        )
        self.assertFalse(
            app.retryable_api_error("API Error: Request rejected (403) forbidden")
        )

    def test_stopped_container_is_preserved_as_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker"):
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "interrupted-demo",
                    "project_directory": ".",
                    "first_prompt": "完成容器化项目",
                    "_defer_start": True,
                })
                app.update_run(created["id"], phase="first_running")
                app.update_turn(created["id"], 1, status="running")
                with mock.patch.object(
                    app, "refresh_trace_snapshot", return_value=(root, None)
                ), mock.patch.object(
                    app, "docker_container_running", return_value=False
                ), mock.patch.object(app, "export_and_remove_container") as export:
                    app.monitor_docker_turn(created["id"], 1)

                stored = app.serialize_run(app.run_row(created["id"]))

        self.assertEqual(stored["phase"], "interrupted")
        self.assertEqual(stored["turns"][0]["status"], "interrupted")
        self.assertIn("本轮完成前已退出", stored["error"])
        export.assert_called_once_with(created["id"], force=True)

    def test_docker_monitor_marks_confirmation_without_writing_to_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker"):
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "attention-demo",
                    "project_directory": ".",
                    "first_prompt": "完成容器化项目",
                    "_defer_start": True,
                })
                app.update_run(created["id"], phase="first_running")

                def finish_monitor(_seconds):
                    app.update_run(created["id"], phase="stopped")

                with mock.patch.object(
                    app, "refresh_trace_snapshot", return_value=(root, None)
                ), mock.patch.object(
                    app, "docker_container_running", return_value=True
                ), mock.patch.object(
                    app,
                    "terminal_screen_text",
                    return_value="Do you want to proceed? 1. Yes 2. No",
                ), mock.patch.object(
                    app, "play_terminal_attention_sound", return_value=True
                ) as sound, mock.patch.object(
                    app.time, "monotonic", side_effect=[100.0, 161.0]
                ), mock.patch.object(
                    app.time, "sleep", side_effect=finish_monitor
                ), mock.patch.object(
                    app, "send_prompt_to_screen"
                ) as send_prompt:
                    app.monitor_docker_turn(created["id"], 1)

                stored = app.serialize_run(app.run_row(created["id"]))

        self.assertIn("等待人工确认", stored["status_detail"])
        sound.assert_called_once_with()
        send_prompt.assert_not_called()

    def test_six_hour_idle_container_is_archived_and_closed_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker"):
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "long-running-demo",
                    "project_directory": ".",
                    "first_prompt": "完成容器化项目",
                    "_defer_start": True,
                })
                app.update_run(created["id"], phase="first_running")
                old_started_at = app.datetime.fromtimestamp(
                    time.time() - app.RUN_TIMEOUT_SECONDS - 60
                ).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
                with app.db_connection() as database:
                    database.execute(
                        "UPDATE run_turns SET created_at = ? WHERE run_id = ? AND turn_number = 1",
                        (old_started_at, created["id"]),
                    )

                with mock.patch.object(
                    app, "refresh_trace_snapshot", return_value=(root, None)
                ), mock.patch.object(
                    app, "docker_container_running", return_value=True
                ), mock.patch.object(
                    app, "trace_activity_signature", return_value=None
                ), mock.patch.object(
                    app.time,
                    "monotonic",
                    side_effect=[0, app.INACTIVITY_WARNING_SECONDS + 1],
                ), mock.patch.object(app, "add_event") as event, mock.patch.object(
                    app, "send_prompt_to_screen"
                ) as send_prompt, mock.patch.object(app, "export_and_remove_container") as export:
                    app.monitor_docker_turn(created["id"], 1)

                stored = app.serialize_run(app.run_row(created["id"]))

        messages = [call.args[1] for call in event.call_args_list]
        self.assertTrue(any("超过 6 小时且连续 30 分钟" in message for message in messages))
        self.assertEqual(stored["phase"], "interrupted")
        self.assertIn("代码和轨迹已保留", stored["status_detail"])
        send_prompt.assert_not_called()
        export.assert_called_once_with(created["id"], force=True)


class ReviewTests(unittest.TestCase):
    def test_split_score_source_marker_triggers_attribution_repair(self):
        schema = app.evaluation_split_dimension_schema("delivery")
        self.assertIn("descriptionUsesIndependentReview", schema["required"])
        self.assertTrue(
            app.evaluation_public_description_needs_source_repair(
                {
                    "description": "产物检查发现依赖目录被提交。",
                    "descriptionUsesIndependentReview": True,
                }
            )
        )
        self.assertFalse(
            app.evaluation_public_description_needs_source_repair(
                {
                    "description": "后续独立复核发现依赖目录被提交。",
                    "descriptionUsesIndependentReview": True,
                }
            )
        )

    def test_regrade_forwards_source_marker_issue_to_targeted_repair(self):
        draft = sample_evaluation()
        for key in app.EVALUATION_DIMENSION_KEYS:
            draft[key]["score"] = 4
        draft["_initial_repair_issues"] = [
            "交付完整性描述引用后续独立复核证据但没有注明来源"
        ]
        with mock.patch.object(
            app, "run_codex_split_regrade", return_value=draft
        ), mock.patch.object(
            app,
            "review_evaluation_with_manual_fallback",
            side_effect=lambda evaluation, *_args, **_kwargs: (evaluation, ""),
        ) as review:
            app.run_codex_regrade(Path("."), "题面", [], "轨迹", 1)

        self.assertEqual(
            review.call_args.kwargs["initial_repair_issues"],
            ["交付完整性描述引用后续独立复核证据但没有注明来源"],
        )

    def test_regrade_runs_one_joint_calibration_only_when_total_exceeds_cap(self):
        over_cap = sample_evaluation()
        over_cap.update({
            "scores": [5, 5, 5, 5, 5],
            "descriptions": [
                over_cap[key]["description"]
                for key in app.EVALUATION_DIMENSION_KEYS
            ],
            "other": "无",
        })
        adjusted = json.loads(json.dumps(over_cap, ensure_ascii=False))
        for key in app.EVALUATION_DIMENSION_KEYS[1:]:
            adjusted[key]["score"] = 4
        adjusted["scores"] = [5, 4, 4, 4, 4]
        with mock.patch.object(
            app, "run_codex_split_regrade", return_value=over_cap
        ), mock.patch.object(
            app,
            "review_evaluation_with_manual_fallback",
            side_effect=lambda evaluation, *_args, **_kwargs: (evaluation, ""),
        ), mock.patch.object(
            app,
            "run_codex_evaluation_score_cap_calibration",
            return_value=adjusted,
        ) as calibrate:
            result = app.run_codex_regrade(
                Path("."), "题面", [], "轨迹", 1
            )

        calibrate.assert_called_once()
        self.assertEqual(result["scores"], [5, 4, 4, 4, 4])
        self.assertEqual(app.evaluation_total_score(result), 21)

    def test_regrade_preserves_over_cap_scores_and_warns_when_calibration_fails(self):
        over_cap = sample_evaluation()
        over_cap.update({
            "scores": [5, 5, 5, 5, 5],
            "descriptions": [
                over_cap[key]["description"]
                for key in app.EVALUATION_DIMENSION_KEYS
            ],
            "other": "无",
        })
        with mock.patch.object(
            app, "run_codex_split_regrade", return_value=over_cap
        ), mock.patch.object(
            app,
            "review_evaluation_with_manual_fallback",
            return_value=(over_cap, ""),
        ), mock.patch.object(
            app,
            "run_codex_evaluation_score_cap_calibration",
            side_effect=app.WorkflowError("没有可用降分事实"),
        ):
            result = app.run_codex_regrade(
                Path("."), "题面", [], "轨迹", 1
            )

        self.assertEqual(result["scores"], [5, 5, 5, 5, 5])
        self.assertIn("保留原始证据评分并阻止提交", result["_evaluation_warning"])

    def test_generation_failure_codes_keep_semantic_and_style_causes_separate(self):
        self.assertEqual(
            app.generation_failure_codes("与历史题面重复"),
            ["HISTORY_DUPLICATE"],
        )
        self.assertEqual(
            app.generation_failure_codes("题面表达模板化"),
            ["STYLE_ONLY"],
        )

    def test_generation_call_metrics_record_prompt_cost_and_attempts(self):
        previous = app.current_job_key()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(
                app, "run_codex_structured", return_value={"ok": True}
            ):
                app.initialize_database()
                app.CODEX_JOB_CONTEXT.key = "generation:test-run"
                try:
                    app.run_codex_generation_structured(
                        "精简题面",
                        {"type": "object"},
                        root,
                        "task-generation",
                        120,
                        reasoning_effort="medium",
                    )
                finally:
                    app.CODEX_JOB_CONTEXT.key = previous
                with app.db_connection() as database:
                    metric = database.execute(
                        "SELECT * FROM generation_call_metrics"
                    ).fetchone()

        self.assertEqual(metric["job_key"], "generation:test-run")
        self.assertEqual(metric["prefix"], "task-generation")
        self.assertEqual(metric["prompt_chars"], len("精简题面"))
        self.assertEqual(metric["attempt_count"], 1)
        self.assertEqual(metric["status"], "complete")

    def test_generation_structured_retries_only_transient_capacity_failure(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app,
            "run_codex_structured",
            side_effect=[
                app.WorkflowError("Selected model is at capacity"),
                {"ok": True},
            ],
        ) as runner, mock.patch.object(
            app, "wait_for_generation_retry"
        ) as wait:
            result = app.run_codex_generation_structured(
                "返回结果",
                {"type": "object"},
                Path(directory),
                "task-generation",
                120,
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args.kwargs["reasoning_effort"], "low")
        wait.assert_called_once_with(15)

    def test_generation_structured_does_not_retry_content_failure(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app,
            "run_codex_structured",
            side_effect=app.WorkflowError("题面与历史项目重复"),
        ) as runner, mock.patch.object(
            app, "wait_for_generation_retry"
        ) as wait, self.assertRaisesRegex(app.WorkflowError, "历史项目重复"):
            app.run_codex_generation_structured(
                "返回结果",
                {"type": "object"},
                Path(directory),
                "task-generation",
                120,
            )

        runner.assert_called_once()
        wait.assert_not_called()

    def test_worker_slot_is_reentrant_and_releases_shared_capacity(self):
        semaphore = threading.BoundedSemaphore(1)
        with mock.patch.object(app, "WORKER_SEMAPHORE", semaphore):
            with app.worker_slot():
                with app.worker_slot(timeout_seconds=0.01):
                    pass
            acquired = semaphore.acquire(blocking=False)
            self.assertTrue(acquired)
            if acquired:
                semaphore.release()

    def test_public_evaluation_history_keeps_only_qc_passed_prose(self):
        accepted = sample_evaluation()
        accepted["delivery"]["description"] = "历史 `交付` 点评"
        newer_accepted = sample_evaluation()
        newer_accepted["delivery"]["description"] = "较新的交付点评"
        rejected = sample_evaluation()
        rejected["delivery"]["description"] = "不应进入提示的返修点评"
        rows = [
            {
                "solo_qa_state": "qc_passed",
                "solo_qa_remote_submission_id": "6532",
                "turn_review_result": json.dumps(
                    {"evaluation": accepted}, ensure_ascii=False
                ),
                "turn_manual_evaluation": "",
            },
            {
                "solo_qa_state": "needs_fix",
                "solo_qa_remote_submission_id": "6546",
                "turn_review_result": json.dumps(
                    {"evaluation": rejected}, ensure_ascii=False
                ),
                "turn_manual_evaluation": "",
            },
            {
                "solo_qa_state": "qc_passed",
                "solo_qa_remote_submission_id": "6539",
                "turn_review_result": json.dumps(
                    {"evaluation": newer_accepted}, ensure_ascii=False
                ),
                "turn_manual_evaluation": "",
            },
        ]

        with mock.patch.object(app, "completed_turn_rows", return_value=rows):
            history = app.recent_qc_passed_public_evaluation_history(
                limit=1,
                include_account_remote=False,
            )

        self.assertEqual(history["delivery"], ["#6539 较新的交付点评"])
        self.assertTrue(
            all(len(history[key]) == 1 for key in app.EVALUATION_DIMENSION_KEYS)
        )
        self.assertFalse(
            any("6546" in entry for values in history.values() for entry in values)
        )

    def test_public_evaluation_history_prioritizes_b5_inflight_and_old_samples(self):
        def history_row(
            run_id,
            remote_id,
            state,
            updated_at,
            description,
            *,
            remote_status="",
            qc_summary="",
        ):
            evaluation = sample_evaluation()
            for key in app.EVALUATION_DIMENSION_KEYS:
                evaluation[key]["description"] = f"{description}-{key}"
            return {
                "run_id": run_id,
                "turn_number": 1,
                "turn_updated_at": updated_at,
                "solo_qa_state": state,
                "solo_qa_remote_status": remote_status,
                "solo_qa_remote_submission_id": remote_id,
                "solo_qa_qc_summary": qc_summary,
                "turn_review_result": json.dumps(
                    {"evaluation": evaluation}, ensure_ascii=False
                ),
                "turn_manual_evaluation": "",
            }

        rows = [
            history_row(
                "b5-rejected", "900", "needs_fix", "2026-09-13T12:30:00",
                "B5被拒点评", remote_status="PENDING_FIX",
                qc_summary="B-5 公共长片段与已交付数据 #100 重复",
            ),
            history_row(
                "inflight", "", "", "2026-09-13T12:20:00", "尚未提交点评"
            ),
            history_row(
                "ordinary-rejected", "901", "needs_fix", "2026-09-13T12:10:00",
                "普通返修点评", remote_status="PENDING_FIX",
                qc_summary="事实措辞需要调整",
            ),
            history_row(
                "discarded", "902", "discarded", "2026-09-13T12:00:00",
                "废弃点评", remote_status="DISCARDED",
            ),
        ]
        for index in range(10):
            rows.append(history_row(
                f"recent-{index}", str(800 - index), "qc_passed",
                f"2026-09-13T11:{59 - index:02d}:00", f"近期通过点评{index}",
                remote_status="QC_PASSED",
            ))
        rows.extend([
            history_row(
                "b5-reference", "100", "qc_passed", "2026-02-01T00:00:00",
                "被B5引用的旧点评", remote_status="QC_PASSED",
            ),
            history_row(
                "oldest", "50", "qc_passed", "2025-01-01T00:00:00",
                "全量扫描抽到的旧点评", remote_status="QC_PASSED",
            ),
        ])

        with mock.patch.object(app, "completed_turn_rows", return_value=rows):
            history = app.recent_qc_passed_public_evaluation_history(
                limit=8,
                max_chars=4_000,
                include_account_remote=False,
            )

        delivery = history["delivery"]
        self.assertTrue(any(entry.startswith("B-5引用 #100 ") for entry in delivery))
        self.assertTrue(any(entry.startswith("B-5反例 #900 ") for entry in delivery))
        self.assertTrue(any(entry.startswith("在途 inflight:1 ") for entry in delivery))
        self.assertTrue(any(entry.startswith("旧样本 #50 ") for entry in delivery))
        self.assertFalse(any("普通返修点评" in entry for entry in delivery))
        self.assertFalse(any("废弃点评" in entry for entry in delivery))
        self.assertTrue(all(len(values) <= 8 for values in history.values()))

    def test_public_evaluation_history_enforces_per_dimension_character_budget(self):
        rows = []
        for index in range(12):
            evaluation = sample_evaluation()
            for key in app.EVALUATION_DIMENSION_KEYS:
                evaluation[key]["description"] = f"第{index}条" + ("长点评" * 12)
            rows.append({
                "run_id": f"run-{index}",
                "turn_number": 1,
                "turn_updated_at": f"2026-09-13T11:{index:02d}:00",
                "solo_qa_state": "qc_passed",
                "solo_qa_remote_status": "QC_PASSED",
                "solo_qa_remote_submission_id": str(700 + index),
                "turn_review_result": json.dumps(
                    {"evaluation": evaluation}, ensure_ascii=False
                ),
                "turn_manual_evaluation": "",
            })

        with mock.patch.object(app, "completed_turn_rows", return_value=rows):
            history = app.recent_qc_passed_public_evaluation_history(
                limit=20,
                max_chars=180,
                include_account_remote=False,
            )

        for values in history.values():
            self.assertLessEqual(len("\n".join(values)), 180)
            self.assertLess(len(values), len(rows))

    def test_evaluation_rubric_is_loaded_from_doc(self):
        rubric = app.evaluation_rubric_text()
        self.assertIn("交付完整性 (Delivery)", rubric)
        self.assertIn("执行能力(Toolcall)", rubric)
        self.assertIn("5分 (完美/超预期)", rubric)
        self.assertIn("1分 (完全不可用/严重事故)", rubric)
        self.assertIn("不索取或猜测不可见的内部思维", rubric)
        self.assertNotIn("reasoning token", rubric)
        self.assertNotIn("CoT较长", rubric)

    def test_split_regrade_runs_dimensions_concurrently_and_assembles_v2_in_fixed_order(self):
        keys = list(app.EVALUATION_DIMENSION_KEYS)
        all_dimensions_started = threading.Event()
        completion_events = {key: threading.Event() for key in keys}
        reverse_keys = list(reversed(keys))
        previous_in_completion = {
            key: reverse_keys[index - 1]
            for index, key in enumerate(reverse_keys)
            if index
        }
        state_lock = threading.Lock()
        active_dimensions = 0
        max_active_dimensions = 0
        started_dimensions = set()
        completion_order = []
        artifact_findings = (
            "当前产物为 commit aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa；"
            "运行条件为临时仓库；检查覆盖源码读取；"
            "0 项通过、0 项失败、0 项跳过；未验证范围为浏览器交互。"
        )
        metadata = {
            "task_type": "Feature 迭代",
            "task_difficulty": "困难",
            "language_framework": "Python",
            "environment_reproducibility": "已容器化，可一键起环境",
            "other_issues": "无",
            "artifactFindings": artifact_findings,
        }

        def structured_result(_prompt, _schema, _cwd, prefix, _timeout, **_kwargs):
            nonlocal active_dimensions, max_active_dimensions
            if prefix == "parallel-metadata":
                return metadata
            key = prefix.removeprefix("parallel-")
            with state_lock:
                active_dimensions += 1
                max_active_dimensions = max(max_active_dimensions, active_dimensions)
                started_dimensions.add(key)
                if len(started_dimensions) == len(keys):
                    all_dimensions_started.set()
            try:
                self.assertTrue(all_dimensions_started.wait(3))
                previous = previous_in_completion.get(key)
                if previous:
                    self.assertTrue(completion_events[previous].wait(3))
                with state_lock:
                    completion_order.append(key)
                completion_events[key].set()
                index = keys.index(key)
                return {
                    "score": index + 1,
                    "description": f"description-{key}",
                    "when": f"when-{key}",
                    "behavior": f"behavior-{key}",
                    "impact": f"impact-{key}",
                    "expected": f"expected-{key}",
                    "evidenceRefs": f"{key}.py:1",
                    "processFinding": (
                        f"{app.EVALUATION_DIMENSION_LABELS[key]}={index + 1}分；"
                        f"事实={key}.py:1"
                    ),
                }
            finally:
                with state_lock:
                    active_dimensions -= 1

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app, "run_codex_structured", side_effect=structured_result
        ) as runner:
            result = app.run_codex_split_regrade(
                Path(directory),
                "原始题面",
                [],
                "轨迹",
                1,
                call_prefix="parallel",
            )

        self.assertEqual(max_active_dimensions, 5)
        self.assertEqual(completion_order, reverse_keys)
        self.assertEqual(result["score_stage_version"], 2)
        self.assertEqual(result["scores"], [1, 2, 3, 4, 5])
        self.assertEqual(
            result["descriptions"], [f"description-{key}" for key in keys]
        )
        for field in ("when", "behavior", "impact", "expected"):
            self.assertEqual(result[field], [f"{field}-{key}" for key in keys])
        self.assertEqual(
            result["evidenceRefs"], [f"{key}.py:1" for key in keys]
        )
        self.assertTrue(result["processFindings"].startswith("评分版本 2；"))
        process_positions = [
            result["processFindings"].index(app.EVALUATION_DIMENSION_LABELS[key])
            for key in keys
        ]
        self.assertEqual(process_positions, sorted(process_positions))
        self.assertEqual(result["artifactFindings"], artifact_findings)
        self.assertEqual(runner.call_count, 6)

    def test_review_score_retry_keeps_bug_findings_and_notifies_before_scoring(self):
        findings = {
            "summary": "发现确定问题",
            "next_action": "bugfix",
            "bugs": [
                {
                    "severity": "中",
                    "title": "状态未更新",
                    "reproduction": "提交完成状态后重新打开详情",
                    "actual": "详情仍显示处理前状态",
                    "expected": "详情显示最新完成状态",
                    "evidence": "实际请求成功后查询仍返回旧状态",
                    "fix": "在事务中保存完成状态",
                    "customer_summary": (
                        "提交完成状态后详情仍显示旧状态，正确结果应展示最新状态"
                    ),
                }
            ],
            "quality_gaps": [],
        }
        checkpoints = []
        transient = app.WorkflowError("API Error: 504 Gateway Time-out")

        def fail_scoring(*_args, **_kwargs):
            self.assertEqual(len(checkpoints), 1)
            self.assertEqual(checkpoints[0]["bugs"][0]["title"], "状态未更新")
            self.assertIn("提交完成状态后", checkpoints[0]["repair_prompt"])
            raise transient

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app, "run_codex_structured", return_value=findings
        ) as findings_runner, mock.patch.object(
            app, "run_codex_regrade", side_effect=fail_scoring
        ) as scorer:
            with self.assertRaises(app.WorkflowError) as raised:
                app.run_codex_review(
                    Path(directory),
                    "原始题面",
                    [],
                    "轨迹",
                    findings_notifier=lambda value: checkpoints.append(value),
                )

        self.assertIs(raised.exception, transient)
        findings_runner.assert_called_once()
        scorer.assert_called_once()
        self.assertEqual(len(checkpoints), 1)
        saved = raised.exception.review_result
        self.assertEqual(saved["next_action"], "bugfix")
        self.assertEqual(saved["bugs"][0]["title"], "状态未更新")
        self.assertIn("提交完成状态后", saved["repair_prompt"])
        self.assertIn("504 Gateway Time-out", saved["evaluation_blocker"])
        self.assertTrue(app.retryable_control_error(str(raised.exception)))

    def test_split_regrade_wording_exhaustion_falls_back_without_losing_findings(self):
        findings = {
            "summary": "代码复核完成",
            "next_action": "complete",
            "bugs": [],
            "quality_gaps": [],
        }
        draft = sample_evaluation()
        latest = sample_evaluation()
        latest["planning"]["description"] = "本轮评分仍需要人工修订。"
        exhausted = app.EvaluationRepairExhausted(
            "自动检查的 planning 描述包含高风险公共片段：修复仅限上述问题",
            latest,
        )

        with mock.patch.object(
            app, "run_codex_split_regrade", return_value=draft
        ), mock.patch.object(
            app,
            "normalize_evaluation_with_targeted_repairs",
            side_effect=exhausted,
        ), mock.patch.object(
            app, "SOLO_QA_MAX_TOTAL_SCORE", 25
        ):
            result = app.score_review_findings(
                findings,
                None,
                Path("."),
                "原始题面",
                [],
                "本轮轨迹",
                1,
                None,
                call_prefix="fallback",
            )

        self.assertEqual(result["next_action"], "complete")
        self.assertEqual(result["bugs"], [])
        self.assertEqual(result["evaluation"]["planning"], latest["planning"])
        self.assertEqual(result["evaluation"]["scores"], [5, 5, 5, 5, 5])
        self.assertNotIn("_evaluation_warning", result["evaluation"])
        self.assertIn("修复仅限上述问题", result["evaluation_warning"])

    def test_review_findings_checkpoint_is_atomic_and_resumable(self):
        findings = {
            "summary": "发现确定问题",
            "next_action": "bugfix",
            "bugs": [{"title": "状态未更新"}],
            "quality_gaps": [],
            "repair_prompt": "提交完成状态后详情应展示最新状态。",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    for run_id in ("persist111111", "rollback11111"):
                        database.execute(
                            """INSERT INTO runs(
                              id, repo_name, repo_path, phase, first_prompt,
                              first_verification, verification_commands,
                              created_at, updated_at
                            ) VALUES (?, ?, ?, 'review_running', ?, '[]', '[]', ?, ?)""",
                            (
                                run_id,
                                f"review-{run_id}",
                                str(root / run_id),
                                "原始题面",
                                timestamp,
                                timestamp,
                            ),
                        )
                        database.execute(
                            """INSERT INTO run_turns(
                              run_id, turn_number, intent_type, prompt, status,
                              verification, created_at, updated_at
                            ) VALUES (?, 1, '0-1 代码生成', '原始题面',
                                      'reviewing', '[]', ?, ?)""",
                            (run_id, timestamp, timestamp),
                        )

                self.assertTrue(
                    app.persist_review_findings_before_scoring(
                        "persist111111",
                        1,
                        "review_running",
                        "review_result",
                        findings,
                    )
                )
                turn_saved = json.loads(
                    app.turn_row("persist111111", 1)["review_result"]
                )
                run_saved = json.loads(
                    app.run_row("persist111111")["review_result"]
                )
                self.assertEqual(turn_saved, run_saved)
                self.assertEqual(turn_saved["evaluation_blocker"], "五维评分进行中")
                self.assertEqual(turn_saved["bugs"][0]["title"], "状态未更新")
                resumed = app.resumable_review_findings(turn_saved, "bugs")
                self.assertEqual(resumed, findings)

                with app.db_connection() as database:
                    database.execute(
                        """CREATE TRIGGER block_review_result_mirror
                           BEFORE UPDATE OF review_result ON runs
                           BEGIN
                             SELECT RAISE(ABORT, 'mirror write blocked');
                           END"""
                    )
                with self.assertRaises(app.sqlite3.IntegrityError):
                    app.persist_review_findings_before_scoring(
                        "rollback11111",
                        1,
                        "review_running",
                        "review_result",
                        findings,
                    )
                self.assertIsNone(
                    app.turn_row("rollback11111", 1)["review_result"]
                )
                self.assertIsNone(app.run_row("rollback11111")["review_result"])

    def test_regrade_uses_rubric_without_requesting_code_changes(self):
        def score_part(_prompt, _schema, _cwd, prefix, _timeout, **_kwargs):
            if prefix == "turn-regrade-metadata":
                return {
                    "task_type": "Feature 迭代",
                    "task_difficulty": "中等",
                    "language_framework": "Python",
                    "environment_reproducibility": "未提供容器配置",
                    "other_issues": "无",
                    "artifactFindings": (
                        "当前产物为本轮代码；实际运行条件为临时目录；"
                        "检查覆盖 make test；12 项通过、0 项失败、0 项跳过；"
                        "未验证范围为页面交互。"
                    ),
                }
            key = prefix.removeprefix("turn-regrade-")
            label = app.EVALUATION_DIMENSION_LABELS[key]
            return {
                "score": 5,
                "description": f"第 1 轮已核对 {key}.py 与 make test 的最终结果。",
                "when": "第 1 轮第 1 步操作",
                "behavior": f"核对 {key}.py 并执行 make test",
                "impact": "本轮交付与验收结果一致",
                "expected": "保持当前行为",
                "evidenceRefs": f"{key}.py:1",
                "processFinding": (
                    f"{label}=5分；事实={key}.py:1；"
                    "相邻4分差别=无可核验缺口"
                ),
            }

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            with mock.patch.object(
                app, "run_codex_structured", side_effect=score_part
            ) as runner:
                result = app.run_codex_split_regrade(
                    repo,
                    "增加拒收流程",
                    [{"command": "make test", "exit_code": 0, "output": "ok"}],
                    (
                        'TOOL Bash: {"command": "python -m pytest -q"}\n'
                        'TOOL RESULT: 12 passed in 1.0s'
                    ),
                    1,
                )

        dimension_calls = [
            call for call in runner.call_args_list
            if call.args[3] != "turn-regrade-metadata"
        ]
        delivery_call = next(
            call for call in dimension_calls
            if call.args[3] == "turn-regrade-delivery"
        )
        prompt = delivery_call.args[0]
        self.assertIn(app.EVALUATION_SCORE_GUIDANCE, prompt)
        self.assertIn(app.EVALUATION_FACT_ATTRIBUTION_GUIDANCE, prompt)
        self.assertIn("交付完整性 (Delivery)", prompt)
        self.assertIn("只独立评定第 1 轮", prompt)
        self.assertIn("不得调用 shell", prompt)
        self.assertIn("不得再次检查仓库", prompt)
        self.assertIn("后端检查：最后记录 12 项通过、0 项失败", prompt)
        self.assertIn("后出现的结果覆盖同类早期结果", prompt)
        self.assertEqual(len(dimension_calls), 5)
        for call in runner.call_args_list:
            self.assertEqual(call.kwargs["sandbox"], "read-only")
            self.assertEqual(call.kwargs["reasoning_effort"], "medium")
        self.assertEqual(result["task_type"], "Feature 迭代")

    def test_codex_review_uses_pinned_model_and_structured_output(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()

            testcase = self

            class FakeProcess:
                def __init__(self, args, **kwargs):
                    self.args = args
                    self.returncode = 0
                    self.pid = 999999

                def communicate(self, input=None, timeout=None):
                    args = self.args
                    testcase.assertIn("gpt-5.6-sol", args)
                    testcase.assertIn("workspace-write", args)
                    testcase.assertIn("--ephemeral", args)
                    testcase.assertIn("--ignore-user-config", args)
                    testcase.assertIn("--ignore-rules", args)
                    testcase.assertIn(app.BUG_REPAIR_PROMPT_STYLE_GUIDANCE, input)
                    testcase.assertIn("与本轮范围无关的历史问题", input)
                    testcase.assertIn("不得要求修改相应代码", input)
                    testcase.assertIn("本次只返回代码复核结论", input)
                    testcase.assertIn("本次不能输出 evaluation", input)
                    testcase.assertNotIn(app.EVALUATION_SCORE_GUIDANCE, input)
                    testcase.assertNotIn(app.EVALUATION_FACT_ATTRIBUTION_GUIDANCE, input)
                    testcase.assertNotIn("交付完整性 (Delivery)", input)
                    output_path = Path(args[args.index("--output-last-message") + 1])
                    output_path.write_text(
                        json.dumps({
                            "summary": "发现一个事务问题",
                            "next_action": "bugfix",
                            "bugs": [
                                {
                                    "severity": "高",
                                    "title": "并发写入产生重复记录",
                                    "reproduction": "启动两个独立连接并同步提交同一业务键",
                                    "actual": "两个请求均成功并生成两条记录",
                                    "expected": "只能有一个请求创建记录",
                                    "evidence": "并发命令返回两个 201，数据库查询得到两行",
                                    "fix": "增加唯一约束并处理冲突",
                                    "customer_summary": "同一业务键同时提交会生成两条记录，正确结果只能保留一条",
                                }
                            ],
                            "quality_gaps": [],
                        }, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    return "", ""

            with mock.patch.object(
                app.subprocess, "Popen", side_effect=FakeProcess
            ), mock.patch.object(
                app, "run_codex_regrade", return_value=sample_evaluation()
            ) as scorer:
                result = app.run_codex_review(repo, "原始题面", [])

            self.assertEqual(result["bugs"][0]["severity"], "高")
            self.assertNotIn("\n", result["repair_prompt"])
            self.assertEqual(
                result["repair_prompt"],
                "同一业务键同时提交会生成两条记录，正确结果只能保留一条。",
            )
            scorer.assert_called_once()

    def test_followup_bug_prompt_uses_the_same_natural_style_guidance(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            completed = {
                "summary": "修复已通过",
                "next_action": "complete",
                "remaining_bugs": [],
                "quality_gaps": [],
            }
            with mock.patch.object(
                app, "run_codex_structured", return_value=completed
            ) as runner, mock.patch.object(
                app,
                "run_codex_regrade",
                return_value=sample_evaluation("Bug 修复"),
            ) as scorer:
                app.run_codex_final_review(
                    repo, "原始需求", "修复当前问题", [], "轨迹"
                )

        prompt = runner.call_args.args[0]
        self.assertIn(app.BUG_REPAIR_PROMPT_STYLE_GUIDANCE, prompt)
        self.assertIn("每个 Bug 另写一条 customer_summary", prompt)
        self.assertIn("与本轮范围无关的历史问题", prompt)
        self.assertIn("不得要求修改相应代码", prompt)
        self.assertIn("本次只返回代码复核结论", prompt)
        self.assertIn("本次不能输出 evaluation", prompt)
        self.assertNotIn(app.EVALUATION_SCORE_GUIDANCE, prompt)
        self.assertNotIn(app.EVALUATION_FACT_ATTRIBUTION_GUIDANCE, prompt)
        scorer.assert_called_once()
        self.assertEqual(
            scorer.call_args.kwargs["call_prefix"],
            "final-review-2-evaluation",
        )

    def test_final_review_repairs_only_rejected_dimension_description(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            evaluation = sample_evaluation("Bug 修复")
            original_delivery = dict(evaluation["delivery"])
            evaluation["planning"] = {
                "score": 4,
                "description": (
                    "第 2 轮的规划遗漏了关键检查"
                ),
            }
            completed = {
                "summary": "修复已通过",
                "next_action": "complete",
                "remaining_bugs": [],
                "quality_gaps": [],
                "evaluation": evaluation,
            }
            repaired = {
                "description": (
                    "第 2 轮的规划遗漏了兼容入口检查。"
                    "随后查看 App.tsx 才补齐该步骤，导致一次返工。"
                )
            }
            notifier = mock.Mock()
            with mock.patch.object(
                app,
                "run_codex_structured",
                side_effect=[completed, repaired],
            ) as runner:
                result = app.run_codex_final_review(
                    repo,
                    "原始需求",
                    "修复当前问题",
                    [],
                    'TOOL Read: {"path": "App.tsx"}\n'
                    "TOOL RESULT: 已读取并修正兼容入口。",
                    evaluation_repair_notifier=notifier,
                )

        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args_list[0].args[3], "final-review")
        self.assertEqual(
            runner.call_args_list[1].args[3], "planning-description-repair"
        )
        self.assertEqual(result["evaluation"]["delivery"], original_delivery)
        self.assertEqual(
            result["evaluation"]["planning"]["description"],
            repaired["description"],
        )
        self.assertEqual(result["remaining_bugs"], [])
        notifier.assert_called_once()

    def test_review_wording_exhaustion_keeps_code_result_for_manual_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            evaluation = sample_evaluation("Bug 修复")
            evaluation["planning"] = {
                "score": 4,
                "description": (
                    "第 2 轮的规划遗漏了关键检查"
                ),
            }
            completed = {
                "summary": "代码复核已经完成",
                "next_action": "complete",
                "remaining_bugs": [],
                "quality_gaps": [],
                "evaluation": evaluation,
            }
            still_invalid = {
                "description": (
                    "第 2 轮的规划仍遗漏了检查"
                )
            }
            with mock.patch.object(
                app,
                "run_codex_structured",
                side_effect=[
                    completed,
                    still_invalid,
                    still_invalid,
                    still_invalid,
                    still_invalid,
                    still_invalid,
                ],
            ) as runner:
                result = app.run_codex_final_review(
                    repo,
                    "原始需求",
                    "修复当前问题",
                    [],
                    "本轮完成了修改。",
                )

        self.assertEqual(runner.call_count, 6)
        self.assertEqual(result["summary"], "代码复核已经完成")
        self.assertEqual(result["next_action"], "complete")
        self.assertIn("至少两个完整句子", result["evaluation_warning"])
        self.assertEqual(result["evaluation"]["planning"]["score"], 4)
        self.assertEqual(
            result["evaluation"]["planning"]["description"],
            still_invalid["description"],
        )

    def test_targeted_repairs_validate_after_fourth_rewrite(self):
        evaluation = sample_evaluation("0-1 代码生成")
        normalized = sample_evaluation("0-1 代码生成")
        errors = [
            app.WorkflowError("自动检查的指令遵循非满分描述需要至少两个完整句子"),
            app.WorkflowError("自动检查的推理能力非满分描述需要至少两个完整句子"),
            app.WorkflowError("自动检查的推理能力描述包含高风险公共片段：修复仅限上述问题"),
            app.WorkflowError("自动检查的指令遵循描述与本轮最后一次检查结果矛盾"),
        ]
        with mock.patch.object(
            app,
            "normalize_evaluation",
            side_effect=[*errors, normalized],
        ) as validator, mock.patch.object(
            app,
            "run_codex_evaluation_dimension_repair",
            return_value="第 1 轮存在有证据的具体不足。该问题造成了实际影响。",
        ) as repair, mock.patch.object(
            app,
            "validate_evaluation_final_verification_consistency",
        ), mock.patch.object(
            app,
            "validate_evaluation_trace_commands",
        ):
            result = app.normalize_evaluation_with_targeted_repairs(
                evaluation,
                1,
                Path("."),
                "原始需求",
                [],
                "轨迹",
            )

        self.assertEqual(result, normalized)
        self.assertEqual(validator.call_count, 5)
        self.assertEqual(repair.call_count, 4)

    def test_v2_targeted_repair_requests_complete_dimension_evidence(self):
        evaluation = sample_evaluation("Bug 修复")
        evaluation["score_stage_version"] = 2
        repaired = {
            "score": 4,
            "description": (
                "第 1 轮在 src/App.css 的 280px 场景首次检查失败。"
                "这个遗漏导致一次样式返工，修正后专项检查通过。"
            ),
            "when": "第 1 轮第 6 步调用",
            "behavior": "280px 页面检查暴露文件输入框越界，随后修改 src/App.css。",
            "impact": "首次页面检查出现 1 项失败，需要增加定位和样式修正。",
            "expected": "首次修改时同时检查文件输入框的固有宽度。",
            "evidenceRefs": "src/App.css:215;e2e/upload.spec.ts:88",
            "processFinding": (
                "执行能力=4分；事实=280px 场景首次失败后修正；"
                "相邻3分差别=一次局部返工后完成；相邻5分差别=没有一次完成"
            ),
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app, "run_codex_structured", return_value=repaired
        ) as runner:
            result = app.run_codex_evaluation_dimension_repair(
                Path(directory),
                "修复 280px 页面溢出",
                [],
                "轨迹",
                evaluation,
                "execution",
                "执行能力",
                1,
                "自动检查的执行能力满分描述包含扣分点",
            )

        schema = runner.call_args.args[1]
        self.assertEqual(
            set(schema["required"]),
            {
                "score", "description", "when", "behavior", "impact", "expected",
                "evidenceRefs", "processFinding",
            },
        )
        prompt = runner.call_args.args[0]
        self.assertIn("属于当前维度就降低分数", prompt)
        self.assertIn("只属于其他维度就保持当前维度的正确分数", prompt)
        self.assertEqual(result, repaired)

    def test_completed_record_repair_prompt_keeps_the_saved_score_locked(self):
        evaluation = sample_evaluation("Bug 修复")
        repaired = {
            "score": 5,
            "description": "保存记录显示第 1 轮按题面核对了交付结果，现有验收项都有对应输出。",
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app, "run_codex_structured", return_value=repaired
        ) as runner:
            result = app.run_codex_evaluation_dimension_repair(
                Path(directory),
                "核对交付结果",
                [],
                "轨迹",
                evaluation,
                "delivery",
                "交付完整性",
                1,
                "自动检查的交付完整性满分描述写入了失败或返工",
                preserve_score=True,
            )

        prompt = runner.call_args.args[0]
        self.assertIn("不得降低分数", prompt)
        self.assertIn("转人工", prompt)
        self.assertIn("完整文件名或路径", prompt)
        self.assertIn("不能交换数字归属", prompt)
        self.assertNotIn("属于当前维度就降低分数", prompt)
        self.assertEqual(result, repaired)

    def test_v2_targeted_repair_synchronizes_all_dimension_mirrors(self):
        evaluation = sample_evaluation("Bug 修复")
        dimension_count = len(app.EVALUATION_DIMENSION_KEYS)
        evaluation.update({
            "score_stage_version": 2,
            "scores": [5] * dimension_count,
            "descriptions": [
                evaluation[key]["description"] for key in app.EVALUATION_DIMENSION_KEYS
            ],
            "other": "无",
            "when": [f"第 1 轮第 {index + 1} 步操作" for index in range(dimension_count)],
            "behavior": [f"原 behavior {key}" for key in app.EVALUATION_DIMENSION_KEYS],
            "impact": [f"原 impact {key}" for key in app.EVALUATION_DIMENSION_KEYS],
            "expected": [f"原 expected {key}" for key in app.EVALUATION_DIMENSION_KEYS],
            "evidenceRefs": [f"{key}.py:1" for key in app.EVALUATION_DIMENSION_KEYS],
            "processFindings": "评分版本 2；" + "；".join(
                f"{app.EVALUATION_DIMENSION_LABELS[key]}=5分；事实={key}；"
                "相邻4分差别=没有当前维度缺口"
                for key in app.EVALUATION_DIMENSION_KEYS
            ),
            "artifactFindings": "当前产物已经完成检查。",
        })
        evaluation["execution"]["description"] = (
            "第 1 轮先完成 TypeScript 检查和 23 个 Vitest 用例。"
            "新增场景最初出现 1 项窄屏失败，定位并修正上传控件后 4 项专项场景通过。"
        )
        evaluation["descriptions"][-1] = evaluation["execution"]["description"]
        repaired = {
            "score": 4,
            "description": (
                "文件输入框在第 1 轮 src/App.css 的 280px 场景首次检查失败，遗漏了固有宽度。"
                "这个遗漏导致额外定位和一次样式返工，修正后专项检查通过。"
            ),
            "when": "第 1 轮第 6 步调用",
            "behavior": "页面检查暴露文件输入框越界，随后修改 src/App.css。",
            "impact": "首次检查出现 1 项失败，需要增加定位和样式修正。",
            "expected": "首次修改时同时检查文件输入框的固有宽度。",
            "evidenceRefs": "src/App.css:215;e2e/upload.spec.ts:88",
            "processFinding": (
                "执行能力=4分；事实=280px 场景首次失败后修正；"
                "相邻3分差别=一次局部返工后完成；相邻5分差别=没有一次完成"
            ),
        }
        trajectory = (
            'TOOL Read: {"path": "src/App.css"}\n'
            "TOOL RESULT: upload input width styles\n"
            'TOOL Bash: {"command": "run narrow-page check"}\n'
            "TOOL RESULT: 280px: 1 failed\n"
        )
        with mock.patch.object(
            app, "run_codex_evaluation_dimension_repair", return_value=repaired
        ) as repair:
            result = app.normalize_evaluation_with_targeted_repairs(
                evaluation,
                1,
                Path("."),
                "修复 280px 页面溢出",
                [],
                trajectory,
            )

        execution_index = app.EVALUATION_DIMENSION_KEYS.index("execution")
        self.assertEqual(result["execution"]["score"], 4)
        self.assertEqual(result["scores"][execution_index], 4)
        self.assertEqual(
            result["descriptions"][execution_index], repaired["description"]
        )
        for field in app.EVALUATION_SCORE_STAGE_DETAIL_FIELDS:
            self.assertEqual(result[field][execution_index], repaired[field])
        self.assertIn(repaired["processFinding"], result["processFindings"])
        self.assertNotIn("执行能力=5分", result["processFindings"])
        self.assertIn("任务规划=5分", result["processFindings"])
        repair.assert_called_once()

    def test_generation_environment_only_deduction_can_be_repaired_to_full_score(self):
        evaluation = sample_evaluation("Feature 迭代")
        evaluation["execution"] = {
            "score": 4,
            "description": (
                "第 1 轮系统解释器不可用，导致验证过程变慢。"
                "之后准备环境并完成检查。"
            ),
        }
        repaired = {
            "score": 5,
            "description": "第 1 轮逐项核对题面约束并完成验证，交付记录与轨迹一致。",
        }
        with mock.patch.object(
            app,
            "run_codex_evaluation_dimension_repair",
            return_value=repaired,
        ) as repair:
            result = app.normalize_evaluation_with_targeted_repairs(
                evaluation,
                1,
                Path("."),
                "原始需求",
                [],
                "第 1 轮完成了功能修改和验证。",
            )

        self.assertEqual(result["execution"], repaired)
        repair.assert_called_once()

    def test_targeted_repair_loop_exhaustion_can_fall_back_to_manual_edit(self):
        evaluation = sample_evaluation("Bug 修复")
        latest = sample_evaluation("Bug 修复")
        latest["planning"]["description"] = "第 2 轮最后一次定向修正的描述。"
        with mock.patch.object(
            app,
            "normalize_evaluation_with_targeted_repairs",
            side_effect=app.EvaluationRepairExhausted(
                "评分描述定向修正未能收敛", latest
            ),
        ):
            result, warning = app.review_evaluation_with_manual_fallback(
                evaluation,
                2,
                Path("."),
                "修复需求",
                [],
                "轨迹",
            )

        self.assertEqual(result["planning"], latest["planning"])
        self.assertEqual(warning, "评分描述定向修正未能收敛")

    def test_review_scoring_uses_only_saved_finding_evidence_for_grounding(self):
        evaluation = sample_evaluation("Bug 修复")
        findings = {
            "summary": "summary_only_anchor",
            "remaining_bugs": [
                {
                    "evidence": "probe_timeout_after_2s",
                    "expected": "expected_only_anchor",
                }
            ],
        }
        with mock.patch.object(
            app,
            "normalize_evaluation_with_targeted_repairs",
            return_value=evaluation,
        ) as validator:
            result, warning = app.review_evaluation_with_manual_fallback(
                evaluation,
                2,
                Path("."),
                "修复需求",
                [],
                "轨迹",
                review_findings=findings,
            )

        self.assertEqual(result, evaluation)
        self.assertEqual(warning, "")
        evidence = validator.call_args.kwargs["supplemental_evidence"]
        self.assertEqual(evidence, "probe_timeout_after_2s")
        self.assertNotIn("summary_only_anchor", evidence)
        self.assertNotIn("expected_only_anchor", evidence)

    def test_bug_repair_prompt_uses_direct_natural_single_line(self):
        bugs = app.normalize_bugs([
            {
                "severity": "高",
                "title": "重复确认",
                "reproduction": "两人同时提交同一个接收码",
                "actual": "容器位置更新了两次",
                "expected": "容器只移动一次且两人看到相同结果",
                "evidence": "两个请求都返回成功且时间线新增两条记录",
                "fix": "让确认操作保持幂等",
                "customer_summary": "两人同时确认会让容器移动两次，正确结果只能移动一次",
            },
            {
                "severity": "中",
                "title": "过期码仍可使用",
                "reproduction": "等待交接超时后提交原接收码",
                "actual": "系统仍然完成接收",
                "expected": "系统提示交接过期并保持原位置",
                "evidence": "超时后接口返回成功且容器位置发生变化",
                "fix": "按服务端时间阻止过期确认",
                "customer_summary": "交接超时后旧接收码仍能使用，应该提示过期并保持原位置",
            },
        ])

        prompt = app.bug_repair_prompt(bugs, "run-123:2")
        self.assertNotIn("\n", prompt)
        self.assertEqual(
            prompt,
            "两人同时确认会让容器移动两次，正确结果只能移动一次；"
            "交接超时后旧接收码仍能使用，应该提示过期并保持原位置。",
        )
        self.assertEqual(prompt, app.bug_repair_prompt(bugs, "run-123:2"))
        self.assertEqual(prompt, app.bug_repair_prompt(bugs, "another-run:8"))

    def test_bug_repair_prompt_stops_unchanged_issue_for_manual_confirmation(self):
        bugs = [{
            "customer_summary": "盘点标签末尾有空白行时仍会生成记录，应该提示标签无效并保持记录不变",
        }]
        previous = "盘点标签末尾有空白行时仍会生成记录，应该提示标签无效并保持记录不变。"

        with self.assertRaisesRegex(app.WorkflowError, "人工确认"):
            app.bug_repair_prompt(bugs, previous_prompt=previous)

    def test_bug_repair_prompt_allows_proven_residual_state(self):
        bugs = [{
            "customer_summary": "上轮已拦截单个尾随换行，但连续两个空行仍会生成记录并消耗实物序号",
        }]
        previous = "盘点标签末尾有空白行时仍会生成记录，应该提示标签无效并保持记录不变。"

        self.assertEqual(
            app.bug_repair_prompt(bugs, previous_prompt=previous),
            "上轮已拦截单个尾随换行，但连续两个空行仍会生成记录并消耗实物序号。",
        )

    def test_bug_customer_summary_repairs_harmless_formatting(self):
        bug = {
            "severity": "中",
            "title": "导出提前开放",
            "reproduction": "保留区间还没有人工确认时打开导出面板",
            "actual": "两个下载入口已经可用",
            "expected": "所有区间确认前都应保持锁定",
            "evidence": "页面显示待确认一项但按钮没有禁用",
            "fix": "按待确认数量控制下载入口",
            "customer_summary": "1. 未确认时导出入口显示为“可用”，应该继续锁定",
        }

        normalized = app.normalize_bugs([bug])

        self.assertEqual(
            normalized[0]["customer_summary"],
            "未确认时导出入口显示为可用，应该继续锁定",
        )

    def test_bug_customer_summary_allows_observable_quantity_increase(self):
        bug = {
            "severity": "中",
            "title": "空行生成记录",
            "reproduction": "盘点标签末尾保留空白行后提交",
            "actual": "盘点记录数量增加一条",
            "expected": "提示标签无效且记录数量不变",
            "evidence": "提交前后一共多出一条盘点记录",
            "fix": "拒绝包含空白行的标签列表",
            "customer_summary": (
                "2. 盘点标签末尾有空白行时，记录数量仍会增加。"
                "正确结果应提示标签无效，并保持记录不变！"
            ),
        }

        normalized = app.normalize_bugs([bug])

        self.assertEqual(
            normalized[0]["customer_summary"],
            "盘点标签末尾有空白行时，记录数量仍会增加，正确结果应提示标签无效，并保持记录不变",
        )

    def test_test_coverage_gap_does_not_become_a_bugfix_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            review = {
                "summary": "业务行为通过，只有测试覆盖建议",
                "next_action": "complete",
                "bugs": [],
                "quality_gaps": [
                    {
                        "title": "缺少真实 PostgreSQL 并发测试",
                        "evidence": "当前并发测试使用 SQLite",
                        "recommendation": "后续补充 PostgreSQL 回归测试",
                    }
                ],
            }
            with mock.patch.object(
                app, "run_codex_structured", return_value=review
            ) as runner, mock.patch.object(
                app, "run_codex_regrade", return_value=sample_evaluation()
            ):
                result = app.run_codex_review(repo, "原始题面", [])

        self.assertEqual(result["next_action"], "complete")
        self.assertEqual(result["bugs"], [])
        self.assertEqual(len(result["quality_gaps"]), 1)
        prompt = runner.call_args.args[0]
        self.assertIn("未复现风险和覆盖不足只能写入 quality_gaps", prompt)
        self.assertIn("只有 bugs 非空时 next_action 才能是 bugfix", prompt)

    def test_review_worker_stores_findings_and_queues_second_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, phase, first_prompt, first_verification,
                          verification_commands, created_at, updated_at
                        ) VALUES (?, ?, ?, 'review_queued', ?, '[]', '[]', ?, ?)""",
                        ("review111111", "review-demo", str(repo), "原始需求", timestamp, timestamp),
                    )
                    database.execute(
                        """INSERT INTO run_stage_timings(run_id, stage, started_at)
                           VALUES (?, 'review', ?)""",
                        ("review111111", timestamp),
                    )
                    database.execute(
                        """INSERT INTO run_turns(
                          run_id, turn_number, intent_type, prompt, status,
                          verification, created_at, updated_at
                        ) VALUES (?, 1, '0-1 代码生成', '原始需求', 'reviewing', '[]', ?, ?)""",
                        ("review111111", timestamp, timestamp),
                    )
                review = {
                    "summary": "检查完成",
                    "next_action": "bugfix",
                    "bugs": [{"severity": "中", "title": "缺少边界校验", "evidence": "接口未校验", "fix": "补充校验"}],
                    "repair_prompt": "修复接口缺少边界校验的问题，并补充回归测试和 Docker 验收。",
                    "evaluation": sample_evaluation(),
                }
                with mock.patch.object(app, "run_codex_review", return_value=review), mock.patch.object(
                    app, "schedule_worker"
                ) as scheduler:
                    app.review_worker("review111111")

                stored = app.serialize_run(app.run_row("review111111"))
                self.assertEqual(stored["phase"], "second_queued")
                self.assertEqual(stored["review_model"], "gpt-5.6-sol")
                self.assertEqual(stored["review_result"]["bugs"][0]["title"], "缺少边界校验")
                self.assertEqual(stored["task_difficulty"], "困难")
                self.assertEqual(stored["second_prompt"], review["repair_prompt"])
                self.assertEqual(stored["turn_count"], 2)
                scheduler.assert_called_once_with(
                    "review111111", "second_queued", app.second_turn_worker
                )

    def test_bugfix_followup_uses_the_existing_container_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, workspace_path, phase, session_id,
                          first_prompt, first_prompt_id, second_prompt,
                          container_name, screen_name, verification_commands, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'second_queued', 'original-session',
                                  '原始需求', 'p1', '修复问题', 'container-1', 'screen-1', '[]', ?, ?)""",
                        ("session44444", "session-demo", str(repo), str(repo), timestamp, timestamp),
                    )
                    for number, intent, prompt, prompt_id, status in (
                        (1, "0-1 代码生成", "原始需求", "p1", "complete"),
                        (2, "Bug 修复", "修复问题", None, "queued"),
                    ):
                        database.execute(
                            """INSERT INTO run_turns(
                              run_id, turn_number, intent_type, prompt, prompt_id,
                              verification, status, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, '[]', ?, ?, ?)""",
                            ("session44444", number, intent, prompt, prompt_id, status, timestamp, timestamp),
                        )
                with mock.patch.object(app, "docker_container_running", return_value=True), mock.patch.object(
                    app, "refresh_trace_snapshot", side_effect=app.WorkflowError("轨迹尚未生成")
                ), mock.patch.object(app, "send_prompt_to_screen") as send_prompt, mock.patch.object(
                    app, "monitor_docker_turn"
                ) as monitor:
                    app.second_turn_worker("session44444")

                stored = app.serialize_run(app.run_row("session44444"))
                self.assertEqual(stored["phase"], "second_running")
                self.assertEqual(stored["session_id"], "original-session")
                self.assertEqual(stored["turns"][-1]["status"], "running")
                send_prompt.assert_called_once_with("session44444", "screen-1", "修复问题")
                monitor.assert_called_once_with("session44444", 2)

    def test_review_worker_stops_after_first_turn_when_no_confirmed_bug(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, phase, first_prompt, first_verification,
                          verification_commands, created_at, updated_at
                        ) VALUES (?, ?, ?, 'review_queued', ?, '[]', '[]', ?, ?)""",
                        ("review222222", "review-clean", str(repo), "原始需求", timestamp, timestamp),
                    )
                    database.execute(
                        """INSERT INTO run_turns(
                          run_id, turn_number, intent_type, prompt, status,
                          verification, created_at, updated_at
                        ) VALUES (?, 1, '0-1 代码生成', '原始需求', 'reviewing', '[]', ?, ?)""",
                        ("review222222", timestamp, timestamp),
                    )
                review = {
                    "summary": "没有发现可核验问题",
                    "next_action": "complete",
                    "bugs": [],
                    "repair_prompt": "",
                    "evaluation": sample_evaluation(),
                }
                with mock.patch.object(app, "run_codex_review", return_value=review), mock.patch.object(
                    app, "schedule_worker"
                ) as scheduler:
                    app.review_worker("review222222")

                stored = app.serialize_run(app.run_row("review222222"))
                self.assertEqual(stored["phase"], "complete")
                self.assertIsNone(stored["second_prompt"])
                self.assertEqual(stored["current_turn"], 1)
                scheduler.assert_not_called()

    def test_clean_container_run_only_closes_after_existing_turn_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, phase, first_prompt, first_verification,
                          container_name, screen_name, verification_commands, created_at, updated_at
                        ) VALUES (?, ?, ?, 'review_queued', ?, '[]', 'container-clean',
                                  'screen-clean', '[]', ?, ?)""",
                        ("archive11111", "archive-demo", str(repo), "原始需求", timestamp, timestamp),
                    )
                    database.execute(
                        """INSERT INTO run_turns(
                          run_id, turn_number, intent_type, prompt, status,
                          verification, created_at, updated_at
                        ) VALUES (?, 1, '0-1 代码生成', '原始需求', 'reviewing', '[]', ?, ?)""",
                        ("archive11111", timestamp, timestamp),
                    )
                review = {
                    "summary": "没有发现可核验问题",
                    "next_action": "complete",
                    "bugs": [],
                    "repair_prompt": "",
                    "evaluation": sample_evaluation(),
                }
                calls = []
                with mock.patch.object(app, "run_codex_review", return_value=review), mock.patch.object(
                    app, "checkpoint_completed_work", side_effect=lambda run_id: calls.append(("checkpoint", run_id)) or "a" * 40
                ) as checkpoint, mock.patch.object(
                    app, "export_and_remove_container", side_effect=lambda run_id, force=False: calls.append(("cleanup", run_id, force)) or root / "traces"
                ) as cleanup:
                    app.review_worker("archive11111")

                self.assertEqual(app.run_row("archive11111")["phase"], "complete")
                checkpoint.assert_not_called()
                cleanup.assert_called_once_with("archive11111", force=True)
                self.assertEqual(calls, [("cleanup", "archive11111", True)])

    def test_final_review_worker_stores_second_turn_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, workspace_path, phase, first_prompt,
                          second_prompt, second_prompt_id, second_verification,
                          verification_commands, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'final_review_queued', ?, ?, ?, '[]', '[]', ?, ?)""",
                        ("review333333", "review-final", str(repo), str(repo), "原始需求", "修复问题", "p2", timestamp, timestamp),
                    )
                    database.execute(
                        """INSERT INTO run_turns(
                          run_id, turn_number, intent_type, prompt, prompt_id, verification,
                          status, created_at, updated_at
                        ) VALUES (?, 2, 'Bug 修复', '修复问题', 'p2', '[]', 'reviewing', ?, ?)""",
                        ("review333333", timestamp, timestamp),
                    )
                final = {
                    "summary": "第二轮修复完成",
                    "next_action": "complete",
                    "remaining_bugs": [],
                    "repair_prompt": "",
                    "evaluation": sample_evaluation("Bug 修复"),
                }
                app.update_run(
                    "review333333",
                    container_name="container-final",
                    screen_name="screen-final",
                )
                calls = []
                with mock.patch.object(
                    app, "run_codex_final_review", return_value=final
                ), mock.patch.object(
                    app,
                    "checkpoint_completed_work",
                    side_effect=lambda run_id: calls.append(("commit", run_id)) or "b" * 40,
                ), mock.patch.object(
                    app,
                    "export_and_remove_container",
                    side_effect=lambda run_id, force=False: calls.append(("export-close", run_id, force)) or root / "traces",
                ):
                    app.final_review_worker("review333333")

                stored = app.serialize_run(app.run_row("review333333"))
                self.assertEqual(stored["phase"], "complete")
                self.assertEqual(stored["final_review_result"]["evaluation"]["task_type"], "Bug 修复")
                self.assertEqual(stored["task_difficulty"], "困难")
                self.assertEqual(
                    calls,
                    [
                        ("export-close", "review333333", True),
                    ],
                )
                self.assertIn("Git 和轨迹检查点已保存", stored["status_detail"])

    def test_final_review_manual_confirmation_snapshots_raw_trace_without_closing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, workspace_path, phase, session_id,
                          first_prompt, second_prompt, second_verification,
                          container_name, screen_name, verification_commands,
                          created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'final_review_queued', 'same-session',
                                  '原始需求', '第二轮修复', '[]', 'container-review',
                                  'screen-review', '[]', ?, ?)""",
                        (
                            "manual333333",
                            "manual-review-demo",
                            str(repo),
                            str(repo),
                            timestamp,
                            timestamp,
                        ),
                    )
                    database.execute(
                        """INSERT INTO run_turns(
                          run_id, turn_number, intent_type, prompt, prompt_id,
                          verification, status, created_at, updated_at
                        ) VALUES (?, 2, 'Bug 修复', '第二轮修复', 'p2',
                                  '[]', 'reviewing', ?, ?)""",
                        ("manual333333", timestamp, timestamp),
                    )
                repeated = app.WorkflowError(
                    "复查发现的问题与当前修复题面没有新的可观察差异，"
                    "已停止自动换词续轮，请人工确认"
                )
                with mock.patch.object(
                    app, "run_codex_final_review", side_effect=repeated
                ), mock.patch.object(
                    app, "export_container_trace_snapshot"
                ) as snapshot, mock.patch.object(
                    app, "export_and_remove_container"
                ) as close:
                    app.final_review_worker("manual333333")

                stored = app.serialize_run(app.run_row("manual333333"))

        self.assertEqual(stored["phase"], "manual_review")
        self.assertIn("原始轨迹已保存", stored["status_detail"])
        snapshot.assert_called_once_with("manual333333")
        close.assert_not_called()

    def test_final_review_worker_queues_next_bugfix_until_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, workspace_path, phase, session_id,
                          first_prompt, first_prompt_id, second_prompt, second_prompt_id,
                          second_verification, verification_commands, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'final_review_queued', 'same-session',
                                  '原始需求', 'p1', '第一次修复', 'p2', '[]', '[]', ?, ?)""",
                        ("loop33333333", "loop-demo", str(repo), str(repo), timestamp, timestamp),
                    )
                    for number, intent, prompt, prompt_id in (
                        (1, "0-1 代码生成", "原始需求", "p1"),
                        (2, "Bug 修复", "第一次修复", "p2"),
                    ):
                        database.execute(
                            """INSERT INTO run_turns(
                              run_id, turn_number, intent_type, prompt, prompt_id,
                              verification, status, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, '[]', 'reviewing', ?, ?)""",
                            ("loop33333333", number, intent, prompt, prompt_id, timestamp, timestamp),
                        )
                review = {
                    "summary": "仍有一个真实问题",
                    "next_action": "bugfix",
                    "remaining_bugs": [{
                        "severity": "中",
                        "title": "事务回滚不完整",
                        "evidence": "service.py 的 save() 在第二次写入失败后保留了首条记录",
                        "fix": "把两次写入放入同一个事务并增加失败回归测试",
                    }],
                    "repair_prompt": "修复 service.py 中两次写入未处于同一事务的问题，确保任一步失败都完整回滚，并补充失败路径回归测试后运行全部 Docker 验收命令。",
                    "evaluation": sample_evaluation("Bug 修复"),
                }
                with mock.patch.object(app, "run_codex_final_review", return_value=review), mock.patch.object(
                    app, "schedule_worker"
                ) as scheduler:
                    app.final_review_worker("loop33333333")

                stored = app.serialize_run(app.run_row("loop33333333"))
                self.assertEqual(stored["phase"], "second_queued")
                self.assertEqual(stored["session_id"], "same-session")
                self.assertEqual(stored["turn_count"], 3)
                self.assertEqual(stored["turns"][2]["intent_type"], "Bug 修复")
                self.assertEqual(stored["turns"][2]["prompt"], review["repair_prompt"])
                scheduler.assert_called_once_with("loop33333333", "second_queued", app.second_turn_worker)

    def test_final_review_worker_stops_at_tenth_turn_with_open_bug(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, workspace_path, phase, session_id,
                          first_prompt, first_prompt_id, second_prompt, second_prompt_id,
                          second_verification, verification_commands, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'final_review_queued', 'same-session',
                                  '原始需求', 'p1', '第十轮修复', 'p10', '[]', '[]', ?, ?)""",
                        ("limit3333333", "limit-demo", str(repo), str(repo), timestamp, timestamp),
                    )
                    for number in range(1, 11):
                        database.execute(
                            """INSERT INTO run_turns(
                              run_id, turn_number, intent_type, prompt, prompt_id,
                              verification, status, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, '[]', 'complete', ?, ?)""",
                            (
                                "limit3333333",
                                number,
                                "0-1 代码生成" if number == 1 else "Bug 修复",
                                "原始需求" if number == 1 else f"第 {number} 轮修复",
                                f"p{number}",
                                timestamp,
                                timestamp,
                            ),
                        )
                review = {
                    "summary": "第十轮仍有一个可核验问题",
                    "next_action": "bugfix",
                    "remaining_bugs": [{
                        "severity": "中",
                        "title": "回滚状态不完整",
                        "evidence": "service.py 失败分支未恢复 status 字段",
                        "fix": "在同一事务内恢复状态并补回归测试",
                    }],
                    "repair_prompt": "修复 service.py 失败分支中 status 字段未回滚的问题，将状态恢复放入同一事务，补充回归测试并运行全部 Docker 验收命令。",
                    "evaluation": sample_evaluation("Bug 修复"),
                }
                with mock.patch.object(app, "run_codex_final_review", return_value=review), mock.patch.object(
                    app, "schedule_worker"
                ) as scheduler:
                    app.final_review_worker("limit3333333")

                stored = app.serialize_run(app.run_row("limit3333333"))
                self.assertEqual(stored["phase"], "turn_limit")
                self.assertEqual(stored["turn_count"], 10)
                self.assertEqual(stored["turns"][-1]["review_result"]["next_action"], "bugfix")
                scheduler.assert_not_called()


class RepositoryTests(unittest.TestCase):
    def test_snapshot_clone_retries_transient_network_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            app.run_command(["git", "init"], cwd=source)
            app.run_command(["git", "config", "user.name", "Test User"], cwd=source)
            app.run_command(["git", "config", "user.email", "test@example.com"], cwd=source)
            (source / "README.md").write_text("source\n", encoding="utf-8")
            app.run_command(["git", "add", "README.md"], cwd=source)
            app.run_command(["git", "commit", "-m", "initial"], cwd=source)
            expected = app.run_command(
                ["git", "rev-parse", "HEAD"], cwd=source
            ).stdout.strip()
            original_run_command = app.run_command
            clone_calls = 0

            def flaky_run_command(args, **kwargs):
                nonlocal clone_calls
                if "clone" in args:
                    clone_calls += 1
                    if clone_calls == 1:
                        (destination / "partial").write_text(
                            "incomplete", encoding="utf-8"
                        )
                        raise app.WorkflowError(
                            "RPC failed; curl 16 Error in the HTTP2 framing layer"
                        )
                return original_run_command(args, **kwargs)

            with mock.patch.object(
                app, "run_command", side_effect=flaky_run_command
            ), mock.patch.object(app, "wait_for_generation_retry") as wait:
                checked_out = app.clone_repository_snapshot(
                    str(source), destination, expected
                )

        self.assertEqual(checked_out, expected)
        self.assertEqual(clone_calls, 2)
        wait.assert_called_once_with(3)

    def test_snapshot_clone_does_not_clean_preexisting_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "destination"
            destination.mkdir()
            marker = destination / "keep.txt"
            marker.write_text("preserve", encoding="utf-8")
            with mock.patch.object(
                app,
                "run_command",
                side_effect=app.WorkflowError("RPC failed; connection reset"),
            ) as runner, mock.patch.object(
                app, "wait_for_generation_retry"
            ) as wait, self.assertRaisesRegex(app.WorkflowError, "RPC failed"):
                app.clone_repository_snapshot(
                    "https://example.invalid/demo.git",
                    destination,
                    "f" * 40,
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")
            runner.assert_called_once()
            wait.assert_not_called()

    def test_snapshot_clone_checks_out_recorded_commit_after_main_advances(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            app.run_command(["git", "init"], cwd=source)
            app.run_command(["git", "config", "user.name", "Test User"], cwd=source)
            app.run_command(["git", "config", "user.email", "test@example.com"], cwd=source)
            (source / "version.txt").write_text("baseline\n", encoding="utf-8")
            app.run_command(["git", "add", "version.txt"], cwd=source)
            app.run_command(["git", "commit", "-m", "baseline"], cwd=source)
            baseline_sha = app.run_command(
                ["git", "rev-parse", "HEAD"], cwd=source
            ).stdout.strip()
            (source / "version.txt").write_text("new iteration\n", encoding="utf-8")
            app.run_command(["git", "commit", "-am", "advance main"], cwd=source)
            advanced_sha = app.run_command(
                ["git", "rev-parse", "HEAD"], cwd=source
            ).stdout.strip()

            checked_out = app.clone_repository_snapshot(
                str(source), destination, baseline_sha
            )

            self.assertEqual(checked_out, baseline_sha)
            self.assertEqual(
                app.run_command(["git", "rev-parse", "HEAD"], cwd=destination).stdout.strip(),
                baseline_sha,
            )
            self.assertEqual((destination / "version.txt").read_text(), "baseline\n")
            branch = app.run_command(
                ["git", "symbolic-ref", "-q", "HEAD"],
                cwd=destination,
                check=False,
            )
            self.assertNotEqual(branch.returncode, 0)

            existing = root / "existing-clone"
            app.run_command(["git", "clone", str(source), str(existing)], cwd=root)
            self.assertEqual(
                app.run_command(["git", "rev-parse", "HEAD"], cwd=existing).stdout.strip(),
                advanced_sha,
            )
            self.assertEqual(
                app.checkout_repository_snapshot(existing, baseline_sha), baseline_sha
            )
            self.assertEqual((existing / "version.txt").read_text(), "baseline\n")

    def test_snapshot_clone_reports_unreachable_recorded_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            app.run_command(["git", "init"], cwd=source)
            app.run_command(["git", "config", "user.name", "Test User"], cwd=source)
            app.run_command(["git", "config", "user.email", "test@example.com"], cwd=source)
            (source / "README.md").write_text("source\n", encoding="utf-8")
            app.run_command(["git", "add", "README.md"], cwd=source)
            app.run_command(["git", "commit", "-m", "initial"], cwd=source)

            with self.assertRaisesRegex(app.WorkflowError, "初始快照已不可达"):
                app.clone_repository_snapshot(str(source), destination, "f" * 40)

    def test_turn_checkpoint_commit_contains_session_and_turn_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            (repo / ".git").mkdir(parents=True)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, phase, session_id, first_prompt,
                          verification_commands, created_at, updated_at
                        ) VALUES (?, ?, ?, 'first_idle', ?, ?, '[]', ?, ?)""",
                        (
                            "commit111111",
                            "commit-demo",
                            str(repo),
                            "session-123",
                            "原始需求",
                            timestamp,
                            timestamp,
                        ),
                    )
                    database.execute(
                        """INSERT INTO run_turns(
                          run_id, turn_number, intent_type, prompt, prompt_id,
                          verification, status, created_at, updated_at
                        ) VALUES (?, 1, 'Feature 迭代', ?, ?, '[]', 'reviewing', ?, ?)""",
                        ("commit111111", "原始需求", "prompt-456", timestamp, timestamp),
                    )

                calls = []
                rev_parse_count = 0

                def command(args, **_kwargs):
                    nonlocal rev_parse_count
                    calls.append(args)
                    if args[:3] == ["git", "rev-parse", "HEAD"]:
                        rev_parse_count += 1
                        sha = "a" * 40 if rev_parse_count == 1 else "b" * 40
                        return subprocess.CompletedProcess(args, 0, sha + "\n", "")
                    if args[:4] == ["git", "log", "-1", "--format=%B"]:
                        return subprocess.CompletedProcess(args, 0, "baseline\n", "")
                    if args[:3] == ["git", "ls-remote", "origin"]:
                        return subprocess.CompletedProcess(
                            args, 0, "b" * 40 + "\trefs/heads/main\n", ""
                        )
                    return subprocess.CompletedProcess(args, 0, "", "")

                with mock.patch.object(app, "run_command", side_effect=command):
                    sha = app.checkpoint_completed_work("commit111111", 1)
                    repeated_sha = app.checkpoint_completed_work("commit111111", 1)

                turn = app.turn_row("commit111111", 1)

        self.assertEqual(sha, "b" * 40)
        self.assertEqual(repeated_sha, "b" * 40)
        self.assertEqual(turn["commit_sha"], "b" * 40)
        commits = [args for args in calls if args[:2] == ["git", "commit"]]
        self.assertEqual(len(commits), 1)
        commit = commits[0]
        self.assertIn("--allow-empty", commit)
        message = "\n".join(commit)
        self.assertIn("Session-ID: session-123", message)
        self.assertIn("Turn-Number: 1", message)
        self.assertIn("Turn-ID: prompt-456", message)
        push_index = next(i for i, args in enumerate(calls) if args[:2] == ["git", "push"])
        remote_index = next(i for i, args in enumerate(calls) if args[:2] == ["git", "ls-remote"])
        self.assertLess(push_index, remote_index)

    def test_turn_trajectory_checkpoint_stops_at_its_own_boundary_and_writes_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            run_directory = root / "run"
            source = root / "full.jsonl"
            events = [
                {"type": "user", "promptId": "p1", "message": {"content": "第一轮需求"}},
                {
                    "type": "assistant",
                    "message": {
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "第一轮完成"}],
                    },
                },
                {"type": "system", "subtype": "turn_duration"},
                {"type": "user", "promptId": "p2", "message": {"content": "第二轮修复"}},
                {
                    "type": "assistant",
                    "message": {
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "第二轮完成"}],
                    },
                },
                {"type": "system", "subtype": "turn_duration"},
            ]
            source.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
                encoding="utf-8",
            )
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, run_directory, phase, session_id,
                          first_prompt, verification_commands, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'first_idle', ?, ?, '[]', ?, ?)""",
                        (
                            "trace1111111",
                            "trace-demo",
                            str(repo),
                            str(run_directory),
                            "session-abc",
                            "第一轮需求",
                            timestamp,
                            timestamp,
                        ),
                    )
                    database.execute(
                        """INSERT INTO run_turns(
                          run_id, turn_number, intent_type, prompt, prompt_id, commit_sha,
                          verification, status, created_at, updated_at
                        ) VALUES (?, 1, '0-1 代码生成', ?, 'p1', ?, '[]', 'reviewing', ?, ?)""",
                        ("trace1111111", "第一轮需求", "c" * 40, timestamp, timestamp),
                    )

                with mock.patch.object(
                    app, "export_container_trace_snapshot", return_value=source
                ) as export_raw:
                    destination = app.export_turn_checkpoint("trace1111111", 1)
                content = destination.read_text(encoding="utf-8")
                manifest = json.loads((destination.parent / "manifest.json").read_text())
                turn = app.turn_row("trace1111111", 1)

        self.assertIn("第一轮完成", content)
        self.assertNotIn("第二轮修复", content)
        self.assertEqual(manifest["session_id"], "session-abc")
        self.assertEqual(manifest["turns"][0]["commit_sha"], "c" * 40)
        self.assertEqual(manifest["turns"][0]["turn_id"], "p1")
        self.assertEqual(len(turn["trajectory_sha256"]), 64)
        export_raw.assert_called_once_with("trace1111111")

    def test_trace_checkpoint_accepts_legacy_multiline_terminal_paste(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "full.jsonl"
            destination = root / "turn-02.jsonl"
            events = [
                {
                    "type": "user",
                    "sessionId": "session-split",
                    "timestamp": "2026-09-10T09:14:52.500Z",
                    "promptId": "prompt-split",
                    "message": {"content": "修复第一个问题"},
                },
                {
                    "type": "queue-operation",
                    "operation": "enqueue",
                    "sessionId": "session-split",
                    "timestamp": "2026-09-10T09:14:53.000Z",
                    "content": "修复第二个问题",
                },
                {
                    "type": "assistant",
                    "message": {
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "两个问题都已修复。"}],
                    },
                },
                {"type": "system", "subtype": "turn_duration"},
            ]
            source.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
                encoding="utf-8",
            )

            app.write_trace_through_turn(
                source, destination, "修复第一个问题\n修复第二个问题"
            )
            content = destination.read_text(encoding="utf-8")

        self.assertIn("两个问题都已修复", content)

    def test_trace_checkpoint_keeps_final_reply_after_automatic_api_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "full.jsonl"
            destination = root / "turn-01.jsonl"
            events = [
                {
                    "type": "user",
                    "promptId": "prompt-original",
                    "message": {"content": "完成这个项目"},
                },
                {
                    "type": "assistant",
                    "isApiErrorMessage": True,
                    "apiErrorStatus": 504,
                    "message": {
                        "stop_reason": "stop_sequence",
                        "content": [{"type": "text", "text": "API Error: 504 Gateway Time-out"}],
                    },
                },
                {
                    "type": "user",
                    "promptId": "prompt-resume",
                    "message": {"content": "继续"},
                },
                {
                    "type": "assistant",
                    "message": {
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "恢复后完成全部工作。"}],
                    },
                },
                {"type": "system", "subtype": "turn_duration"},
                {
                    "type": "user",
                    "promptId": "prompt-next",
                    "message": {"content": "修复下一问题"},
                },
            ]
            source.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
                encoding="utf-8",
            )

            app.write_trace_through_turn(source, destination, "完成这个项目")
            content = destination.read_text(encoding="utf-8")

        self.assertIn('"content": "继续"', content)
        self.assertIn("恢复后完成全部工作", content)
        self.assertNotIn("修复下一问题", content)

    def test_trace_checkpoint_keeps_reply_after_incomplete_turn_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "full.jsonl"
            destination = root / "turn-01.jsonl"
            events = [
                {
                    "type": "user",
                    "promptId": "prompt-original",
                    "message": {"content": "完成这个项目"},
                },
                {
                    "type": "assistant",
                    "message": {
                        "stop_reason": None,
                        "content": [{"type": "text", "text": "准备补装依赖："}],
                    },
                },
                {"type": "system", "subtype": "turn_duration"},
                {
                    "type": "user",
                    "promptId": "prompt-resume",
                    "message": {"content": "继续"},
                },
                {
                    "type": "assistant",
                    "message": {
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "恢复后完成全部工作。"}],
                    },
                },
                {"type": "system", "subtype": "turn_duration"},
                {
                    "type": "user",
                    "promptId": "prompt-next",
                    "message": {"content": "修复下一问题"},
                },
            ]
            source.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
                encoding="utf-8",
            )

            app.write_trace_through_turn(source, destination, "完成这个项目")
            content = destination.read_text(encoding="utf-8")

        self.assertIn('"content": "继续"', content)
        self.assertIn("恢复后完成全部工作", content)
        self.assertNotIn("修复下一问题", content)

    def test_trace_checkpoint_keeps_final_reply_after_cli_interruption_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "full.jsonl"
            destination = root / "turn-01.jsonl"
            events = [
                {
                    "type": "user",
                    "promptId": "prompt-original",
                    "message": {"content": "完成这个项目"},
                },
                {
                    "type": "user",
                    "interruptedMessageId": "assistant-tool-call",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "[Request interrupted by user for tool use]",
                            }
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "恢复后完成全部工作。"}],
                    },
                },
                {"type": "system", "subtype": "turn_duration"},
            ]
            source.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
                encoding="utf-8",
            )

            app.write_trace_through_turn(source, destination, "完成这个项目")
            content = destination.read_text(encoding="utf-8")

        self.assertIn("恢复后完成全部工作", content)

    def test_trace_is_exported_before_container_conversation_is_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = {
                "id": "archive-order",
                "container_cleaned": 0,
                "trajectory_path": "",
                "session_id": "session-order",
                "container_name": "container-order",
                "screen_name": "screen-order",
            }
            calls = []

            def copy_traces(_row, destination):
                calls.append("export")
                transcript = destination / "project" / "session-order.jsonl"
                transcript.parent.mkdir(parents=True)
                transcript.write_text("{}\n", encoding="utf-8")
                return destination

            def command(args, **_kwargs):
                if args[:2] == ["docker", "rm"]:
                    calls.append("remove")
                return subprocess.CompletedProcess(args, 0, "", "")

            with mock.patch.object(app, "run_row", return_value=row), mock.patch.object(
                app, "run_directory_for", return_value=root
            ), mock.patch.object(
                app, "copy_container_traces", side_effect=copy_traces
            ), mock.patch.object(
                app,
                "close_container_conversation",
                side_effect=lambda _row, force=False: calls.append("close"),
            ), mock.patch.object(
                app, "screen_session_running", return_value=False
            ), mock.patch.object(
                app,
                "close_terminal_screen",
                side_effect=lambda _run_id: calls.append("terminal") or True,
            ), mock.patch.object(
                app, "run_command", side_effect=command
            ), mock.patch.object(app, "update_run"), mock.patch.object(app, "add_event"):
                app.export_and_remove_container("archive-order", force=True)

        self.assertEqual(calls, ["export", "close", "remove", "terminal"])

    def test_raw_trace_snapshot_keeps_container_and_uses_session_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = {
                "id": "snapshot-only",
                "session_id": "session-snapshot",
                "container_name": "container-snapshot",
            }

            def copy_traces(_row, destination):
                transcript = destination / "-workspace" / "session-snapshot.jsonl"
                transcript.parent.mkdir(parents=True)
                transcript.write_text('{}\n', encoding="utf-8")
                return destination

            with mock.patch.object(app, "run_row", return_value=row), mock.patch.object(
                app, "run_directory_for", return_value=root
            ), mock.patch.object(
                app, "copy_container_traces", side_effect=copy_traces
            ), mock.patch.object(app, "update_run") as update, mock.patch.object(
                app, "add_event"
            ), mock.patch.object(app, "close_container_conversation") as close, mock.patch.object(
                app, "run_command"
            ) as command:
                path = app.export_container_trace_snapshot("snapshot-only")

        self.assertEqual(path.name, "session-snapshot.jsonl")
        self.assertEqual(path.parent.name, "-workspace")
        update.assert_called_once_with("snapshot-only", trajectory_path=str(path))
        close.assert_not_called()
        command.assert_not_called()

    def test_initial_repository_contains_empty_readme(self):
        with tempfile.TemporaryDirectory() as directory:
            repo_path = Path(directory) / "new-project"

            def fake_command(args, cwd=None, timeout=120, check=True):
                if args[:3] == ["gh", "repo", "view"]:
                    return subprocess.CompletedProcess(args, 1, "", "not found")
                if args[:3] == ["git", "rev-parse", "HEAD"]:
                    return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
                return subprocess.CompletedProcess(args, 0, "", "")

            with mock.patch.object(app, "run_command", side_effect=fake_command), mock.patch.object(
                app, "add_event"
            ):
                repo_url, sha, snapshot = app.create_github_repo("run-id", "new-project", repo_path)

            self.assertTrue((repo_path / "README.md").exists())
            self.assertEqual((repo_path / "README.md").read_bytes(), b"")
            self.assertEqual(repo_url, "https://github.com/makabaka-boop/new-project")
            self.assertEqual(sha, "a" * 40)
            self.assertTrue(snapshot.endswith("/commit/" + "a" * 40))

    def test_terminal_launcher_uses_isolated_container_without_storing_a_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            assets = root / "terminal-assets"
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "TERMINAL_ASSETS_DIR", assets
            ), mock.patch.object(app, "HISTORY_PATH", root / "history.md"), mock.patch.object(
                app, "schedule_worker"
            ):
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "docker-terminal-demo",
                    "project_directory": "zzzz",
                    "task_type": "0-1 代码生成",
                    "first_prompt": "完成一个容器化项目",
                    "_defer_start": True,
                })
                paths = app.write_terminal_launcher(app.run_row(created["id"]))

            launcher = paths["launcher"].read_text(encoding="utf-8")
            self.assertIn("adminfather/benzhi-claude-code:20260909-isolated-git", launcher)
            self.assertIn("dst=/workspace", launcher)
            self.assertIn("--cap-drop ALL", launcher)
            self.assertIn('--label "claude-eval.run-id=$run_id"', launcher)
            self.assertIn("CLAUDE_EVAL_DOCKER_API_KEY", launcher)
            self.assertIn("ANTHROPIC_AUTH_TOKEN", launcher)
            self.assertIn("settings.json", launcher)
            self.assertIn('ANTHROPIC_MODEL=$model', launcher)
            self.assertIn("输入不显示", launcher)
            self.assertNotIn("xxxxx", launcher)
            self.assertTrue(Path(created["repo_path"]).is_dir())
            self.assertEqual(list(Path(created["repo_path"]).iterdir()), [])

    def test_screen_launch_does_not_capture_long_lived_container_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "TERMINAL_ASSETS_DIR", root / "terminal-assets"
            ), mock.patch.object(app, "HISTORY_PATH", root / "history.md"), mock.patch.object(
                app, "schedule_worker"
            ):
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "screen-output-demo",
                    "project_directory": "zzzz",
                    "task_type": "0-1 代码生成",
                    "first_prompt": "完成一个容器化项目",
                    "_defer_start": True,
                })
                completed = subprocess.CompletedProcess([], 1, "", "")
                with mock.patch.object(app, "ensure_docker_engine_ready"), mock.patch.object(
                    app, "docker_container_exists", return_value=False
                ), mock.patch.object(
                    app, "screen_session_running", return_value=False
                ), mock.patch.object(app, "run_command", return_value=completed) as command, mock.patch.object(
                    app, "open_terminal_screen"
                ):
                    app.launch_docker_terminal(app.run_row(created["id"]))

            screen_call = next(
                call for call in command.call_args_list
                if call.args[0] and call.args[0][0] == "screen"
            )
            self.assertIn("-dmS", screen_call.args[0])
            self.assertNotIn("-DmS", screen_call.args[0])
            self.assertIs(screen_call.kwargs["capture_output"], False)

    def test_docker_startup_probe_uses_server_version_and_caches_result(self):
        completed = subprocess.CompletedProcess([], 0, "28.4.0\n", "")
        with mock.patch.object(app, "DOCKER_STARTUP_HEALTH_AT", 0.0), mock.patch.object(
            app,
            "DOCKER_STARTUP_HEALTH_RESULT",
            (False, "尚未检查 Docker 服务"),
        ), mock.patch.object(app, "run_command", return_value=completed) as command:
            app.ensure_docker_engine_ready()
            app.ensure_docker_engine_ready()

        command.assert_called_once_with(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            timeout=app.DOCKER_STARTUP_HEALTH_TIMEOUT_SECONDS,
            check=False,
        )

    def test_container_existence_check_targets_one_container_namespace(self):
        missing = subprocess.CompletedProcess(
            [], 1, "", "Error: No such container: claude-eval-one"
        )
        with mock.patch.object(app, "run_command", return_value=missing) as command:
            self.assertFalse(app.docker_container_exists("claude-eval-one"))

        command.assert_called_once_with(
            [
                "docker", "container", "inspect", "--format", "{{.Id}}",
                "claude-eval-one",
            ],
            timeout=app.DOCKER_STARTUP_HEALTH_TIMEOUT_SECONDS,
            check=False,
        )

    def test_container_cleanup_ownership_rejects_a_foreign_run_label(self):
        inspected = {
            "Config": {
                "Image": app.DOCKER_IMAGE,
                "Labels": {"claude-eval.run-id": "other-run"},
            },
            "Mounts": [],
        }
        completed = subprocess.CompletedProcess(
            [], 0, json.dumps(inspected), ""
        )
        row = {"id": "expected-run", "repo_path": "/tmp/expected-workspace"}
        with mock.patch.object(app, "run_command", return_value=completed):
            present, owned, detail = app.docker_container_owned_by_run(
                "claude-eval-expected-run", row
            )

        self.assertTrue(present)
        self.assertFalse(owned)
        self.assertIn("不匹配", detail)

    def test_container_running_does_not_treat_daemon_error_as_absent(self):
        failed = subprocess.CompletedProcess(
            [], 1, "", "permission denied while connecting to Docker daemon"
        )
        with mock.patch.object(app, "run_command", return_value=failed):
            with self.assertRaisesRegex(app.SystemicStartupError, "状态查询"):
                app.docker_container_running("claude-eval-one")

    def test_prompt_submission_marker_is_written_only_after_enter_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            completed = subprocess.CompletedProcess([], 0, "", "")
            with mock.patch.object(app, "TERMINAL_ASSETS_DIR", root), mock.patch.object(
                app, "screen_session_running", return_value=True
            ), mock.patch.object(
                app, "run_command", return_value=completed
            ) as command, mock.patch.object(app.time, "sleep"):
                app.send_prompt_to_screen("prompt-marker-demo", "screen-demo", "完成任务")
                paths = app.terminal_asset_paths("prompt-marker-demo")

            self.assertEqual(paths["prompt"].read_text(encoding="utf-8"), "完成任务")
            self.assertTrue(paths["prompt_submitted"].is_file())
            self.assertEqual(
                command.call_args.args[0],
                ["screen", "-S", "screen-demo", "-p", "0", "-X", "stuff", "\r"],
            )

    def test_terminal_ui_timeout_does_not_abort_the_detached_container(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "TERMINAL_ASSETS_DIR", root / "terminal-assets"
            ), mock.patch.object(app, "HISTORY_PATH", root / "history.md"), mock.patch.object(
                app, "schedule_worker"
            ):
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "terminal-timeout-demo",
                    "project_directory": "zzzz",
                    "task_type": "0-1 代码生成",
                    "first_prompt": "完成一个容器化项目",
                    "_defer_start": True,
                })
                success = subprocess.CompletedProcess([], 0, "", "")
                with mock.patch.object(app, "ensure_docker_engine_ready"), mock.patch.object(
                    app, "failed_startup_resource_count", return_value=0
                ), mock.patch.object(
                    app, "docker_container_exists", return_value=False
                ), mock.patch.object(
                    app, "screen_session_running", return_value=False
                ), mock.patch.object(
                    app, "run_command", return_value=success
                ), mock.patch.object(
                    app,
                    "open_terminal_screen",
                    side_effect=app.WorkflowError("命令执行超时：osascript -e"),
                ), mock.patch.object(app, "add_event") as event:
                    screen_name = app.launch_docker_terminal(app.run_row(created["id"]))
                    owner_created = app.terminal_asset_paths(created["id"])[
                        "startup_owner"
                    ].is_file()

            self.assertEqual(screen_name, created["screen_name"])
            self.assertTrue(owner_created)
            self.assertTrue(
                any("继续启动" in str(call.args[1]) for call in event.call_args_list)
            )

    def test_open_terminal_records_the_run_specific_tty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            completed = subprocess.CompletedProcess([], 0, "/dev/ttys123\n", "")
            with mock.patch.object(app, "TERMINAL_ASSETS_DIR", root), mock.patch.object(
                app, "run_command", return_value=completed
            ) as command:
                app.open_terminal_screen("run-terminal-demo", "screen-terminal-demo")
                paths = app.terminal_asset_paths("run-terminal-demo")

            self.assertEqual(paths["terminal_tty"].read_text(encoding="utf-8"), "/dev/ttys123\n")
            apple_script = command.call_args.args[0][2]
            self.assertIn("screen-terminal-demo", apple_script)
            self.assertIn("Claude Eval · run-terminal-demo", apple_script)
            self.assertIn("return tty of taskTab", apple_script)

    def test_close_terminal_targets_only_the_recorded_idle_tab(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "TERMINAL_ASSETS_DIR", root), mock.patch.object(
                app, "AUTO_CLOSE_TERMINAL", True
            ), mock.patch.object(app.sys, "platform", "darwin"):
                paths = app.terminal_asset_paths("run-close-demo")
                paths["root"].mkdir(parents=True)
                paths["terminal_tty"].write_text("/dev/ttys456\n", encoding="utf-8")
                completed = subprocess.CompletedProcess([], 0, "closed\n", "")
                with mock.patch.object(app, "run_command", return_value=completed) as command:
                    closed = app.close_terminal_screen("run-close-demo")

            self.assertTrue(closed)
            self.assertFalse(paths["terminal_tty"].exists())
            arguments = command.call_args.args[0]
            self.assertEqual(arguments[-2:], ["/dev/ttys456", "Claude Eval · run-close-demo"])
            self.assertIn("busy of terminalTab", arguments[2])
            self.assertIn("close terminalTab", arguments[2])

    def test_close_terminal_leaves_a_busy_tab_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "TERMINAL_ASSETS_DIR", root), mock.patch.object(
                app, "AUTO_CLOSE_TERMINAL", True
            ), mock.patch.object(app.sys, "platform", "darwin"), mock.patch.object(
                app.time, "sleep"
            ), mock.patch.object(app.time, "time", side_effect=[0, 0, 6]):
                paths = app.terminal_asset_paths("run-busy-demo")
                paths["root"].mkdir(parents=True)
                paths["terminal_tty"].write_text("/dev/ttys789\n", encoding="utf-8")
                completed = subprocess.CompletedProcess([], 0, "busy\n", "")
                with mock.patch.object(app, "run_command", return_value=completed):
                    closed = app.close_terminal_screen("run-busy-demo")

            self.assertFalse(closed)
            self.assertTrue(paths["terminal_tty"].exists())

    def test_terminal_attention_detection_only_reads_visible_prompt(self):
        self.assertEqual(
            app.terminal_attention_reason_from_text(
                "\x1b[31mDo you want to proceed?\x1b[0m  1. Yes  2. No"
            ),
            "终端正在等待操作确认",
        )
        self.assertEqual(
            app.terminal_attention_reason_from_text("正在继续生成页面和检查内容"),
            "",
        )

    def test_terminal_screen_capture_uses_hardcopy_without_sending_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()

            def hardcopy(args, **_kwargs):
                Path(args[-1]).write_text(
                    "Do you want to proceed?\n1. Yes\n2. No\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(args, 0, "", "")

            with mock.patch.object(app, "TERMINAL_ASSETS_DIR", root), mock.patch.object(
                app, "screen_session_running", return_value=True
            ), mock.patch.object(app, "run_command", side_effect=hardcopy) as command:
                output = app.terminal_screen_text("attention-demo", "screen-demo")

        self.assertIn("Do you want to proceed?", output)
        self.assertEqual(
            command.call_args.args[0][:7],
            ["screen", "-S", "screen-demo", "-p", "0", "-X", "hardcopy"],
        )
        self.assertNotIn("stuff", command.call_args.args[0])

    def test_container_permission_prompt_is_confirmed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "TERMINAL_ASSETS_DIR", root):
                paths = app.terminal_asset_paths("permission-demo")
                paths["root"].mkdir(parents=True)
                paths["screen_log"].write_text(
                    "WARNING: Claude Code running in Bypass Permissions mode\n2. Yes, I accept\n",
                    encoding="utf-8",
                )
                with mock.patch.object(app, "docker_container_running", return_value=True), mock.patch.object(
                    app, "run_command", return_value=subprocess.CompletedProcess([], 0, "", "")
                ) as command, mock.patch.object(
                    app, "terminal_screen_text", return_value=""
                ), mock.patch.object(app, "add_event"), mock.patch.object(app.time, "sleep"):
                    app.accept_container_permission_prompt("permission-demo", "screen-demo", "container-demo")
                    app.accept_container_permission_prompt("permission-demo", "screen-demo", "container-demo")

            command.assert_called_once_with(
                ["screen", "-S", "screen-demo", "-p", "0", "-X", "stuff", "2\r"],
                timeout=20,
            )
            self.assertEqual(paths["permission_status"].read_text(encoding="utf-8"), "accepted\n")

    def test_container_permission_prompt_moves_from_no_to_yes_in_new_version(self):
        prompt = """WARNING: Claude Code running in Bypass Permissions mode

❯ No, exit
  Yes, I accept

Enter to confirm · Esc to cancel
"""
        self.assertEqual(app.container_permission_accept_input(prompt), "\x1b[B\r")

    def test_container_permission_prompt_handles_cursor_positioning_log(self):
        prompt = (
            "WARNING:\x1b[12GClaude\x1b[19GCode\x1b[24Grunning\x1b[32Gin"
            "\x1b[35GBypass\x1b[42GPermissions\x1b[54Gmode\n"
            "\x1b[38;5;153m❯\x1b[5GNo,\x1b[9Gexit\n"
            "\x1b[5GYes,\x1b[10GI\x1b[12Gaccept\n"
        )
        self.assertEqual(app.container_permission_accept_input(prompt), "\x1b[B\r")

    def test_container_permission_prompt_handles_screen_hardcopy_marker(self):
        prompt = """WARNING: Claude Code running in Bypass Permissions mode

  o No, exit
    Yes, I accept

  Enter to confirm  Esc to cancel
"""
        self.assertEqual(app.container_permission_accept_input(prompt), "\x1b[B\r")

    def test_container_permission_prompt_confirms_yes_when_already_selected(self):
        prompt = """WARNING: Claude Code running in Bypass Permissions mode

  No, exit
❯ Yes, I accept
"""
        self.assertEqual(app.container_permission_accept_input(prompt), "\r")

    def test_permission_confirmation_prefers_current_screen_over_historical_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "TERMINAL_ASSETS_DIR", root):
                paths = app.terminal_asset_paths("permission-current-demo")
                paths["root"].mkdir(parents=True)
                paths["screen_log"].write_text(
                    "WARNING: Claude Code running in Bypass Permissions mode\n"
                    "❯ No, exit\n  Yes, I accept\n",
                    encoding="utf-8",
                )
                current = "Claude Code v2.1.269\n❯ Ready for a prompt\n"
                with mock.patch.object(
                    app, "docker_container_running", return_value=True
                ), mock.patch.object(
                    app, "terminal_screen_text", return_value=current
                ), mock.patch.object(
                    app.time, "time", side_effect=[0, 0, 21]
                ), mock.patch.object(app.time, "sleep"), mock.patch.object(
                    app, "run_command"
                ) as command:
                    with self.assertRaisesRegex(app.WorkflowError, "无法识别"):
                        app.accept_container_permission_prompt(
                            "permission-current-demo", "screen-demo", "container-demo"
                        )

            command.assert_not_called()

    def test_first_turn_worker_opens_terminal_before_preparing_the_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker"):
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "terminal-first-demo",
                    "project_directory": "zzzz",
                    "task_type": "0-1 代码生成",
                    "first_prompt": "完成一个容器化项目",
                    "_defer_start": True,
                })
                with mock.patch.object(app, "ensure_docker_engine_ready"), mock.patch.object(
                    app, "launch_docker_terminal", return_value="screen-new"
                ) as launch, mock.patch.object(
                    app, "continue_first_turn_after_terminal"
                ) as continue_after_terminal:
                    app.first_turn_worker(created["id"])

                launch.assert_called_once()
                continue_after_terminal.assert_called_once_with(created["id"])
                stored = app.run_row(created["id"])
                self.assertEqual(stored["phase"], "first_starting")
                self.assertEqual(stored["first_agent_id"], "screen-new")

    def test_first_turn_systemic_startup_failure_rolls_back_and_pauses_refill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "TERMINAL_ASSETS_DIR", root / "terminal-assets"
            ), mock.patch.object(app, "HISTORY_PATH", root / "history.md"), mock.patch.object(
                app, "schedule_worker"
            ):
                app.initialize_database()
                app.set_auto_refill({"enabled": True, "project_directory": "zzzz"})
                created = app.create_run({
                    "repo_name": "systemic-startup-demo",
                    "project_directory": "zzzz",
                    "task_type": "0-1 代码生成",
                    "first_prompt": "完成一个容器化项目",
                    "_auto_refill": True,
                    "_defer_start": True,
                })
                with mock.patch.object(
                    app,
                    "ensure_docker_engine_ready",
                    side_effect=app.SystemicStartupError("Docker 服务不可用"),
                ), mock.patch.object(app, "log_workflow_exception"):
                    app.first_turn_worker(created["id"])

                stored = app.run_row(created["id"])
                turn = app.turn_row(created["id"], 1)
                refill = app.auto_refill_configuration()

            self.assertEqual(stored["phase"], "failed")
            self.assertEqual(stored["container_cleaned"], 1)
            self.assertEqual(turn["status"], "failed")
            self.assertFalse(refill["enabled"])
            self.assertIn("容器启动失败", refill["error"])

    def test_successful_prompt_start_releases_startup_owner_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / "workspace"
            (repo / ".git").mkdir(parents=True)
            row = {
                "id": "abc123abc123",
                "repo_path": str(repo),
                "container_name": "claude-eval-abc123abc123",
                "screen_name": "claude-eval-abc123abc123",
                "source_run_id": None,
                "repo_url": "https://example.test/repo",
                "base_sha": "",
                "first_prompt": "完成一个容器化项目",
                "phase": "first_starting",
                "model": "auto_model/urm",
                "auto_refill": 1,
            }
            with mock.patch.object(app, "TERMINAL_ASSETS_DIR", root / "terminal"):
                paths = app.terminal_asset_paths(row["id"])
                paths["root"].mkdir(parents=True)
                paths["startup_owner"].write_text("{}", encoding="utf-8")
                with mock.patch.object(app, "run_row", return_value=row), mock.patch.object(
                    app, "wait_for_docker_container"
                ), mock.patch.object(
                    app, "accept_container_permission_prompt"
                ), mock.patch.object(
                    app, "refresh_trace_snapshot", return_value=(root, None)
                ), mock.patch.object(app, "send_prompt_to_screen"), mock.patch.object(
                    app, "update_run"
                ), mock.patch.object(app, "update_turn"), mock.patch.object(
                    app, "add_event"
                ), mock.patch.object(app, "monitor_docker_turn"), mock.patch.object(
                    app, "record_auto_refill_success"
                ) as refill_success:
                    app.continue_first_turn_after_terminal(row["id"])

            self.assertFalse(paths["startup_owner"].exists())
            refill_success.assert_called_once_with()

    def test_unstarted_resource_rollback_targets_only_the_run_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "TERMINAL_ASSETS_DIR", root / "terminal-assets"
            ), mock.patch.object(app, "HISTORY_PATH", root / "history.md"), mock.patch.object(
                app, "schedule_worker"
            ):
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "rollback-startup-demo",
                    "project_directory": "zzzz",
                    "task_type": "0-1 代码生成",
                    "first_prompt": "完成一个容器化项目",
                    "_defer_start": True,
                })
                run_id = created["id"]
                app.update_run(run_id, phase="failed")
                app.add_event(
                    run_id,
                    f"正在为本题启动独立容器 claude-eval-{run_id}",
                )
                paths = app.terminal_asset_paths(run_id)
                paths["root"].mkdir(parents=True)
                paths["startup_owner"].write_text("{}", encoding="utf-8")

                def cleanup_command(args, **_kwargs):
                    if args[:3] == ["docker", "container", "rm"]:
                        return subprocess.CompletedProcess(
                            args, 1, "", f"Error: No such container: claude-eval-{run_id}"
                        )
                    return subprocess.CompletedProcess(args, 1, "", "No screen session")

                with mock.patch.object(
                    app, "run_command", side_effect=cleanup_command
                ) as command, mock.patch.object(
                    app,
                    "docker_container_owned_by_run",
                    return_value=(True, True, "run-id 标签匹配"),
                ), mock.patch.object(
                    app, "docker_container_exists", return_value=False
                ), mock.patch.object(
                    app, "close_terminal_screen", return_value=True
                ):
                    cleanup = app.rollback_unstarted_run_resources(run_id)

                stored = app.run_row(run_id)

            self.assertTrue(cleanup["cleaned"])
            self.assertEqual(stored["container_cleaned"], 1)
            issued = [call.args[0] for call in command.call_args_list]
            self.assertIn(
                ["screen", "-S", f"claude-eval-{run_id}", "-X", "quit"],
                issued,
            )
            self.assertIn(
                [
                    "docker", "container", "rm", "--force",
                    f"claude-eval-{run_id}",
                ],
                issued,
            )
            self.assertFalse(any(args[:2] == ["docker", "rm"] for args in issued))

    def test_rollback_keeps_owner_until_screen_and_container_are_both_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "TERMINAL_ASSETS_DIR", root / "terminal-assets"
            ), mock.patch.object(app, "HISTORY_PATH", root / "history.md"), mock.patch.object(
                app, "schedule_worker"
            ):
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "rollback-verification-demo",
                    "project_directory": "zzzz",
                    "task_type": "0-1 代码生成",
                    "first_prompt": "完成一个容器化项目",
                    "_defer_start": True,
                })
                run_id = created["id"]
                app.update_run(run_id, phase="failed", container_cleaned=0)
                app.add_event(
                    run_id,
                    f"正在为本题启动独立容器 claude-eval-{run_id}",
                )
                paths = app.terminal_asset_paths(run_id)
                paths["root"].mkdir(parents=True)
                paths["startup_owner"].write_text(
                    json.dumps({"run_id": run_id, "startup_protocol": 2}),
                    encoding="utf-8",
                )

                def screen_still_present(args, **_kwargs):
                    if args == ["screen", "-ls"]:
                        return subprocess.CompletedProcess(
                            args, 0, f"123.{created['screen_name']}\t(Detached)\n", ""
                        )
                    return subprocess.CompletedProcess(args, 0, "", "")

                with mock.patch.object(
                    app, "docker_container_owned_by_run",
                    return_value=(False, True, "容器不存在"),
                ), mock.patch.object(
                    app, "docker_container_exists", return_value=False
                ), mock.patch.object(
                    app, "run_command", side_effect=screen_still_present
                ), mock.patch.object(app, "close_terminal_screen", return_value=True):
                    cleanup = app.rollback_unstarted_run_resources(run_id)

                stored = app.run_row(run_id)

            self.assertFalse(cleanup["cleaned"])
            self.assertEqual(stored["container_cleaned"], 0)
            self.assertTrue(paths["startup_owner"].is_file())

    def test_failed_capacity_ignores_legacy_database_flags_without_owner_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "TERMINAL_ASSETS_DIR", root / "terminal-assets"
            ), mock.patch.object(app, "HISTORY_PATH", root / "history.md"), mock.patch.object(
                app, "schedule_worker"
            ):
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "legacy-cleanup-flag-demo",
                    "project_directory": "zzzz",
                    "task_type": "0-1 代码生成",
                    "first_prompt": "完成一个容器化项目",
                    "_defer_start": True,
                })
                run_id = created["id"]
                app.update_run(run_id, phase="failed", container_cleaned=0)
                app.add_event(
                    run_id,
                    f"正在为本题启动独立容器 claude-eval-{run_id}",
                )
                self.assertEqual(app.failed_startup_resource_count(), 0)

                paths = app.terminal_asset_paths(run_id)
                paths["root"].mkdir(parents=True)
                paths["startup_owner"].write_text(
                    json.dumps({"run_id": run_id, "startup_protocol": 2}),
                    encoding="utf-8",
                )
                self.assertEqual(app.failed_startup_resource_count(), 1)

                paths["prompt"].write_text("等待发送", encoding="utf-8")
                self.assertEqual(app.failed_startup_resource_count(), 1)
                self.assertTrue(
                    app.failed_startup_retry_candidate(app.run_row(run_id))
                )

                paths["prompt_submitted"].write_text("已按下 Enter", encoding="utf-8")
                self.assertEqual(app.failed_startup_resource_count(), 0)
                self.assertFalse(
                    app.failed_startup_retry_candidate(app.run_row(run_id))
                )

    def test_retry_failed_startup_reuses_empty_run_after_exact_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "TERMINAL_ASSETS_DIR", root / "terminal-assets"
            ), mock.patch.object(app, "HISTORY_PATH", root / "history.md"), mock.patch.object(
                app, "schedule_worker"
            ) as schedule:
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "retry-startup-demo",
                    "project_directory": "zzzz",
                    "task_type": "0-1 代码生成",
                    "first_prompt": "完成一个容器化项目",
                    "_defer_start": True,
                })
                run_id = created["id"]
                app.update_run(run_id, phase="failed", error="Docker 启动失败")
                app.update_turn(run_id, 1, status="failed")
                app.add_event(
                    run_id,
                    f"正在为本题启动独立容器 claude-eval-{run_id}",
                )

                def cleanup_command(args, **_kwargs):
                    if args[:3] == ["docker", "container", "rm"]:
                        return subprocess.CompletedProcess(
                            args, 1, "", f"Error: No such container: claude-eval-{run_id}"
                        )
                    return subprocess.CompletedProcess(args, 1, "", "No screen session")

                with mock.patch.object(app, "ensure_docker_engine_ready"), mock.patch.object(
                    app, "run_command", side_effect=cleanup_command
                ), mock.patch.object(
                    app,
                    "docker_container_owned_by_run",
                    return_value=(False, True, "容器不存在"),
                ), mock.patch.object(
                    app, "docker_container_exists", return_value=False
                ), mock.patch.object(app, "close_terminal_screen", return_value=True):
                    retried = app.retry_failed_startup(run_id)

                stored = app.run_row(run_id)
                turn = app.turn_row(run_id, 1)

            self.assertEqual(retried["id"], run_id)
            self.assertEqual(stored["phase"], "queued")
            self.assertEqual(stored["container_cleaned"], 0)
            self.assertIsNone(stored["error"])
            self.assertEqual(turn["status"], "queued")
            schedule.assert_called_once_with(run_id, "queued", app.first_turn_worker)

    def test_retry_failed_startup_refuses_a_written_prompt_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "TERMINAL_ASSETS_DIR", root / "terminal-assets"
            ), mock.patch.object(app, "HISTORY_PATH", root / "history.md"), mock.patch.object(
                app, "schedule_worker"
            ):
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "retry-prompt-guard-demo",
                    "project_directory": "zzzz",
                    "task_type": "0-1 代码生成",
                    "first_prompt": "完成一个容器化项目",
                    "_defer_start": True,
                })
                run_id = created["id"]
                app.update_run(run_id, phase="failed")
                app.add_event(run_id, "正在进行本题容器启动预检")
                paths = app.terminal_asset_paths(run_id)
                paths["root"].mkdir(parents=True)
                paths["prompt"].write_text("已经发送", encoding="utf-8")

                with mock.patch.object(app, "ensure_docker_engine_ready") as health:
                    with self.assertRaisesRegex(app.WorkflowError, "题面发送或轨迹"):
                        app.retry_failed_startup(run_id)

            health.assert_not_called()

    def test_retry_startup_api_rejects_generation_failure_placeholder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "TERMINAL_ASSETS_DIR", root / "terminal-assets"
            ), mock.patch.object(app, "HISTORY_PATH", root / "history.md"), mock.patch.object(
                app, "schedule_worker"
            ):
                app.initialize_database()
                created = app.create_run({
                    "repo_name": "generation-placeholder-demo",
                    "project_directory": "zzzz",
                    "task_type": "0-1 代码生成",
                    "first_prompt": "题面生成中，请稍候",
                    "_defer_start": True,
                })
                run_id = created["id"]
                with app.db_connection() as database:
                    database.execute(
                        """UPDATE runs
                           SET repo_name = '题目生成中', phase = 'failed',
                               error = '题面生成失败'
                           WHERE id = ?""",
                        (run_id,),
                    )
                app.add_event(run_id, "正在进行本题容器启动预检")

                serialized = app.serialize_run(app.run_row(run_id))
                with mock.patch.object(app, "ensure_docker_engine_ready") as health:
                    with self.assertRaisesRegex(app.WorkflowError, "只能重新生成题面"):
                        app.retry_failed_startup(run_id)

            self.assertFalse(serialized["can_retry_startup"])
            health.assert_not_called()


class DraftTests(unittest.TestCase):
    def candidate(self):
        return {
            "title": "Webhook Failure Replay Lab",
            "repo_slug": "webhook-failure-replay-lab",
            "business_domain": "第三方 webhook 可靠交付",
            "engineering_core": "不可变投递状态机与可恢复重放",
            "input_form": "签名 HTTP webhook 事件",
            "primary_user": "平台值班工程师",
            "failure_boundary": "目标超时、进程退出与密钥轮换",
            "implementation_modules": ["事件接收", "投递状态", "人工重放", "结果查询"],
            "runtime_components": ["API", "投递 worker"],
            "supporting_mechanisms": ["幂等接收", "失败重放"],
            "complex_mechanisms": ["进程中断恢复"],
            "custom_algorithm_families": [],
            "acceptance_scenarios": ["正常投递", "超时进入死信", "中断后恢复"],
            "language_framework": ["Docker", "Python", "FastAPI", "PostgreSQL"],
            "prompt": (
                "合作方的回调端点时好时坏，值班人员需要看清一条事件为何没有抵达，并在不篡改原记录的前提下安全重放。代码从一个空仓库起步，不创建任何前端页面，使用 Python 编写服务，由 FastAPI 提供事件接收、订阅配置、失败查询和人工重放接口，PostgreSQL 保存不可变投递记录。保存签名密钥的本地文件要进入 .gitignore，README 在订阅配置说明旁写清轮换步骤，代码不能留下占位实现、假接口或固定响应。Docker Compose 负责启动 API、工作进程和数据库，容器使用非 root 用户并暴露健康检查；pytest 在这里验证进程边界、事务回滚和恢复行为。同一业务事件通过幂等键避免重复入库，投递尝试按严格状态流转并记录签名版本、响应摘要与耗时。多个工作进程并发领取任务时不得重复发送，失败重试采用带抖动的指数退避，达到上限后进入死信，人工重放必须创建新尝试而不能覆盖历史。统计接口按订阅和时间范围计算成功率、延迟分位数与积压量，非法状态跳转返回结构化错误；密钥轮换期间的旧任务仍使用创建时版本，目标超时不能吞掉尝试记录。最终即使进程在投递中途退出，重启后也能从历史记录还原每个事件的真实去向。"
            ),
            "verification_commands": [
                "docker compose run --rm api pytest -q",
                "docker compose config --quiet",
            ],
        }

    def scope_review(self):
        return {
            "approved": True,
            "history_overlap": False,
            "estimated_task_difficulty": "困难",
            "difficulty_evidence": ["进程中断恢复需要跨请求维持投递状态不变量"],
            "reasons": [],
            "soft_suggestions": [],
            "closest_history_repo": "",
            "scope_review": {
                "engineering_core_count": 1,
                "implementation_modules": ["事件接收", "投递状态", "人工重放", "结果查询"],
                "runtime_components": ["API", "投递 worker"],
                "supporting_mechanisms": ["幂等接收", "失败重放"],
                "complex_mechanisms": ["进程中断恢复"],
                "custom_algorithm_families": [],
                "acceptance_scenario_count": 3,
                "undeclared_scope_items": [],
                "undefined_domain_decisions": [],
                "unjustified_infrastructure": [],
            },
        }

    def test_task_draft_is_generated_and_validated_by_gpt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "unique_repo_name", side_effect=lambda name: name), mock.patch.object(
                app, "run_codex_task_generation", return_value=self.candidate()
            ) as generate, mock.patch.object(
                app, "run_codex_task_validation",
                return_value=self.scope_review(),
            ) as validate:
                app.initialize_database()
                draft = app.generate_task_draft(1)

        self.assertEqual(draft["project_number"], "0001")
        self.assertEqual(draft["task_type"], "0-1 代码生成")
        self.assertEqual(draft["task_difficulty"], "待评估")
        self.assertEqual(
            draft["difficulty_contract"]["estimated_task_difficulty"], "困难"
        )
        self.assertEqual(draft["difficulty_contract"]["axis"], "状态不变量")
        self.assertEqual(draft["first_prompt"], self.candidate()["prompt"])
        self.assertNotIn("项目编号", draft["first_prompt"])
        self.assertNotIn("\n", draft["first_prompt"])
        self.assertIn("Docker, Python", draft["language_framework"])
        generate.assert_called_once()
        validate.assert_called_once()

    def test_task_generation_batches_candidates_and_reviews_only_best_local_match(self):
        preferred = self.candidate()
        generic_ending = self.candidate()
        generic_ending["title"] = "Alternate Replay Service"
        generic_ending["repo_slug"] = "alternate-replay-service"
        generic_ending.update({
            "business_domain": "冷链探针数据接入",
            "engineering_core": "分段校验与断点续传",
            "input_form": "离线设备上传的二进制数据包",
            "primary_user": "实验室设备维护员",
            "failure_boundary": "数据包截断与校验失败",
        })
        generic_ending["prompt"] = generic_ending["prompt"] + "相关说明同时记录在 README、.gitignore 与占位实现约束中。"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "unique_repo_name", side_effect=lambda name: name), mock.patch.object(
                app,
                "run_codex_task_generation",
                return_value={"candidates": [generic_ending, preferred]},
            ) as generate, mock.patch.object(
                app,
                "run_codex_task_validation",
                return_value=self.scope_review(),
            ) as validate:
                app.initialize_database()
                draft = app.generate_task_draft(1)

        self.assertEqual(draft["repo_name"], preferred["repo_slug"])
        self.assertEqual(draft["first_prompt"], preferred["prompt"])
        generate.assert_called_once()
        validate.assert_called_once()
        self.assertEqual(validate.call_args.args[0]["first_prompt"], preferred["prompt"])

    def test_task_candidate_schema_enforces_hard_prompt_length(self):
        prompt_schema = app.task_candidate_schema()["properties"]["prompt"]

        self.assertEqual(prompt_schema["minLength"], 300)
        self.assertEqual(prompt_schema["maxLength"], 600)

    def test_targeted_rewrite_repeats_prompt_length_budget(self):
        candidate = self.candidate()
        review = self.scope_review()
        review["approved"] = False
        review["scope_review"]["supporting_mechanisms"] = ["幂等", "重试", "统计"]
        with mock.patch.object(
            app, "run_codex_structured", return_value=candidate
        ) as structured:
            app.run_codex_task_rewrite(
                candidate,
                "纯后端",
                [],
                "核心验收边界不唯一",
                review=review,
            )

        rewrite_prompt = structured.call_args.args[0]
        self.assertIn("目标约 450 字", rewrite_prompt)
        self.assertIn("优先控制在 300 至 520 字", rewrite_prompt)
        self.assertIn("必须处于 300 至 600 字", rewrite_prompt)
        self.assertIn("不能只增不减", rewrite_prompt)
        self.assertIn("从空仓库起步和 Docker Compose", rewrite_prompt)
        self.assertIn('"independent_review"', rewrite_prompt)
        self.assertIn('"supporting_mechanisms": ["幂等", "重试", "统计"]', rewrite_prompt)

    def test_task_generation_uses_second_batch_only_after_local_hard_failure(self):
        invalid = self.candidate()
        invalid["prompt"] = "代码从空仓库起步，并通过 Docker 运行。"
        progress = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "unique_repo_name", side_effect=lambda name: name), mock.patch.object(
                app,
                "run_codex_task_generation",
                side_effect=[
                    {"candidates": [invalid, invalid]},
                    {"candidates": [self.candidate(), self.candidate()]},
                ],
            ) as generate, mock.patch.object(
                app, "run_codex_task_validation", return_value=self.scope_review()
            ), mock.patch.object(app, "run_codex_task_rewrite") as rewrite:
                app.initialize_database()
                draft = app.generate_task_draft(1, progress=progress.append)

        self.assertEqual(draft["repo_name"], self.candidate()["repo_slug"])
        self.assertEqual(generate.call_count, 2)
        rewrite.assert_not_called()
        self.assertIn("第 1/2 批候选生成中", progress)
        self.assertIn("第 2/2 批候选生成中", progress)
        self.assertIn("正在独立复核候选题", progress)

    def test_failed_review_rewrites_same_candidate_once_instead_of_new_batch(self):
        rejected = self.scope_review()
        rejected["approved"] = False
        rejected["reasons"] = ["核心验收边界不唯一"]
        rewritten = self.candidate()
        rewritten["title"] = "Webhook Replay Boundary Lab"
        rewritten["repo_slug"] = "webhook-replay-boundary-lab"
        rewritten["prompt"] = rewritten["prompt"].replace(
            "目标超时不能吞掉尝试记录",
            "目标超时按服务端记录的截止时间裁决，且不能吞掉尝试记录",
        )
        progress = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "unique_repo_name", side_effect=lambda name: name), mock.patch.object(
                app,
                "run_codex_task_generation",
                return_value={"candidates": [self.candidate(), self.candidate()]},
            ) as generate, mock.patch.object(
                app,
                "run_codex_task_validation",
                side_effect=[rejected, self.scope_review()],
            ) as validate, mock.patch.object(
                app, "run_codex_task_rewrite", return_value=rewritten
            ) as rewrite:
                app.initialize_database()
                draft = app.generate_task_draft(1, progress=progress.append)

        self.assertEqual(generate.call_count, 1)
        rewrite.assert_called_once()
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(draft["repo_name"], rewritten["repo_slug"])
        self.assertIn("正在按复核意见定向改写", progress)
        self.assertIn("正在复核定向改写结果", progress)

    def test_history_overlap_uses_second_candidate_without_rewriting_first(self):
        preferred = self.candidate()
        alternate = self.candidate()
        alternate["title"] = "Cold Chain Boundary Viewer"
        alternate["repo_slug"] = "cold-chain-boundary-viewer"
        alternate["business_domain"] = "冷链边界复核"
        alternate["engineering_core"] = "温区区间归并"
        alternate["input_form"] = "记录仪导出的 CSV"
        alternate["primary_user"] = "冷链质量员"
        alternate["failure_boundary"] = "跨日时间回拨"
        rejected = self.scope_review()
        rejected["approved"] = False
        rejected["history_overlap"] = True
        rejected["closest_history_repo"] = "old-replay-lab"
        rejected["reasons"] = ["与历史题目的核心交互结构实质重复"]
        progress = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "unique_repo_name", side_effect=lambda name: name), mock.patch.object(
                app,
                "run_codex_task_generation",
                return_value={"candidates": [preferred, alternate]},
            ), mock.patch.object(
                app,
                "generated_task_quality_key",
                side_effect=lambda candidate, history: (
                    0 if candidate["repo_name"] == preferred["repo_slug"] else 1,
                ),
            ), mock.patch.object(
                app,
                "run_codex_task_validation",
                side_effect=[rejected, self.scope_review()],
            ) as validate, mock.patch.object(app, "run_codex_task_rewrite") as rewrite:
                app.initialize_database()
                draft = app.generate_task_draft(1, progress=progress.append)

        self.assertEqual(draft["repo_name"], alternate["repo_slug"])
        self.assertEqual(validate.call_count, 2)
        rewrite.assert_not_called()
        self.assertIn("当前候选与历史题面实质重复，按需生成下一候选", progress)

    def test_task_generation_has_a_ten_minute_overall_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(
                app.time, "monotonic", side_effect=[0, 601]
            ), mock.patch.object(
                app, "run_codex_task_generation"
            ) as generate:
                app.initialize_database()
                with self.assertRaisesRegex(app.WorkflowError, "10 分钟"):
                    app.generate_task_draft(1)

        generate.assert_not_called()

    def test_history_generation_payload_is_compact_and_review_shortlist_is_bounded(self):
        history = [
            app.history_record(
                f"repo-{number}",
                "0-1 代码生成",
                (f"历史业务 {number}。" + "不同的完整实现细节。" * 80),
            )
            for number in range(20)
        ]
        payload = app.history_summary_payload(history)
        closest = app.closest_history_for_candidate(self.candidate(), history, 10)

        self.assertEqual(len(payload), 15)
        self.assertTrue(all("prompt" not in item for item in payload))
        self.assertTrue(all(len(item["summary"]) <= 260 for item in payload))
        self.assertEqual(len(closest), 10)

    def test_task_generation_targets_hard_work_without_self_reported_difficulty(self):
        with mock.patch.object(
            app,
            "run_codex_structured",
            return_value={"candidates": [self.candidate()] * 2},
        ) as codex:
            app.run_codex_task_generation(2, "纯前端", [])

        schema = codex.call_args.args[1]
        self.assertEqual(schema["properties"]["candidates"]["minItems"], 1)
        self.assertEqual(schema["properties"]["candidates"]["maxItems"], 1)
        properties = schema["properties"]["candidates"]["items"]["properties"]
        self.assertNotIn("task_difficulty", properties)
        for field in app.TASK_DIVERSITY_FIELDS:
            self.assertIn(field, properties)
        for field in app.TASK_SCOPE_LIST_FIELDS:
            self.assertIn(field, properties)
        generation_prompt = codex.call_args.args[0]
        self.assertIn("目标约 450 字", generation_prompt)
        self.assertIn("300 至 520 字", generation_prompt)
        self.assertIn("不能成为主体", generation_prompt)
        self.assertIn("只会在它无法通过或无法定向修复时再请求下一候选", generation_prompt)
        self.assertIn("最后 160 字", generation_prompt)
        self.assertIn("最多出现 3 种", generation_prompt)
        self.assertIn("这是写作偏好", generation_prompt)
        self.assertIn("只有会导致核心验收结果不唯一", generation_prompt)
        self.assertIn("有且只有一个", generation_prompt)
        self.assertIn("范围预算", generation_prompt)
        self.assertIn("最多 2 个", generation_prompt)
        self.assertIn("自定义算法只能选择一条作为主难点", generation_prompt)
        self.assertIn("没有需要持久化的数据就不要启动数据库", generation_prompt)
        self.assertNotIn("复杂度控制在中等偏易", generation_prompt)
        self.assertIn("预计必须达到困难或地狱", generation_prompt)
        self.assertIn("题面正文不得出现难度标签", generation_prompt)
        self.assertIn(app.GENERATION_DIFFICULTY_AXIS_GUIDANCE, generation_prompt)
        self.assertIn("设计 1 道", generation_prompt)
        self.assertNotIn("一次设计 2 道", generation_prompt)
        self.assertEqual(codex.call_args.kwargs["reasoning_effort"], "medium")

    def test_task_batch_rejects_repeated_scope_dimensions(self):
        first = app.validate_generated_task(
            self.candidate(), 1, "纯后端", [], resolve_unique_name=False
        )
        second_candidate = self.candidate()
        second_candidate["title"] = "Different Surface Name"
        second_candidate["repo_slug"] = "different-surface-name"
        second = app.validate_generated_task(
            second_candidate, 1, "纯后端", [], resolve_unique_name=False
        )

        with self.assertRaisesRegex(app.WorkflowError, "不能只更换业务名词"):
            app.validate_task_batch_diversity([first, second])

    def test_project_number_keeps_four_three_three_distribution(self):
        self.assertEqual(
            [app.category_for_project_number(number) for number in range(1, 11)],
            ["纯后端", "纯前端", "全栈", "纯后端", "纯前端", "全栈", "纯后端", "纯前端", "全栈", "纯后端"],
        )
        self.assertEqual(app.category_for_project_number(11), "纯后端")

    def test_task_validation_has_independent_hard_difficulty_gate(self):
        with mock.patch.object(
            app,
            "run_codex_structured",
            return_value={
                "approved": True,
                "reasons": [],
                "soft_suggestions": [],
                "closest_history_repo": "",
            },
        ) as codex:
            app.run_codex_task_validation(
                app.validate_generated_task(self.candidate(), 1, "纯后端", []),
                "纯后端",
                [],
            )
        schema = codex.call_args.args[1]
        self.assertIn("estimated_task_difficulty", schema["properties"])
        self.assertIn("difficulty_evidence", schema["properties"])
        self.assertIn("difficulty_contract", schema["properties"])
        self.assertIn("只有同时满足这些硬条件", codex.call_args.args[0])
        self.assertIn("estimated_task_difficulty 为困难或地狱", codex.call_args.args[0])
        self.assertIn("scope_review", schema["properties"])
        self.assertIn("soft_suggestions", schema["properties"])
        self.assertIn("不能照抄或信任候选题自报的范围字段", codex.call_args.args[0])
        self.assertIn(app.REVIEW_DIFFICULTY_AXIS_GUIDANCE, codex.call_args.args[0])
        self.assertIn(app.DIFFICULTY_CONTRACT_REVIEW_GUIDANCE, codex.call_args.args[0])
        self.assertEqual(codex.call_args.kwargs["reasoning_effort"], "medium")

    def test_local_task_validation_rejects_generic_systems_overcomplexity_and_forbidden_topics(self):
        candidate = self.candidate()
        candidate["prompt"] += "最终做成统一的设备管理系统。"
        with self.assertRaisesRegex(app.WorkflowError, "管理系统"):
            app.validate_generated_task(candidate, 1, "纯后端", [])
        candidate = self.candidate()
        candidate["prompt"] += "再增加一套分布式架构。"
        with self.assertRaisesRegex(app.WorkflowError, "复杂机制"):
            app.validate_generated_task(candidate, 1, "纯后端", [])
        candidate = self.candidate()
        candidate["prompt"] += "最后增加一个天气看板。"
        with self.assertRaisesRegex(app.WorkflowError, "禁止题材"):
            app.validate_generated_task(candidate, 1, "纯后端", [])
        candidate = self.candidate()
        candidate["prompt"] += "最后再附带一个 TODO 应用。"
        with self.assertRaisesRegex(app.WorkflowError, "禁止题材"):
            app.validate_generated_task(candidate, 1, "纯后端", [])

    def test_local_task_validation_enforces_scope_budget(self):
        cases = (
            ("implementation_modules", ["一", "二", "三", "四", "五"], "实现模块"),
            ("runtime_components", ["API", "worker", "simulator"], "应用运行组件"),
            ("supporting_mechanisms", ["幂等", "重试", "统计"], "辅助机制"),
            ("complex_mechanisms", ["崩溃续作", "反向补偿"], "复杂机制"),
            ("custom_algorithm_families", ["格式解释", "计算几何"], "自定义算法体系"),
            ("acceptance_scenarios", ["一", "二", "三", "四", "五", "六", "七"], "验收场景"),
        )
        for field, values, message in cases:
            with self.subTest(field=field):
                candidate = self.candidate()
                candidate[field] = values
                with self.assertRaisesRegex(app.WorkflowError, message):
                    app.validate_generated_task(candidate, 1, "纯后端", [])

    def test_local_task_validation_rejects_complex_state_plus_custom_algorithm(self):
        candidate = self.candidate()
        candidate["custom_algorithm_families"] = ["区间裁决"]
        with self.assertRaisesRegex(app.WorkflowError, "只能选择复杂状态机制或自定义算法"):
            app.validate_generated_task(candidate, 1, "纯后端", [])

    def test_local_task_validation_requires_scope_metadata(self):
        candidate = self.candidate()
        del candidate["complex_mechanisms"]
        with self.assertRaisesRegex(app.WorkflowError, "缺少范围字段"):
            app.validate_generated_task(candidate, 1, "纯后端", [])

    def test_independent_review_scope_cannot_approve_stacked_mechanisms(self):
        review = self.scope_review()
        review["scope_review"]["complex_mechanisms"] = ["崩溃续作", "反向补偿"]
        review["scope_review"]["undeclared_scope_items"] = ["设备模拟器"]
        review["scope_review"]["undefined_domain_decisions"] = ["盲文编码标准"]
        review["scope_review"]["unjustified_infrastructure"] = ["没有持久化职责的数据库"]

        errors = app.task_review_scope_errors(review)

        self.assertTrue(any("复杂机制共 2 项" in error for error in errors))
        self.assertTrue(any("未申报的实质范围" in error for error in errors))
        self.assertTrue(any("未定义规则" in error for error in errors))
        self.assertTrue(any("没有实际职责的基础设施" in error for error in errors))

    def test_independent_review_rejects_medium_difficulty(self):
        review = self.scope_review()
        review["estimated_task_difficulty"] = "中等"
        review["difficulty_evidence"] = ["只涉及常规接口与页面联调"]

        errors = app.task_review_scope_errors(review)

        self.assertTrue(any("未达到困难：中等" in error for error in errors))

    def test_independent_review_rejects_borderline_difficulty_margin(self):
        review = self.scope_review()
        review["difficulty_margin"] = "困难边缘"
        review["hardness_basis"] = "核心流程仍可能退化成常规接口串联"

        errors = app.task_review_scope_errors(review)

        self.assertTrue(any("难度余量不足：困难边缘" in error for error in errors))

    def test_local_task_validation_hard_limit_is_600_chars(self):
        candidate = self.candidate()
        candidate["prompt"] += "补充异常恢复约束。" * 80
        with self.assertRaisesRegex(app.WorkflowError, "600"):
            app.validate_generated_task(candidate, 1, "纯后端", [])

    def test_local_task_validation_hard_minimum_is_300_chars(self):
        candidate = self.candidate()
        candidate["prompt"] = "设备突发故障，代码从空仓库起步，并使用 Docker 完成运行与验收。"
        with self.assertRaisesRegex(app.WorkflowError, "300"):
            app.validate_generated_task(candidate, 1, "纯后端", [])

    def test_local_task_validation_rejects_project_number_in_prompt(self):
        candidate = self.candidate()
        candidate["prompt"] = "项目编号 0001。" + candidate["prompt"]
        with self.assertRaisesRegex(app.WorkflowError, "只能用于文件夹名称"):
            app.validate_generated_task(candidate, 1, "纯后端", [])

    def test_local_task_validation_rejects_project_number_in_repo_slug(self):
        candidate = self.candidate()
        candidate["repo_slug"] = "0001-webhook-failure-replay-lab"
        with self.assertRaisesRegex(app.WorkflowError, "仓库名不能包含编号前缀"):
            app.validate_generated_task(candidate, 1, "纯后端", [])

    def test_local_task_validation_treats_generic_opening_as_soft_quality_issue(self):
        candidate = self.candidate()
        candidate["prompt"] = "从空仓库实现一个回调故障复盘服务。" + candidate["prompt"]
        validated = app.validate_generated_task(candidate, 1, "纯后端", [])
        self.assertGreater(app.generated_task_quality_key(validated, [])[2], 0)

    def test_candidate_order_prefers_a_real_hard_driver(self):
        hard = app.validate_generated_task(
            self.candidate(), 1, "纯后端", [], resolve_unique_name=False
        )
        medium = self.candidate()
        medium["complex_mechanisms"] = []
        medium = app.validate_generated_task(
            medium, 1, "纯后端", [], resolve_unique_name=False
        )

        self.assertLess(
            app.generated_task_quality_key(hard, []),
            app.generated_task_quality_key(medium, []),
        )

    def test_local_task_validation_treats_delivery_checklist_ending_as_soft_quality_issue(self):
        candidate = self.candidate()
        candidate["prompt"] = candidate["prompt"][:-29] + (
            "提交 Dockerfile、compose.yaml、README 和 .gitignore，增加健康检查，"
            "不得留下占位实现、假数据或未实现分支。"
        )
        validated = app.validate_generated_task(candidate, 1, "纯后端", [])
        self.assertGreater(app.generated_task_quality_key(validated, [])[3], 0)

    def test_local_task_validation_does_not_hard_reject_historical_edge_similarity(self):
        candidate = self.candidate()
        history = [{"repo_name": "old-relay", "prompt": candidate["prompt"][:120] + "甲" * 900}]
        validated = app.validate_generated_task(candidate, 1, "纯后端", history)
        self.assertGreater(app.generated_task_quality_key(validated, history)[4], 0)

    def test_local_task_validation_still_rejects_a_substantive_history_duplicate(self):
        candidate = self.candidate()
        history = [{"repo_name": "old-relay", "prompt": candidate["prompt"]}]
        with self.assertRaisesRegex(app.WorkflowError, "题面与历史仓库"):
            app.validate_generated_task(candidate, 1, "纯后端", history)

    def test_markdown_history_is_loaded_when_database_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "history-prompts.md"
            history.write_text(
                """# 历史 0-1 题库
<!-- task-entry-start {"run_id":"old1","repo_name":"old-project","task_type":"0-1 代码生成"} -->
## 0001 · old-project
### User Prompt
<!-- prompt-start -->
从空仓库完成一个历史项目，并确保全部服务通过 Docker Compose 运行。
<!-- prompt-end -->
<!-- task-entry-end -->
""",
                encoding="utf-8",
            )
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "HISTORY_PATH", history):
                app.initialize_database()
                records = app.historical_task_context()

        self.assertEqual(records[0]["repo_name"], "old-project")
        self.assertIn("历史项目", records[0]["prompt"])

    def test_unique_repo_name_skips_existing_local_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample-project").mkdir()
            unavailable = subprocess.CompletedProcess([], 1, "", "not found")
            with mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "run_command", return_value=unavailable
            ):
                name = app.unique_repo_name("sample-project")
            self.assertNotEqual(name, "sample-project")
            self.assertTrue(name.startswith("sample-project-"))

    def test_automatic_run_is_visible_before_background_generation(self):
        draft = {
            "project_number": "0001",
            "project_name": "示例项目",
            "repo_name": "sample-project",
            "category": "纯后端",
            "task_type": "0-1 代码生成",
            "task_difficulty": "困难",
            "language_framework": "Docker, Python, FastAPI",
            "first_prompt": "完整的第一轮任务",
            "verification_commands": ["docker compose run --rm api pytest -q"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            history = root / "history-prompts.md"
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", history
            ), mock.patch.object(
                app, "generate_task_draft", return_value=draft
            ) as generate, mock.patch.object(app, "schedule_worker") as scheduler:
                app.initialize_database()
                created = app.create_automatic_run({"project_directory": "team-a"})

            self.assertEqual(created["project_number"], "0001")
            self.assertEqual(created["project_directory"], "team-a")
            self.assertEqual(created["project_category"], "纯后端")
            self.assertEqual(created["repo_name"], "题目生成中")
            self.assertEqual(created["phase"], "generation_queued")
            self.assertEqual(created["stage_timings"]["generation"]["status"], "current")
            self.assertEqual(Path(created["repo_path"]), root / "team-a" / "0001-pending-project" / "workspace")
            self.assertFalse(history.exists())
            generate.assert_not_called()
            scheduler.assert_called_once_with(
                created["id"], "generation_queued", app.automatic_generation_worker
            )

    def test_background_generation_updates_the_same_run_and_starts_it(self):
        draft = {
            "project_number": "0001",
            "project_name": "示例项目",
            "repo_name": "sample-project",
            "category": "纯后端",
            "task_type": "0-1 代码生成",
            "task_difficulty": "待评估",
            "language_framework": "Docker, Python, FastAPI",
            "first_prompt": "完整的第一轮任务",
            "verification_commands": ["docker compose run --rm api pytest -q"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            history = root / "history-prompts.md"
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", history
            ), mock.patch.object(app, "schedule_worker") as scheduler, mock.patch.object(
                app, "generate_task_draft", return_value=draft
            ) as generate:
                app.initialize_database()
                created = app.create_automatic_run({"project_directory": "team-a"})
                scheduler.reset_mock()
                app.automatic_generation_worker(created["id"])
                stored = app.serialize_run(app.run_row(created["id"]))

            self.assertEqual(stored["id"], created["id"])
            self.assertEqual(stored["project_number"], "0001")
            self.assertEqual(stored["repo_name"], "sample-project")
            self.assertEqual(stored["phase"], "queued")
            self.assertEqual(stored["first_prompt"], "完整的第一轮任务")
            self.assertEqual(
                Path(stored["repo_path"]),
                root / "team-a" / "0001-sample-project" / "workspace",
            )
            self.assertEqual(stored["stage_timings"]["generation"]["status"], "done")
            self.assertEqual(stored["stage_timings"]["repo"]["status"], "current")
            self.assertIn("完整的第一轮任务", history.read_text(encoding="utf-8"))
            generate.assert_called_once_with(1, progress=mock.ANY)
            scheduler.assert_called_once_with(created["id"], "queued", app.first_turn_worker)

    def test_repeated_automatic_create_returns_the_same_generating_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker") as scheduler:
                app.initialize_database()
                first = app.create_automatic_run({"project_directory": "team-a"})
                second = app.create_automatic_run({"project_directory": "team-a"})

            self.assertEqual(first["id"], second["id"])
            self.assertEqual(second["project_number"], "0001")
            scheduler.assert_called_once()

    def test_auto_refill_can_generate_multiple_new_projects_in_parallel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker") as scheduler:
                app.initialize_database()
                created = [
                    app.create_automatic_run(
                        {"project_directory": "team-a", "_auto_refill": True},
                        allow_parallel_generation=True,
                    )
                    for _ in range(app.TASK_GENERATION_MAX_PARALLEL)
                ]
                capped = app.create_automatic_run(
                    {"project_directory": "team-a", "_auto_refill": True},
                    allow_parallel_generation=True,
                )

            self.assertEqual(len({item["id"] for item in created}), app.TASK_GENERATION_MAX_PARALLEL)
            self.assertIn(capped["id"], {item["id"] for item in created})
            self.assertEqual(
                [item["project_number"] for item in created],
                [f"{index:04d}" for index in range(1, app.TASK_GENERATION_MAX_PARALLEL + 1)],
            )
            self.assertEqual(scheduler.call_count, app.TASK_GENERATION_MAX_PARALLEL)

    def test_failed_generation_retries_same_number_before_next_create_advances(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker"), mock.patch.object(
                app, "generate_task_draft", side_effect=app.WorkflowError("生成失败")
            ):
                app.initialize_database()
                first = app.create_automatic_run({"project_directory": "team-a"})
                app.automatic_generation_worker(first["id"])
                retrying = app.serialize_run(app.run_row(first["id"]))
                same = app.create_automatic_run({"project_directory": "team-a"})
                app.automatic_generation_worker(first["id"])
                failed = app.serialize_run(app.run_row(first["id"]))
                second = app.create_automatic_run({"project_directory": "team-a"})

            self.assertEqual(retrying["phase"], "generation_queued")
            self.assertEqual(retrying["generation_retry_count"], 1)
            self.assertEqual(retrying["generation_feedback"], "生成失败")
            self.assertEqual(same["id"], first["id"])
            self.assertEqual(failed["phase"], "failed")
            self.assertEqual(failed["project_number"], "0001")
            self.assertEqual(failed["status_detail"], "题目生成失败")
            self.assertEqual(second["project_number"], "0002")

    def test_recovery_requeues_a_generation_interrupted_by_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker") as scheduler:
                app.initialize_database()
                created = app.create_automatic_run({"project_directory": "team-a"})
                app.update_run(created["id"], phase="generation_running")
                scheduler.reset_mock()
                app.recover_monitors()
                recovered = app.run_row(created["id"])

            self.assertEqual(recovered["phase"], "generation_queued")
            self.assertIn("服务恢复", recovered["status_detail"])
            scheduler.assert_called_once_with(
                created["id"], "generation_queued", app.automatic_generation_worker
            )

    def test_failed_generation_can_retry_under_the_same_number(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker") as scheduler:
                app.initialize_database()
                created = app.create_automatic_run({"project_directory": "team-a"})
                app.update_turn(created["id"], 1, status="failed")
                app.update_run(
                    created["id"], phase="failed", status_detail="题目生成失败",
                    error="候选不合规", generation_retry_count=2,
                )
                scheduler.reset_mock()
                retried = app.retry_automatic_generation(created["id"])

            self.assertEqual(retried["id"], created["id"])
            self.assertEqual(retried["project_number"], "0001")
            self.assertEqual(retried["phase"], "generation_queued")
            self.assertIsNone(retried["error"])
            self.assertEqual(retried["generation_feedback"], "候选不合规")
            self.assertEqual(retried["generation_retry_count"], 3)
            scheduler.assert_called_once_with(
                created["id"], "generation_queued", app.automatic_generation_worker
            )

    def test_auto_filled_generation_failure_skips_source_without_global_pause(self):
        rejection = (
            f"连续 {app.TASK_GENERATION_BATCH_ATTEMPTS} 批未生成合规题目："
            "并列裁决与数值序列化规则不明确"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker") as scheduler, mock.patch.object(
                app, "generate_task_draft", side_effect=app.WorkflowError(rejection)
            ) as generate:
                app.initialize_database()
                app.set_auto_refill({"enabled": True, "project_directory": "team-a"})
                created = app.create_automatic_run(
                    {"project_directory": "team-a", "_auto_refill": True}
                )
                scheduler.reset_mock()

                app.automatic_generation_worker(created["id"])
                retrying = app.serialize_run(app.run_row(created["id"]))
                app.automatic_generation_worker(created["id"])
                exhausted = app.serialize_run(app.run_row(created["id"]))
                configuration = app.auto_refill_configuration()
                with app.db_connection() as database:
                    failures = database.execute(
                        "SELECT value FROM settings "
                        "WHERE key = 'auto_refill_consecutive_failures'"
                    ).fetchone()["value"]

            self.assertEqual(retrying["phase"], "generation_queued")
            self.assertEqual(retrying["generation_retry_count"], 1)
            self.assertEqual(exhausted["phase"], "failed")
            self.assertEqual(exhausted["project_number"], "0001")
            self.assertEqual(exhausted["generation_retry_count"], 1)
            self.assertTrue(configuration["enabled"])
            self.assertIn("跳过未通过题面校验", configuration["detail"])
            self.assertEqual(failures, "0")
            scheduler.assert_called_once_with(
                created["id"], "generation_queued", app.automatic_generation_worker
            )
            self.assertEqual(generate.call_count, 2)
            self.assertNotIn("initial_feedback", generate.call_args_list[0].kwargs)
            self.assertEqual(
                generate.call_args_list[1].kwargs["initial_feedback"], rejection
            )

    def test_generation_candidate_rejection_is_not_an_infrastructure_failure(self):
        self.assertTrue(app.task_generation_candidate_quality_failure(
            "候选复核与一次定向改写后仍不合规：范围超过预算"
        ))
        self.assertTrue(app.task_generation_candidate_quality_failure(
            f"连续 {app.TASK_GENERATION_BATCH_ATTEMPTS} 批未生成合规题目：边界不明确"
        ))
        self.assertFalse(app.task_generation_candidate_quality_failure(
            "task-generation 超时，已停止"
        ))
        self.assertFalse(app.task_generation_candidate_quality_failure(
            "API 请求失败：连接中断"
        ))

    def test_cancelled_generation_is_stopped_without_counting_auto_refill_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker"), mock.patch.object(
                app, "generate_task_draft", side_effect=app.JobCancelled("后台任务已取消")
            ), mock.patch.object(app, "record_auto_refill_failure") as record_failure:
                app.initialize_database()
                created = app.create_automatic_run(
                    {"project_directory": "team-a", "_auto_refill": True}
                )
                app.automatic_generation_worker(created["id"])
                stopped = app.serialize_run(app.run_row(created["id"]))

            self.assertEqual(stopped["phase"], "stopped")
            self.assertIn("用户取消", stopped["status_detail"])
            record_failure.assert_not_called()

    def test_generation_cancelled_by_service_restart_returns_to_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker"), mock.patch.object(
                app, "generate_task_draft", side_effect=app.JobCancelled("后台任务已取消")
            ):
                app.initialize_database()
                created = app.create_automatic_run({"project_directory": "team-a"})
                app.SERVER_SHUTTING_DOWN.set()
                try:
                    app.automatic_generation_worker(created["id"])
                    queued = app.serialize_run(app.run_row(created["id"]))
                finally:
                    app.SERVER_SHUTTING_DOWN.clear()

            self.assertEqual(queued["phase"], "generation_queued")
            self.assertIn("服务重启", queued["status_detail"])
            self.assertEqual(queued["turns"][0]["status"], "queued")

    def test_retry_generation_button_is_only_for_failed_placeholders(self):
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="retry-generation"', javascript)
        self.assertIn("async function retryAutomaticGeneration()", javascript)
        self.assertIn("/retry-generation", javascript)


class AutoRefillTests(unittest.TestCase):
    def insert_run(
        self,
        database,
        run_id,
        repo_name,
        phase="complete",
        source_run_id=None,
        auto_refill=0,
        task_type=None,
        prompt="需求",
        task_difficulty="待评估",
        difficulty_contract=None,
    ):
        timestamp = app.now_text()
        contract = json.dumps(difficulty_contract or {}, ensure_ascii=False)
        database.execute(
            """INSERT INTO runs(
                 id, repo_name, repo_path, run_directory, repo_url, phase,
                 first_prompt, first_prompt_id, container_cleaned, task_type,
                 source_run_id, auto_refill, task_difficulty, difficulty_contract,
                 verification_commands, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prompt-1', 1, ?, ?, ?, ?, ?, '[]', ?, ?)""",
            (
                run_id,
                repo_name,
                f"/tmp/{repo_name}/workspace",
                f"/tmp/{repo_name}",
                f"https://example.invalid/{repo_name}",
                phase,
                prompt,
                task_type or ("Feature 迭代" if source_run_id else "0-1 代码生成"),
                source_run_id,
                auto_refill,
                task_difficulty,
                contract,
                timestamp,
                timestamp,
            ),
        )

    def test_switch_is_persistent_and_defaults_off(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root):
                app.initialize_database()
                self.assertFalse(app.auto_refill_configuration()["enabled"])
                enabled = app.set_auto_refill(
                    {"enabled": True, "project_directory": "team-a"}
                )
                self.assertTrue(enabled["enabled"])
                self.assertEqual(enabled["project_directory"], "team-a")
                self.assertEqual(enabled["max_iterations_per_root"], 6)
                self.assertEqual(enabled["max_new_modules_per_root"], 2)
                self.assertEqual(enabled["new_module_slots"], [3, 6])
                self.assertEqual(enabled["bugfix_slots"], [2, 5])
                self.assertIsNone(enabled["disable_at"])

    def test_scheduled_refill_shutdown_is_persistent_and_does_not_stop_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root):
                app.initialize_database()
                with mock.patch.object(app.time, "time", return_value=1_000):
                    scheduled = app.set_auto_refill(
                        {
                            "enabled": True,
                            "project_directory": "team-a",
                            "disable_after_hours": 1.5,
                        }
                    )
                self.assertTrue(scheduled["enabled"])
                self.assertIsNotNone(scheduled["disable_at"])
                self.assertEqual(scheduled["remaining_seconds"], 5_400)
                with app.db_connection() as database:
                    stored = database.execute(
                        "SELECT value FROM settings WHERE key = 'auto_refill_disable_at'"
                    ).fetchone()
                self.assertEqual(stored["value"], "6400")

                with mock.patch.object(app.time, "time", return_value=6_401):
                    expired = app.auto_refill_configuration()
                self.assertFalse(expired["enabled"])
                self.assertIsNone(expired["disable_at"])
                self.assertIsNone(expired["remaining_seconds"])
                self.assertIn("按计划关闭", expired["detail"])
                self.assertIn("已启动的任务继续运行", expired["detail"])
                with app.db_connection() as database:
                    values = {
                        row["key"]: row["value"]
                        for row in database.execute(
                            "SELECT key, value FROM settings WHERE key IN "
                            "('auto_refill_enabled', 'auto_refill_disable_at')"
                        )
                    }
                self.assertEqual(values["auto_refill_enabled"], "0")
                self.assertEqual(values["auto_refill_disable_at"], "")

    def test_scheduled_refill_start_is_persistent_and_enables_when_due(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root):
                app.initialize_database()
                with mock.patch.object(app.time, "time", return_value=1_000):
                    scheduled = app.set_auto_refill(
                        {
                            "enabled": False,
                            "project_directory": "team-a",
                            "enable_after_hours": 1.5,
                        }
                    )
                self.assertFalse(scheduled["enabled"])
                self.assertIsNotNone(scheduled["enable_at"])
                self.assertEqual(scheduled["start_remaining_seconds"], 5_400)
                self.assertIsNone(scheduled["disable_at"])
                self.assertIn("已预约", scheduled["detail"])
                with app.db_connection() as database:
                    stored = database.execute(
                        "SELECT value FROM settings WHERE key = 'auto_refill_enable_at'"
                    ).fetchone()
                self.assertEqual(stored["value"], "6400")

                with mock.patch.object(app.time, "time", return_value=6_401):
                    started = app.auto_refill_configuration()
                self.assertTrue(started["enabled"])
                self.assertIsNone(started["enable_at"])
                self.assertIsNone(started["start_remaining_seconds"])
                self.assertIn("按计划开启", started["detail"])
                with app.db_connection() as database:
                    values = {
                        row["key"]: row["value"]
                        for row in database.execute(
                            "SELECT key, value FROM settings WHERE key IN "
                            "('auto_refill_enabled', 'auto_refill_enable_at')"
                        )
                    }
                self.assertEqual(values["auto_refill_enabled"], "1")
                self.assertEqual(values["auto_refill_enable_at"], "")

    def test_scheduled_refill_start_validates_hours_and_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root):
                app.initialize_database()
                for invalid in (True, 0.25, 169, "later", []):
                    with self.subTest(invalid=invalid), self.assertRaisesRegex(
                        app.WorkflowError, "0.5 至 168"
                    ):
                        app.set_auto_refill(
                            {
                                "enabled": False,
                                "project_directory": "team-a",
                                "enable_after_hours": invalid,
                            }
                        )
                app.set_auto_refill(
                    {
                        "enabled": False,
                        "project_directory": "team-a",
                        "enable_after_hours": 2,
                    }
                )
                cancelled = app.set_auto_refill(
                    {
                        "enabled": False,
                        "project_directory": "team-a",
                        "enable_after_hours": None,
                    }
                )
                self.assertFalse(cancelled["enabled"])
                self.assertIsNone(cancelled["enable_at"])
                self.assertIsNone(cancelled["start_remaining_seconds"])
                self.assertIn("已关闭", cancelled["detail"])

    def test_scheduled_refill_shutdown_validates_hours_and_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root):
                app.initialize_database()
                for invalid in (True, 0.25, 169, "later", []):
                    with self.subTest(invalid=invalid), self.assertRaisesRegex(
                        app.WorkflowError, "0.5 至 168"
                    ):
                        app.set_auto_refill(
                            {
                                "enabled": True,
                                "project_directory": "team-a",
                                "disable_after_hours": invalid,
                            }
                        )
                app.set_auto_refill(
                    {
                        "enabled": True,
                        "project_directory": "team-a",
                        "disable_after_hours": 4,
                    }
                )
                cleared = app.set_auto_refill(
                    {
                        "enabled": True,
                        "project_directory": "team-a",
                        "disable_after_hours": None,
                    }
                )
                self.assertTrue(cleared["enabled"])
                self.assertIsNone(cleared["disable_at"])

    def test_candidate_respects_six_iteration_cap_and_one_active_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root):
                app.initialize_database()
                with app.db_connection() as database:
                    self.insert_run(database, "root11111111", "root-one")
                    self.insert_run(database, "root22222222", "root-two")
                    for number in range(6):
                        self.insert_run(
                            database,
                            f"full{number:08d}",
                            f"full-{number}",
                            source_run_id="root22222222",
                        )
                    parent_id = "root11111111"
                    for number in range(4):
                        child_id = f"done{number:08d}"
                        self.insert_run(
                            database,
                            child_id,
                            f"done-{number}",
                            source_run_id=parent_id,
                        )
                        parent_id = child_id
                    self.insert_run(
                        database,
                        "active111111",
                        "active-child",
                        phase="first_running",
                        source_run_id=parent_id,
                    )
                self.assertIsNone(app.auto_refill_iteration_candidate())
                app.update_run("active111111", phase="complete")
                candidate = app.auto_refill_iteration_candidate()

            self.assertEqual(candidate["id"], "root11111111")
            self.assertEqual(candidate["iteration_count"], 5)

    def test_candidate_skips_repository_with_an_active_sibling_lineage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root):
                app.initialize_database()
                with app.db_connection() as database:
                    self.insert_run(database, "shared111111", "shared-repo")
                    self.insert_run(database, "shared222222", "shared-repo")
                    self.insert_run(
                        database,
                        "active111111",
                        "shared-repo",
                        phase="first_running",
                        source_run_id="shared111111",
                    )
                    self.insert_run(database, "other1111111", "other-repo")
                candidate = app.auto_refill_iteration_candidate()

            self.assertEqual(candidate["id"], "other1111111")

    def test_refill_and_manual_iteration_skip_project_rejected_by_solo_qa(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    self.insert_run(database, "rejected1111", "rejected-root")
                    self.insert_run(
                        database,
                        "rejected2222",
                        "rejected-child",
                        source_run_id="rejected1111",
                    )
                    self.insert_run(database, "eligible1111", "eligible-root")
                    database.execute(
                        """INSERT INTO run_turns(
                             run_id, turn_number, intent_type, prompt, status,
                             created_at, updated_at
                           ) VALUES (?, 1, 'initial', '需求', 'complete', ?, ?)""",
                        ("rejected1111", timestamp, timestamp),
                    )
                    database.execute(
                        """INSERT INTO solo_qa_submissions(
                             run_id, turn_number, remote_submission_id, remote_status,
                             state, qc_summary, created_at, updated_at
                           ) VALUES (?, 1, '673', 'PENDING_FIX', 'needs_fix', ?, ?, ?)""",
                        (
                            "rejected1111",
                            "命中雷同题库「常见小应用」：todo",
                            timestamp,
                            timestamp,
                        ),
                    )

                candidate = app.auto_refill_iteration_candidate()
                self.assertEqual(candidate["id"], "eligible1111")
                with self.assertRaisesRegex(app.WorkflowError, "项目链.*不合格"):
                    app.validate_iteration_lineage_type(
                        "rejected2222", "Feature 迭代"
                    )

    def test_terminal_iteration_without_product_does_not_block_refill_lineage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root):
                app.initialize_database()
                with app.db_connection() as database:
                    self.insert_run(database, "root11111111", "root-one")
                    self.insert_run(
                        database,
                        "done1111111",
                        "done-one",
                        source_run_id="root11111111",
                    )
                    self.insert_run(
                        database,
                        "failed111111",
                        "failed-one",
                        phase="failed",
                        source_run_id="done1111111",
                    )

                candidate = app.auto_refill_iteration_candidate()
                state = app.iteration_lineage_state("root11111111")
                self.assertEqual(candidate["id"], "root11111111")
                self.assertEqual(candidate["iteration_count"], 1)
                self.assertEqual(candidate["abandoned_iteration_count"], 1)
                self.assertEqual(state["unresolved_iteration_count"], 0)
                self.assertEqual(state["abandoned_iteration_count"], 1)
                self.assertEqual(state["history"][-1]["outcome"], "abandoned")

                app.update_run("failed111111", phase="first_running")
                self.assertIsNone(app.auto_refill_iteration_candidate())

    def test_lineage_history_and_new_module_quota_cover_the_entire_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                with app.db_connection() as database:
                    self.insert_run(
                        database,
                        "root11111111",
                        "root-one",
                        prompt="原始项目题面",
                    )
                    self.insert_run(
                        database,
                        "feature11111",
                        "feature-one",
                        source_run_id="root11111111",
                        prompt="第一轮平滑扩展",
                    )
                    self.insert_run(
                        database,
                        "module111111",
                        "module-one",
                        source_run_id="feature11111",
                        task_type="0-1 代码生成",
                        prompt="第二轮完整模块",
                    )

                state = app.iteration_lineage_state("module111111")
                self.assertEqual(
                    [item["prompt"] for item in state["history"]],
                    ["原始项目题面", "第一轮平滑扩展", "第二轮完整模块"],
                )
                self.assertEqual(state["iteration_count"], 2)
                self.assertEqual(state["new_module_count"], 1)
                with self.assertRaisesRegex(app.WorkflowError, "不能连续"):
                    app.validate_iteration_lineage_type(
                        "module111111", "0-1 代码生成"
                    )

                with app.db_connection() as database:
                    self.insert_run(
                        database,
                        "feature22222",
                        "feature-two",
                        source_run_id="module111111",
                    )
                    self.insert_run(
                        database,
                        "module222222",
                        "module-two",
                        source_run_id="feature22222",
                        task_type="0-1 代码生成",
                    )
                with self.assertRaisesRegex(app.WorkflowError, "最多创建 2 个"):
                    app.validate_iteration_lineage_type(
                        "module222222", "0-1 代码生成"
                    )

    def test_automatic_type_interleaves_at_most_two_nonconsecutive_modules(self):
        self.assertEqual(
            app.automatic_iteration_task_type(
                {
                    "iteration_count": 0,
                    "new_module_count": 0,
                    "last_iteration_task_type": "",
                }
            ),
            "Feature 迭代",
        )
        self.assertEqual(
            app.automatic_iteration_task_type(
                {
                    "iteration_count": 1,
                    "new_module_count": 0,
                    "last_iteration_task_type": "Feature 迭代",
                }
            ),
            "Bug 修复",
        )
        self.assertEqual(
            app.automatic_iteration_task_type(
                {
                    "iteration_count": 4,
                    "new_module_count": 1,
                    "last_iteration_task_type": "Feature 迭代",
                }
            ),
            "Bug 修复",
        )
        self.assertEqual(
            app.automatic_iteration_task_type(
                {
                    "iteration_count": 2,
                    "new_module_count": 0,
                    "last_iteration_task_type": "Feature 迭代",
                }
            ),
            "0-1 代码生成",
        )
        self.assertEqual(
            app.automatic_iteration_task_type(
                {
                    "iteration_count": 5,
                    "new_module_count": 1,
                    "last_iteration_task_type": "Feature 迭代",
                }
            ),
            "0-1 代码生成",
        )
        for state in (
            {
                "iteration_count": 2,
                "new_module_count": 1,
                "last_iteration_task_type": "0-1 代码生成",
            },
            {
                "iteration_count": 5,
                "new_module_count": 2,
                "last_iteration_task_type": "Feature 迭代",
            },
        ):
            with self.subTest(state=state):
                self.assertEqual(
                    app.automatic_iteration_task_type(state), "Feature 迭代"
                )

    def test_medium_delivery_with_contract_forces_feature_recovery(self):
        contract = {
            "version": 1,
            "estimated_task_difficulty": "困难",
            "axis": "状态不变量",
            "hard_requirement": "断点恢复后必须保持批次状态唯一",
            "acceptance_evidence": ["中断后从最后确认点继续"],
            "rejected_shortcut": "只保存一个完成标记无法恢复分段状态",
            "difficulty_evidence": ["跨进程维护恢复状态"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                with app.db_connection() as database:
                    self.insert_run(
                        database,
                        "medium111111",
                        "medium-project",
                        task_difficulty="中等",
                        difficulty_contract=contract,
                    )
                    self.insert_run(
                        database,
                        "hard11111111",
                        "hard-project",
                        task_difficulty="困难",
                        difficulty_contract=contract,
                    )

                candidate = app.auto_refill_iteration_candidate()
                recovery = app.auto_refill_iteration_candidate(
                    difficulty_recovery_only=True
                )
                state = app.iteration_lineage_state("medium111111")

        self.assertEqual(candidate["id"], "medium111111")
        self.assertEqual(recovery["id"], "medium111111")
        self.assertTrue(state["difficulty_recovery_required"])
        self.assertEqual(
            app.automatic_iteration_task_type(state), "Feature 迭代"
        )
        self.assertEqual(
            state["latest_product_difficulty_contract"]["hard_requirement"],
            "断点恢复后必须保持批次状态唯一",
        )

    def test_difficulty_recovery_runs_even_when_normal_refill_is_disabled(self):
        source = {
            "id": "medium111111",
            "repo_name": "medium-project",
            "iteration_count": 0,
            "difficulty_recovery_required": True,
            "latest_product_task_difficulty": "中等",
            "next_iteration_task_type": "Feature 迭代",
        }
        with mock.patch.object(
            app,
            "auto_refill_configuration",
            return_value={"enabled": False},
        ), mock.patch.object(
            app, "automatic_refill_occupancy", return_value=0
        ), mock.patch.object(
            app, "auto_refill_iteration_candidate", return_value=source
        ) as select, mock.patch.object(
            app,
            "queue_refill_iteration",
            return_value={"status": "generating", "task_type": "Feature 迭代"},
        ) as queue, mock.patch.object(app, "record_auto_refill_detail"):
            result = app.automatic_refill_once()

        select.assert_called_once_with(difficulty_recovery_only=True)
        queue.assert_called_once_with("medium111111")
        self.assertEqual(result["action"], "iteration")
        self.assertIn("难度止损", result["detail"])

    def test_refill_prefers_feature_iteration_then_falls_back_to_new_0_1(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root):
                app.initialize_database()
                app.set_auto_refill({"enabled": True, "project_directory": "team-a"})
                source = {"id": "root11111111", "repo_name": "root-one", "iteration_count": 2}
                with mock.patch.object(app, "automatic_refill_occupancy", return_value=1), mock.patch.object(
                    app, "auto_refill_iteration_candidate", return_value=source
                ), mock.patch.object(
                    app,
                    "queue_refill_iteration",
                    return_value={"status": "generating", "source_run_id": source["id"]},
                ) as queue:
                    iteration = app.automatic_refill_once()

                self.assertEqual(iteration["action"], "iteration")
                queue.assert_called_once_with(source["id"])

                created = {"id": "new01111111", "project_number": "0009"}
                with mock.patch.object(app, "automatic_refill_occupancy", return_value=1), mock.patch.object(
                    app, "auto_refill_iteration_candidate", return_value=None
                ), mock.patch.object(
                    app, "create_automatic_run", return_value=created
                ) as create, mock.patch.object(app, "add_event"):
                    new_task = app.automatic_refill_once()

                self.assertEqual(new_task["action"], "0-1")
                create.assert_called_once_with(
                    {"project_directory": "team-a", "_auto_refill": True},
                    allow_parallel_generation=True,
                )

    def test_refill_keeps_zero_to_one_generation_within_global_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker"):
                app.initialize_database()
                app.set_auto_refill({"enabled": True, "project_directory": "team-a"})
                for _ in range(app.TASK_GENERATION_MAX_PARALLEL):
                    app.create_automatic_run(
                        {"project_directory": "team-a", "_auto_refill": True},
                        allow_parallel_generation=True,
                    )
                with mock.patch.object(
                    app,
                    "automatic_refill_occupancy",
                    return_value=app.MAX_PARALLEL_RUNS - 1,
                ), mock.patch.object(
                    app, "auto_refill_iteration_candidate", return_value=None
                ), mock.patch.object(app, "create_automatic_run") as create:
                    result = app.automatic_refill_once()

        self.assertEqual(result["action"], "waiting_for_task_generation")
        self.assertEqual(result["count"], app.TASK_GENERATION_MAX_PARALLEL)
        create.assert_not_called()

    def test_refill_caps_zero_to_one_and_iteration_generation_together(self):
        with mock.patch.object(
            app, "auto_refill_configuration", return_value={"enabled": True}
        ), mock.patch.object(
            app, "automatic_refill_occupancy", return_value=2
        ), mock.patch.object(
            app,
            "active_task_generation_count",
            return_value=app.TASK_GENERATION_MAX_PARALLEL,
        ), mock.patch.object(
            app, "active_iteration_generation_count", return_value=0
        ), mock.patch.object(
            app, "auto_refill_iteration_candidate"
        ) as select:
            result = app.automatic_refill_once()

        self.assertEqual(result["action"], "waiting_for_task_generation")
        self.assertEqual(result["count"], app.TASK_GENERATION_MAX_PARALLEL)
        select.assert_not_called()

    def test_refill_generation_slot_race_does_not_fail_the_source(self):
        source = {
            "id": "root11111111",
            "repo_name": "root-one",
            "iteration_count": 1,
            "next_iteration_task_type": "Feature 迭代",
        }
        with mock.patch.object(
            app, "auto_refill_configuration", return_value={"enabled": True}
        ), mock.patch.object(
            app, "automatic_refill_occupancy", return_value=2
        ), mock.patch.object(
            app,
            "active_task_generation_count",
            side_effect=[2, app.TASK_GENERATION_MAX_PARALLEL],
        ), mock.patch.object(
            app, "active_iteration_generation_count", return_value=0
        ), mock.patch.object(
            app, "auto_refill_iteration_candidate", return_value=source
        ), mock.patch.object(
            app,
            "queue_refill_iteration",
            side_effect=app.WorkflowError(
                f"已有 {app.TASK_GENERATION_MAX_PARALLEL} 个题面正在生成，请等待生成槽"
            ),
        ), mock.patch.object(app, "put_iteration_job") as put:
            result = app.automatic_refill_once()

        self.assertEqual(result["action"], "waiting_for_task_generation")
        self.assertEqual(result["count"], app.TASK_GENERATION_MAX_PARALLEL)
        put.assert_not_called()

    def test_refill_uses_idle_slot_for_zero_to_one_when_iteration_generation_is_full(self):
        created = {"id": "new01111111", "project_number": "0009"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(
                app, "DB_PATH", root / "test.db"
            ), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(
                app, "PROJECTS_ROOT", root
            ), mock.patch.object(
                app, "auto_refill_configuration", return_value={
                    "enabled": True,
                    "project_directory": "team-a",
                }
            ), mock.patch.object(
                app,
                "automatic_refill_occupancy",
                return_value=app.ITERATION_GENERATION_MAX_PARALLEL,
            ), mock.patch.object(
                app,
                "active_task_generation_count",
                return_value=app.ITERATION_GENERATION_MAX_PARALLEL,
            ), mock.patch.object(
                app,
                "active_iteration_generation_count",
                return_value=app.ITERATION_GENERATION_MAX_PARALLEL,
            ), mock.patch.object(
                app, "auto_refill_new_project_backlog", return_value=0
            ), mock.patch.object(
                app, "auto_refill_iteration_candidate"
            ) as select, mock.patch.object(
                app, "create_automatic_run", return_value=created
            ) as create, mock.patch.object(
                app, "record_auto_refill_detail"
            ), mock.patch.object(app, "add_event"):
                app.initialize_database()
                result = app.automatic_refill_once()

        self.assertEqual(result["action"], "0-1")
        self.assertIn("迭代题面已满 3 路", result["detail"])
        select.assert_not_called()
        create.assert_called_once_with(
            {"project_directory": "team-a", "_auto_refill": True},
            allow_parallel_generation=True,
        )

    def test_refill_queue_uses_the_interleaved_task_type(self):
        source = {
            "id": "root11111111",
            "repo_name": "root-one",
            "iteration_count": 2,
            "new_module_count": 0,
            "last_iteration_task_type": "Feature 迭代",
        }
        with app.ITERATION_JOB_LOCK:
            app.ITERATION_JOBS.pop(source["id"], None)
        try:
            with mock.patch.object(
                app, "auto_refill_iteration_candidate", return_value=source
            ), mock.patch.object(
                app, "validate_iteration_lineage_type", return_value={}
            ), mock.patch.object(
                app,
                "latest_iteration_baseline_run_id",
                return_value="latest111111",
            ), mock.patch.object(
                app, "run_row", return_value={"id": "latest111111"}
            ), mock.patch.object(
                app, "iteration_project_context", return_value={"repo_path": "/tmp/demo"}
            ), mock.patch.object(app, "add_event") as event, mock.patch.object(
                app.threading, "Thread"
            ) as thread:
                job = app.queue_refill_iteration(source["id"])

            self.assertEqual(job["task_type"], "0-1 代码生成")
            event.assert_called_once_with(
                source["id"], "自动补题：开始生成第 3 轮 0-1 代码生成"
            )
            thread.assert_called_once_with(
                target=app.automatic_iteration_worker,
                args=(source["id"], "0-1 代码生成", False, True, 0, ""),
                daemon=True,
            )
        finally:
            with app.ITERATION_JOB_LOCK:
                app.ITERATION_JOBS.pop(source["id"], None)

    def test_auto_refill_pauses_only_after_three_consecutive_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root):
                app.initialize_database()
                app.set_auto_refill({"enabled": True, "project_directory": "team-a"})
                app.record_auto_refill_failure("来源 A 失败")
                app.record_auto_refill_failure("来源 B 失败")
                before_threshold = app.auto_refill_configuration()
                app.record_auto_refill_failure("来源 C 容器启动失败")
                configuration = app.auto_refill_configuration()

            self.assertTrue(before_threshold["enabled"])
            self.assertIn("失败任务", before_threshold["detail"])
            self.assertNotIn("失败来源", before_threshold["detail"])
            self.assertFalse(configuration["enabled"])
            self.assertIn("容器启动失败", configuration["error"])

    def test_candidate_quality_skip_keeps_auto_refill_running_and_resets_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root):
                app.initialize_database()
                app.set_auto_refill({"enabled": True, "project_directory": "team-a"})
                app.record_auto_refill_failure("来源 A 超时")
                app.record_auto_refill_failure("来源 B 连接失败")
                app.record_auto_refill_candidate_skip("来源 C 题面只有 283 字")
                configuration = app.auto_refill_configuration()
                values = app.settings_values(("auto_refill_consecutive_failures",))

            self.assertTrue(configuration["enabled"])
            self.assertIn("题面校验", configuration["detail"])
            self.assertIn("283 字", configuration["error"])
            self.assertEqual(values["auto_refill_consecutive_failures"], "0")

    def test_rule_c_saturation_prioritizes_a_new_project_over_old_iterations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root):
                app.initialize_database()
                app.set_auto_refill({"enabled": True, "project_directory": "team-a"})
                app.request_auto_refill_new_project("来源 A 命中同仓库规则 C")
                created = {"id": "new01111111", "project_number": "0009"}
                with mock.patch.object(
                    app, "automatic_refill_occupancy", return_value=1
                ), mock.patch.object(
                    app, "active_task_generation_count", return_value=0
                ), mock.patch.object(
                    app, "active_iteration_generation_count", return_value=0
                ), mock.patch.object(
                    app, "auto_refill_iteration_candidate"
                ) as select, mock.patch.object(
                    app, "create_automatic_run", return_value=created
                ) as create, mock.patch.object(app, "add_event"):
                    result = app.automatic_refill_once()

                self.assertEqual(result["action"], "0-1")
                self.assertIn("语义已饱和", result["detail"])
                select.assert_not_called()
                create.assert_called_once_with(
                    {"project_directory": "team-a", "_auto_refill": True},
                    allow_parallel_generation=True,
                )
                self.assertEqual(app.auto_refill_new_project_backlog(), 0)

    def test_reenabling_auto_refill_starts_a_fresh_failure_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root):
                app.initialize_database()
                app.set_auto_refill({"enabled": True, "project_directory": "team-a"})
                app.record_auto_refill_failure("旧失败 A")
                app.record_auto_refill_failure("旧失败 B")
                app.set_auto_refill({"enabled": True, "project_directory": "team-a"})
                app.record_auto_refill_failure("重新开启后的第一次失败")
                configuration = app.auto_refill_configuration()

            self.assertTrue(configuration["enabled"])
            self.assertIn("连续 1/3", configuration["detail"])

    def test_inflight_results_do_not_overwrite_an_automatic_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root):
                app.initialize_database()
                app.set_auto_refill({"enabled": True, "project_directory": "team-a"})
                app.record_auto_refill_failure("来源 A 失败")
                app.record_auto_refill_failure("来源 B 失败")
                app.record_auto_refill_failure("来源 C 失败")
                paused = app.auto_refill_configuration()

                app.record_auto_refill_failure("暂停前已在途的来源 D 失败")
                app.record_auto_refill_success()
                app.record_auto_refill_detail("暂停前已在途任务后来完成")
                final = app.auto_refill_configuration()
                values = app.settings_values(("auto_refill_consecutive_failures",))

            self.assertFalse(paused["enabled"])
            self.assertEqual(final, paused)
            self.assertEqual(values["auto_refill_consecutive_failures"], "3")
            self.assertIn("来源 C 失败", final["error"])
            self.assertNotIn("来源 D", final["error"])

    def test_page_exposes_auto_refill_toggle_and_policy(self):
        html = (app.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        javascript = (app.STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="auto-refill-toggle"', html)
        self.assertIn('id="auto-refill-hours"', html)
        self.assertIn('id="auto-refill-schedule"', html)
        self.assertIn('id="auto-refill-clear"', html)
        self.assertIn('id="auto-refill-start-hours"', html)
        self.assertIn('id="auto-refill-start-schedule"', html)
        self.assertIn('id="auto-refill-start-clear"', html)
        self.assertIn('id="auto-refill-start-controls"', html)
        self.assertIn('id="auto-refill-stop-controls"', html)
        self.assertIn("async function toggleAutoRefill()", javascript)
        self.assertIn("async function setAutoRefillStartSchedule()", javascript)
        self.assertIn("async function clearAutoRefillStartSchedule()", javascript)
        self.assertIn("async function setAutoRefillSchedule()", javascript)
        self.assertIn("async function clearAutoRefillSchedule()", javascript)
        self.assertIn("enable_after_hours", javascript)
        self.assertIn("disable_after_hours", javascript)
        self.assertIn("Bug 修复", javascript)
        self.assertIn("Feature、Bug 修复和完整模块", html)
        self.assertIn("现有问题整理（Bug 修复）", javascript)
        self.assertIn("/api/settings/auto-refill", javascript)


class IterationGenerationTests(unittest.TestCase):
    def candidate(self):
        return {
            "task_type": "Feature 迭代",
            "prompt": (
                "值班人员已经能在现有系统中完成样本交接，但批次需要隔离、复核或放行时，决定仍散落在口头沟通里，下一班难以确认处置依据和责任人。请在现有批次详情中加入处置决策能力，数据库保存处置申请、证据引用、审核结论和生效版本，服务层只允许存在未解决异常或暴露超限的批次进入处置流程。API 提供发起处置和提交审核两个入口，并用业务幂等键避免重复写入；页面展示当前结论、待办动作和证据摘要，断网时保留输入但不提前改变状态。规则、证据或审核结果变化后应重新计算当前结论，只保留仍与新结果完全对应的确认，若确认失效则在页面说明对应批次和原因。已有交接、撤销、过期及重开记录不得被改写，处置生效后还要拦截与结论冲突的新交接，并让服务端错误刷新后仍能得到一致的当前位置和责任链。补充数据库迁移、服务层与接口测试、前端错误映射和一条浏览器主流程，验收异常批次发起隔离、证据不足被拒绝、复核通过后放行以及冲突交接被拦截。"
            ),
            "expansion_axis": "批次异常处置与责任链",
            "engineering_core": "批次处置决策闭环",
            "main_user_flow": "值班人员发起处置，审核人确认后形成当前结论",
            "modules": ["数据与领域状态", "FastAPI 接口", "React 页面", "自动化测试"],
            "new_runtime_components": [],
            "complex_mechanisms": ["处置版本状态机"],
            "api_or_actions": ["发起处置", "提交审核"],
            "new_state_sets": ["处置状态"],
            "acceptance_scenarios": [
                "异常批次发起隔离",
                "双人复核后放行",
                "冲突交接被服务端阻止",
            ],
        }

    def new_module_candidate(self):
        candidate = self.candidate()
        candidate.update(
            {
                "task_type": "0-1 代码生成",
                "modules": ["数据库与迁移", "领域服务", "FastAPI 接口", "自动化测试"],
                "expansion_axis": "独立批次处置决策模块",
                "engineering_core": "批次处置决策闭环",
                "new_runtime_components": [],
                "complex_mechanisms": ["不可覆盖的处置版本状态机"],
                "acceptance_scenarios": [
                    "异常批次发起隔离",
                    "证据不足时返回可定位错误",
                    "复核通过后形成完整责任链",
                ],
            }
        )
        return candidate

    def bugfix_candidate(self):
        return {
            "task_type": "Bug 修复",
            "focus_area": "样本交接确认",
            "main_user_flow": "接收人输入接收码并确认样本位置",
            "scope_summary": "样本交接确认集中在接收人输入接收码、查看交接状态并确认样本位置的流程",
            "modules": ["交接服务", "接收页面"],
            "confirmed_bugs": [
                {
                    "title": "重复确认会移动两次",
                    "reproduction": "两人同时提交同一个接收码",
                    "actual": "容器位置更新两次",
                    "expected": "容器只移动一次且无重复记录",
                    "evidence": "两个请求都返回成功且时间线增加两条记录",
                    "estimated_fix_scope": "中",
                    "customer_summary": "两人同时确认会让容器移动两次，正确结果只能移动一次",
                },
                {
                    "title": "过期接收码仍可使用",
                    "reproduction": "等待交接过期后提交原接收码",
                    "actual": "系统仍然完成接收",
                    "expected": "提示交接已过期并保持原位置",
                    "evidence": "过期后接口返回成功且位置发生变化",
                    "estimated_fix_scope": "小",
                    "customer_summary": "交接超时后旧接收码仍能使用，应该提示过期并保持原位置",
                },
                {
                    "title": "撤销后详情没有刷新",
                    "reproduction": "发起人撤销交接后接收人刷新详情",
                    "actual": "页面仍显示可以接收",
                    "expected": "页面显示交接已撤销",
                    "evidence": "接口已返回撤销状态但页面仍保留接收按钮",
                    "estimated_fix_scope": "小",
                    "customer_summary": "交接撤销后详情页仍显示可以接收，刷新后应该显示已撤销",
                },
                {
                    "title": "断网重试丢失接收码",
                    "reproduction": "确认操作时断网后恢复网络连接",
                    "actual": "接收码输入内容被清空",
                    "expected": "保留接收码供用户重试",
                    "evidence": "模拟网络失败后输入框内容为空",
                    "estimated_fix_scope": "小",
                    "customer_summary": "确认时断网会清空已经输入的接收码，恢复网络后应该可以直接重试",
                },
            ],
        }

    def review_result(self, task_type="Feature 迭代", approved=True, reasons=None):
        candidate = (
            self.new_module_candidate()
            if task_type == "0-1 代码生成"
            else self.candidate()
        )
        return {
            "approved": approved,
            "reasons": list(reasons or []),
            "task_type": task_type,
            "estimated_task_difficulty": "困难",
            "difficulty_evidence": ["处置版本状态机需要跨层维护生效不变量"],
            "scope_review": {
                "engineering_core_count": 1,
                "modules": candidate["modules"],
                "complex_mechanisms": candidate["complex_mechanisms"],
                "api_or_actions": candidate["api_or_actions"],
                "new_state_sets": candidate["new_state_sets"],
                "new_runtime_components": candidate["new_runtime_components"],
                "acceptance_scenarios": candidate["acceptance_scenarios"],
                "history_overlap": False,
                "overlapping_sequences": [],
                "ai_style_issues": [],
            },
        }

    def test_iteration_prompt_is_normalized_and_requires_cross_module_scope(self):
        candidate = self.candidate()
        candidate["prompt"] = candidate["prompt"].replace("：", "：\n", 1)
        prompt = app.validate_generated_iteration(candidate)

        self.assertNotIn("\n", prompt)
        self.assertIn("数据库保存", prompt)

        candidate = self.candidate()
        candidate["modules"] = ["API", "API", "测试"]
        with self.assertRaisesRegex(app.WorkflowError, "至少三个"):
            app.validate_generated_iteration(candidate)

    def test_iteration_prompt_does_not_expose_internal_scope_filters(self):
        for forbidden in ("高并发", "需要多天验证", "无需长周期观察"):
            candidate = self.candidate()
            candidate["prompt"] += forbidden
            with self.subTest(forbidden=forbidden), self.assertRaisesRegex(
                app.WorkflowError, "内部范围限制"
            ):
                app.validate_generated_iteration(candidate)

    def test_iteration_prompt_naturalness_is_hard_validated(self):
        candidate = self.candidate()
        candidate["prompt"] = candidate["prompt"].replace("，", "；", 3)
        with self.assertRaisesRegex(app.WorkflowError, "分号最多"):
            app.validate_generated_iteration(candidate)

        candidate = self.candidate()
        candidate["prompt"] = candidate["prompt"].replace("。", "，", 2)
        with self.assertRaisesRegex(app.WorkflowError, "单句最多"):
            app.validate_generated_iteration(candidate)

        candidate = self.candidate()
        candidate["prompt"] = candidate["prompt"].replace(
            "已有交接", "沿用既有不变量，已有交接"
        )
        with self.assertRaisesRegex(app.WorkflowError, "模板化表达"):
            app.validate_generated_iteration(candidate)

    def test_iteration_actions_states_and_structured_history_are_hard_validated(self):
        candidate = self.candidate()
        candidate["api_or_actions"].append("撤回处置")
        with self.assertRaisesRegex(app.WorkflowError, "新增接口或用户操作最多为 2 项"):
            app.validate_generated_iteration(candidate)

        candidate = self.candidate()
        candidate["new_state_sets"].append("复核状态")
        with self.assertRaisesRegex(app.WorkflowError, "新增状态集合最多为 1 项"):
            app.validate_generated_iteration(candidate)

        candidate = self.candidate()
        with self.assertRaisesRegex(app.WorkflowError, "扩展方向与历史第 1 轮重复"):
            app.validate_generated_iteration(
                candidate,
                iteration_history=[
                    {
                        "sequence": 1,
                        "expansion_axis": candidate["expansion_axis"],
                        "engineering_core": "另一个核心",
                        "prompt": "完全不同的旧题面",
                    }
                ],
            )

    def test_abandoned_history_is_not_treated_as_implemented_code(self):
        candidate = self.candidate()
        abandoned = {
            "sequence": 1,
            "counts_toward_quota": False,
            "outcome": "abandoned",
            "expansion_axis": candidate["expansion_axis"],
            "engineering_core": candidate["engineering_core"],
            "modules": candidate["modules"],
            "main_user_flow": candidate["main_user_flow"],
            "prompt": "此前失败的题面只是一条未落地记录，与本次正文并不相同。",
        }
        self.assertEqual(
            app.validate_generated_iteration(
                candidate, iteration_history=[abandoned]
            ),
            candidate["prompt"],
        )

        abandoned["prompt"] = candidate["prompt"]
        with self.assertRaisesRegex(app.WorkflowError, "历史第 1 轮过于相似"):
            app.validate_generated_iteration(
                candidate, iteration_history=[abandoned]
            )

    def test_repository_history_blocks_cross_session_repeat(self):
        candidate = self.candidate()
        repository_history = [{
            "reference": "SOLO-QA #8075",
            "source": "solo_qa",
            "prompt": candidate["prompt"],
            "task_type": "Feature 迭代",
            "dedup_required": True,
        }]
        with self.assertRaisesRegex(app.WorkflowError, "SOLO-QA #8075"):
            app.validate_generated_iteration(
                candidate,
                iteration_history=[],
                repository_history=repository_history,
            )

    def test_repository_history_includes_every_local_turn_and_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    for run_id, repo_name in (
                        ("older1111111", "shared-repo"),
                        ("target111111", "shared-repo"),
                    ):
                        database.execute(
                            """INSERT INTO runs(
                                 id, repo_name, repo_path, run_directory, repo_url,
                                 phase, first_prompt, first_prompt_id,
                                 container_cleaned, task_type,
                                 verification_commands, created_at, updated_at
                               ) VALUES (?, ?, ?, ?, ?, 'complete', ?, 'prompt-1',
                                         1, '0-1 代码生成', '[]', ?, ?)""",
                            (
                                run_id,
                                repo_name,
                                str(root / run_id / "workspace"),
                                str(root / run_id),
                                "https://github.com/example/shared-repo.git",
                                "首轮项目需求",
                                timestamp,
                                timestamp,
                            ),
                        )
                    database.execute(
                        """INSERT INTO run_turns(
                             run_id, turn_number, intent_type, prompt, prompt_id,
                             status, created_at, updated_at
                           ) VALUES ('older1111111', 1, '0-1 代码生成',
                                   '首轮项目需求', 'prompt-1', 'complete', ?, ?)""",
                        (timestamp, timestamp),
                    )
                    database.execute(
                        """INSERT INTO run_turns(
                             run_id, turn_number, intent_type, prompt, prompt_id,
                             status, created_at, updated_at
                           ) VALUES ('older1111111', 2, 'Bug 修复',
                                   '极大有限坐标换算后结果为空，应保留有效落点。',
                                   'prompt-2', 'complete', ?, ?)""",
                        (timestamp, timestamp),
                    )
                    database.execute(
                        """INSERT INTO run_turns(
                             run_id, turn_number, intent_type, prompt, prompt_id,
                             status, created_at, updated_at
                           ) VALUES ('target111111', 1, '0-1 代码生成',
                                   '另一个首轮项目需求', 'prompt-target', 'complete', ?, ?)""",
                        (timestamp, timestamp),
                    )
                    database.execute(
                        """INSERT INTO solo_qa_submissions(
                             run_id, turn_number, remote_submission_id,
                             remote_status, state, created_at, updated_at
                           ) VALUES ('older1111111', 2, '13653',
                                   'QC_PASSED', 'qc_passed', ?, ?)""",
                        (timestamp, timestamp),
                    )
                history = app.repository_prompt_history(
                    app.run_row("target111111")
                )
                global_history = app.global_prompt_dedup_history(
                    "example/another-repo",
                    {
                        "task_type": "Bug 修复",
                        "confirmed_bugs": [{
                            "customer_summary": (
                                "极大有限坐标换算后结果为空，应保留有效落点。"
                            ),
                        }],
                    },
                    "Bug 修复",
                )

        second_turn = next(
            item for item in history
            if item.get("reference") == "SOLO-QA #13653"
        )
        self.assertEqual(second_turn["turn_number"], 2)
        self.assertEqual(second_turn["task_type"], "Bug 修复")
        self.assertIn("极大有限坐标", second_turn["prompt"])
        self.assertEqual(global_history[0]["reference"], "本地任务 older111")

    def test_global_bug_guard_blocks_only_near_verbatim_cross_repo_issue(self):
        history = [{
            "reference": "SOLO-QA #10927",
            "task_type": "Bug 修复",
            "prompt": "同时构建接口和验收服务时镜像名称冲突，容器构建失败且一次性验收无法运行。",
            "dedup_required": True,
        }]
        duplicate = {
            "task_type": "Bug 修复",
            "confirmed_bugs": [{
                "customer_summary": "同时构建接口和验收服务时镜像冲突，整个 Docker 构建失败，验收服务应能正常运行",
            }],
        }
        different = {
            "task_type": "Bug 修复",
            "confirmed_bugs": [{
                "customer_summary": "导入含空白编号的批次后页面排序错乱，正确结果应保留有效编号并提示无效行",
            }],
        }

        self.assertIn(
            "SOLO-QA #10927",
            app.cross_repository_bug_duplicate_reason(
                duplicate, "Bug 修复", history
            ),
        )
        self.assertEqual(
            app.cross_repository_bug_duplicate_reason(
                different, "Bug 修复", history
            ),
            "",
        )

    def test_global_prompt_history_shortlists_other_repository_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO solo_qa_prompt_history(
                               remote_submission_id, repo_key, repo_name, repo_url,
                               prompt, task_type, remote_status, qc_summary,
                               submitted_at, last_synced_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            "10927", "example/container-code-check-gate",
                            "container-code-check-gate", "",
                            "同时构建接口和验收服务时镜像名称冲突，容器构建失败且一次性验收无法运行。",
                            "Bug 修复", "QC_PASSED", "", "2026-09-01", "2026-09-15",
                        ),
                    )
                result = app.global_prompt_dedup_history(
                    "example/saddle-stitch-signature-imposer",
                    {
                        "task_type": "Bug 修复",
                        "confirmed_bugs": [{
                            "customer_summary": "同时构建接口和验收服务时镜像冲突，整个 Docker 构建失败，验收服务应能正常运行",
                        }],
                    },
                    "Bug 修复",
                )

        self.assertEqual(result[0]["reference"], "SOLO-QA #10927")
        self.assertFalse(result[0]["same_repository"])
        self.assertGreater(result[0]["lexical_similarity"], 0.6)

    def test_semantic_dedup_blocks_only_high_confidence_matches(self):
        high = {
            "duplicate": True,
            "confidence": "high",
            "match_scope": "cross_repository",
            "reference": "SOLO-QA #10927",
            "overlap_kind": "same_bug",
            "reason": "触发、镜像冲突和验收服务无法启动均相同",
        }
        medium = {**high, "confidence": "medium"}
        same_repo_medium = {
            **medium,
            "match_scope": "same_repository",
            "reference": "SOLO-QA #10873",
        }

        self.assertIn("SOLO-QA #10927", app.prompt_dedup_review_reason(high))
        self.assertEqual(app.prompt_dedup_review_reason(medium), "")
        self.assertIn(
            "SOLO-QA #10873",
            app.prompt_dedup_review_reason(same_repo_medium),
        )

    def test_semantic_dedup_always_reviews_eligible_same_repository_history(self):
        unrelated_candidate = {
            "prompt": "为新的库存盘点入口增加离线差异复核。",
        }
        repository_history = [{
            "reference": "SOLO-QA #10873",
            "prompt": "在既有排程页面增加打印批次预览。",
            "dedup_required": True,
        }]

        self.assertTrue(
            app.semantic_dedup_review_needed(
                unrelated_candidate,
                [],
                repository_history,
            )
        )
        self.assertFalse(
            app.semantic_dedup_review_needed(
                unrelated_candidate,
                [],
                [{**repository_history[0], "dedup_required": False}],
            )
        )

    def test_semantic_dedup_prompt_is_conservative_for_cross_repo_history(self):
        review = {
            "duplicate": False,
            "confidence": "low",
            "match_scope": "none",
            "reference": "",
            "overlap_kind": "none",
            "reason": "没有高置信度重复",
        }
        with mock.patch.object(
            app, "run_codex_structured", return_value=review
        ) as codex:
            result = app.run_codex_prompt_dedup_validation(
                self.candidate(), "Feature 迭代", [], []
            )

        self.assertEqual(result, review)
        prompt = codex.call_args.args[0]
        self.assertIn("跨仓库必须更保守", prompt)
        self.assertIn("通用技术词不构成重复", prompt)
        self.assertIn("同一主接口、同一页面流程或同一状态机", prompt)
        self.assertIn("开发动作、输入校验、失败结果和验收结构", prompt)
        self.assertEqual(codex.call_args.kwargs["reasoning_effort"], "medium")

    def test_prompt_context_is_bounded_and_history_is_ranked_for_review(self):
        candidate = self.candidate()
        history = [
            {
                "reference": f"SOLO-QA #{number}",
                "prompt": f"完全不同的历史流程 {number}。" * 80,
                "task_type": "Feature 迭代",
                "dedup_required": True,
            }
            for number in range(20)
        ]
        history[-1]["prompt"] = candidate["prompt"]
        context = {
            "repo_path": "/tmp/example",
            "readme": "说明" * 9000,
            "original_or_current_prompt": "原题" * 5000,
            "tracked_files": [f"src/{number}.py" for number in range(220)],
            "iteration_history": history,
            "repository_prompt_history": history,
        }

        compact = app.compact_iteration_prompt_context(
            context,
            candidate,
            repository_limit=app.ITERATION_REVIEW_HISTORY_LIMIT,
        )

        self.assertLessEqual(len(compact["readme"]), 8000)
        self.assertLessEqual(len(compact["original_or_current_prompt"]), 6000)
        self.assertEqual(len(compact["tracked_files"]), 160)
        self.assertEqual(
            len(compact["repository_prompt_history"]),
            app.ITERATION_REVIEW_HISTORY_LIMIT,
        )
        self.assertEqual(
            compact["repository_prompt_history"][0]["reference"],
            "SOLO-QA #19",
        )
        self.assertLessEqual(
            len(compact["repository_prompt_history"][0]["prompt"]), 700
        )

    def test_low_risk_cross_repo_without_same_repo_history_skips_semantic_call(self):
        candidate = self.candidate()
        context = {
            "repo_path": "/tmp/existing-project",
            "repo_name": "demo",
            "repo_key": "example/demo",
            "repository_prompt_history": [],
        }
        source = {
            "phase": "complete",
            "container_cleaned": 1,
            "repo_url": "https://example.invalid/demo",
            "first_prompt_id": "prompt-1",
        }
        low_risk = [{
            "reference": "SOLO-QA #77",
            "prompt": "一本书的目录页面需要增加只读页码展示。",
            "task_type": "Feature 迭代",
            "lexical_similarity": 0.08,
            "bigram_containment": 0.05,
            "dedup_required": True,
        }]
        with mock.patch.object(app, "run_row", return_value=source), mock.patch.object(
            app, "iteration_project_context", return_value=context
        ), mock.patch.object(
            app, "run_codex_iteration_generation", return_value=candidate
        ), mock.patch.object(
            app, "global_prompt_dedup_history", return_value=low_risk
        ), mock.patch.object(
            app, "run_codex_iteration_validation", return_value=self.review_result()
        ), mock.patch.object(
            app, "run_codex_prompt_dedup_validation"
        ) as dedup:
            result = app.generate_iteration_candidate("source111111")

        self.assertEqual(result["prompt"], candidate["prompt"])
        dedup.assert_not_called()

    def test_style_only_review_uses_local_repair_without_full_rereview(self):
        candidate = self.candidate()
        review = self.review_result()
        review["scope_review"]["ai_style_issues"] = ["结尾集中罗列测试清单"]
        context = {"repo_path": "/tmp/existing-project", "repo_name": "demo"}
        source = {
            "phase": "complete",
            "container_cleaned": 1,
            "repo_url": "https://example.invalid/demo",
            "first_prompt_id": "prompt-1",
        }
        with mock.patch.object(app, "run_row", return_value=source), mock.patch.object(
            app, "iteration_project_context", return_value=context
        ), mock.patch.object(
            app, "run_codex_iteration_generation", return_value=candidate
        ) as generate, mock.patch.object(
            app, "run_codex_iteration_validation", return_value=review
        ) as validate, mock.patch.object(
            app, "run_codex_iteration_format_repair", return_value=candidate
        ) as repair:
            result = app.generate_iteration_candidate("source111111")

        self.assertEqual(result["prompt"], candidate["prompt"])
        generate.assert_called_once()
        validate.assert_called_once()
        repair.assert_called_once()

    def test_repository_key_matches_fallback_without_cross_owner_collision(self):
        self.assertEqual(
            app.canonical_repository_key(
                "git@github.com:WanFeng/demo-repo.git", ""
            ),
            "wanfeng/demo-repo",
        )
        self.assertTrue(app.repository_keys_match("wanfeng/demo-repo", "demo-repo"))
        self.assertFalse(
            app.repository_keys_match("wanfeng/demo-repo", "someone/demo-repo")
        )

    def test_iteration_generation_is_fixed_to_gpt_5_6_sol(self):
        context = {"repo_path": "/tmp/existing-project", "repo_name": "demo"}
        with mock.patch.object(
            app, "run_codex_structured", return_value=self.candidate()
        ) as codex:
            result = app.run_codex_iteration_generation(context)

        self.assertEqual(result, self.candidate())
        self.assertEqual(codex.call_args.args[2], Path("/tmp/existing-project"))
        self.assertEqual(codex.call_args.kwargs["model"], "gpt-5.6-sol")
        self.assertIn(app.DEVELOPER_PROMPT_STYLE_GUIDANCE, codex.call_args.args[0])
        self.assertIn(app.GENERATION_DIFFICULTY_AXIS_GUIDANCE, codex.call_args.args[0])
        self.assertIn("repository_prompt_history", codex.call_args.args[0])

    def test_medium_delivery_recovery_prompt_keeps_project_and_changes_axis(self):
        context = {
            "repo_path": "/tmp/existing-project",
            "repo_name": "demo",
            "difficulty_recovery": {
                "required": True,
                "previous_actual_difficulty": "中等",
                "previous_contract": {
                    "hard_requirement": "断点恢复必须保持状态唯一",
                    "rejected_shortcut": "单一完成标记无法恢复分段状态",
                },
            },
        }
        with mock.patch.object(
            app, "run_codex_structured", return_value=self.candidate()
        ) as codex:
            app.run_codex_iteration_generation(context)

        generation_prompt = codex.call_args.args[0]
        self.assertIn("代码成果必须继续作为本轮基线", generation_prompt)
        self.assertIn("不得废弃或重新出同一道题", generation_prompt)
        self.assertIn("与上一题不同的 Feature 扩展轴", generation_prompt)

    def test_new_module_generation_schema_has_scope_caps(self):
        context = {"repo_path": "/tmp/existing-project", "repo_name": "demo"}
        candidate = self.new_module_candidate()
        with mock.patch.object(
            app, "run_codex_structured", return_value=candidate
        ) as codex:
            result = app.run_codex_iteration_generation(
                context, target_task_type="0-1 代码生成"
            )

        self.assertEqual(result, candidate)
        generation_prompt, schema = codex.call_args.args[:2]
        self.assertEqual(schema["properties"]["modules"]["maxItems"], 4)
        self.assertEqual(
            schema["properties"]["new_runtime_components"]["maxItems"], 1
        )
        self.assertEqual(schema["properties"]["complex_mechanisms"]["maxItems"], 1)
        self.assertEqual(schema["properties"]["acceptance_scenarios"]["minItems"], 3)
        self.assertEqual(schema["properties"]["acceptance_scenarios"]["maxItems"], 4)
        self.assertIn("engineering_core", schema["required"])
        self.assertEqual(schema["properties"]["api_or_actions"]["maxItems"], 2)
        self.assertEqual(schema["properties"]["new_state_sets"]["maxItems"], 1)
        self.assertIn("整个需求只能围绕一个工程核心", generation_prompt)
        self.assertIn("300 至 480", generation_prompt)

    def test_feature_generation_receives_history_and_uses_small_scope_budget(self):
        context = {
            "repo_path": "/tmp/existing-project",
            "repo_name": "demo",
            "iteration_history": [
                {
                    "sequence": 0,
                    "task_type": "0-1 代码生成",
                    "prompt": "原始项目题面",
                },
                {
                    "sequence": 1,
                    "task_type": "Feature 迭代",
                    "prompt": "已有的第一轮扩展题面",
                },
            ],
        }
        with mock.patch.object(
            app, "run_codex_structured", return_value=self.candidate()
        ) as codex:
            app.run_codex_iteration_generation(context)

        generation_prompt, schema = codex.call_args.args[:2]
        self.assertIn("已有的第一轮扩展题面", generation_prompt)
        self.assertIn("iteration_history", generation_prompt)
        self.assertEqual(schema["properties"]["modules"]["maxItems"], 4)
        self.assertEqual(
            schema["properties"]["new_runtime_components"]["maxItems"], 0
        )
        self.assertEqual(schema["properties"]["complex_mechanisms"]["maxItems"], 1)
        self.assertEqual(schema["properties"]["acceptance_scenarios"]["minItems"], 3)
        self.assertEqual(schema["properties"]["acceptance_scenarios"]["maxItems"], 4)
        self.assertIn("engineering_core", schema["required"])
        self.assertIn("300 至 480", generation_prompt)

    def test_feature_scope_and_history_similarity_are_hard_validated(self):
        candidate = self.candidate()
        candidate["modules"].append("额外 worker")
        with self.assertRaisesRegex(app.WorkflowError, "最多涉及 4 个"):
            app.validate_generated_iteration(candidate)

        candidate = self.candidate()
        candidate["new_runtime_components"] = ["独立通知 worker"]
        with self.assertRaisesRegex(app.WorkflowError, "新增独立运行组件最多为 0 项"):
            app.validate_generated_iteration(candidate)

        candidate = self.candidate()
        candidate["acceptance_scenarios"].extend(["额外场景一", "额外场景二"])
        with self.assertRaisesRegex(app.WorkflowError, "验收场景必须为 3 至 4 项"):
            app.validate_generated_iteration(candidate)

        candidate = self.candidate()
        with self.assertRaisesRegex(app.WorkflowError, "历史第 2 轮过于相似"):
            app.validate_generated_iteration(
                candidate,
                iteration_history=[
                    {
                        "sequence": 2,
                        "task_type": "Feature 迭代",
                        "prompt": candidate["prompt"],
                    }
                ],
            )

    def test_new_module_scope_is_hard_validated(self):
        candidate = self.new_module_candidate()
        self.assertEqual(
            app.validate_generated_iteration(candidate, "0-1 代码生成"),
            candidate["prompt"],
        )

        candidate = self.new_module_candidate()
        candidate["modules"].append("独立 worker")
        with self.assertRaisesRegex(app.WorkflowError, "最多涉及 4 个"):
            app.validate_generated_iteration(candidate, "0-1 代码生成")

        candidate = self.new_module_candidate()
        candidate["new_runtime_components"] = ["导出 worker", "清理 worker"]
        with self.assertRaisesRegex(app.WorkflowError, "独立运行组件最多为 1 项"):
            app.validate_generated_iteration(candidate, "0-1 代码生成")

        candidate = self.new_module_candidate()
        candidate["new_runtime_components"] = ["导出 worker"]
        with self.assertRaisesRegex(app.WorkflowError, "不能同时新增"):
            app.validate_generated_iteration(candidate, "0-1 代码生成")

        candidate = self.new_module_candidate()
        candidate["complex_mechanisms"] = ["密码学证明", "确定性归档"]
        with self.assertRaisesRegex(app.WorkflowError, "复杂机制最多为 1 项"):
            app.validate_generated_iteration(candidate, "0-1 代码生成")

        candidate = self.new_module_candidate()
        candidate["acceptance_scenarios"].extend(["失败重试", "崩溃回收"])
        with self.assertRaisesRegex(app.WorkflowError, "验收场景必须为 3 至 4 项"):
            app.validate_generated_iteration(candidate, "0-1 代码生成")

        candidate = self.new_module_candidate()
        candidate["prompt"] += "继续增加额外功能。" * 20
        with self.assertRaisesRegex(app.WorkflowError, "300 至 480"):
            app.validate_generated_iteration(candidate, "0-1 代码生成")

    def test_iteration_review_rejects_mechanical_ai_style(self):
        context = {"repo_path": "/tmp/existing-project", "repo_name": "demo"}
        with mock.patch.object(
            app,
            "run_codex_structured",
            return_value=self.review_result(),
        ) as codex:
            app.run_codex_iteration_validation(context, self.candidate())

        review_prompt, schema = codex.call_args.args[:2]
        self.assertIn(app.DEVELOPER_PROMPT_STYLE_GUIDANCE, review_prompt)
        self.assertIn("这属于可局部修复的表达问题", review_prompt)
        self.assertIn("不要仅因此令 approved=false", review_prompt)
        self.assertIn("不运行完整测试套件", review_prompt)
        self.assertIn("estimated_task_difficulty", schema["properties"])
        self.assertIn("difficulty_evidence", schema["properties"])
        self.assertIn("difficulty_contract", schema["properties"])
        self.assertIn("只有预估为困难或地狱才可 approved=true", review_prompt)
        self.assertIn(app.REVIEW_DIFFICULTY_AXIS_GUIDANCE, review_prompt)
        self.assertIn(app.DIFFICULTY_CONTRACT_REVIEW_GUIDANCE, review_prompt)
        self.assertEqual(
            codex.call_args.args[4], app.ITERATION_VALIDATION_TIMEOUT_SECONDS
        )
        self.assertEqual(codex.call_args.kwargs["reasoning_effort"], "medium")

    def test_new_module_review_rejects_combined_complex_mechanisms(self):
        context = {"repo_path": "/tmp/existing-project", "repo_name": "demo"}
        with mock.patch.object(
            app,
            "run_codex_structured",
            return_value=self.review_result(
                "0-1 代码生成", False, ["叠加了多个复杂机制"]
            ),
        ) as codex:
            app.run_codex_iteration_validation(
                context,
                self.new_module_candidate(),
                target_task_type="0-1 代码生成",
            )

        review_prompt = codex.call_args.args[0]
        self.assertIn("最多一个新增独立运行组件和一项复杂机制", review_prompt)
        self.assertIn("即使被合并写成一个字段", review_prompt)

    def test_review_scope_rejects_underreported_large_requirement(self):
        review = self.review_result()
        review["scope_review"]["api_or_actions"].append("撤回处置")

        errors = app.iteration_review_scope_errors(review, "Feature 迭代")

        self.assertTrue(any("超过 2 项" in error for error in errors))

    def test_iteration_generation_retries_invalid_candidate_before_review(self):
        invalid = self.candidate()
        invalid["prompt"] += "需要高并发压测"
        context = {"repo_path": "/tmp/existing-project", "repo_name": "demo"}
        source = {
            "phase": "complete",
            "container_cleaned": 1,
            "repo_url": "https://example.invalid/demo",
            "first_prompt_id": "prompt-1",
        }
        with mock.patch.object(app, "run_row", return_value=source), mock.patch.object(
            app, "iteration_project_context", return_value=context
        ), mock.patch.object(
            app, "run_codex_iteration_generation", side_effect=[invalid, self.candidate()]
        ) as generate, mock.patch.object(
            app, "run_codex_iteration_format_repair"
        ) as repair, mock.patch.object(
            app,
            "run_codex_iteration_validation",
            return_value=self.review_result(),
        ) as review:
            prompt = app.generate_iteration_prompt("source111111")

        self.assertEqual(prompt, self.candidate()["prompt"])
        self.assertEqual(generate.call_count, 2)
        repair.assert_not_called()
        review.assert_called_once()
        self.assertIn("内部范围限制", generate.call_args_list[1].args[1])

    def test_iteration_generation_retries_high_confidence_semantic_duplicate(self):
        candidate = self.candidate()
        context = {
            "repo_path": "/tmp/existing-project",
            "repo_name": "demo",
            "repo_key": "example/demo",
            "repository_prompt_history": [{
                "reference": "SOLO-QA #10",
                "prompt": "仓库最初只实现样本登记和基础详情查看。",
                "task_type": "0-1 代码生成",
                "dedup_required": True,
            }],
        }
        source = {
            "phase": "complete",
            "container_cleaned": 1,
            "repo_url": "https://example.invalid/demo",
            "first_prompt_id": "prompt-1",
        }
        duplicate = {
            "duplicate": True,
            "confidence": "high",
            "match_scope": "cross_repository",
            "reference": "SOLO-QA #88",
            "overlap_kind": "same_feature",
            "reason": "用户操作、状态变化和验收结果均相同",
        }
        distinct = {
            "duplicate": False,
            "confidence": "low",
            "match_scope": "none",
            "reference": "",
            "overlap_kind": "none",
            "reason": "没有高置信度重复",
        }
        with mock.patch.object(app, "run_row", return_value=source), mock.patch.object(
            app, "iteration_project_context", return_value=context
        ), mock.patch.object(
            app, "run_codex_iteration_generation", return_value=candidate
        ) as generate, mock.patch.object(
            app,
            "global_prompt_dedup_history",
            return_value=[{
                "reference": "SOLO-QA #88",
                "prompt": candidate["prompt"],
                "task_type": "Feature 迭代",
                "lexical_similarity": 0.91,
                "bigram_containment": 0.86,
                "dedup_required": True,
            }],
        ), mock.patch.object(
            app, "run_codex_iteration_validation", return_value=self.review_result()
        ), mock.patch.object(
            app,
            "run_codex_prompt_dedup_validation",
            side_effect=[duplicate, distinct],
        ) as dedup:
            result = app.generate_iteration_candidate("source111111")

        self.assertEqual(result["prompt"], candidate["prompt"])
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(dedup.call_count, 2)
        self.assertIn("SOLO-QA #88", generate.call_args_list[1].args[1])
        self.assertEqual(result["prompt_dedup_review"], distinct)

    def test_same_repository_semantic_dedup_failure_discards_candidate(self):
        candidate = self.candidate()
        context = {
            "repo_path": "/tmp/existing-project",
            "repo_name": "demo",
            "repo_key": "example/demo",
            "repository_prompt_history": [{
                "reference": "SOLO-QA #10",
                "prompt": "仓库最初只实现样本登记和基础详情查看。",
                "task_type": "0-1 代码生成",
                "dedup_required": True,
            }],
        }
        source = {
            "phase": "complete",
            "container_cleaned": 1,
            "repo_url": "https://example.invalid/demo",
            "first_prompt_id": "prompt-1",
        }
        distinct = {
            "duplicate": False,
            "confidence": "low",
            "match_scope": "none",
            "reference": "",
            "overlap_kind": "none",
            "reason": "没有高置信度重复",
        }
        with mock.patch.object(app, "run_row", return_value=source), mock.patch.object(
            app, "iteration_project_context", return_value=context
        ), mock.patch.object(
            app, "run_codex_iteration_generation", return_value=candidate
        ) as generate, mock.patch.object(
            app, "global_prompt_dedup_history", return_value=[]
        ), mock.patch.object(
            app, "run_codex_iteration_validation", return_value=self.review_result()
        ), mock.patch.object(
            app,
            "run_codex_prompt_dedup_validation",
            side_effect=[app.WorkflowError("504 Gateway Time-out"), distinct],
        ) as dedup:
            result = app.generate_iteration_candidate("source111111")

        self.assertEqual(result["prompt"], candidate["prompt"])
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(dedup.call_count, 2)
        self.assertIn("规则 C 语义查重异常", generate.call_args_list[1].args[1])
        self.assertNotIn("prompt_dedup_warning", result)

    def test_cross_repository_semantic_dedup_failure_remains_non_blocking(self):
        candidate = self.candidate()
        context = {
            "repo_path": "/tmp/existing-project",
            "repo_name": "demo",
            "repo_key": "example/demo",
            "repository_prompt_history": [],
        }
        source = {
            "phase": "complete",
            "container_cleaned": 1,
            "repo_url": "https://example.invalid/demo",
            "first_prompt_id": "prompt-1",
        }
        global_history = [{
            "reference": "SOLO-QA #88",
            "prompt": candidate["prompt"],
            "task_type": "Feature 迭代",
            "lexical_similarity": 0.91,
            "bigram_containment": 0.86,
            "dedup_required": True,
        }]
        with mock.patch.object(app, "run_row", return_value=source), mock.patch.object(
            app, "iteration_project_context", return_value=context
        ), mock.patch.object(
            app, "run_codex_iteration_generation", return_value=candidate
        ) as generate, mock.patch.object(
            app, "global_prompt_dedup_history", return_value=global_history
        ), mock.patch.object(
            app, "run_codex_iteration_validation", return_value=self.review_result()
        ), mock.patch.object(
            app,
            "run_codex_prompt_dedup_validation",
            side_effect=app.WorkflowError("504 Gateway Time-out"),
        ):
            result = app.generate_iteration_candidate("source111111")

        generate.assert_called_once()
        self.assertIn("504 Gateway Time-out", result["prompt_dedup_warning"])

    def test_iteration_format_failure_is_repaired_without_full_regeneration(self):
        invalid = self.candidate()
        invalid["prompt"] += "继续补充重复说明。" * 80
        repaired = self.candidate()
        context = {"repo_path": "/tmp/existing-project", "repo_name": "demo"}
        source = {
            "phase": "complete",
            "container_cleaned": 1,
            "repo_url": "https://example.invalid/demo",
            "first_prompt_id": "prompt-1",
            "imported_baseline": 0,
        }
        with mock.patch.object(app, "run_row", return_value=source), mock.patch.object(
            app, "iteration_project_context", return_value=context
        ), mock.patch.object(
            app, "run_codex_iteration_generation", return_value=invalid
        ) as generate, mock.patch.object(
            app, "run_codex_iteration_format_repair", return_value=repaired
        ) as repair, mock.patch.object(
            app,
            "run_codex_iteration_validation",
            return_value=self.review_result(),
        ) as review:
            prompt = app.generate_iteration_prompt("source111111")

        self.assertEqual(prompt, repaired["prompt"])
        generate.assert_called_once()
        repair.assert_called_once()
        review.assert_called_once()

    def test_iteration_generation_retries_generator_timeout_with_feedback(self):
        candidate = self.candidate()
        context = {"repo_path": "/tmp/existing-project", "repo_name": "demo"}
        source = {
            "phase": "complete",
            "container_cleaned": 1,
            "repo_url": "https://example.invalid/demo",
            "first_prompt_id": "prompt-1",
            "imported_baseline": 0,
        }
        with mock.patch.object(app, "run_row", return_value=source), mock.patch.object(
            app, "iteration_project_context", return_value=context
        ), mock.patch.object(
            app,
            "run_codex_iteration_generation",
            side_effect=[app.WorkflowError("bugfix-generation 超时，已停止"), candidate],
        ) as generate, mock.patch.object(
            app,
            "run_codex_iteration_validation",
            return_value=self.review_result(),
        ):
            prompt = app.generate_iteration_prompt("source111111")

        self.assertEqual(prompt, candidate["prompt"])
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(
            generate.call_args_list[1].args[1],
            "bugfix-generation 超时，已停止",
        )

    def test_iteration_timeout_does_not_erase_previous_quality_feedback(self):
        candidate = self.candidate()
        context = {"repo_path": "/tmp/existing-project", "repo_name": "demo"}
        source = {
            "phase": "complete",
            "container_cleaned": 1,
            "repo_url": "https://example.invalid/demo",
            "first_prompt_id": "prompt-1",
            "imported_baseline": 0,
        }
        previous = "编号问题与历史题面重复，必须改选其他功能区域"
        with mock.patch.object(app, "run_row", return_value=source), mock.patch.object(
            app, "iteration_project_context", return_value=context
        ), mock.patch.object(
            app, "run_codex_iteration_generation",
            side_effect=[app.WorkflowError("bugfix-generation 超时，已停止"), candidate],
        ) as generate, mock.patch.object(
            app,
            "run_codex_iteration_validation",
            return_value=self.review_result(),
        ):
            prompt = app.generate_iteration_prompt(
                "source111111", initial_feedback=previous
            )

        self.assertEqual(prompt, candidate["prompt"])
        retry_feedback = generate.call_args_list[1].args[1]
        self.assertIn(previous, retry_feedback)
        self.assertIn("bugfix-generation 超时，已停止", retry_feedback)

    def test_iteration_generation_preserves_cancellation_instead_of_retrying(self):
        context = {"repo_path": "/tmp/existing-project", "repo_name": "demo"}
        source = {
            "phase": "complete",
            "container_cleaned": 1,
            "repo_url": "https://example.invalid/demo",
            "first_prompt_id": "prompt-1",
            "imported_baseline": 0,
        }
        with mock.patch.object(app, "run_row", return_value=source), mock.patch.object(
            app, "iteration_project_context", return_value=context
        ), mock.patch.object(
            app,
            "run_codex_iteration_generation",
            side_effect=app.JobCancelled("后台任务已取消"),
        ) as generate, self.assertRaises(app.JobCancelled):
            app.generate_iteration_prompt("source111111")

        generate.assert_called_once()

    def test_iteration_generation_retries_when_reviewed_difficulty_is_medium(self):
        context = {"repo_path": "/tmp/existing-project", "repo_name": "demo"}
        source = {
            "phase": "complete",
            "container_cleaned": 1,
            "repo_url": "https://example.invalid/demo",
            "first_prompt_id": "prompt-1",
        }
        medium_review = self.review_result()
        medium_review["approved"] = False
        medium_review["estimated_task_difficulty"] = "中等"
        medium_review["difficulty_evidence"] = ["仅为常规跨模块链路"]
        with mock.patch.object(app, "run_row", return_value=source), mock.patch.object(
            app, "iteration_project_context", return_value=context
        ), mock.patch.object(
            app,
            "run_codex_iteration_generation",
            side_effect=[self.candidate(), self.candidate()],
        ) as generate, mock.patch.object(
            app,
            "run_codex_iteration_targeted_repair",
            return_value=self.candidate(),
        ) as repair, mock.patch.object(
            app,
            "run_codex_iteration_validation",
            side_effect=[medium_review, self.review_result()],
        ) as review:
            prompt = app.generate_iteration_prompt("source111111")

        self.assertEqual(prompt, self.candidate()["prompt"])
        self.assertEqual(generate.call_count, 1)
        repair.assert_called_once()
        self.assertEqual(review.call_count, 2)
        self.assertIn("未达到困难：中等", repair.call_args.args[3])

    def test_iteration_generation_accepts_stopped_completed_baseline(self):
        context = {"repo_path": "/tmp/existing-project", "repo_name": "demo"}
        source = {
            "phase": "stopped",
            "container_cleaned": 1,
            "repo_url": "https://example.invalid/demo",
            "first_prompt_id": "prompt-1",
        }
        with mock.patch.object(app, "run_row", return_value=source), mock.patch.object(
            app, "iteration_project_context", return_value=context
        ), mock.patch.object(
            app, "run_codex_iteration_generation", return_value=self.candidate()
        ), mock.patch.object(
            app,
            "run_codex_iteration_validation",
            return_value=self.review_result(),
        ):
            prompt = app.generate_iteration_prompt("stopped11111")

        self.assertEqual(prompt, self.candidate()["prompt"])

    def test_iteration_generation_retries_when_reviewed_type_misses_selection(self):
        candidate = self.new_module_candidate()
        context = {"repo_path": "/tmp/existing-project", "repo_name": "demo"}
        source = {
            "phase": "complete",
            "container_cleaned": 1,
            "repo_url": "https://example.invalid/demo",
            "first_prompt_id": "prompt-1",
        }
        with mock.patch.object(app, "run_row", return_value=source), mock.patch.object(
            app, "iteration_project_context", return_value=context
        ), mock.patch.object(
            app, "run_codex_iteration_generation", return_value=candidate
        ) as generate, mock.patch.object(
            app,
            "run_codex_iteration_validation",
            side_effect=[
                self.review_result("Feature 迭代", False),
                self.review_result("0-1 代码生成"),
            ],
        ):
            prompt = app.generate_iteration_prompt(
                "source111111", "0-1 代码生成"
            )

        self.assertEqual(prompt, candidate["prompt"])
        self.assertEqual(generate.call_count, 2)
        self.assertIn("类型为Feature 迭代", generate.call_args_list[1].args[1])

    def test_invalid_iteration_type_is_rejected_before_generation(self):
        with self.assertRaisesRegex(app.WorkflowError, "只能是"):
            app.queue_automatic_iteration("source111111", "代码理解")

    def test_first_bugfix_prompt_is_complete_and_keeps_verified_problem_scope(self):
        candidate = self.bugfix_candidate()
        context = {
            "repo_path": "/tmp/existing-project",
            "repo_name": "sample-handoff-ledger",
            "iteration_history": [],
        }
        with mock.patch.object(
            app, "run_codex_structured", return_value=candidate
        ) as codex:
            result = app.run_codex_iteration_generation(
                context, target_task_type="Bug 修复"
            )

        prompt = result["prompt"]
        self.assertNotIn("\n", prompt)
        self.assertEqual(prompt.count("。"), 5)
        self.assertGreaterEqual(len(prompt), app.FIRST_BUGFIX_PROMPT_MIN_CHARS)
        self.assertLessEqual(len(prompt), app.FIRST_BUGFIX_PROMPT_MAX_CHARS)
        self.assertTrue(prompt.startswith(candidate["scope_summary"] + "。"))
        self.assertIn(candidate["confirmed_bugs"][0]["customer_summary"], prompt)
        self.assertNotIn("回归测试", prompt)
        self.assertNotIn("Docker Compose", prompt)
        self.assertNotIn("不扩大到无关历史缺陷", prompt)
        self.assertNotIn("当前表现为", prompt)
        self.assertNotIn("pytest", prompt)
        self.assertNotIn("请修复", prompt)
        self.assertEqual(len(result["confirmed_bugs"]), 4)
        self.assertIn("程序只把 scope_summary", codex.call_args.args[0])
        self.assertIn("实际执行的检查命令及关键结果摘要", codex.call_args.args[0])
        self.assertEqual(
            codex.call_args.args[4],
            app.BUGFIX_GENERATION_ATTEMPT_TIMEOUT_SECONDS,
        )
        schema = codex.call_args.args[1]
        self.assertEqual(schema["properties"]["task_type"]["enum"], ["Bug 修复"])
        self.assertIn("scope_summary", schema["required"])
        bug_schema = schema["properties"]["confirmed_bugs"]["items"]["properties"]
        self.assertEqual(bug_schema["reproduction"]["minLength"], 12)
        self.assertEqual(bug_schema["reproduction"]["maxLength"], 22)
        self.assertEqual(bug_schema["actual"]["minLength"], 8)
        self.assertEqual(bug_schema["actual"]["maxLength"], 18)
        self.assertEqual(bug_schema["expected"]["minLength"], 8)
        self.assertEqual(bug_schema["expected"]["maxLength"], 18)
        self.assertEqual(codex.call_args.kwargs["reasoning_effort"], "medium")

    def test_first_bugfix_prompt_uses_only_scope_and_customer_summaries(self):
        candidate = self.bugfix_candidate()
        candidate["confirmed_bugs"] = candidate["confirmed_bugs"][:3]

        result = app.normalize_generated_bugfix_candidate(candidate)

        prompt = result["prompt"]
        self.assertGreaterEqual(len(prompt), app.FIRST_BUGFIX_PROMPT_MIN_CHARS)
        self.assertLessEqual(len(prompt), app.FIRST_BUGFIX_PROMPT_MAX_CHARS)
        self.assertEqual(
            prompt,
            candidate["scope_summary"] + "。" + "".join(
                bug["customer_summary"] + "。"
                for bug in candidate["confirmed_bugs"]
            ),
        )

    def test_first_bugfix_prompt_rejects_generic_acceptance_tail(self):
        candidate = self.bugfix_candidate()
        candidate["scope_summary"] = (
            "样本交接确认覆盖接收码和位置更新，并要求为每条复现路径补充回归测试"
        )

        with self.assertRaisesRegex(app.WorkflowError, "通用验收模板"):
            app.normalize_generated_bugfix_candidate(candidate)

    def test_first_bugfix_prompt_rejects_insufficient_detail(self):
        candidate = self.bugfix_candidate()
        for bug in candidate["confirmed_bugs"]:
            bug["reproduction"] = "执行操作"
            bug["actual"] = "结果错误"
            bug["expected"] = "结果正确"

        with self.assertRaisesRegex(app.WorkflowError, "必须控制在"):
            app.normalize_generated_bugfix_candidate(candidate)

    def test_first_bugfix_prompt_rejects_solution_language(self):
        candidate = self.bugfix_candidate()
        candidate["confirmed_bugs"][0]["customer_summary"] = (
            "重复确认会移动两次，请修改事务逻辑保证只移动一次"
        )

        with self.assertRaisesRegex(app.WorkflowError, "解决方法"):
            app.normalize_generated_bugfix_candidate(candidate)

    def test_first_bugfix_prompt_repairs_summary_punctuation_before_validation(self):
        candidate = self.bugfix_candidate()
        candidate["confirmed_bugs"][0]["customer_summary"] = (
            "1. 两人同时确认时，位置会更新两次。"
            "页面应只保留一次移动结果！"
        )

        result = app.normalize_generated_bugfix_candidate(candidate)

        self.assertEqual(
            result["confirmed_bugs"][0]["customer_summary"],
            "两人同时确认时，位置会更新两次，页面应只保留一次移动结果",
        )
        self.assertIn(
            "两人同时确认时，位置会更新两次，页面应只保留一次移动结果。",
            result["prompt"],
        )

    def test_first_bugfix_prompt_fills_missing_expected_state_from_evidence(self):
        candidate = self.bugfix_candidate()
        candidate["confirmed_bugs"][0]["customer_summary"] = (
            "两人同时确认会让容器位置更新两次并产生重复记录"
        )

        result = app.normalize_generated_bugfix_candidate(candidate)

        summary = result["confirmed_bugs"][0]["customer_summary"]
        self.assertEqual(
            summary,
            "两人同时确认会让容器位置更新两次并产生重复记录，"
            "正确结果是容器只移动一次且无重复记录",
        )
        self.assertIn(summary + "。", result["prompt"])

    def test_first_bugfix_prompt_rebuilds_long_incomplete_summary(self):
        candidate = self.bugfix_candidate()
        candidate["confirmed_bugs"][0]["customer_summary"] = "重复确认导致异常" * 10

        result = app.normalize_generated_bugfix_candidate(candidate)

        summary = result["confirmed_bugs"][0]["customer_summary"]
        self.assertEqual(
            summary,
            "两人同时提交同一个接收码，当前容器位置更新两次，"
            "正确结果是容器只移动一次且无重复记录",
        )
        self.assertLessEqual(summary.__len__(), app.FIRST_BUGFIX_SUMMARY_MAX_CHARS)

    def test_bugfix_review_requires_verified_focused_scope(self):
        approved = {
            "approved": True,
            "reasons": [],
            "task_type": "Bug 修复",
            "estimated_task_difficulty": "困难",
            "difficulty_evidence": ["交接状态的并发与过期边界相互影响"],
            "bug_review": {
                "verified_bug_count": 4,
                "unverified_bugs": [],
                "overlapping_sequences": [],
                "solution_leaks": [],
                "style_issues": [],
                "scope_too_large": False,
                "single_focus": True,
            },
        }
        self.assertEqual(app.iteration_review_scope_errors(approved, "Bug 修复"), [])
        approved["bug_review"]["unverified_bugs"] = ["断网重试无法稳定复现"]
        self.assertIn(
            "无法确认问题",
            app.iteration_review_scope_errors(approved, "Bug 修复")[0],
        )

    def test_bugfix_review_reuses_complete_generation_evidence(self):
        context = {"repo_path": "/tmp/existing-project", "repo_name": "demo"}
        with mock.patch.object(
            app,
            "run_codex_structured",
            return_value=self.review_result("Bug 修复"),
        ) as codex:
            app.run_codex_bugfix_validation(context, self.bugfix_candidate())

        review_prompt = codex.call_args.args[0]
        self.assertIn("证据完整且相互一致时不要重复执行同一检查", review_prompt)
        self.assertIn("证据缺失、矛盾或无法对应当前提交时才补跑", review_prompt)

    def test_bugfix_review_rejects_medium_and_generation_prompt_allows_skip(self):
        review = {
            "approved": False,
            "reasons": ["组合工作量只达到中等"],
            "task_type": "Bug 修复",
            "estimated_task_difficulty": "中等",
            "difficulty_evidence": ["问题都是局部直接修补"],
            "bug_review": {
                "verified_bug_count": 4,
                "unverified_bugs": [],
                "overlapping_sequences": [],
                "solution_leaks": [],
                "style_issues": [],
                "scope_too_large": False,
                "single_focus": True,
            },
        }
        errors = app.iteration_review_scope_errors(review, "Bug 修复")
        self.assertTrue(any("Bug 修复难度未达到困难：中等" in error for error in errors))

        context = {"repo_path": "/tmp/existing-project", "repo_name": "demo"}
        with mock.patch.object(
            app, "run_codex_structured", return_value=self.bugfix_candidate()
        ) as codex:
            app.run_codex_bugfix_generation(context)
        self.assertIn("若当前代码没有这种组合", codex.call_args.args[0])
        self.assertIn(app.GENERATION_DIFFICULTY_AXIS_GUIDANCE, codex.call_args.args[0])

    def test_bugfix_candidate_keeps_independent_review_and_source_commit(self):
        candidate = self.bugfix_candidate()
        review = {
            "approved": True,
            "reasons": [],
            "task_type": "Bug 修复",
            "estimated_task_difficulty": "困难",
            "difficulty_evidence": ["交接状态的并发与过期边界相互影响"],
            "bug_review": {
                "verified_bug_count": 4,
                "unverified_bugs": [],
                "overlapping_sequences": [],
                "solution_leaks": [],
                "style_issues": [],
                "scope_too_large": False,
                "single_focus": True,
            },
        }
        source = {
            "phase": "complete",
            "container_cleaned": 1,
            "repo_url": "https://example.invalid/sample-handoff-ledger",
            "first_prompt_id": "prompt-root",
            "imported_baseline": 0,
        }
        context = {
            "repo_path": "/tmp/existing-project",
            "repo_name": "sample-handoff-ledger",
            "current_commit": "a" * 40,
            "iteration_history": [],
        }
        with mock.patch.object(app, "run_row", return_value=source), mock.patch.object(
            app, "iteration_project_context", return_value=context
        ), mock.patch.object(
            app, "run_codex_iteration_generation", return_value=candidate
        ), mock.patch.object(
            app, "run_codex_iteration_validation", return_value=review
        ):
            result = app.generate_iteration_candidate(
                "source111111", "Bug 修复"
            )

        self.assertEqual(result["confirmed_bugs"], candidate["confirmed_bugs"])
        self.assertEqual(result["independent_review"], review)
        self.assertEqual(result["evidence_source_commit"], "a" * 40)
        self.assertTrue(result["evidence_verified_at"])

    def test_complete_module_choice_is_enforced_by_generation_and_review(self):
        candidate = self.new_module_candidate()
        context = {"repo_path": "/tmp/existing-project", "repo_name": "demo"}
        with mock.patch.object(
            app, "run_codex_structured", return_value=candidate
        ) as codex:
            result = app.run_codex_iteration_generation(
                context, target_task_type="0-1 代码生成"
            )

        schema = codex.call_args.args[1]
        self.assertEqual(schema["properties"]["task_type"]["enum"], ["0-1 代码生成"])
        self.assertIn("此前不存在的完整新模块", codex.call_args.args[0])
        self.assertIn("不运行完整测试套件", codex.call_args.args[0])
        self.assertEqual(
            codex.call_args.args[4], app.ITERATION_GENERATION_ATTEMPT_TIMEOUT_SECONDS
        )
        self.assertEqual(codex.call_args.kwargs["reasoning_effort"], "medium")
        self.assertEqual(
            app.validate_generated_iteration(candidate, "0-1 代码生成"),
            candidate["prompt"],
        )
        with self.assertRaisesRegex(app.WorkflowError, "指定的任务类型"):
            app.validate_generated_iteration(candidate, "Feature 迭代")

    def test_one_click_iteration_starts_a_new_session_and_clears_guard(self):
        created = {"id": "created11111", "phase": "queued"}
        candidate = self.new_module_candidate()
        prompt = candidate["prompt"]
        with mock.patch.object(
            app, "existing_generated_iteration", return_value=None
        ), mock.patch.object(
            app, "validate_iteration_lineage_type", return_value={}
        ), mock.patch.object(
            app, "latest_iteration_baseline_run_id", return_value="source111111"
        ), mock.patch.object(
            app, "run_row", return_value={"id": "source111111"}
        ), mock.patch.object(
            app,
            "iteration_project_context",
            return_value={"current_commit": "a" * 40},
        ), mock.patch.object(
            app, "generate_iteration_candidate", return_value=candidate
        ) as generate, mock.patch.object(
            app, "start_second_turn", return_value=created
        ) as start, mock.patch.object(app, "add_event") as add_event:
            result = app.generate_and_start_iteration(
                "source111111", "0-1 代码生成"
            )

        self.assertEqual(result, created)
        generate.assert_called_once_with("source111111", "0-1 代码生成")
        start.assert_called_once_with(
            "source111111",
            {
                "prompt": prompt,
                "task_type": "0-1 代码生成",
                "_expected_baseline_run_id": "source111111",
                "_expected_baseline_sha": "a" * 40,
                "_iteration_metadata": {
                    "expansion_axis": candidate["expansion_axis"],
                    "modules": candidate["modules"],
                    "engineering_core": candidate["engineering_core"],
                    "complex_dimensions": candidate["complex_mechanisms"],
                    "main_user_flow": candidate["main_user_flow"],
                    "api_or_actions": candidate["api_or_actions"],
                    "new_state_sets": candidate["new_state_sets"],
                },
            },
        )
        add_event.assert_called_once_with(
            "created11111",
            "0-1 代码生成需求已由 gpt-5.6-sol 生成并复核",
            "success",
        )
        self.assertNotIn("source111111", app.ITERATION_GENERATIONS)

    def test_one_click_bugfix_passes_verified_evidence_to_new_run(self):
        created = {"id": "createdbugs1", "phase": "queued"}
        candidate = self.bugfix_candidate()
        candidate.update(
            {
                "prompt": app.validate_generated_bugfix_iteration(candidate),
                "expansion_axis": "修复样本交接确认中的已复现问题",
                "engineering_core": candidate["focus_area"],
                "complex_mechanisms": [],
                "api_or_actions": [],
                "new_state_sets": [],
                "independent_review": {
                    "approved": True,
                    "reasons": [],
                    "task_type": "Bug 修复",
                    "bug_review": {"verified_bug_count": 4},
                },
                "evidence_source_commit": "b" * 40,
                "evidence_verified_at": "2026-09-12 13:00:00 +0800",
            }
        )
        with mock.patch.object(
            app, "existing_generated_iteration", return_value=None
        ), mock.patch.object(
            app, "validate_iteration_lineage_type", return_value={}
        ), mock.patch.object(
            app, "latest_iteration_baseline_run_id", return_value="source111111"
        ), mock.patch.object(
            app, "run_row", return_value={"id": "source111111"}
        ), mock.patch.object(
            app,
            "iteration_project_context",
            return_value={"current_commit": "a" * 40},
        ), mock.patch.object(
            app, "generate_iteration_candidate", return_value=candidate
        ), mock.patch.object(
            app, "start_second_turn", return_value=created
        ) as start, mock.patch.object(app, "add_event"):
            result = app.generate_and_start_iteration(
                "source111111", "Bug 修复"
            )

        self.assertEqual(result, created)
        evidence = start.call_args.args[1]["_bug_generation_evidence"]
        self.assertEqual(evidence["source_run_id"], "source111111")
        self.assertEqual(evidence["source_commit"], "b" * 40)
        self.assertEqual(evidence["bugs"], candidate["confirmed_bugs"])
        self.assertTrue(evidence["independent_review"]["approved"])

    def test_one_click_iteration_uses_latest_lineage_baseline(self):
        created = {"id": "created22222", "phase": "queued"}
        candidate = self.candidate()
        prompt = candidate["prompt"]
        with mock.patch.object(
            app, "existing_generated_iteration", return_value=None
        ), mock.patch.object(
            app, "validate_iteration_lineage_type", return_value={}
        ), mock.patch.object(
            app,
            "latest_iteration_baseline_run_id",
            return_value="latest222222",
        ), mock.patch.object(
            app, "run_row", return_value={"id": "latest222222"}
        ), mock.patch.object(
            app,
            "iteration_project_context",
            return_value={"current_commit": "a" * 40},
        ), mock.patch.object(
            app, "generate_iteration_candidate", return_value=candidate
        ) as generate, mock.patch.object(
            app, "start_second_turn", return_value=created
        ) as start, mock.patch.object(app, "add_event") as add_event:
            result = app.generate_and_start_iteration(
                "root111111", "Feature 迭代"
            )

        self.assertEqual(result, created)
        generate.assert_called_once_with("latest222222", "Feature 迭代")
        start.assert_called_once_with(
            "latest222222",
            {
                "prompt": prompt,
                "task_type": "Feature 迭代",
                "_expected_baseline_run_id": "latest222222",
                "_expected_baseline_sha": "a" * 40,
                "_iteration_metadata": {
                    "expansion_axis": candidate["expansion_axis"],
                    "modules": candidate["modules"],
                    "engineering_core": candidate["engineering_core"],
                    "complex_dimensions": candidate["complex_mechanisms"],
                    "main_user_flow": candidate["main_user_flow"],
                    "api_or_actions": candidate["api_or_actions"],
                    "new_state_sets": candidate["new_state_sets"],
                },
            },
        )
        add_event.assert_any_call(
            "root111111",
            "本次迭代改用同一项目链的最新代码记录 latest222222",
        )
        self.assertNotIn("latest222222", app.ITERATION_GENERATIONS)

    def test_refill_can_create_an_additional_feature_iteration(self):
        created = {"id": "created11111", "phase": "queued"}
        candidate = self.candidate()
        prompt = candidate["prompt"]
        with mock.patch.object(app, "existing_generated_iteration") as existing, mock.patch.object(
            app, "validate_iteration_lineage_type", return_value={}
        ), mock.patch.object(
            app, "latest_iteration_baseline_run_id", return_value="source111111"
        ), mock.patch.object(
            app, "run_row", return_value={"id": "source111111"}
        ), mock.patch.object(
            app,
            "iteration_project_context",
            return_value={"current_commit": "a" * 40},
        ), mock.patch.object(
            app, "generate_iteration_candidate", return_value=candidate
        ), mock.patch.object(
            app, "start_second_turn", return_value=created
        ) as start, mock.patch.object(app, "add_event"):
            result = app.generate_and_start_iteration(
                "source111111",
                "Feature 迭代",
                reuse_existing=False,
                auto_refill=True,
            )

        self.assertEqual(result, created)
        existing.assert_not_called()
        start.assert_called_once_with(
            "source111111",
            {
                "prompt": prompt,
                "task_type": "Feature 迭代",
                "_expected_baseline_run_id": "source111111",
                "_expected_baseline_sha": "a" * 40,
                "_iteration_metadata": {
                    "expansion_axis": candidate["expansion_axis"],
                    "modules": candidate["modules"],
                    "engineering_core": candidate["engineering_core"],
                    "complex_dimensions": candidate["complex_mechanisms"],
                    "main_user_flow": candidate["main_user_flow"],
                    "api_or_actions": candidate["api_or_actions"],
                    "new_state_sets": candidate["new_state_sets"],
                },
                "_auto_refill": True,
            },
        )

    def test_background_queue_returns_existing_iteration_idempotently(self):
        with mock.patch.object(
            app,
            "existing_generated_iteration",
            return_value={
                "id": "created11111",
                "phase": "first_running",
                "task_type": "0-1 代码生成",
            },
        ), mock.patch.object(app.threading, "Thread") as thread:
            result = app.queue_automatic_iteration("source111111")

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["created_run_id"], "created11111")
        self.assertEqual(result["task_type"], "0-1 代码生成")
        thread.assert_not_called()

    def test_background_queue_starts_once_and_returns_immediately(self):
        source = {
            "phase": "stopped",
            "container_cleaned": 1,
            "repo_url": "https://example.invalid/demo",
            "first_prompt_id": "prompt-1",
        }
        with app.ITERATION_JOB_LOCK:
            app.ITERATION_JOBS.pop("source111111", None)
        try:
            with mock.patch.object(
                app, "existing_generated_iteration", return_value=None
            ), mock.patch.object(
                app, "validate_iteration_lineage_type", return_value={}
            ), mock.patch.object(app, "run_row", return_value=source), mock.patch.object(
                app, "latest_iteration_baseline_run_id", return_value="source111111"
            ), mock.patch.object(
                app, "iteration_origin_run_id", return_value="source111111"
            ), mock.patch.object(
                app, "iteration_project_context", return_value={"repo_path": "/tmp/demo"}
            ), mock.patch.object(app, "add_event"), mock.patch.object(
                app, "automatic_refill_occupancy", return_value=0
            ), mock.patch.object(
                app.threading, "Thread"
            ) as thread:
                first = app.queue_automatic_iteration(
                    "source111111", "0-1 代码生成"
                )
                second = app.queue_automatic_iteration(
                    "source111111", "0-1 代码生成"
                )

            self.assertEqual(first["status"], "generating")
            self.assertEqual(second["status"], "generating")
            thread.assert_called_once_with(
                target=app.automatic_iteration_worker,
                args=("source111111", "0-1 代码生成", True, False, 0, ""),
                daemon=True,
            )
            thread.return_value.start.assert_called_once_with()
        finally:
            with app.ITERATION_JOB_LOCK:
                app.ITERATION_JOBS.pop("source111111", None)

    def test_background_queue_reuses_previous_failure_as_generation_feedback(self):
        source = {
            "phase": "stopped",
            "container_cleaned": 1,
            "repo_url": "https://example.invalid/demo",
            "first_prompt_id": "prompt-1",
        }
        feedback = "编号问题与历史题面重复，必须改选其他功能区域"
        with app.ITERATION_JOB_LOCK:
            app.ITERATION_JOBS["source111111"] = {
                "status": "failed",
                "task_type": "Bug 修复",
                "error": feedback,
            }
        try:
            with mock.patch.object(
                app, "existing_generated_iteration", return_value=None
            ), mock.patch.object(
                app, "validate_iteration_lineage_type", return_value={}
            ), mock.patch.object(app, "run_row", return_value=source), mock.patch.object(
                app, "latest_iteration_baseline_run_id", return_value="source111111"
            ), mock.patch.object(
                app, "iteration_origin_run_id", return_value="source111111"
            ), mock.patch.object(
                app, "iteration_project_context", return_value={"repo_path": "/tmp/demo"}
            ), mock.patch.object(app, "add_event"), mock.patch.object(
                app, "automatic_refill_occupancy", return_value=0
            ), mock.patch.object(app.threading, "Thread") as thread:
                job = app.queue_automatic_iteration("source111111", "Bug 修复")

            self.assertEqual(job["last_error"], feedback)
            thread.assert_called_once_with(
                target=app.automatic_iteration_worker,
                args=("source111111", "Bug 修复", True, False, 0, feedback),
                daemon=True,
            )
        finally:
            with app.ITERATION_JOB_LOCK:
                app.ITERATION_JOBS.pop("source111111", None)

    def test_cancelled_iteration_recovers_feedback_from_latest_failure_event(self):
        database = mock.MagicMock()
        database.execute.return_value.fetchone.return_value = {
            "message": "自动生成迭代需求失败：旧方向与历史题面重复"
        }
        connection = mock.MagicMock()
        connection.__enter__.return_value = database
        with mock.patch.object(app, "db_connection", return_value=connection):
            feedback = app.previous_iteration_generation_feedback(
                "source111111",
                "Bug 修复",
                {
                    "status": "stopped",
                    "task_type": "Bug 修复",
                    "error": "迭代题面生成已取消",
                },
            )

        self.assertEqual(feedback, "旧方向与历史题面重复")

    def test_service_shutdown_keeps_generating_iteration_for_recovery(self):
        with app.ITERATION_JOB_LOCK:
            app.ITERATION_JOBS["source111111"] = {
                "status": "generating",
                "task_type": "Bug 修复",
                "last_error": "旧方向与历史题面重复",
            }
        app.SERVER_SHUTTING_DOWN.set()
        try:
            with mock.patch.object(
                app,
                "generate_and_start_iteration",
                side_effect=app.JobCancelled("服务关闭"),
            ):
                app.automatic_iteration_worker("source111111", "Bug 修复")

            with app.ITERATION_JOB_LOCK:
                job = dict(app.ITERATION_JOBS["source111111"])
            self.assertEqual(job["status"], "generating")
            self.assertEqual(job["last_error"], "旧方向与历史题面重复")
        finally:
            app.SERVER_SHUTTING_DOWN.clear()
            with app.ITERATION_JOB_LOCK:
                app.ITERATION_JOBS.pop("source111111", None)

    def test_service_recovery_requeues_excess_auto_generation(self):
        jobs = [
            {
                "status": "generating",
                "source_run_id": f"source{index:06d}",
                "baseline_run_id": f"source{index:06d}",
                "lineage_origin_run_id": f"source{index:06d}",
                "task_type": "Feature 迭代",
                "auto_refill": True,
                "started_at": f"2026-09-16 08:0{index}:00 +0800",
                "last_error": "旧候选未完成",
            }
            for index in range(app.ITERATION_GENERATION_MAX_PARALLEL + 2)
        ]
        database = mock.MagicMock()

        def execute(query, *_args):
            cursor = mock.MagicMock()
            cursor.fetchone.return_value = (
                (0,) if "SELECT COUNT(*) FROM runs" in query else None
            )
            return cursor

        database.execute.side_effect = execute
        connection = mock.MagicMock()
        connection.__enter__.return_value = database
        saved = []
        with mock.patch.object(
            app, "iteration_job_values", return_value=jobs
        ), mock.patch.object(
            app, "db_connection", return_value=connection
        ), mock.patch.object(
            app, "run_row", return_value={"id": "source"}
        ), mock.patch.object(
            app, "put_iteration_job", side_effect=lambda job: saved.append(dict(job))
        ), mock.patch.object(app, "clear_job_cancellation"), mock.patch.object(
            app, "add_event"
        ), mock.patch.object(app.threading, "Thread") as thread:
            app.recover_iteration_jobs()

        self.assertEqual(thread.call_count, app.ITERATION_GENERATION_MAX_PARALLEL)
        self.assertEqual(
            thread.return_value.start.call_count,
            app.ITERATION_GENERATION_MAX_PARALLEL,
        )
        deferred = [
            job for job in saved
            if job.get("stage") == "服务恢复后等待生成槽"
        ]
        self.assertEqual(len(deferred), 2)
        self.assertTrue(all(job["status"] == "failed" for job in deferred))
        self.assertTrue(all(job["cooldown_until_epoch"] is None for job in deferred))

    def test_background_worker_exposes_success_and_failure_status(self):
        with app.ITERATION_JOB_LOCK:
            app.ITERATION_JOBS["source111111"] = {"status": "generating"}
        with mock.patch.object(
            app,
            "generate_and_start_iteration",
            return_value={"id": "created11111", "task_type": "0-1 代码生成"},
        ):
            app.automatic_iteration_worker("source111111", "0-1 代码生成")
        with app.ITERATION_JOB_LOCK:
            success = dict(app.ITERATION_JOBS["source111111"])
        self.assertEqual(success["status"], "complete")
        self.assertEqual(success["created_run_id"], "created11111")
        self.assertEqual(success["task_type"], "0-1 代码生成")

        with mock.patch.object(
            app,
            "generate_and_start_iteration",
            side_effect=app.WorkflowError("模型复核未通过"),
        ), mock.patch.object(app, "add_event") as event:
            app.automatic_iteration_worker("source111111", "0-1 代码生成")
        with app.ITERATION_JOB_LOCK:
            failure = app.ITERATION_JOBS.pop("source111111")
        self.assertEqual(failure["status"], "failed")
        self.assertEqual(failure["error"], "模型复核未通过")
        event.assert_called_once_with(
            "source111111", "自动生成迭代需求失败：模型复核未通过", "error"
        )

    def test_auto_refill_cools_down_non_semantic_feature_review_failure(self):
        detail = (
            f"连续 {app.ITERATION_GENERATION_ATTEMPTS} 次未生成合规迭代需求："
            "迭代题面应为 260 至 800 字，当前共 845 字"
        )
        with app.ITERATION_JOB_LOCK:
            app.ITERATION_JOBS["source111111"] = {"status": "generating"}
        try:
            with mock.patch.object(
                app,
                "generate_and_start_iteration",
                side_effect=app.WorkflowError(detail),
            ), mock.patch.object(app, "add_event") as event, mock.patch.object(
                app, "record_auto_refill_candidate_skip"
            ) as record_skip, mock.patch.object(
                app, "record_auto_refill_failure"
            ) as record_failure, mock.patch.object(app, "pause_auto_refill") as pause, mock.patch.object(
                app.threading, "Thread"
            ) as thread:
                app.automatic_iteration_worker(
                    "source111111",
                    "Feature 迭代",
                    False,
                    True,
                )

            with app.ITERATION_JOB_LOCK:
                job = dict(app.ITERATION_JOBS["source111111"])
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["stage"], "生成失败")
            self.assertGreater(job["cooldown_until_epoch"], int(time.time()))
            event.assert_called_once_with(
                "source111111", f"自动生成迭代需求失败：{detail}", "error"
            )
            record_skip.assert_called_once()
            record_failure.assert_not_called()
            pause.assert_not_called()
            thread.assert_not_called()
        finally:
            with app.ITERATION_JOB_LOCK:
                app.ITERATION_JOBS.pop("source111111", None)

    def test_auto_refill_permanently_skips_source_after_rule_c_exhaustion(self):
        detail = (
            f"连续 {app.ITERATION_GENERATION_ATTEMPTS} 次未生成合规迭代需求："
            "提交前语义查重命中同仓库历史 SOLO-QA #88：同一页面流程继续扩展"
        )
        with app.ITERATION_JOB_LOCK:
            app.ITERATION_JOBS["source111111"] = {"status": "generating"}
        try:
            with mock.patch.object(
                app,
                "generate_and_start_iteration",
                side_effect=app.WorkflowError(detail),
            ), mock.patch.object(app, "add_event"), mock.patch.object(
                app, "record_auto_refill_candidate_skip"
            ) as record_skip, mock.patch.object(
                app, "request_auto_refill_new_project"
            ) as request_new_project:
                app.automatic_iteration_worker(
                    "source111111",
                    "Feature 迭代",
                    False,
                    True,
                )

            with app.ITERATION_JOB_LOCK:
                job = dict(app.ITERATION_JOBS["source111111"])
            self.assertEqual(job["status"], "blocked")
            self.assertEqual(
                job["stage"],
                "同仓库规则 C 命中，当前来源已永久跳过",
            )
            self.assertIsNone(job["cooldown_until_epoch"])
            record_skip.assert_called_once()
            request_new_project.assert_called_once()
        finally:
            with app.ITERATION_JOB_LOCK:
                app.ITERATION_JOBS.pop("source111111", None)

    def test_recovery_blocks_exhausted_rule_c_source_instead_of_resuming_it(self):
        detail = (
            f"连续 {app.ITERATION_GENERATION_ATTEMPTS} 次未生成合规迭代需求："
            "提交前语义查重命中同仓库历史 SOLO-QA #88：同一页面流程继续扩展"
        )
        job = {
            "status": "generating",
            "source_run_id": "source111111",
            "lineage_origin_run_id": "source111111",
            "task_type": "Feature 迭代",
            "auto_refill": True,
            "last_error": detail,
        }
        saved = []
        with mock.patch.object(
            app, "iteration_job_values", return_value=[job]
        ), mock.patch.object(
            app, "db_connection", return_value=mock.MagicMock()
        ), mock.patch.object(
            app, "put_iteration_job", side_effect=lambda item: saved.append(dict(item))
        ), mock.patch.object(app.threading, "Thread") as thread:
            app.recover_iteration_jobs()

        self.assertEqual(saved[0]["status"], "blocked")
        self.assertEqual(
            saved[0]["stage"],
            "同仓库规则 C 命中，当前来源已永久跳过",
        )
        self.assertIsNone(saved[0]["cooldown_until_epoch"])
        thread.assert_not_called()

    def test_auto_refill_still_counts_generation_timeout_as_platform_failure(self):
        detail = (
            f"连续 {app.ITERATION_GENERATION_ATTEMPTS} 次未生成合规迭代需求："
            "bugfix-generation 超时，已停止"
        )
        with app.ITERATION_JOB_LOCK:
            app.ITERATION_JOBS["source111111"] = {"status": "generating"}
        try:
            with mock.patch.object(
                app,
                "generate_and_start_iteration",
                side_effect=app.WorkflowError(detail),
            ), mock.patch.object(app, "add_event"), mock.patch.object(
                app, "record_auto_refill_candidate_skip"
            ) as record_skip, mock.patch.object(
                app, "record_auto_refill_failure"
            ) as record_failure:
                app.automatic_iteration_worker(
                    "source111111",
                    "Bug 修复",
                    False,
                    True,
                )

            record_skip.assert_not_called()
            record_failure.assert_called_once()
        finally:
            with app.ITERATION_JOB_LOCK:
                app.ITERATION_JOBS.pop("source111111", None)

    def test_auto_refill_falls_back_to_feature_when_no_new_module_is_suitable(self):
        detail = (
            f"连续 {app.ITERATION_GENERATION_ATTEMPTS} 次未生成合规迭代需求："
            "候选与现有模块重复"
        )
        with app.ITERATION_JOB_LOCK:
            app.ITERATION_JOBS["source111111"] = {"status": "generating"}
        try:
            with mock.patch.object(
                app,
                "generate_and_start_iteration",
                side_effect=app.WorkflowError(detail),
            ), mock.patch.object(app, "add_event") as event, mock.patch.object(
                app, "record_auto_refill_detail"
            ) as record, mock.patch.object(app, "pause_auto_refill") as pause, mock.patch.object(
                app.threading, "Thread"
            ) as thread:
                app.automatic_iteration_worker(
                    "source111111",
                    "0-1 代码生成",
                    False,
                    True,
                )

            with app.ITERATION_JOB_LOCK:
                job = dict(app.ITERATION_JOBS["source111111"])
            self.assertEqual(job["status"], "generating")
            self.assertEqual(job["task_type"], "Feature 迭代")
            event.assert_called_once()
            record.assert_called_once()
            pause.assert_not_called()
            thread.assert_called_once_with(
                target=app.automatic_iteration_worker,
                args=(
                    "source111111",
                    "Feature 迭代",
                    False,
                    True,
                    0,
                    detail,
                ),
                daemon=True,
            )
            thread.return_value.start.assert_called_once_with()
        finally:
            with app.ITERATION_JOB_LOCK:
                app.ITERATION_JOBS.pop("source111111", None)

    def test_status_without_type_recovers_the_current_background_job(self):
        job = {
            "status": "generating",
            "source_run_id": "source111111",
            "task_type": "0-1 代码生成",
        }
        with app.ITERATION_JOB_LOCK:
            app.ITERATION_JOBS["source111111"] = job
        try:
            with mock.patch.object(app, "run_row", return_value={"id": "source111111"}), mock.patch.object(
                app, "existing_generated_iteration"
            ) as existing:
                result = app.automatic_iteration_status("source111111", None)
            self.assertEqual(result, job)
            existing.assert_not_called()
        finally:
            with app.ITERATION_JOB_LOCK:
                app.ITERATION_JOBS.pop("source111111", None)

    def test_duplicate_one_click_generation_is_rejected(self):
        with app.ITERATION_GENERATION_LOCK:
            app.ITERATION_GENERATIONS.add("source111111")
        try:
            with mock.patch.object(
                app, "existing_generated_iteration", return_value=None
            ), mock.patch.object(
                app, "latest_iteration_baseline_run_id", return_value="source111111"
            ), mock.patch.object(
                app, "validate_iteration_lineage_type", return_value={}
            ), self.assertRaisesRegex(app.WorkflowError, "正在生成"):
                app.generate_and_start_iteration("source111111")
        finally:
            with app.ITERATION_GENERATION_LOCK:
                app.ITERATION_GENERATIONS.discard("source111111")

    def test_parallel_generation_for_the_same_repository_is_rejected(self):
        repository_token = "repo:example/shared-repo"
        with app.ITERATION_GENERATION_LOCK:
            app.ITERATION_GENERATIONS.add(repository_token)
        try:
            with mock.patch.object(
                app, "existing_generated_iteration", return_value=None
            ), mock.patch.object(
                app, "latest_iteration_baseline_run_id", return_value="source111111"
            ), mock.patch.object(
                app, "validate_iteration_lineage_type", return_value={}
            ), mock.patch.object(
                app,
                "run_row",
                return_value={
                    "id": "source111111",
                    "repo_name": "shared-repo",
                    "repo_url": "https://github.com/example/shared-repo.git",
                },
            ), self.assertRaisesRegex(app.WorkflowError, "同一仓库"):
                app.generate_and_start_iteration("source111111")
        finally:
            with app.ITERATION_GENERATION_LOCK:
                app.ITERATION_GENERATIONS.discard(repository_token)


class ExportTests(unittest.TestCase):
    def insert_completed_turn(self, root, run_id="abc123abc123"):
        timestamp = app.now_text()
        repo = root / "0007-export-demo" / "workspace"
        repo.mkdir(parents=True, exist_ok=True)
        session_id = "session-export"
        trace_events = [
            {
                "type": "user",
                "sessionId": session_id,
                "version": "2.1.263",
                "promptId": "prompt-export",
                "message": {"content": "完成真实导出链路"},
            },
            {
                "type": "assistant",
                "sessionId": session_id,
                "version": "2.1.263",
                "message": {
                    "stop_reason": "stop_sequence",
                    "content": [{"type": "text", "text": "已经完成。"}],
                },
            },
            {
                "type": "system",
                "subtype": "turn_duration",
                "sessionId": session_id,
                "version": "2.1.263",
            },
        ]
        trace_content = "\n".join(
            json.dumps(event, ensure_ascii=False) for event in trace_events
        ) + "\n"
        raw_trace = root / "traces" / "-workspace" / f"{session_id}.jsonl"
        turn_trace = root / "traces" / session_id / "turn-01.jsonl"
        raw_trace.parent.mkdir(parents=True)
        turn_trace.parent.mkdir(parents=True)
        raw_trace.write_text(trace_content, encoding="utf-8")
        turn_trace.write_text(trace_content, encoding="utf-8")
        trace_sha256 = hashlib.sha256(turn_trace.read_bytes()).hexdigest()
        with app.db_connection() as database:
            database.execute(
                """INSERT INTO runs(
                     id, repo_name, model, task_type, task_difficulty,
                     language_framework, repo_path, repo_url, phase, session_id, snapshot_url,
                     first_prompt, trajectory_path, verification_commands, harness_version,
                     created_at, updated_at
                   ) VALUES (?, 'export-demo', 'gpt-5.6-sol', '0-1 代码生成', '困难',
                             'Python, FastAPI', ?, 'https://github.com/example/export-demo',
                             'complete', 'session-export',
                             'https://github.com/example/export-demo/commit/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                             '原始题面', ?, '[]', '2.1.263', ?, ?)""",
                (run_id, str(repo), str(raw_trace), timestamp, timestamp),
            )
            database.execute(
                """INSERT INTO run_turns(
                     run_id, turn_number, intent_type, prompt, model, prompt_id,
                     review_result, commit_sha, trajectory_path, trajectory_sha256, status,
                     verification, created_at, updated_at
                   ) VALUES (?, 1, '0-1 代码生成', '完成真实导出链路', 'gpt-5.6-sol',
                             'prompt-export', ?, 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', ?, ?,
                             'complete', '[]', ?, ?)""",
                (
                    run_id,
                    json.dumps({"evaluation": sample_evaluation()}, ensure_ascii=False),
                    str(turn_trace),
                    trace_sha256,
                    timestamp,
                    timestamp,
                ),
            )
        return repo

    def set_turn_difficulty_and_date(
        self,
        run_id,
        difficulty="中等",
        completed_at="2026-09-15 12:00:00 +0800",
    ):
        with app.db_connection() as database:
            row = database.execute(
                """SELECT review_result FROM run_turns
                    WHERE run_id = ? AND turn_number = 1""",
                (run_id,),
            ).fetchone()
            review = json.loads(row["review_result"])
            review["evaluation"]["task_difficulty"] = difficulty
            database.execute(
                """UPDATE run_turns SET review_result = ?, updated_at = ?
                    WHERE run_id = ? AND turn_number = 1""",
                (
                    json.dumps(review, ensure_ascii=False),
                    completed_at,
                    run_id,
                ),
            )
            database.execute(
                "UPDATE runs SET task_difficulty = ? WHERE id = ?",
                (difficulty, run_id),
            )

    def test_difficulty_reassessment_preview_excludes_remote_locked_turns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root, "e11111111111")
                self.insert_completed_turn(root / "second", "d11111111111")
                self.set_turn_difficulty_and_date("e11111111111")
                self.set_turn_difficulty_and_date("d11111111111")
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO solo_qa_submissions(
                             run_id, turn_number, remote_submission_id,
                             remote_status, state, created_at, updated_at
                           ) VALUES (?, 1, 'remote-locked', 'QC_PASSED',
                                     'qc_passed', ?, ?)""",
                        ("d11111111111", timestamp, timestamp),
                    )

                preview = app.difficulty_reassessment_preview(
                    "2026-09-15", low_only=True
                )

        self.assertEqual(preview["total"], 2)
        self.assertEqual(preview["eligible"], 1)
        self.assertEqual(preview["locked"], 1)
        self.assertEqual(preview["distribution"]["中等"], 2)

    def test_difficulty_reassessment_stages_then_applies_only_difficulty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app.threading, "Thread") as thread:
                app.initialize_database()
                self.insert_completed_turn(root)
                self.set_turn_difficulty_and_date("abc123abc123")
                before = app.turn_evaluation(app.completed_turn_rows()[0])
                started = app.start_difficulty_reassessment(
                    {"date": "2026-09-15", "low_only": True}
                )
                job_id = started["job"]["id"]
                thread.assert_called_once()
                with mock.patch.object(
                    app,
                    "run_codex_difficulty_reassessment_batch",
                    return_value={
                        "abc123abc123:1": {
                            "key": "abc123abc123:1",
                            "task_difficulty": "困难",
                            "confidence": "高",
                            "rationale": "实现包含不可删除的恢复状态不变量。",
                            "evidence": ["状态恢复验收通过"],
                        }
                    },
                ):
                    app.difficulty_reassessment_worker(job_id)

                staged = app.difficulty_reassessment_job(job_id)
                unchanged_before_apply = app.turn_evaluation(
                    app.completed_turn_rows()[0]
                )
                applied = app.apply_difficulty_reassessment(
                    job_id, ["abc123abc123:1"]
                )
                after = app.turn_evaluation(app.completed_turn_rows()[0])
                run = dict(app.run_row("abc123abc123"))

        self.assertEqual(staged["job"]["status"], "ready_to_apply")
        self.assertEqual(staged["items"][0]["status"], "proposed")
        self.assertEqual(unchanged_before_apply["task_difficulty"], "中等")
        self.assertEqual(applied["applied"], 1)
        self.assertEqual(after["task_difficulty"], "困难")
        self.assertEqual(after["delivery"], before["delivery"])
        self.assertEqual(run["task_difficulty"], "困难")

    def test_difficulty_reassessment_apply_skips_newly_locked_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app.threading, "Thread"):
                app.initialize_database()
                self.insert_completed_turn(root)
                self.set_turn_difficulty_and_date("abc123abc123")
                started = app.start_difficulty_reassessment(
                    {"date": "2026-09-15", "low_only": True}
                )
                job_id = started["job"]["id"]
                with mock.patch.object(
                    app,
                    "run_codex_difficulty_reassessment_batch",
                    return_value={
                        "abc123abc123:1": {
                            "key": "abc123abc123:1",
                            "task_difficulty": "困难",
                            "confidence": "中",
                            "rationale": "跨模块状态约束达到困难。",
                            "evidence": ["验收材料"],
                        }
                    },
                ):
                    app.difficulty_reassessment_worker(job_id)
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO solo_qa_submissions(
                             run_id, turn_number, remote_submission_id,
                             remote_status, state, created_at, updated_at
                           ) VALUES ('abc123abc123', 1, 'remote-pending',
                                     'SUBMITTED', 'qc_pending', ?, ?)""",
                        (timestamp, timestamp),
                    )

                result = app.apply_difficulty_reassessment(
                    job_id, ["abc123abc123:1"]
                )
                current = app.turn_evaluation(app.completed_turn_rows()[0])
                item = app.difficulty_reassessment_job(job_id)["items"][0]

        self.assertEqual(result, {
            "job_id": job_id,
            "applied": 0,
            "skipped": 1,
            "remaining": 0,
        })
        self.assertEqual(current["task_difficulty"], "中等")
        self.assertEqual(item["status"], "stale")

    def test_new_score_policy_blocks_over_21_and_offers_automatic_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                old_row = app.completed_turn_rows()[0]
                old_ready, old_issues = app.export_readiness(old_row)
                old_summary = app.completed_turns()[0]
                evaluation = sample_evaluation()
                evaluation["_score_cap_policy_version"] = (
                    app.EVALUATION_SCORE_CAP_POLICY_VERSION
                )
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": evaluation}, ensure_ascii=False
                    ),
                )
                row = app.completed_turn_rows()[0]
                export_ready, export_issues = app.export_readiness(row)
                solo_ready, solo_issues = app.solo_qa_readiness(row)
                summary = app.completed_turns()[0]

        self.assertTrue(old_ready, old_issues)
        self.assertFalse(old_summary["score_cap_applies"])
        self.assertFalse(export_ready)
        self.assertFalse(solo_ready)
        self.assertTrue(any("最高允许 21 分" in issue for issue in export_issues))
        self.assertTrue(any("最高允许 21 分" in issue for issue in solo_issues))
        self.assertEqual(summary["score_total"], 25)
        self.assertEqual(summary["score_max_total"], 21)
        self.assertTrue(summary["score_cap_applies"])
        self.assertEqual(summary["evaluation_repair"]["status"], "needed")

    def test_manual_score_save_rejects_over_cap_only_for_new_policy_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["_score_cap_policy_version"] = (
                    app.EVALUATION_SCORE_CAP_POLICY_VERSION
                )
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": evaluation}, ensure_ascii=False
                    ),
                )
                manual = {
                    key: dict(sample_evaluation()[key])
                    for key in app.EVALUATION_DIMENSION_KEYS
                }
                with self.assertRaisesRegex(
                    app.WorkflowError, "最高允许 21 分"
                ):
                    app.save_completed_turn_evaluation({
                        "turn_key": "abc123abc123:1",
                        "evaluation": manual,
                    })

    def test_score_cap_auto_repair_can_atomically_persist_lower_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                original = sample_evaluation()
                original["_score_cap_policy_version"] = (
                    app.EVALUATION_SCORE_CAP_POLICY_VERSION
                )
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": original}, ensure_ascii=False
                    ),
                )
                queued = app.queue_completed_turn_evaluation_repairs(
                    {"turn_keys": ["abc123abc123:1"]}, schedule_jobs=False
                )
                source_sha256 = app.completed_turns()[0][
                    "evaluation_repair"
                ]["revision"]
                self.assertTrue(
                    app.claim_evaluation_repair_job(
                        "abc123abc123:1", source_sha256
                    )
                )
                repaired = json.loads(json.dumps(original, ensure_ascii=False))
                for key in app.EVALUATION_DIMENSION_KEYS[1:]:
                    repaired[key] = {
                        "score": 4,
                        "description": (
                            f"第 1 轮检查 {key}.py 时遗漏了一项边界。"
                            "该遗漏造成对应路径没有验证。"
                        ),
                    }
                repaired["_score_cap"] = {
                    "policy_version": app.EVALUATION_SCORE_CAP_POLICY_VERSION,
                    "max_total": 21,
                    "original_scores": [5, 5, 5, 5, 5],
                    "adjusted_scores": [5, 4, 4, 4, 4],
                    "adjusted_dimensions": list(
                        app.EVALUATION_DIMENSION_KEYS[1:]
                    ),
                    "reason": "依据真实边界事实校准",
                }
                row = app.completed_turn_rows()[0]
                app.persist_completed_turn_evaluation_repair(
                    row,
                    source_sha256,
                    repaired,
                    repaired_dimensions=list(app.EVALUATION_DIMENSION_KEYS[1:]),
                    allow_score_cap_repair=True,
                )
                saved = app.completed_turn_rows()[0]
                job = app.completed_turns()[0]["evaluation_repair"]

        self.assertEqual(queued["queued"], 1)
        self.assertEqual(app.evaluation_total_score(app.turn_evaluation(saved)), 21)
        self.assertEqual(job["status"], "succeeded")


    def test_english_dominant_public_description_is_targeted_for_repair(self):
        evaluation = sample_evaluation()
        evaluation["delivery"]["description"] = (
            "Round one delivered the requested browser workflow with strict "
            "validation, visible results, downloadable output, and verification."
        )
        issues = app.automatic_evaluation_description_repair_issues(evaluation)

        self.assertTrue(any("交付完整性描述主要为英文" in issue for issue in issues))
        self.assertFalse(
            app.evaluation_description_is_english_dominant(
                "第 1 轮在 src/App.tsx 调用 parseTimeline()，随后运行 npm test 完成检查。"
            )
        )

    def test_english_public_description_waits_for_auto_repair_before_solo_qa(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["score_validation_mode"] = "quality_platform_review"
                evaluation["delivery"]["description"] = (
                    "Round one delivered the requested browser workflow with strict "
                    "validation, visible results, downloadable output, and verification."
                )
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": evaluation}, ensure_ascii=False
                    ),
                )

                completed = app.completed_turns()[0]

        self.assertTrue(completed["export_ready"])
        self.assertFalse(completed["solo_qa_ready"])
        self.assertEqual(completed["evaluation_repair"]["status"], "needed")
        self.assertTrue(
            any("交付完整性描述主要为英文" in issue for issue in completed["solo_qa_issues"])
        )

    def test_description_only_repair_keeps_score_and_internal_evidence_out_of_schema(self):
        evaluation = sample_evaluation()
        replacement = "陶坯称重流程在第 1 轮完成交付，页面状态和导出结果都有对应记录。"
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app,
            "run_codex_structured",
            return_value={"description": replacement},
        ) as runner:
            result = app.run_codex_evaluation_description_repair(
                Path(directory),
                "完成陶坯称重流程",
                "STEP 1: 修改 src/App.tsx",
                evaluation,
                "delivery",
                1,
                ["自动检查的交付完整性描述主要为英文"],
                avoidance_history=["#7001 另一条历史交付点评"],
            )

        schema = runner.call_args.args[1]
        self.assertEqual(schema["required"], ["description"])
        self.assertEqual(result, replacement)
        self.assertIn("分数固定为 5 分", runner.call_args.args[0])
        self.assertIn("不得返回或改变分数", runner.call_args.args[0])

    def test_description_repair_keeps_review_fact_with_explicit_source(self):
        evaluation = sample_evaluation()
        evaluation["execution"] = {
            "score": 4,
            "description": "第 1 轮提交了大量依赖文件，造成仓库体积增加。",
        }
        replacement = (
            "后续产物检查显示，第 1 轮提交中有 4803 个依赖文件进入版本控制。"
            "这个遗漏造成仓库体积增加。"
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app,
            "run_codex_structured",
            return_value={"description": replacement},
        ) as runner:
            result = app.run_codex_evaluation_description_repair(
                Path(directory),
                "不要提交依赖目录",
                "ASSISTANT FINAL: 已经完成。",
                evaluation,
                "execution",
                1,
                ["执行能力描述引用后续独立复核证据但没有注明来源"],
                supplemental_evidence="产物盘点发现 4803 个依赖文件进入版本控制",
            )

        self.assertEqual(result, replacement)
        prompt = runner.call_args.args[0]
        self.assertIn("后续独立代码复核证据", prompt)
        self.assertIn("后续产物检查显示", prompt)

    def test_description_repair_rejects_unattributed_review_only_number(self):
        evaluation = sample_evaluation()
        evaluation["execution"] = {
            "score": 4,
            "description": "第 1 轮提交了大量依赖文件，造成仓库体积增加。",
        }
        replacement = (
            "第 1 轮提交中有 4803 个依赖文件进入版本控制。"
            "这个遗漏造成仓库体积增加。"
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app,
            "run_codex_structured",
            return_value={"description": replacement},
        ):
            with self.assertRaisesRegex(app.WorkflowError, "没有注明来源"):
                app.run_codex_evaluation_description_repair(
                    Path(directory),
                    "不要提交依赖目录",
                    "ASSISTANT FINAL: 已经完成。",
                    evaluation,
                    "execution",
                    1,
                    ["执行能力描述引用后续独立复核证据但没有注明来源"],
                    supplemental_evidence="产物盘点发现 4803 个依赖文件进入版本控制",
                )

    def test_completed_turn_queues_missing_review_evidence_attribution_as_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["score_validation_mode"] = "quality_platform_review"
                evaluation["execution"] = {
                    "score": 4,
                    "description": (
                        "第 1 轮提交中有 4803 个依赖文件进入版本控制。"
                        "这个遗漏造成仓库体积增加。"
                    ),
                }
                review = {
                    "quality_gaps": [
                        {"evidence": "产物盘点发现 4803 个依赖文件进入版本控制"}
                    ],
                    "evaluation": evaluation,
                }
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(review, ensure_ascii=False),
                )
                row = app.completed_turn_row("abc123abc123:1")

                repairable, _ = app.completed_turn_repairable_evaluation_issues(row)

        self.assertTrue(
            any("执行能力描述引用后续独立复核证据但没有注明来源" in issue for issue in repairable),
            repairable,
        )

    def test_completed_turn_list_and_delivery_row_use_reviewed_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["other_issues"] = "这段内容只应保留在本地评审记录中。"
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": evaluation}, ensure_ascii=False
                    ),
                )
                summaries = app.completed_turns()
                row = app.delivery_export_row(app.completed_turn_rows()[0])

        self.assertEqual(summaries[0]["key"], "abc123abc123:1")
        self.assertEqual(summaries[0]["project_number"], "0007")
        self.assertEqual(summaries[0]["task_difficulty"], "困难")
        self.assertEqual(summaries[0]["prompt"], "完成真实导出链路")
        self.assertTrue(summaries[0]["export_ready"])
        self.assertEqual(len(row), len(app.DELIVERY_EXPORT_COLUMNS))
        self.assertEqual(row[0], "0007")
        self.assertEqual(row[1], "export-demo")
        self.assertEqual(row[2], "完成真实导出链路")
        self.assertEqual(row[5], 1)
        self.assertEqual(row[11], "2.1.263")
        self.assertEqual(row[16], 5)
        self.assertEqual(row[-2], "")
        self.assertEqual(row[-1], "牛宇航")

    def test_locked_turn_intent_wins_over_reviewed_task_type_everywhere(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["task_type"] = "Feature 迭代"
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": evaluation}, ensure_ascii=False
                    ),
                )

                stored_row = app.completed_turn_rows()[0]
                summary = app.completed_turns()[0]
                export_row = app.delivery_export_row(stored_row)
                solo_values = app.solo_qa_values(stored_row)
                solo_ready, solo_issues = app.solo_qa_readiness(stored_row)

        self.assertEqual(summary["task_type"], "0-1 代码生成")
        self.assertEqual(export_row[13], "0-1 代码生成")
        self.assertEqual(solo_values["任务类型"], "0-1代码生成")
        self.assertTrue(solo_ready, solo_issues)

    def test_manual_evaluation_overrides_export_and_solo_qa_without_replacing_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                original_row = app.completed_turn_rows()[0]
                original_review = original_row["turn_review_result"]
                original_digest = app.solo_qa_payload_sha256(original_row)
                manual = {
                    key: {
                        "score": 5,
                        "description": f"人工逐项核对题面约束后的{label}描述，保留该轮实际结果。",
                    }
                    for key, label in (
                        ("delivery", "交付完整性"),
                        ("instruction_following", "指令遵循"),
                        ("planning", "任务规划"),
                        ("reasoning", "推理能力"),
                        ("execution", "执行能力"),
                    )
                }

                saved = app.save_completed_turn_evaluation(
                    {
                        "turn_key": "abc123abc123:1",
                        "evaluation": manual,
                    }
                )
                effective_row = app.completed_turn_rows()[0]
                export_row = app.delivery_export_row(effective_row)
                solo_values = app.solo_qa_values(effective_row)
                solo_payload = app.solo_qa_turn_payload("abc123abc123:1")
                changed_digest = app.solo_qa_payload_sha256(effective_row)
                with app.db_connection() as database:
                    stored = database.execute(
                        """SELECT review_result, manual_evaluation
                             FROM run_turns
                            WHERE run_id = 'abc123abc123' AND turn_number = 1"""
                    ).fetchone()

                restored = app.save_completed_turn_evaluation(
                    {"turn_key": "abc123abc123:1", "reset": True}
                )
                restored_row = app.completed_turn_rows()[0]

        self.assertTrue(saved["evaluation_overridden"])
        self.assertEqual(saved["evaluation"]["delivery"]["score"], 5)
        self.assertEqual(export_row[16], 5)
        self.assertEqual(export_row[17], manual["delivery"]["description"])
        self.assertEqual(solo_values["执行能力"], 5)
        self.assertEqual(
            solo_values["执行能力 - 描述"], manual["execution"]["description"]
        )
        self.assertEqual(solo_payload["values"]["交付完整性"], 5)
        self.assertEqual(
            solo_payload["values"]["交付完整性 - 描述"],
            manual["delivery"]["description"],
        )
        self.assertNotEqual(changed_digest, original_digest)
        self.assertEqual(stored["review_result"], original_review)
        self.assertEqual(
            json.loads(stored["manual_evaluation"])["planning"]["score"], 5
        )
        self.assertFalse(restored["evaluation_overridden"])
        self.assertEqual(
            app.turn_evaluation(restored_row)["delivery"]["score"],
            sample_evaluation()["delivery"]["score"],
        )

    def test_manual_evaluation_rejects_missing_description_and_invalid_score(self):
        evaluation = {
            key: {"score": 5, "description": "可核对的人工说明。"}
            for key in app.EVALUATION_DIMENSION_KEYS
        }
        evaluation["planning"] = {"score": 6, "description": "超出范围。"}
        with self.assertRaisesRegex(app.WorkflowError, "任务规划分数必须是 1～5"):
            app.normalize_manual_evaluation(evaluation)

        evaluation["planning"] = {"score": 4, "description": "  "}
        with self.assertRaisesRegex(app.WorkflowError, "任务规划描述不能为空"):
            app.normalize_manual_evaluation(evaluation)

    def test_manual_evaluation_save_preserves_draft_but_export_reports_issues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                manual = {
                    key: dict(sample_evaluation()[key])
                    for key in app.EVALUATION_DIMENSION_KEYS
                }
                manual["planning"] = {
                    "score": 4,
                    "description": (
                        "检查 app.py 时遗漏了容器健康状态。"
                        "这导致正式验收没有完成。"
                    ),
                }

                saved = app.save_completed_turn_evaluation(
                    {
                        "turn_key": "abc123abc123:1",
                        "evaluation": manual,
                    }
                )
                saved_row = app.completed_turn_rows()[0]
                policy_issues = app.completed_turn_evaluation_policy_issues(
                    saved_row, app.turn_evaluation(saved_row)
                )

        self.assertTrue(saved["evaluation_overridden"])
        self.assertEqual(
            saved["evaluation"]["planning"]["description"],
            manual["planning"]["description"],
        )
        self.assertTrue(
            any("未写明第 1 轮" in issue for issue in policy_issues),
            policy_issues,
        )

    def test_completed_turn_exposes_grounded_evaluation_repair_as_needed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["planning"] = {
                    "score": 4,
                    "description": "第 1 轮检查 `app.py` 后发现计划仍有遗漏，该遗漏造成了一次返工。",
                }
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": evaluation}, ensure_ascii=False
                    ),
                )

                summary = app.completed_turns()[0]

        repair = summary["evaluation_repair"]
        self.assertEqual(repair["status"], "needed")
        self.assertTrue(repair["can_start"])
        self.assertTrue(repair["repairable_issues"])
        self.assertIn("反引号", repair["repairable_issues"][0])

    def test_solo_qa_returned_description_issue_targets_named_dimensions(self):
        row = {
            "solo_qa_state": "needs_fix",
            "solo_qa_remote_submission_id": "6890",
            "solo_qa_remote_status": "PENDING_FIX",
            "solo_qa_remote_updated_at": "2026-09-13T13:00:47",
            "solo_qa_qc_summary": (
                "描述与轨迹不符（一致性抽检）：【交付完整性】构建命令未找到；"
                "【推理能力】耗时数字未找到。请修改对应维度的描述。"
            ),
        }

        issues = app.solo_qa_returned_evaluation_repair_issues(
            row, sample_evaluation()
        )

        self.assertEqual(len(issues), 2)
        self.assertIn("交付完整性", issues[0])
        self.assertIn("推理能力", issues[1])
        self.assertTrue(all("SOLO-QA 打回原文" in issue for issue in issues))

    def test_solo_qa_abbreviated_multi_dimension_uses_synced_hit_fields(self):
        row = {
            "solo_qa_state": "needs_fix",
            "solo_qa_remote_submission_id": "8060",
            "solo_qa_remote_status": "PENDING_FIX",
            "solo_qa_remote_updated_at": "2026-09-13T15:56:45",
            "solo_qa_qc_summary": (
                "执行能力描述等 3 个维度的描述与已交付数据 #8018 重复，"
                "命中公共长片段。"
            ),
        }

        with mock.patch.object(
            app,
            "solo_qa_remote_duplicate_dimensions",
            return_value=["planning", "reasoning", "execution"],
        ):
            issues = app.solo_qa_returned_evaluation_repair_issues(
                row, sample_evaluation()
            )

        self.assertEqual(len(issues), 3)
        self.assertIn("执行能力", issues[0])
        self.assertIn("任务规划", issues[1])
        self.assertIn("推理能力", issues[2])
        self.assertTrue(all("描述与历史点评高度重复" in issue for issue in issues))

    def test_solo_qa_full_score_consistency_targets_only_leading_dimension(self):
        row = {
            "solo_qa_state": "needs_fix",
            "solo_qa_remote_submission_id": "16010",
            "solo_qa_remote_status": "PENDING_FIX",
            "solo_qa_remote_updated_at": "2026-09-16T00:40:00",
            "solo_qa_qc_summary": (
                "指令遵循：指令遵循给了满分，但交付完整性、推理能力和"
                "执行能力三段都指出，请求校验曾漏报多个字段错误。"
            ),
        }
        evaluation = sample_evaluation()
        evaluation["delivery"]["score"] = 4
        evaluation["reasoning"]["score"] = 4
        evaluation["execution"]["score"] = 3

        issues = app.solo_qa_returned_evaluation_repair_issues(row, evaluation)

        self.assertEqual(len(issues), 1)
        self.assertIn("指令遵循满分与跨维度事实不一致", issues[0])

    def test_solo_qa_spelling_return_rewrites_all_five_descriptions(self):
        row = {
            "solo_qa_state": "needs_fix",
            "solo_qa_remote_submission_id": "9250",
            "solo_qa_remote_status": "PENDING_FIX",
            "solo_qa_remote_updated_at": "2026-09-13T22:43:13",
            "solo_qa_qc_summary": "五段描述中检出 1 个错别字",
        }

        issues = app.solo_qa_returned_evaluation_repair_issues(
            row, sample_evaluation()
        )

        self.assertEqual(len(issues), 5)
        self.assertTrue(all("描述包含错别字" in issue for issue in issues))

    def test_solo_qa_environment_attribution_return_targets_named_dimension(self):
        row = {
            "solo_qa_state": "needs_fix",
            "solo_qa_remote_submission_id": "11975",
            "solo_qa_remote_status": "PENDING_FIX",
            "solo_qa_remote_updated_at": "2026-09-14T20:30:00",
            "solo_qa_qc_summary": (
                "任务规划：这段描述把扣分点归因为运行环境里没有 Docker，"
                "属于环境限制，不能作为该维度的扣分理由。"
            ),
        }

        issues = app.solo_qa_returned_evaluation_repair_issues(
            row, sample_evaluation()
        )

        self.assertEqual(len(issues), 1)
        self.assertIn("任务规划", issues[0])
        self.assertIn("环境条件", issues[0])

    def test_solo_qa_review_evidence_attribution_return_targets_named_dimensions(self):
        row = {
            "solo_qa_state": "needs_fix",
            "solo_qa_remote_submission_id": "13001",
            "solo_qa_remote_status": "PENDING_FIX",
            "solo_qa_remote_updated_at": "2026-09-15T16:00:00",
            "solo_qa_qc_summary": (
                "指令遵循、任务规划和执行能力写了 .venv 有 2679 个文件、"
                "Git pack 增加约 13.05 MiB，这些数字只存在于后续代码复核材料中，"
                "来源没有明确区分。"
            ),
        }

        issues = app.solo_qa_returned_evaluation_repair_issues(
            row, sample_evaluation()
        )

        self.assertEqual(len(issues), 3)
        self.assertTrue(
            all("引用后续独立复核证据但没有注明来源" in issue for issue in issues),
            issues,
        )

    def test_solo_qa_conflicting_check_counts_rewrite_all_five(self):
        row = {
            "solo_qa_state": "needs_fix",
            "solo_qa_remote_submission_id": "11974",
            "solo_qa_remote_status": "PENDING_FIX",
            "solo_qa_remote_updated_at": "2026-09-14T20:30:00",
            "solo_qa_qc_summary": (
                "交付完整性把环境限制作为扣分理由；整体：执行能力写 6 项通过，"
                "其余四段写 35 项通过，同一份验收统计出现互斥的数字。"
            ),
        }

        issues = app.solo_qa_returned_evaluation_repair_issues(
            row, sample_evaluation()
        )

        self.assertEqual(len(issues), 5)
        self.assertTrue(all("统一验收统计" in issue for issue in issues))

    def test_solo_qa_return_marker_suppresses_only_the_same_rejection(self):
        row = {
            "solo_qa_state": "needs_fix",
            "solo_qa_remote_submission_id": "8047",
            "solo_qa_remote_status": "PENDING_FIX",
            "solo_qa_remote_updated_at": "2026-09-13T15:56:49",
            "solo_qa_qc_summary": "交付完整性满分描述包含返工负面事实。",
        }
        evaluation = sample_evaluation()
        evaluation["_solo_qa_repair_qc_sha256"] = (
            app.solo_qa_returned_evaluation_fingerprint(row)
        )

        self.assertEqual(
            app.solo_qa_returned_evaluation_repair_issues(row, evaluation), []
        )
        row["solo_qa_remote_updated_at"] = "2026-09-13T16:00:00"
        self.assertEqual(
            len(app.solo_qa_returned_evaluation_repair_issues(row, evaluation)),
            1,
        )

    def test_evaluation_repair_queue_is_idempotent_for_same_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "schedule_evaluation_repair") as schedule:
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["planning"] = {
                    "score": 4,
                    "description": "第 1 轮检查 `app.py` 后发现计划仍有遗漏，该遗漏造成了一次返工。",
                }
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": evaluation}, ensure_ascii=False
                    ),
                )

                first = app.queue_completed_turn_evaluation_repairs(
                    {"turn_keys": ["abc123abc123:1"]}
                )
                second = app.queue_completed_turn_evaluation_repairs(
                    {"turn_keys": ["abc123abc123:1"]}
                )

        self.assertEqual(first["queued"], 1)
        self.assertEqual(second["queued"], 0)
        self.assertEqual(second["results"][0]["status"], "queued")
        schedule.assert_called_once()

    def test_evaluation_repair_queue_rejects_a_stale_request_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "schedule_evaluation_repair") as schedule:
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["planning"] = {
                    "score": 4,
                    "description": "第 1 轮检查 `app.py` 后发现计划仍有遗漏，该遗漏造成了一次返工。",
                }
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": evaluation}, ensure_ascii=False
                    ),
                )
                stale_row = app.completed_turn_rows()[0]
                changed = json.loads(json.dumps(evaluation, ensure_ascii=False))
                changed["planning"]["description"] = (
                    "第 1 轮另一份检查计划仍有遗漏。这个遗漏造成了一次返工。"
                )
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": changed}, ensure_ascii=False
                    ),
                )

                with mock.patch.object(
                    app, "completed_turn_rows", return_value=[stale_row]
                ):
                    result = app.queue_completed_turn_evaluation_repairs(
                        {"turn_keys": ["abc123abc123:1"]}
                    )
                with app.db_connection() as database:
                    job = database.execute(
                        "SELECT status FROM evaluation_repair_jobs"
                    ).fetchone()

        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["results"][0]["status"], "stale")
        self.assertIsNone(job)
        schedule.assert_not_called()

    def test_evaluation_repair_job_can_only_be_claimed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "schedule_evaluation_repair"):
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["planning"] = {
                    "score": 4,
                    "description": "第 1 轮检查 `app.py` 后发现计划仍有遗漏，该遗漏造成了一次返工。",
                }
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": evaluation}, ensure_ascii=False
                    ),
                )
                app.queue_completed_turn_evaluation_repairs(
                    {"turn_keys": ["abc123abc123:1"]}
                )
                source_sha256 = app.completed_turn_row(
                    "abc123abc123:1"
                )["evaluation_repair_source_sha256"]

                first = app.claim_evaluation_repair_job(
                    "abc123abc123:1", source_sha256
                )
                second = app.claim_evaluation_repair_job(
                    "abc123abc123:1", source_sha256
                )

        self.assertTrue(first)
        self.assertFalse(second)

    def test_evaluation_repair_worker_retries_one_transient_504(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "schedule_evaluation_repair"):
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["planning"] = {
                    "score": 4,
                    "description": "第 1 轮检查 `app.py` 后发现计划仍有遗漏，该遗漏造成了一次返工。",
                }
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": evaluation}, ensure_ascii=False
                    ),
                )
                app.queue_completed_turn_evaluation_repairs(
                    {"turn_keys": ["abc123abc123:1"]}
                )
                source_sha256 = app.completed_turn_row(
                    "abc123abc123:1"
                )["evaluation_repair_source_sha256"]
                repaired_description = (
                    "“完成真实导出链路”在第 1 轮第 1 步规划时没有拆分提交前检查，"
                    "导致最终回复前缺少阶段记录。保存的最终回复显示“已经完成”，"
                    "因此该遗漏只造成过程依据不完整，没有影响最终结果。"
                )
                with mock.patch.object(
                    app,
                    "run_codex_evaluation_description_repair",
                    side_effect=[
                        app.WorkflowError("API Error: 504 Gateway Time-out"),
                        repaired_description,
                    ],
                ) as repair, mock.patch.object(app.time, "sleep"):
                    app.evaluation_repair_worker(
                        "abc123abc123:1", source_sha256
                    )
                row = app.completed_turn_row("abc123abc123:1")

        self.assertEqual(repair.call_count, 2)
        self.assertEqual(row["evaluation_repair_job_status"], "succeeded")
        self.assertIn(
            "第 1 轮第 1 步规划",
            app.turn_evaluation(row, clean_description_markup=False)["planning"][
                "description"
            ],
        )

    def test_evaluation_repair_recovery_rebases_a_changed_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "schedule_evaluation_repair") as schedule:
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["planning"] = {
                    "score": 4,
                    "description": "第 1 轮检查 `app.py` 后发现计划仍有遗漏，该遗漏造成了一次返工。",
                }
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": evaluation}, ensure_ascii=False
                    ),
                )
                app.queue_completed_turn_evaluation_repairs(
                    {"turn_keys": ["abc123abc123:1"]}
                )
                old_source = app.completed_turn_row(
                    "abc123abc123:1"
                )["evaluation_repair_source_sha256"]
                with app.db_connection() as database:
                    database.execute(
                        """UPDATE run_turns SET result = '服务重启前补充的真实结果'
                             WHERE run_id = 'abc123abc123' AND turn_number = 1"""
                    )
                schedule.reset_mock()

                recovered = app.recover_evaluation_repair_jobs()
                row = app.completed_turn_row("abc123abc123:1")

        self.assertEqual(recovered, 1)
        self.assertEqual(row["evaluation_repair_job_status"], "queued")
        self.assertNotEqual(row["evaluation_repair_source_sha256"], old_source)
        schedule.assert_called_once_with(
            "abc123abc123:1", row["evaluation_repair_source_sha256"]
        )

    def test_evaluation_repair_does_not_invent_a_missing_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "schedule_evaluation_repair") as schedule:
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["planning"] = {
                    "score": 4,
                    "description": "第 1 轮检查 `app.py` 后发现计划仍有遗漏，该遗漏造成了一次返工。",
                }
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": evaluation}, ensure_ascii=False
                    ),
                    trajectory_path=str(root / "missing.jsonl"),
                )

                result = app.queue_completed_turn_evaluation_repairs(
                    {"turn_keys": ["abc123abc123:1"]}
                )

        self.assertEqual(result["queued"], 0)
        self.assertEqual(result["results"][0]["status"], "skipped")
        self.assertIn("轨迹文件不存在", result["results"][0]["message"])
        schedule.assert_not_called()

    def test_evaluation_repair_worker_saves_then_rechecks_but_keeps_real_blockers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "schedule_evaluation_repair"):
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["planning"] = {
                    "score": 4,
                    "description": "第 1 轮检查 `app.py` 后发现计划仍有遗漏，该遗漏造成了一次返工。",
                }
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": evaluation}, ensure_ascii=False
                    ),
                )
                with app.db_connection() as database:
                    database.execute(
                        "UPDATE runs SET snapshot_url = '' WHERE id = 'abc123abc123'"
                    )
                queued = app.queue_completed_turn_evaluation_repairs(
                    {"turn_keys": ["abc123abc123:1"]}
                )
                source_sha256 = queued["results"][0] and app.completed_turn_row(
                    "abc123abc123:1"
                )["evaluation_repair_source_sha256"]
                repaired_description = (
                    "“完成真实导出链路”在第 1 轮第 1 步规划时没有拆分提交前检查，"
                    "导致最终回复前缺少阶段记录。保存的最终回复显示“已经完成”，"
                    "因此该遗漏只造成过程依据不完整，没有影响最终结果。"
                )
                with mock.patch.object(
                    app,
                    "run_codex_evaluation_description_repair",
                    return_value=repaired_description,
                ):
                    app.evaluation_repair_worker(
                        "abc123abc123:1", source_sha256
                    )
                summary = app.completed_turns()[0]

        self.assertEqual(summary["evaluation_repair"]["status"], "succeeded")
        self.assertNotIn(
            "自动检查的任务规划非满分描述没有把不足定位到具体步骤、文件、函数、接口或报错",
            summary["export_issues"],
        )
        self.assertIn("初始环境快照不是 GitHub Commit 地址", summary["export_issues"])
        self.assertFalse(summary["export_ready"])

    def test_running_evaluation_repair_never_overwrites_new_manual_score(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "schedule_evaluation_repair"):
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["planning"] = {
                    "score": 4,
                    "description": "第 1 轮检查 `app.py` 后发现计划仍有遗漏，该遗漏造成了一次返工。",
                }
                original_review = json.dumps(
                    {"evaluation": evaluation}, ensure_ascii=False
                )
                app.update_turn(
                    "abc123abc123", 1, review_result=original_review
                )
                queued = app.queue_completed_turn_evaluation_repairs(
                    {"turn_keys": ["abc123abc123:1"]}
                )
                source_sha256 = app.completed_turn_row(
                    "abc123abc123:1"
                )["evaluation_repair_source_sha256"]

                def add_manual_override(*_args, **_kwargs):
                    manual = {
                        key: dict(sample_evaluation()[key])
                        for key in app.EVALUATION_DIMENSION_KEYS
                    }
                    with app.db_connection() as database:
                        database.execute(
                            """UPDATE run_turns SET manual_evaluation = ?
                                 WHERE run_id = 'abc123abc123' AND turn_number = 1""",
                            (json.dumps(manual, ensure_ascii=False),),
                        )
                    return sample_evaluation()

                with mock.patch.object(
                    app,
                    "run_codex_evaluation_description_repair",
                    side_effect=add_manual_override,
                ):
                    app.evaluation_repair_worker(
                        "abc123abc123:1", source_sha256
                    )
                row = app.completed_turn_row("abc123abc123:1")

        self.assertEqual(row["turn_review_result"], original_review)
        self.assertTrue(app.turn_manual_evaluation(row))
        self.assertEqual(queued["queued"], 1)

    def test_completed_evaluation_prose_repair_cannot_change_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "schedule_evaluation_repair"):
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["planning"] = {
                    "score": 4,
                    "description": "第 1 轮检查 `app.py` 后发现计划仍有遗漏，该遗漏造成了一次返工。",
                }
                original_review = json.dumps(
                    {"evaluation": evaluation}, ensure_ascii=False
                )
                app.update_turn(
                    "abc123abc123", 1, review_result=original_review
                )
                app.queue_completed_turn_evaluation_repairs(
                    {"turn_keys": ["abc123abc123:1"]}
                )
                source_sha256 = app.completed_turn_row(
                    "abc123abc123:1"
                )["evaluation_repair_source_sha256"]
                row = app.completed_turn_row("abc123abc123:1")
                self.assertTrue(
                    app.claim_evaluation_repair_job(
                        "abc123abc123:1", source_sha256
                    )
                )
                changed_score = json.loads(json.dumps(evaluation, ensure_ascii=False))
                changed_score["planning"]["score"] = 5
                changed_score["planning"]["description"] = (
                    "第 1 轮检查 app.py 后完成计划核对，最终结果已有记录。"
                )
                with self.assertRaisesRegex(app.WorkflowError, "不能改变原分数"):
                    app.persist_completed_turn_evaluation_repair(
                        row,
                        source_sha256,
                        changed_score,
                        repaired_dimensions=["planning"],
                    )
                row = app.completed_turn_row("abc123abc123:1")

        self.assertEqual(row["turn_review_result"], original_review)
        self.assertEqual(row["evaluation_repair_job_status"], "running")

    def test_returned_full_score_consistency_repair_can_lower_named_dimension(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "schedule_evaluation_repair"):
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["delivery"]["score"] = 4
                evaluation["reasoning"]["score"] = 4
                evaluation["execution"]["score"] = 3
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": evaluation}, ensure_ascii=False
                    ),
                )
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO solo_qa_submissions(
                               run_id, turn_number, remote_submission_id,
                               remote_status, state, qc_summary,
                               created_at, updated_at
                             ) VALUES (?, 1, '16010', 'PENDING_FIX',
                               'needs_fix', ?, ?, ?)""",
                        (
                            "abc123abc123",
                            "指令遵循：指令遵循给了满分，但交付完整性、推理能力和"
                            "执行能力三段都指出，请求校验曾漏报多个字段错误。",
                            timestamp,
                            timestamp,
                        ),
                    )
                queued = app.queue_completed_turn_evaluation_repairs(
                    {"turn_keys": ["abc123abc123:1"]}, schedule_jobs=False
                )
                row = app.completed_turn_row("abc123abc123:1")
                source_sha256 = row["evaluation_repair_source_sha256"]
                self.assertTrue(
                    app.claim_evaluation_repair_job(
                        "abc123abc123:1", source_sha256
                    )
                )
                repaired = json.loads(json.dumps(evaluation, ensure_ascii=False))
                repaired["instruction_following"] = {
                    "score": 4,
                    "description": (
                        "真实接口检查最终通过，但第 1 轮请求校验曾漏报同批字段错误。"
                        "该遗漏造成校验控制流返工。"
                    ),
                }
                with mock.patch.object(
                    app,
                    "completed_description_repair_candidate_policy_issues",
                    return_value=[],
                ):
                    app.persist_completed_turn_evaluation_repair(
                        row,
                        source_sha256,
                        repaired,
                        repaired_dimensions=["instruction_following"],
                        allowed_score_repair_dimensions=["instruction_following"],
                    )
                saved = app.completed_turn_row("abc123abc123:1")

        self.assertEqual(queued["queued"], 1)
        self.assertEqual(
            app.turn_evaluation(saved)["instruction_following"]["score"], 4
        )
        self.assertEqual(saved["evaluation_repair_job_status"], "succeeded")

    def test_completed_evaluation_repair_cas_stops_after_remote_qc_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "schedule_evaluation_repair"):
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["planning"] = {
                    "score": 4,
                    "description": "第 1 轮检查 `app.py` 后发现计划仍有遗漏，该遗漏造成了一次返工。",
                }
                original_review = json.dumps(
                    {"evaluation": evaluation}, ensure_ascii=False
                )
                app.update_turn(
                    "abc123abc123", 1, review_result=original_review
                )
                app.queue_completed_turn_evaluation_repairs(
                    {"turn_keys": ["abc123abc123:1"]}
                )
                row = app.completed_turn_row("abc123abc123:1")
                source_sha256 = row["evaluation_repair_source_sha256"]
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO solo_qa_submissions(
                               run_id, turn_number, state, created_at, updated_at
                             ) VALUES ('abc123abc123', 1, 'qc_pending', ?, ?)""",
                        (app.now_text(), app.now_text()),
                    )

                with self.assertRaisesRegex(app.WorkflowError, "远端质检"):
                    app.persist_completed_turn_evaluation_repair(
                        row, source_sha256, sample_evaluation()
                    )
                unchanged = app.completed_turn_row("abc123abc123:1")

        self.assertEqual(unchanged["turn_review_result"], original_review)

    def test_export_rejects_nonfull_description_without_turn_number(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["planning"] = {
                    "score": 4,
                    "description": (
                        "页面展示检查计划。"
                        "列表展示当前项目。"
                    ),
                }
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": evaluation}, ensure_ascii=False
                    ),
                )

                summary = app.completed_turns()[0]

        self.assertFalse(summary["export_ready"])
        self.assertTrue(
            any("未写明第 1 轮" in issue for issue in summary["export_issues"]),
            summary["export_issues"],
        )

    def test_hourly_output_counts_complete_turns_and_keeps_soft_hidden_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                with app.db_connection() as database:
                    database.execute(
                        """UPDATE run_turns
                              SET updated_at = '2026-09-10 09:12:00 +0800',
                                  export_deleted_at = '2026-09-10 10:00:00 +0800'
                            WHERE run_id = 'abc123abc123' AND turn_number = 1"""
                    )
                    database.execute(
                        """INSERT INTO run_turns(
                             run_id, turn_number, intent_type, prompt, status,
                             created_at, updated_at
                           ) VALUES
                             ('abc123abc123', 2, 'Bug 修复', '修复问题', 'complete',
                              '2026-09-10 09:30:00 +0800', '2026-09-10 09:45:00 +0800'),
                             ('abc123abc123', 3, 'Feature 迭代', '增加功能', 'complete',
                              '2026-09-10 14:00:00 +0800', '2026-09-10 14:20:00 +0800'),
                             ('abc123abc123', 4, 'Bug 修复', '尚未完成', 'running',
                              '2026-09-10 15:00:00 +0800', '2026-09-10 15:20:00 +0800'),
                             ('abc123abc123', 5, 'Bug 修复', '其他日期', 'complete',
                              '2026-09-09 09:00:00 +0800', '2026-09-09 09:20:00 +0800')"""
                    )
                result = app.hourly_output_analytics("2026-09-10")

        self.assertEqual(len(result["hours"]), 24)
        self.assertEqual(result["summary"]["completed_turns"], 3)
        self.assertEqual(result["summary"]["active_hours"], 2)
        self.assertEqual(result["summary"]["peak_count"], 2)
        self.assertEqual(result["summary"]["peak_hours"], ["09:00–10:00"])
        self.assertEqual(result["hours"][9]["total"], 2)
        self.assertEqual(result["hours"][9]["by_task_type"]["0-1 代码生成"], 1)
        self.assertEqual(result["hours"][9]["by_task_type"]["Bug 修复"], 1)
        self.assertEqual(result["hours"][14]["by_task_type"]["Feature 迭代"], 1)

    def test_hourly_output_rejects_invalid_date(self):
        with self.assertRaisesRegex(app.WorkflowError, "YYYY-MM-DD"):
            app.hourly_output_analytics("2026-09-40")

    def test_active_iteration_generation_is_exposed_as_a_read_only_list_row(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO iteration_jobs(
                             source_run_id, baseline_run_id, lineage_origin_run_id,
                             task_type, auto_refill, status, stage, recovery_count,
                             started_at, updated_at
                           ) VALUES (
                             'abc123abc123', 'abc123abc123', 'abc123abc123',
                             'Bug 修复', 1, 'generating', '生成候选 1/2', 0,
                             '2026-09-10 20:00:00 +0800', '2026-09-10 20:01:00 +0800'
                           )"""
                    )
                rows = app.active_background_generation_rows()

        row = next(item for item in rows if item["source_run_id"] == "abc123abc123")
        self.assertTrue(row["background_generation"])
        self.assertEqual(row["project_number"], "待创建")
        self.assertEqual(row["source_project_number"], "0007")
        self.assertEqual(row["phase"], "iteration_generation_running")
        self.assertEqual(row["status_detail"], "Bug 修复题面 · 生成候选 1/2")
        self.assertEqual(row["turn_label"], "等待创建")

    def test_completed_turn_with_missing_evidence_is_visible_but_not_exportable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                app.update_turn("abc123abc123", 1, commit_sha=None)
                summary = app.completed_turns()[0]
                with self.assertRaisesRegex(app.WorkflowError, "缺少完整 Git Commit"):
                    app.build_completed_turns_xlsx(["abc123abc123:1"])

        self.assertFalse(summary["export_ready"])
        self.assertIn("缺少完整 Git Commit", summary["export_issues"])

    def test_saved_legacy_evaluation_is_revalidated_before_export_and_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["execution"]["description"] = (
                    "领域测试通过，随后执行 `Docker-Compose CONFIG --quiet` 检查配置。"
                )
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": evaluation}, ensure_ascii=False
                    ),
                )

                summary = app.completed_turns()[0]
                with self.assertRaisesRegex(
                    app.WorkflowError, "docker-compose config --quiet"
                ):
                    app.solo_qa_turn_payload("abc123abc123:1")

        self.assertFalse(summary["export_ready"])
        self.assertTrue(
            any(
                "docker-compose config --quiet" in issue
                for issue in summary["export_issues"]
            )
        )

    def test_saved_evaluation_with_untraced_command_is_not_exportable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["delivery"]["description"] = (
                    "隔离副本执行 `npm ci` 后得到全部测试通过。"
                )
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": evaluation}, ensure_ascii=False
                    ),
                )

                summary = app.completed_turns()[0]

        self.assertFalse(summary["export_ready"])
        self.assertIn(
            "交付完整性描述引用了本轮轨迹中未执行的命令：npm ci",
            summary["export_issues"],
        )

    def test_completed_turn_delete_is_recoverable_and_preserves_run_and_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                repo = self.insert_completed_turn(root)
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO run_turns(
                             run_id, turn_number, intent_type, prompt, status,
                             created_at, updated_at
                           ) VALUES ('abc123abc123', 2, 'Bug 修复', '修复问题',
                                     'complete', ?, ?)""",
                        (timestamp, timestamp),
                    )

                deleted = app.set_completed_turns_export_deleted(
                    ["abc123abc123:1", "abc123abc123:2"]
                )
                self.assertEqual(deleted["changed"], 2)
                self.assertTrue(deleted["evidence_preserved"])
                self.assertEqual(app.completed_turns(), [])
                self.assertEqual(len(app.all_runs()), 1)
                self.assertTrue(repo.is_dir())
                with app.db_connection() as database:
                    stored = database.execute(
                        """SELECT COUNT(*) AS total,
                                  COUNT(export_deleted_at) AS hidden
                             FROM run_turns WHERE run_id = 'abc123abc123'"""
                    ).fetchone()
                self.assertEqual(dict(stored), {"total": 2, "hidden": 2})

                restored = app.set_completed_turns_export_deleted(
                    ["abc123abc123:1", "abc123abc123:2"], deleted=False
                )
                self.assertEqual(restored["changed"], 2)
                self.assertEqual(len(app.completed_turns()), 2)

    def test_preflight_matches_session_prompt_turn_harness_and_raw_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                result = app.preflight_completed_turns(["abc123abc123:1"])

        self.assertEqual(result["summary"], {"total": 1, "passed": 1, "warning": 0, "failed": 0})
        self.assertEqual(result["eligible_keys"], ["abc123abc123:1"])
        self.assertTrue(all(result["results"][0]["checks"].values()))

    def test_completed_turn_list_reuses_cache_and_invalidates_on_row_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                with mock.patch.object(
                    app, "_completed_turn_record", wraps=app._completed_turn_record
                ) as build_record:
                    first = app.completed_turns()
                    second = app.completed_turns()
                    self.assertEqual(build_record.call_count, 1)
                    self.assertEqual(first, second)

                    with app.db_connection() as database:
                        database.execute(
                            """UPDATE run_turns SET model = 'gpt-5.6-sol-cache-test'
                               WHERE run_id = 'abc123abc123' AND turn_number = 1"""
                        )
                    changed = app.completed_turns()

                self.assertEqual(build_record.call_count, 2)
                self.assertEqual(changed[0]["model"], "gpt-5.6-sol-cache-test")

    def test_preflight_hashes_trajectory_fresh_even_when_list_cache_is_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                self.assertEqual(len(app.completed_turns()), 1)
                row = app.completed_turn_rows()[0]
                trajectory_path = Path(str(row["turn_trajectory_path"]))
                original_stat = trajectory_path.stat()
                changed_content = trajectory_path.read_text(encoding="utf-8").replace(
                    "已经完成。", "已经失效。"
                )
                trajectory_path.write_text(changed_content, encoding="utf-8")
                os.utime(
                    trajectory_path,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
                changed_stat = trajectory_path.stat()
                self.assertEqual(changed_stat.st_size, original_stat.st_size)
                self.assertEqual(changed_stat.st_mtime_ns, original_stat.st_mtime_ns)

                result = app.preflight_completed_turns(["abc123abc123:1"])

        blockers = result["results"][0]["blockers"]
        self.assertIn("轨迹文件摘要不匹配", blockers)
        self.assertIn("轨迹文件 SHA-256 与数据库记录不一致", blockers)

    def test_preflight_completed_turn_remains_eligible_while_next_turn_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                raw_trace = Path(
                    str(app.run_row("abc123abc123")["trajectory_path"])
                )
                app.export_turn_checkpoint(
                    "abc123abc123", 1, source_trace=raw_trace
                )
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO run_turns(
                             run_id, turn_number, intent_type, prompt, status,
                             created_at, updated_at
                           ) VALUES (
                             'abc123abc123', 2, 'Bug 修复', '修复已确认问题',
                             'running', ?, ?
                           )""",
                        (timestamp, timestamp),
                    )
                    database.execute(
                        """UPDATE runs SET phase = 'second_running'
                            WHERE id = 'abc123abc123'"""
                    )

                stored_raw_trace = Path(
                    str(app.run_row("abc123abc123")["trajectory_path"])
                )
                result = app.preflight_completed_turns(["abc123abc123:1"])
                payload = app.solo_qa_turn_payload("abc123abc123:1")

        self.assertEqual(stored_raw_trace, raw_trace)
        self.assertEqual(
            result["summary"],
            {"total": 1, "passed": 1, "warning": 0, "failed": 0},
        )
        self.assertEqual(result["eligible_keys"], ["abc123abc123:1"])
        self.assertEqual(result["results"][0]["status"], "passed")
        self.assertTrue(result["results"][0]["eligible"])
        self.assertTrue(result["results"][0]["checks"]["raw_trace_preserved"])
        self.assertEqual(result["results"][0]["blockers"], [])
        self.assertEqual(payload["trajectory"]["name"], "turn-01.jsonl")

    def test_preflight_rejects_prompt_id_that_does_not_match_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                app.update_turn("abc123abc123", 1, prompt_id="wrong-prompt")
                result = app.preflight_completed_turns(["abc123abc123:1"])

        turn = result["results"][0]
        self.assertEqual(turn["status"], "failed")
        self.assertFalse(turn["eligible"])
        self.assertIn(
            "PromptID 无法唯一定位到本轮完整 User Prompt",
            turn["blockers"],
        )

    def test_preflight_requires_original_trace_directory_even_with_turn_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                row = app.completed_turn_rows()[0]
                Path(row["run_trajectory_path"]).unlink()
                result = app.preflight_completed_turns(["abc123abc123:1"])

        self.assertIn(
            "没有保留 projects/-workspace 下的原始完整轨迹",
            result["results"][0]["blockers"],
        )

    @unittest.skipUnless(
        app.ARTIFACT_NODE_EXECUTABLE.is_file() and app.ARTIFACT_NODE_MODULES.is_dir(),
        "bundled spreadsheet runtime is unavailable",
    )
    def test_selected_completed_turn_exports_a_real_xlsx(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                content, filename = app.build_completed_turns_xlsx(["abc123abc123:1"])

        self.assertTrue(filename.startswith("completed-turns-"))
        self.assertTrue(filename.endswith(".xlsx"))
        self.assertTrue(content.startswith(b"PK"))
        self.assertGreater(len(content), 5000)

    def test_excel_values_are_protected_from_formula_injection(self):
        self.assertEqual(app.excel_safe_value("=HYPERLINK(\"bad\")"), "'=HYPERLINK(\"bad\")")
        self.assertEqual(app.excel_safe_value("@SUM(A1:A2)"), "'@SUM(A1:A2)")
        self.assertEqual(app.excel_safe_value(4), 4)

    def test_solo_qa_payload_maps_reviewed_fields_and_verified_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["other_issues"] = "这段内容不得提交到 SOLO-QA。"
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": evaluation}, ensure_ascii=False
                    ),
                )
                payload = app.solo_qa_turn_payload("abc123abc123:1")

        self.assertEqual(payload["values"]["任务类型"], "0-1代码生成")
        self.assertEqual(payload["values"]["SessionID"], "session-export")
        self.assertEqual(payload["values"]["TurnID/PromptID"], "prompt-export")
        self.assertEqual(payload["values"]["当前对话轮次排序"], 1)
        self.assertEqual(payload["values"]["交付完整性"], 5)
        self.assertEqual(payload["values"]["其他问题"], "")
        self.assertEqual(len(payload["payload_sha256"]), 64)
        self.assertEqual(payload["trajectory"]["name"], "turn-01.jsonl")

    def test_solo_qa_state_is_saved_and_detects_later_local_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                payload = app.solo_qa_turn_payload("abc123abc123:1")
                saved = app.record_solo_qa_state({
                    "turn_key": "abc123abc123:1",
                    "state": "qc_pending",
                    "remote_id": "42",
                    "remote_status": "SUBMITTED",
                    "payload_sha256": payload["payload_sha256"],
                    "submitted_at": "2026-09-10 12:00:00 +0800",
                })
                changed_evaluation = sample_evaluation()
                changed_evaluation["delivery"]["description"] = (
                    "人工逐项核对题面约束后调整了描述，验收记录保持不变。"
                )
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": changed_evaluation}, ensure_ascii=False
                    ),
                )
                changed = app.completed_turns()[0]["solo_qa"]

        self.assertEqual(saved["state"], "qc_pending")
        self.assertEqual(saved["remote_id"], "42")
        self.assertEqual(changed["state"], "local_changed")
        self.assertTrue(changed["payload_changed"])

    def test_failed_solo_qa_repair_keeps_previous_remote_payload_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                original = app.solo_qa_turn_payload("abc123abc123:1")
                app.record_solo_qa_state({
                    "turn_key": "abc123abc123:1",
                    "state": "qc_pending",
                    "remote_id": "42",
                    "remote_status": "SUBMITTED",
                    "payload_sha256": original["payload_sha256"],
                })
                changed_evaluation = sample_evaluation()
                changed_evaluation["delivery"]["description"] = (
                    "本轮逐项核对了题面约束并完成项目验收，库存卡片交付结果已有对应记录。"
                )
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps(
                        {"evaluation": changed_evaluation}, ensure_ascii=False
                    ),
                )
                changed_payload = app.solo_qa_turn_payload("abc123abc123:1")
                app.record_solo_qa_state({
                    "turn_key": "abc123abc123:1",
                    "state": "needs_fix",
                    "remote_id": "42",
                    "remote_status": "PENDING_FIX",
                    "payload_sha256": changed_payload["payload_sha256"],
                    "error": "502 Bad Gateway",
                })
                row = app.completed_turn_rows()[0]
                summary = app.completed_turns()[0]["solo_qa"]

        self.assertEqual(row["solo_qa_payload_sha256"], original["payload_sha256"])
        self.assertEqual(summary["state"], "local_changed")
        self.assertTrue(summary["payload_changed"])

    def test_solo_qa_sync_matches_session_and_turn_and_marks_remote_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                result = app.sync_solo_qa_submissions({
                    "items": [{
                        "id": 77,
                        "status": "QC_PASSED",
                        "session_id": "session-export",
                        "turn_id": "prompt-export",
                        "round_no": 1,
                        "user_prompt": "完成陶坯称重核对并保留批次证据",
                        "repo_url": "https://github.com/example/export-demo.git",
                        "repo_name": "export-demo",
                        "task_type": "0-1 代码生成",
                        "qc_summary": "质检通过",
                        "submitted_at": "2026-09-10 12:00:00 +0800",
                    }],
                    "complete": True,
                })
                synced = app.completed_turns()[0]["solo_qa"]
                missing_result = app.sync_solo_qa_submissions({
                    "items": [], "complete": True
                })
                missing = app.completed_turns()[0]["solo_qa"]

        self.assertEqual(result["matched"], 1)
        self.assertEqual(synced["state"], "qc_passed")
        self.assertEqual(synced["remote_id"], "77")
        self.assertEqual(missing_result["remote_missing"], 1)
        self.assertEqual(missing["state"], "remote_missing")

    def test_solo_qa_sync_saves_bounded_remote_evaluation_history_in_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                result = app.sync_solo_qa_submissions({
                    "items": [{
                        "id": 91,
                        "status": "QC_PASSED",
                        "session_id": "session-export",
                        "turn_id": "prompt-export",
                        "round_no": 1,
                        "user_prompt": "完成陶坯称重核对并保留批次证据",
                        "repo_url": "https://github.com/example/export-demo.git",
                        "repo_name": "export-demo",
                        "task_type": "0-1 代码生成",
                        "delivery": {
                            "score": 5,
                            "description": "陶坯称重核对在第 1 轮完成并留下验收记录。",
                        },
                        "instruction": {
                            "score": 4,
                            "description": "第 1 轮按题面处理了批次差异。",
                        },
                        "dedup_hits": [{
                            "field": "desc_delivery",
                            "submission_id": 80,
                            "similarity": 0.2,
                            "excerpt": "一段历史文字",
                            "unsafe": {"secret": "discard"},
                        }],
                    }],
                    "complete": False,
                    "history_bootstrap_complete": True,
                })
                completed = app.sync_solo_qa_submissions({
                    "items": [],
                    "complete": True,
                    "remote_ids": ["91"],
                })
                with app.db_connection() as database:
                    stored = dict(database.execute(
                        "SELECT * FROM solo_qa_remote_evaluations "
                        "WHERE remote_submission_id = '91'"
                    ).fetchone())
                    prompt_history = dict(database.execute(
                        "SELECT * FROM solo_qa_prompt_history "
                        "WHERE remote_submission_id = '91'"
                    ).fetchone())
                history = app.historical_evaluation_descriptions(
                    "delivery",
                    exclude_turn_key="abc123abc123:1",
                )
                prompt_status = app.solo_qa_prompt_history_status()
                run = app.run_row("abc123abc123")
                repo_history = app.repository_prompt_history(run)

        self.assertEqual(result["matched"], 1)
        self.assertEqual(completed["remote_missing"], 0)
        self.assertEqual(stored["delivery_score"], 5)
        self.assertIn("陶坯称重核对", stored["delivery_description"])
        self.assertNotIn("unsafe", stored["dedup_hits"])
        self.assertEqual(prompt_history["repo_key"], "example/export-demo")
        self.assertIn("陶坯称重", prompt_history["prompt"])
        self.assertTrue(prompt_status["bootstrapped"])
        self.assertEqual(repo_history[0]["reference"], "SOLO-QA #91")
        self.assertEqual(history[0]["reference"], "SOLO-QA #91")
        self.assertIn("陶坯称重核对", history[0]["description"])

    def test_export_and_solo_qa_reject_difficulty_below_hard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                self.insert_completed_turn(root)
                evaluation = sample_evaluation()
                evaluation["task_difficulty"] = "中等"
                app.update_turn(
                    "abc123abc123",
                    1,
                    review_result=json.dumps({"evaluation": evaluation}, ensure_ascii=False),
                )
                row = app.completed_turn_rows()[0]
                export_ready, export_issues = app.export_readiness(row)
                ready, issues = app.solo_qa_readiness(row)

        self.assertFalse(export_ready)
        self.assertFalse(ready)
        expected = "任务难度为中等，只允许导出和提交困难或地狱难度"
        self.assertIn(expected, export_issues)
        self.assertIn(expected, issues)

    def test_only_hard_and_hell_are_submittable(self):
        self.assertTrue(app.submittable_difficulty_issue("简单"))
        self.assertTrue(app.submittable_difficulty_issue("中等"))
        self.assertEqual(app.submittable_difficulty_issue("困难"), "")
        self.assertEqual(app.submittable_difficulty_issue("地狱"), "")

    def test_delete_run_is_recoverable_and_preserves_project_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                repo = self.insert_completed_turn(root)
                with app.db_connection() as database:
                    database.execute(
                        "INSERT INTO events(run_id, level, message, created_at) VALUES (?, 'info', 'done', ?)",
                        ("abc123abc123", app.now_text()),
                    )
                    database.execute(
                        "INSERT INTO run_stage_timings(run_id, stage) VALUES (?, 'repo')",
                        ("abc123abc123",),
                    )
                result = app.delete_run_record("abc123abc123")
                self.assertEqual(app.all_runs(), [])
                restored = app.restore_run_record("abc123abc123")

                self.assertTrue(result["deleted"])
                self.assertTrue(result["files_preserved"])
                self.assertTrue(result["recoverable"])
                self.assertEqual(restored["id"], "abc123abc123")
                self.assertEqual(len(app.all_runs()), 1)
                self.assertTrue(repo.is_dir())

    def test_delete_rejects_active_runs_and_sources_with_children(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    for run_id, phase, source_run_id in (
                        ("aaa111aaa111", "first_running", None),
                        ("bbb222bbb222", "complete", None),
                        ("ccc333ccc333", "stopped", "bbb222bbb222"),
                    ):
                        database.execute(
                            """INSERT INTO runs(
                                 id, repo_name, repo_path, phase, source_run_id,
                                 first_prompt, verification_commands, created_at, updated_at
                               ) VALUES (?, ?, ?, ?, ?, '需求', '[]', ?, ?)""",
                            (run_id, run_id, str(root / run_id), phase, source_run_id, timestamp, timestamp),
                        )
                with self.assertRaisesRegex(app.WorkflowError, "运行中的任务不能删除"):
                    app.delete_run_record("aaa111aaa111")
                with self.assertRaisesRegex(app.WorkflowError, "请先删除后续任务"):
                    app.delete_run_record("bbb222bbb222")


class DatabaseTests(unittest.TestCase):
    def test_iteration_scope_metadata_is_persisted_and_reused_in_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            timestamp = app.now_text()
            project_root = root / "0001-demo"
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "schedule_worker"
            ), mock.patch.object(
                app, "HISTORY_PATH", root / "history-prompts.md"
            ):
                app.initialize_database()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                             id, repo_name, project_directory, repo_path, run_directory,
                             repo_url, phase, first_prompt, first_prompt_id,
                             container_cleaned, task_type, verification_commands,
                             created_at, updated_at
                           ) VALUES ('rootmeta1111', 'demo', '.', ?, ?,
                                     'https://example.invalid/demo', 'complete',
                                     '根需求', 'prompt-root', 1, '0-1 代码生成', '[]', ?, ?)""",
                        (
                            str(project_root / "workspace"),
                            str(project_root),
                            timestamp,
                            timestamp,
                        ),
                    )
                metadata = {
                    "expansion_axis": "人工复核",
                    "modules": ["领域层", "API", "页面", "测试"],
                    "engineering_core": "复核授权闭环",
                    "complex_dimensions": ["确认失效规则"],
                    "main_user_flow": "审阅人确认命中后开放下载",
                    "api_or_actions": ["确认当前项", "确认全部"],
                    "new_state_sets": ["确认状态"],
                }
                contract = {
                    "version": 1,
                    "estimated_task_difficulty": "困难",
                    "axis": "跨模块契约",
                    "hard_requirement": "确认状态必须与证据版本跨层一致",
                    "acceptance_evidence": ["证据变化后旧确认自动失效"],
                    "rejected_shortcut": "只保存确认布尔值无法识别证据版本变化",
                    "difficulty_evidence": ["状态、接口和页面共同维护版本一致性"],
                }
                created = app.create_run(
                    {
                        "repo_name": "demo",
                        "project_directory": ".",
                        "task_type": "Feature 迭代",
                        "first_prompt": "迭代需求",
                        "_intent_type": "Feature 迭代",
                        "_source_run_id": "rootmeta1111",
                        "_iteration_source_run_id": "rootmeta1111",
                        "_source_repo_url": "https://example.invalid/demo",
                        "_source_snapshot": "https://example.invalid/demo/commit/abc123",
                        "_iteration_metadata": metadata,
                        "_difficulty_contract": contract,
                    }
                )

                row = app.run_row(created["id"])
                self.assertEqual(row["iteration_expansion_axis"], "人工复核")
                self.assertEqual(json.loads(row["iteration_modules"]), metadata["modules"])
                self.assertEqual(created["iteration_metadata"], metadata)
                self.assertEqual(
                    created["difficulty_contract"],
                    {
                        **contract,
                        "version": 2,
                        "difficulty_margin": "明确困难",
                        "hardness_basis": "状态、接口和页面共同维护版本一致性",
                    },
                )
                history = app.iteration_lineage_state(created["id"])["history"]
                child = next(item for item in history if item["run_id"] == created["id"])
                self.assertEqual(child["engineering_core"], "复核授权闭环")
                self.assertEqual(child["api_or_actions"], ["确认当前项", "确认全部"])

    def test_independent_bug_generation_evidence_is_persisted_on_new_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            timestamp = app.now_text()
            project_root = root / "0001-demo"
            bugs = [
                {
                    "title": f"问题 {index}",
                    "reproduction": f"复现第 {index} 个业务问题",
                    "actual": f"实际结果 {index}",
                    "expected": f"正确结果 {index}",
                    "evidence": f"第 {index} 个问题的响应与状态记录",
                    "estimated_fix_scope": "小",
                    "customer_summary": f"第 {index} 个客户可见问题摘要",
                }
                for index in range(1, 4)
            ]
            evidence = {
                "source_run_id": "rootbugs111",
                "source_commit": "a" * 40,
                "verified_at": timestamp,
                "focus_area": "交接确认",
                "main_user_flow": "接收人确认样本位置",
                "scope_summary": "样本交接确认范围",
                "bugs": bugs,
                "independent_review": {
                    "approved": True,
                    "reasons": [],
                    "task_type": "Bug 修复",
                },
            }
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "schedule_worker"
            ), mock.patch.object(
                app, "HISTORY_PATH", root / "history-prompts.md"
            ):
                app.initialize_database()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                             id, repo_name, project_directory, repo_path, run_directory,
                             repo_url, phase, first_prompt, first_prompt_id,
                             container_cleaned, task_type, verification_commands,
                             created_at, updated_at
                           ) VALUES ('rootbugs111', 'demo', '.', ?, ?,
                                     'https://example.invalid/demo', 'complete',
                                     '根需求', 'prompt-root', 1, '0-1 代码生成', '[]', ?, ?)""",
                        (
                            str(project_root / "workspace"),
                            str(project_root),
                            timestamp,
                            timestamp,
                        ),
                    )
                created = app.create_run(
                    {
                        "repo_name": "demo",
                        "project_directory": ".",
                        "task_type": "Bug 修复",
                        "first_prompt": "三个经过复核的交接问题。",
                        "_intent_type": "Bug 修复",
                        "_source_run_id": "rootbugs111",
                        "_iteration_source_run_id": "rootbugs111",
                        "_source_repo_url": "https://example.invalid/demo",
                        "_source_snapshot": (
                            "https://example.invalid/demo/commit/" + "a" * 40
                        ),
                        "_bug_generation_evidence": evidence,
                    }
                )

                row = app.run_row(created["id"])
                stored = json.loads(row["bug_generation_evidence"])

            self.assertEqual(stored["source_run_id"], "rootbugs111")
            self.assertEqual(stored["source_commit"], "a" * 40)
            self.assertEqual(stored["bugs"], bugs)
            self.assertTrue(stored["independent_review"]["approved"])
            self.assertEqual(created["bug_generation_evidence"], stored)

    def test_latest_iteration_baseline_follows_remote_main_and_nested_lineage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            remote = root / "remote.git"
            origin_repo = root / "0003-demo" / "workspace"
            child_repo = root / "0003-1-demo" / "workspace"
            app.run_command(["git", "init", "--bare", "--initial-branch=main", str(remote)])
            origin_repo.parent.mkdir(parents=True)
            app.run_command(["git", "clone", str(remote), str(origin_repo)])
            app.run_command(["git", "config", "user.name", "Test User"], cwd=origin_repo)
            app.run_command(["git", "config", "user.email", "test@example.com"], cwd=origin_repo)
            (origin_repo / "version.txt").write_text("root\n", encoding="utf-8")
            app.run_command(["git", "add", "version.txt"], cwd=origin_repo)
            app.run_command(["git", "commit", "-m", "root"], cwd=origin_repo)
            app.run_command(["git", "push", "origin", "HEAD:main"], cwd=origin_repo)

            child_repo.parent.mkdir(parents=True)
            app.run_command(["git", "clone", str(remote), str(child_repo)])
            app.run_command(["git", "config", "user.name", "Test User"], cwd=child_repo)
            app.run_command(["git", "config", "user.email", "test@example.com"], cwd=child_repo)
            (child_repo / "version.txt").write_text("latest\n", encoding="utf-8")
            app.run_command(["git", "commit", "-am", "latest"], cwd=child_repo)
            app.run_command(["git", "push", "origin", "HEAD:main"], cwd=child_repo)

            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                             id, repo_name, repo_path, run_directory, repo_url, phase,
                             first_prompt, first_prompt_id, container_cleaned, task_type,
                             verification_commands, created_at, updated_at
                           ) VALUES ('root11111111', 'demo', ?, ?, ?, 'complete',
                                     '根需求', 'prompt-root', 1, '0-1 代码生成', '[]',
                                     '2026-01-01 00:00:00', '2026-01-01 00:00:00')""",
                        (str(origin_repo), str(origin_repo.parent), str(remote)),
                    )
                    database.execute(
                        """INSERT INTO runs(
                             id, repo_name, repo_path, run_directory, repo_url, phase,
                             first_prompt, first_prompt_id, container_cleaned, task_type,
                             source_run_id, verification_commands, created_at, updated_at
                           ) VALUES ('child222222', 'demo', ?, ?, ?, 'stopped',
                                     '第一版迭代', 'prompt-child', 1, 'Feature 迭代',
                                     'root11111111', '[]',
                                     '2026-01-02 00:00:00', '2026-01-02 00:00:00')""",
                        (str(child_repo), str(child_repo.parent), str(remote)),
                    )
                    for run_id, intent in (
                        ("root11111111", "0-1 代码生成"),
                        ("child222222", "Feature 迭代"),
                    ):
                        database.execute(
                            """INSERT INTO run_turns(
                                 run_id, turn_number, intent_type, prompt, prompt_id,
                                 verification, status, created_at, updated_at
                               ) VALUES (?, 1, ?, '需求', 'prompt', '[]', 'complete',
                                         '2026-01-02 00:00:00', '2026-01-02 00:00:00')""",
                            (run_id, intent),
                        )

                self.assertEqual(
                    app.latest_iteration_baseline_run_id("root11111111"),
                    "child222222",
                )

                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                             id, repo_name, repo_path, run_directory, repo_url, phase,
                             first_prompt, first_prompt_id, container_cleaned, task_type,
                             source_run_id, verification_commands, created_at, updated_at
                           ) VALUES ('failed33333', 'demo', '/tmp/failed/workspace',
                                     '/tmp/failed', ?, 'interrupted', '失败迭代',
                                     'prompt-failed', 1, 'Feature 迭代', 'child222222',
                                     '[]', '2026-01-03 00:00:00', '2026-01-03 00:00:00')""",
                        (str(remote),),
                    )

                self.assertEqual(
                    app.latest_iteration_baseline_run_id("root11111111"),
                    "child222222",
                )

                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                             id, repo_name, repo_path, run_directory, repo_url, phase,
                             first_prompt, first_prompt_id, container_cleaned, task_type,
                             source_run_id, verification_commands, created_at, updated_at
                           ) VALUES ('active333333', 'demo', '/tmp/active/workspace',
                                     '/tmp/active', ?, 'first_running', '下一版',
                                     'prompt-active', 0, 'Feature 迭代', 'failed33333',
                                     '[]', '2026-01-03 00:00:00', '2026-01-03 00:00:00')""",
                        (str(remote),),
                    )
                with self.assertRaisesRegex(app.WorkflowError, "仍有运行中的迭代"):
                    app.latest_iteration_baseline_run_id("root11111111")

    def test_latest_iteration_baseline_caches_remote_main_without_rewriting_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            remote = root / "remote.git"
            origin_repo = root / "0003-demo" / "workspace"
            publisher_repo = root / "publisher"
            cache_dir = root / "cache"
            app.run_command(["git", "init", "--bare", "--initial-branch=main", str(remote)])
            origin_repo.parent.mkdir(parents=True)
            app.run_command(["git", "clone", str(remote), str(origin_repo)])
            app.run_command(["git", "config", "user.name", "Test User"], cwd=origin_repo)
            app.run_command(["git", "config", "user.email", "test@example.com"], cwd=origin_repo)
            (origin_repo / "README.md").write_text("# Original\n", encoding="utf-8")
            app.run_command(["git", "add", "README.md"], cwd=origin_repo)
            app.run_command(["git", "commit", "-m", "root"], cwd=origin_repo)
            app.run_command(["git", "push", "origin", "HEAD:main"], cwd=origin_repo)
            original_sha = app.run_command(
                ["git", "rev-parse", "HEAD"], cwd=origin_repo
            ).stdout.strip()

            app.run_command(["git", "clone", str(remote), str(publisher_repo)])
            app.run_command(["git", "config", "user.name", "Publisher"], cwd=publisher_repo)
            app.run_command(["git", "config", "user.email", "publisher@example.com"], cwd=publisher_repo)
            (publisher_repo / "README.md").write_text("# Remote latest\n", encoding="utf-8")
            app.run_command(["git", "commit", "-am", "remote update"], cwd=publisher_repo)
            app.run_command(["git", "push", "origin", "HEAD:main"], cwd=publisher_repo)
            remote_sha = app.run_command(
                ["git", "rev-parse", "HEAD"], cwd=publisher_repo
            ).stdout.strip()

            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(
                app, "ITERATION_BASELINE_CACHE_DIR", cache_dir
            ):
                app.ITERATION_BASELINE_OVERRIDES.clear()
                app.initialize_database()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                             id, repo_name, repo_path, run_directory, repo_url, phase,
                             first_prompt, first_prompt_id, container_cleaned, task_type,
                             verification_commands, created_at, updated_at
                           ) VALUES ('root11111111', 'demo', ?, ?, ?, 'complete',
                                     '根需求', 'prompt-root', 1, '0-1 代码生成', '[]',
                                     '2026-01-01 00:00:00', '2026-01-01 00:00:00')""",
                        (str(origin_repo), str(origin_repo.parent), str(remote)),
                    )
                    database.execute(
                        """INSERT INTO run_turns(
                             run_id, turn_number, intent_type, prompt, prompt_id,
                             verification, status, created_at, updated_at
                           ) VALUES ('root11111111', 1, '0-1 代码生成', '需求', 'prompt-root',
                                     '[]', 'complete', '2026-01-01 00:00:00',
                                     '2026-01-01 00:00:00')"""
                    )

                self.assertEqual(
                    app.latest_iteration_baseline_run_id("root11111111"),
                    "root11111111",
                )
                context = app.iteration_project_context(app.run_row("root11111111"))
                self.assertEqual(context["current_commit"], remote_sha)
                self.assertIn("Remote latest", context["readme"])
                self.assertNotEqual(Path(context["repo_path"]), origin_repo)
                self.assertEqual(
                    app.run_command(
                        ["git", "rev-parse", "HEAD"], cwd=origin_repo
                    ).stdout.strip(),
                    original_sha,
                )
                self.assertEqual(
                    app.run_command(
                        ["git", "status", "--porcelain"], cwd=origin_repo
                    ).stdout.strip(),
                    "",
                )
                app.ITERATION_BASELINE_OVERRIDES.clear()

    def test_run_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            temp_db = Path(directory) / "test.db"
            with mock.patch.object(app, "DB_PATH", temp_db), mock.patch.object(app, "DATA_DIR", Path(directory)):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, phase, first_prompt,
                          verification_commands, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        ("abc123abc123", "demo", "/tmp/demo", "queued", "需求", "[]", timestamp, timestamp),
                    )
                run = app.serialize_run(app.run_row("abc123abc123"))
                self.assertEqual(run["repo_name"], "demo")
                self.assertEqual(run["task_difficulty"], "待评估")
                self.assertEqual(run["verification_commands"], [])
                self.assertEqual(run["events"], [])

    def test_create_run_keeps_prompt_and_selected_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            temp_db = root / "test.db"
            with mock.patch.object(app, "DB_PATH", temp_db), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(app, "schedule_worker"):
                app.initialize_database()
                app.set_global_model("ark/next-model")
                created = app.create_run({
                    "repo_name": "parallel-demo",
                    "project_directory": "zzzz",
                    "task_type": "0-1 项目开发",
                    "task_difficulty": "地狱",
                    "language_framework": "Python、FastAPI、React",
                    "first_prompt": "  原样需求  ",
                })
                row = app.run_row(created["id"])
                self.assertEqual(row["first_prompt"], "  原样需求  ")
                self.assertEqual(row["model"], "ark/next-model")
                self.assertEqual(row["task_type"], "0-1 项目开发")
                self.assertEqual(row["task_difficulty"], "待评估")
                self.assertEqual(row["language_framework"], "Python、FastAPI、React")
                self.assertEqual(row["project_directory"], "zzzz")
                self.assertEqual(Path(row["repo_path"]), root / "zzzz" / "0001-parallel-demo" / "workspace")
                self.assertEqual(Path(row["run_directory"]), root / "zzzz" / "0001-parallel-demo")
                self.assertEqual(created["project_number"], "0001")
                self.assertEqual(created["current_turn"], 1)
                self.assertEqual(created["turn_label"], "第 1 轮")
                self.assertEqual(created["stage_timings"]["repo"]["status"], "current")
                self.assertEqual(created["stage_timings"]["first"]["status"], "pending")

    def test_stage_timings_accumulate_across_phase_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, phase, first_prompt,
                          verification_commands, created_at, updated_at
                        ) VALUES (?, ?, ?, 'queued', ?, '[]', ?, ?)""",
                        (
                            "timing111111",
                            "timing-demo",
                            "/tmp/timing-demo",
                            "需求",
                            "2026-09-09 10:00:00 +0800",
                            "2026-09-09 10:00:00 +0800",
                        ),
                    )
                    database.execute(
                        """INSERT INTO run_stage_timings(
                          run_id, stage, elapsed_seconds, started_at
                        ) VALUES (?, 'repo', 0, ?)""",
                        ("timing111111", "2026-09-09 10:00:00 +0800"),
                    )

                with mock.patch.object(app, "now_text", return_value="2026-09-09 10:05:00 +0800"):
                    app.update_run("timing111111", phase="first_starting")
                with mock.patch.object(app, "now_text", return_value="2026-09-09 10:12:00 +0800"):
                    serialized = app.serialize_run(app.run_row("timing111111"))

                self.assertEqual(serialized["stage_timings"]["repo"]["status"], "done")
                self.assertEqual(serialized["stage_timings"]["repo"]["elapsed_seconds"], 300)
                self.assertEqual(serialized["stage_timings"]["first"]["status"], "current")
                self.assertEqual(serialized["stage_timings"]["first"]["elapsed_seconds"], 420)
                self.assertEqual(serialized["stage_timings"]["review"]["status"], "pending")

    def test_numbered_project_paths_include_disk_and_reserved_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "zzzz"
            target.mkdir()
            (target / "0001-existing").mkdir()
            (target / "0001-1-existing-iteration").mkdir()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, phase, first_prompt,
                          verification_commands, created_at, updated_at
                        ) VALUES (?, ?, ?, 'queued', ?, '[]', ?, ?)""",
                        (
                            "reserved0002",
                            "reserved",
                            str(target / "0002-reserved"),
                            "需求",
                            timestamp,
                            timestamp,
                        ),
                    )
                self.assertEqual(
                    app.next_numbered_project_path(target, "new-project"),
                    target / "0003-new-project",
                )

    def test_normal_and_imported_project_number_ranges_are_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "zzzz"
            target.mkdir()
            (target / "0001-existing").mkdir()
            (target / "3000-imported").mkdir()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                             id, repo_name, repo_path, run_directory, phase,
                             first_prompt, verification_commands, created_at, updated_at
                           ) VALUES ('legacy300200', '题目生成中', ?, ?, 'stopped',
                                     '题目生成中', '[]', ?, ?)""",
                        (
                            str(target / "3002-pending-project" / "workspace"),
                            str(target / "3002-pending-project"),
                            timestamp,
                            timestamp,
                        ),
                    )
                self.assertEqual(
                    app.next_numbered_project_path(target, "normal"),
                    target / "0002-normal",
                )
                self.assertEqual(
                    app.next_imported_project_path(target, "imported"),
                    target / "3001-imported",
                )

    def test_imported_baseline_is_registered_without_fabricated_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "zzzz"
            project_root = target / "3000-import-demo"
            repo = project_root / "workspace"
            (repo / ".git").mkdir(parents=True)
            sha = "d" * 40

            def command(args, **kwargs):
                if args[:4] == ["git", "remote", "get-url", "origin"]:
                    return subprocess.CompletedProcess(
                        args, 0, "git@github.com:makabaka-boop/import-demo.git\n", ""
                    )
                if args[:3] == ["git", "rev-parse", "HEAD"]:
                    return subprocess.CompletedProcess(args, 0, sha + "\n", "")
                if args[:2] == ["git", "ls-remote"]:
                    return subprocess.CompletedProcess(
                        args, 0, f"{sha}\trefs/heads/main\n", ""
                    )
                return subprocess.CompletedProcess(args, 0, "", "")

            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history-prompts.md"
            ), mock.patch.object(app, "run_command", side_effect=command):
                app.initialize_database()
                created = app.create_imported_baseline(
                    {
                        "source_path": str(project_root),
                        "project_directory": "zzzz",
                        "project_category": "纯后端",
                        "language_framework": "Python, FastAPI, PostgreSQL",
                        "first_prompt": "从空仓库构建一个可由 Docker Compose 验收的样本服务。",
                        "verification_commands": [
                            "docker compose config --quiet",
                            "docker compose run --rm verify",
                        ],
                    }
                )

                self.assertEqual(created["project_number"], "3000")
                self.assertTrue(created["imported_baseline"])
                self.assertEqual(created["turn_count"], 0)
                self.assertEqual(created["turn_label"], "导入基线")
                self.assertEqual(created["turns"], [])
                self.assertEqual(created["repo_url"], "https://github.com/makabaka-boop/import-demo")
                self.assertEqual(created["base_sha"], sha)
                listed = app.all_runs()[0]
                self.assertTrue(listed["imported_baseline"])
                self.assertEqual(listed["turn_label"], "导入基线")
                self.assertEqual(app.completed_turns(), [])
                self.assertEqual(
                    app.latest_iteration_baseline_run_id(created["id"]),
                    created["id"],
                )
                self.assertEqual(
                    app.auto_refill_iteration_candidate()["id"], created["id"]
                )

                with self.assertRaisesRegex(app.WorkflowError, "已经登记"):
                    app.create_imported_baseline(
                        {
                            "source_path": str(project_root),
                            "project_directory": "zzzz",
                            "project_category": "纯后端",
                            "language_framework": "Python",
                            "first_prompt": "重复导入",
                            "verification_commands": ["docker compose config --quiet"],
                        }
                    )

    def test_number_only_import_infers_metadata_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "zzzz"
            project_root = target / "3000-review-console"
            repo = project_root / "workspace"
            (repo / ".git").mkdir(parents=True)
            (repo / "README.md").write_text(
                "# 烧成检查台\n\n导入温度 CSV，对照目标温度范围并保存复核结果；"
                "支持异常筛选、备注持久化和 Docker Compose 本地验收。",
                encoding="utf-8",
            )
            (repo / "package.json").write_text(
                json.dumps(
                    {
                        "dependencies": {"react": "latest"},
                        "devDependencies": {
                            "typescript": "latest",
                            "vite": "latest",
                            "vitest": "latest",
                        },
                    }
                ),
                encoding="utf-8",
            )
            sha = "e" * 40

            def command(args, **kwargs):
                if args[:4] == ["git", "remote", "get-url", "origin"]:
                    return subprocess.CompletedProcess(
                        args, 0, "https://github.com/makabaka-boop/review-console.git\n", ""
                    )
                if args[:3] == ["git", "rev-parse", "HEAD"]:
                    return subprocess.CompletedProcess(args, 0, sha + "\n", "")
                if args[:2] == ["git", "ls-remote"]:
                    return subprocess.CompletedProcess(
                        args, 0, f"{sha}\trefs/heads/main\n", ""
                    )
                return subprocess.CompletedProcess(args, 0, "", "")

            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history-prompts.md"
            ), mock.patch.object(app, "run_command", side_effect=command):
                app.initialize_database()
                result = app.create_imported_baselines_by_number(
                    {
                        "project_numbers": "3000，3000 3001",
                        "project_directory": "zzzz",
                    }
                )
                self.assertEqual(result["requested_count"], 2)
                self.assertEqual(len(result["imported"]), 1)
                self.assertEqual(result["imported"][0]["project_category"], "纯前端")
                self.assertIn("React", result["imported"][0]["language_framework"])
                self.assertIn("Vite", result["imported"][0]["language_framework"])
                self.assertIn("README 自动整理", result["imported"][0]["first_prompt"])
                self.assertEqual(result["failed"][0]["project_number"], "3001")
                self.assertIn("没有找到", result["failed"][0]["error"])

                repeated = app.create_imported_baselines_by_number(
                    {"project_numbers": ["3000"], "project_directory": "zzzz"}
                )
                self.assertEqual(repeated["imported"], [])
                self.assertEqual(len(repeated["existing"]), 1)
                self.assertEqual(repeated["failed"], [])

    def test_conversation_turn_tracks_second_round(self):
        self.assertEqual(
            app.conversation_turn({"phase": "awaiting_second"}),
            {"current_turn": 2, "turn_label": "待第 2 轮"},
        )
        self.assertEqual(
            app.conversation_turn({"phase": "failed", "second_prompt": "修复问题"}),
            {"current_turn": 2, "turn_label": "第 2 轮"},
        )

    def test_feature_iteration_cannot_be_added_to_an_existing_conversation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, phase, first_prompt,
                          verification_commands, created_at, updated_at
                        ) VALUES (?, ?, '/tmp/demo', 'complete', '原始需求', '[]', ?, ?)""",
                        ("separate1111", "separate-demo", timestamp, timestamp),
                    )
                with self.assertRaisesRegex(app.WorkflowError, "Feature 迭代必须新建会话"):
                    app.create_followup_turn("separate1111", "增加功能", "Feature 迭代")

    def test_completed_task_starts_feature_iteration_as_a_new_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source_project = root / "0003-iterate-demo"
            source_repo = source_project / "workspace"
            source_repo.mkdir(parents=True)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker") as scheduler, mock.patch.object(
                app, "run_command"
            ) as command, mock.patch.object(
                app, "latest_iteration_baseline_run_id", return_value="iterate11111"
            ):
                command.side_effect = lambda args, **kwargs: subprocess.CompletedProcess(
                    args,
                    0,
                    "a" * 40 + "\n" if args[:3] == ["git", "rev-parse", "HEAD"] else "",
                    "",
                )
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, run_directory, repo_url, phase, session_id, first_prompt,
                          first_prompt_id, container_cleaned, project_directory, task_difficulty,
                          verification_commands, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'https://github.com/makabaka-boop/iterate-demo',
                                  'complete', 'session-1', '首轮需求', 'prompt-1', 1,
                                  '.', '困难', '[]', ?, ?)""",
                        (
                            "iterate11111",
                            "iterate-demo",
                            str(source_repo),
                            str(source_project),
                            timestamp,
                            timestamp,
                        ),
                    )
                    database.execute(
                        """INSERT INTO run_turns(
                          run_id, turn_number, intent_type, prompt, prompt_id, status,
                          verification, created_at, updated_at
                        ) VALUES (?, 1, '0-1 代码生成', '首轮需求', 'prompt-1', 'complete', '[]', ?, ?)""",
                        ("iterate11111", timestamp, timestamp),
                    )

                created = app.start_second_turn(
                    "iterate11111", {"prompt": "在现有功能上增加批量导出，并补充回归测试。"}
                )

        self.assertNotEqual(created["id"], "iterate11111")
        self.assertEqual(created["phase"], "queued")
        self.assertFalse(created["session_id"])
        self.assertEqual(created["source_run_id"], "iterate11111")
        self.assertEqual(created["task_type"], "Feature 迭代")
        self.assertEqual(created["task_difficulty"], "待评估")
        self.assertEqual(created["project_number"], "0003-1")
        self.assertEqual(Path(created["run_directory"]), root / "0003-1-iterate-demo")
        self.assertEqual(
            Path(created["repo_path"]),
            root / "0003-1-iterate-demo" / "workspace",
        )
        self.assertEqual(created["turn_count"], 1)
        self.assertEqual(created["turns"][0]["intent_type"], "Feature 迭代")
        self.assertEqual(created["snapshot_url"], "https://github.com/makabaka-boop/iterate-demo/commit/" + "a" * 40)
        scheduler.assert_called_once_with(created["id"], "queued", app.first_turn_worker)

    def test_iteration_sequence_counts_legacy_children(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source_project = root / "0003-base-project"
            source_repo = source_project / "workspace"
            source_repo.mkdir(parents=True)
            legacy_project = root / "0005-base-project"
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, run_directory, phase, task_type,
                          first_prompt, verification_commands, created_at, updated_at
                        ) VALUES (?, 'base-project', ?, ?, 'complete', '0-1 代码生成',
                                  '原始需求', '[]', ?, ?)""",
                        (
                            "origin333333",
                            str(source_repo),
                            str(source_project),
                            timestamp,
                            timestamp,
                        ),
                    )
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, run_directory, phase, task_type,
                          source_run_id, first_prompt, verification_commands, created_at, updated_at
                        ) VALUES (?, 'base-project', ?, ?, 'first_running', 'Feature 迭代', ?,
                                  '旧规则创建的迭代', '[]', ?, ?)""",
                        (
                            "legacy555555",
                            str(legacy_project / "workspace"),
                            str(legacy_project),
                            "origin333333",
                            timestamp,
                            timestamp,
                        ),
                    )

                self.assertEqual(
                    app.next_iteration_project_path(root, "base-project", "origin333333"),
                    root / "0003-2-base-project",
                )
                self.assertEqual(
                    app.next_numbered_project_path(root, "new-project"),
                    root / "0006-new-project",
                )

    def test_iteration_project_number_label_includes_iteration_sequence(self):
        self.assertEqual(
            app.project_number_label("/tmp/0003-2-base-project/workspace"),
            "0003-2",
        )

    def test_completed_legacy_iteration_is_moved_and_all_local_paths_follow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source_project = root / "0003-base-project"
            source_repo = source_project / "workspace"
            source_repo.mkdir(parents=True)
            legacy_project = root / "0005-base-project"
            legacy_repo = legacy_project / "workspace"
            legacy_trace = legacy_project / "traces" / "session.jsonl"
            legacy_repo.mkdir(parents=True)
            legacy_trace.parent.mkdir()
            legacy_trace.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, run_directory, phase, task_type,
                          first_prompt, verification_commands, created_at, updated_at
                        ) VALUES (?, 'base-project', ?, ?, 'complete', '0-1 代码生成',
                                  '原始需求', '[]', ?, ?)""",
                        (
                            "origin333333",
                            str(source_repo),
                            str(source_project),
                            timestamp,
                            timestamp,
                        ),
                    )
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, run_directory, workspace_path,
                          trajectory_path, phase, task_type, source_run_id, container_cleaned,
                          first_prompt, verification_commands, created_at, updated_at
                        ) VALUES (?, 'base-project', ?, ?, ?, ?, 'complete', 'Feature 迭代', ?, 1,
                                  '迭代需求', '[]', ?, ?)""",
                        (
                            "legacy555555",
                            str(legacy_repo),
                            str(legacy_project),
                            str(legacy_repo),
                            str(legacy_trace),
                            "origin333333",
                            timestamp,
                            timestamp,
                        ),
                    )

                before_move = app.serialize_run(app.run_row("legacy555555"))
                moved = app.migrate_completed_legacy_iteration_directory("legacy555555")
                migrated = app.serialize_run(app.run_row("legacy555555"))

            expected_project = root / "0003-1-base-project"
            self.assertEqual(moved, expected_project)
            self.assertEqual(before_move["project_number"], "0003-1")
            self.assertFalse(legacy_project.exists())
            self.assertTrue((expected_project / "workspace").is_dir())
            self.assertTrue((expected_project / "traces" / "session.jsonl").is_file())
            self.assertEqual(migrated["project_number"], "0003-1")
            self.assertEqual(Path(migrated["run_directory"]), expected_project)
            self.assertEqual(Path(migrated["repo_path"]), expected_project / "workspace")
            self.assertEqual(Path(migrated["workspace_path"]), expected_project / "workspace")
            self.assertEqual(
                Path(migrated["trajectory_path"]),
                expected_project / "traces" / "session.jsonl",
            )

    def test_global_model_updates_only_sessions_whose_container_has_not_started(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    for run_id, phase in (
                        ("queued111111", "queued"),
                        ("second222222", "second_queued"),
                        ("active333333", "first_running"),
                    ):
                        database.execute(
                            """INSERT INTO runs(
                              id, repo_name, model, repo_path, phase, first_prompt,
                              verification_commands, created_at, updated_at
                            ) VALUES (?, ?, 'ark/old-model', '/tmp/demo', ?, '需求', '[]', ?, ?)""",
                            (run_id, run_id, phase, timestamp, timestamp),
                        )

                self.assertEqual(app.set_global_model("ark/new-model"), "ark/new-model")
                self.assertEqual(app.current_model(), "ark/new-model")
                self.assertEqual(app.run_row("queued111111")["model"], "ark/new-model")
                self.assertEqual(app.run_row("second222222")["model"], "ark/old-model")
                self.assertIsNone(app.run_row("second222222")["second_model"])
                self.assertEqual(app.run_row("active333333")["model"], "ark/old-model")

    def test_interrupted_first_turn_restarts_in_a_fresh_container_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project_root = root / "0002-existing-repo"
            repo = project_root / "workspace"
            repo.mkdir(parents=True)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker") as scheduler:
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, repo_url, snapshot_url, phase, first_prompt, base_sha,
                          session_id, first_agent_id, container_cleaned, project_directory,
                          verification_commands, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'interrupted', '原样需求', ?,
                                  'old-session', 'old-agent', 1, '.', '[]', ?, ?)""",
                        (
                            "retry111111",
                            "existing-repo",
                            str(repo),
                            "https://github.com/makabaka-boop/existing-repo",
                            "https://github.com/makabaka-boop/existing-repo/commit/" + "a" * 40,
                            "a" * 40,
                            timestamp,
                            timestamp,
                        ),
                    )

                created = app.retry_first_turn("retry111111")

        self.assertNotEqual(created["id"], "retry111111")
        self.assertEqual(created["phase"], "queued")
        self.assertEqual(created["task_type"], "0-1 重跑")
        self.assertEqual(created["source_run_id"], "retry111111")
        self.assertEqual(created["project_number"], "0002")
        self.assertEqual(
            Path(created["repo_path"]),
            project_root / "retries" / "retry-01" / "workspace",
        )
        self.assertFalse(created["session_id"])
        scheduler.assert_called_once_with(created["id"], "queued", app.first_turn_worker)

    def test_feature_retry_keeps_iteration_generation_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project_root = root / "0017-1-port-laytime-adjudicator"
            repo = project_root / "workspace"
            repo.mkdir(parents=True)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker"):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, run_directory, repo_url, snapshot_url,
                          phase, task_type, first_prompt, base_sha, container_cleaned,
                          project_directory, verification_commands, iteration_expansion_axis,
                          iteration_modules, iteration_engineering_core,
                          iteration_complex_dimensions, iteration_main_user_flow,
                          iteration_api_or_actions, iteration_new_state_sets,
                          created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'interrupted', 'Feature 迭代', '原样需求', ?,
                                  1, '.', '[]', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            "feature171717",
                            "port-laytime-adjudicator",
                            str(repo),
                            str(project_root),
                            "https://github.com/makabaka-boop/port-laytime-adjudicator",
                            "https://github.com/makabaka-boop/port-laytime-adjudicator/commit/" + "f" * 40,
                            "f" * 40,
                            "允许时长扣减",
                            json.dumps(["模型", "服务", "迁移"], ensure_ascii=False),
                            "有界扣减",
                            json.dumps(["确定性计费"], ensure_ascii=False),
                            "创建后查询",
                            json.dumps(["创建", "查询"], ensure_ascii=False),
                            "[]",
                            timestamp,
                            timestamp,
                        ),
                    )

                created = app.retry_first_turn("feature171717")

        self.assertEqual(created["task_type"], "Feature 迭代重跑")
        self.assertEqual(created["iteration_metadata"]["expansion_axis"], "允许时长扣减")
        self.assertEqual(created["iteration_metadata"]["modules"], ["模型", "服务", "迁移"])
        self.assertEqual(created["iteration_metadata"]["engineering_core"], "有界扣减")

    def test_bugfix_retry_keeps_bugfix_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project_root = root / "0003-2-sample-handoff-ledger"
            repo = project_root / "workspace"
            repo.mkdir(parents=True)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_worker"):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, run_directory, repo_url, snapshot_url,
                          phase, task_type, first_prompt, base_sha, container_cleaned,
                          project_directory, verification_commands, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'interrupted', 'Bug 修复', '问题摘要', ?,
                                  1, '.', '[]', ?, ?)""",
                        (
                            "bugfix333333",
                            "sample-handoff-ledger",
                            str(repo),
                            str(project_root),
                            "https://github.com/makabaka-boop/sample-handoff-ledger",
                            "https://github.com/makabaka-boop/sample-handoff-ledger/commit/" + "b" * 40,
                            "b" * 40,
                            timestamp,
                            timestamp,
                        ),
                    )

                created = app.retry_first_turn("bugfix333333")

        self.assertEqual(created["task_type"], "Bug 修复重跑")
        self.assertEqual(created["turns"][0]["intent_type"], "Bug 修复")

    def test_transient_api_error_schedules_isolated_retry_under_same_project_number(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project_root = root / "0004-api-retry-demo"
            repo = project_root / "workspace"
            repo.mkdir(parents=True)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_delayed_first_turn") as delayed:
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                          id, repo_name, repo_path, run_directory, repo_url, snapshot_url,
                          phase, first_prompt, base_sha, model, container_cleaned,
                          project_directory, verification_commands, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'interrupted', '原样需求', ?, ?, 1, '.', '[]', ?, ?)""",
                        (
                            "auto4444444",
                            "api-retry-demo",
                            str(repo),
                            str(project_root),
                            "https://github.com/makabaka-boop/api-retry-demo",
                            "https://github.com/makabaka-boop/api-retry-demo/commit/" + "b" * 40,
                            "b" * 40,
                            "ark/urm-01",
                            timestamp,
                            timestamp,
                        ),
                    )

                created = app.schedule_automatic_api_retry("auto4444444")
                source = app.serialize_run(app.run_row("auto4444444"))

        self.assertEqual(created["phase"], "queued")
        self.assertEqual(created["model"], "ark/urm-01")
        self.assertEqual(created["project_number"], "0004")
        self.assertEqual(
            Path(created["repo_path"]),
            project_root / "retries" / "retry-01" / "workspace",
        )
        self.assertEqual(source["retry_run_id"], created["id"])
        delayed.assert_called_once_with(created["id"], app.AUTO_API_RETRY_DELAY_SECONDS)

    def test_feature_iteration_ancestry_does_not_consume_api_retry_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project_root = root / "0003-3-sample-handoff-ledger"
            workspace = project_root / "workspace"
            workspace.mkdir(parents=True)
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root), mock.patch.object(
                app, "HISTORY_PATH", root / "history.md"
            ), mock.patch.object(app, "schedule_delayed_first_turn") as delayed:
                app.initialize_database()
                timestamp = app.now_text()
                snapshot = "https://github.com/makabaka-boop/sample-handoff-ledger/commit/" + "c" * 40
                with app.db_connection() as database:
                    rows = (
                        (
                            "root00000001", "0-1 代码生成", None,
                            root / "0003-sample-handoff-ledger" / "workspace", "complete", 1,
                        ),
                        (
                            "iter00000001", "Feature 迭代", "root00000001",
                            root / "0003-1-sample-handoff-ledger" / "workspace", "complete", 1,
                        ),
                        (
                            "iter00000003", "Feature 迭代", "iter00000001",
                            workspace, "interrupted", 1,
                        ),
                    )
                    for run_id, task_type, source_run_id, repo_path, phase, cleaned in rows:
                        database.execute(
                            """INSERT INTO runs(
                              id, repo_name, task_type, repo_path, run_directory, repo_url,
                              snapshot_url, base_sha, source_run_id, phase, first_prompt,
                              container_cleaned, project_directory, verification_commands,
                              created_at, updated_at
                            ) VALUES (?, 'sample-handoff-ledger', ?, ?, ?, ?, ?, ?, ?, ?,
                                      '原样 Feature 需求', ?, '.', '[]', ?, ?)""",
                            (
                                run_id,
                                task_type,
                                str(repo_path),
                                str(Path(repo_path).parent),
                                "https://github.com/makabaka-boop/sample-handoff-ledger",
                                snapshot,
                                "c" * 40,
                                source_run_id,
                                phase,
                                cleaned,
                                timestamp,
                                timestamp,
                            ),
                        )

                first_retry = app.schedule_automatic_api_retry("iter00000003")
                self.assertEqual(
                    Path(first_retry["repo_path"]),
                    project_root / "retries" / "retry-01" / "workspace",
                )
                app.update_run(
                    first_retry["id"], phase="interrupted", container_cleaned=1
                )

                second_retry = app.schedule_automatic_api_retry(first_retry["id"])
                self.assertEqual(
                    Path(second_retry["repo_path"]),
                    project_root / "retries" / "retry-02" / "workspace",
                )
                app.update_run(
                    second_retry["id"], phase="interrupted", container_cleaned=1
                )

                with self.assertRaisesRegex(app.WorkflowError, "自动重跑上限 2 次"):
                    app.schedule_automatic_api_retry(second_retry["id"])

        self.assertEqual(delayed.call_count, 2)

class ResilienceTests(unittest.TestCase):
    def test_harness_version_is_normalized_to_numeric_semver(self):
        self.assertEqual(app.normalize_harness_version("2.1.263 (Claude Code)"), "2.1.263")
        self.assertEqual(app.normalize_harness_version("20260909-isolated-git"), "")

    def test_iteration_generation_job_survives_memory_cache_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_id = "persist111111"
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                             id, repo_name, repo_path, phase, first_prompt,
                             verification_commands, created_at, updated_at
                           ) VALUES (?, 'persist-demo', '/tmp/persist', 'complete',
                                     '需求', '[]', ?, ?)""",
                        (run_id, timestamp, timestamp),
                    )
                app.put_iteration_job({
                    "source_run_id": run_id,
                    "task_type": "Feature 迭代",
                    "status": "generating",
                    "stage": "独立复核中",
                    "target_sequence": 2,
                    "started_at": timestamp,
                })
                with app.ITERATION_JOB_LOCK:
                    app.ITERATION_JOBS.pop(run_id, None)
                recovered = app.get_iteration_job(run_id)
                app.remove_iteration_job(run_id)

        self.assertEqual(recovered["status"], "generating")
        self.assertEqual(recovered["stage"], "独立复核中")
        self.assertEqual(recovered["target_sequence"], 2)

    def test_compose_verification_uses_per_run_project_and_numeric_ports(self):
        first = app.verification_environment("aaa111aaa111")
        second = app.verification_environment("bbb222bbb222")
        self.assertNotEqual(first["COMPOSE_PROJECT_NAME"], second["COMPOSE_PROJECT_NAME"])
        self.assertNotIn("COMPOSE_PROFILES", first)
        ports = [first[name] for name in ("APP_PORT", "API_PORT", "WEB_PORT", "POSTGRES_PORT")]
        self.assertTrue(all(port.isdigit() for port in ports))
        self.assertEqual(len(ports), len(set(ports)))

    def test_stage_retry_uses_backoff_then_stops_for_manual_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_id = "retry1111111"
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "schedule_worker_at") as schedule:
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                             id, repo_name, repo_path, phase, first_prompt,
                             verification_commands, created_at, updated_at
                           ) VALUES (?, 'retry-demo', '/tmp/retry', 'review_running',
                                     '需求', '[]', ?, ?)""",
                        (run_id, timestamp, timestamp),
                    )
                self.assertTrue(app.queue_control_stage_retry(
                    run_id, "首轮复核", "review_queued", app.review_worker, "504 Gateway Time-out"
                ))
                app.update_run(run_id, phase="review_running")
                self.assertTrue(app.queue_control_stage_retry(
                    run_id, "首轮复核", "review_queued", app.review_worker, "504 Gateway Time-out"
                ))
                app.update_run(run_id, phase="review_running")
                self.assertFalse(app.queue_control_stage_retry(
                    run_id, "首轮复核", "review_queued", app.review_worker, "504 Gateway Time-out"
                ))
                stopped = app.run_row(run_id)

        self.assertEqual(schedule.call_count, 2)
        self.assertEqual(stopped["phase"], "failed")
        self.assertEqual(stopped["stage_retry_count"], 2)

    def test_failed_review_wording_is_requeued_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_id = "review111111"
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "schedule_worker_at") as schedule:
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                             id, repo_name, repo_path, phase, first_prompt,
                             verification_commands, stage_retry_name, error,
                             created_at, updated_at
                           ) VALUES (?, 'review-demo', '/tmp/review', 'failed',
                                     '需求', '[]', '首轮复核', ?, ?, ?)""",
                        (
                            run_id,
                            "交付完整性描述引用了本轮轨迹中未执行的命令：make test",
                            timestamp,
                            timestamp,
                        ),
                    )

                recovered = app.recover_retryable_review_failures()
                stored = app.run_row(run_id)

        self.assertEqual(recovered, 1)
        self.assertEqual(stored["phase"], "review_queued")
        self.assertEqual(stored["stage_retry_count"], 1)
        schedule.assert_called_once()

    def test_failed_trace_checkpoint_is_requeued_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_id = "tracefail111"
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "schedule_worker_at") as schedule:
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                             id, repo_name, repo_path, phase, first_prompt,
                             verification_commands, stage_retry_name, error,
                             created_at, updated_at
                           ) VALUES (?, 'trace-demo', '/tmp/trace', 'failed',
                                     '需求', '[]', 'Git/轨迹检查点', ?, ?, ?)""",
                        (
                            run_id,
                            "轨迹中没有找到本轮最终回复，未生成检查点",
                            timestamp,
                            timestamp,
                        ),
                    )
                    database.execute(
                        """INSERT INTO run_turns(
                             run_id, turn_number, intent_type, prompt, status,
                             verification, created_at, updated_at
                           ) VALUES (?, 1, '0-1 代码生成', '需求', 'reviewing',
                                     '[]', ?, ?)""",
                        (run_id, timestamp, timestamp),
                    )

                recovered = app.recover_retryable_review_failures()
                stored = app.run_row(run_id)

        self.assertEqual(recovered, 1)
        self.assertEqual(stored["phase"], "first_idle")
        self.assertEqual(stored["stage_retry_count"], 1)
        schedule.assert_called_once()

    def test_long_trace_keeps_first_and_last_tool_in_compact_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            events = [{"type": "user", "promptId": "prompt-1", "message": {"content": "需求"}}]
            for index in range(120):
                events.append({
                    "type": "assistant",
                    "message": {"content": [{
                        "type": "tool_use", "name": f"tool_{index:03d}",
                        "input": {"path": f"file-{index}", "payload": "x" * 80},
                    }]},
                })
                events.append({
                    "type": "user",
                    "message": {"content": [{"type": "tool_result", "content": f"result-{index}"}]},
                })
            path.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
                encoding="utf-8",
            )
            excerpt = app.transcript_excerpt_from_path(path, "prompt-1", max_chars=5000)

        self.assertIn("CALL tool_000", excerpt)
        self.assertIn("CALL tool_119", excerpt)
        self.assertLessEqual(len(excerpt), 5000)


class ConcurrencyTests(unittest.TestCase):
    def test_default_parallel_limit_is_four(self):
        self.assertEqual(app.MAX_PARALLEL_RUNS, 4)

    def test_same_run_cannot_start_duplicate_delivery_verification(self):
        with app.verification_run_lock("singleflight111"):
            with self.assertRaisesRegex(app.JobCancelled, "交付验收已在运行"):
                with app.verification_run_lock("singleflight111"):
                    self.fail("duplicate verification lock should not be acquired")

    def test_restart_cleanup_terminates_matching_verification_process_group(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app, "DATA_DIR", Path(directory)
        ), mock.patch.object(
            app, "process_group_snapshot", return_value=(43210, "docker compose build")
        ), mock.patch.object(
            app, "terminate_recorded_process_group"
        ) as terminate:
            record = app.verification_process_record_path("orphan111111")
            record.parent.mkdir(parents=True)
            record.write_text(
                json.dumps({
                    "token": "stale",
                    "owner_pid": os.getpid() + 1000,
                    "pid": 43210,
                    "pgid": 43210,
                    "command": "docker compose build",
                    "cwd": "/tmp/workspace",
                }),
                encoding="utf-8",
            )

            cleaned = app.cleanup_orphaned_verification_processes()

        self.assertEqual(cleaned, ["orphan111111"])
        terminate.assert_called_once_with(43210)
        self.assertFalse(record.exists())

    def test_restart_cleanup_does_not_kill_reused_unrelated_pid(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            app, "DATA_DIR", Path(directory)
        ), mock.patch.object(
            app, "process_group_snapshot", return_value=(43210, "python unrelated.py")
        ), mock.patch.object(
            app, "terminate_recorded_process_group"
        ) as terminate:
            record = app.verification_process_record_path("stale1111111")
            record.parent.mkdir(parents=True)
            record.write_text(
                json.dumps({
                    "token": "stale",
                    "owner_pid": os.getpid() + 1000,
                    "pid": 43210,
                    "pgid": 43210,
                    "command": "docker compose build",
                    "cwd": "/tmp/workspace",
                }),
                encoding="utf-8",
            )

            cleaned = app.cleanup_orphaned_verification_processes()

        self.assertEqual(cleaned, [])
        terminate.assert_not_called()
        self.assertFalse(record.exists())

    def test_scheduler_respects_parallel_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            temp_db = root / "test.db"
            first_started = threading.Event()
            release_first = threading.Event()
            second_started = threading.Event()

            def worker(run_id):
                if run_id == "first1111111":
                    first_started.set()
                    release_first.wait(2)
                else:
                    second_started.set()

            with mock.patch.object(app, "DB_PATH", temp_db), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "WORKER_SEMAPHORE", threading.BoundedSemaphore(1)):
                app.initialize_database()
                timestamp = app.now_text()
                with app.db_connection() as database:
                    for run_id in ("first1111111", "second222222"):
                        database.execute(
                            """INSERT INTO runs(
                              id, repo_name, repo_path, phase, first_prompt,
                              verification_commands, created_at, updated_at
                            ) VALUES (?, ?, '/tmp/demo', 'queued', '需求', '[]', ?, ?)""",
                            (run_id, run_id, timestamp, timestamp),
                        )
                app.schedule_worker("first1111111", "queued", worker)
                self.assertTrue(first_started.wait(1))
                app.schedule_worker("second222222", "queued", worker)
                time.sleep(0.08)
                self.assertFalse(second_started.is_set())
                release_first.set()
                self.assertTrue(second_started.wait(1))


class NiuBugWorkflowMergeTests(unittest.TestCase):
    def test_bounded_review_trajectory_keeps_tool_ledger_and_boundaries(self):
        trajectory = (
            "TRACE-HEAD\n"
            + "a" * 30000
            + "\nTOOL Bash: test-command\nTOOL RESULT: test-result\n"
            + "b" * 30000
            + "\nTRACE-TAIL"
        )

        compact = app.bounded_review_trajectory(trajectory, 24000)

        self.assertLessEqual(len(compact), 24000)
        self.assertIn("TRACE-HEAD", compact)
        self.assertIn("TOOL Bash: test-command", compact)
        self.assertIn("TRACE-TAIL", compact)

    def test_review_compacts_findings_but_scores_with_full_trajectory(self):
        trajectory = "TRACE-HEAD\n" + "x" * 80000 + "\nTRACE-TAIL"
        findings = {
            "summary": "没有发现问题",
            "next_action": "complete",
            "bugs": [],
            "quality_gaps": [],
        }
        scored = {**findings, "evaluation": sample_evaluation()}
        with mock.patch.object(
            app, "run_codex_structured", return_value=findings
        ) as structured, mock.patch.object(
            app, "score_review_findings", return_value=scored
        ) as score:
            result = app.run_codex_review(
                Path("/tmp/review"),
                "原始需求",
                [],
                trajectory,
                findings_reasoning_effort="low",
                evaluation_trajectory=trajectory,
            )

        findings_prompt = structured.call_args.args[0]
        self.assertLess(len(findings_prompt), 35000)
        self.assertIn("找 Bug 轨迹中段已压缩", findings_prompt)
        self.assertEqual(structured.call_args.kwargs["reasoning_effort"], "low")
        self.assertEqual(score.call_args.args[5], trajectory)
        self.assertEqual(result, scored)

    def test_candidate_quality_blocks_only_bugfix_auto_refill(self):
        detail = (
            f"连续 {app.ITERATION_GENERATION_ATTEMPTS} 次未生成合规迭代需求："
            "代码基线中没有三个可以稳定复现的真实问题"
        )
        current_job = {
            "status": "generating",
            "source_run_id": "source111111",
            "baseline_run_id": "base11111111",
            "lineage_origin_run_id": "source111111",
            "task_type": "Bug 修复",
            "auto_refill": True,
            "target_sequence": 2,
        }
        saved = {}

        def save_job(job):
            saved.clear()
            saved.update(job)
            return dict(job)

        with mock.patch.object(
            app, "generate_and_start_iteration", side_effect=app.WorkflowError(detail)
        ), mock.patch.object(
            app, "get_iteration_job", return_value=current_job
        ), mock.patch.object(
            app, "put_iteration_job", side_effect=save_job
        ), mock.patch.object(app, "add_event"), mock.patch.object(
            app, "record_auto_refill_candidate_skip"
        ) as skipped, mock.patch.object(
            app, "record_auto_refill_failure"
        ) as failed:
            app.automatic_iteration_worker(
                "source111111", "Bug 修复", False, True
            )

        self.assertEqual(saved["status"], "blocked")
        self.assertEqual(saved["baseline_run_id"], "base11111111")
        self.assertEqual(saved["target_sequence"], 2)
        self.assertIsNone(saved["cooldown_until_epoch"])
        skipped.assert_called_once()
        failed.assert_not_called()

    def test_bugfix_below_hard_is_skipped_and_replaced_with_feature(self):
        detail = (
            f"连续 {app.ITERATION_GENERATION_ATTEMPTS} 次未生成合规迭代需求："
            "独立复核预估 Bug 修复难度未达到困难：中等"
        )
        current_job = {
            "status": "generating",
            "source_run_id": "source111111",
            "baseline_run_id": "base11111111",
            "lineage_origin_run_id": "source111111",
            "task_type": "Bug 修复",
            "auto_refill": True,
            "target_sequence": 2,
        }
        saved = {}

        def save_job(job):
            saved.clear()
            saved.update(job)
            return dict(job)

        with mock.patch.object(
            app, "generate_and_start_iteration", side_effect=app.WorkflowError(detail)
        ), mock.patch.object(
            app, "get_iteration_job", return_value=current_job
        ), mock.patch.object(
            app, "put_iteration_job", side_effect=save_job
        ), mock.patch.object(app, "add_event") as event, mock.patch.object(
            app, "record_auto_refill_candidate_skip"
        ) as skipped, mock.patch.object(
            app.threading, "Thread"
        ) as thread:
            app.automatic_iteration_worker(
                "source111111", "Bug 修复", False, True
            )

        self.assertEqual(saved["status"], "generating")
        self.assertEqual(saved["task_type"], "Feature 迭代")
        self.assertIn("没有困难 Bug", saved["stage"])
        self.assertEqual(thread.call_args.kwargs["target"], app.automatic_iteration_worker)
        self.assertEqual(thread.call_args.kwargs["args"][1], "Feature 迭代")
        event.assert_called_once()
        skipped.assert_called_once()

    def test_transient_bugfix_generation_failure_is_not_blocked(self):
        for detail in (
            "API Error: Request rejected (429)",
            "504 Gateway Time-out",
            "certificate_verification_error",
            "连接失败",
        ):
            with self.subTest(detail=detail):
                self.assertTrue(
                    app.iteration_generation_infrastructure_failure(detail)
                )

    def test_blocked_bugfix_is_scoped_to_the_current_lineage_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timestamp = app.now_text()
            with mock.patch.object(app, "DB_PATH", root / "test.db"), mock.patch.object(
                app, "DATA_DIR", root
            ), mock.patch.object(app, "PROJECTS_ROOT", root):
                app.initialize_database()
                with app.db_connection() as database:
                    database.execute(
                        """INSERT INTO runs(
                             id, repo_name, repo_path, run_directory, repo_url, phase,
                             first_prompt, first_prompt_id, container_cleaned, task_type,
                             source_run_id, verification_commands, created_at, updated_at
                           ) VALUES (?, ?, ?, ?, ?, 'complete', '原始需求', 'prompt-1',
                                     1, ?, ?, '[]', ?, ?)""",
                        (
                            "root11111111", "root", str(root / "root"), str(root),
                            "https://example.invalid/root", "0-1 代码生成", None,
                            timestamp, timestamp,
                        ),
                    )
                    database.execute(
                        """INSERT INTO runs(
                             id, repo_name, repo_path, run_directory, repo_url, phase,
                             first_prompt, first_prompt_id, container_cleaned, task_type,
                             source_run_id, verification_commands, created_at, updated_at
                           ) VALUES (?, ?, ?, ?, ?, 'complete', '功能迭代', 'prompt-2',
                                     1, ?, ?, '[]', ?, ?)""",
                        (
                            "feature11111", "feature", str(root / "feature"), str(root),
                            "https://example.invalid/root", "Feature 迭代", "root11111111",
                            timestamp, timestamp,
                        ),
                    )
                blocked = app.put_iteration_job(
                    {
                        "source_run_id": "root11111111",
                        "baseline_run_id": "feature11111",
                        "lineage_origin_run_id": "root11111111",
                        "task_type": "Bug 修复",
                        "auto_refill": True,
                        "status": "blocked",
                        "target_sequence": 2,
                    }
                )
                self.assertIsNone(app.auto_refill_iteration_candidate())

                blocked["target_sequence"] = 1
                app.put_iteration_job(blocked)
                candidate = app.auto_refill_iteration_candidate()
                app.remove_iteration_job("root11111111")

        self.assertEqual(candidate["id"], "root11111111")
        self.assertEqual(candidate["next_iteration_task_type"], "Bug 修复")

    def test_terminal_idle_detection_uses_latest_state_marker(self):
        idle = "Bypass Permissions On\nEsc to interrupt\nDone\nNew task?"
        active = "Bypass Permissions On\nDone\nNew task?\nEsc to interrupt"

        self.assertTrue(app.terminal_idle_prompt_visible(idle))
        self.assertFalse(app.terminal_idle_prompt_visible(active))

    def test_final_difficulty_guidance_keeps_one_deep_algorithm_hard(self):
        guidance = app.TASK_DIFFICULTY_GUIDANCE

        self.assertIn("至少一项不可删除", guidance)
        self.assertIn("自定义算法判据", guidance)
        self.assertIn("不要求为了达到困难同时叠加多项机制", guidance)
        self.assertIn("后续局部 Bug 修复不得直接继承", guidance)
        self.assertIn("地狱只用于产物确实同时包含多组深层机制", guidance)

    def test_terminal_screen_capture_falls_back_to_log_when_hardcopy_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = app.terminal_asset_paths("fallback-demo")
            with mock.patch.object(app, "TERMINAL_ASSETS_DIR", root):
                paths = app.terminal_asset_paths("fallback-demo")
                paths["root"].mkdir(parents=True)
                paths["screen_log"].write_text("Done\nNew task?\n", encoding="utf-8")

                def empty_hardcopy(args, **_kwargs):
                    Path(args[-1]).write_text("", encoding="utf-8")
                    return subprocess.CompletedProcess(args, 0, "", "")

                with mock.patch.object(
                    app, "screen_session_running", return_value=True
                ), mock.patch.object(app, "run_command", side_effect=empty_hardcopy):
                    output = app.terminal_screen_text(
                        "fallback-demo", "screen-fallback"
                    )

        self.assertIn("New task?", output)

    def test_business_code_probe_ignores_lockfile_and_accepts_runtime_config(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            app.run_command(["git", "init"], cwd=workspace)
            app.run_command(["git", "config", "user.name", "Test User"], cwd=workspace)
            app.run_command(
                ["git", "config", "user.email", "test@example.com"], cwd=workspace
            )
            (workspace / "README.md").write_text("baseline\n", encoding="utf-8")
            app.run_command(["git", "add", "README.md"], cwd=workspace)
            app.run_command(["git", "commit", "-m", "baseline"], cwd=workspace)
            base_sha = app.run_command(
                ["git", "rev-parse", "HEAD"], cwd=workspace
            ).stdout.strip()
            row = {"repo_path": str(workspace), "base_sha": base_sha}

            (workspace / "package-lock.json").write_text("{}\n", encoding="utf-8")
            self.assertEqual(app.workspace_business_code_output_paths(row), [])

            (workspace / "docker-compose.yml").write_text(
                "services: {}\n", encoding="utf-8"
            )
            self.assertEqual(
                app.workspace_business_code_output_paths(row),
                ["docker-compose.yml"],
            )

    def test_no_code_watchdog_stops_an_inactive_first_turn(self):
        row = {
            "phase": "first_running",
            "container_name": "container-demo",
            "screen_name": "screen-demo",
            "retry_not_before_epoch": 0,
        }
        with mock.patch.object(app, "run_row", return_value=row), mock.patch.object(
            app, "turn_row", return_value={"created_at": app.now_text()}
        ), mock.patch.object(
            app, "refresh_trace_snapshot", return_value=(None, None)
        ), mock.patch.object(
            app, "docker_container_running", return_value=True
        ), mock.patch.object(
            app, "terminal_screen_text", return_value=""
        ), mock.patch.object(
            app, "trace_activity_signature", return_value=None
        ), mock.patch.object(
            app, "completion_recovery_sent_epoch", return_value=None
        ), mock.patch.object(
            app, "final_summary_recovery_sent_epoch", return_value=None
        ), mock.patch.object(
            app, "seconds_between", return_value=2000
        ), mock.patch.object(
            app, "workspace_business_code_output_paths", return_value=[]
        ), mock.patch.object(
            app, "add_event"
        ), mock.patch.object(
            app.time, "monotonic", side_effect=[0, 2000]
        ), mock.patch.object(
            app, "NO_CODE_OUTPUT_PROBE_INTERVAL_SECONDS", 0
        ), mock.patch.object(
            app, "stop_run_for_no_code_output"
        ) as stop:
            app.monitor_docker_turn("watch1111111", 1)

        stop.assert_called_once()
        self.assertIn("30 分钟", stop.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
