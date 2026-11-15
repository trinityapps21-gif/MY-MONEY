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

## Password recovery
- **Forgot password:** the member files a request. A group administrator, treasurer or vice chair of their group sees it under *Password Resets*, confirms it is really that person, then sends a reset link (valid 24 hours, one use, emailed too if SMTP and an email exist). Links only work for admin-approved requests. Officials cannot approve their own request.
- **Recovery code:** on first sign-in every account must save a one-time recovery code (shown once, downloadable as .txt). Only a hash is stored. It can be used at `/recover` to reset the password without an administrator; it is single use and a new one is issued at the next sign-in. A new code can be generated from *Profile* (needs the current password).
- Platform admins have no group to ask, so they rely on the recovery code (or `flask create-platform-admin` access on the server).

## Security and design notes
Tenant isolation is enforced centrally (`tenant_route`, `scoped`). Money is stored in integer minor units, interest uses Decimal, the ledger is append-only (corrections are reversal rows), and each loan stores its own terms so platform default changes affect new loans only.

## Known limitations (please read)
- Not visually verified in a browser against the mockup; the UI was built from the CSS design system and only render-tested via the test client.
- The ImageKit upload URL default (`https://upload.imagekit.io/api/v1/files/upload`) is unverified; confirm against ImageKit docs and override `IMAGEKIT_UPLOAD_URL` if needed. Neither provider was exercised against live accounts.
- Uploadcare REST uses the `Uploadcare.Simple` auth scheme; use signed auth for production hardening.
- Image crop is server-side centre-square only. `@mention` notifications are not implemented. Any member may attach documents to community posts.
- Bootstrap is not used (custom CSS). Libsql driver behaviour on Turso was not tested live.
