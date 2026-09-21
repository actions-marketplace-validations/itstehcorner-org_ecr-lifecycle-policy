#!/usr/bin/env python3
"""Apply an ECR lifecycle policy across every repository in an account/region.

Usage:
    python apply_lifecycle_policy.py --policy-file policy.json --region eu-west-1 [--dry-run] [--repo-filter 'prefix-*'] [--max-workers 20]
"""

import argparse
import fnmatch
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s level=%(levelname)s %(message)s",
)
log = logging.getLogger("ecr-lifecycle-policy")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--policy-file", required=True, help="Path to the lifecycle policy JSON file")
    p.add_argument("--region", required=True, help="AWS region, e.g. eu-west-1")
    p.add_argument("--dry-run", action="store_true", help="Discover and validate only, don't call PutLifecyclePolicy")
    p.add_argument(
        "--repo-filter",
        default="*",
        help="Glob pattern to limit which repos get the policy applied (default: all repos, '*'). "
        "Ignored if --repository-config is given.",
    )
    p.add_argument(
        "--repository-config",
        help='JSON string or path to a JSON file: {"include": ["all"] or ["repo1", "org/*"], '
        '"exclude": ["repo3"]}. "all" in include means every repository. Takes priority over --repo-filter.',
    )
    p.add_argument("--aws-account-id", help="Sanity check: fail if the assumed role's account doesn't match")
    p.add_argument("--max-workers", type=int, default=20, help="Concurrent PutLifecyclePolicy calls (default: 20)")
    return p.parse_args()


def load_repository_config(raw):
    # Accept either a literal JSON string (typical from a GitHub Actions
    # `with:` input) or a path to a JSON file.
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        with open(raw) as f:
            return json.load(f)


def resolve_repositories(ecr, repo_filter, repository_config):
    all_repos = discover_repositories(ecr, "*")

    if not repository_config:
        return sorted(r for r in all_repos if fnmatch.fnmatch(r, repo_filter))

    include = repository_config.get("include", ["all"])
    exclude = repository_config.get("exclude", [])

    if any(p.lower() == "all" for p in include):
        others = [p for p in include if p.lower() != "all"]
        if others:
            log.info("include contains 'all' - ignoring other include entries: %s", others)
        selected = set(all_repos)
    else:
        selected = set()
        for pattern in include:
            matched = [r for r in all_repos if fnmatch.fnmatch(r, pattern)]
            if not matched:
                log.warning("include pattern %r matched no existing repositories", pattern)
            selected.update(matched)

    for pattern in exclude:
        before = len(selected)
        selected = {r for r in selected if not fnmatch.fnmatch(r, pattern)}
        if before != len(selected):
            log.info("exclude pattern %r removed %d repositories", pattern, before - len(selected))

    return sorted(selected)


def make_ecr_client(region):
    # Adaptive retry mode backs off automatically on ECR's account-level
    # TPS throttling instead of failing fast - required at this scale
    # (2000+ repos = 2000+ back-to-back API calls).
    config = Config(region_name=region, retries={"max_attempts": 10, "mode": "adaptive"})
    return boto3.client("ecr", config=config)


def discover_repositories(ecr, repo_filter):
    repo_names = []
    paginator = ecr.get_paginator("describe_repositories")
    for page in paginator.paginate():
        for repo in page["repositories"]:
            name = repo["repositoryName"]
            if fnmatch.fnmatch(name, repo_filter):
                repo_names.append(name)
    return repo_names


def apply_policy_to_repo(ecr, repo_name, policy_text, dry_run):
    if dry_run:
        log.info("[dry-run] would apply lifecycle policy to %s", repo_name)
        return repo_name, True, None

    try:
        ecr.put_lifecycle_policy(repositoryName=repo_name, lifecyclePolicyText=policy_text)
        log.info("applied lifecycle policy to %s", repo_name)
        return repo_name, True, None
    except Exception as e:
        log.error("failed to apply lifecycle policy to %s: %s", repo_name, e)
        return repo_name, False, str(e)


def write_step_summary(results, dry_run):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    succeeded = [r for r in results if r[1]]
    failed = [r for r in results if not r[1]]
    action_word = "Would apply" if dry_run else "Applied"

    lines = [
        f"## ECR Lifecycle Policy {'(dry run)' if dry_run else ''}",
        "",
        f"**{action_word}**: {len(succeeded)} succeeded, {len(failed)} failed, {len(results)} total",
        "",
        "| Repository | Status | Error |",
        "|---|---|---|",
    ]
    for name, ok, error in sorted(results, key=lambda r: (r[1], r[0])):
        lines.append(f"| {name} | {'✅' if ok else '❌'} | {error or ''} |")

    with open(summary_path, "a") as f:
        f.write("\n".join(lines) + "\n")


def main():
    args = parse_args()

    with open(args.policy_file) as f:
        policy_text = f.read()
    # Fail fast on invalid JSON rather than letting every one of 2000 API
    # calls reject with the same error.
    json.loads(policy_text)

    ecr = make_ecr_client(args.region)

    if args.aws_account_id:
        caller = boto3.client("sts", region_name=args.region).get_caller_identity()
        if caller["Account"] != args.aws_account_id:
            log.error(
                "assumed role's account (%s) doesn't match --aws-account-id (%s), refusing to continue",
                caller["Account"], args.aws_account_id,
            )
            sys.exit(1)

    repository_config = load_repository_config(args.repository_config) if args.repository_config else None

    log.info("discovering repositories...")
    repo_names = resolve_repositories(ecr, args.repo_filter, repository_config)
    log.info("found %d repositories matching filter", len(repo_names))

    if not repo_names:
        log.warning("no repositories matched, nothing to do")
        return

    results = []
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        futures = {
            pool.submit(apply_policy_to_repo, ecr, name, policy_text, args.dry_run): name
            for name in repo_names
        }
        for future in as_completed(futures):
            results.append(future.result())

    succeeded = [r for r in results if r[1]]
    failed = [r for r in results if not r[1]]

    write_step_summary(results, args.dry_run)

    log.info("done: %d succeeded, %d failed (of %d total)", len(succeeded), len(failed), len(results))
    if failed:
        log.error("failures:")
        for name, _, error in failed:
            log.error("  %s: %s", name, error)
        sys.exit(1)


if __name__ == "__main__":
    main()
