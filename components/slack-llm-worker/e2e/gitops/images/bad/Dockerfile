# Stand-in for the 6/30 bad build: exits on startup, like a missing config or import error.
FROM busybox:1.36.1
CMD ["sh", "-c", "echo 'fatal: required setting REPORT_QUEUE_URL is missing' >&2; exit 1"]
