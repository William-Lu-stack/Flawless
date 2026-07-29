#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

version="${ARGO_ROLLOUTS_VERSION:-v1.9.1}"
namespace="${ARGO_ROLLOUTS_NAMESPACE:-argo-rollouts}"
official_url="https://github.com/argoproj/argo-rollouts/releases/download/${version}/install.yaml"
official_sha256="78c82343803c2bbc13a36049e269a532dd67f25b7e2cb3603c99e31d8d8a40b5"
source_manifest="${ARGO_ROLLOUTS_INSTALL_MANIFEST:-}"
domestic_image="${ARGO_ROLLOUTS_IMAGE:-quay.m.daocloud.io/argoproj/argo-rollouts:v1.9.1@sha256:15c0d41f2c69a382d4399bcb28ed4f03ee9f58b56cfc9e6cd55bcbf0f311c06d}"
work_dir="$(mktemp -d)"
trap 'rm -r "$work_dir"' EXIT

if [[ -n "$source_manifest" ]]; then
  cp "$source_manifest" "$work_dir/install.yaml"
else
  curl -fsSL --retry 5 --retry-all-errors "$official_url" -o "$work_dir/install.yaml"
  actual_sha256="$(sha256sum "$work_dir/install.yaml" | awk '{print $1}')"
  if [[ "$version" == "v1.9.1" && "$actual_sha256" != "$official_sha256" ]]; then
    echo "Argo Rollouts install.yaml SHA256 mismatch: $actual_sha256" >&2
    exit 1
  fi
fi

sed \
  "s#quay.io/argoproj/argo-rollouts:${version}#${domestic_image}#g" \
  "$work_dir/install.yaml" > "$work_dir/install-domestic.yaml"

kubectl create namespace "$namespace" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n "$namespace" -f "$work_dir/install-domestic.yaml"
kubectl rollout status deployment/argo-rollouts -n "$namespace" --timeout=300s
kubectl get deployment,pod -n "$namespace" -o wide
