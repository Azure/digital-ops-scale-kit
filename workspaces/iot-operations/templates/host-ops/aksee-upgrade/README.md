# AKS Edge Essentials upgrade implementation

The [operator guide](../../../manifests/aksee-upgrade/README.md) owns Site
configuration, deployment, monitoring, remediation and recovery.

This directory owns the Bicep template, the partial local to this
implementation and the scripts delivered to the host. The public manifest supplies the completion wait.
Keep upgrade execution and worker sources beside the template that delivers
them.

Use the [script build workflow](scripts/README.md) to regenerate launchers
after changing their sources. Completion of the host worker, platform readiness and
AIO workload health remain different outcomes.
