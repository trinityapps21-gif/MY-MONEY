# MY MONEY
Multi-tenant savings-group and lending platform. Developed and Powered by Emtrixz Technology Company.

Flask + SQLAlchemy, Turso (libSQL) in production, Uploadcare (images), ImageKit (documents).

## Run locally
```
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
flask db upgrade && flask seed
flask create-platform-admin      # no default credentials exist
python run.py                    # or: flask run
pytest                           # tests use SQLite memory + a memory storage backend
```
Without Turso variables the app uses a local SQLite file in `instance/` (dev only).

## Deploy on Render + Turso
1. Create a Turso database: `turso db create my-money`, then `turso db show my-money --url` and `turso db tokens create my-money`.
2. Push this repo, create a Render Blueprint from `render.yaml`, and fill the secret variables below.
3. In the Render shell, once: `flask db upgrade && flask seed && flask create-platform-admin`.
4. Health check: `/health`.

Production start refuses to boot without `SECRET_KEY`, `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`.

## Environment variables
See `.env.example`. Key ones: `SECRET_KEY`, `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `UPLOADCARE_PUBLIC_KEY`, `UPLOADCARE_SECRET_KEY`, `IMAGEKIT_PRIVATE_KEY`, `IMAGEKIT_PUBLIC_KEY`, `IMAGEKIT_URL_ENDPOINT`, `SIGNED_URL_SECONDS`, `PLATFORM_ADMIN_EMAIL`, `SMTP_*`.

## Media storage architecture
- `app/services/storage.py` is the facade. `image/*` goes to Uploadcare, everything else to ImageKit.
- Each provider lives in `app/services/storage_providers/` behind the `StorageProvider` interface (`upload`, `read`, `delete`, `signed_url`). Replace one provider without touching the rest of the app.
- Turso table `stored_files` holds metadata only (provider, provider id, filename, MIME, size, checksum, group, uploader, related record, soft-delete fields). Uploads and deletions are audit logged.
- All uploads go through authenticated Flask endpoints; keys never reach the browser. Images are re-encoded (metadata stripped); documents are checked by extension, MIME and magic bytes. Limits: 2 MB logos, 5 MB attachments.
- Files are served only via `/files/<id>` after a group-membership check. Images are proxied; documents redirect to short-lived signed ImageKit URLs (private files). Only group logos are publicly viewable.
- Account setup: Uploadcare project -> copy public and secret keys. ImageKit -> copy private key, public key and URL endpoint. Set them in Render.

## Importing existing records
Group officials open **Import Data**, download an Excel/CSV template (or use their own sheet, common column names such as `Name`, `Mobile`, `Amount Paid`, `Date Paid` are understood), upload it, review a **preview** (nothing is saved), then confirm. Order: members -> contributions (use category `OPENING` for a savings balance as of today) -> loans (original terms) -> repayments.
- Rows are posted through the same services as manual entry, so the ledger, duplicate protection and the interest engine behave identically. Imports send **no notifications** and add **no feed posts**; historical loans are allowed even if imported savings do not cover them.
- Every batch can be **undone** (ledger reversal rows; nothing is deleted). Undo loans only after undoing their repayments. Members who already have records are kept.
- Dates are read day-first (31/12/2025). Limits: 5 MB, 2000 rows per file.
- **PDF:** PDFs with real, selectable tables are read (pdfplumber). **Scanned PDFs (photos of paper, e.g. CamScanner) cannot be read automatically**: they are kept privately as a reference document (ImageKit, officials only) and the figures must be typed into a template. No OCR is included because it is not reliable enough for money.
- Imported members have a random password; they sign in by tapping **Forgot password** and an official approves it (Password Resets).

## Security upgrades (Turnstile, inactivity sign-out, biometrics)
**Cloudflare Turnstile** protects login, biometric login, group sign-up, forgot-password, recovery-code reset and the reset-link page.
1. Cloudflare dashboard -> Turnstile -> Add widget (add your domain, e.g. `joinmymoney.com` and your `*.onrender.com` host).
2. In Render set `TURNSTILE_SITE_KEY` and `TURNSTILE_SECRET_KEY`. Without both the check is off (a warning is logged in production).
3. Every protected POST is verified server-side with Cloudflare; a missing, reused or rejected token blocks the request, and if Cloudflare cannot be reached the request is refused (fails closed).
4. Local testing: site key `1x00000000000000000000AA` / secret `1x0000000000000000000000000000000AA` always passes; `2x00000000000000000000AB` / `2x0000000000000000000000000000000AA` always fails.

**30-minute inactivity sign-out** (`IDLE_TIMEOUT_MINUTES`, default 30) applies to the website and the installed phone app.
- Enforced on the server: any request after 30 idle minutes is refused and the session ends. The "Keep me signed in" box was removed (a 7-day cookie would contradict the rule).
- `static/js/idle.js` warns 60 seconds before, signs out on time, keeps the session alive only while the person is really typing or tapping, and re-checks when a phone app returns from the background.

**Biometric sign-in** (fingerprint / face / device unlock) uses WebAuthn passkeys, so no biometric data ever reaches the server, only a public key (`webauthn_credentials` table).
- Enable it in **Profile -> Fingerprint / face sign-in** (needs the current password). Password login keeps working.
- By default it is offered only inside the installed home-screen app (`BIOMETRIC_INSTALLED_APP_ONLY=0` also allows browsers). Requires HTTPS.
- Credentials belong to one website address. If you use a custom domain, set `WEBAUTHN_RP_ID` (e.g. `joinmymoney.com`) and `WEBAUTHN_ORIGIN` (e.g. `https://joinmymoney.com`) before users enrol.
- A password reset (recovery code or reset link) removes the person's enrolled phones.
- The new table is created by `flask sync-schema`, which the start command runs before gunicorn (`flask --app run sync-schema && gunicorn run:app ...`). Do NOT use `flask db upgrade` on this project's live database: it was built with `create_all`, has no Alembic version record, and the migration fails with "table already exists", which stops the app from starting.

## Interest page and "total interest" rule
- **Interest** (left menu, officials only) shows four figures: **Received** (collected so far), **Expected** (still to collect on running loans), **Incoming** (expected interest whose date has not arrived; the next 30 days are called out) and **Overdue** (date passed, unpaid). Tabs list every interest date by loan and borrower: Coming in, Overdue, Received, All dates, and By loan.
- A loan's full-term interest is only shown, and only counted as *earned*, once the loan is **fully repaid**. Until then the loan page shows "Interest received so far", the dashboard / Loans / Interest-income report count completed loans only, and the Loan disbursements report shows interest received so far.
- Not changed: the payment schedule and agreement PDFs given to the borrower still list each instalment's interest, and the loan page's schedule table still shows interest per instalment (needed to collect payments).

## Overdue & late fees (company policy)
Menu -> **Overdue & Late Fees** (officials only). Rules: no fine from the due date through day 7; after that **5% of the late amount for every completed 7-day block**, capped at **100%** (`LATE_FEE_BLOCK_MODE=STARTED` charges at the start of each block instead).
- **Tracking log** with the columns Loan ID | Borrower Name | Due Date | Days Overdue | Principal Due | Penalty % | Fine Accrued | Total Owed | Last Notified, exportable to CSV. One row per overdue instalment.
- **Admin alert** (in-app notification to every official) once each time a borrower enters a new penalty block, with borrower, loan, due date, days overdue, late amount, tier, fine, new total and status ("Active Overdue - Tier N" / "Max Fine Cap Reached"). Alerts fire when an official opens the dashboard or this page, and from `flask late-fees`; schedule that command daily (Render Cron Job: `flask --app run late-fees`) so alerts do not wait for someone to log in.
- **PDF notice**: the PDF icon on each row (or tapping the alert) builds the "Overdue Loan & Late Fine Notice" with the current fine. Payment instructions come from the group's Payment instructions setting.
- These figures are informational: nothing is posted to the ledger here. The older per-group "Loan fines" (Group rules) and lender product penalties are separate and unchanged.
- New table `late_fee_tracker`: created by `flask sync-schema` on deploy.

## Speed notes
Icons and charts are now served from this site, `/static` files carry a version stamp and are cached for a year, pages are compressed, `/sw.js` is actually served (it was registered but returned 404, so the service worker never ran), and per-page database round trips were cut (menu counters 4 -> 1, platform settings cached 30 s, group settings once per request, group loaded with the membership). Set `DB_POOL_PRE_PING=0` to save one more round trip per request if you see no stale-connection errors on Turso.

## Password recovery
- **Forgot password:** the member files a request. A group administrator, treasurer or vice chair of their group sees it under *Password Resets*, confirms it is really that person, then sends a reset link (valid 24 hours, one use, emailed too if SMTP and an email exist). Links only work for admin-approved requests. Officials cannot approve their own request.
- **Recovery code:** on first sign-in every account must save a one-time recovery code (shown once, downloadable as .txt). Only a hash is stored. It can be used at `/recover` to reset the password without an administrator; it is single use and a new one is issued at the next sign-in. A new code can be generated from *Profile* (needs the current password).
- Platform admins have no group to ask, so they rely on the recovery code (or `flask create-platform-admin` access on the server).

## Security and design notes
Tenant isolation is enforced centrally (`tenant_route`, `scoped`). Money is stored in integer minor units, interest uses Decimal, the ledger is append-only (corrections are reversal rows), and each loan stores its own terms so platform default changes affect new loans only.

## Known limitations (please read)
- The biometric and Turnstile browser flows were verified with automated tests and a software authenticator, not on a physical phone or against your live Cloudflare keys: try both once after deploying.
- Not visually verified in a browser against the mockup; the UI was built from the CSS design system and only render-tested via the test client.
- The ImageKit upload URL default (`https://upload.imagekit.io/api/v1/files/upload`) is unverified; confirm against ImageKit docs and override `IMAGEKIT_UPLOAD_URL` if needed. Neither provider was exercised against live accounts.
- Uploadcare REST uses the `Uploadcare.Simple` auth scheme; use signed auth for production hardening.
- Image crop is server-side centre-square only. `@mention` notifications are not implemented. Any member may attach documents to community posts.
- Bootstrap is not used (custom CSS). Libsql driver behaviour on Turso was not tested live.
