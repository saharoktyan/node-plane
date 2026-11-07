<script lang="ts">
  import { page } from '$app/state';
  import Invite from '$lib/auth/Invite.svelte';
</script>

<Invite token={page.params.token ?? ''} />
