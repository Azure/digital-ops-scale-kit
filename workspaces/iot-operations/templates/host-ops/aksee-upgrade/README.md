# AKS Edge Essentials upgrade implementation

The [operator guide](../../../manifests/aksee-upgrade/README.md) owns target
configuration, deployment, monitoring, remediation and recovery.

This directory owns the Bicep template, implementation-local partial and
target-delivered scripts. The public manifest supplies the completion wait.
Keep upgrade execution and worker sources beside the template that delivers
them.

Use the [script build workflow](scripts/README.md) to regenerate launchers
after changing their sources. Host-worker completion, platform readiness and
AIO workload health remain different outcomes.
