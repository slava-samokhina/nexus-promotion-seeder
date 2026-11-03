#!/usr/bin/env python3
"""Seed UAT/Prod promotion pipelines into a target GitHub repository.

Desired state is nexus_configs/promotion_seed.yaml. GitHub calls use the
Nexus-repo-creator installation token (GH_TOKEN) — same app pattern as
scion-deployment's nexus_project_vending.py. This seeder only writes
GitHub config (workflows, environments, variables, settings, ruleset).
It does not create GCP projects, SAs, WIF pools, or Artifact Registry.

Dispatch inputs arrive as environment variables (see
seed-promotion-pipelines.yaml). Every GitHub write is idempotent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - workflow installs PyYAML
    sys.stderr.write(
        "PyYAML is required. Install with: python3 -m pip install pyyaml\n"
    )
    sys.exit(1)

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "nexus_configs" / "promotion_seed.yaml"
CODEOWNERS_HEADER = """\
# Code owners for promotion manifests.
#
# IMPORTANT: this is intentionally scoped ONLY to the promotion paths.
# It does NOT gate normal dev PRs — Scion agents keep creating/merging PRs freely.
# A PR only requires an owner's approval if it touches a path listed below.
"""
SECTION = "----------------------"


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 1) -> None:
    sys.stderr.write(msg.rstrip() + "\n")
    sys.stderr.flush()
    sys.exit(code)


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        die(f"missing required env {name}")
    return value


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    log(f"+ {' '.join(args)}")
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        input=input_text,
        capture_output=capture,
    )


def gh_api(
    method: str,
    path: str,
    *,
    fields: dict | None = None,
    raw_fields: dict | None = None,
    input_json: dict | None = None,
    allow_statuses: tuple[int, ...] = (),
) -> subprocess.CompletedProcess:
    """Call `gh api`. path is e.g. repos/owner/repo/environments/uat."""
    args = ["gh", "api", "-X", method, path]
    if input_json is not None:
        args += ["--input", "-"]
        body = json.dumps(input_json)
    else:
        body = None
        for key, value in (fields or {}).items():
            if isinstance(value, bool):
                args += ["-F", f"{key}={'true' if value else 'false'}"]
            else:
                args += ["-F", f"{key}={value}"]
        for key, value in (raw_fields or {}).items():
            args += ["-f", f"{key}={value}"]
    result = run(
        args,
        check=False,
        capture=True,
        input_text=body,
    )
    # gh prints HTTP status on failure to stderr; also embeds in output sometimes.
    if result.returncode != 0:
        # Retry-friendly: caller may allow specific statuses via parsing.
        err = (result.stderr or "") + (result.stdout or "")
        for status in allow_statuses:
            if f"HTTP {status}" in err or f"(HTTP {status})" in err:
                return result
        die(f"gh api {method} {path} failed:\n{err}")
    return result


def load_manifest() -> dict:
    if not CONFIG_PATH.is_file():
        die(f"manifest not found: {CONFIG_PATH}")
    with CONFIG_PATH.open() as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        die(f"{CONFIG_PATH}: expected a mapping")
    return data


def resolve_tiers(manifest: dict, tiers_input: str) -> list[str]:
    available = list((manifest.get("tiers") or {}).keys())
    choice = (tiers_input or "both").strip().lower()
    if choice == "both":
        selected = [t for t in ("uat", "prod") if t in available]
    elif choice in available:
        selected = [choice]
    else:
        die(f"tiers must be both|{'|'.join(available)}, got {tiers_input!r}")
    if not selected:
        die("no tiers selected")
    return selected


def normalize_owner_handle(raw: str) -> str:
    """Accept '@user', 'user', or '@org/team' → strip a single leading @ for tmpl."""
    handle = raw.strip()
    if not handle:
        die("codeowners_owner is empty")
    # Manifest lines already include '@{{CODEOWNERS_OWNER}}'; store without @.
    return handle[1:] if handle.startswith("@") else handle


def parse_resolved_vars(raw: str) -> dict[str, str]:
    text = (raw or "").strip() or "{}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        die(f"resolved_vars is not valid JSON: {exc}")
    if not isinstance(data, dict):
        die("resolved_vars must be a JSON object")
    out: dict[str, str] = {}
    for key, value in data.items():
        if value is None or value == "":
            continue
        out[str(key)] = str(value)
    return out


def ensure_target_clone(target_repo: str, work: Path) -> str:
    """Clone target into work. Return default branch name (usually main)."""
    token = env("GH_TOKEN")
    url = f"https://x-access-token:{token}@github.com/{target_repo}.git"
    if work.exists():
        shutil.rmtree(work)
    # Empty repos clone successfully but have no HEAD/branch.
    result = run(
        ["git", "clone", "--quiet", url, str(work)],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        die(f"git clone failed:\n{result.stderr or result.stdout}")

    run(
        ["git", "-C", str(work), "config", "user.name", "nexus-promotion-seeder"],
        check=True,
    )
    run(
        ["git", "-C", str(work), "config", "user.email", "nexus-promotion-seeder[bot]@users.noreply.github.com"],
        check=True,
    )

    # Detect existing default branch.
    head = run(
        ["git", "-C", str(work), "rev-parse", "--verify", "HEAD"],
        check=False,
        capture=True,
    )
    if head.returncode != 0:
        log("target repo has no commits; creating main")
        run(["git", "-C", str(work), "checkout", "-B", "main"], check=True)
        readme = work / "README.md"
        if not readme.exists():
            readme.write_text(f"# {target_repo.split('/')[-1]}\n")
        return "main"

    # Prefer remote HEAD symbolic-ref, else current branch.
    sym = run(
        ["git", "-C", str(work), "symbolic-ref", "refs/remotes/origin/HEAD"],
        check=False,
        capture=True,
    )
    if sym.returncode == 0:
        ref = (sym.stdout or "").strip()  # refs/remotes/origin/main
        if ref.startswith("refs/remotes/origin/"):
            return ref.rsplit("/", 1)[-1]
    branch = run(
        ["git", "-C", str(work), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture=True,
    )
    return (branch.stdout or "main").strip()


def write_seed_files(
    work: Path,
    manifest: dict,
    selected: list[str],
    codeowners_owner: str,
) -> list[str]:
    written: list[str] = []
    tiers = manifest["tiers"]

    for name in selected:
        tier = tiers[name]
        for entry in tier.get("files") or []:
            src = ROOT / entry["src"]
            dest = work / entry["dest"]
            if not src.is_file():
                die(f"template missing: {src}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            written.append(entry["dest"])

        keep_src = tier.get("release_keep")
        if keep_src:
            src = ROOT / keep_src
            # dest path: release/<tier>/.gitkeep
            dest = work / "release" / name / ".gitkeep"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.is_file():
                shutil.copy2(src, dest)
            else:
                dest.write_text("")
            written.append(str(dest.relative_to(work)))

    # CODEOWNERS: header + selected tier lines (idempotent rewrite of managed block).
    co_path = work / ".github" / "CODEOWNERS"
    co_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [CODEOWNERS_HEADER.rstrip(), ""]
    for name in selected:
        line = (tiers[name].get("codeowners") or "").replace(
            "{{CODEOWNERS_OWNER}}", codeowners_owner
        )
        if line:
            lines.append(line)
    co_path.write_text("\n".join(lines) + "\n")
    written.append(".github/CODEOWNERS")
    return written


def commit_and_push(
    work: Path,
    target_repo: str,
    branch: str,
    selected: list[str],
    push_mode: str,
) -> str:
    run(["git", "-C", str(work), "add", "-A"], check=True)
    status = run(
        ["git", "-C", str(work), "status", "--porcelain"],
        check=True,
        capture=True,
    )
    if not (status.stdout or "").strip():
        log("no file changes to commit (already seeded)")
        return "unchanged"

    msg = f"seed: promotion pipelines ({','.join(selected)})"
    run(["git", "-C", str(work), "commit", "-m", msg], check=True)

    mode = (push_mode or "direct").strip().lower()
    if mode == "direct":
        run(
            ["git", "-C", str(work), "push", "origin", f"HEAD:{branch}"],
            check=True,
        )
        return f"pushed to {branch}"

    if mode != "pr":
        die(f"push_mode must be direct|pr, got {push_mode!r}")

    seed_branch = f"seed/promotion-pipelines-{'-'.join(selected)}"
    run(["git", "-C", str(work), "checkout", "-B", seed_branch], check=True)
    run(
        ["git", "-C", str(work), "push", "-u", "origin", seed_branch, "--force"],
        check=True,
    )
    # Open or reuse PR
    existing = run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            target_repo,
            "--head",
            seed_branch,
            "--base",
            branch,
            "--json",
            "url",
            "--jq",
            ".[0].url // empty",
        ],
        check=False,
        capture=True,
    )
    url = (existing.stdout or "").strip()
    if url:
        log(f"PR already open: {url}")
        return f"pr {url}"

    created = run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            target_repo,
            "--base",
            branch,
            "--head",
            seed_branch,
            "--title",
            msg,
            "--body",
            "Seeded by nexus-promotion-seeder. Merge to install/update promotion pipelines.",
        ],
        check=True,
        capture=True,
    )
    return f"pr {(created.stdout or '').strip()}"


def ensure_environment(target_repo: str, name: str) -> None:
    # PUT is idempotent create-or-update with no reviewers.
    gh_api(
        "PUT",
        f"repos/{target_repo}/environments/{name}",
        input_json={
            "wait_timer": 0,
            "prevent_self_review": False,
            "reviewers": [],
            "deployment_branch_policy": None,
        },
    )
    log(f"environment {name}: upserted")


def set_variables(
    target_repo: str,
    manifest: dict,
    resolved: dict[str, str],
) -> tuple[list[str], list[str]]:
    set_names: list[str] = []
    skipped: list[str] = []

    constants = manifest.get("variables_constant") or {}
    for name, value in constants.items():
        run(
            [
                "gh",
                "variable",
                "set",
                str(name),
                "--repo",
                target_repo,
                "--body",
                str(value),
            ],
            check=True,
        )
        set_names.append(str(name))

    for name in manifest.get("variables_from_input") or []:
        key = str(name)
        if key in resolved:
            run(
                [
                    "gh",
                    "variable",
                    "set",
                    key,
                    "--repo",
                    target_repo,
                    "--body",
                    resolved[key],
                ],
                check=True,
            )
            set_names.append(key)
        else:
            skipped.append(key)
            log(f"variable {key}: skipped (not in resolved_vars)")

    return set_names, skipped


def apply_repo_settings(target_repo: str, manifest: dict) -> None:
    settings = manifest.get("repo_settings") or {}
    if settings.get("allow_auto_merge"):
        gh_api(
            "PATCH",
            f"repos/{target_repo}",
            fields={"allow_auto_merge": True},
        )
        log("repo setting: allow_auto_merge=true")

    perms = settings.get("default_workflow_permissions", "read")
    can_approve = bool(settings.get("actions_can_create_and_approve_prs"))
    gh_api(
        "PUT",
        f"repos/{target_repo}/actions/permissions/workflow",
        raw_fields={"default_workflow_permissions": str(perms)},
        fields={"can_approve_pull_request_reviews": can_approve},
    )
    log(
        f"actions workflow permissions: default={perms} "
        f"can_approve_pull_request_reviews={can_approve}"
    )


def collect_required_checks(
    manifest: dict,
    selected: list[str],
    add_prod_check: bool,
) -> list[dict]:
    ruleset = manifest.get("ruleset") or {}
    integration_id = int(ruleset.get("actions_integration_id") or 15368)
    contexts: list[str] = []
    for name in selected:
        for ctx in (manifest["tiers"][name].get("required_checks") or []):
            if ctx == "prod-checks" and not add_prod_check:
                log("phase 1: omitting prod-checks from ruleset (add_prod_check=false)")
                continue
            if ctx not in contexts:
                contexts.append(ctx)
    return [{"context": c, "integration_id": integration_id} for c in contexts]


def build_ruleset_payload(
    manifest: dict,
    selected: list[str],
    add_prod_check: bool,
) -> dict:
    cfg = manifest.get("ruleset") or {}
    rules: list[dict] = []
    if cfg.get("block_deletion", True):
        rules.append({"type": "deletion"})
    if cfg.get("block_force_push", True):
        rules.append({"type": "non_fast_forward"})
    if cfg.get("require_pull_request", True):
        rules.append(
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": int(
                        cfg.get("required_approving_reviews", 0)
                    ),
                    "dismiss_stale_reviews_on_push": False,
                    "required_reviewers": [],
                    "require_code_owner_review": bool(
                        cfg.get("require_code_owner_review", True)
                    ),
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": False,
                },
            }
        )
    checks = collect_required_checks(manifest, selected, add_prod_check)
    if checks:
        rules.append(
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": bool(
                        cfg.get("require_branches_up_to_date", False)
                    ),
                    "do_not_enforce_on_create": False,
                    "required_status_checks": checks,
                },
            }
        )

    return {
        "name": cfg.get("name") or "main-guard",
        "target": "branch",
        "enforcement": "active",
        "conditions": {
            "ref_name": {
                "include": [cfg.get("target_branch") or "~DEFAULT_BRANCH"],
                "exclude": [],
            }
        },
        "rules": rules,
    }


def upsert_ruleset(
    target_repo: str,
    manifest: dict,
    selected: list[str],
    add_prod_check: bool,
) -> str:
    payload = build_ruleset_payload(manifest, selected, add_prod_check)
    name = payload["name"]

    listed = gh_api("GET", f"repos/{target_repo}/rulesets", allow_statuses=())
    try:
        rulesets = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        die(f"could not parse rulesets list: {listed.stdout}")

    existing_id = None
    for item in rulesets:
        if isinstance(item, dict) and item.get("name") == name:
            existing_id = item.get("id")
            break

    if existing_id is None:
        gh_api(
            "POST",
            f"repos/{target_repo}/rulesets",
            input_json=payload,
        )
        log(f"ruleset {name}: created")
        return "created"

    gh_api(
        "PUT",
        f"repos/{target_repo}/rulesets/{existing_id}",
        input_json=payload,
    )
    log(f"ruleset {name}: updated (id={existing_id})")
    return f"updated:{existing_id}"


def main() -> None:
    target_repo = env("TARGET_REPO").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", target_repo):
        die(f"TARGET_REPO must be owner/repo, got {target_repo!r}")

    tiers_input = os.environ.get("TIERS", "both")
    codeowners_owner = normalize_owner_handle(env("CODEOWNERS_OWNER"))
    push_mode = os.environ.get("PUSH_MODE", "direct")
    apply_ruleset = os.environ.get("APPLY_RULESET", "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    add_prod_check = os.environ.get("ADD_PROD_CHECK", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    resolved = parse_resolved_vars(os.environ.get("RESOLVED_VARS", "{}"))

    if not os.environ.get("GH_TOKEN"):
        die("GH_TOKEN is required (installation token for the target repo)")

    manifest = load_manifest()
    selected = resolve_tiers(manifest, tiers_input)

    log(SECTION)
    log(f"target:     {target_repo}")
    log(f"tiers:      {', '.join(selected)}")
    log(f"push_mode:  {push_mode}")
    log(f"ruleset:    {apply_ruleset} (add_prod_check={add_prod_check})")
    log(f"codeowners: @{codeowners_owner}")
    log(SECTION)

    summary: dict = {}

    with tempfile.TemporaryDirectory(prefix="promo-seed-") as tmp:
        work = Path(tmp) / "repo"
        branch = ensure_target_clone(target_repo, work)
        written = write_seed_files(work, manifest, selected, codeowners_owner)
        push_result = commit_and_push(
            work, target_repo, branch, selected, push_mode
        )
        summary["files"] = written
        summary["push"] = push_result
        summary["branch"] = branch

    # Environments
    envs = []
    for name in selected:
        env_name = manifest["tiers"][name].get("environment") or name
        ensure_environment(target_repo, env_name)
        envs.append(env_name)
    summary["environments"] = envs

    # Variables
    set_names, skipped = set_variables(target_repo, manifest, resolved)
    summary["variables_set"] = set_names
    summary["variables_skipped"] = skipped

    # Repo settings
    apply_repo_settings(target_repo, manifest)
    summary["repo_settings"] = "applied"

    # Ruleset last — do not lock out the file push above.
    if apply_ruleset:
        summary["ruleset"] = upsert_ruleset(
            target_repo, manifest, selected, add_prod_check
        )
    else:
        summary["ruleset"] = "skipped"
        log("ruleset: skipped (APPLY_RULESET=false)")

    log(SECTION)
    log("seed summary")
    log(json.dumps(summary, indent=2))
    log(SECTION)
    log("done")


if __name__ == "__main__":
    main()
