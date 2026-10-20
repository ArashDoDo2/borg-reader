# 0010 — Two-level catalog categories, several per book, admin-managed list

Status: accepted (2026-10-20, v8.10)

## Context
The public catalog had one free-text category per book. With ~2,000–3,000 books a year the owner wants a browsable
category tree like large e-book sites (category → subcategories). Owner's decisions: exactly two levels, a book may be
in several categories, the admin keeps a fixed list, and books pushed through the API key route create missing
categories automatically.

## Decision
- Migration 9: `categories` (`parent_id` 0 = top level, unique name among siblings, `sort`) and the link table
  `catalog_categories` (catalog book ↔ category, cascading). Existing category strings became top-level categories.
- Max 10 categories per book. A subcategory cannot have children; a category with children cannot be deleted or moved
  under another (409 `has_children`). Deleting a category relinks its books to a chosen category or unlinks them.
- The legacy `catalog.category` column and the `category` field in responses stay as a mirror (top-level name of the
  first category) so older cached clients keep working; the `category=` filter still works. New code uses `cat=ID`
  (a top-level ID includes its children) and `categories: [ids]`.
- Only admins create categories (category tab, legacy name on admin routes, `api-book` paths `Parent/Child; Other`).
  Users submitting a book pick from the existing list.
- Counts: a top-level count is the distinct number of approved books in it or any of its children.
- No per-category icons (deferred).

## Consequences
- The catalog navigator, admin tree, picker and filters all depend on `GET categories`; when it is empty the client
  falls back to the old free-text flows.
- E2E sections that test the free-text flows mock an empty category list (the shared test server has real categories
  after section 10).
