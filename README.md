# nexus-promotion-seeder

Dispatch-triggered orchestrator that drops templated **UAT + Prod** promotion
pipelines into an existing app repository and wires everything they need to
run: workflows, `release/{uat,prod}/` paths, CODEOWNERS, GitHub environments,
repository variables, repo settings, and the `main-guard` ruleset.

A developer never hand-adds a deployment pipeline again — they create the app
(via project vending), work in Dev, and when they are ready to promote, this
seeding runs and the pipelines are pre-seeded.

## Where this sits

```
project vending  →  app repo + GCP Dev
       ↓
(later) UAT/Prod GCP projects exist
       ↓
THIS SEEDER     →  promotion workflows + GitHub config on the app repo
       ↓
developer promotes Dev → UAT → Prod
```

Source-of-truth pipeline behaviour lives in the templates under
`templates/workflows/` (originally taken from
[scion-testing](https://github.com/slava-samokhina/scion-testing)). Templates
consume every per-repo value through `${{ vars.* }}`, so seeding is **copy
files verbatim + set variables** — no find-and-replace inside YAML.

## Layout

```
.github/
  workflows/seed-promotion-pipelines.yaml   # workflow_dispatch orchestrator
  scripts/seed_promotion_pipelines.py       # git + gh work, driven by manifest
nexus_configs/
  promotion_seed.yaml                       # declarative "what a seeded repo looks like"
templates/
  workflows/                                # 6 promotion workflows (verbatim copy)
  CODEOWNERS.tmpl
  release/{uat,prod}/.gitkeep
```

Same config-driven shape as
[scion-deployment](https://github.com/nexus-software-factory/scion-deployment)
vending (`nexus_configs/` + `.github/scripts/` + App token).

## Coordination with project vending

Checked against `nexus_project_vending.py` in scion-deployment:

| Concern | Vending today | This seeder |
|---|---|---|
| Create empty app repo + initial `main` commit | Yes | Assumes repo already exists (handles empty `main`) |
| Grant owners admin | Yes | No |
| Branch protection / ruleset | **No** | Owns `main-guard` |
| Environments / promotion vars / CODEOWNERS | No | Yes |

Vending does **not** apply base branch protection. Until that changes, this
seeder is the sole owner of `main-guard`. If vending later adds a ruleset,
agree on one named ruleset that the last step upserts — do not stack two
conflicting rulesets on `main`.

## Prerequisites on this repo

Settings → Secrets and variables → Actions:

| Kind | Name | Notes |
|---|---|---|
| Variable | `NEXUS_APP_CLIENT_ID` | GitHub App client ID (same as scion-deployment) |
| Secret | `NEXUS_APP_PRIVATE_KEY` | Full PEM including BEGIN/END lines |

Install the App on the **target owner** (org or user) with the target repo
selected (or All repositories). Permissions needed to seed:

- Contents, Pull requests, Workflows — push workflow files / open seed PRs
- Administration — ruleset + repo settings
- Environments — create `uat` / `prod`
- Variables — set repository variables

## Dispatch

Actions → **seed-promotion-pipelines** → Run workflow:

| Input | Example | Notes |
|---|---|---|
| `target_repo` | `slava-samokhina/target-repo` | `owner/repo` |
| `tiers` | `both` | `both` \| `uat` \| `prod` |
| `codeowners_owner` | `slava-samokhina` | with or without `@`; teams as `org/team` |
| `push_mode` | `direct` | first seed: `direct`; re-seed behind ruleset: `pr` |
| `apply_ruleset` | `true` | set `false` to only land files/vars |
| `add_prod_check` | `false` | phase 2 only — see ordering below |
| `resolved_vars` | `{}` | JSON of project-specific vars once GCP exists |

Example `resolved_vars` after UAT/Prod projects exist:

```json
{
  "DEV_PROJECT_ID": "my-app-dev",
  "UAT_PROJECT_ID": "my-app-uat",
  "PROD_PROJECT_ID": "my-app-prod",
  "PROMOTER_SA": "uat-promoter@my-app-uat.iam.gserviceaccount.com",
  "UAT_RUNTIME_SA": "runtime-sa-uat@my-app-uat.iam.gserviceaccount.com",
  "PROD_PROMOTER_SA": "prod-promoter@my-app-prod.iam.gserviceaccount.com",
  "PROD_RUNTIME_SA": "runtime-sa-prod@my-app-prod.iam.gserviceaccount.com",
  "WIF_PROVIDER": "projects/123/locations/global/workloadIdentityPools/github-uat-pool/providers/github-uat-provider"
}
```

Constant variables (`GCP_REGION`, AR repo names, attestor/KMS, `RUNNER_LABELS`,
…) are always set from `nexus_configs/promotion_seed.yaml`.

## Ordering gotchas

1. **Files before ruleset.** The ruleset requires PR + code-owner review +
   status checks. First seed uses `push_mode=direct`, then applies the
   ruleset last. Re-seeds on a protected repo use `push_mode=pr`.
2. **`prod-checks` phase 2.** A required status check must have been reported
   at least once before the ruleset will accept it. Phase 1 requires only
   `checks` (UAT). After a prod PR has run `prod-pr-checks` once, re-dispatch
   with `add_prod_check=true`.
3. **GCP is out of scope.** This seeder writes GitHub config only. SAs, WIF,
   KMS, attestors, and Artifact Registry come from project creation; pass
   their IDs via `resolved_vars`.

## What gets written on the target

- `.github/workflows/` — selected tier workflows (6 for `both`)
- `.github/CODEOWNERS` — `/release/uat/` and/or `/release/prod/`
- `release/uat/.gitkeep`, `release/prod/.gitkeep`
- Environments `uat` and/or `prod` (no required reviewers)
- Repository variables (constants + any provided `resolved_vars`)
- `allow_auto_merge=true`, Actions can approve PRs, default workflow perms `read`
- Ruleset `main-guard` on `~DEFAULT_BRANCH`

All writes are upserts — safe to re-run.

## Manual test plan

1. **Blank target** (`slava-samokhina/target-repo`): dispatch
   `tiers=both`, `codeowners_owner=slava-samokhina`, `push_mode=direct`,
   `apply_ruleset=true`, `add_prod_check=false`, `resolved_vars={}`.
   Expect 6 workflows, CODEOWNERS, envs, constant vars, ruleset with only
   `checks` required.
2. **Idempotency:** re-dispatch same inputs → no errors, no duplicates.
3. **Resolved vars:** dispatch with a dummy JSON → those variables appear.
4. **Phase 2 (optional):** after `prod-checks` has run once,
   `add_prod_check=true` → ruleset also requires `prod-checks`.

## Later integration

Expose a `repository_dispatch` or `workflow_call` entry so the
UAT/Prod project-creation flow can invoke this seeder automatically
instead of a manual `workflow_dispatch`.
