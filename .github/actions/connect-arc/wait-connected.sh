#!/usr/bin/env bash
# Wait for an Arc-enabled cluster to report connectivityStatus=Connected.
#
# Usage: wait-connected.sh <cluster-name> <resource-group>
#
# Extracted from action.yaml so the post-restart re-wait can reuse the exact
# same logic (and we avoid duplicating ~20 lines of polling).

set -euo pipefail

CLUSTER_NAME="${1:?cluster name required}"
RESOURCE_GROUP="${2:?resource group required}"

MAX_ATTEMPTS=20
SLEEP_SECONDS=15

# Capture stderr to a tempfile so we can surface the underlying az failure on
# the final attempt. Silently masking to "Unknown" for 5 minutes on a
# permanent auth/RBAC/not-found problem wastes runner time and produces a
# useless error message.
ERR_FILE=$(mktemp)
trap 'rm -f "${ERR_FILE}"' EXIT

for attempt in $(seq 1 $MAX_ATTEMPTS); do
  if STATUS=$(az connectedk8s show \
      --name "${CLUSTER_NAME}" \
      --resource-group "${RESOURCE_GROUP}" \
      --query connectivityStatus \
      --output tsv 2>"${ERR_FILE}"); then
    :
  else
    STATUS="ERROR"
  fi

  if [ "${STATUS}" = "Connected" ]; then
    echo "Cluster '${CLUSTER_NAME}' is Connected (attempt ${attempt})."
    exit 0
  fi

  if [ "${attempt}" -eq "${MAX_ATTEMPTS}" ]; then
    echo "::error::Cluster '${CLUSTER_NAME}' did not reach Connected within $((MAX_ATTEMPTS * SLEEP_SECONDS))s (last status: ${STATUS})"
    if [ -s "${ERR_FILE}" ]; then
      echo "::group::Last az connectedk8s show stderr"
      cat "${ERR_FILE}"
      echo "::endgroup::"
    fi
    exit 1
  fi

  echo "Cluster status='${STATUS}' (attempt ${attempt}/${MAX_ATTEMPTS}); retrying in ${SLEEP_SECONDS}s..."
  sleep "${SLEEP_SECONDS}"
done
