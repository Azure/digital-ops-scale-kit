# AKS Edge Essentials bootstrap implementation

The [operator guide](../../../manifests/aksee-bootstrap/README.md) owns target
configuration, deployment, monitoring, recovery and the bootstrap state
contract.

This directory owns the Bicep template, implementation-local partial and
target-delivered scripts. The public manifest adds the worker-completion wait
before another operation can use the cluster.

Edit the launcher and worker sources, then use the
[script build workflow](scripts/README.md) to regenerate delivered launchers.
Keep the template, partial and script sources together. Generated launchers
are build outputs, not independent authoring sources.
