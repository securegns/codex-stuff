#!/usr/bin/env python3
"""Create a small Azure Heartbeat-to-Event-Hub POC with Azure CLI.

Usage: python setup.py <resource-group>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


LOCATION = "eastus"
LAW_NAME = "law-gns-poc-eastus"
EVENT_HUB_NAMESPACE = "securegns-heartbeat-poc-ehns"
EVENT_HUB_NAME = "heartbeat"
VM_NAME = "vm-gns-poc-win01"
VM_SIZE = "Standard_B1s"
VM_IMAGE = "MicrosoftWindowsServer:WindowsServer:2022-datacenter-azure-edition:latest"
ADMIN_USERNAME = "gns"
ADMIN_PASSWORD = "SitaGns-123"
DCR_NAME = "dcr-gns-poc-heartbeat"
DCR_ASSOCIATION_NAME = "dcra-gns-poc-heartbeat"
DATA_EXPORT_NAME = "export-heartbeat-to-eventhub"
BLOB_CONTAINER = "checkpoints"


def az(*args: str, output: str = "none") -> str:
    command = ["az", *args, "--only-show-errors", "--output", output]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        raise SystemExit(result.returncode)
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("resource_group", help="Existing Azure resource group")
    args = parser.parse_args()

    if not shutil.which("az"):
        raise SystemExit("Azure CLI is not installed or is not in PATH.")

    rg = args.resource_group
    subscription_id = az("account", "show", "--query", "id", output="tsv")
    storage_suffix = hashlib.sha256(f"{subscription_id}:{rg}".encode()).hexdigest()[:16]
    storage_account = f"stgnspoc{storage_suffix}"

    az("group", "show", "--name", rg)
    az("provider", "register", "--namespace", "Microsoft.Insights", "--wait")

    print("Creating Log Analytics workspace...")
    law_id = az(
        "monitor", "log-analytics", "workspace", "create",
        "--resource-group", rg,
        "--workspace-name", LAW_NAME,
        "--location", LOCATION,
        "--sku", "PerGB2018",
        "--query", "id",
        output="tsv",
    )

    print("Creating Event Hub...")
    az(
        "eventhubs", "namespace", "create",
        "--resource-group", rg,
        "--name", EVENT_HUB_NAMESPACE,
        "--location", LOCATION,
        "--sku", "Basic",
        "--capacity", "1",
        "--public-network-access", "Enabled",
    )
    event_hub_id = az(
        "eventhubs", "eventhub", "create",
        "--resource-group", rg,
        "--namespace-name", EVENT_HUB_NAMESPACE,
        "--name", EVENT_HUB_NAME,
        "--partition-count", "2",
        "--query", "id",
        output="tsv",
    )

    print("Creating public-endpoint Blob Storage...")
    az(
        "storage", "account", "create",
        "--resource-group", rg,
        "--name", storage_account,
        "--location", LOCATION,
        "--sku", "Standard_LRS",
        "--kind", "StorageV2",
        "--public-network-access", "Enabled",
        "--default-action", "Allow",
        "--allow-shared-key-access", "true",
        "--allow-blob-public-access", "false",
        "--https-only", "true",
    )
    storage_key = az(
        "storage", "account", "keys", "list",
        "--resource-group", rg,
        "--account-name", storage_account,
        "--query", "[0].value",
        output="tsv",
    )
    az(
        "storage", "container", "create",
        "--name", BLOB_CONTAINER,
        "--account-name", storage_account,
        "--account-key", storage_key,
        "--public-access", "off",
    )

    print("Creating Windows Server VM...")
    vm_id = az(
        "vm", "create",
        "--resource-group", rg,
        "--name", VM_NAME,
        "--location", LOCATION,
        "--image", VM_IMAGE,
        "--size", VM_SIZE,
        "--admin-username", ADMIN_USERNAME,
        "--admin-password", ADMIN_PASSWORD,
        "--security-type", "Standard",
        "--nsg-rule", "NONE",
        "--query", "id",
        output="tsv",
    )

    print("Connecting the VM to Log Analytics...")
    az("vm", "identity", "assign", "--resource-group", rg, "--name", VM_NAME)
    az("extension", "add", "--name", "monitor-control-service", "--upgrade")

    dcr = {
        "kind": "Windows",
        "properties": {
            "dataSources": {
                "performanceCounters": [
                    {
                        "name": "minimalPerf",
                        "streams": ["Microsoft-Perf"],
                        "samplingFrequencyInSeconds": 60,
                        "counterSpecifiers": [r"\Processor(_Total)\% Processor Time"],
                    }
                ]
            },
            "destinations": {
                "logAnalytics": [
                    {"name": "law", "workspaceResourceId": law_id}
                ]
            },
            "dataFlows": [
                {"streams": ["Microsoft-Perf"], "destinations": ["law"]}
            ],
        },
    }

    rule_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as rule_file:
            json.dump(dcr, rule_file)
            rule_path = Path(rule_file.name)

        dcr_id = az(
            "monitor", "data-collection", "rule", "create",
            "--resource-group", rg,
            "--name", DCR_NAME,
            "--location", LOCATION,
            "--kind", "Windows",
            "--rule-file", str(rule_path),
            "--query", "id",
            output="tsv",
        )
    finally:
        if rule_path:
            rule_path.unlink(missing_ok=True)

    az(
        "vm", "extension", "set",
        "--ids", vm_id,
        "--name", "AzureMonitorWindowsAgent",
        "--publisher", "Microsoft.Azure.Monitor",
        "--enable-auto-upgrade", "true",
    )
    az(
        "monitor", "data-collection", "rule", "association", "create",
        "--name", DCR_ASSOCIATION_NAME,
        "--rule-id", dcr_id,
        "--resource", vm_id,
    )

    print("Exporting new Heartbeat records from Log Analytics to Event Hub...")
    az(
        "monitor", "log-analytics", "workspace", "data-export", "create",
        "--resource-group", rg,
        "--workspace-name", LAW_NAME,
        "--name", DATA_EXPORT_NAME,
        "--tables", "Heartbeat",
        "--destination", event_hub_id,
        "--enable", "true",
    )

    namespace_connection = az(
        "eventhubs", "namespace", "authorization-rule", "keys", "list",
        "--resource-group", rg,
        "--namespace-name", EVENT_HUB_NAMESPACE,
        "--name", "RootManageSharedAccessKey",
        "--query", "primaryConnectionString",
        output="tsv",
    )
    event_hub_connection = f"{namespace_connection};EntityPath={EVENT_HUB_NAME}"

    print("\nSetup complete")
    print(f"Event Hub connection string: {event_hub_connection}")
    print(f"Event Hub name: {EVENT_HUB_NAME}")
    print(f"Storage account name: {storage_account}")
    print(f"Storage account key: {storage_key}")


if __name__ == "__main__":
    main()
