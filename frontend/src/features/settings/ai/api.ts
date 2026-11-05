// SPDX-License-Identifier: AGPL-3.0-or-later
/** Typed calls of the AI access page (docs/API.md, "AI settings"). */
import { api, unwrap } from "../../../lib/api/client";
import type { components } from "../../../lib/api/schema";
import { queryRoots } from "../../../lib/queryKeys";

type Schemas = components["schemas"];

export type AiClient = Schemas["AiClientOut"];
export type AiClientCreated = Schemas["AiClientCreatedOut"];
export type AiClientCreate = Schemas["AiClientCreate"];
export type AiOptions = Schemas["AiOptionsIn"];
export type AiAccess = Schemas["AiAccessOut"];
export type AiMode = Schemas["AiMode"];
export type AiTransport = Schemas["AiTransport"];
export type AiCall = Schemas["AiCallOut"];
export type ExposurePreview = Schemas["ExposurePreviewOut"];
export type PreviewTool = Schemas["PreviewToolOut"];
export type ScrubberPreview = Schemas["ScrubberPreviewOut"];
export type ScrubberPreviewIn = Schemas["ScrubberPreviewIn"];

export const aiKeys = {
  access: [...queryRoots.settings, "ai", "access"],
  system: [...queryRoots.settings, "ai", "system"],
  clients: [...queryRoots.settings, "ai", "clients"],
  calls: (clientId: string, contains: string) => [...queryRoots.settings, "ai", "calls", clientId, contains],
} as const;

export function getAccess(): Promise<AiAccess> {
  return unwrap(api.GET("/api/v1/settings/ai"));
}

export function setHouseholdEnabled(enabled: boolean): Promise<AiAccess> {
  return unwrap(api.PATCH("/api/v1/settings/ai", { body: { enabled } }));
}

export function setSystemEnabled(enabled: boolean): Promise<Schemas["SystemAiOut"]> {
  return unwrap(api.PATCH("/api/v1/settings/system/ai", { body: { enabled } }));
}

export function listClients(): Promise<AiClient[]> {
  return unwrap(api.GET("/api/v1/settings/ai/clients"));
}

export function createClient(body: AiClientCreate): Promise<AiClientCreated> {
  return unwrap(api.POST("/api/v1/settings/ai/clients", { body }));
}

export async function revokeClient(clientId: string): Promise<void> {
  await unwrap(api.DELETE("/api/v1/settings/ai/clients/{client_id}", { params: { path: { client_id: clientId } } }));
}

export function previewOptions(body: AiOptions): Promise<ExposurePreview> {
  return unwrap(api.POST("/api/v1/settings/ai/preview", { body }));
}

export function previewClient(clientId: string): Promise<ExposurePreview> {
  return unwrap(
    api.GET("/api/v1/settings/ai/clients/{client_id}/preview", { params: { path: { client_id: clientId } } }),
  );
}

export async function listCalls(clientId: string, contains: string): Promise<AiCall[]> {
  const result = await unwrap(
    api.GET("/api/v1/settings/ai/calls", {
      params: {
        query: {
          limit: 200,
          ...(clientId === "" ? {} : { client_id: clientId }),
          ...(contains === "" ? {} : { contains }),
        },
      },
    }),
  );
  return result.items;
}

export function previewScrubber(body: ScrubberPreviewIn): Promise<ScrubberPreview> {
  return unwrap(api.POST("/api/v1/settings/ai/scrubber-preview", { body }));
}
