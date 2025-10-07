#!/bin/bash

set -exo pipefail

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# Install the CaaS operator from the chart we are about to ship
# Make sure to use the images that we just built
helm upgrade azimuth-schedule-operator ./charts/operator \
  --dependency-update \
  --create-namespace \
  --install \
  --wait \
  --timeout 10m \
  --set-string image.tag=${IMAGE_TAG} \
  --set-string config.blazarEnabled=no \
  --set config.checkInterval=2 \
  --set config.defaultGracePeriod=0 \
  --set-json 'managedResources=[{"apiGroup": "", "resources": ["configmaps"]}]'

until [ `kubectl get crds | grep schedules.scheduling.azimuth.stackhpc.com | wc -l` -eq 1 ]; do echo "wait for crds"; sleep 5; done
kubectl get crds


get_date() (
    set +x -e
    TZ=UTC date --date="$1" +"%Y-%m-%dT%H:%M:%SZ"
)


#####
# Test the deprecated schedule CRD
#####
export AFTER="$(get_date "-1 hour")"
envsubst < $SCRIPT_DIR/test_schedule.yaml | kubectl apply -f -
kubectl wait --for=jsonpath='{.status.refExists}'=true schedule caas-mycluster
# ensure updatedAt is written out
kubectl get schedule caas-mycluster -o yaml | grep "updatedAt"
kubectl wait --for=jsonpath='{.status.refDeleteTriggered}'=true schedule caas-mycluster
kubectl get schedule caas-mycluster -o yaml


JOB_ID="${GITHUB_JOB_ID:-test}"


create_credential_secret() (
    set +x -e
    tmpfile="$(mktemp)"
    openstack application credential create --unrestricted -f json "az-schedule-$1-$JOB_ID" > $tmpfile
    kubectl apply -f - 1>&2 <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: $1
stringData:
  clouds.yaml: |
    clouds:
      openstack:
        auth:
          auth_url: $(openstack catalog show -f json identity | jq -r '.endpoints | map(select(.interface == "public")) | first | .url')
          application_credential_id: $(jq -r '.ID' "$tmpfile")
          application_credential_secret: $(jq -r '.Secret' "$tmpfile")
        region_name: RegionOne
        interface: public
        identity_api_version: 3
        auth_type: v3applicationcredential
EOF
    echo "$(jq -r '.ID' "$tmpfile")"
)


verify_credential_deleted() (
    set +x -e
    if openstack application credential show $2 >/dev/null 2>&1; then
	# https://review.opendev.org/c/openstack/python-openstackclient/+/962663
	appcred_id=$(openstack application credential show $2 -f json | jq -r ".ID")
	if [ ${#appcred_id} -eq 32 ]; then
	    echo "Application credential $2 still exists" 1>&2
            return 1
	fi
    fi
    if kubectl get secret $1 >/dev/null 2>&1; then
        echo "Kubernetes secret $1 still exists" 1>&2
        return 1
    fi
    echo "Credential $1 deleted successfully"
)


create_configmap() (
    set +x -e
    kubectl create configmap $1 \
      --from-literal=key1=config1 \
      --from-literal=key2=config2 \
      --output go-template='{{.metadata.uid}}'
)


verify_configmap_deleted() (
    set +x -e
    if kubectl get configmap $1 >/dev/null 2>&1; then
        echo "ConfigMap $1 still exists" 1>&2
        return 1
    fi
    echo "ConfigMap $1 deleted successfully"
)


create_lease() (
    set +x -eo pipefail
    LEASE_NAME="$1" \
    OWNER_UID="$2" \
    END_TIME="$3" \
    START_TIME="$4" \
    gomplate < "$SCRIPT_DIR/lease.yaml" | \
      kubectl apply -f -
)


check_lease_phase() (
    set +x -e
    phase="$(kubectl get lease.scheduling $1 -o go-template='{{.status.phase}}')"
    if [ "$phase" == "$2" ]; then
        echo "Lease $1 has phase $phase, as expected"
        return
    else
        echo "Lease $1 has phase $phase, not $2" 1>&2
        return 1
    fi
)


delete_lease() (
    set +x -e
    kubectl delete lease.scheduling $1
)


verify_lease_deleted() (
    set +x -e
    if kubectl get lease.scheduling $1 >/dev/null 2>&1; then
        echo "Lease $1 still exists" 1>&2
        return 1
    fi
    echo "Lease $1 deleted successfully"
)


cleanup() {
    # When we exit, delete all the leases and configmaps
    set +xe
    echo "Cleaning up resources..."
    kubectl delete lease.scheduling --all
    # for debugging get the logs from the operator
    kubectl logs deployment/azimuth-schedule-operator
}
trap cleanup EXIT


#####
# Test the lease CRD with no start or end time
#####
appcred_id="$(create_credential_secret lease-no-end)"
create_lease lease-no-end
# Wait a few seconds then check that the lease has moved to active
sleep 5
check_lease_phase lease-no-end Active
delete_lease lease-no-end
# Verify that the application credential has been deleted
verify_credential_deleted lease-no-end $appcred_id

#####
# Test the lease CRD with an end time but no start time
#####
create_credential_secret lease-end-no-start
# Create the configmap that we will delete
owner_uid="$(create_configmap lease-end-no-start)"
# The end time will be one minute in the future
create_lease lease-end-no-start "$owner_uid" "$(get_date "+1 minute")"
# Wait for a few seconds, then check that the lease is active
sleep 5
check_lease_phase lease-end-no-start Active
# Wait for another 60 seconds, then verify that the configmap, lease and credential are gone
sleep 60
verify_configmap_deleted lease-end-no-start
verify_lease_deleted lease-end-no-start
verify_credential_deleted lease-end-no-start

#####
# Test the lease CRD with a start time and end time
#####
create_credential_secret lease-start-end
# Create the configmap that we will delete
owner_uid="$(create_configmap lease-start-end)"
# The start time will be 30s in the future and the end time one minute
create_lease lease-start-end "$owner_uid" "$(get_date "+1 minute")" "$(get_date "+30 seconds")"
# Wait for a few seconds and check that the lease is still pending
sleep 15
check_lease_phase lease-start-end Pending
# Wait for another 30s and check that the lease is active
sleep 30
check_lease_phase lease-start-end Active
# Wait for another 20 seconds, the verify that the configmap, lease and credential are gone
sleep 20
verify_configmap_deleted lease-start-end
verify_lease_deleted lease-start-end
verify_credential_deleted lease-start-end

#####
# TODO(mkjpryor) Test the lease CRD with Blazar
#####
