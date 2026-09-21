# ECR Lifecycle Policy

Applies an [ECR lifecycle policy](https://docs.aws.amazon.com/AmazonECR/latest/userguide/LifecyclePolicies.html)
across every repository in an AWS account/region (or a filtered subset),
concurrently. Built for accounts with a large number of ECR repositories
(2000+) where clicking through each repo's lifecycle policy manually, or
managing 2000 separate Terraform resources, isn't practical.

## Usage

```yaml
- uses: aws-actions/configure-aws-credentials@v6
  with:
    role-to-assume: ${{ vars.AWS_ECR_ROLE_ARN }}
    aws-region: us-east-1

- uses: itstehcorner-org/ecr-lifecycle-policy@v1.0.1
  with:
    policy-file: policy.json
    region: us-east-1
    dry-run: false
    repository: '{"include": ["all"], "exclude": ["org/repo3"]}'
```

Requires AWS credentials already configured in the job (e.g. via
[`aws-actions/configure-aws-credentials`](https://github.com/aws-actions/configure-aws-credentials)
with OIDC federation) — this action does not set up AWS auth itself.

## Inputs

| Name | Required | Default | Description |
|---|---|---|---|
| `policy-file` | yes | | Path to the lifecycle policy JSON file, relative to the calling repo root. |
| `region` | yes | | AWS region, e.g. `us-east-1`. |
| `dry-run` | no | `true` | Discover and validate only, don't call `PutLifecyclePolicy`. |
| `repository` | no | | JSON string: `{"include": ["all"] or ["repo1", "org/*"], "exclude": ["repo3"]}`. `"all"` in `include` means every repository in the account/region, and any other entries alongside it are ignored. Exclude patterns are applied after include is resolved. Takes priority over `repo-filter` if both are set. |
| `repo-filter` | no | `*` | Glob pattern for repos to include, used only if `repository` is not set. |
| `aws-account-id` | no | | Sanity check: fails before making any changes if the assumed role isn't in this account. |
| `max-workers` | no | `20` | Concurrent `PutLifecyclePolicy` calls. |

## How it works

1. Paginates `describe_repositories` to discover every ECR repo in the
   account/region.
2. Resolves the final repo list from `repository` (include/exclude, with an
   `"all"` sentinel) or the simpler `repo-filter` glob.
3. Applies the policy to each resolved repo concurrently
   (`ThreadPoolExecutor`, `max-workers` at a time), with adaptive
   retry/backoff for ECR's account-level API throttling.
4. On `dry-run: true`, only logs what would happen — no `PutLifecyclePolicy`
   calls are made.
5. Writes a markdown results table (repo, success/fail, error) to the GitHub
   Actions Job Summary (`$GITHUB_STEP_SUMMARY`), in addition to per-repo log
   lines.
6. Exits non-zero if any repo failed, so the workflow run fails visibly.

## Testing locally without publishing

Run the script directly, no Actions involved:

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

export AWS_PROFILE=<your-profile>
python apply_lifecycle_policy.py \
  --policy-file policy.example.json \
  --region eu-west-1 \
  --dry-run
```

Or exercise the composite action itself, still inside this repo, using
`uses: ./` instead of a published `owner/repo@tag` reference — see
`.github/workflows/ecr.yaml` for the real consumer-facing example, and swap
`uses: itstehcorner-org/ecr-lifecycle-policy@main` for `uses: ./` in a
throwaway workflow to test the action wrapper end-to-end before publishing.

## License

[MIT](LICENSE)
