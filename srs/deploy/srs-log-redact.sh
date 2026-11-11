#!/usr/bin/env bash
set -euo pipefail

# Preserve the SRS process as PID 1 so Docker shutdown signals reach it.
exec 3>&1
exec > >(
    LC_ALL=C sed -u -E \
        -e 's/"param"[[:space:]]*:[[:space:]]*"([^"\\]|\\.)*"/"param":"[redacted]"/g' \
        -e 's/\?[^[:space:]"\\]+/?[redacted]/g' \
        -e 's/(srs_ticket=)[A-Za-z0-9._%~-]+/\1[redacted]/g' \
        -e 's#(Authorization[:=][[:space:]]+(Basic|Bearer)[[:space:]]+)[A-Za-z0-9._~+/=-]+#\1[redacted]#Ig' \
        -e 's/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/[redacted-jwt]/g' >&3
) 2>&1
exec 3>&-
exec "$@"
