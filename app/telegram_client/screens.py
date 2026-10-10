"""Rich Message screen construction; never use Markdown parse mode."""
from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import (InputRichBlockDetails, InputRichBlockParagraph,
    InputRichBlockSectionHeading, InputRichMessage, InputRichBlockPhoto,
    InputRichBlockDocument, InputMediaPhoto, InputMediaDocument, BufferedInputFile,
    MessageEntity)
from aiogram.types import (InputRichBlockButtons, RichMessageButton, InlineKeyboardButton,
    InputRichBlockDivider, InputRichBlockTable, RichBlockTableCell)
from aiogram.types import RichTextButton, RichTextBold, RichTextCode
import unicodedata


@dataclass(frozen=True)
class Breadcrumb:
    label: str
    callback: str


def text_width(text):
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in {'W', 'F'} else 1 for c in text)


def shorten_label(text, limit):
    if text_width(text) <= limit:
        return text
    result = ''
    for char in text:
        if text_width(result + char) > limit - 1:
            break
        result += char
    return result.rstrip() + '…'


def breadcrumb_text(parents, title, more_callback=None, limit=48):
    """Keep the immediate parent and current screen legible on mobile clients."""
    visible = list(parents)
    if not more_callback:
        limit = 100000
    hidden = False
    while len(visible) > 1 and text_width(' › '.join(
            [p.label for p in visible] + [title])) + (4 if hidden else 0) > limit:
        visible.pop(0)
        hidden = True
    labels = [p.label for p in visible]
    if text_width(' › '.join(labels + [title])) + (4 if hidden else 0) > limit:
        labels = [shorten_label(label, 16) for label in labels]
        title = shorten_label(title, 24)
        hidden = True
    parts = []
    if hidden and more_callback:
        parts.extend([RichTextButton(button=RichMessageButton(text='…',
            callback_data=more_callback, style='link')), ' › '])
    for parent, label in zip(visible, labels):
        parts.extend([RichTextButton(button=RichMessageButton(text=label,
            callback_data=parent.callback, style='link')), ' › '])
    parts.append(RichTextBold(text=title))
    return parts


def server_label(node: dict) -> str:
    return f"{node.get('flag') or ''} {node['title']}".strip()


def format_size(size_bytes: int) -> str:
    """Display nonempty small files without rounding them to zero MiB."""
    if size_bytes < 1024:
        return f'{size_bytes} B'
    amount = float(size_bytes)
    for unit in ('KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB'):
        amount /= 1024
        if amount < 1024 or unit == 'EiB':
            return f'{amount:.1f} {unit}'
    raise ValueError('invalid size')


BACK_LABELS = {'← Back', '← Назад', 'Back', 'Назад'}


def rich_buttons(rows, *, navigation=False):
    return [InputRichBlockButtons(buttons=[RichMessageButton(
        text=button.text, callback_data=button.callback_data, url=button.url,
        copy_text=button.copy_text, style=button.style or ('link' if navigation and button.callback_data and
            (len(row) == 1 or button.text in BACK_LABELS) else None))
        for button in row]) for row in rows if row]


@dataclass(frozen=True)
class Table:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    row_callbacks: tuple[str, ...] = ()

    def rich(self):
        return InputRichBlockTable(is_compact=True, is_striped=True, cells=[
            [RichBlockTableCell(text=RichTextButton(button=RichMessageButton(text=value,
                callback_data=self.row_callbacks[index - 1], style='link'))
                if index and column == 0 and index <= len(self.row_callbacks) else value,
                align='left', valign='top', is_header=index == 0)
             for column, value in enumerate(row)] for index, row in enumerate((self.headers, *self.rows))])

    def plain(self):
        return tuple(' · '.join(f'{label}: {value}' for label, value in zip(self.headers, row))
                     for row in self.rows)


@dataclass(frozen=True)
class Section:
    title: str
    lines: tuple[str, ...] = ()
    rows: tuple[tuple[InlineKeyboardButton, ...], ...] = ()
    collapsed: bool = False
    divider_after: bool = False
    sections: tuple['Section', ...] = ()
    heading_size: int | None = None
    is_open: bool = False
    tables: tuple[Table, ...] = ()
    heading_rows: tuple[tuple[InlineKeyboardButton, ...], ...] = ()
    inline_rows: tuple[tuple[str | InlineKeyboardButton, ...], ...] = ()

    def rich(self, depth=2):
        # iOS cannot activate controls inside Details, even in nested sections.
        # Expand the whole interactive branch so every control stays accessible.
        def interactive(section):
            return bool(section.rows or section.heading_rows or section.inline_rows or any(t.row_callbacks for t in section.tables) or
                        any(interactive(child) for child in section.sections))
        collapsed = self.collapsed and not interactive(self)
        size = self.heading_size or min(depth, 6)
        blocks = [InputRichBlockParagraph(text=line) for line in self.lines if line]
        blocks.extend(table.rich() for table in self.tables)
        blocks.extend(InputRichBlockParagraph(text=[
            RichTextButton(button=RichMessageButton(text=item.text, callback_data=item.callback_data,
                url=item.url, style='link')) if isinstance(item, InlineKeyboardButton) else item
            for item in row]) for row in self.inline_rows)
        for section in self.sections:
            blocks.extend(section.rich(depth=size if collapsed or not self.title else size + 1))
        blocks.extend(rich_buttons(self.rows))
        if collapsed:
            content = [*rich_buttons(self.heading_rows), *blocks]
            if not content:
                return []
            return [InputRichBlockDetails(summary=self.title,
                blocks=content, is_open=self.is_open)]
        return [*([InputRichBlockSectionHeading(text=self.title, size=size)] if self.title else []),
                *rich_buttons(self.heading_rows), *blocks,
                *([InputRichBlockDivider()] if self.divider_after else [])]


@dataclass(frozen=True)
class Screen:
    title: str
    lines: tuple[str, ...] = ()
    details_title: str | None = None
    details_lines: tuple[str, ...] = ()
    uri: str | None = None
    qr: bytes | None = None
    qr_title: str | None = None
    files: tuple[tuple[str, bytes], ...] = ()
    files_title: str | None = None
    uri_title: str | None = None
    uri_rows: tuple[tuple[InlineKeyboardButton, ...], ...] = ()
    uri_collapsed: bool = False
    sections: tuple[Section, ...] = ()
    embedded_buttons: bool = False
    navigation: bool = False
    breadcrumbs: tuple[Breadcrumb, ...] = ()
    breadcrumb_more: str | None = None
    navigation_return: str | None = None

    def rich(self, rows=()) -> InputRichMessage:
        blocks = ([InputRichBlockParagraph(text=breadcrumb_text(self.breadcrumbs,
            self.title, self.breadcrumb_more))] if self.breadcrumbs else
            [InputRichBlockSectionHeading(text=self.title, size=1)])
        if self.breadcrumbs and self.breadcrumb_more and text_width(self.title) > 24 and text_width(
                ' › '.join([p.label for p in self.breadcrumbs] + [self.title])) > 48:
            blocks.append(InputRichBlockSectionHeading(text=self.title, size=1))
        blocks.extend(InputRichBlockParagraph(text=line) for line in self.lines if line)
        for section in self.sections:
            blocks.extend(section.rich())
        if self.details_title and self.details_lines:
            blocks.append(InputRichBlockDetails(summary=self.details_title,
                blocks=[InputRichBlockParagraph(text=line) for line in self.details_lines]))
        if self.qr and self.qr_title:
            blocks.append(InputRichBlockDetails(summary=self.qr_title, is_open=False,
                blocks=[InputRichBlockPhoto(photo=InputMediaPhoto(
                    media=BufferedInputFile(self.qr, 'config.png')))]))
        if self.uri and self.uri_collapsed:
            blocks.append(InputRichBlockDetails(summary=self.uri_title or '', is_open=False,
                blocks=[InputRichBlockParagraph(text=RichTextCode(text=self.uri))]))
        for row in self.uri_rows:
            # Native inline text links are visible outside the collapsible URI.
            links = []
            for button in row:
                if links:
                    links.append(' · ')
                links.append(RichTextButton(button=RichMessageButton(text=button.text,
                    callback_data=button.callback_data, url=button.url, style='link')))
            if links:
                blocks.append(InputRichBlockParagraph(text=links))
        if self.uri and not self.uri_collapsed:
            uri = InputRichBlockParagraph(text=RichTextCode(text=self.uri))
            if self.uri_title and not self.uri_rows:
                blocks.append(InputRichBlockSectionHeading(text=self.uri_title, size=2))
            blocks.append(uri)
        documents = [InputRichBlockDocument(document=InputMediaDocument(
            media=BufferedInputFile(content, filename))) for filename, content in self.files]
        if documents:
            if self.files_title:
                blocks.append(InputRichBlockSectionHeading(text=self.files_title, size=2))
            blocks.extend(documents)
        if self.embedded_buttons:
            for index, row in enumerate(rows):
                back = bool(row and index == len(rows) - 1 and
                            ((self.navigation and len(row) == 1) or
                             any(button.text in BACK_LABELS for button in row)))
                if back and blocks[-1].type != 'divider':
                    blocks.append(InputRichBlockDivider())
                blocks.extend(rich_buttons([row], navigation=back))
        elif rows and len(rows[-1]) == 1 and rows[-1][0].text in BACK_LABELS and blocks[-1].type != 'divider':
            blocks.append(InputRichBlockDivider())
        return InputRichMessage(blocks=blocks, skip_entity_detection=True)

    def plain(self) -> str:
        lines = [' › '.join([p.label for p in self.breadcrumbs] + [self.title]), *self.lines]
        def append_sections(sections):
            for section in sections:
                lines.extend((section.title, *section.lines))
                lines.extend(''.join(item.text if isinstance(item, InlineKeyboardButton) else item
                    for item in row) for row in section.inline_rows)
                for table in section.tables:
                    lines.extend(table.plain())
                append_sections(section.sections)
        append_sections(self.sections)
        if self.uri:
            lines.append(self.uri)
        if self.details_title and self.details_lines:
            lines.extend((self.details_title, *self.details_lines))
        return '\n\n'.join(line for line in lines if line)

    def fallback_rows(self, rows):
        def section_rows(sections):
            for section in sections:
                yield from (list(row) for row in section.heading_rows)
                for row in section.inline_rows:
                    buttons = [item for item in row if isinstance(item, InlineKeyboardButton)]
                    if buttons:
                        yield buttons
                for table in section.tables:
                    for row, callback in zip(table.rows, table.row_callbacks):
                        yield [InlineKeyboardButton(text=row[0], callback_data=callback)]
                yield from section_rows(section.sections)
                yield from (list(row) for row in section.rows)
        parents = [[InlineKeyboardButton(text=p.label, callback_data=p.callback)
                    for p in self.breadcrumbs]] if self.breadcrumbs else []
        return [*parents, *section_rows(self.sections), *(list(row) for row in self.uri_rows), *rows]

    def plain_entities(self) -> list[MessageEntity] | None:
        if not self.uri:
            return None
        text = self.plain()
        offset = len(text[:text.index(self.uri)].encode('utf-16-le')) // 2
        return [MessageEntity(type='code', offset=offset,
            length=len(self.uri.encode('utf-16-le')) // 2)]
