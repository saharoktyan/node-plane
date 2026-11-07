<!-- One text in every language the controller supports, as the bot's editor
     writes it: the main language must have a text; another left empty shows
     the main one to its readers. -->
<script lang="ts">
  import type { Draft } from './localized';
  import { t } from '$lib/i18n/index.svelte';

  // `named`: false where the row it sits in already names it.
  let { draft = $bindable(), languages, label, multiline = false, max, named = true }: {
    draft: Draft; languages: string[]; label: string; multiline?: boolean; max: number; named?: boolean;
  } = $props();
</script>

<fieldset class="localized" aria-label={named ? undefined : label}>
  {#if named}<legend>{label}</legend>{/if}
  {#each languages as language (language)}
    <div class="language">
      <div class="head">
        <span>{t('settings.language_' + language)}</span>
        <label class="check small"><input type="radio" value={language} bind:group={draft.source} /> {t('texts.main_language')}</label>
      </div>
      {#if multiline}
        <textarea rows="3" maxlength={max} bind:value={draft.texts[language]} aria-label={label + ' · ' + language}></textarea>
      {:else}
        <input maxlength={max} bind:value={draft.texts[language]} aria-label={label + ' · ' + language} />
      {/if}
      {#if language !== draft.source && !draft.texts[language]?.trim()}
        <span class="muted small">{t('texts.shows_main', { language: t('settings.language_' + draft.source) })}</span>
      {:else if language === draft.source && !draft.texts[language]?.trim()}
        <span class="muted small">{t('texts.main_needed')}</span>
      {/if}
    </div>
  {/each}
</fieldset>

<style>
  .localized {
    border: none;
    padding: 0;
    margin: 0;
    display: grid;
    gap: 0.75rem;
  }
  legend {
    margin-bottom: 0.25rem;
  }
  .language {
    display: grid;
    gap: 0.25rem;
  }
  .head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 1rem;
    color: var(--muted);
    font-size: 13px;
  }
  .small {
    font-size: 13px;
  }
  /* A message reads as prose, not code. */
  textarea {
    font: inherit;
  }
</style>
