import json

from azimuth_schedule_operator.models import registry
from azimuth_schedule_operator.tests import base


class TestModels(base.TestCase):
    def test_cluster_type_crd_json(self):
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
                  "notBefore": {
                    "format": "date-time",
                    "type": "string"
                  },
                  "notAfter": {
                    "format": "date-time",
                    "type": "string"
                  }
                },
                "required": [
                  "ref",
                  "notBefore",
                  "notAfter"
                ],
                "type": "object"
              },
              "status": {
                "properties": {
                  "refFound": {
                    "type": "boolean"
                  },
                  "deleteTriggered": {
                    "type": "boolean"
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
