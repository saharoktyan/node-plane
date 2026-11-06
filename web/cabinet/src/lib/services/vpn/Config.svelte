<!-- An issued configuration: its link to copy, a QR code drawn in the page
     (the configuration goes to no other service for it) and its files. -->
<script lang="ts">
  import type { Schemas } from '$lib/api/client';
  import { t } from '$lib/i18n/index.svelte';
  import { qr } from './qr';

  let { issuance, artifact, onclose }: {
    issuance: Schemas['ConfigIssuanceOutput'];
    artifact: Schemas['ConfigArtifactOutput'];
    onclose: () => void;
  } = $props();

  // A VLESS link, or AmneziaVPN's vpn:// key; a .conf alone has no link.
  const link = $derived(issuance.protocol === 'xray' || issuance.transport === 'vpn' ? artifact.content : null);
  const files = $derived(artifact.files?.length ? artifact.files
    : [{ filename: artifact.filename ?? `${issuance.protocol}-${issuance.node_key}.txt`, content: artifact.content }]);
  let copied = $state(false);

  const image = $derived(link ? qr(issuance.protocol, issuance.transport, link) : null);

  async function copy() {
    if (!link) return;
    try {
      await navigator.clipboard.writeText(link);
      copied = true;
      setTimeout(() => (copied = false), 2000);
    } catch {
      copied = false;
    }
  }

  function save(file: { filename: string; content: string }) {
    const url = URL.createObjectURL(new Blob([file.content], { type: 'text/plain' }));
    const anchor = Object.assign(document.createElement('a'), { href: url, download: file.filename });
    anchor.click();
    URL.revokeObjectURL(url);
  }
</script>

<div class="config">
  <div class="head">
    <strong>{artifact.display_name ?? artifact.filename}</strong>
    <button class="bare close" onclick={onclose} aria-label={t('vpn.close')} title={t('vpn.close')}>×</button>
  </div>
  <p class="muted">{t(issuance.protocol === 'xray' ? 'vpn.import_xray' : 'vpn.import_awg')}</p>
  <div class="parts">
    {#if link}
      <div class="qr">
        <div class="label">{t('vpn.qr')}</div>
        {#if image}
          <!-- eslint-disable-next-line svelte/no-at-html-tags -->
          <div class="image">{@html image}</div>
        {:else}
          <p class="muted">{t('vpn.qr_too_long')}</p>
        {/if}
      </div>
    {/if}
    <div class="grow">
      {#if link}
        <div class="label">{t('vpn.link')}</div>
        <textarea readonly rows="4" onfocus={(event) => event.currentTarget.select()}>{link}</textarea>
        <div class="row"><button class="primary" onclick={copy}>{copied ? t('vpn.copied') : t('vpn.copy')}</button></div>
      {/if}
      <div class="label">{t('vpn.files')}</div>
      <div class="row">
        {#each files as file (file.filename)}
          <button onclick={() => save(file)}>{file.filename}</button>
        {/each}
      </div>
    </div>
  </div>
</div>

<style>
  /* Opens under its server's row, marked by the accent on the left. */
  .config {
    border-left: 2px solid var(--accent);
    background: var(--panel);
    padding: 0.9rem 1.1rem 1.1rem;
  }
  .head {
    display: flex;
    align-items: center;
    gap: 1rem;
    justify-content: space-between;
  }
  .close {
    font-size: 1.4rem;
    line-height: 1;
  }
  .head + p {
    margin: 0.25rem 0 0;
    font-size: 13px;
  }
  .parts {
    display: flex;
    gap: 0.5rem 1.75rem;
    flex-wrap: wrap;
  }
  .grow {
    flex: 1 1 18rem;
    min-width: 0;
  }
  .label {
    font-size: 11px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--muted);
    margin: 0.9rem 0 0.4rem;
  }
  .qr .image {
    width: 11rem;
    max-width: 100%;
    background: #fff;
    padding: 0.5rem;
    box-sizing: border-box;
    line-height: 0;
  }
  .qr :global(svg) {
    width: 100%;
    height: auto;
  }
  textarea {
    background: var(--bg);
    width: 100%;
    resize: vertical;
    word-break: break-all;
    margin-bottom: 0.25rem;
  }
  .row button {
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
  }
</style>
