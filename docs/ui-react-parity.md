# React UI parity contract

The React migration preserves the current operator experience. It changes the
rendering architecture, not the product workflow or visual language.

## Audience and jobs

- Operators use **Status** to understand current translation and maintenance
  work, queued recovery, protection state, recent outcomes, and service health.
- Operators use **Manual review** to inspect failed recovery plans, recheck an
  externally restored file, authorize one retry, or dismiss a record.
- Operators use **Logs** to search sanitized operational output without gaining
  access to credentials, subtitle text, hashes, or absolute managed paths.

## Shared behavior

- `/`, `/review`, and `/logs` retain their URLs and consistent navigation.
- Dark and warm-cream light themes use the existing Kaneel tokens and persist
  through the `dashboard-theme` local-storage key, with system preference as
  fallback.
- Every request has visible loading, empty, failure, and recovery behavior.
- All controls use native semantic elements, visible focus, accessible names,
  and a predictable keyboard order. Status changes use appropriate polite or
  assertive live announcements without announcing every background refresh.
- Buttons and filters remain usable at 200% zoom and on narrow viewports. Data
  tables retain the current card reflow at 920 px and below.
- Reduced-motion preferences disable non-essential transitions.

## Status acceptance criteria

- Bootstrap data renders before the first request and is removed from the DOM.
- Active polling remains 3 seconds, idle polling 20 seconds, exponential error
  backoff is capped at 60 seconds, and hidden tabs do not poll.
- Manual refresh cannot overlap an in-flight request.
- Work views, retry sorting, pagination, expanded retry/observation details,
  recovery diagnostics, and focused controls survive background updates.
- Status tooltips work with pointer, keyboard focus, click, Escape, and viewport
  repositioning.
- Invalid bootstrap data and refresh failures retain a usable page and explain
  that displayed data may be stale.

## Manual-review acceptance criteria

- Filters, sorting, pagination, background polling, long paths, technical
  details, action history, and empty results remain available.
- Existing data stays visible when a background refresh fails.
- Actions are disabled while requests run and when the server reports that
  manual actions are disabled.
- `queue_retry` and `dismiss` require confirmation. Every mutation sends the
  action header and expected update timestamp and exposes 400/403/404/409/503
  feedback to the operator.
- Focus returns to the initiating control when an action fails and moves to the
  result announcement when the affected record is no longer present.

## Logs acceptance criteria

- Level, show/job, and free-text filters preserve their current query contract.
- Refresh replaces results, **Load older** appends results, and a new filter
  resets the cursor.
- Log text is rendered as text, never HTML. Empty, failure, sanitized record
  count, and pagination states are announced.

## Explicit non-goals

- No API, database, persistence, authentication, or external service change.
- No client-side router, state-management dependency, or separate SPA server.
- No visual redesign or optional information-density changes in this PR.
