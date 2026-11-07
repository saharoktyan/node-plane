<!-- A question under the thing it is about, before an action that cannot be
     taken back: the action's button and Cancel. -->
<script lang="ts">
  import { t } from '$lib/i18n/index.svelte';
  import { unfold } from './motion';

  let { text, action, danger = false, busy = false, confirm, cancel }: {
    text: string;
    action: string;
    danger?: boolean;
    busy?: boolean;
    confirm: () => void;
    cancel: () => void;
  } = $props();
</script>

<div class="confirm" role="alertdialog" aria-label={text} transition:unfold>
  <p>{text}</p>
  <div class="row">
    <button class={danger ? 'danger' : 'primary'} disabled={busy} onclick={confirm}>{action}</button>
    <button onclick={cancel}>{t('common.cancel')}</button>
  </div>
</div>

<style>
  .confirm {
    flex-basis: 100%;
    padding: 0.6rem 0.9rem;
    margin: 0.4rem 0;
    border-left: 2px solid var(--warn);
    background: var(--panel);
  }
  .confirm p {
    margin: 0 0 0.35rem;
  }
</style>
