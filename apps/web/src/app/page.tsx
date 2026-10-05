import { ConversationWorkspace } from "@/components/workspace/conversation-workspace";
import { getApiBaseUrl } from "@/lib/api";

export const dynamic = "force-dynamic";

export default function HomePage() {
  const endpoint = new URL("/api/v1/connectors/ws", getApiBaseUrl());
  endpoint.protocol = endpoint.protocol === "https:" ? "wss:" : "ws:";
  const connectorEndpoint = process.env.KANE_CONNECTOR_WS_URL || endpoint.toString();
  return <ConversationWorkspace connectorEndpoint={connectorEndpoint} />;
}
