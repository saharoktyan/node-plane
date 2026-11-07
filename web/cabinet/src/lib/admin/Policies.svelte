<!-- VPN limits of the installation, a profile or a device: each key's value
     in effect and where it comes from, and an own value set or removed. A
     removed value falls back to the tariff, the profile or the
     installation, as the controller resolves it. -->
<script lang="ts">
  import { account } from '$lib/account.svelte';
  import { api, call, ApiError } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';
  import { bytes, key } from '$lib/ui/format';
  import { FIELDS, GROUPS, fromInput, shown, toInput, type Field, type PolicyKey } from './policies';

  type Subject = { kind: 'installation' } | { kind: 'profile'; profileId: string }
    | { kind: 'device'; profileId: string; deviceId: string };
  let { subject, bare = false, onchange }: { subject: Subject; bare?: boolean; onchange?: () => void | Promise<void> } = $props();

  let keys = $state<PolicyKey[] | null>(null);
  let draft = $state<Record<string, { own: boolean; input: string }> | null>(null);
  let failure = $state('');
  let notice = $state('');
  let busy = $state(false);

  const permission = $derived(subject.kind === 'installation' ? 'settings.manage' : 'profiles.manage');
  const installation = $derived(subject.kind === 'installation');

  async function read() {
    if (subject.kind === 'installation') return call(api.GET('/api/v1/vpn-policies/installation'));
    if (subject.kind === 'profile') {
      return call(api.GET('/api/v1/profiles/{profile_id}/vpn-policies', { params: { path: { profile_id: subject.profileId } } }));
    }
    return call(api.GET('/api/v1/profiles/{profile_id}/devices/{device_id}/vpn-policies',
      { params: { path: { profile_id: subject.profileId, device_id: subject.deviceId } } }));
  }

  function write(changes: { key: string; value: unknown; revision: number }[]) {
    const headers = { 'Idempotency-Key': key() };
    const body = { changes };
    if (subject.kind === 'installation') return call(api.PATCH('/api/v1/vpn-policies/installation', { headers, body }));
    if (subject.kind === 'profile') {
      return call(api.PATCH('/api/v1/profiles/{profile_id}/vpn-policies',
        { params: { path: { profile_id: subject.profileId } }, headers, body }));
    }
    return call(api.PATCH('/api/v1/profiles/{profile_id}/devices/{device_id}/vpn-policies',
      { params: { path: { profile_id: subject.profileId, device_id: subject.deviceId } }, headers, body }));
  }

  async function load() {
    failure = '';
    try {
      keys = ((await read()) as unknown as { keys: PolicyKey[] }).keys;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }

  $effect(() => {
    void subject;
    load();
  });

  const fields = $derived(keys ? FIELDS.filter((field) => keys!.some((item) => item.key === field.key)) : []);
  const byKey = $derived(Object.fromEntries((keys ?? []).map((item) => [item.key, item])));

  // What a key comes to with the draft: an own value, or what it falls back to.
  const fallback = (item: PolicyKey) => (item.value == null ? item.effective : (item.tariff ?? item.default));
  const values = $derived(Object.fromEntries((keys ?? []).map((item) => {
    const entry = draft?.[item.key];
    if (!entry) return [item.key, item.effective];
    const field = FIELDS.find((f) => f.key === item.key);
    return [item.key, entry.own && field ? fromInput(field, entry.input) : fallback(item)];
  })));

  const slug = (name: string) => name.replace('.', '_');

  function show(field: Field, value: unknown) {
    if (value == null) return t('policies.unlimited');
    if (field.type === 'choice') return t(`policies.${slug(field.key)}_${value}`);
    if (field.type === 'bytes') return value === 0 ? t('policies.exempt') : bytes(value as number);
    if (field.type === 'speed') {
      if (value === 0) return t('policies.exempt');
      return (value as number) >= 1000 ? t('policies.mbit', { n: (value as number) / 1000 }) : t('policies.kbit', { n: value as number });
    }
    if (field.type === 'number' && field.unit !== 'count') return t('policies.' + field.unit, { n: value as number });
    return String(value);
  }

  function source(item: PolicyKey) {
    if (item.value != null) return t(installation ? 'policies.set' : 'policies.own');
    if (item.tariff != null) return t('policies.from_tariff');
    return t(installation ? 'policies.by_default' : subject.kind === 'device' ? 'policies.from_profile' : 'policies.inherited');
  }

  function edit() {
    draft = Object.fromEntries(fields.map((field) => {
      const item = byKey[field.key];
      return [field.key, { own: item.value != null, input: toInput(field, item.value ?? item.effective) }];
    }));
    notice = '';
  }

  function own(field: Field, on: boolean) {
    if (!draft) return;
    const item = byKey[field.key];
    draft[field.key] = { own: on, input: on ? draft[field.key].input || toInput(field, fallback(item)) : draft[field.key].input };
  }

  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (!draft) return;
    failure = notice = '';
    const changes = [];
    for (const field of fields) {
      const item = byKey[field.key];
      const entry = draft[field.key];
      const value = entry.own ? fromInput(field, entry.input) : null;
      if (entry.own && value == null) {
        failure = t('policies.invalid', { field: t('policies.' + slug(field.key)) });
        return;
      }
      if (JSON.stringify(value) !== JSON.stringify(item.value ?? null)) changes.push({ key: field.key, value, revision: item.revision });
    }
    if (!changes.length) {
      draft = null;
      return;
    }
    busy = true;
    try {
      await write(changes);
      draft = null;
      notice = t('policies.saved');
      await load();
      await onchange?.();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }
</script>

{#snippet body()}
  {#if notice}<p class="muted">{notice}</p>{/if}
  {#if failure}<p class="error">{failure}</p>{/if}
  {#if !keys}
    {#if !failure}<p class="muted">{t('common.loading')}</p>{/if}
  {:else if draft}
    <form onsubmit={save}>
      {#each GROUPS as group (group)}
        {#if fields.some((field) => field.group === group)}
          <h3>{t('policies.group_' + group)}</h3>
          {#each fields.filter((field) => field.group === group && shown(field, values)) as field (field.key)}
            {@const entry = draft[field.key]}
            <div class="field">
              <span class="name">{t('policies.' + slug(field.key))}</span>
              <label class="check"><input type="checkbox" checked={entry.own}
                onchange={(event) => own(field, event.currentTarget.checked)} />
                {t(installation ? 'policies.set' : 'policies.own')}</label>
              {#if entry.own}
                {#if field.type === 'choice'}
                  <select bind:value={entry.input} aria-label={t('policies.' + slug(field.key))}>
                    {#each field.values as value (value)}<option {value}>{t(`policies.${slug(field.key)}_${value}`)}</option>{/each}
                  </select>
                {:else}
                  <span class="input">
                    <input inputmode={field.type === 'text' ? 'text' : 'decimal'} bind:value={entry.input}
                      aria-label={t('policies.' + slug(field.key))} />
                    {#if field.type !== 'text' && !(field.type === 'number' && field.unit === 'count')}
                      <span class="muted">{t('policies.unit_' + (field.type === 'number' ? field.unit : field.type))}</span>
                    {/if}
                  </span>
                {/if}
              {:else}
                <span class="muted">{show(field, fallback(byKey[field.key]))}</span>
              {/if}
            </div>
            {#if entry.own && field.hint}
              <p class="muted small hint">{t('policies.' + slug(field.key) + '_hint')}</p>
            {/if}
          {/each}
        {/if}
      {/each}
      <div class="row actions">
        <button class="primary" disabled={busy}>{t('people.save')}</button>
        <button type="button" onclick={() => (draft = null)}>{t('common.cancel')}</button>
      </div>
    </form>
  {:else}
    <dl class="facts">
      {#each fields.filter((field) => shown(field, values)) as field (field.key)}
        {@const item = byKey[field.key]}
        <div><dt>{t('policies.' + slug(field.key))}</dt>
          <dd>{show(field, item.effective)} <span class="muted small">· {source(item)}</span></dd></div>
      {/each}
    </dl>
  {/if}
{/snippet}

{#if bare}
  {#if keys && !draft && account.may(permission)}<p><button onclick={edit}>{t('people.change')}</button></p>{/if}
  {@render body()}
{:else}
  <section class="block">
    <div class="block-head"><h2>{t('policies.title')}</h2>
      {#if keys && !draft && account.may(permission)}<button onclick={edit}>{t('people.change')}</button>{/if}
    </div>
    {@render body()}
  </section>
{/if}

<style>
  .small {
    font-size: 13px;
  }
  h3 {
    margin: 1rem 0 0.25rem;
    font-size: 1rem;
  }
  .field {
    display: grid;
    grid-template-columns: minmax(10rem, 1fr) auto minmax(8rem, 14rem);
    align-items: center;
    gap: 0.5rem 1rem;
    padding: 0.35rem 0;
    border-bottom: 1px solid var(--line);
  }
  .field .check {
    white-space: nowrap;
  }
  .field select {
    width: 100%;
    min-width: 0;
  }
  .input {
    display: flex;
    align-items: baseline;
    gap: 0.4rem;
  }
  .input input {
    width: 6rem;
  }
  .hint {
    margin: 0.25rem 0 0;
  }
  .actions {
    margin-top: 1rem;
  }
  @media (max-width: 640px) {
    .field {
      grid-template-columns: auto minmax(0, 1fr);
    }
    .field > :last-child {
      justify-self: end;
    }
    .field .name {
      grid-column: 1 / -1;
    }
  }
</style>
