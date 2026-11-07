<!-- The question before an action that cannot be taken back, under the
     buttons it is about. The buttons stay where they are: the one asked
     about turns `armed`, and pressing it again takes the action. -->
<script lang="ts">
  import { t } from '$lib/i18n/index.svelte';
  import { unfold } from './motion';

  let { text, action, cancel }: { text: string; action: string; cancel: () => void } = $props();
</script>

<div class="confirm" role="alertdialog" aria-label={text} transition:unfold>
  <p>{text} <span class="muted">{t('common.press_again', { action })}</span></p>
  <button onclick={cancel}>{t('common.cancel')}</button>
</div>

<style>
  .confirm {
    flex-basis: 100%;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.25rem 1rem;
    padding: 0.5rem 0.9rem;
    margin: 0.4rem 0;
    border-left: 2px solid var(--warn);
    background: var(--panel);
  }
  .confirm p {
    margin: 0;
    flex: 1 1 18rem;
  }
</style>
