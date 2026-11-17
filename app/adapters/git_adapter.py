"""Local Repo Git Adapter enforcing hard bot/<key> branch guard and idempotent PR creation."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.adapters.interfaces import GitAdapterProtocol, PullRequestInfo
from app.config import get_settings
from app.security.dlp import AllBoundaryDLP
from sample_data.init_sample_repo import ensure_sample_git_repo

PROTECTED_BRANCHES: frozenset[str] = frozenset({"main", "develop", "staging", "master"})
FORBIDDEN_BRANCH_SUBSTRINGS: tuple[str, ...] = (
    ":",
    "..",
    "~",
    "^",
    "\\",
    " ",
    "\t",
    "refs/",
    "@{",
)
VALID_BOT_BRANCH_PATTERN = re.compile(r"^bot/(?P<key>[A-Za-z0-9._-]+)$")


class ProtectedBranchError(PermissionError):
    """Raised when any operation attempts to write to a protected branch or invalid refspec."""


def _validate_bot_write_branch(branch: str) -> str:
    """Strictly validate that a target write branch matches 'bot/<key>' without refspec."""
    if not isinstance(branch, str):
        raise ProtectedBranchError("Branch name must be a string.")
    cleaned = branch.strip()
    if not cleaned or cleaned.startswith("+") or cleaned.startswith("-"):
        raise ProtectedBranchError(f"Invalid or option/refspec-prefixed branch name: {branch!r}")
    for token in FORBIDDEN_BRANCH_SUBSTRINGS:
        if token in cleaned:
            raise ProtectedBranchError(
                f"Forbidden refspec or path traversal token {token!r} in branch: {branch!r}"
            )
    if cleaned.lower() in PROTECTED_BRANCHES:
        raise ProtectedBranchError(
            f"Direct write to protected branch {branch!r} is strictly forbidden."
        )
    match = VALID_BOT_BRANCH_PATTERN.fullmatch(cleaned)
    if not match:
        raise ProtectedBranchError(
            f"Bot is strictly restricted to 'bot/<key>' branches. Rejected branch: {branch!r}"
        )
    key_part = match.group("key").lower()
    if key_part in PROTECTED_BRANCHES:
        raise ProtectedBranchError(f"Branch key cannot shadow protected branch name: {branch!r}")
    return cleaned


class LocalRepoGitAdapter(GitAdapterProtocol):
    """GitAdapter operating on the local HIS staging repository without any merge() capability."""

    def __init__(self, repo_path: str | Path | None = None) -> None:
        settings = get_settings()
        self.repo_dir = Path(repo_path or settings.his_repo_path).resolve()
        if not (self.repo_dir / ".git").exists():
            ensure_sample_git_repo(self.repo_dir)
        self._pr_store_path = self.repo_dir / ".git" / "mock_pull_requests.json"
        self._pull_requests: dict[tuple[str, str], PullRequestInfo] = {}
        self._load_persisted_prs()

    def _load_persisted_prs(self) -> None:
        if not self._pr_store_path.exists():
            return
        try:
            raw_list = json.loads(self._pr_store_path.read_text(encoding="utf-8"))
            for item in raw_list:
                pr = PullRequestInfo(
                    pr_id=item["pr_id"],
                    head_branch=item["head_branch"],
                    base_branch=item["base_branch"],
                    title=item["title"],
                    body=item["body"],
                    head_commit_sha=item["head_commit_sha"],
                    status=item.get("status", "OPEN"),
                )
                self._pull_requests[(pr.head_branch, pr.base_branch)] = pr
        except Exception:
            pass

    def _save_persisted_prs(self) -> None:
        try:
            payload = [asdict(pr) for pr in self._pull_requests.values()]
            self._pr_store_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _git(self, args: list[str]) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.repo_dir),
            check=True,
            capture_output=True,
            text=True,
        )
        return proc.stdout.strip()

    def get_branch_commit_sha(self, branch: str) -> str:
        """Return current 40-char commit SHA for a given branch."""
        return self._git(["rev-parse", f"refs/heads/{branch}"])

    def ensure_bot_branch(self, branch: str, from_ref: str = "develop") -> str:
        """Idempotently create or checkout 'bot/<key>' from 'develop'."""
        valid_branch = _validate_bot_write_branch(branch)
        if from_ref != "develop":
            raise ProtectedBranchError(
                f"Bot branches may only be branched from 'develop', rejected from_ref={from_ref!r}"
            )

        branch_exists = False
        try:
            chk = subprocess.run(
                ["git", "rev-parse", "--verify", f"refs/heads/{valid_branch}"],
                cwd=str(self.repo_dir),
                capture_output=True,
                check=False,
            )
            branch_exists = (chk.returncode == 0)
        except Exception:
            branch_exists = False

        if branch_exists:
            self._git(["checkout", valid_branch])
        else:
            self._git(["checkout", "-b", valid_branch, f"refs/heads/{from_ref}"])

        return self.get_branch_commit_sha(valid_branch)

    def commit_files(self, branch: str, files: dict[str, str], commit_message: str) -> str:
        """Write DLP-sanitized files onto 'bot/<key>' and commit."""
        valid_branch = _validate_bot_write_branch(branch)
        self.ensure_bot_branch(valid_branch, from_ref="develop")

        rel_paths: list[str] = []
        for rel_path, content in files.items():
            clean_rel = rel_path.lstrip("/\\")
            parts = Path(clean_rel).parts
            if ".." in parts or any(p.startswith(".git") for p in parts):
                raise ProtectedBranchError(f"Invalid or forbidden file path: {rel_path!r}")
            target_file = (self.repo_dir / clean_rel).resolve()
            if not target_file.is_relative_to(self.repo_dir):
                raise ProtectedBranchError(f"Path traversal detected: {rel_path!r}")
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(AllBoundaryDLP.sanitize_text(content), encoding="utf-8")
            rel_paths.append(clean_rel)

        if rel_paths:
            self._git(["add", *rel_paths])
        status = self._git(["status", "--porcelain"])
        clean_msg = AllBoundaryDLP.sanitize_text(commit_message)
        if status:
            self._git(["commit", "-m", clean_msg])

        sha = self.get_branch_commit_sha(valid_branch)
        # Switch working tree back to staging so kb status stays clean on staging
        self._git(["checkout", "staging"])
        return sha

    def push_branch(self, branch: str) -> str:
        """Validate branch guard for push operation and return branch commit SHA."""
        valid_branch = _validate_bot_write_branch(branch)
        return self.get_branch_commit_sha(valid_branch)

    def run_mock_build_and_test(self, branch: str) -> dict[str, Any]:
        """Run simulated build & test suite on the bot/<key> branch."""
        valid_branch = _validate_bot_write_branch(branch)
        sha = self.get_branch_commit_sha(valid_branch)
        return {
            "branch": valid_branch,
            "commitSha": sha,
            "buildStatus": "SUCCESS",
            "testsPassed": 14,
            "testsFailed": 0,
        }

    def create_or_update_pull_request(
        self,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> PullRequestInfo:
        """Idempotently open or update a Pull Request strictly from 'bot/<key>' into 'develop'."""
        valid_head = _validate_bot_write_branch(head_branch)
        if base_branch != "develop":
            raise ProtectedBranchError(
                f"Pull Requests from bot branches must target 'develop' only. "
                f"Rejected base_branch={base_branch!r}"
            )

        sha = self.get_branch_commit_sha(valid_head)
        clean_title = AllBoundaryDLP.sanitize_text(title)
        clean_body = AllBoundaryDLP.sanitize_text(body)
        pr_key = (valid_head, base_branch)

        self._load_persisted_prs()
        existing = self._pull_requests.get(pr_key)
        if existing is not None and existing.status == "OPEN":
            existing.title = clean_title
            existing.body = clean_body
            existing.head_commit_sha = sha
            self._save_persisted_prs()
            return existing

        story_suffix = valid_head.split("/", 1)[1]
        pr_info = PullRequestInfo(
            pr_id=f"PR-{story_suffix}",
            head_branch=valid_head,
            base_branch="develop",
            title=clean_title,
            body=clean_body,
            head_commit_sha=sha,
            status="OPEN",
        )
        self._pull_requests[pr_key] = pr_info
        self._save_persisted_prs()
        return pr_info

    def list_open_pull_requests(self, head_branch: str | None = None) -> list[PullRequestInfo]:
        """Return open pull requests (optionally filtered by head_branch)."""
        self._load_persisted_prs()
        prs = [pr for pr in self._pull_requests.values() if pr.status == "OPEN"]
        if head_branch is not None:
            prs = [pr for pr in prs if pr.head_branch == head_branch]
        return prs


_GLOBAL_GIT_ADAPTER: LocalRepoGitAdapter | None = None


def get_git_adapter() -> LocalRepoGitAdapter:
    """Return singleton LocalRepoGitAdapter instance synchronized with current settings."""
    global _GLOBAL_GIT_ADAPTER
    target_dir = get_settings().resolved_repo_path
    if _GLOBAL_GIT_ADAPTER is None or _GLOBAL_GIT_ADAPTER.repo_dir != target_dir:
        _GLOBAL_GIT_ADAPTER = LocalRepoGitAdapter(target_dir)
    return _GLOBAL_GIT_ADAPTER
