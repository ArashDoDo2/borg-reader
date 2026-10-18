# 0003 — Keep existing storage names after the rename

Status: accepted (2026-10-18)

## Decision
`ketabkhane-db`, `ketabkhane-reader-preferences` and `ketab-theme` keep their names even though the product
is now called Borg.

## Consequences
Existing users keep their books and settings with no migration. The names are internal and never shown.
Renaming them later requires a tested migration that copies data before deleting anything.
