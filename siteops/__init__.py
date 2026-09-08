# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Site Ops - Multi-site Azure infrastructure orchestration.

Site Ops is a CLI tool for deploying Azure infrastructure across multiple
sites using Bicep/ARM templates with support for:

- Site definitions with labels, properties, and inheritance
- Manifest-based deployment orchestration
- Parameter templating with site variables and output chaining
- Parallel deployment with configurable concurrency
- kubectl operations via Arc-connected clusters

Usage:
    siteops -w <workspace> sites
    siteops -w <workspace> validate <manifest>
    siteops -w <workspace> plan <manifest>
    siteops -w <workspace> deploy <manifest>

The package automatically configures Azure CLI User-Agent tracking
(AZURE_HTTP_USER_AGENT) to include "siteops/{version}" for usage
telemetry in Azure Activity Logs.
"""

__version__ = "1.0.0b1"
__all__ = ["__version__"]
