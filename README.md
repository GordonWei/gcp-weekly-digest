# GCP Weekly Digest

> **TL;DR** — A Cloud Run Job that runs every Friday and does two things: it
> reads the week's GCP Release Notes and Cloud Blog feeds and writes you a
> categorized digest, and it reads **your own project's Cloud Recommender
> findings** and tells you what is worth fixing in it. What shipped, and what
> it means for what you already run, in one email. Gemini for the writing,
> Gmail API for delivery (keyless — no key files, no API keys), GCS for the
> archive. Deployed straight from source with `gcloud run jobs deploy`.

Sibling project to [AWS Weekly Digest](https://github.com/GordonWei/aws-weekly-digest) —
same two-half idea, same environment-variable style, different cloud. Written
so the two can be read side by side as an "AWS vs GCP serverless digest bot"
comparison.

I built this because I kept meaning to read the GCP Release Notes feed and
never did. But keeping up with what shipped is only half of why I wanted to
read it. The other half is what any of it means for the things I am already
running — and that half never arrives on its own, because the feed does not
know what I have. So the digest also looks at my own project's Cloud
Recommender findings and tells me what is worth fixing there. It is still a
small personal tool, not a product.

## How it works

```
  GCP Release Notes Atom Feed ─┐
                               ├─→ ┌───────────────────────┐ ─→ Gemini ─→ digest ─┐
  GCP Cloud Blog RSS          ─┘   │  Cloud Run Job         │                     ├─→ Gmail API → your inbox
  Cloud Recommender ────────────→  │  (Python 3.12)         │                     └─→ GCS  → digests/<date>/
  (your project's findings)        └───────────────────────┘ ─→ Gemini ─→ advice ─┘
                                             ▲
                          Cloud Scheduler (Fri 17:00 Asia/Taipei)
```

Two halves, one email. The **digest** half is the same for everybody: what
GCP shipped this week, categorized. The **advice** half is the one only your
project can produce — it reads Cloud Recommender's findings for idle IPs,
idle disks, idle VMs, oversized machine types, unused committed-use discounts,
and near-empty projects, and asks Gemini to explain what is actually worth
doing about them.

Neither half is the point on its own. Knowing what GCP released does not tell
you whether it matters to you, and a feed cannot tell you that because it
does not know what you have.

The advice half ships switched off — it needs `roles/recommender.viewer` the
digest half does not, so turning it on should be your decision. Set
`FEATURE_ACCOUNT_ADVICE=true` to turn it on.

Detection is left entirely to Cloud Recommender rather than reimplemented —
Google already ships this better than a hand-rolled equivalent would be, so
the model's job here is explaining the findings, not spotting them.

Two optional output channels — LinkedIn and a generic webhook — are wired up
but off by default. See the feature flags below.

## Prerequisites

- A GCP project with billing enabled.
- `gcloud` CLI authenticated against that project.
- A Google account (personal Gmail or Workspace) to receive the digest.
  Delivery goes through the Gmail API via domain-wide delegation — see
  [Email delivery](#email-delivery-keyless-gmail-api) below. There is one
  manual step in the Google Admin/account settings that `gcloud` cannot do
  for you.

## Deploy

```bash
cd v2-cloud-run

gcloud services enable \
  run.googleapis.com cloudscheduler.googleapis.com storage.googleapis.com \
  aiplatform.googleapis.com iamcredentials.googleapis.com gmail.googleapis.com \
  --project=<PROJECT_ID>

# Service accounts: one to run the Job, one to trigger it from Scheduler,
# one purely as the domain-wide-delegation identity for sending mail.
gcloud iam service-accounts create gcp-weekly-digest-sa --project=<PROJECT_ID>
gcloud iam service-accounts create gcp-weekly-digest-scheduler-sa --project=<PROJECT_ID>
gcloud iam service-accounts create gcp-weekly-digest-mailer-sa --project=<PROJECT_ID>

gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="serviceAccount:gcp-weekly-digest-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"

# Only if you turn on FEATURE_ACCOUNT_ADVICE
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="serviceAccount:gcp-weekly-digest-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/recommender.viewer"

# The Job SA needs to sign JWTs as the mailer SA — this is what makes the
# whole delivery path keyless. Scoped to the mailer SA resource, not the project.
gcloud iam service-accounts add-iam-policy-binding \
  gcp-weekly-digest-mailer-sa@<PROJECT_ID>.iam.gserviceaccount.com \
  --member="serviceAccount:gcp-weekly-digest-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator"

gcloud storage buckets create gs://<BUCKET_NAME> --project=<PROJECT_ID> --location=<REGION> \
  --uniform-bucket-level-access
gcloud storage buckets add-iam-policy-binding gs://<BUCKET_NAME> \
  --member="serviceAccount:gcp-weekly-digest-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

gcloud run jobs deploy gcp-weekly-digest --source=. --region=<REGION> \
  --service-account=gcp-weekly-digest-sa@<PROJECT_ID>.iam.gserviceaccount.com \
  --task-timeout=900 --max-retries=1 --memory=512Mi --cpu=1 \
  --set-env-vars="GCP_PROJECT_ID=<PROJECT_ID>,RECIPIENT_EMAIL=<YOUR_EMAIL>,SENDER_EMAIL=<YOUR_EMAIL>,GMAIL_IMPERSONATE_USER=<YOUR_EMAIL>,GCS_BUCKET=<BUCKET_NAME>,DIGEST_LANGUAGE=en"

gcloud run jobs add-iam-policy-binding gcp-weekly-digest --region=<REGION> \
  --member="serviceAccount:gcp-weekly-digest-scheduler-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

gcloud scheduler jobs create http gcp-weekly-digest-friday \
  --location=<REGION> --schedule="0 17 * * 5" --time-zone="Asia/Taipei" \
  --uri="https://run.googleapis.com/v2/projects/<PROJECT_ID>/locations/<REGION>/jobs/gcp-weekly-digest:run" \
  --http-method=POST \
  --oauth-service-account-email=gcp-weekly-digest-scheduler-sa@<PROJECT_ID>.iam.gserviceaccount.com
```

Then finish the one step `gcloud` cannot do — see
[Email delivery](#email-delivery-keyless-gmail-api) below — and run:

```bash
gcloud run jobs execute gcp-weekly-digest --region=<REGION> --wait
```

## Configuration

All settings are environment variables on the Cloud Run Job.

| Variable | Default | Notes |
|---|---|---|
| `GCP_PROJECT_ID` | — | Required |
| `RECIPIENT_EMAIL` | — | Required |
| `SENDER_EMAIL` | — | Required. Usually the same address as `GMAIL_IMPERSONATE_USER` |
| `GCS_BUCKET` | `''` | Empty disables archiving even if the flag is on |
| `DAYS_LOOKBACK` | `7` | How far back to pull feed items |
| `MAX_RELEASE_NOTES` | `80` | Cap on Release Notes items sent to the model |
| `MAX_BLOG_POSTS` | `15` | Cap on Blog items sent to the model |
| `VERTEX_LOCATION` | `global` | Gemini endpoint location |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | Check the [release notes](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/release-notes) before pinning a different one — Preview model IDs move |
| `DIGEST_LANGUAGE` | `en` | `en` or `zh-TW`. See below |
| `EMAIL_PROVIDER` | `gmail` | `gmail` (keyless, recommended) or `sendgrid` (kept for reference, see below) |
| `GMAIL_MAILER_SA_EMAIL` | `gcp-weekly-digest-mailer-sa@<GCP_PROJECT_ID>.iam.gserviceaccount.com` | The domain-wide-delegation identity |
| `GMAIL_IMPERSONATE_USER` | — | Required for `EMAIL_PROVIDER=gmail`. The account the digest is sent *as* |
| `SENDGRID_API_KEY` | `''` | Only used when `EMAIL_PROVIDER=sendgrid` |

Feature flags: `FEATURE_SEND_EMAIL`, `FEATURE_EMBED_CONTENT`,
`FEATURE_SAVE_TO_GCS`, `FEATURE_POST_TO_LINKEDIN`, `FEATURE_POST_TO_WEBHOOK`,
`FEATURE_ACCOUNT_ADVICE`.

### Digest language

`DIGEST_LANGUAGE` picks the language the digest is written in. It defaults to
`en`; the other value shipped today is `zh-TW` (Traditional Chinese).

One setting covers the digest body, the advice section, and every static
string in the email wrapper (subject, header stats, footer, the plain-text
fallback) — so you never end up with an English digest inside a
Chinese-labelled email or vice versa. Each language has its own prompt
(`_prompt_en`, `_prompt_zh_tw` in `main.py`) rather than one prompt with
"reply in X" appended, because the section headings are part of the output
contract that the Markdown-to-HTML converter reads back — they have to be
written in the target language to come back reliably.

### Account advice

Set `FEATURE_ACCOUNT_ADVICE=true` to turn this half on. It reads Cloud
Recommender findings across six recommenders (idle static IPs, idle disks,
idle VMs, machine-type rightsizing, unused committed-use discounts, and
near-empty projects) and asks Gemini to explain what is worth doing about
them — detection stays deterministic (Cloud Recommender's own analysis),
the model's job is only to prioritize and explain.

Run `python3 v2-cloud-run/verify_advice.py` against a real project before
deploying with this flag on — it checks the API is enabled, IAM is sufficient,
and shows you what the rendered section actually looks like without sending
any mail.

### Email delivery: keyless Gmail API

The default (`EMAIL_PROVIDER=gmail`) sends through the Gmail API using
[domain-wide delegation](https://developers.google.com/identity/protocols/oauth2/service-account),
with the Cloud Run Job's own identity signing short-lived tokens rather than
a downloaded service account key sitting anywhere:

1. The Job's own service account (Cloud Run gives it Application Default
   Credentials for free) calls the [IAM Credentials API's `signJwt`](https://docs.cloud.google.com/iam/docs/create-short-lived-credentials-direct)
   on the mailer service account, signing a JWT with `sub=GMAIL_IMPERSONATE_USER`.
   This is the step that needs `roles/iam.serviceAccountTokenCreator`, granted
   at the mailer SA's own resource level, not the project.
2. That signed JWT is exchanged at Google's OAuth token endpoint for an access
   token that represents the impersonated user (RFC 7523 JWT-bearer flow) —
   this is the step where domain-wide delegation actually takes effect.
3. That token calls `gmail.googleapis.com/.../messages/send` directly.

No key file is ever written to disk, downloaded, or mounted into the
container. See [Traps worth knowing about](#traps-worth-knowing-about) for
why this project ended up here instead of the more obvious "download a
service account key" path.

**The one manual step**: get the mailer SA's numeric client ID —

```bash
gcloud iam service-accounts describe \
  gcp-weekly-digest-mailer-sa@<PROJECT_ID>.iam.gserviceaccount.com \
  --format="value(uniqueId)"
```

— then in your Google Admin console: **Security → API controls →
Domain-wide delegation → Add new**, paste that client ID, and grant scope
`https://www.googleapis.com/auth/gmail.send`. This requires super-admin
access on the Workspace/account and is not something `gcloud` can do for you.
Until it is done, the token exchange in step 2 fails with `unauthorized_client`
— the error message says so.

`EMAIL_PROVIDER=sendgrid` is kept in the code for reference (a previous
version of this project used it) but is not the recommended path — it needs
its own account, its own API key in Secret Manager, and gives you one more
external dependency with its own uptime and deliverability history to manage.

## Traps worth knowing about

### 1. An organization policy can block the "obvious" path entirely

The first design for keyless delivery still wasn't keyless — it downloaded a
service account key, put it in Secret Manager, and mounted it into the
container. That plan died at `gcloud iam service-accounts keys create`:

```
FAILED_PRECONDITION: Key creation is not allowed on this service account
```

This is [`constraints/iam.disableServiceAccountKeyCreation`](https://docs.cloud.google.com/resource-manager/docs/organization-policy/restricting-service-accounts),
an organization policy that blocks SA key creation outright — not an IAM
permission problem, a policy problem, and no amount of re-checking roles
fixes it. The fix wasn't requesting an exception to the policy. It was the
signJwt + impersonation flow described above, which needs no key file to
exist in the first place and is arguably the better design anyway.

### 2. Getting every identity step right doesn't save you from the boring 403

With domain-wide delegation authorized and the signJwt flow working end to
end, the very next run still failed:

```
"code": 403, "status": "PERMISSION_DENIED", "reason": "SERVICE_DISABLED",
"message": "Gmail API has not been used in project ... or it is disabled."
```

Every piece of identity plumbing — IAM bindings, the JWT-bearer exchange, the
domain-wide delegation authorization — was correct. The Gmail API itself
had simply never been enabled on the project. `gcloud services enable
gmail.googleapis.com` and the next run sent real mail. Worth writing down
because it is easy to assume a 403 after getting the hard identity part right
must be another identity problem, when it can just as easily be the
unglamorous one you forgot two steps back.

### 3. Copying a working plan forward without re-checking it is a plan to fail later

The V1 (Google Apps Script) version of this project worked. Rebuilding it
on Cloud Run — mostly to make it reproducible for readers without a Google
Workspace account — surfaced three things that had quietly changed since V1
was written and would have broken a "runs every Friday, unattended" job
within two months if carried forward unchecked: the AI SDK the plan assumed
(`google-cloud-aiplatform`'s generative modules) had already been fully
retired, the model V1 used (`gemini-2.5-flash-lite`) was retiring within
seven weeks, and the Blog RSS URL V1 pointed at had quietly started serving
an HTML page instead of XML — a `200`, not a `404`, so nothing in V1's error
handling would ever have flagged it. None of these were code bugs. They were
the ground having moved under a plan that was only weeks old.

## Limitations

- Two hardcoded feeds (Release Notes, Cloud Blog). No config for adding more.
- Weekly schedule only.
- The account advice section sees Cloud Recommender's findings only — it
  cannot see a single configuration value beyond what Recommender already
  surfaces.
- On a project with very little billed usage, the advice is technically
  correct and financially close to pointless. It gets more useful the more
  you actually run.
- The LinkedIn and webhook channels are written but off by default and have
  seen much less use than email and GCS.

## License

MIT — see [LICENSE](LICENSE).
