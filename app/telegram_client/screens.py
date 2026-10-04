"""Rich Message screen construction; never use Markdown parse mode."""
from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import (InputRichBlockDetails, InputRichBlockParagraph,
    InputRichBlockSectionHeading, InputRichMessage, InputRichBlockPhoto,
    InputRichBlockDocument, InputMediaPhoto, InputMediaDocument, BufferedInputFile,
    RichTextCode, MessageEntity)
from aiogram.types import (InputRichBlockButtons, RichMessageButton, InlineKeyboardButton,
    InputRichBlockDivider, InputRichBlockTable, RichBlockTableCell)


def server_label(node: dict) -> str:
    return f"{node.get('flag') or ''} {node['title']}".strip()


BACK_LABELS = {'🔙 Back', '🔙 Назад', 'Back', 'Назад'}


def rich_buttons(rows, *, navigation=False):
    return [InputRichBlockButtons(buttons=[RichMessageButton(
        text=button.text, callback_data=button.callback_data, url=button.url,
        copy_text=button.copy_text, style='link' if navigation and button.callback_data and
            (len(row) == 1 or button.text in BACK_LABELS) else button.style)
        for button in row]) for row in rows if row]


@dataclass(frozen=True)
class Table:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]

    def rich(self):
        return InputRichBlockTable(is_compact=True, is_striped=True, cells=[
            [RichBlockTableCell(text=value, align='left', valign='top', is_header=index == 0)
             for value in row] for index, row in enumerate((self.headers, *self.rows))])

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
    heading_size: int = 3
    is_open: bool = False
    tables: tuple[Table, ...] = ()
    heading_rows: tuple[tuple[InlineKeyboardButton, ...], ...] = ()

    def rich(self):
        blocks = [InputRichBlockParagraph(text=line) for line in self.lines if line]
        blocks.extend(table.rich() for table in self.tables)
        for section in self.sections:
            blocks.extend(section.rich())
        blocks.extend(rich_buttons(self.rows))
        if self.collapsed:
            return [InputRichBlockDetails(summary=self.title,
                blocks=[*rich_buttons(self.heading_rows), *blocks], is_open=self.is_open)]
        return [InputRichBlockSectionHeading(text=self.title, size=self.heading_size),
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
    sections: tuple[Section, ...] = ()
    embedded_buttons: bool = False
    navigation: bool = False

    def rich(self, rows=()) -> InputRichMessage:
        blocks = [InputRichBlockSectionHeading(text=self.title, size=2)]
        blocks.extend(InputRichBlockParagraph(text=line) for line in self.lines if line)
        for section in self.sections:
            blocks.extend(section.rich())
        if self.qr and self.qr_title:
            blocks.append(InputRichBlockDetails(summary=self.qr_title, is_open=False,
                blocks=[InputRichBlockPhoto(photo=InputMediaPhoto(
                    media=BufferedInputFile(self.qr, 'config.png')))]))
        if self.uri:
            uri = InputRichBlockParagraph(text=RichTextCode(text=self.uri))
            blocks.append(InputRichBlockDetails(summary=self.uri_title, blocks=[uri], is_open=False)
                          if self.uri_title else uri)
        documents = [InputRichBlockDocument(document=InputMediaDocument(
            media=BufferedInputFile(content, filename))) for filename, content in self.files]
        if documents:
            blocks.extend([InputRichBlockDetails(summary=self.files_title, blocks=documents, is_open=False)]
                          if self.files_title else documents)
        if self.details_title and self.details_lines:
            blocks.append(InputRichBlockDetails(summary=self.details_title,
                blocks=[InputRichBlockParagraph(text=line) for line in self.details_lines]))
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
        lines = [self.title, *self.lines]
        def append_sections(sections):
            for section in sections:
                lines.extend((section.title, *section.lines))
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
                yield from section_rows(section.sections)
                yield from (list(row) for row in section.rows)
        return [*section_rows(self.sections), *rows]

    def plain_entities(self) -> list[MessageEntity] | None:
        if not self.uri:
            return None
        text = self.plain()
        offset = len(text[:text.index(self.uri)].encode('utf-16-le')) // 2
        return [MessageEntity(type='code', offset=offset,
            length=len(self.uri.encode('utf-16-le')) // 2)]
