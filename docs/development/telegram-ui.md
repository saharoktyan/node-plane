# Telegram interface development

`app/telegram_client/` owns presentation and dialog state. Business mutations are backend API requests. Keep Telegram identity resolution separate from account authorization.

## Screens and navigation

Use existing render helpers and localized text keys. The location path is composed of clickable parent screens and the current screen. Shorten long paths without losing the most useful recent context. Back must use the actual entry path, including entry from Updates.

Preserve drafts until saved or explicitly discarded. A successful save clears the draft state. Do not leave a Reset draft action that can restore outdated values after a committed mutation.

## Formatting

Use compact rows and bold opening lines to distinguish events and subsections. Explain state and action consequences; avoid implementation history in user text. Render protocol names consistently with ` · ` separators.

Interactive links must look actionable. Keep controls outside nested expandable lists because Telegram clients differ in interaction support. Use a separate copyable-message action for URI/public-key delivery on iOS. Preserve a compatible monospace representation for configuration text.

## Progress and notifications

Persist enough panel state to resume updates after a bot restart. Poll only active operations; stop at a terminal state and retain a readable result. Coalesce repeated updates and avoid unnecessary remote inspection on navigation.

Notifications are separate from the current menu. Close removes the delivered message without discarding the saved announcement; dismiss also removes it from the user's notification list. Alert dismissal clears attention, not the underlying condition.

## Validation

Use unit tests for callbacks, draft lifecycle, actor authorization and panel recovery. Test formatted messages manually on Android, iOS and desktop clients. Measure callback stages separately: backend request, rendering and Telegram API calls. Total handler duration alone does not identify the bottleneck.
