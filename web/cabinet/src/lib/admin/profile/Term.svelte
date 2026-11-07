<!-- A profile's term and tariff: until when it works (a date, a number of
     days more, or forever), the tariff it is on and how assigning one moves
     the term, and a renewal the person asked for. -->
<script lang="ts">
  import { account } from '$lib/account.svelte';
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import { errorText } from '$lib/ui/errors';
  import { date, key } from '$lib/ui/format';

  let { profile, summary, permanent, onchange }: {
    profile: Schemas['ProfileOutput'];
    summary: Schemas['MemberProfileSummary'] | null;
    permanent: boolean;
    onchange: () => Promise<void>;
  } = $props();

  type Tariff = { id: string; title: string; duration_days: number | null };
  const tariff = $derived(summary?.tariff as Tariff | null | undefined);
  const renewal = $derived(summary?.renewal as { id: string; status: string } | null | undefined);

  let editing = $state<'term' | 'tariff' | null>(null);
  let until = $state('');
  let tariffs = $state<Schemas['TariffOutput'][]>([]);
  let draft = $state({ tariff: '', expiry: 'start' as 'keep' | 'start' | 'extend', access: true });
  // A renewal without a tariff's term names its days.
  let renewDays = $state(30);
  let busy = $state(false);
  let failure = $state('');
  let notice = $state('');

  const path = () => ({ path: { profile_id: profile.id } });
  const headers = () => ({ 'Idempotency-Key': key(), 'If-Match': `"${profile.desired_revision}"` });

  async function run(work: () => Promise<unknown>, done: string) {
    busy = true;
    failure = notice = '';
    try {
      await work();
      editing = null;
      notice = t(done);
      await onchange();
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  function editTerm() {
    until = profile.expires_at ? profile.expires_at.slice(0, 10) : '';
    editing = 'term';
  }

  async function editTariff() {
    tariffs = (await call(api.GET('/api/v1/tariffs')).catch(() => ({ items: [] }))).items;
    draft = { tariff: tariff?.id ?? '', expiry: 'start', access: true };
    editing = 'tariff';
  }

  // The end of the chosen day, in UTC, as the bot and Workstation set it.
  const endOf = (day: string) => day + 'T23:59:59+00:00';

  function plus(days: number) {
    const from = profile.expires_at && new Date(profile.expires_at) > new Date() ? new Date(profile.expires_at) : new Date();
    until = new Date(from.getTime() + days * 86_400_000).toISOString().slice(0, 10);
  }

  const setTerm = (value: string | null) => run(() => call(api.PATCH('/api/v1/profiles/{profile_id}', {
    params: { ...path(), header: headers() }, body: { expires_at: value } })), 'profiles.term_saved');

  const setTariff = () => run(() => call(api.PUT('/api/v1/profiles/{profile_id}/tariff', {
    params: { ...path(), header: headers() },
    body: { tariff_id: draft.tariff || null, expiry: draft.expiry, apply_access: draft.access } })), 'profiles.tariff_saved');

  const approve = () => run(() => call(api.POST('/api/v1/renewal-requests/{request_id}/approve', {
    params: { path: { request_id: renewal!.id }, header: headers() }, body: { days: tariff?.duration_days ? null : renewDays } })), 'profiles.renewal_approved');

  const reject = () => run(() => call(api.POST('/api/v1/renewal-requests/{request_id}/reject', {
    params: { path: { request_id: renewal!.id } } })), 'profiles.renewal_rejected');
</script>

<section class="block">
  <h2>{t('profiles.term')}</h2>
  {#if notice}<p class="muted">{notice}</p>{/if}
  {#if failure}<p class="error">{failure}</p>{/if}
  <div class="rows">
    <div>
      <span class="grow">{t('profiles.expires')}: <strong>{permanent || !profile.expires_at ? t('profiles.forever')
        : date(profile.expires_at)}</strong>
        {#if permanent}<span class="muted small">{t('profiles.staff_forever')}</span>{/if}</span>
      {#if !permanent && account.may('profiles.manage') && editing !== 'term'}
        <button onclick={editTerm}>{t('people.change')}</button>
      {/if}
    </div>
    {#if editing === 'term'}
      <form class="edit" onsubmit={(event) => { event.preventDefault(); setTerm(until ? endOf(until) : null); }}>
        <div class="row">
          <input type="date" bind:value={until} aria-label={t('profiles.expires')} />
          <button type="button" onclick={() => plus(30)}>+30</button>
          <button type="button" onclick={() => plus(90)}>+90</button>
          <button type="button" onclick={() => plus(365)}>+365</button>
        </div>
        <p class="muted small">{t('profiles.term_hint')}</p>
        <div class="row">
          <button class="primary" disabled={busy}>{until ? t('people.save') : t('profiles.set_forever')}</button>
          <button type="button" onclick={() => (editing = null)}>{t('common.cancel')}</button>
        </div>
      </form>
    {/if}
    <div>
      <span class="grow">{t('profiles.tariff')}: <strong>{tariff?.title ?? t('profiles.no_tariff')}</strong>
        {#if tariff?.duration_days}<span class="muted small">{t('profiles.tariff_days', { days: tariff.duration_days })}</span>{/if}</span>
      {#if account.may('profiles.manage') && editing !== 'tariff'}
        <button onclick={editTariff}>{t('people.change')}</button>
      {/if}
    </div>
    {#if editing === 'tariff'}
      <form class="edit" onsubmit={(event) => { event.preventDefault(); setTariff(); }}>
        <label>{t('profiles.tariff')}
          <select bind:value={draft.tariff}>
            <option value="">{t('profiles.no_tariff')}</option>
            {#each tariffs as item (item.id)}<option value={item.id}>{item.title}</option>{/each}
          </select>
        </label>
        {#if draft.tariff && !permanent}
          <fieldset>
            {#each ['start', 'extend', 'keep'] as const as mode (mode)}
              <label class="check"><input type="radio" name="expiry" value={mode} bind:group={draft.expiry} />
                {t('profiles.tariff_expiry_' + mode)}</label>
            {/each}
          </fieldset>
          <label class="check"><input type="checkbox" bind:checked={draft.access} /> {t('profiles.tariff_access')}</label>
        {/if}
        <div class="row">
          <button class="primary" disabled={busy}>{t('people.save')}</button>
          <button type="button" onclick={() => (editing = null)}>{t('common.cancel')}</button>
        </div>
      </form>
    {/if}
    {#if renewal?.status === 'pending'}
      <div>
        <span class="grow"><strong>{t('profiles.renewal_asked')}</strong>
          <span class="muted small">{tariff?.duration_days ? t('profiles.renewal_by', { days: tariff.duration_days }) : ''}</span></span>
        {#if account.may('profiles.manage')}
          {#if !tariff?.duration_days}
            <input class="days" type="number" min="1" max="3650" bind:value={renewDays} aria-label={t('profiles.renewal_days')} />
          {/if}
          <button class="primary" disabled={busy} onclick={approve}>{t('people.approve')}</button>
          <button disabled={busy} onclick={reject}>{t('people.reject')}</button>
        {/if}
      </div>
    {/if}
  </div>
</section>

<style>
  .rows > div {
    padding: 0.4rem 0;
  }
  .small {
    font-size: 13px;
    margin-left: 0.5rem;
  }
  .edit {
    display: grid;
    gap: 0.6rem;
    padding: 0.5rem 0 0.75rem;
    max-width: 30rem;
  }
  .edit input[type='date'] {
    width: auto;
  }
  .edit p {
    margin: 0;
  }
  .days {
    width: 5rem;
  }
  fieldset {
    border: none;
    padding: 0;
    margin: 0;
  }
</style>
