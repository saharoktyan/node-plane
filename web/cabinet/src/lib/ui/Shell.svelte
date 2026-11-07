<!-- The frame of a signed-in page: a side panel with the sections in groups,
     the way between the cabinet and the administration, and the account;
     the page takes the rest of the width. The panel folds to icons on a
     desktop (remembered in this browser) and slides in over the page on a
     phone. -->
<script lang="ts" module>
  /** A section; neighbours with the same `group` go under its heading. `dot`
   * marks one that wants attention, `count` says how many things wait there. */
  export type Link = { href: string; label: string; icon: string; group?: string; count?: number; dot?: boolean };
</script>

<script lang="ts">
  import Toasts from './Toasts.svelte';
  import type { Snippet } from 'svelte';
  import { page } from '$app/state';
  import { account } from '$lib/account.svelte';
  import { t } from '$lib/i18n/index.svelte';
  import Icon from './Icon.svelte';

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

  // Each group's sections, in the order given.
  const groups = $derived(sections.reduce<{ name: string; links: Link[] }[]>((all, link) => {
    if (!all.length || all[all.length - 1].name !== (link.group ?? '')) all.push({ name: link.group ?? '', links: [] });
    all[all.length - 1].links.push(link);
    return all;
  }, []));

  $effect(() => {
    page.url.pathname;
    open = false;
  });
</script>

<div class="frame" class:folded>
  <aside class:open>
    <div class="head">
      <div class="brand">
        <span class="name">Node Plane</span>
        <span class="caps title">{title}</span>
      </div>
      <button class="bare fold" onclick={fold} aria-label={t(folded ? 'common.expand' : 'common.collapse')}
        title={t(folded ? 'common.expand' : 'common.collapse')}>
        <Icon name={folded ? 'expand' : 'collapse'} />
      </button>
    </div>
    <nav>
      {#each groups as group, index (group.name + index)}
        <div class="group">
          {#if group.name}<span class="caps heading">{group.name}</span>{/if}
          {#each group.links as section (section.href)}
            <a href={section.href} class:active={current(section.href)} title={section.label}
              aria-current={current(section.href) ? 'page' : undefined}>
              <Icon name={section.icon} /><span class="label">{section.label}</span>
              {#if section.count}<span class="count">{section.count}</span>
              {:else if section.dot}<span class="dot" aria-label={t('common.attention')}>●</span>{/if}
            </a>
          {/each}
        </div>
      {/each}
    </nav>
    <div class="foot">
      {#if switchTo}
        <a href={switchTo.href} title={switchTo.label}><Icon name={switchTo.icon} /><span class="label">{switchTo.label}</span></a>
      {/if}
      <!-- The account's name opens its settings; they have no section of their own. -->
      <a href="/settings" class:active={page.url.pathname.startsWith('/settings')} title={account.me?.login ?? ''}>
        <span class="avatar">{(account.me?.display_name ?? account.me?.login ?? '?').slice(0, 1).toUpperCase()}</span>
        <span class="label">{account.me?.display_name ?? account.me?.login ?? ''}</span>
      </a>
      <button class="out" onclick={() => account.signOut()} title={t('common.sign_out')}>
        <Icon name="out" /><span class="label">{t('common.sign_out')}</span>
      </button>
    </div>
  </aside>
  {#if open}<button class="scrim" aria-label={t('common.collapse')} onclick={() => (open = false)}></button>{/if}
  <div class="body">
    <header class="mobile">
      <button class="bare" onclick={() => (open = true)} aria-label={t('common.menu')}><Icon name="menu" /></button>
      <span class="name">Node Plane</span>
    </header>
    <main>{@render children()}</main>
  </div>
</div>
<Toasts />

<style>
  .frame {
    --width: 15rem;
    display: grid;
    grid-template-columns: var(--width) 1fr;
    min-height: 100vh;
  }
  .frame.folded {
    --width: 3.75rem;
  }
  aside {
    position: sticky;
    top: 0;
    height: 100vh;
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
    padding: 1.25rem 0.75rem;
    box-sizing: border-box;
    background: var(--panel);
    border-right: 1px solid var(--line);
    overflow: hidden auto;
  }
  .head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    padding: 0 0.25rem 0 0.6rem;
  }
  .brand {
    display: grid;
    gap: 0.1rem;
  }
  .name {
    font-family: var(--serif);
    font-size: 1.3rem;
    font-weight: 600;
    white-space: nowrap;
  }
  .title {
    color: var(--accent);
    white-space: nowrap;
  }
  nav {
    display: grid;
    gap: 1.1rem;
  }
  .group,
  .foot {
    display: grid;
    gap: 0.1rem;
  }
  .heading {
    padding: 0 0.6rem 0.25rem;
    white-space: nowrap;
  }
  .foot {
    margin-top: auto;
    border-top: 1px solid var(--line);
    padding-top: 0.75rem;
  }
  nav a,
  .foot a,
  .out {
    display: flex;
    align-items: center;
    gap: 0.7rem;
    padding: 0.45rem 0.6rem;
    border-left: 2px solid transparent;
    color: var(--soft);
    text-decoration: none;
    white-space: nowrap;
    font-weight: 400;
    text-align: left;
  }
  nav a :global(svg),
  .foot :global(svg) {
    flex: none;
    color: var(--muted);
  }
  nav a:hover,
  .foot a:hover,
  .out:hover {
    color: var(--text);
  }
  nav a.active,
  .foot a.active {
    color: var(--text);
    font-weight: 600;
    border-left-color: var(--accent);
  }
  .label {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .count,
  .dot {
    color: var(--accent);
    font-weight: 600;
    font-size: 12px;
  }
  .avatar {
    flex: none;
    width: 20px;
    height: 20px;
    border-radius: 50%;
    background: var(--text);
    color: var(--bg);
    font-size: 11px;
    font-weight: 600;
    display: grid;
    place-items: center;
  }
  .folded .brand,
  .folded .heading,
  .folded .label,
  .folded .count {
    display: none;
  }
  .folded .head {
    justify-content: center;
    padding: 0;
  }
  .folded .dot {
    position: absolute;
    margin-left: 14px;
    margin-top: -12px;
  }
  .body {
    min-width: 0;
  }
  main {
    padding: 1.5rem 2.5rem 3rem;
  }
  /* What a page shows comes in softly, also once its data has loaded; its
     title and breadcrumbs stand at once. */
  main > :global(*:not(.page-head, .crumbs)) {
    animation: appear 0.35s ease both;
  }
  @keyframes appear {
    from {
      opacity: 0;
      transform: translateY(6px);
    }
  }
  @media (prefers-reduced-motion: reduce) {
    main > :global(*:not(.page-head, .crumbs)) {
      animation-duration: 0.15s;
    }
    @keyframes appear {
      from {
        opacity: 0;
      }
    }
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
    .folded .brand,
    .folded .heading,
    .folded .label,
    .folded .count {
      display: revert;
    }
    .folded .head {
      justify-content: space-between;
    }
    .scrim {
      display: block;
      position: fixed;
      inset: 0;
      z-index: 10;
      border: none;
      background: rgb(0 0 0 / 0.4);
      padding: 0;
    }
    .mobile {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.5rem 1rem;
      background: var(--panel);
      border-bottom: 1px solid var(--line);
    }
    .mobile .name {
      font-size: 1.1rem;
    }
    main {
      padding: 1rem;
    }
  }
</style>
