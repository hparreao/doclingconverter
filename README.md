# Docling Converter

An upload-only converter built on Docling. Its production runtime is an
asynchronous AWS Lambda pipeline; its Docker image remains a supported local
and self-hosted option using the official `docling-serve` REST API.

## 2026 architecture update

The original Streamlit application is deprecated. It remains available only in
Git history for historical reference and must not be restored or deployed: it
does not implement the current upload boundary, result-retention policy, or
runtime controls.

The first 2026 update replaced Streamlit with native HTML, CSS, and JavaScript
served by the same ASGI process as `docling-serve`. The current update moves
the public runtime from a Hugging Face Docker Space to AWS Lambda because free
Hugging Face accounts cannot create Docker Spaces. Docker is retained for
local development and for operators who want the full Docling Serve REST
contract.

In AWS, FastAPI owns the Lambda control plane and Mangum adapts its ASGI app to
the public Function URL. The browser receives a short-lived S3 upload policy
and an opaque per-job capability, uploads directly to a private bucket,
submits the job, then polls for a short-lived result URL. The capability
is returned only in the create response and its SHA-256 digest, never the
value, is stored with the job. Submission and polling require it in the
`x-job-token` header. It limits access to a job and its result URL; it does not
turn the public Function URL into an authenticated user service. A conditional
DynamoDB lock serializes conversions,
so one x86_64 worker job is accepted at a time even in AWS accounts whose
regional concurrency quota cannot support reserved concurrency.
The worker uses 3008 MB, which is the compatible ceiling for restricted
accounts; supported file size stays at 25 MB to keep this execution envelope
practical.
To stay within Lambda's initialization window, the worker imports Docling only
after a job has passed validation; the cold-start cost is paid by the accepted
conversion, rather than by every API request.
Source files are deleted immediately after processing; S3 lifecycle rules and
DynamoDB TTL remove any remaining result objects and job state after one day.

The public Lambda contract deliberately supports one document per job and
Markdown and JSON outputs. That constrains cost and execution duration. The
Docker deployment retains the broader Docling Serve format and API contract.

## AWS deployment

The repository contains two CloudFormation templates:

- `infrastructure/foundation.yaml` creates the private artifacts bucket,
  DynamoDB job table, ECR repository, Lambda execution roles, and a GitHub
  Actions OIDC role scoped to this repository's `main` branch.
- `infrastructure/service.yaml` creates the API Function URL and the x86_64
  conversion worker after an immutable image exists in ECR.

Bootstrap the foundation once from an authenticated AWS CLI. Values stay in
your terminal or GitHub Actions configuration; do not put account IDs, role
ARNs, access keys, bucket names, or Function URLs into source files.

```bash
aws iam list-open-id-connect-providers --query 'OpenIDConnectProviderList[].Arn' --output table
aws cloudformation deploy \
  --stack-name docling-converter-foundation \
  --template-file infrastructure/foundation.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    ProjectName=docling-converter \
    SiteOrigin=https://your-public-site.example \
    GitHubRepository=hparreao/doclingconverter \
    GitHubOidcProviderArn=arn:aws:iam::ACCOUNT:oidc-provider/token.actions.githubusercontent.com
```

Then configure these GitHub Actions variables in the repository settings:

- `AWS_REGION`
- `AWS_ROLE_ARN` (the `GitHubActionsRoleArn` output)
- `SITE_ORIGIN` (the same exact HTTPS origin passed to the foundation stack)

The deployment job runs through the `production` GitHub Environment. Its branch
policy is restricted to `main`, and the AWS role trust policy accepts that
Environment claim only. This keeps the OIDC role out of pull requests and
other branches without storing long-lived AWS credentials in GitHub.
The public Function URL uses the two resource-policy statements required by
AWS for URLs created after October 2025; direct Lambda invocation remains
disallowed by the URL-only condition.

Pushing `main` builds `Dockerfile.lambda` for `linux/amd64`, pushes an
immutable `sha-<commit>` image to ECR, scans that exact published image with
Trivy, and deploys the service stack only when no fixable High or Critical OS
or library vulnerability is detected. ECR scan-on-push remains a second
signal; neither scanner replaces dependency upgrades or an application review.
Read the `ApiFunctionUrl` stack output and configure the public website with
that URL.

The service has cost guardrails: one conversion worker, ten jobs per IP per
day, a global 100-job monthly admission limit, one 25 MB file per job, and a
10-minute Lambda timeout. Each S3 upload policy expires after two minutes.
Every Function URL request is also limited to 60 per IP per minute, 3,000 per
day, and 20,000 per month. Creating an upload policy does not debit conversion
quota. After S3 confirms the upload, the submit action atomically reserves the
worker lock, transitions the job, and debits both job counters in DynamoDB; a
worker-dispatch retry retains that admission and is not charged twice.
Asynchronous worker retries are disabled. At 3008 MB, 100 full 10-minute worker
executions consume about 176,000 GB-seconds, below
Lambda's 400,000 GB-second monthly free allocation. S3, ECR, CloudWatch,
transfer, account eligibility, and provider pricing remain independent billing
variables; this is a bounded
compute envelope, not a zero-cost guarantee. Enable Free Tier usage alerts and
a zero-spend budget before public traffic.

The worker logs only a random job identifier, processing stage, and exception
type on failure. It deliberately excludes filenames, object keys, document
content, presigned URLs, and exception text. The operational records therefore
remain useful for diagnosing a failed stage without becoming a document-data
log. The opaque browser capability must stay in the page session; losing it
makes a pending job inaccessible and a browser extension or local compromise
can still read it. This is capability-based isolation, not user identity.

## Docker runtime contract

- `docling-serve` is pinned to `1.32.0` through the official CPU image digest
  `sha256:56f12f30f672272a557beb191be6855ba76478e91bac94e8cdedd52d2c8d9da2`.
- That release includes the validated `docling-slim` `2.124.0` matrix. The
  image adds only the compatible `format-iwork` extra and LibreOffice, required
  for Pages and legacy Office formats respectively.
- The server listens on port `7860`, disables the upstream demonstrator UI,
  and uses `POST /v1/convert/file/async`, `GET /v1/status/poll/{task_id}`,
  `GET /v1/result/{task_id}`, and `GET /health`.
- The browser reads `/openapi.json` at startup and renders only output formats
  and conversion controls advertised by that deployed contract.

The self-hosted Docker input set follows the fixed Docling matrix: PDF, current and
legacy Office, OpenDocument, EPUB, Pages, HTML, Markdown, AsciiDoc, LaTeX, CSV,
images, EML/MSG, WebVTT, BoxNote, Docling JSON, and supported XML variants.
Audio, video, VLM, and EBCDIC are deliberately excluded from this deployment.

The Lambda worker has a different, narrower image contract. It starts from a
pinned Python 3.12 slim base and installs only `docling-slim`, LibreOffice, the
Lambda Runtime Interface Client, and the AWS SDK. It does not include the
`docling-serve` process, Ray, or its bundled Java libraries because the worker
calls `DocumentConverter` directly. The release gate scans this image. The
self-hosted server remains a distinct operator-managed runtime and should be
scanned and patched by that operator before exposure to untrusted traffic.

## Docker privacy and limits

Remote-source endpoints are blocked before they reach `docling-serve`; this
deployment accepts local uploads only. The outer ASGI boundary accepts at most
five files, 25 MB each, validates a safe filename plus an extension and useful
magic bytes, and never logs file names or contents. Docling enforces 100 pages
per document and a 10-minute document timeout. One CPU worker processes one job
at a time and polling exposes `queued` or `started` states.

Responses, including results, receive `Cache-Control: no-store`. Results are
single-use and configured for removal within 30 minutes. The local Docker UI
does not add analytics integration.

## Local Docker use

Docker is the supported runtime because the official CPU image supplies the
Docling matrix. With Docker available:

```bash
make build
make run
curl --fail http://localhost:7860/health
```

Run the executable Lambda/API suite from an isolated Python environment. It
uses mocked AWS clients and a real Function URL v2 event through Mangum; it
does not access an AWS account or process a document.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
make test PYTHON=.venv/bin/python
make coverage PYTHON=.venv/bin/python
```

The CI gate enforces 50% line coverage for `lambda_app`. This is a regression
floor rather than evidence of complete production behavior; the deploy
workflow also builds and scans the Linux Lambda image. Exercise conversion
fixtures only against a built container and do not use personal or confidential
documents as fixtures.

Build the Lambda image locally only when Docker is installed:

```bash
make build-lambda
```
