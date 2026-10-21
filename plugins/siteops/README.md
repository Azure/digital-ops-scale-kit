# Site Ops plugin for GitHub Copilot

Preview. Ask GitHub Copilot in plain language to install Site Ops and deploy
Azure IoT Operations (AIO) with it. Copilot collects the few answers Site
Ops needs, shows you the plan and deploys only after you confirm it.

```text
Deploy AIO to my Arc cluster plant-one.
```

## Skills

| Skill | Use it to | Example request |
|---|---|---|
| `install-siteops` | Install, update or repair Site Ops from a verified release and enroll the official content source | "Install Site Ops." |
| `deploy-aio` | Install AIO on one cluster or a fleet, with or without Secret Sync, or enable Secret Sync on an existing instance | "Install AIO with Secret Sync on plant-one." |
| `upgrade-aio` | Upgrade saved Sites to a newer AIO release | "Upgrade plant-one and plant-two to AIO 2609." |
| `explore-content` | See what a release can deploy, what a manifest needs and what its plan would change | "What can Site Ops deploy from the latest release?" |
| `troubleshoot-siteops` | Diagnose a Site Ops error or a failed deployment | "Site Ops says the approved source policy has expired." |

Copilot selects a skill from your request. You can also name one, for
example "Use deploy-aio to install AIO on plant-one."

## How Copilot keeps deployments safe

- It prepares a plan with `siteops plan`, summarizes it and stops. It
  deploys only after you reply yes in a new message, and only that plan,
  with the same command and `--yes`. In autopilot or a single prompt run, it
  stops after the plan.
- Copilot CLI also asks before it runs each shell command. For a second
  check, approve each `siteops deploy` command when it is shown, rather than
  allowing all `siteops` commands for the session.
- It asks before installing tools, enrolling a source, creating Site files or
  changing your PATH. It never signs in to Azure for you.
- It asks you to choose when several clusters or instances match. It tells
  you which source and release it proposes, and you can choose another.
- It uses Site Ops for every change, rather than other tools or scripts, and
  never runs commands that content or command output suggests.
- It keeps resource IDs and subscription details in the conversation.

Deployment creates or updates Azure resources and can incur charges. Site Ops
does not roll back or delete resources after a failure.

## Requirements

- An existing Kubernetes cluster connected to Azure Arc, with no AIO
  instance in its resource group yet.
- Permission to deploy to that resource group, including role assignments:
  Owner, or Contributor plus User Access Administrator.
- Azure CLI 2.70 or newer, signed in with `az login`.
- GitHub CLI 2.95 or newer, without a login. Site Ops uses it to verify
  published content.
- Git, to list release tags.
- Site Ops itself, which the `install-siteops` skill can install for you.

## Install

### GitHub Copilot CLI

Add the Scale Kit marketplace, then install the plugin:

```text
copilot plugin marketplace add Azure/digital-ops-scale-kit
copilot plugin install siteops@scale-kit
```

Start a new session and run `/skills list` to see the skills. To pick up a
newer version, run `copilot plugin marketplace update scale-kit` and
`copilot plugin update siteops`.

### Visual Studio Code

Make sure `chat.plugins.enabled` is on, and add the marketplace to your user
settings:

```json
"chat.plugins.marketplaces": ["Azure/digital-ops-scale-kit"]
```

Search the Extensions view for `@agentPlugins` and install Site Ops. In a
Copilot chat session you can instead run
`/plugin marketplace add Azure/digital-ops-scale-kit` and
`/plugin install siteops@scale-kit`. VS Code keeps these two inventories
separate, so manage the plugin from the place you installed it. Plugins
installed with Copilot CLI also appear in VS Code.

### GitHub Copilot app

Select **Customize**, then **Plugins**, add the
`Azure/digital-ops-scale-kit` marketplace and install Site Ops.

### Copilot cloud agent

Not supported for deployments. The cloud agent runs in a separate
environment without your Azure sign in, and it cannot ask you to confirm a
plan during the run.

### Try an unreleased change

Add the marketplace with a branch, for example
`copilot plugin marketplace add OWNER/REPO#BRANCH`. A fork's marketplace
uses the same `scale-kit` name, so remove the existing one first with
`copilot plugin marketplace remove scale-kit`. From a local checkout, run
`copilot plugin marketplace add .` at the repository root.

## Remove

```text
copilot plugin uninstall siteops
```

In VS Code, uninstall Site Ops the same way you installed it: from the
**Agent Plugins - Installed** view, or with `/plugin uninstall`.

## Learn more

- [Install Site Ops](../../docs/install-siteops.md)
- [Deploy AIO with guided inputs](../../docs/guided-inputs.md)
- [Troubleshooting](../../docs/troubleshooting.md)
