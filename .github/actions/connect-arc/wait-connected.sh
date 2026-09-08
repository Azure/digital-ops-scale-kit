#!/usr/bin/env bash
# Wait for an Arc-enabled cluster to report connectivityStatus=Connected.
#
# Usage: wait-connected.sh <cluster-name> <resource-group>
#
# Shared by the initial and post-restart connectivity checks.

set -euo pipefail

CLUSTER_NAME="${1:?cluster name required}"
RESOURCE_GROUP="${2:?resource group required}"

MAX_ATTEMPTS=20
SLEEP_SECONDS=15

ERR_FILE=$(mktemp)
trap 'rm -f "${ERR_FILE}"' EXIT

for attempt in $(seq 1 $MAX_ATTEMPTS); do
  if STATUS=$(az connectedk8s show \
      --name "${CLUSTER_NAME}" \
      --resource-group "${RESOURCE_GROUP}" \
      --query connectivityStatus \
      --output tsv 2>"${ERR_FILE}"); then
    QUERY_FAILED=0
  else
    QUERY_FAILED=1
    STATUS=""
  fi

  if [ "${STATUS}" = "Connected" ]; then
    echo "Arc cluster is Connected (attempt ${attempt})."
    exit 0
  fi

  if [ "${attempt}" -eq "${MAX_ATTEMPTS}" ]; then
    if [ "${QUERY_FAILED}" -eq 1 ]; then
      echo "::error::Arc connectivity could not be queried after ${MAX_ATTEMPTS} attempts."
    else
      echo "::error::Arc cluster did not reach Connected after ${MAX_ATTEMPTS} attempts."
    fi
    exit 1
  fi

  if [ "${QUERY_FAILED}" -eq 1 ]; then
    echo "Arc connectivity query failed (attempt ${attempt}/${MAX_ATTEMPTS}). Retrying in ${SLEEP_SECONDS}s."
  else
    echo "Arc cluster is not Connected (attempt ${attempt}/${MAX_ATTEMPTS}). Retrying in ${SLEEP_SECONDS}s."
  fi
  sleep "${SLEEP_SECONDS}"
done
