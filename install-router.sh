#!/bin/sh
# GitHub entry point, installer revision 82.0.12. Run with: sudo bash install-router.sh
set -eu
case "$0" in
    */*) SCRIPT_PARENT=${0%/*} ;;
    *) SCRIPT_PARENT=. ;;
esac
SCRIPT_DIR=$(CDPATH='' cd -- "$SCRIPT_PARENT" && pwd)
if [ -f "$SCRIPT_DIR/deploy/hash_router/install.sh" ]; then
    ROUTER_ROOT="$SCRIPT_DIR"
else
    echo 'Router package not found. Clone the complete hashroute repository.' >&2
    exit 1
fi
for required in hash_router/__main__.py deploy/hash_router/setup_connection.py deploy/hash_router/config.example.json deploy/hash_router/requirements.txt deploy/hash_router/hash-router.service; do
    if [ ! -f "$ROUTER_ROOT/$required" ]; then
        echo "Incomplete router package: $required" >&2
        exit 1
    fi
done
if [ "$#" -gt 1 ]; then
    echo 'Usage: sudo bash install-router.sh [--check]' >&2
    exit 2
fi
case "${1:-}" in
    --check) echo 'Hash Router installer 82.0.12: package complete.'; exit 0 ;;
    '') ;;
    *) echo 'Usage: sudo bash install-router.sh [--check]' >&2; exit 2 ;;
esac
if [ "$#" -gt 0 ]; then
    echo 'Usage: sudo bash install-router.sh [--check]' >&2
    exit 2
fi
exec bash "$ROUTER_ROOT/deploy/hash_router/install.sh"
