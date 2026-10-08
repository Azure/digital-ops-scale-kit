# AKS Edge Essentials bootstrap implementation

The [operator guide](../../../manifests/aksee-bootstrap/README.md) owns Site
configuration, deployment, monitoring, recovery and the bootstrap state
contract.

This directory owns the Bicep template, the partial local to this
implementation and the scripts delivered to the host. The public manifest adds
the wait for worker completion
before another operation can use the cluster.

Edit the launcher and worker sources, then use the
[script build workflow](scripts/README.md) to regenerate delivered launchers.
Keep the template, partial and script sources together. Generated launchers
are build outputs, not independent authoring sources.
