<!-- The profile's devices, as the bot keeps them: add one under the limit,
     rename it, give it a new VLESS or AWG key, remove it. A key change and a
     removal ask once more, since the device's configurations stop working. -->
<script lang="ts">
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';
  import { key } from '$lib/ui/format';

  type Device = Schemas['DeviceOutput'];
  let { profileId, list, onchange }: {
    profileId: string;
    list: Schemas['DeviceList'];
    onchange: () => Promise<void>;
  } = $props();

  const items = $derived(list.items.filter((item) => item.status !== 'retired')
    .sort((a, b) => a.display_name.localeCompare(b.display_name) || a.id.localeCompare(b.id)));
  const active = $derived(items.filter((item) => item.status === 'active').length);
  const room = $derived(list.device_limit == null || active < list.device_limit);

  let name = $state('');
  let renaming = $state<{ id: string; name: string } | null>(null);
  // An action waiting for its confirmation, with the key it is sent with.
  let confirming = $state<{ id: string; action: 'rotate_xray' | 'rotate_awg' | 'delete'; key: string } | null>(null);
  let busy = $state(false);
  let notice = $state('');

  async function run(action: () => Promise<unknown>) {
    busy = true;
    notice = '';
    try {
      await action();
      await onchange();
      return true;
    } catch (error) {
      notice = error instanceof ApiError ? errorText(error) : t('common.error');
      return false;
    } finally {
      busy = false;
    }
  }

  // A change names the revision it was made on and a key that makes a retry safe.
  const match = (revision: number) => ({ 'If-Match': `"${revision}"` });

  async function add(event: SubmitEvent) {
    event.preventDefault();
    if (!name.trim()) return;
    const added = await run(async () => {
      const profile = await call(api.GET('/api/v1/profiles/{profile_id}', { params: { path: { profile_id: profileId } } }));
      await call(api.POST('/api/v1/profiles/{profile_id}/devices', {
        params: { path: { profile_id: profileId }, header: { 'Idempotency-Key': key() } },
        headers: match(profile.desired_revision), body: { display_name: name.trim() } }));
    });
    if (added) name = '';
  }

  async function rename(device: Device, event: SubmitEvent) {
    event.preventDefault();
    if (!renaming?.name.trim()) return;
    const value = renaming.name.trim();
    if (await run(() => call(api.PATCH('/api/v1/profiles/{profile_id}/devices/{device_id}', {
      params: { path: { profile_id: profileId, device_id: device.id }, header: { 'Idempotency-Key': key() } },
      headers: match(device.revision),
      body: { display_name: value } })))) renaming = null;
  }

  async function confirm(device: Device) {
    if (!confirming) return;
    const action = confirming.action;
    const headers = match(device.revision);
    // The key chosen with the confirmation: pressing again repeats the same command.
    const params = { path: { profile_id: profileId, device_id: device.id }, header: { 'Idempotency-Key': confirming.key } };
    const done = await run(() => action === 'delete'
      ? call(api.DELETE('/api/v1/profiles/{profile_id}/devices/{device_id}', { params, headers }))
      : call(api.POST('/api/v1/profiles/{profile_id}/devices/{device_id}/rotate', { params, headers,
        body: { protocol: action === 'rotate_awg' ? 'awg' : 'xray' } })));
    if (done) confirming = null;
  }

  // A device being removed leaves the list once the nodes let it go.
  $effect(() => {
    if (!items.some((item) => item.status === 'deleting')) return;
    const timer = setTimeout(() => onchange(), 3000);
    return () => clearTimeout(timer);
  });
</script>

<section class="block">
  <h2>{t('vpn.devices')}
    {#if list.device_limit != null}<span class="muted count">{t('vpn.devices_of', { count: active, limit: list.device_limit })}</span>{/if}
  </h2>
  <p class="muted">{items.length ? t('vpn.devices_hint') : t('vpn.devices_empty')}</p>
  <div class="rows">
    {#each items as device (device.id)}
      <div>
        {#if renaming?.id === device.id}
          <form class="row grow" onsubmit={(event) => rename(device, event)}>
            <input class="name" maxlength="256" aria-label={t('vpn.device_name')} bind:value={renaming.name} />
            <button class="primary" disabled={busy}>{t('vpn.save')}</button>
            <button type="button" onclick={() => (renaming = null)}>{t('vpn.cancel')}</button>
          </form>
        {:else}
          <div class="grow">
            <strong>{device.display_name}</strong>
            {#if device.status === 'deleting'}<span class="muted"> · {t('vpn.deleting')}</span>{/if}
            {#if device.status === 'active' && device.networks}
              <div class="muted small">{t('vpn.networks', device.networks)}</div>
            {/if}
          </div>
          {#if device.status === 'active'}
            <div class="row">
              <button onclick={() => { renaming = { id: device.id, name: device.display_name }; confirming = null; }}>
                {t('vpn.rename')}</button>
              <button onclick={() => (confirming = { id: device.id, action: 'rotate_xray', key: key() })}>{t('vpn.rotate_xray')}</button>
              <button onclick={() => (confirming = { id: device.id, action: 'rotate_awg', key: key() })}>{t('vpn.rotate_awg')}</button>
              <button class="danger" onclick={() => (confirming = { id: device.id, action: 'delete', key: key() })}>
                {t('vpn.delete')}</button>
            </div>
          {/if}
        {/if}
        {#if confirming?.id === device.id}
          <div class="confirm">
            <p>{t('vpn.' + confirming.action + '_warning')}</p>
            <div class="row">
              <button class={confirming.action === 'delete' ? 'danger' : 'primary'} disabled={busy}
                onclick={() => confirm(device)}>{t('vpn.' + confirming.action)}</button>
              <button onclick={() => (confirming = null)}>{t('vpn.cancel')}</button>
            </div>
          </div>
        {/if}
      </div>
    {/each}
  </div>
  {#if room}
    <form class="row add" onsubmit={add}>
      <input class="name" maxlength="256" placeholder={t('vpn.device_name')} aria-label={t('vpn.device_name')}
        bind:value={name} />
      <button class="primary" disabled={busy || !name.trim()}>{t('vpn.add_device')}</button>
    </form>
  {:else}
    <p class="muted">{t('vpn.limit_reached')}</p>
  {/if}
  {#if notice}<p class="error">{notice}</p>{/if}
</section>

<style>
  .count {
    font-weight: 400;
    font-size: 0.9rem;
    margin-left: 0.5rem;
  }
  .small {
    font-size: 0.85rem;
  }
  .name {
    flex: 1 1 14rem;
    width: auto;
    max-width: 24rem;
  }
  .add {
    margin-top: 0.9rem;
  }
  .confirm {
    flex-basis: 100%;
    padding: 0.5rem 0.75rem;
    border-left: 3px solid var(--warn);
    background: var(--surface);
  }
  .confirm p {
    margin: 0 0 0.5rem;
  }
</style>
