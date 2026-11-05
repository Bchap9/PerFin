// SPDX-License-Identifier: AGPL-3.0-or-later
/** Typed calls for the notification channels page (`/settings/notifications/channels`). */
import { api, unwrap } from "../../../lib/api/client";
import type { components } from "../../../lib/api/schema";
import { queryRoots } from "../../../lib/queryKeys";

type Schemas = components["schemas"];

export type Channel = Schemas["ChannelOut"];
export type ChannelSaved = Schemas["ChannelSavedOut"];
export type ChannelCreate = Schemas["ChannelCreate"];
export type ChannelUpdate = Schemas["ChannelUpdate"];
export type ChannelTest = Schemas["ChannelTestOut"];
export type ChannelType = Schemas["ChannelType"];
export type Outcome = ChannelTest["outcome"];
export type Kind = Channel["events"][number];

export const channelsKey = [...queryRoots.settings, "notification-channels"] as const;

export function listChannels(): Promise<Channel[]> {
  return unwrap(api.GET("/api/v1/settings/notifications/channels"));
}

export function createChannel(body: ChannelCreate): Promise<ChannelSaved> {
  return unwrap(api.POST("/api/v1/settings/notifications/channels", { body }));
}

export function updateChannel(channelId: string, body: ChannelUpdate): Promise<ChannelSaved> {
  return unwrap(
    api.PATCH("/api/v1/settings/notifications/channels/{channel_id}", {
      params: { path: { channel_id: channelId } },
      body,
    }),
  );
}

export async function deleteChannel(channelId: string): Promise<void> {
  await unwrap(
    api.DELETE("/api/v1/settings/notifications/channels/{channel_id}", {
      params: { path: { channel_id: channelId } },
    }),
  );
}

export function testChannel(channelId: string): Promise<ChannelTest> {
  return unwrap(
    api.POST("/api/v1/settings/notifications/channels/{channel_id}/test", {
      params: { path: { channel_id: channelId } },
    }),
  );
}
