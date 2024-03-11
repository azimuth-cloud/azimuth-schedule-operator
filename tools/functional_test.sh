#!/bin/bash

set -ex

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# Install the CaaS operator from the chart we are about to ship
# Make sure to use the images that we just built
helm upgrade azimuth-schedule-operator ./charts/operator \
  --dependency-update \
  --namespace azimuth-schedule-operator \
  --create-namespace \
  --install \
  --wait \
  --timeout 10m \
  --set-string image.tag=${GITHUB_SHA::7}

until [ `kubectl get crds | grep schedule | wc -l` -gt 1 ]; do echo "wait for crds"; sleep 5; done
kubectl get crds

kubectl apply -f $SCRIPT_DIR/test_schedule.yaml
# until kubectl wait --for=jsonpath='{.status.phase}'=Available clustertype quick-test; do echo "wait for status to appear"; sleep 5; done
kubectl get schedule caas-cluster -o yaml
