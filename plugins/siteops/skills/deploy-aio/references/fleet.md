# Install AIO on several clusters

Read this file when the user wants AIO on more than one cluster. It replaces
steps 6 and 7 of the deploy-aio skill. Steps 1 to 5 still apply to each
cluster, including the check for an existing AIO instance, and every rule in
"Stay on this path" still holds.

Site Ops saves one Site per cluster in a project directory, then plans and
deploys the selected Sites together.

## 1. Agree on the project and names

Agree with the user on a project directory, such as `./factory`, and a Site
name for each cluster, such as the cluster name. Site names use lowercase
letters, digits and hyphens. Ask before creating files, then create the
project's `sites` directory if it does not exist.

Confirm that each cluster is in its own resource group. AIO supports one
instance per resource group.

## 2. Save one Site per cluster

The cluster read fills in each Site's subscription, resource group and
region. Give every Site the same optional inputs:

```text
siteops --project "<project>" inputs aio-install --source "<source>@<release>" --input "cluster=<cluster-resource-ID>" --input siteName=<site-name> --read-resources --save-site "<project>/sites/<site-name>.yaml"
```

Site Ops never overwrites a file or reuses the name of a configured Site. If
it refuses, ask the user rather than choosing another name.

## 3. Plan only the new Sites

Select the Sites by name:

```text
siteops --project "<project>" plan aio-install --source "<source>@<release>" -l name=<site-name>,name=<other-site-name>
```

The manifest deploys three Sites at once by default. For more, add
`--parallel` with the Site count to both plan and deploy. Do not select by a
shared label such as `environment=dev`. It can include clusters that already
run AIO, and deploying `aio-install` again can overwrite their settings.

Continue only when the plan prints `Executable: yes`. Summarize the plan
with one line for each Site (name, resource group, region, AIO release),
then the plan's Operations line. End your reply with this line, in plain
text: "Reply deploy to run this plan." Do not ask through a question tool
or form. Then end your turn. Do not deploy in this turn.

## 4. Deploy after confirmation

When the user's next message agrees to this plan, such as `deploy` or
`yes`:

```text
siteops --project "<project>" deploy aio-install --source "<source>@<release>" -l name=<site-name>,name=<other-site-name> --yes
```

Report the result for each Site, then continue with step 8 of the deploy-aio
skill for each cluster. The saved Sites remain in the project, so a later
upgrade can reuse them with the upgrade-aio skill.
