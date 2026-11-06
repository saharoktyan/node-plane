/** Each service type's screens. A built-in type (VPN) has its own module;
 * every other type — services from manifests — uses the generic one, which
 * draws what the type declares, so a new service needs no cabinet code.
 * Panels load on demand: a member never downloads an administrator's. */
import type { Component } from 'svelte';
import type { Schemas } from '$lib/api/client';
import { t } from '$lib/i18n/index.svelte';

export type ServiceProfile = Schemas['ServiceProfileOutput'];
export type Service = Schemas['ServiceOutput'];
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Panel<Props extends Record<string, any>> = () => Promise<{ default: Component<Props> }>;

export interface ServiceModule {
  /** What the holder of a service profile sees and does. */
  member: Panel<{ profile: ServiceProfile }>;
  /** An instance on a node in the administration; stage I fills it in for
   * services from manifests (settings form, lifecycle, actions). */
  instance?: Panel<{ instanceId: string }>;
  /** The profile on an account's card in the administration. */
  profile?: Panel<{ profileId: string }>;
}

const modules: Record<string, ServiceModule> = {
  vpn: { member: () => import('./vpn/Member.svelte') }
};

export const generic: ServiceModule = { member: () => import('./generic/Member.svelte') };

export function moduleOf(service: string): ServiceModule {
  return modules[service] ?? generic;
}

/** A service's name: from the catalog for known types, else its id. */
export function serviceName(service: string): string {
  const key = 'services.name_' + service;
  const name = t(key);
  return name === key ? service : name;
}
