# InnovisionBD Cloud Archive Worker — No CLI deployment

Upload the three source files to the **root of a private GitHub repository**, retaining `cloud-run/` as a folder. Do not upload credentials.

## Repository layout
- `Dockerfile`
- `cloud-run/worker.py`
- `cloud-run/requirements.txt`

## Google Cloud Run console
1. Enable Billing, Cloud Run API, Cloud Build API, Artifact Registry API, Cloud Scheduler API.
2. Cloud Run > Services > Create service > continuously deploy from repository; connect private GitHub repository; build via Dockerfile `/Dockerfile`.
3. Service name `innovisionbd-archive-worker`, region e.g. asia-south1, memory 1GiB, request timeout 300 seconds, concurrency 1, min instances 0.
4. Variables/Secrets:
  `IVBD_GATEWAY_URL=https://innovisionbd.com/business-data/cloud_gateway.php`
  `IVBD_GATEWAY_SECRET=<the 64-character gateway secret generated in website Cloud archive page>`
  `IVBD_RUN_SECRET=<different random secret >=32 characters>`
  `IVBD_WORKERS=4`
  `IVBD_RUN_SECONDS=45`
  `IVBD_FILE_SLICE_SECONDS=165`
Use Google Secret Manager for both secrets. Never commit secrets to GitHub.
5. Prefer Require authentication. Create a Scheduler service account with Cloud Run Invoker permission. Create an HTTP Cloud Scheduler job at `* * * * *`, method POST, URL `https://YOUR-SERVICE-URL/run`, headers `X-Run-Secret: <IVBD_RUN_SECRET>`, `Content-Type: application/json`, body `{}`, OIDC with audience the base service URL.
6. Cloud Run `/health` should report `gateway_configured: true` and `run_secret_configured: true`; note private services need authenticated invocation to test. Website Cloud archive test uses `/diagnose` and may receive 403 if Cloud Run IAM authentication is required, independently of application-level secrets.
7. Force-run Scheduler and inspect Cloud Run logs; test with a small source account and confirm output in Workspace Drive.
8. Stop old archive scheduler/worker jobs so they do not process the same archive concurrently.

## Notes
This is the V13.1 diagnostic-enabled worker from the prior application package, repackaged for Google Cloud Run deployment. It requires the matching Hostinger application and its `cloud_gateway.php`; do not deploy worker alone and expect archiving to work. No Cloud deployment or live Google transfer is asserted. Google OAuth source/destination accounts must already be connected.
