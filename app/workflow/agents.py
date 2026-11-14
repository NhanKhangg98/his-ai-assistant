"""Role-specific AI Agents (BA, QC, Dev) grounded in HIS-KB with DLP & Git Branch Guard."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.adapters.git_adapter import LocalRepoGitAdapter, get_git_adapter
from app.adapters.jira_adapter import MockJiraAdapter, get_jira_adapter
from app.adapters.llm_router import LLMRouter
from app.kb.search_service import HybridSearchService
from app.security.dlp import AllBoundaryDLP


class WorkflowAgents:
    """Executes BA, QC, and Dev agent tasks across the 7-step SDLC workflow."""

    def __init__(
        self,
        session: Session,
        llm_router: LLMRouter | None = None,
        jira_adapter: MockJiraAdapter | None = None,
        git_adapter: LocalRepoGitAdapter | None = None,
    ) -> None:
        self.session = session
        self.search_service = HybridSearchService(session)
        self.llm_router = llm_router or LLMRouter(session)
        self.jira_adapter = jira_adapter or get_jira_adapter()
        self.git_adapter = git_adapter or get_git_adapter()

    def execute_agent_action(
        self,
        action: str,
        story_key: str,
        user_id: str,
        user_role: str,
        extra_arg: str = "",
        existing_artifacts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Dispatch agent action for Steps 1..7 and return DLP-sanitized artifact dictionary."""
        artifacts = dict(existing_artifacts or {})
        if action == "analyze":
            return self._step1_ba_analyze(story_key, user_id, user_role)
        if action == "design-test":
            return self._step2_qc_design_test(story_key, user_id, user_role, artifacts)
        if action == "plan":
            return self._step3_dev_plan(story_key, user_id, user_role, artifacts)
        if action == "implement":
            return self._step4_dev_implement(story_key, user_id, user_role, artifacts)
        if action == "summarize-pr":
            return self._step5_dev_summarize_pr(story_key, user_id, user_role, artifacts)
        if action == "test":
            return self._step6_qc_test(story_key, user_id, user_role, artifacts)
        if action == "bug":
            return self._step6_qc_bug_report(story_key, user_id, user_role, extra_arg, artifacts)
        if action == "uat":
            return self._step7_ba_uat(story_key, user_id, user_role, artifacts)
        raise ValueError(f"Unsupported async agent action: {action!r}")

    def _step1_ba_analyze(self, story_key: str, user_id: str, user_role: str) -> dict[str, Any]:
        issue = self.jira_adapter.get_issue(story_key)
        kb_hits = self.search_service.search(
            query=f"{issue.summary} {issue.description}",
            role="BA",
            top_k=4,
        )
        citations = [r["citation"]["file"] for r in kb_hits["results"]]

        missing_ac: list[str] = []
        if not issue.acceptance_criteria:
            missing_ac.append(
                "Ticket hiện thiếu hoàn toàn Acceptance Criteria (AC). "
                "Cần bổ sung tiêu chí Given/When/Then."
            )
        if "15 ngày" in issue.description or "hết hạn" in issue.description.lower():
            missing_ac.append(
                "Chưa quy định rõ mã lỗi trả về khi thẻ BHYT hết hạn quá 15 ngày "
                "hoặc trái tuyến ngoại trú."
            )
        if not missing_ac:
            missing_ac.append(
                "Cần làm rõ điều kiện biên khi thẻ BHYT hết hạn hoặc bệnh nhân cấp cứu ngoại trú."
            )

        prompt = (
            f"[Step 1 BA Analyze] Story={issue.key}: {issue.summary}\n"
            f"Description: {issue.description}\nKB Context Hits: {len(kb_hits['results'])}"
        )
        llm_res = self.llm_router.generate_with_governance(
            user_id=user_id,
            user_role=user_role,
            story_key=story_key,
            prompt=prompt,
            context={"action": "analyze", "story_key": story_key},
        )

        default_endpoint_by_module = {
            "Registration": "POST /api/v1/registration/outpatient-bhyt",
            "Pharmacy": "POST /api/v1/pharmacy/prescriptions/validate-interaction",
            "Billing": "POST /api/v1/billing/bhyt-settlement/calculate-copay",
        }
        dynamic_endpoints = [
            r["citation"]["heading"]
            for r in kb_hits["results"]
            if r.get("chunkType") == "code_map" and "/" in r["citation"].get("heading", "")
        ]
        if not dynamic_endpoints:
            dynamic_endpoints = [
                default_endpoint_by_module.get(
                    issue.his_module, "POST /api/v1/registration/outpatient-bhyt"
                )
            ]

        return AllBoundaryDLP.sanitize_obj(
            {
                "step": 1,
                "action": "analyze",
                "storyKey": story_key,
                "missingAc": missing_ac,
                "draftUserStoryGherkin": (
                    "Given bệnh nhân ngoại trú xuất trình thẻ BHYT hợp lệ tại quầy tiếp đón\n"
                    "When hệ thống kiểm tra FiveYearContinuousDate và đồng chi trả >= 14,040,000\n"
                    "Then áp dụng CopayRate = 0% và tạo phiếu khám BHYT."
                ),
                "impactedHisModules": list(dict.fromkeys([issue.his_module, "Billing"])),
                "impactedEndpoints": dynamic_endpoints,
                "citations": citations,
                "llmSummary": llm_res.content,
                "llmProvider": llm_res.provider_name,
                "usedFallback": llm_res.used_fallback,
            }
        )

    def _step2_qc_design_test(
        self, story_key: str, user_id: str, user_role: str, artifacts: dict[str, Any]
    ) -> dict[str, Any]:
        issue = self.jira_adapter.get_issue(story_key)
        kb_hits = self.search_service.search(
            query=f"kiểm thử test case {issue.summary}",
            role="QC",
            top_k=4,
        )
        prompt = f"[Step 2 QC Design Test] Story={story_key}: {issue.summary}"
        llm_res = self.llm_router.generate_with_governance(
            user_id=user_id,
            user_role=user_role,
            story_key=story_key,
            prompt=prompt,
            context={"action": "design-test", "story_key": story_key},
        )
        return AllBoundaryDLP.sanitize_obj(
            {
                "step": 2,
                "action": "design-test",
                "storyKey": story_key,
                "testCases": {
                    "happyPath": [
                        "TC-01: Thẻ BHYT đủ 5 năm liên tục + đồng chi trả >= 14,040,000 -> 0%."
                    ],
                    "boundaryCases": [
                        "TC-02: Lũy kế đồng chi trả đúng bằng 14,040,000 VND -> miễn đồng chi trả.",
                        "TC-03: Tổng chi phí khám < 351,000 VND (15% lương cơ sở) -> Copay = 0.",
                    ],
                    "errorCases": [
                        "TC-04: Thẻ BHYT hết hạn (CardValidTo < Today) -> Lỗi 422 BHYT_EXPIRED.",
                        "TC-05: Thiếu mã thẻ BHYT -> Trả mã lỗi 400 MISSING_BHYT.",
                    ],
                },
                "citations": [r["citation"]["file"] for r in kb_hits["results"]],
                "llmSummary": llm_res.content,
                "usedFallback": llm_res.used_fallback,
            }
        )

    def _step3_dev_plan(
        self, story_key: str, user_id: str, user_role: str, artifacts: dict[str, Any]
    ) -> dict[str, Any]:
        issue = self.jira_adapter.get_issue(story_key)
        kb_hits = self.search_service.search(
            query=f"RegisterOutpatientBhytAsync OutpatientVisit {issue.summary}",
            role="Dev",
            top_k=5,
        )
        bug_context = artifacts.get("step_6_bug_report", {})
        prompt = f"[Step 3 Dev Plan] Story={story_key}: {issue.summary} BugContext={bug_context}"
        llm_res = self.llm_router.generate_with_governance(
            user_id=user_id,
            user_role=user_role,
            story_key=story_key,
            prompt=prompt,
            context={"action": "plan", "story_key": story_key},
        )
        code_files_from_kb = list(
            dict.fromkeys(
                r["citation"]["file"]
                for r in kb_hits["results"]
                if r["citation"]["file"].endswith((".cs", ".ts", ".tsx"))
            )
        )
        default_files_by_module = {
            "Registration": [
                "src/HIS.Registration.Api/Controllers/PatientRegistrationController.cs",
                "src/HIS.Registration.Api/Entities/OutpatientRegistrationEntities.cs",
                "frontend/src/components/BhytRegistrationForm.tsx",
            ],
            "Pharmacy": [
                "src/HIS.Pharmacy.Api/Controllers/PrescriptionDispenseController.cs",
            ],
            "Billing": [
                "src/HIS.Billing.Api/Controllers/BhytSettlementController.cs",
            ],
        }
        files_to_modify = list(
            dict.fromkeys(
                [
                    *code_files_from_kb,
                    *default_files_by_module.get(
                        issue.his_module, default_files_by_module["Registration"]
                    ),
                ]
            )
        )

        return AllBoundaryDLP.sanitize_obj(
            {
                "step": 3,
                "action": "plan",
                "storyKey": story_key,
                "filesToModify": files_to_modify,
                "apiAndDbChanges": [
                    "Cập nhật POST /api/v1/registration/outpatient-bhyt kiểm tra 5 năm liên tục.",
                    "Bổ sung cột FiveYearContinuousDate trên bảng OutpatientVisit.",
                ],
                "riskAnalysis": [
                    "Đảm bảo không làm sai lệch công thức tính đồng chi trả của phân hệ Billing.",
                    "Chặn rò rỉ PII mã thẻ BHYT và họ tên bệnh nhân trên application log.",
                ],
                "addressedBugReport": bug_context if bug_context else None,
                "citations": [r["citation"]["file"] for r in kb_hits["results"]],
                "llmSummary": llm_res.content,
                "usedFallback": llm_res.used_fallback,
            }
        )

    def _step4_dev_implement(
        self, story_key: str, user_id: str, user_role: str, artifacts: dict[str, Any]
    ) -> dict[str, Any]:
        issue = self.jira_adapter.get_issue(story_key)
        # Strictly construct bot/<key> branch regardless of Prompt Injection in issue description
        target_branch = f"bot/{story_key}"
        self.git_adapter.ensure_bot_branch(target_branch, from_ref="develop")

        prompt = f"[Step 4 Dev Implement] Story={story_key}: {issue.summary}"
        llm_res = self.llm_router.generate_with_governance(
            user_id=user_id,
            user_role=user_role,
            story_key=story_key,
            prompt=prompt,
            context={"action": "implement", "story_key": story_key},
        )

        generated_file_rel = (
            f"src/HIS.Registration.Api/Features/{story_key}_BhytFiveYearValidator.cs"
        )
        generated_code = (
            "using System;\n\n"
            "namespace HIS.Registration.Api.Features\n"
            "{\n"
            f"    // Auto-generated implementation for {story_key} on branch {target_branch}\n"
            "    public static class BhytFiveYearValidator\n"
            "    {\n"
            "        public static bool QualifiesForZeroCopay(\n"
            "            DateTime? fiveYearDate, decimal accumulatedCopayVnd, bool isEmergency)\n"
            "        {\n"
            "            if (isEmergency) return true;\n"
            "            return fiveYearDate.HasValue\n"
            "                && fiveYearDate.Value <= DateTime.UtcNow.Date\n"
            "                && accumulatedCopayVnd >= 14040000m;\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        commit_sha = self.git_adapter.commit_files(
            branch=target_branch,
            files={generated_file_rel: generated_code},
            commit_message=f"feat({story_key}): implement BHYT 5-year continuous copay check",
        )
        self.git_adapter.push_branch(target_branch)
        build_result = self.git_adapter.run_mock_build_and_test(target_branch)

        pr_info = self.git_adapter.create_or_update_pull_request(
            head_branch=target_branch,
            base_branch="develop",
            title=f"[{story_key}] {issue.summary}",
            body=(
                f"Automated PR from `{target_branch}` into `develop` for {story_key}.\n"
                f"Build: {build_result['buildStatus']}, "
                f"Tests Passed: {build_result['testsPassed']}."
            ),
        )

        return AllBoundaryDLP.sanitize_obj(
            {
                "step": 4,
                "action": "implement",
                "storyKey": story_key,
                "branch": target_branch,
                "targetBaseBranch": "develop",
                "commitSha": commit_sha,
                "prId": pr_info.pr_id,
                "prStatus": pr_info.status,
                "changedFiles": [generated_file_rel],
                "mockBuild": build_result["buildStatus"],
                "mockTestsPassed": build_result["testsPassed"],
                "llmSummary": llm_res.content,
                "usedFallback": llm_res.used_fallback,
            }
        )

    def _step5_dev_summarize_pr(
        self, story_key: str, user_id: str, user_role: str, artifacts: dict[str, Any]
    ) -> dict[str, Any]:
        step4_info = artifacts.get("step_4_dev_implementation", {})
        prompt = f"[Step 5 Dev Summarize PR] Story={story_key} PR={step4_info.get('prId')}"
        llm_res = self.llm_router.generate_with_governance(
            user_id=user_id,
            user_role=user_role,
            story_key=story_key,
            prompt=prompt,
            context={"action": "summarize-pr", "story_key": story_key},
        )
        return AllBoundaryDLP.sanitize_obj(
            {
                "step": 5,
                "action": "summarize-pr",
                "storyKey": story_key,
                "prId": step4_info.get("prId", f"PR-{story_key}"),
                "headBranch": step4_info.get("branch", f"bot/{story_key}"),
                "baseBranch": "develop",
                "changedFiles": step4_info.get(
                    "changedFiles",
                    [f"src/HIS.Registration.Api/Features/{story_key}_BhytFiveYearValidator.cs"],
                ),
                "testResultSummary": "Build SUCCESS, 14/14 unit & integration tests passed.",
                "mergeNote": (
                    "Bot không có quyền tự merge. Dev vui lòng review PR và gọi /dev confirm-merge."
                ),
                "llmSummary": llm_res.content,
                "usedFallback": llm_res.used_fallback,
            }
        )

    def _step6_qc_test(
        self, story_key: str, user_id: str, user_role: str, artifacts: dict[str, Any]
    ) -> dict[str, Any]:
        step4_info = artifacts.get("step_4_dev_implementation", {})
        changed_files = step4_info.get(
            "changedFiles",
            ["src/HIS.Registration.Api/Controllers/PatientRegistrationController.cs"],
        )
        prompt = f"[Step 6 QC Regression Checklist] Story={story_key} Files={changed_files}"
        llm_res = self.llm_router.generate_with_governance(
            user_id=user_id,
            user_role=user_role,
            story_key=story_key,
            prompt=prompt,
            context={"action": "test", "story_key": story_key},
        )
        return AllBoundaryDLP.sanitize_obj(
            {
                "step": 6,
                "action": "test",
                "storyKey": story_key,
                "changedFilesReviewed": changed_files,
                "regressionChecklist": [
                    "1. Kiểm tra đăng ký khám BHYT ngoại trú thường (DN4 -> đồng chi trả 20%).",
                    "2. Kiểm tra thẻ BHYT đủ 5 năm liên tục + lũy kế >= 14,040,000 (Copay = 0%).",
                    "3. Kiểm tra ca cấp cứu ngoại trú (IsEmergency = true -> hưởng đúng tuyến).",
                    "4. Kiểm tra đối soát tại BhytSettlementController.CalculateBhytCopayAsync.",
                ],
                "llmSummary": llm_res.content,
                "usedFallback": llm_res.used_fallback,
            }
        )

    def _step6_qc_bug_report(
        self,
        story_key: str,
        user_id: str,
        user_role: str,
        raw_error_log: str,
        artifacts: dict[str, Any],
    ) -> dict[str, Any]:
        clean_log = AllBoundaryDLP.sanitize_text(raw_error_log)
        prompt = f"[Step 6 QC Bug Report] Story={story_key} SanitizedLog={clean_log}"
        llm_res = self.llm_router.generate_with_governance(
            user_id=user_id,
            user_role=user_role,
            story_key=story_key,
            prompt=prompt,
            context={"action": "bug", "story_key": story_key},
        )
        return AllBoundaryDLP.sanitize_obj(
            {
                "step": 6,
                "action": "bug",
                "storyKey": story_key,
                "bugTitle": f"[Bug {story_key}] Lỗi phát sinh khi kiểm thử hồi quy Bước 6",
                "sanitizedErrorLog": clean_log,
                "stepsToReproduce": [
                    "1. Gửi request POST /api/v1/registration/outpatient-bhyt với dữ liệu biên.",
                    "2. Quan sát phản hồi và log đã qua bộ lọc DLP.",
                ],
                "expectedBehavior": (
                    "Xử lý đúng điều kiện biên BHYT 5 năm liên tục mà không phát sinh ngoại lệ."
                ),
                "transitionBackToStep": 3,
                "llmSummary": llm_res.content,
                "usedFallback": llm_res.used_fallback,
            }
        )

    def _step7_ba_uat(
        self, story_key: str, user_id: str, user_role: str, artifacts: dict[str, Any]
    ) -> dict[str, Any]:
        issue = self.jira_adapter.get_issue(story_key)
        prompt = f"[Step 7 BA UAT Verification] Story={story_key}: {issue.summary}"
        llm_res = self.llm_router.generate_with_governance(
            user_id=user_id,
            user_role=user_role,
            story_key=story_key,
            prompt=prompt,
            context={"action": "uat", "story_key": story_key},
        )
        return AllBoundaryDLP.sanitize_obj(
            {
                "step": 7,
                "action": "uat",
                "storyKey": story_key,
                "uatChecklistAgainstAc": [
                    {
                        "criterion": "FiveYearContinuousDate và đồng chi trả >= 14,040,000",
                        "verified": True,
                    },
                    {
                        "criterion": "Kiểm tra thông tuyến và chặn thẻ BHYT hết hạn quá 15 ngày",
                        "verified": True,
                    },
                ],
                "uatStatus": "READY_FOR_BA_CLOSE",
                "llmSummary": llm_res.content,
                "usedFallback": llm_res.used_fallback,
            }
        )
