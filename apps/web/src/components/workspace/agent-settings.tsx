"use client";

import { useEffect, useState } from "react";
import { Check, KeyRound, Plug, Save } from "lucide-react";
import { api, type Agent, type ModelApiFormat } from "@/lib/api";
import { useT } from "@/lib/i18n/LocaleProvider";
import { ErrorNotice, Modal } from "./ui";

export function AgentSettings({ connectorEndpoint: connectorUrl, agents, error, onRefresh, onClose, onChoose, onManage, selectedAgentId, initialDisconnect = false, mode = "settings" }: { connectorEndpoint: string; agents: Agent[]; error: unknown; onRefresh: () => void; onClose: () => void; onChoose: (id: string) => void; onManage: (id: string) => void; selectedAgentId?: string | null; initialDisconnect?: boolean; mode?: "settings" | "external" | "kanaloa" }) {
  const t = useT();
  const [copiedValue, setCopiedValue] = useState<"code" | "config" | "guide" | "mcp" | null>(null);
  const [copyError, setCopyError] = useState(false);
  const [activating, setActivating] = useState(false);
  const [activationError, setActivationError] = useState<unknown>(null);
  const [savingPreference, setSavingPreference] = useState(false);
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiFormat, setApiFormat] = useState<ModelApiFormat>("openai-completions");
  const [apiKey, setApiKey] = useState("");
  const [savingApiKey, setSavingApiKey] = useState(false);
  const [apiKeySaved, setApiKeySaved] = useState(false);
  const selectedAgent = agents.find(agent => agent.agent_id === selectedAgentId);
  const managingExternal = mode === "settings" && Boolean(selectedAgent && selectedAgent.agent_id !== "kanaloa");
  const [agentId, setAgentId] = useState(selectedAgentId || "");
  const [displayName, setDisplayName] = useState(selectedAgent?.display_name || selectedAgent?.agent_id || "");
  const [managementBusy, setManagementBusy] = useState(false);
  const [confirmation, setConfirmation] = useState<"delete" | "disconnect" | null>(initialDisconnect ? "disconnect" : null);
  const [nameSaved, setNameSaved] = useState(false);
  const [pairingCode, setPairingCode] = useState("");
  const [pairingEstablished, setPairingEstablished] = useState(false);
  const [pairingError, setPairingError] = useState<unknown>(null);
  const [creatingPairing, setCreatingPairing] = useState(false);
  const [pairingAgentId, setPairingAgentId] = useState("");
  const [pairingDisplayName, setPairingDisplayName] = useState("");
  const [repositoryPath, setRepositoryPath] = useState("");
  const [loopbackEndpointOnRemoteUi, setLoopbackEndpointOnRemoteUi] = useState(false);
  const pairingOnline = !error && agents.some(agent => agent.agent_id === pairingAgentId && agent.status === "ready");
  const validAgentId = /^[A-Za-z0-9_.-]{1,80}$/.test(agentId.trim());
  const kanaloa = agents.find(agent => agent.agent_id === "kanaloa");
  useEffect(() => { if (pairingOnline) setPairingEstablished(true); }, [pairingOnline]);
  const connectionConfig = `Agent ID: ${pairingAgentId}\nDisplay name: ${pairingDisplayName}\nConnector WebSocket: ${connectorUrl}`;
  const apiEndpoint = new URL(connectorUrl);
  apiEndpoint.protocol = apiEndpoint.protocol === "wss:" ? "https:" : "http:";
  apiEndpoint.pathname = "/";
  const mcpConfig = JSON.stringify({ mcpServers: { kane: { command: "python", args: [`${repositoryPath.trim().replace(/[\\/]+$/, "")}/connectors/kane-mcp/server.py`], env: { KANE_API_BASE_URL: apiEndpoint.origin, KANE_CONNECTOR_WS_URL: connectorUrl, KANE_AGENT_ID: pairingAgentId } } } }, null, 2);
  const connectGuide = `${connectionConfig}\nRead skills/kane-connect/SKILL.md and docs/KANE_CONNECTOR_PROTOCOL.md from the Kane repository. With Kane MCP, call get_connection_guide, get_connector_spec and get_connection_info. Configure an existing compatible Agent-side Connector, or implement one using the canonical protocol. Transfer the one-time code separately through protected input. Keep the Connector running; validate a redacted trace with run_conformance_check. MCP is bootstrap, not chat transport. Do not modify Kane Core or claim unsupported native capabilities.`;

  useEffect(() => {
    const loopback = (host: string) => ["localhost", "127.0.0.1", "[::1]"].includes(host);
    try { setLoopbackEndpointOnRemoteUi(loopback(new URL(connectorUrl).hostname) && !loopback(window.location.hostname)); }
    catch { setLoopbackEndpointOnRemoteUi(false); }
  }, [connectorUrl]);

  async function activateKanaloa() {
    setActivating(true);
    setActivationError(null);
    try { await api.activateKanaloa(); onRefresh(); }
    catch (failure) { setActivationError(failure); }
    finally { setActivating(false); }
  }
  async function setAutoStart(enabled: boolean) {
    setSavingPreference(true);
    setActivationError(null);
    try { await api.setKanaloaAutoStart(enabled); onRefresh(); }
    catch (failure) { setActivationError(failure); }
    finally { setSavingPreference(false); }
  }
  async function deactivateKanaloa() {
    setActivating(true); setActivationError(null);
    try { await api.deactivateKanaloa(); onRefresh(); }
    catch (failure) { setActivationError(failure); }
    finally { setActivating(false); }
  }
  async function saveApiKey() {
    setSavingApiKey(true);
    setActivationError(null);
    setApiKeySaved(false);
    try { await api.saveKanaloaModelConfig({ base_url: baseUrl.trim(), model: model.trim(), api_key: apiKey, ...(apiFormat !== "openai-completions" ? { api_format: apiFormat } : {}) }); setApiKey(""); setApiKeySaved(true); onRefresh(); }
    catch (failure) { setActivationError(failure); }
    finally { setSavingApiKey(false); }
  }
  async function createPairing() {
    setCreatingPairing(true);
    setPairingError(null);
    setPairingCode("");
    setPairingEstablished(false);
    try {
      const result = await api.createAgentPairing(agentId.trim(), displayName.trim());
      setPairingCode(result.pairing_code);
      setPairingAgentId(result.agent_id);
      setPairingDisplayName(displayName.trim());
      setCopiedValue(null);
      onRefresh();
    } catch (failure) { setPairingError(failure); }
    finally { setCreatingPairing(false); }
  }
  async function manageAgent(operation: "rename" | "delete" | "disconnect") {
    setManagementBusy(true); setPairingError(null); setNameSaved(false);
    try {
      if (operation === "rename") { await api.renameAgent(agentId, displayName.trim()); setNameSaved(true); }
      else if (operation === "disconnect") { await api.disconnectAgent(agentId); setPairingCode(""); }
      else { await api.deleteAgent(agentId); onClose(); }
      setConfirmation(null); onRefresh();
    } catch (error) { setPairingError(error); }
    finally { setManagementBusy(false); }
  }
  return <Modal title={t(mode === "external" ? "connectAgent" : mode === "kanaloa" ? "kanaloaConfig" : "agentSettings")} onClose={onClose}>
    {mode === "settings" && <div className="agent-setup">
      <section><h3>{t("registeredAgents")}</h3>{Boolean(error) && <ErrorNotice error={error} onRetry={onRefresh} />}
        {!agents.length && <p>{t("noAgents")}</p>}
        {agents.filter(agent => !selectedAgentId || agent.agent_id === selectedAgentId).map(agent => <div className="setup-agent" key={agent.agent_id}><div><strong>{agent.agent_id === "kanaloa" ? "Kanaloa" : agent.display_name || agent.agent_id}</strong><p>{t(agent.status === "ready" ? "agentReady" : agent.status === "idle" ? "agentIdle" : agent.status === "unavailable" ? "agentUnavailable" : agent.status)}</p></div><div className="setup-agent-actions"><button className="button secondary compact" onClick={() => onManage(agent.agent_id)}>{t("agentSettings")}</button>{agent.agent_id === "kanaloa" && agent.status !== "ready" && <button className="button primary compact" disabled={activating || agent.status === "unavailable"} onClick={() => void activateKanaloa()}>{t(activating ? "loading" : "activateKanaloa")}</button>}<button className="button secondary compact" disabled={Boolean(error) || agent.status === "unavailable"} onClick={() => onChoose(agent.agent_id)}>{t("newConversation")}</button></div></div>)}
        {Boolean(activationError) && <ErrorNotice error={activationError} />}
        <p>{t("agentReadinessNote")}</p><button className="text-button" onClick={onRefresh}>{t("refresh")}</button>
      </section>
    </div>}
    {mode === "kanaloa" && <div className="agent-setup"><section><div className="settings-section-heading"><KeyRound size={19} /><h3>{t("providerSetup")}</h3></div>
        {error ? <ErrorNotice error={error} onRetry={onRefresh} /> : !kanaloa ? <p role="status">{t("kanaloaNotRegistered")}</p> : <>
          <p>{t(kanaloa.status === "unavailable" ? "agentUnavailable" : kanaloa.status === "idle" ? "agentIdle" : "agentReady")}</p>
          <button className="button secondary compact" disabled={activating || kanaloa.status === "unavailable"} onClick={() => void (kanaloa.status === "ready" ? deactivateKanaloa() : activateKanaloa())}>{t(activating ? "loading" : kanaloa.status === "ready" ? "disconnectAgent" : "activateKanaloa")}</button>
          <label className="check-label"><input type="checkbox" checked={kanaloa.auto_start ?? false} disabled={savingPreference || kanaloa.status === "unavailable"} onChange={event => void setAutoStart(event.target.checked)} />{t("kanaloaAutoStart")}</label>
        </>}
        {Boolean(activationError) && <ErrorNotice error={activationError} />}
        <form onSubmit={event => { event.preventDefault(); void saveApiKey(); }}>
        <label className="form-field">{t("apiFormat")}<select aria-label={t("apiFormat")} disabled={savingApiKey} value={apiFormat} onChange={event => { setApiFormat(event.target.value as ModelApiFormat); setApiKeySaved(false); }}><option value="openai-completions">OpenAI Chat Completions</option><option value="openai-responses">OpenAI Responses</option><option value="anthropic-messages">Anthropic Messages</option></select></label>
        <label className="form-field">{t("kanaloaBaseUrl")}<input disabled={savingApiKey} required type="url" maxLength={2048} value={baseUrl} onChange={event => { setBaseUrl(event.target.value); setApiKeySaved(false); }} placeholder="https://api.example.com/v1" autoComplete="url" /></label>
        <label className="form-field">{t("kanaloaModel")}<input disabled={savingApiKey} required maxLength={256} value={model} onChange={event => { setModel(event.target.value); setApiKeySaved(false); }} placeholder="model-name" autoComplete="off" /></label>
        <label className="form-field">{t("kanaloaApiKeyLabel")}<input disabled={savingApiKey} required type="password" maxLength={4096} value={apiKey} onChange={event => { setApiKey(event.target.value); setApiKeySaved(false); }} autoComplete="new-password" spellCheck={false} /></label>
        <button type="submit" className="button primary compact" disabled={savingApiKey || Boolean(error) || !kanaloa || !baseUrl.trim() || !model.trim() || !apiKey.trim() || kanaloa.status === "unavailable"}><Save size={15} />{t(savingApiKey ? "loading" : "saveKanaloaApiKey")}</button>
        </form>
        {apiKeySaved && <p className="save-success" role="status"><Check size={16} />{t("kanaloaApiKeySaved")}</p>}
        <p className="config-note">{t("modelConfigSessionNote")}</p>
      </section></div>}
    {(mode === "external" || managingExternal) && <section className="agent-pairing">
      <div className="pairing-fields">
        <label className="form-field">{t("agentIdLabel")}<input readOnly={managingExternal} value={agentId} onChange={event => setAgentId(event.target.value)} autoComplete="off" maxLength={80} pattern="[A-Za-z0-9_.-]+" aria-label={t("agentIdLabel")} aria-describedby="agent-id-hint" required /><small id="agent-id-hint">{t(managingExternal ? "agentIdentityFixed" : "agentIdHint")}</small></label>
        <label className="form-field">{t("agentNameLabel")}<input value={displayName} onChange={event => setDisplayName(event.target.value)} autoComplete="off" maxLength={120} required /></label>
      </div>
      {managingExternal && <div className="pairing-actions"><button className="button secondary compact" disabled={managementBusy || !displayName.trim()} onClick={() => void manageAgent("rename")}>{t("saveDisplayName")}</button><button className="button secondary compact" disabled={managementBusy} onClick={() => setConfirmation("disconnect")}>{t("disconnectAgent")}</button><button className="button danger compact" disabled={managementBusy} onClick={() => setConfirmation("delete")}>{t("deleteAgent")}</button></div>}
      {nameSaved && <p role="status">{t("agentNameSaved")}</p>}
      {confirmation && <div className="setup-boundary"><p>{t(confirmation === "delete" ? "deleteAgentHint" : "disconnectAgentHint")}</p><div className="pairing-actions"><button className="button secondary compact" disabled={managementBusy} onClick={() => setConfirmation(null)}>{t("cancel")}</button><button className="button danger compact" disabled={managementBusy} onClick={() => void manageAgent(confirmation)}>{t(confirmation === "delete" ? "deleteAgent" : "disconnectAgent")}</button></div></div>}
      <button className="button primary compact" disabled={creatingPairing || managementBusy || !validAgentId || !displayName.trim() || (managingExternal && selectedAgent?.status === "ready")} onClick={() => void createPairing()}>{t(creatingPairing ? "loading" : managingExternal ? "repairAgent" : "createPairing")}</button>
      {Boolean(pairingError) && <ErrorNotice error={pairingError} />}
      {pairingCode && <div className={`pairing-result ${pairingOnline ? "connected" : "waiting"}`} role="status">
        <strong className="pairing-status"><Plug size={16} />{t(pairingOnline ? "pairingOnline" : pairingEstablished ? "disconnected" : "pairingWaiting")}</strong>
        <div className="pairing-fact"><span>{t("agentIdLabel")}</span><code>{pairingAgentId}</code></div>
        {!pairingOnline && !pairingEstablished && <><code className="pairing-code">{pairingCode}</code><p>{t("pairingOneUse")}</p><div className="pairing-actions"><button className="button secondary compact" onClick={async () => { try { await navigator.clipboard.writeText(pairingCode); setCopiedValue("code"); setCopyError(false); } catch { setCopyError(true); } }}>{t(copiedValue === "code" ? "pairingCopied" : "pairingCopy")}</button><button className="button secondary compact" onClick={async () => { try { await navigator.clipboard.writeText(connectionConfig); setCopiedValue("config"); setCopyError(false); } catch { setCopyError(true); } }}>{t(copiedValue === "config" ? "copied" : "copyConnectionConfig")}</button></div>{copyError && <p role="alert">{t("copyFailed")}</p>}<div className="pairing-fact"><span>{t("connectorEndpoint")}</span><code>{connectorUrl}</code></div></>}
        {pairingEstablished && !pairingOnline && <p>{t("pairingDisconnected")}</p>}
        {pairingOnline && <p>{t("pairingConnectedDetail")}</p>}
      </div>}
      <details className="connection-instructions"><summary>{t("connectionInstructions")}</summary><p>{t("connectorNeedsRuntime")}</p>
      <ol className="connector-steps"><li>{t("connectorStepOne")}</li><li>{t("connectorStepTwo")}</li><li>{t("connectorStepThree")}</li></ol>
      <p className="setup-boundary">{t("connectorReferenceFiles")}</p></details>
      {pairingCode && <div className="connector-bootstrap"><h3>{t("mcpBootstrap")}</h3><button className="button secondary compact" onClick={async () => { try { await navigator.clipboard.writeText(connectGuide); setCopiedValue("guide"); setCopyError(false); } catch { setCopyError(true); } }}>{t(copiedValue === "guide" ? "copied" : "copyConnectGuide")}</button><label className="form-field">{t("connectorRepository")}<input value={repositoryPath} onChange={event => setRepositoryPath(event.target.value)} autoComplete="off" /></label>{repositoryPath.trim() && <><pre className="setup-command">{mcpConfig}</pre><button className="button secondary compact" onClick={async () => { try { await navigator.clipboard.writeText(mcpConfig); setCopiedValue("mcp"); setCopyError(false); } catch { setCopyError(true); } }}>{t(copiedValue === "mcp" ? "copied" : "copyMcpConfig")}</button></>}{copyError && <p role="alert">{t("copyFailed")}</p>}</div>}
      {loopbackEndpointOnRemoteUi && <p className="setup-boundary" role="alert">{t("connectorLoopbackWarning")}</p>}
    </section>}
  </Modal>;
}
