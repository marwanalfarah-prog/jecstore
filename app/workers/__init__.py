"""Background jobs.

Part II §7.4 offers Celery + Redis for production job volume, or APScheduler as
a lighter alternative "if job volume stays modest". This store's recurring work
is a handful of periodic sweeps — draining the outbox, low-stock alerts, marking
abandoned carts — so APScheduler is the honest choice: no broker to run, no
worker fleet to operate.

Every job is written to be **idempotent and safe to run twice**, so switching to
Celery later is a scheduling change rather than a rewrite.
"""
