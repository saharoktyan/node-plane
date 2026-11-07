<!-- An announcement to everyone with access, as the bot and Workstation
     send it: a text, and a title if wanted, in each language; a preview
     says who gets it and what each language reads before it goes. Below,
     how the last one is being delivered. -->
<script lang="ts">
  import { api, call, ApiError, type Schemas } from '$lib/api/client';
  import Localized from '$lib/admin/Localized.svelte';
  import { draftOf, empty, inputOf, type Draft } from '$lib/admin/localized';
  import { i18n, t } from '$lib/i18n/index.svelte';
  import Confirm from '$lib/ui/Confirm.svelte';
  import { errorText } from '$lib/ui/errors';
  import { key } from '$lib/ui/format';
  import { unfold } from '$lib/ui/motion';
  import { toast } from '$lib/ui/toast.svelte';

  type Job = { id: string; status: 'running' | 'completed'; total: number; counts: Record<string, number> };
  type Text = Schemas['LocalizedTextOutput'];
  type Preview = { texts: Text; titles: Text | null; recipients: number; telegram: number; languages: Record<string, number> };
  type Body = { texts: Schemas['LocalizedTextInput']; titles: Schemas['LocalizedTextInput'] | null };

  let languages = $state<string[]>(['en', 'ru']);
  let last = $state<Job | null | undefined>(undefined);
  let texts = $state<Draft>(draftOf(null, ['en', 'ru'], i18n.language));
  let titles = $state<Draft>(draftOf(null, ['en', 'ru'], i18n.language));
  // What was previewed is what goes, under one key, so a retry sends it once.
  let preview = $state<{ body: Body; result: Preview; key: string } | null>(null);
  let sending = $state(false);
  let failure = $state('');
  let busy = $state(false);

  async function load() {
    try {
      last = ((await call(api.GET('/api/v1/announcements'))) as unknown as { last_job: Job | null }).last_job;
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    }
  }
  (async () => {
    const system = await call(api.GET('/api/v1/system/languages')).catch(() => null);
    if (system) {
      languages = system.languages;
      texts = draftOf(null, languages, i18n.language);
      titles = draftOf(null, languages, i18n.language);
    }
    await load();
  })();

  // A delivery under way is asked about every few seconds until it is done.
  $effect(() => {
    if (last?.status !== 'running') return;
    const id = last.id;
    const timer = setInterval(async () => {
      last = (await call(api.GET('/api/v1/announcements/{announcement_id}', { params: { path: { announcement_id: id } } }))
        .catch(() => last)) as Job;
    }, 3000);
    return () => clearInterval(timer);
  });

  function body(): Body | null {
    const text = inputOf(texts, languages);
    if (!text) {
      failure = t('announcements.text_needed');
      return null;
    }
    const title = empty(titles) ? null : inputOf(titles, languages);
    if (!empty(titles) && !title) {
      failure = t('announcements.title_needed');
      return null;
    }
    return { texts: text, titles: title };
  }

  async function look() {
    failure = '';
    const next = body();
    if (!next) return;
    busy = true;
    try {
      const result = (await call(api.POST('/api/v1/announcements/preview', { body: next }))) as unknown as Preview;
      preview = { body: next, result, key: key() };
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
    }
  }

  async function send() {
    if (!preview) return;
    if (!sending) {
      sending = true;
      return;
    }
    busy = true;
    failure = '';
    try {
      last = (await call(api.POST('/api/v1/announcements', { headers: { 'Idempotency-Key': preview.key },
        body: preview.body }))) as unknown as Job;
      toast(t('announcements.sent'));
      preview = null;
      texts = draftOf(null, languages, i18n.language);
      titles = draftOf(null, languages, i18n.language);
    } catch (error) {
      failure = error instanceof ApiError ? errorText(error) : t('common.error');
    } finally {
      busy = false;
      sending = false;
    }
  }

  const name = (language: string) => t('settings.language_' + language);
  // What a reader of this language gets: its own text, or the one it falls back to.
  const reads = (value: Text | null, language: string) => {
    if (!value) return null;
    const shown = value.texts[language] ? language : (value.fallback[language] ?? value.source);
    return { text: value.texts[shown] ?? '', from: shown === language ? null : shown };
  };
  const COUNTS = ['sent', 'failed', 'unknown', 'skipped', 'inbox_only'];
</script>

<div class="page-head"><h1>{t('admin.announcements')}</h1></div>
{#if failure}<p class="error">{failure}</p>{/if}

<div class="columns appear">
  <div class="main">
    <section class="block">
      <div class="block-head"><h2>{t('announcements.new')}</h2></div>
      <p class="muted small">{t('announcements.hint')}</p>
      {#if preview}
        <div transition:unfold>
          <p><strong>{preview.result.telegram < preview.result.recipients
            ? t('announcements.recipients_split', { total: preview.result.recipients, telegram: preview.result.telegram,
              inbox: preview.result.recipients - preview.result.telegram })
            : t('announcements.recipients', { total: preview.result.recipients })}</strong></p>
          {#each Object.entries(preview.result.languages) as [language, count] (language)}
            {@const text = reads(preview.result.texts, language)!}
            {@const title = reads(preview.result.titles, language)}
            <div class="reader">
              <div class="muted small">{name(language)} · {t('announcements.people', { count })}
                {#if text.from}· {t('announcements.reads', { language: name(text.from) })}{/if}</div>
              {#if title}<strong>{title.text}</strong>{/if}
              <p class="text">{text.text}</p>
            </div>
          {/each}
          <div class="row">
            <button class="primary" class:armed={sending} disabled={busy || !preview.result.recipients} onclick={send}>
              {t('announcements.send')}</button>
            <button disabled={busy} onclick={() => { preview = null; sending = false; }}>{t('announcements.edit')}</button>
          </div>
          {#if sending}<Confirm text={t('announcements.send_confirm', { total: preview.result.recipients })}
            action={t('announcements.send')} cancel={() => (sending = false)} />{/if}
        </div>
      {:else}
        <form class="compose" onsubmit={(event) => { event.preventDefault(); look(); }}>
          <Localized bind:draft={titles} {languages} label={t('announcements.title_optional')} max={120} optional />
          <Localized bind:draft={texts} {languages} label={t('announcements.text')} max={3000} multiline />
          <div class="row"><button class="primary" disabled={busy}>{t('announcements.preview')}</button></div>
        </form>
      {/if}
    </section>
  </div>
  <aside>
    <section class="block">
      <div class="block-head"><h2>{t('announcements.last')}</h2>
        {#if last}<span class="badge" class:live={last.status === 'running'}>{t('announcements.status_' + last.status)}</span>{/if}</div>
      {#if last === null}
        <p class="muted">{t('announcements.none')}</p>
      {:else if last}
        <dl class="facts">
          {#each COUNTS.filter((name) => name !== 'inbox_only' || last!.counts.inbox_only) as name (name)}
            <div><dt>{t('announcements.count_' + name)}</dt><dd>{last.counts[name] ?? 0}</dd></div>
          {/each}
          {#if last.status === 'running'}
            <div><dt>{t('announcements.count_waiting')}</dt><dd>{(last.counts.queued ?? 0) + (last.counts.claimed ?? 0)}</dd></div>
          {/if}
        </dl>
      {/if}
    </section>
  </aside>
</div>

<style>
  .small {
    font-size: 13px;
  }
  .compose {
    display: grid;
    gap: 1.25rem;
    max-width: 40rem;
    margin-top: 1rem;
  }
  .reader {
    padding: 0.75rem 0;
    border-bottom: 1px solid var(--line);
  }
  .text {
    white-space: pre-line;
    margin: 0.25rem 0 0;
  }
  .row {
    margin-top: 1rem;
  }
  .live {
    color: var(--accent);
  }
</style>
