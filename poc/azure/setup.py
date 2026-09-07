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


LOCATION = "eastus"
LAW_NAME = "law-gns-poc-eastus"
EVENT_HUB_NAMESPACE = "securegns-heartbeat-poc-ehns"
EVENT_HUB_NAME = "heartbeat"
VM_NAME = "vm-gns-poc-win01"
VM_SIZE = "Standard_B1s"
VM_IMAGE = "MicrosoftWindowsServer:WindowsServer:2022-datacenter-azure-edition:latest"
ADMIN_USERNAME = "gns"
ADMIN_PASSWORD = "SitaGns-123"
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
    storage_suffix = hashlib.sha256(rg.lower().encode()).hexdigest()[:16]
    storage_account = f"stgnspoc{storage_suffix}"

    az("group", "show", "--name", rg)

    print("Creating Log Analytics workspace...")
    az(
        "monitor", "log-analytics", "workspace", "create",
        "--resource-group", rg,
        "--workspace-name", LAW_NAME,
        "--location", LOCATION,
        "--sku", "PerGB2018",
    )
    law_workspace_id = az(
        "monitor", "log-analytics", "workspace", "show",
        "--resource-group", rg,
        "--workspace-name", LAW_NAME,
        "--query", "customerId",
        output="tsv",
    )
    law_workspace_key = az(
        "monitor", "log-analytics", "workspace", "get-shared-keys",
        "--resource-group", rg,
        "--workspace-name", LAW_NAME,
        "--query", "primarySharedKey",
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
    az(
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
    )

    print("Connecting the VM to Log Analytics...")
    az(
        "vm", "extension", "set",
        "--resource-group", rg,
        "--vm-name", VM_NAME,
        "--name", "MicrosoftMonitoringAgent",
        "--publisher", "Microsoft.EnterpriseCloud.Monitoring",
        "--settings", json.dumps({"workspaceId": law_workspace_id}),
        "--protected-settings", json.dumps({"workspaceKey": law_workspace_key}),
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
