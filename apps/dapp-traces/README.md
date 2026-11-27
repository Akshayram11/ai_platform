# DataOS Agent Traces

The Agent Builder checkbox saves tracing with the draft and its published version. Playground turns use the checkbox immediately. Republish to apply it to an existing agent app. Rolling back restores the setting on the selected version.

The shared agent runtime reports one trace per turn to the LLM gateway with the app's existing gateway key. The gateway sends OTLP/HTTP traces to MLflow. The dashboard authorizes the viewer before it asks the gateway to read them. **Traces** shows your Playground turns and the published agents you own. Each trace holds the whole run: the question and the answer, the messages sent to the model on each call and its reply (including tool calls), each tool call's arguments and result, MCP server/function names, model and backend, timings, status, errors and token counts (per call and in total). Each input or output is cut at 8,000 characters. Credentials (gateway keys, DataOS tokens) are never recorded. A published agent with tracing on tells its users that conversations are recorded for the owner.

## Deploy in Poliks

Run from the repository root. Use the active DataOS tenant and an image registry the apps can pull from.

1. Build and push the MLflow app image from this directory.

   ```sh
   docker buildx build --platform linux/amd64 --push -t docker.io/tmdcio/dapp-traces:0.1.1 apps/dapp-traces
   ```

2. Create its persistent volume and a shared credential. The gateway and MLflow app both reference the same DataOS secret.

   ```sh
   dataos-ctl resource apply -f deploy/traces-data.volume.yaml
   umask 077
   trace_env="$(mktemp)"
   python3 -c 'import secrets; print("DAPP_TRACE_PASSWORD=" + secrets.token_urlsafe(48))' > "$trace_env"
   dapp secret set dapp-traces dapp-traces-auth --from-env-file "$trace_env" --no-attach
   rm -f "$trace_env"
   dapp deploy -f apps/dapp-traces/dapp.yaml
   ```

3. Build and push the shared dashboard/gateway image under a new tag. Set that tag in `deploy/dashboard.dapp.yaml` and `deploy/gateway.service.yaml`. Apply the gateway Service and deploy the dashboard.

   ```sh
   dataos-ctl resource apply -f deploy/gateway.service.yaml
   dapp deploy -f deploy/dashboard.dapp.yaml
   ```

4. Tick **Enable tracing** and send a Playground message. Open **View trace**. Publish and send another message in the published app. Open **Traces** and select the published agent. Repeat with a DataOS-hosted model to verify local gateway routing. The MLflow endpoint uses HTTP Basic authentication with username `dapp-traces` and the shared credential. DataOS users should use the dashboard's scoped Traces view.

This pilot uses one SQLite instance on a persistent volume. Back up the volume and monitor disk usage. Manage retention in MLflow before increasing trace volume.
