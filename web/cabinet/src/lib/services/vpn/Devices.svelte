<!-- The profile's devices, as the bot keeps them: add one under the limit,
     rename it, give it a new VLESS or AWG key, remove it. A key change and a
     removal ask once more, since the device's configurations stop working.
     Staff also set a device's own limits here. -->
<script lang="ts">
  import Policies from '$lib/admin/Policies.svelte';
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';
  import { key } from '$lib/ui/format';
  import Dialog from '$lib/ui/Dialog.svelte';
  import Menu from '$lib/ui/Menu.svelte';

  type Device = Schemas['DeviceOutput'];
  let { profileId, list, onchange, limits = false }: {
    profileId: string;
    list: Schemas['DeviceList'];
    onchange: () => Promise<void>;
    limits?: boolean;
  } = $props();
  // The device whose limits are open.
  let limited = $state<Device | null>(null);
  let limitsOpen = $state(false);

  const items = $derived(list.items.filter((item) => item.status !== 'retired')
    .sort((a, b) => a.display_name.localeCompare(b.display_name) || a.id.localeCompare(b.id)));
  const active = $derived(items.filter((item) => item.status === 'active').length);
  const room = $derived(list.device_limit == null || active < list.device_limit);

  let name = $state('');
  let adding = $state(false);
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
    if (added) {
      name = '';
      adding = false;
    }
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
  <div class="block-head">
    <h2>{t('vpn.devices')}
      {#if list.device_limit != null}<small>{t('vpn.devices_of', { count: active, limit: list.device_limit })}</small>{/if}
    </h2>
    {#if room && !adding}<button class="primary" onclick={() => (adding = true)}>{t('vpn.add_short')}</button>{/if}
  </div>
  {#if adding && room}
    <form class="row add" onsubmit={add}>
      <!-- svelte-ignore a11y_autofocus -->
      <input class="name" maxlength="256" placeholder={t('vpn.device_name')} aria-label={t('vpn.device_name')}
        bind:value={name} autofocus />
      <button class="primary" disabled={busy || !name.trim()}>{t('vpn.add_device')}</button>
      <button type="button" onclick={() => { adding = false; name = ''; }}>{t('vpn.cancel')}</button>
    </form>
  {/if}
  {#if !items.length}<p class="muted">{t('vpn.devices_empty')}</p>{/if}
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
          <div class="grow line">
            <strong class="title">{device.display_name}</strong>
            {#if device.status === 'deleting'}<span class="muted small">{t('vpn.deleting')}</span>
            {:else if device.networks}<span class="muted small">{t('vpn.networks', device.networks)}</span>{/if}
          </div>
          {#if device.status === 'active'}
            <Menu label={t('vpn.device_actions', { name: device.display_name })} items={[
              { label: t('vpn.rename'), select: () => { renaming = { id: device.id, name: device.display_name }; confirming = null; } },
              { label: t('vpn.rotate_xray'), select: () => (confirming = { id: device.id, action: 'rotate_xray', key: key() }) },
              { label: t('vpn.rotate_awg'), select: () => (confirming = { id: device.id, action: 'rotate_awg', key: key() }) },
              ...(limits ? [{ label: t('policies.title'), select: () => { limited = device; limitsOpen = true; } }] : []),
              { label: t('vpn.delete'), danger: true,
                select: () => (confirming = { id: device.id, action: 'delete', key: key() }) }
            ]} />
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
  {#if items.length}<p class="muted small hint">{room ? t('vpn.devices_hint') : t('vpn.limit_reached')}</p>{/if}
  {#if notice}<p class="error">{notice}</p>{/if}
</section>
{#if limited}
  <Dialog bind:open={limitsOpen} title={t('policies.device_title', { name: limited.display_name })}>
    {#key limited.id}<Policies subject={{ kind: 'device', profileId, deviceId: limited.id }} bare />{/key}
  </Dialog>
{/if}

<style>
  .rows > div {
    padding: 0.35rem 0;
    min-height: 2.6rem;
  }
  .line {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.1rem 1rem;
  }
  .title {
    flex: 0 0 14rem;
    max-width: 100%;
  }
  .small {
    font-size: 13px;
  }
  .name {
    flex: 1 1 14rem;
    width: auto;
    max-width: 24rem;
  }
  .add {
    padding: 0.75rem 0 0.5rem;
  }
  .hint {
    margin-top: 0.6rem;
  }
  .confirm {
    flex-basis: 100%;
    padding: 0.6rem 0.9rem;
    margin-bottom: 0.4rem;
    border-left: 2px solid var(--warn);
    background: var(--panel);
  }
  .confirm p {
    margin: 0 0 0.25rem;
  }
</style>
