from .errors import ApiError


async def collect(fetch):
    """Every item of a listing; `fetch(cursor)` returns one page
    (`items`, `next_cursor`), the first with cursor None."""
    page = await fetch(None)
    items, seen = list(page['items']), set()
    while cursor := page.get('next_cursor'):
        if cursor in seen:
            raise ApiError('invalid_pagination', 502)
        seen.add(cursor)
        page = await fetch(cursor)
        items.extend(page['items'])
    return items
