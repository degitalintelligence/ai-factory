"""Deterministic repository audit scope. Model prose never chooses its own baseline."""

import re

SELF_REPOSITORY = "degitalintelligence/ai-factory"


def repository_audit(requirement: str) -> bool:
    return bool(
        re.search(r"\baudit\b", requirement, re.I)
        and re.search(r"\b(?:repository|repo|current main)\b", requirement, re.I)
    )


def readonly_repository_audit(requirement: str) -> bool:
    # Added clarification must be resolved normally, never inherit a read-only contract.
    if re.search(r"User clarification:", requirement, re.I):
        return False
    return repository_audit(requirement) and bool(
        re.search(r"jangan (?:mengubah|melakukan perubahan)|read[- ]only|review[- ]only", requirement, re.I)
    )


def referenced_tasks(requirement: str) -> set[int]:
    return {
        int(value) for value in re.findall(r"\b(?:task|tujuan|intent)\s*#?\s*(\d+)\b", requirement, re.I)[:10]
    }


def target_project(requirement: str, project: str, projects: dict) -> str:
    if not repository_audit(requirement):
        return project
    named = {
        alias
        for alias, policy in projects.items()
        if re.search(r"(?<![\w-])" + re.escape(policy.repo) + r"(?![\w-])", requirement, re.I)
        or re.search(r"(?<![\w-])" + re.escape(policy.repo.split("/")[1]) + r"(?![\w-])", requirement, re.I)
    }
    if project:
        if named and project not in named:
            raise ValueError("Audit target conflicts with the explicit project; specify one repository")
        return project
    if len(named) != 1:
        raise ValueError("Repository audit requires one registered target project; specify its alias")
    return named.pop()


def validate_sha(sha: str | None) -> str:
    if not sha or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(
            "Repository audit requires an exact 40-character base commit SHA before context gathering"
        )
    return sha
