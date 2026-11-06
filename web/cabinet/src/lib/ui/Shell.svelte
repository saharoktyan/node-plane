<!-- The frame of a signed-in page: a side panel with the sections, the way
     between the cabinet and the administration, and the account; the page
     takes the rest of the width. The panel folds to icons on a desktop
     (remembered in this browser) and slides in over the page on a phone. -->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import { page } from '$app/state';
  import { account } from '$lib/account.svelte';
  import { t } from '$lib/i18n/index.svelte';
  import Icon from './Icon.svelte';

  type Link = { href: string; label: string; icon: string };
  let {
    sections,
    title,
    switchTo,
    children
  }: { sections: Link[]; title: string; switchTo: Link | null; children: Snippet } = $props();

  const KEY = 'node-plane.panel';
  let folded = $state(read());
  let open = $state(false);

  function read() {
    try {
      return localStorage.getItem(KEY) === 'folded';
    } catch {
      return false;
    }
  }

  function fold() {
    folded = !folded;
    try {
      localStorage.setItem(KEY, folded ? 'folded' : 'open');
    } catch {
      // Lasts until the page closes.
    }
  }

  function current(href: string) {
    const path = page.url.pathname;
    return href === '/' || href === '/admin' ? path === href || (href === '/' && path.startsWith('/services/'))
      : path.startsWith(href);
  }

  $effect(() => {
    page.url.pathname;
    open = false;
  });
</script>

<div class="frame" class:folded>
  <aside class:open>
    <div class="head">
      <span class="title">{title}</span>
      <button class="icon fold" onclick={fold} title={t(folded ? 'common.expand' : 'common.collapse')}>
        <Icon name={folded ? 'expand' : 'collapse'} />
      </button>
    </div>
    <nav>
      {#each sections as section (section.href)}
        <a href={section.href} class:active={current(section.href)} title={section.label}>
          <Icon name={section.icon} /><span>{section.label}</span>
        </a>
      {/each}
    </nav>
    <div class="foot">
      {#if switchTo}
        <a href={switchTo.href} class="switch" title={switchTo.label}>
          <Icon name={switchTo.icon} /><span>{switchTo.label}</span>
        </a>
      {/if}
      <!-- The account's name opens its settings. -->
      <a href="/settings" class="who" title={account.me?.login ?? ''}>
        <span class="avatar">{(account.me?.display_name ?? account.me?.login ?? '?').slice(0, 1).toUpperCase()}</span>
        <span>{account.me?.display_name ?? account.me?.login ?? ''}</span>
      </a>
      <button class="link" onclick={() => account.signOut()} title={t('common.sign_out')}>
        <Icon name="out" /><span>{t('common.sign_out')}</span>
      </button>
    </div>
  </aside>
  {#if open}<button class="scrim" aria-label={t('common.collapse')} onclick={() => (open = false)}></button>{/if}
  <div class="body">
    <header class="mobile">
      <button class="icon" onclick={() => (open = true)} aria-label={t('common.menu')}><Icon name="menu" /></button>
      <span class="title">{title}</span>
    </header>
    <main>{@render children()}</main>
  </div>
</div>

<style>
  .frame {
    --panel: 15rem;
    display: grid;
    grid-template-columns: var(--panel) 1fr;
    min-height: 100vh;
  }
  .frame.folded {
    --panel: 3.75rem;
  }
  aside {
    position: sticky;
    top: 0;
    height: 100vh;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    padding: 0.75rem 0.6rem;
    box-sizing: border-box;
    background: var(--surface);
    border-right: 1px solid var(--line);
    overflow: hidden;
  }
  .head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.25rem 0.4rem 0.75rem;
  }
  .title {
    font-weight: 600;
    white-space: nowrap;
  }
  nav,
  .foot {
    display: grid;
    gap: 0.15rem;
  }
  .foot {
    margin-top: auto;
    border-top: 1px solid var(--line);
    padding-top: 0.5rem;
  }
  nav a,
  .switch,
  .who,
  .link {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.5rem 0.6rem;
    border-radius: 8px;
    color: var(--muted);
    text-decoration: none;
    white-space: nowrap;
    border: none;
    background: none;
    font: inherit;
    cursor: pointer;
    text-align: left;
  }
  nav a:hover,
  .switch:hover,
  .who:hover,
  .link:hover {
    background: var(--bg);
    color: var(--text);
  }
  nav a.active {
    background: var(--accent-soft);
    color: var(--accent);
  }
  .switch {
    color: var(--text);
  }
  .who {
    color: var(--text);
  }
  .avatar {
    flex: none;
    width: 20px;
    height: 20px;
    border-radius: 50%;
    background: var(--accent-soft);
    color: var(--accent);
    font-size: 0.75rem;
    display: grid;
    place-items: center;
  }
  .icon {
    border: none;
    background: none;
    padding: 0.3rem;
    color: var(--muted);
    display: grid;
  }
  .folded aside .title,
  .folded nav span,
  .folded .foot span:not(.avatar) {
    display: none;
  }
  .folded .head {
    justify-content: center;
  }
  .body {
    min-width: 0;
  }
  main {
    padding: 1.5rem 2rem 3rem;
  }
  .mobile,
  .scrim {
    display: none;
  }
  @media (max-width: 760px) {
    .frame,
    .frame.folded {
      grid-template-columns: 1fr;
    }
    aside {
      position: fixed;
      z-index: 20;
      width: 16rem;
      left: 0;
      transform: translateX(-100%);
      transition: transform 0.2s;
    }
    aside.open {
      transform: none;
    }
    .fold {
      display: none;
    }
    .folded aside .title,
    .folded nav span,
    .folded .foot span:not(.avatar) {
      display: inline;
    }
    .scrim {
      display: block;
      position: fixed;
      inset: 0;
      z-index: 10;
      border: none;
      background: rgb(0 0 0 / 0.35);
    }
    .mobile {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.5rem 1rem;
      background: var(--surface);
      border-bottom: 1px solid var(--line);
    }
    main {
      padding: 1rem;
    }
  }
</style>
