#!/bin/sh
# Start as root, make sure the mounted folders are writable, then drop to PUID:PGID.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
UMASK="${UMASK:-002}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR" "$LIBRARY_DIR" "$INGEST_DIR"
    # app state: fix fully; library/ingest: only the top folders (libraries can be huge)
    chown -R "$PUID:$PGID" "$DATA_DIR"
    chown "$PUID:$PGID" "$LIBRARY_DIR" "$INGEST_DIR" 2>/dev/null || true
    umask "$UMASK"
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

umask "$UMASK"
exec "$@"
