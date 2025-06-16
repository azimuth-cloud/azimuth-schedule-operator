# ruff: noqa: E501

import json

from azimuth_schedule_operator.models import registry
from azimuth_schedule_operator.tests import base


class TestModels(base.TestCase):
    def test_schedule_crd_json(self):
        schedule_crd = None
        for resource in registry.get_crd_resources():
            meta = resource.get("metadata", {})
            name = meta.get("name")
            if name == "schedules.scheduling.azimuth.stackhpc.com":
                schedule_crd = resource

        actual = json.dumps(schedule_crd, indent=2)
        expected = """\
{
  "apiVersion": "apiextensions.k8s.io/v1",
  "kind": "CustomResourceDefinition",
  "metadata": {
    "name": "schedules.scheduling.azimuth.stackhpc.com"
  },
  "spec": {
    "group": "scheduling.azimuth.stackhpc.com",
    "scope": "Namespaced",
    "names": {
      "kind": "Schedule",
      "singular": "schedule",
      "plural": "schedules",
      "shortNames": [],
      "categories": [
        "azimuth"
      ]
    },
    "versions": [
      {
        "name": "v1alpha1",
        "served": true,
        "storage": true,
        "schema": {
          "openAPIV3Schema": {
            "properties": {
              "spec": {
                "properties": {
                  "ref": {
                    "properties": {
                      "apiVersion": {
                        "type": "string"
                      },
                      "kind": {
                        "type": "string"
                      },
                      "name": {
                        "type": "string"
                      }
                    },
                    "required": [
                      "apiVersion",
                      "kind",
                      "name"
                    ],
                    "type": "object"
                  },
                  "notAfter": {
                    "format": "date-time",
                    "type": "string"
                  }
                },
                "required": [
                  "ref",
                  "notAfter"
                ],
                "type": "object"
              },
              "status": {
                "properties": {
                  "refExists": {
                    "type": "boolean"
                  },
                  "refDeleteTriggered": {
                    "type": "boolean"
                  },
                  "updatedAt": {
                    "format": "date-time",
                    "nullable": true,
                    "type": "string"
                  }
                },
                "type": "object"
              }
            },
            "required": [
              "spec"
            ],
            "type": "object"
          }
        },
        "subresources": {
          "status": {}
        },
        "additionalPrinterColumns": [
          {
            "name": "Age",
            "type": "date",
            "jsonPath": ".metadata.creationTimestamp"
          }
        ]
      }
    ]
  }
}"""
        self.assertEqual(expected, actual)

    def test_lease_crd_json(self):
        lease_crd = None
        for resource in registry.get_crd_resources():
            meta = resource.get("metadata", {})
            name = meta.get("name")
            if name == "leases.scheduling.azimuth.stackhpc.com":
                lease_crd = resource

        actual = json.dumps(lease_crd, indent=2)
        expected = """\
{
  "apiVersion": "apiextensions.k8s.io/v1",
  "kind": "CustomResourceDefinition",
  "metadata": {
    "name": "leases.scheduling.azimuth.stackhpc.com"
  },
  "spec": {
    "group": "scheduling.azimuth.stackhpc.com",
    "scope": "Namespaced",
    "names": {
      "kind": "Lease",
      "singular": "lease",
      "plural": "leases",
      "shortNames": [],
      "categories": [
        "azimuth"
      ]
    },
    "versions": [
      {
        "name": "v1alpha1",
        "served": true,
        "storage": true,
        "schema": {
          "openAPIV3Schema": {
            "description": "A lease consisting of one or more reserved resources.",
            "properties": {
              "spec": {
                "description": "The spec of a lease.",
                "properties": {
                  "cloudCredentialsSecretName": {
                    "description": "The name of the secret containing the cloud credentials.",
                    "minLength": 1,
                    "type": "string"
                  },
                  "startsAt": {
                    "description": "The start time for the lease. If no start time is given, it is assumed to start immediately.",
                    "format": "date-time",
                    "nullable": true,
                    "type": "string"
                  },
                  "endsAt": {
                    "description": "The end time for the lease. If no end time is given, the lease is assumed to be infinite.",
                    "format": "date-time",
                    "nullable": true,
                    "type": "string"
                  },
                  "gracePeriod": {
                    "description": "The grace period before the end of the lease that the platform will be given to shut down gracefully. If not given, the operator default grace period will be used.",
                    "minimum": 0,
                    "nullable": true,
                    "type": "integer"
                  },
                  "resources": {
                    "description": "The resources that a lease is reserving.",
                    "properties": {
                      "machines": {
                        "description": "Machines that should be reserved by the lease.",
                        "items": {
                          "description": "Represents a reservation for a machine.",
                          "properties": {
                            "sizeId": {
                              "description": "The ID of the size for the machine.",
                              "minLength": 1,
                              "type": "string"
                            },
                            "count": {
                              "description": "The number of machines of this size to reserve.",
                              "exclusiveMinimum": true,
                              "minimum": 0,
                              "type": "integer"
                            }
                          },
                          "required": [
                            "sizeId",
                            "count"
                          ],
                          "type": "object"
                        },
                        "type": "array"
                      }
                    },
                    "type": "object"
                  }
                },
                "required": [
                  "cloudCredentialsSecretName",
                  "resources"
                ],
                "type": "object"
              },
              "status": {
                "description": "The status of a lease.",
                "properties": {
                  "phase": {
                    "description": "The phase of a lease.",
                    "enum": [
                      "Pending",
                      "Active",
                      "Terminated",
                      "Error",
                      "Creating",
                      "Starting",
                      "Updating",
                      "Terminating",
                      "Deleting",
                      "Unknown"
                    ],
                    "type": "string"
                  },
                  "errorMessage": {
                    "description": "The error message for the lease, if known.",
                    "type": "string"
                  },
                  "sizeMap": {
                    "additionalProperties": {
                      "type": "string"
                    },
                    "description": "Mapping of original size ID to reserved size ID.",
                    "type": "object",
                    "x-kubernetes-preserve-unknown-fields": true
                  },
                  "sizeNameMap": {
                    "additionalProperties": {
                      "type": "string"
                    },
                    "description": "Mapping of original size name to reserved size name.",
                    "type": "object",
                    "x-kubernetes-preserve-unknown-fields": true
                  }
                },
                "type": "object",
                "x-kubernetes-preserve-unknown-fields": true
              }
            },
            "required": [
              "spec"
            ],
            "type": "object"
          }
        },
        "subresources": {
          "status": {}
        },
        "additionalPrinterColumns": [
          {
            "name": "Starts At",
            "type": "string",
            "format": "date-time",
            "jsonPath": ".spec.startsAt"
          },
          {
            "name": "Ends At",
            "type": "string",
            "format": "date-time",
            "jsonPath": ".spec.endsAt"
          },
          {
            "name": "phase",
            "type": "string",
            "jsonPath": ".status.phase"
          },
          {
            "name": "Age",
            "type": "date",
            "jsonPath": ".metadata.creationTimestamp"
          }
        ]
      }
    ]
  }
}"""
        self.assertEqual(expected, actual)
