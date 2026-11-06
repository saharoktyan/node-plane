<!-- The frame of a signed-in page: the top bar with its sections, the
     account's menu and the page. The cabinet and the administration each
     pass their own sections. -->
<script lang="ts">
  import type { Snippet } from 'svelte';
  import { page } from '$app/state';
  import { account } from '$lib/account.svelte';
  import { t } from '$lib/i18n/index.svelte';

  let {
    sections,
    title,
    switchTo,
    children
  }: {
    sections: { href: string; label: string }[];
    title: string;
    switchTo: { href: string; label: string } | null;
    children: Snippet;
  } = $props();

  function current(href: string) {
    const path = page.url.pathname;
    return href === '/' || href === '/admin' ? path === href : path.startsWith(href);
  }
</script>

<header>
  <a class="brand" href={sections[0]?.href ?? '/'}>{title}</a>
  <nav>
    {#each sections as section (section.href)}
      <a href={section.href} class:active={current(section.href)}>{section.label}</a>
    {/each}
  </nav>
  <div class="row end">
    {#if switchTo}<a class="button" href={switchTo.href}>{switchTo.label}</a>{/if}
    <span class="muted">{account.me?.display_name ?? account.me?.login ?? ''}</span>
    <button onclick={() => account.signOut()}>{t('common.sign_out')}</button>
  </div>
</header>
<main>
  {@render children()}
</main>

<style>
  header {
    display: flex;
    gap: 1rem;
    align-items: center;
    flex-wrap: wrap;
    padding: 0.75rem 1rem;
    background: var(--surface);
    border-bottom: 1px solid var(--line);
  }
  .brand {
    font-weight: 600;
    color: var(--text);
    text-decoration: none;
  }
  nav {
    display: flex;
    gap: 0.25rem;
    flex-wrap: wrap;
    flex: 1;
  }
  nav a {
    padding: 0.35rem 0.7rem;
    border-radius: 8px;
    color: var(--muted);
    text-decoration: none;
  }
  nav a.active {
    background: var(--bg);
    color: var(--text);
  }
  .end {
    margin-left: auto;
  }
  main {
    max-width: 64rem;
    margin: 0 auto;
    padding: 1.25rem 1rem 3rem;
  }
</style>
