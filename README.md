# CMT Deployer

Internal FastAPI app for GitHub branch/config operations and AWS Elastic Beanstalk deployments with a live SSE console.

## Layout

```
app/
  main.py                    FastAPI app, middleware, error handlers
  config.py                  Env-driven settings and AWS account map
  dependencies.py            Session/CSRF guards, service singletons, templates
  routers/
    auth.py                  /login, /logout
    ui.py                    / (dashboard), /healthz
    github.py                /api/github/* (token, repos, branches, commit-config)
    aws.py                   /api/aws/* (accounts, environments, deploy, SSE events)
  services/
    github_service.py        GitHub REST via httpx (+ dry-run simulator)
    aws_service.py           STS assume-role, EB, S3 (+ dry-run simulator)
    deployment_manager.py    Background deploy pipeline and event polling
    bundle.py                Zip validation and GitHub zipball repackaging
    session_store.py         Server-side sessions (PAT never sent to the browser)
  templates/                 base.html, login.html, index.html
  static/app.js              Dropdowns, GitHub actions, deploy, EventSource console
requirements.txt
```

## Setup (Windows PowerShell)

```powershell
py -3.11 -m venv .venv          # any Python 3.11+
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

macOS/Linux: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`.

## Run in dry-run mode (no credentials)

```powershell
$env:DRY_RUN = "true"
$env:APP_USERNAME = "admin"; $env:APP_PASSWORD = "change-me"
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000. In dry-run mode:

- Each account has three simulated environments (`<acct>-web`, `<acct>-api`, `<acct>-worker`).
- If you skip the PAT, GitHub is simulated with in-memory branches. If you enter a PAT, the real GitHub API is used.
- Deployments run through the full pipeline (bundle, upload, version, update) and stream simulated EB events for about 15 seconds.

`DRY_RUN=auto` (the default) switches to dry-run only when boto3 finds no AWS credentials.

## Run against live AWS and GitHub

```powershell
$env:DRY_RUN = "false"
$env:AWS_PROFILE = "deployer"                 # or AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
$env:AWS_DEFAULT_REGION = "us-east-1"

# Optional cross-account roles (omit to use the base credentials directly)
$env:AWS_DEV_ROLE_ARN     = "arn:aws:iam::111111111111:role/CmtDeployer"
$env:AWS_STAGING_ROLE_ARN = "arn:aws:iam::222222222222:role/CmtDeployer"
$env:AWS_PROD_ROLE_ARN    = "arn:aws:iam::333333333333:role/CmtDeployer"
$env:AWS_PROD_EXTERNAL_ID = "optional-external-id"
$env:AWS_PROD_REGION      = "eu-west-1"       # per-account region override

uvicorn app.main:app --host 127.0.0.1 --port 8000
```

You can also define accounts as JSON:

```powershell
$env:AWS_ACCOUNTS_JSON = '[{"key":"dev","name":"Development","region":"us-east-1","role_arn":"arn:aws:iam::111111111111:role/CmtDeployer"}]'
```

Add the GitHub PAT on the login page or on the dashboard's GitHub tab. It needs one of:

- A classic token with `repo` scope.
- A fine-grained token with **Contents: Read and write** on the target repositories.

Verify the AWS identity per account at `GET /api/aws/accounts/{key}/identity`.

### Minimum IAM permissions for the deployer role

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": [
      "elasticbeanstalk:DescribeEnvironments", "elasticbeanstalk:DescribeEvents",
      "elasticbeanstalk:DescribeApplicationVersions", "elasticbeanstalk:CreateApplicationVersion",
      "elasticbeanstalk:UpdateEnvironment", "elasticbeanstalk:CreateStorageLocation"
    ], "Resource": "*"},
    {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject", "s3:CreateBucket", "s3:ListBucket",
      "s3:GetBucketLocation", "s3:GetBucketPolicy", "s3:PutBucketPolicy", "s3:PutBucketOwnershipControls"],
     "Resource": ["arn:aws:s3:::elasticbeanstalk-*", "arn:aws:s3:::elasticbeanstalk-*/*"]}
  ]
}
```

`UpdateEnvironment` also needs the permissions EB uses on your behalf (CloudFormation, EC2, Auto Scaling, ELB). For a prototype, the simplest option is the AWS managed policy `AdministratorAccess-AWSElasticBeanstalk`. The source account's principal also needs `sts:AssumeRole` on each target role.

## Deployment flow

1. `POST /api/aws/deploy` (multipart) validates the request and returns `202` with a `deployment_id`.
2. A background task runs these steps:
   - Prepares the bundle. GitHub zipballs are re-rooted so files sit at the zip root.
   - Uploads it to the EB storage bucket (`create_storage_location`).
   - Calls `create_application_version(Process=True)` and waits for `PROCESSED`.
   - Calls `update_environment`.
   - Polls `describe_events` and `describe_environments` until the environment is `Ready` with the new version.
3. `GET /api/aws/deployments/{id}/events` streams the deployment log over SSE. It supports `Last-Event-ID`, so the browser can reconnect without losing log lines.

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `APP_USERNAME` / `APP_PASSWORD` | `admin` / `admin` | Mock login credentials |
| `DRY_RUN` | `auto` | `true`, `false`, or `auto` |
| `SESSION_TTL_SECONDS` | `28800` | Sliding session lifetime |
| `COOKIE_SECURE` | `false` | Set `true` behind HTTPS |
| `MAX_UPLOAD_MB` | `200` | Upload / GitHub archive size limit |
| `MAX_UNCOMPRESSED_MB` | `1024` | Zip-bomb guard |
| `DEPLOY_TIMEOUT_SECONDS` | `1800` | Max wait for an environment update |
| `POLL_INTERVAL_SECONDS` | `5` | EB polling interval (dry-run uses 1s) |
| `GITHUB_API_URL` | `https://api.github.com` | Set for GitHub Enterprise Server |

## Prototype limitations

- Sessions and deployment history are kept in process memory. Run a single Uvicorn worker. Use Redis or DynamoDB before scaling out.
- Authentication is a single mock user. Replace it with SSO (OIDC/SAML) for real use.
- Tailwind is loaded from its CDN, which is not intended for production builds.
