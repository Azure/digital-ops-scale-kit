# aio-with-opc-ua

Composed example that installs the AIO platform and the OPC UA sample in
one deploy. Useful as a single-command starting point on a fresh cluster.

The manifest composes existing partials, so there is no template or input
file under this directory. See `manifest.yaml` for the post-flatten step
sequence and `samples/README.md` for the rules every composition follows.

## Deploy

```bash
siteops -w workspaces/iot-operations deploy samples/aio-with-opc-ua/manifest.yaml -l environment=dev
```

See `../README.md` (samples authoring guide) for the composition pattern and the conventions every sample follows.
