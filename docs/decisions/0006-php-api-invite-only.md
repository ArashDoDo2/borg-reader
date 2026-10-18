# 0006 — Optional PHP API, invite-only accounts, moderated public catalog

Status: accepted (2026-10-18, v7)

## Context
The owner wants accounts, cross-device sync of books and progress, and a public library everyone can add to.
Hosting is shared cPanel (PHP + MySQL only). Users are the owner and a few invited people.

## Decision
- Plain PHP 8 + PDO API under `/borg/api/`, MySQL in production, SQLite for automated tests.
- Registration only with an admin-created invite code; first admin via a one-time setup key in the config file.
- Uploaded books are stored outside `public_html`, deduplicated by SHA-256, served only after a permission check.
- Anyone may submit a book to the public catalog; it is visible to others only after admin approval
  (`catalog_requires_approval`, default true). Rationale: copyright and abuse risk is carried by the site owner.
- The app keeps working fully offline and without an account; the API is an add-on, not a dependency.

## Consequences
No vendor lock-in or build step; deploys stay a file copy. Secrets live only in `borg-config/config.php`.
Moving to open registration later needs email verification, password reset by email and stronger abuse controls.
